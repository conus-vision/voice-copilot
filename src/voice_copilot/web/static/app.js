// Unified client for voice-copilot's web UI.
//
// One page, multiple tabs. Controls live in the topbar at all times so the
// user can pause / resume / speak / interrupt regardless of which tab is
// active. The same page runs in two modes:
//   * full  — everything below the topbar is visible (Launch + Trace + settings)
//   * mini  — a compact popup-window view: only topbar + controls remain
//     (triggered by ?mini=1 or by opening via the popout button).
//
// Voice input (mic → STT → inject) is gated on /api/info's
// `voice_input_enabled`: when the server has it off, every [data-voice-input]
// element stays hidden and the mic is never wired up.

(() => {
  const qs  = (sel) => document.querySelector(sel);
  const qsa = (sel) => document.querySelectorAll(sel);

  const isMini =
    new URLSearchParams(location.search).get("mini") === "1" ||
    location.pathname.replace(/\/+$/, "") === "/mini";
  if (isMini) document.body.classList.add("mini");

  const dot        = qs("#conn-dot");
  const connLabel  = qs("#conn-label");
  const player     = qs("#tts-player");
  const playpause  = qs("#btn-playpause");
  const muteBtn    = qs("#btn-mute");
  const speakBtn   = qs("#btn-speak");
  const interrupt  = qs("#btn-interrupt");
  const skipBtn    = qs("#btn-skip");
  const holdBtn    = qs("#btn-hold");
  const rateBtns   = [...qsa("[data-playback-rate]")];
  const popout     = qs("#btn-popout");
  const picker     = qs("#session-picker");
  const trace      = qs("#trace");
  const tracePause = qs("#btn-trace-pause");
  const traceClear = qs("#btn-trace-clear");
  const traceAuto  = qs("#trace-autoscroll");
  const promptToggle = qs("#trace-show-prompt");
  const traceStats = qs("#trace-stats");
  const saveInd    = qs("#save-indicator");
  const form       = qs("#settings-form");
  const secretsList = qs("#secrets-list");
  const proxyCliList = qs("#proxy-cli-list");
  const proxyCliSummary = qs("#proxy-cli-summary");
  const proxyCliWorkingDirectoryInput = qs('[name="proxy_cli.working_directory"]');
  const proxyCliWorkingDirectoryPicker = qs("[data-cli-pick-global-dir]");
  const cliFilter  = qs("#cli-filter");
  const ttsTestBtn = qs("#btn-test-tts");
  const sttTestBtn = qs("#btn-test-stt");
  const ttsTestOutput = qs("#speech-test-tts-output");
  const speechTranscript = qs("#speech-test-transcript");
  const llmTestOutput = qs("#llm-test-output");

  // ------------------------------------------------------------------ proxy port in the Help tab

  let voiceInputEnabled = false;

  fetch("/api/info").then(r => r.json()).then(({ proxy_port, launch_notice, voice_input_enabled }) => {
    if (proxy_port) {
      document.querySelectorAll(".pport").forEach(el => { el.textContent = proxy_port; });
    }
    if (launch_notice) {
      const banner = qs("#launch-notice");
      if (banner) { banner.textContent = launch_notice; banner.hidden = false; }
    }
    voiceInputEnabled = !!voice_input_enabled;
    applyVoiceInputMode();
  }).catch(() => {});

  // Show the mic, its hotkey and the speech-input settings only when the server
  // says voice input is on; the About note explains the gap when it is off.
  function applyVoiceInputMode() {
    qsa("[data-voice-input]").forEach(el => { el.hidden = !voiceInputEnabled; });
    const note = qs("[data-voice-note]");
    if (note) note.hidden = voiceInputEnabled;
    if (voiceInputEnabled) wireMic();
  }

  // ------------------------------------------------------------------ tabs

  function activateTab(name) {
    qsa(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
    qsa(".panel").forEach(p => p.classList.toggle("active", p.dataset.panel === name));
    try { localStorage.setItem("vc.tab", name); } catch {}
  }
  qsa(".tab").forEach(t => t.addEventListener("click", () => activateTab(t.dataset.tab)));
  // Tabs collapsed from eight to four — map anything a returning browser has
  // stored onto the panel that absorbed it.
  const TAB_ALIASES = {
    tts: "settings", stt: "settings", speech: "settings", llm: "settings",
    keys: "settings", proxy: "launch", instructions: "help", about: "help",
  };
  try {
    const saved = localStorage.getItem("vc.tab");
    const restored = TAB_ALIASES[saved] || saved;
    if (restored && qs(`.panel[data-panel="${restored}"]`)) activateTab(restored);
  } catch {}

  // ------------------------------------------------------------------ ws

  let ws = null;
  let retryMs = 500;

  function setConn(state) {
    dot.dataset.state = state;
    connLabel.textContent =
      state === "connected"  ? "online" :
      state === "connecting" ? "connecting…" : "offline";
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }
  function sendBytes(data) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(data);
  }

  async function readApiResponse(res) {
    const raw = await res.text();
    if (!raw) return { ok: res.ok };
    try {
      return JSON.parse(raw);
    } catch {
      return {
        ok: res.ok,
        where: res.ok ? undefined : "http",
        error: raw.trim() || `${res.status} ${res.statusText}`,
      };
    }
  }

  function formatApiError(out, fallback = "failed") {
    const detail = out?.error || out?.detail || out?.message || fallback;
    return `${out?.where ? `${out.where}: ` : ""}${detail}`;
  }

  function setOutputBox(el, text) {
    if (!el) return;
    el.hidden = !text;
    el.textContent = text || "";
  }

  // Browsers refuse `play()` until the page has been interacted with, and the
  // panel opens on its own — so a user who never clicks in it gets a working
  // pipeline and total silence. Every rejection used to be swallowed; now it
  // surfaces, and the first click anywhere retries.
  const audioBlockedBtn = qs("#audio-blocked");
  let audioBlocked = false;

  function setAudioBlocked(blocked) {
    if (blocked === audioBlocked) return;
    audioBlocked = blocked;
    if (audioBlockedBtn) audioBlockedBtn.hidden = !blocked;
  }

  function startPlayback() {
    const attempt = player.play();
    if (!attempt?.catch) return;
    attempt.then(() => setAudioBlocked(false)).catch((err) => {
      if (err?.name === "NotAllowedError") {
        setAudioBlocked(true);
        console.warn("voice-copilot: browser blocked narration audio until you interact with the page");
      } else {
        console.warn("voice-copilot: narration playback failed", err);
      }
    });
  }

  function retryBlockedPlayback() {
    if (!audioBlocked) return;
    setAudioBlocked(false);
    if (player.src && player.paused) startPlayback();
    else if (!playing && !paused) playNext();
  }

  audioBlockedBtn?.addEventListener("click", retryBlockedPlayback);
  document.addEventListener("pointerdown", retryBlockedPlayback);
  document.addEventListener("keydown", retryBlockedPlayback);

  function setIcon(el, iconName) {
    const icon = el?.querySelector?.(".material-symbols-rounded");
    if (icon) icon.textContent = iconName;
  }

  function setButtonText(el, text) {
    const label = el?.querySelector?.(".icon-label");
    if (label) label.textContent = text;
  }

  function setButtonHint(el, text) {
    if (!el || !text) return;
    el.title = text;
    el.setAttribute("aria-label", text);
  }

  // ------------------------------------------------------------------ playback / state

  const NO_SESSION_KEY = "__no_session__";
  let currentUtt = null;
  const playQueues = new Map();
  let playing    = false;
  let paused     = false;
  let muted      = false;
  let playbackRate = 1.2;
  let currentPlayerUrl = null;
  let currentPlaybackItem = null;
  let tracePaused = false;
  // Show what the commentator was actually asked. On by default — it is
  // the only window into why a narration came out the way it did.
  let showPrompts = true;
  try {
    showPrompts = localStorage.getItem("vc.tracePrompts") !== "0";
  } catch {}
  let selectedSessionId = null;
  let selectionPausedSessionId = null;
  let sessionsCache = [];

  try {
    const savedRate = Number(localStorage.getItem("vc.playbackRate"));
    if (Number.isFinite(savedRate) && savedRate > 0) playbackRate = savedRate;
  } catch {}

  // Sync pause/mute state between main window and mini popup via BroadcastChannel.
  const bc = new BroadcastChannel("vc-ui");
  bc.onmessage = (e) => {
    if (e.data.type !== "state") return;
    paused = e.data.paused;
    muted  = e.data.muted;
    if (typeof e.data.playbackRate === "number") playbackRate = e.data.playbackRate;
    // Only update DOM — do NOT call refreshPlayButton() which would re-broadcast
    // and create an infinite ping-pong between main and mini window.
    applyPlayButton();
    applyMuteButton();
    applyPlaybackRate();
  };

  // The button shows a live green dot while narration is running and the
  // familiar play arrow once paused, so a glance says whether the commentator
  // is working. Both children stay in the DOM; only visibility flips.
  const playGlyph = playpause.querySelector(".material-symbols-rounded");
  const playDot = document.createElement("span");
  playDot.className = "live-dot";
  playDot.setAttribute("aria-hidden", "true");
  playpause.prepend(playDot);

  function applyPlayButton() {
    // DOM-only update — no broadcast. Called from bc.onmessage to avoid ping-pong.
    playpause.classList.toggle("running", !paused);
    playpause.classList.toggle("active", !paused);
    if (playGlyph) playGlyph.hidden = !paused;
    playDot.hidden = paused;
    setIcon(playpause, "play_arrow");
    setButtonHint(playpause, paused ? "Resume narration" : "Pause narration");
  }

  function applyMuteButton() {
    player.muted = muted;
    muteBtn.classList.toggle("active", muted);
    setIcon(muteBtn, muted ? "volume_off" : "volume_up");
    setButtonHint(muteBtn, muted ? "Unmute narration" : "Mute narration");
  }

  function applyPromptToggle() {
    if (promptToggle) promptToggle.checked = showPrompts;
  }

  function applyTracePauseButton() {
    setIcon(tracePause, tracePaused ? "play_circle" : "pause_circle");
    setButtonText(tracePause, tracePaused ? "Resume" : "Pause");
    tracePause.classList.toggle("active", tracePaused);
  }

  function refreshPlayButton() {
    applyPlayButton();
    bc.postMessage({ type: "state", paused, muted, playbackRate });
  }

  function applyPlaybackRate() {
    player.defaultPlaybackRate = playbackRate;
    player.playbackRate = playbackRate;
    rateBtns.forEach((btn) => {
      btn.classList.toggle("active", Number(btn.dataset.playbackRate) === playbackRate);
    });
    try { localStorage.setItem("vc.playbackRate", String(playbackRate)); } catch {}
  }

  function activeNarrationSessionId() {
    return selectedSessionId || undefined;
  }

  function syncPlaybackRateState() {
    send({
      type: "cmd",
      cmd: "playback_rate",
      playback_rate: playbackRate,
      session_id: activeNarrationSessionId(),
    });
  }

  function markPlaybackReady(reason) {
    if (!currentPlaybackItem || currentPlaybackItem.kind !== "narration") return;
    if (currentPlaybackItem.readyReported) return;
    currentPlaybackItem.readyReported = true;
    send({
      type: "cmd",
      cmd: "playback_ready",
      reason,
      playback_rate: playbackRate,
      session_id: currentPlaybackItem.sessionId || activeNarrationSessionId(),
      utterance_id: currentPlaybackItem.utteranceId,
    });
  }

  function clearPlayerSource({ revoke = true } = {}) {
    const url = currentPlayerUrl;
    currentPlayerUrl = null;
    currentPlaybackItem = null;
    player.onended = null;
    try { player.pause(); } catch {}
    player.removeAttribute("src");
    player.load();
    if (revoke && url) URL.revokeObjectURL(url);
  }

  function mimeForAudioFormat(format) {
    return format === "mp3" ? "audio/mpeg"
      : format === "ogg" ? "audio/ogg"
      : format === "wav" ? "audio/wav"
      : "audio/webm";
  }

  function decodeBase64(base64Text) {
    const raw = atob(base64Text);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
    return bytes;
  }

  function formatBytes(value) {
    const size = Number(value || 0);
    if (!Number.isFinite(size) || size <= 0) return "0 B";
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  function formatAge(seconds) {
    if (!Number.isFinite(seconds) || seconds < 1) return "just now";
    if (seconds < 60) return `${Math.round(seconds)}s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
    return `${Math.round(seconds / 3600)}h ago`;
  }

  function sessionKey(sessionId) {
    return typeof sessionId === "string" && sessionId ? sessionId : NO_SESSION_KEY;
  }

  function sessionIdEquals(left, right) {
    return sessionKey(left) === sessionKey(right);
  }

  function messageSessionId(msg) {
    const sessionId = msg?.payload?.session_id;
    return typeof sessionId === "string" && sessionId ? sessionId : undefined;
  }

  function getPlaybackQueue(sessionId) {
    const key = sessionKey(sessionId);
    let queue = playQueues.get(key);
    if (!queue) {
      queue = [];
      playQueues.set(key, queue);
    }
    return queue;
  }

  function clearQueuedPlayback({ revoke = true } = {}) {
    for (const queue of playQueues.values()) {
      queue.forEach((item) => {
        if (revoke && item?.url) URL.revokeObjectURL(item.url);
      });
    }
    playQueues.clear();
  }

  function clearPlaybackQueue(sessionId, { revoke = true } = {}) {
    const key = sessionKey(sessionId);
    const queue = playQueues.get(key);
    if (!queue) return;
    queue.forEach((item) => {
      if (revoke && item?.url) URL.revokeObjectURL(item.url);
    });
    playQueues.delete(key);
  }

  function sessionOptionText(session, { includeCli = true } = {}) {
    const query = (session?.last_query || "").trim();
    const head = query ? short(query, 60) : "(no query yet)";
    const cli = session?.label || "session";
    return includeCli
      ? `${head} — ${cli} (${session?.provider || "proxy"}) · ${session.id}`
      : `${head} · ${session.id}`;
  }

  // Session labels come from the client's user-agent, so match on substrings.
  // Order matters: longer/more specific needles first. Short ids that hide
  // inside other names ("pi" lives in "copilot") are deliberately absent.
  const SESSION_PROFILE_HINTS = [
    ["openclaw", "openclaw"], ["opencode", "opencode"], ["hermes-desktop", "hermes-desktop"],
    ["hermes", "hermes"], ["deepseek", "dsh"], ["oh-my-pi", "omp"], ["cursor", "cursor"],
    ["openhands", "openhands"], ["claude", "claude"], ["codex", "codex"], ["copilot", "copilot"],
    ["github cli", "copilot"], ["aider", "aider"], ["kimi", "kimi"], ["droid", "droid"],
    ["cline", "cline"], ["qwen", "qwen"], ["gemini", "gemini"], ["crush", "crush"],
    ["goose", "goose"], ["auggie", "auggie"], ["grok", "grok"], ["amp", "amp"],
    ["omp", "omp"], ["dsh", "dsh"],
  ];

  function profileIdForSession(session) {
    const raw = `${session?.cli_id || ""} ${session?.label || ""} ${session?.user_agent || ""}`.toLowerCase();
    if (!raw.trim()) return null;
    for (const [needle, profileId] of SESSION_PROFILE_HINTS) {
      if (raw.includes(needle)) return profileId;
    }
    return null;
  }

  function setCliActivity(profileId, state, text) {
    const textEl = document.querySelector(`[data-cli-activity="${profileId}"]`);
    if (textEl) textEl.textContent = text;
    const openDotEl = document.querySelector(`[data-cli-open-dot="${profileId}"]`);
    if (openDotEl) openDotEl.dataset.state = state;
    // The activity strip only earns its row once traffic has been seen.
    const rowEl = document.querySelector(`[data-cli-activity-row="${profileId}"]`);
    if (rowEl) rowEl.hidden = state === "none";
  }

  function applyProxySessionActivity(sessions = []) {
    qsa("[data-cli-activity]").forEach((el) => {
      const profileId = el.dataset.cliActivity;
      setCliActivity(profileId, "none", "");
    });

    const selectedByProfile = new Map();
    const selectedId = activeNarrationSessionId();
    if (selectedId) {
      const selectedSession = (sessions || []).find((session) => session.id === selectedId);
      const selectedProfileId = profileIdForSession(selectedSession);
      if (selectedSession && selectedProfileId) selectedByProfile.set(selectedProfileId, selectedSession);
    }

    const latestByProfile = new Map();
    for (const session of sessions || []) {
      const profileId = profileIdForSession(session);
      if (!profileId) continue;
      if (selectedByProfile.has(profileId)) continue;
      const prev = latestByProfile.get(profileId);
      if (!prev || Number(session.last_seen || 0) > Number(prev.last_seen || 0)) {
        latestByProfile.set(profileId, session);
      }
    }

    for (const [profileId, session] of selectedByProfile.entries()) {
      latestByProfile.set(profileId, session);
    }

    const now = Date.now() / 1000;
    for (const [profileId, session] of latestByProfile.entries()) {
      const age = Math.max(0, now - Number(session.last_seen || 0));
      const state = age < 5 ? "live" : "idle";
      const method = session.last_method || "REQ";
      const path = session.last_path || session.provider || "proxy";
      const size = formatBytes(session.last_request_bytes);
      const query = session.last_query ? ` | ${short(session.last_query, 42)}` : "";
      setCliActivity(profileId, state, `${method} ${path} | ${size} | ${formatAge(age)}${query}`);
    }
  }

  function playPreviewAudio(audioBase64, format) {
    stopPlayback();
    const url = URL.createObjectURL(new Blob([decodeBase64(audioBase64)], { type: mimeForAudioFormat(format) }));
    currentPlayerUrl = url;
    currentPlaybackItem = null;
    player.onended = () => {
      currentPlayerUrl = null;
      currentPlaybackItem = null;
      URL.revokeObjectURL(url);
      playing = false;
      refreshPlayButton();
    };
    player.src = url;
    playing = true;
    refreshPlayButton();
    applyPlaybackRate();
    startPlayback();
  }

  function queueBlobUrl(item) {
    getPlaybackQueue(item.sessionId).push(item);
    if (selectionPausedSessionId) return;
    if (!currentPlaybackItem && !playing && !paused && sessionIdEquals(item.sessionId, activeNarrationSessionId())) {
      playNext(item.sessionId);
    }
  }
  function playNext(sessionId = activeNarrationSessionId()) {
    const item = getPlaybackQueue(sessionId).shift();
    if (!item?.url) {
      playing = false;
      refreshPlayButton();
      return;
    }
    playing = true;
    currentPlaybackItem = item;
    currentPlaybackItem.readyReported = false;
    currentPlayerUrl = item.url;
    selectionPausedSessionId = null;
    refreshPlayButton();
    player.src = item.url;
    applyPlaybackRate();
    startPlayback();
    player.onended = () => {
      const finishedItem = currentPlaybackItem;
      if (finishedItem?.kind === "narration" && !finishedItem.readyReported) {
        markPlaybackReady("eighty_percent");
      }
      currentPlayerUrl = null;
      currentPlaybackItem = null;
      if (finishedItem?.url) URL.revokeObjectURL(finishedItem.url);
      if (!paused && !selectionPausedSessionId) playNext(finishedItem?.sessionId || activeNarrationSessionId());
      else refreshPlayButton();
    };
  }
  function stopPlayback() {
    selectionPausedSessionId = null;
    clearPlayerSource();
    clearQueuedPlayback();
    playing = false;
    if (currentUtt) currentUtt.aborted = true;
    refreshPlayButton();
  }

  function stopPlaybackForSession(sessionId) {
    if (!sessionId) {
      stopPlayback();
      return;
    }

    let changed = false;
    if (currentPlaybackItem && sessionIdEquals(currentPlaybackItem.sessionId, sessionId)) {
      selectionPausedSessionId = null;
      clearPlayerSource();
      playing = false;
      changed = true;
    }
    if (currentUtt && sessionIdEquals(currentUtt.sessionId, sessionId)) {
      currentUtt.aborted = true;
      currentUtt = null;
    }
    clearPlaybackQueue(sessionId);
    if (changed) refreshPlayButton();
  }

  function skipCurrentPlayback() {
    const skippedItem = currentPlaybackItem;
    const hadCurrent = !!currentPlayerUrl;
    if (skippedItem?.kind === "narration") markPlaybackReady("skipped");
    selectionPausedSessionId = null;
    clearPlayerSource();
    playing = false;
    refreshPlayButton();
    if (hadCurrent && !paused) playNext();
  }

  player.addEventListener("play",  () => { playing = true;  refreshPlayButton(); });
  player.addEventListener("pause", () => { if (player.ended) return; /* keep .playing for queue progress */ });
  player.addEventListener("ended", () => { /* handled by player.onended */ });
  player.addEventListener("timeupdate", () => {
    if (!currentPlaybackItem || currentPlaybackItem.kind !== "narration") return;
    if (currentPlaybackItem.readyReported) return;
    const duration = Number(player.duration);
    if (!Number.isFinite(duration) || duration <= 0) return;
    if ((player.currentTime / duration) >= 0.8) markPlaybackReady("eighty_percent");
  });

  function handleSessionSelectionPlayback(nextSessionId) {
    const hasCurrentNarration = !!currentPlayerUrl && currentPlaybackItem?.kind === "narration";
    if (hasCurrentNarration && !sessionIdEquals(currentPlaybackItem.sessionId, nextSessionId)) {
      selectionPausedSessionId = currentPlaybackItem.sessionId || null;
      try { player.pause(); } catch {}
      playing = false;
      refreshPlayButton();
      return;
    }
    if (hasCurrentNarration && selectionPausedSessionId && sessionIdEquals(selectionPausedSessionId, nextSessionId)) {
      selectionPausedSessionId = null;
      if (!paused) {
        applyPlaybackRate();
        startPlayback();
        playing = true;
      }
      refreshPlayButton();
      return;
    }
    if (!hasCurrentNarration && !paused && !selectionPausedSessionId) {
      playNext(nextSessionId);
      return;
    }
    refreshPlayButton();
  }

  playpause.addEventListener("click", () => {
    if (paused) {
      paused = false;
      send({ type: "cmd", cmd: "play" });
      if (selectionPausedSessionId && currentPlaybackItem?.url && sessionIdEquals(currentPlaybackItem.sessionId, activeNarrationSessionId())) {
        selectionPausedSessionId = null;
        applyPlaybackRate();
        startPlayback();
        playing = true;
      } else if (!selectionPausedSessionId && !playing && currentPlaybackItem?.url && player.paused) {
        startPlayback();
        playing = true;
      } else if (!selectionPausedSessionId && !playing) {
        playNext();
      } else if (!selectionPausedSessionId && player.paused && player.src) {
        startPlayback();
        playing = true;
      }
    } else {
      paused = true;
      send({ type: "cmd", cmd: "pause" });
      playing = false;
      try { player.pause(); } catch {}
    }
    refreshPlayButton();
  });
  refreshPlayButton();
  applyMuteButton();
  applyTracePauseButton();
  applyPromptToggle();

  muteBtn.addEventListener("click", () => {
    muted = !muted;
    send({ type: "cmd", cmd: muted ? "mute" : "unmute" });
    applyMuteButton();
    bc.postMessage({ type: "state", paused, muted });
  });
  interrupt.addEventListener("click", () => send({ type: "cmd", cmd: "interrupt" }));

  // Supervisor+ (or the interrupt button / alt+p) left the agent suspended;
  // the banner is the panel's way to let it go again.
  const agentPausedBox = qs("#agent-paused");
  const agentPausedText = qs("#agent-paused-text");
  const agentResumeBtn = qs("#btn-agent-resume");
  function showAgentPaused(payload) {
    if (!agentPausedBox) return;
    if (!payload) { agentPausedBox.hidden = true; return; }
    const reason = payload.reason === "supervisor" ? "Supervisor paused the agent"
                 : payload.reason === "hold_while_narrating" ? null
                 : `Agent paused (${payload.reason || "manual"})`;
    if (!reason) return;  // the narration hold is transient — no banner
    agentPausedText.textContent = reason;
    agentPausedBox.hidden = false;
  }
  agentResumeBtn?.addEventListener("click", () => send({ type: "cmd", cmd: "agent_toggle_pause" }));

  // Hold the agent for as long as a line is being read, so you can follow it
  // instead of racing it. Per-browser preference; the server forgets it when
  // the run ends, so we re-send on every (re)connect.
  let holdWhileNarrating = false;
  try { holdWhileNarrating = localStorage.getItem("vc.holdWhileNarrating") === "1"; } catch {}

  function applyHoldButton() {
    if (!holdBtn) return;
    holdBtn.classList.toggle("active", holdWhileNarrating);
    holdBtn.setAttribute("aria-pressed", String(holdWhileNarrating));
    setButtonHint(
      holdBtn,
      holdWhileNarrating ? "Agent runs freely while narrating" : "Hold the agent while a line is read",
    );
  }

  function syncHoldWhileNarrating() {
    send({ type: "cmd", cmd: "hold_while_narrating", enabled: holdWhileNarrating });
  }

  holdBtn?.addEventListener("click", () => {
    holdWhileNarrating = !holdWhileNarrating;
    try { localStorage.setItem("vc.holdWhileNarrating", holdWhileNarrating ? "1" : "0"); } catch {}
    applyHoldButton();
    syncHoldWhileNarrating();
  });
  applyHoldButton();
  skipBtn.addEventListener("click", skipCurrentPlayback);
  rateBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      const nextRate = Number(btn.dataset.playbackRate);
      if (!Number.isFinite(nextRate) || nextRate <= 0) return;
      playbackRate = playbackRate === nextRate ? 1 : nextRate;
      applyPlaybackRate();
      syncPlaybackRateState();
      bc.postMessage({ type: "state", paused, muted, playbackRate });
    });
  });
  applyPlaybackRate();

  // ------------------------------------------------------------------ connect

  function connect() {
    setConn("connecting");
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.binaryType = "arraybuffer";

    ws.onopen  = () => {
      setConn("connected");
      retryMs = 500;
      syncPlaybackRateState();
      syncHoldWhileNarrating();
      send({ type: "cmd", cmd: "panel_focus", focused: document.hasFocus() });
    };
    ws.onclose = () => { setConn("disconnected"); setTimeout(connect, retryMs); retryMs = Math.min(retryMs*2, 5000); };
    ws.onerror = () => {};

    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        let msg; try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === "event") {
          if (!isMini && msg.kind === "user.skip.requested") {
            skipCurrentPlayback();
            return;
          }
          if (!isMini && voiceInputEnabled && msg.kind === "user.speak.requested") {
            const phase = (msg.payload || {}).phase;
            if (phase === "start" && !speaking) { speaking = true; startSpeak(); }
            else if (phase === "end" && speaking) { speaking = false; endSpeak(); }
            return;
          }
          if (!isMini && isNewHumanQuery(msg)) stopPlaybackForSession(messageSessionId(msg));
          if (msg.kind === "agent.paused") showAgentPaused(msg.payload || {});
          if (msg.kind === "agent.resumed") showAgentPaused(null);
          traceAppend(msg);
        } else if (!isMini && msg.type === "audio_header") {
          currentUtt = {
            id: msg.utterance_id,
            format: msg.format || "mp3",
            chunks: [],
            aborted: false,
            sessionId: msg.session_id || null,
            queryVersion: Number.isInteger(msg.query_version) ? msg.query_version : null,
          };
        } else if (!isMini && msg.type === "audio_end") {
          if (!currentUtt) return;
          if (!currentUtt.aborted && !msg.aborted && !msg.error && currentUtt.chunks.length) {
            const mime = currentUtt.format === "mp3" ? "audio/mpeg"
                       : currentUtt.format === "ogg" ? "audio/ogg"
                       : currentUtt.format === "wav" ? "audio/wav"
                       : "audio/webm";
            const blob = new Blob(currentUtt.chunks, { type: mime });
            queueBlobUrl({
              url: URL.createObjectURL(blob),
              kind: "narration",
              utteranceId: currentUtt.id,
              sessionId: currentUtt.sessionId,
              queryVersion: currentUtt.queryVersion,
              readyReported: false,
            });
          }
          currentUtt = null;
        } else if (!isMini && msg.type === "audio_interrupt") {
          stopPlayback();
          currentUtt = null;
        }
      } else if (!isMini) {
        // Binary audio frame — only the main window assembles audio.
        if (currentUtt) currentUtt.chunks.push(ev.data);
      }
    };
  }
  connect();

  window.addEventListener("focus", () => send({ type: "cmd", cmd: "panel_focus", focused: true }));
  window.addEventListener("blur",  () => send({ type: "cmd", cmd: "panel_focus", focused: false }));

  // ------------------------------------------------------------------ trace

  const TRACE_MAX = 500;
  const traceStates = new Map();

  // Blocks a CLI wraps around the human's turn, and the give-away phrases of
  // requests the CLI makes for itself (title generation, quota probes, the
  // safety classifier). Nothing is dropped — rows that match are folded away
  // so the real conversation stays readable. Mirrors clean_user_query() in
  // commentator/pipeline.py.
  const SCAFFOLD_BLOCK_RE =
    /<(system-reminder|recommended_plugins|transcript|session|command-name|command-message|local-command-stdout)>[\s\S]*?<\/\1>/gi;
  const SCAFFOLD_MARKERS = [
    "write the title in the predominant language",
    "respond with <severity>",
    "your entire response must begin with <block>",
    "stage 1 does not apply user intent",
    "analyze if this message indicates a new conversation topic",
    "please write a 5-10 word title",
    // codex: "Generate a concise, single-line task title of at most 36 characters…"
    "single-line task title",
  ];

  // Playback must stop for a NEW human question and for nothing else. Codex
  // re-sends the whole conversation on every request of a turn — so the same
  // question arrives again and again — and asks the model for a session title
  // over the same session. Each of those used to cut the narration off while
  // it was still being assembled, which is why "Test voice" worked and real
  // narration never did. Kept apart from the Trace's fold state on purpose.
  const lastHumanQueryBySession = new Map();
  function isNewHumanQuery(msg) {
    if (!isAgentQuery(msg)) return false;
    const cleaned = stripScaffoldBlocks(msg.payload?.text);
    if (!cleaned) return false;
    const lowered = cleaned.toLowerCase();
    if (SCAFFOLD_MARKERS.some((m) => lowered.includes(m))) return false;
    if (lowered === "quota" || lowered === "ping") return false;
    const key = sessionKey(messageSessionId(msg));
    if (lastHumanQueryBySession.get(key) === cleaned) return false;
    lastHumanQueryBySession.set(key, cleaned);
    return true;
  }

  function stripScaffoldBlocks(text) {
    return String(text || "").replace(SCAFFOLD_BLOCK_RE, " ").trim();
  }

  function isFoldableUserText(text, state) {
    const cleaned = stripScaffoldBlocks(text);
    if (!cleaned) return true;                      // nothing but injected blocks
    const lowered = cleaned.toLowerCase();
    if (SCAFFOLD_MARKERS.some((m) => lowered.includes(m))) return true;
    if (lowered === "quota" || lowered === "ping") return true;
    if (cleaned === state.lastUserText) return true; // same turn re-sent
    state.lastUserText = cleaned;
    return false;
  }

  function getTraceState(sessionId) {
    const key = sessionKey(sessionId);
    let state = traceStates.get(key);
    if (!state) {
      state = { items: [], mergeIndexByKey: new Map(), lastUserText: null };
      traceStates.set(key, state);
    }
    return state;
  }

  function rebuildTraceMergeIndex(state) {
    state.mergeIndexByKey = new Map();
    state.items.forEach((item, index) => {
      if (item.mergeKey && item.streaming) state.mergeIndexByKey.set(item.mergeKey, index);
    });
  }

  function traceRowCount(state) {
    return state.items.reduce((n, item) => n + (item.cls === "fold" ? item.items.length : 1), 0);
  }

  function buildPlainRow(item) {
    const row = document.createElement("div");
    row.className = `item ${item.cls}`;
    if (item.streaming) row.classList.add("muted");
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.innerHTML = `<span class="tag">${item.tag}</span><span>${item.timeLabel}</span>`;
    const body = document.createElement("div");
    body.className = "body";
    body.textContent = item.text;
    row.appendChild(meta);
    row.appendChild(body);
    return row;
  }

  function buildDisclosure(item, summaryHtml, className) {
    const details = document.createElement("details");
    details.className = `item ${className}`;
    details.open = !!item.open;
    const summary = document.createElement("summary");
    summary.innerHTML = summaryHtml;
    details.appendChild(summary);
    // Remember per-item so a re-render (one per event) doesn't snap it shut.
    details.addEventListener("toggle", () => { item.open = details.open; });
    return details;
  }

  function buildTraceRow(item) {
    if (item.cls === "fold") {
      const details = buildDisclosure(
        item,
        `<span class="tag">FOLDED</span><span>${item.items.length} repeated / service messages</span>`,
        "fold-item",
      );
      item.items.forEach((child) => details.appendChild(buildPlainRow(child)));
      return details;
    }
    if (item.cls === "prompt") {
      const details = buildDisclosure(
        item,
        `<span class="tag">PROMPT</span><span>${item.head}</span>`,
        "prompt-item",
      );
      const body = document.createElement("div");
      body.className = "body";
      body.textContent = item.text;
      details.appendChild(body);
      return details;
    }
    return buildPlainRow(item);
  }

  function visibleTraceItems(state) {
    return showPrompts ? state.items : state.items.filter((item) => item.cls !== "prompt");
  }

  function renderActiveTrace() {
    if (!trace) return;
    const state = getTraceState(activeNarrationSessionId());
    trace.innerHTML = "";
    visibleTraceItems(state).forEach((item) => trace.appendChild(buildTraceRow(item)));
    traceStats.textContent = `${traceRowCount(state)} items`;
    if (traceAuto.checked) trace.scrollTop = trace.scrollHeight;
  }

  function clearActiveTrace() {
    traceStates.set(sessionKey(activeNarrationSessionId()), {
      items: [],
      mergeIndexByKey: new Map(),
      lastUserText: null,
    });
    renderActiveTrace();
  }

  tracePause.addEventListener("click", () => {
    tracePaused = !tracePaused;
    applyTracePauseButton();
  });
  traceClear.addEventListener("click", () => {
    clearActiveTrace();
  });
  promptToggle?.addEventListener("change", () => {
    showPrompts = promptToggle.checked;
    try { localStorage.setItem("vc.tracePrompts", showPrompts ? "1" : "0"); } catch {}
    renderActiveTrace();
  });

  function traceAppend(msg) {
    if (tracePaused) return;
    const sessionId = messageSessionId(msg);
    const state = getTraceState(sessionId);
    const item = classifyForTrace(msg, state);
    if (!item) return;

    if (item.mergeKey && state.mergeIndexByKey.has(item.mergeKey)) {
      const existing = state.items[state.mergeIndexByKey.get(item.mergeKey)];
      // Streamed deltas accumulate; the closing utterance carries the whole
      // text and replaces them — appending it doubled every narration line.
      if (item.streaming) existing.text += item.text;
      else existing.text = item.text;
      existing.streaming = !!item.streaming;
      if (!item.streaming) state.mergeIndexByKey.delete(item.mergeKey);
      if (sessionIdEquals(sessionId, activeNarrationSessionId())) renderActiveTrace();
      return;
    }

    const entry = { ...item, sessionId, timeLabel: new Date().toLocaleTimeString() };

    if (item.fold) {
      // Keep every byte, just tuck the repeats under one disclosure.
      const last = state.items[state.items.length - 1];
      if (last && last.cls === "fold") last.items.push(entry);
      else state.items.push({ cls: "fold", items: [entry], open: false, sessionId });
    } else {
      state.items.push(entry);
      if (item.mergeKey && item.streaming) {
        state.mergeIndexByKey.set(item.mergeKey, state.items.length - 1);
      }
    }

    if (state.items.length > TRACE_MAX) {
      state.items.splice(0, state.items.length - TRACE_MAX);
      rebuildTraceMergeIndex(state);
    }

    if (sessionIdEquals(sessionId, activeNarrationSessionId())) renderActiveTrace();
  }

  function classifyForTrace(msg, state) {
    const p = msg.payload || {};
    switch (msg.kind) {
      case "user.message": {
        const text = (p.text || "").trim();
        if (!text) return null;
        // skip user talking to voice-copilot itself (STT output)
        if (p.delivery !== "observed") return null;
        return { cls: "query", tag: "USER", text, fold: isFoldableUserText(text, state) };
      }
      case "agent.thinking":
        return p.text ? { cls: "thinking", tag: "THINKING", text: p.text, fold: !!p.internal } : null;
      case "agent.text":
        return p.text ? { cls: "answer", tag: "AGENT", text: p.text, fold: !!p.internal } : null;
      case "commentator.prompt": {
        const body = `--- system ---\n${p.system || ""}\n\n--- user ---\n${p.user || ""}`;
        const model = p.model ? ` ${p.model}` : "";
        return {
          cls: "prompt",
          tag: "PROMPT",
          text: body,
          head: `to commentator · ${p.provider || "?"}${model} · ${p.event_count || 0} events · ${body.length} chars`,
          open: false,
        };
      }
      case "commentator.utterance": {
        const text = p.text || "";
        if (!text) return null;
        return {
          cls: "narration",
          tag: "NARRATION",
          text,
          streaming: !!p.streaming,
          mergeKey: p.utterance_id ? `utt:${p.utterance_id}` : null,
        };
      }
      case "tool.call.started":
        return { cls: "tool", tag: "TOOL", text: `${p.tool || "?"}: ${short(JSON.stringify(p.input || ""))}` };
      case "tool.call.finished":
        return { cls: "tool", tag: "TOOL", text: `${p.tool || "?"} ${p.is_error ? "FAILED" : "ok"}: ${short(p.preview || "")}` };
      case "file.edited":
        return { cls: "tool", tag: "FILE", text: p.path || "" };
      case "supervisor.verdict": {
        const status = String(p.status || "ok").toUpperCase();
        const head = p.paused_agent ? `${status} — agent paused` : status;
        const why = p.reason ? ` · ${p.reason}` : "";
        return {
          cls: p.status === "ok" ? "supervisor-ok" : "supervisor",
          tag: "SUPERVISOR",
          text: p.message ? `${head}: ${p.message}` : `${head}${why}`,
        };
      }
      case "agent.paused":
        return { cls: "supervisor", tag: "PAUSED", text: `agent paused (${p.reason || "?"})` };
      case "agent.resumed":
        return { cls: "supervisor-ok", tag: "RESUMED", text: `agent resumed (${p.reason || "?"})` };
      case "error":
        return { cls: "error", tag: "ERROR", text: p.message || JSON.stringify(p) };
      default:
        return null;
    }
  }
  function short(s, n = 200) {
    s = String(s || "");
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function sortProxyProfiles(profiles) {
    return [...(profiles || [])].sort((left, right) => {
      const leftMissing = !left?.resolved_binary;
      const rightMissing = !right?.resolved_binary;
      if (leftMissing !== rightMissing) return leftMissing ? 1 : -1;
      const byOrder = Number(left?.order ?? 0) - Number(right?.order ?? 0);
      if (byOrder) return byOrder;
      return String(left?.label || left?.id || "").localeCompare(String(right?.label || right?.id || ""));
    });
  }

  function applyHotkeyTitles(cfg) {
    const hotkeys = cfg?.hotkeys || {};
    const withHotkey = (label, combo) => combo ? `${label} (${combo})` : label;
    setButtonHint(speakBtn, withHotkey("Push-to-talk", hotkeys.push_to_talk));
    setButtonHint(interrupt, withHotkey("Interrupt agent", hotkeys.interrupt));
    setButtonHint(skipBtn, withHotkey("Skip current narration", hotkeys.skip_current));
  }

  function isAgentQuery(msg) {
    if (msg.kind !== "user.message") return false;
    const text = (msg.payload?.text || "").trim();
    if (!text) return false;
    const source = String(msg.source || "");
    return !source.startsWith("stt.")
        && !source.startsWith("web")
        && !source.startsWith("hotkey");
  }

  // ------------------------------------------------------------------ mic (push-to-talk)

  let recorder = null;
  let micStream = null;
  let speaking = false;
  let speechTestRecorder = null;
  let speechTestStream = null;
  let speechTestChunks = [];
  let speechTestContainer = "webm";
  let speechTestRecording = false;

  function pickMimeType() {
    const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/ogg"];
    for (const m of candidates) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) return m;
    }
    return "";
  }

  function containerForMimeType(mime) {
    return mime.startsWith("audio/ogg") ? "ogg"
      : mime.startsWith("audio/wav") ? "wav"
      : mime.startsWith("audio/mpeg") ? "mp3"
      : "webm";
  }

  async function startSpeak() {
    speakBtn.classList.add("speaking");
    stopPlayback();
    send({ type: "cmd", cmd: "speak_start" });
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      speakBtn.classList.remove("speaking");
      return;
    }
    const mime = pickMimeType();
    const codec = mime.startsWith("audio/webm") ? "webm" : mime.startsWith("audio/ogg") ? "ogg" : "webm";
    send({ type: "cmd", cmd: "mic_start", codec });
    recorder = new MediaRecorder(micStream, mime ? { mimeType: mime } : undefined);
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) e.data.arrayBuffer().then(sendBytes);
    };
    recorder.start(150);
  }

  function endSpeak() {
    speakBtn.classList.remove("speaking");
    send({ type: "cmd", cmd: "speak_end" });
    if (recorder && recorder.state !== "inactive") {
      recorder.onstop = () => {
        send({ type: "cmd", cmd: "mic_end" });
        if (micStream) micStream.getTracks().forEach(t => t.stop());
        micStream = null; recorder = null;
      };
      recorder.stop();
    } else {
      send({ type: "cmd", cmd: "mic_end" });
    }
  }

  let micWired = false;

  function wireMic() {
    if (micWired || !speakBtn) return;
    micWired = true;

    speakBtn.addEventListener("mousedown",  startSpeak);
    speakBtn.addEventListener("mouseup",    endSpeak);
    speakBtn.addEventListener("mouseleave", () => speakBtn.classList.contains("speaking") && endSpeak());
    speakBtn.addEventListener("touchstart", (e) => { e.preventDefault(); startSpeak(); }, { passive: false });
    speakBtn.addEventListener("touchend",   (e) => { e.preventDefault(); endSpeak(); });

    window.addEventListener("keydown", (e) => {
      if (e.altKey && e.code === "Space" && !speaking) { speaking = true; e.preventDefault(); startSpeak(); }
    });
    window.addEventListener("keyup", (e) => {
      if (speaking && (e.code === "Space" || e.key === "Alt")) { speaking = false; endSpeak(); }
    });
    const autoEndIfSpeaking = () => {
      if (speaking) { speaking = false; endSpeak(); }
      if (speakBtn.classList.contains("speaking")) endSpeak();
    };
    window.addEventListener("blur", autoEndIfSpeaking);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") autoEndIfSpeaking();
    });
  }

  // ------------------------------------------------------------------ sessions

  function renderTopSessionPicker() {
    if (!picker) return;
    if (!sessionsCache.length) {
      picker.hidden = true;
      picker.innerHTML = "";
      return;
    }
    picker.hidden = false;
    picker.innerHTML = "";
    for (const session of sessionsCache) {
      const opt = document.createElement("option");
      opt.value = session.id;
      opt.textContent = sessionOptionText(session);
      if (session.id === activeNarrationSessionId()) opt.selected = true;
      picker.appendChild(opt);
    }
    picker.value = activeNarrationSessionId() || "";
  }

  function syncProxySessionPickers() {
    qsa("[data-cli-session-picker]").forEach((select) => {
      const profileId = select.dataset.cliSessionPicker;
      const profileSessions = sessionsCache.filter((session) => profileIdForSession(session) === profileId);
      const row = select.closest("[data-cli-session-row]");
      if (row) row.hidden = profileSessions.length === 0;
      if (!profileSessions.length) {
        select.innerHTML = "";
        return;
      }
      select.innerHTML = "";
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = `Choose dialog for ${profileId}`;
      select.appendChild(placeholder);
      for (const session of profileSessions) {
        const opt = document.createElement("option");
        opt.value = session.id;
        opt.textContent = sessionOptionText(session, { includeCli: false });
        if (session.id === activeNarrationSessionId()) opt.selected = true;
        select.appendChild(opt);
      }
      if (!profileSessions.some((session) => session.id === activeNarrationSessionId())) {
        select.value = "";
      }
    });
  }

  function renderSessionControls() {
    renderTopSessionPicker();
    syncProxySessionPickers();
    applyProxySessionActivity(sessionsCache);
  }

  function applySessionSnapshot(sessions, active) {
    const prevSelectedId = activeNarrationSessionId();
    sessionsCache = sessions || [];
    selectedSessionId = sessionsCache.some((session) => session.id === active)
      ? active
      : sessionsCache[0]?.id || null;
    renderSessionControls();
    renderActiveTrace();
    if (!sessionIdEquals(prevSelectedId, selectedSessionId)) {
      handleSessionSelectionPlayback(selectedSessionId);
      syncPlaybackRateState();
    }
  }

  async function setActiveSession(nextSessionId) {
    if (!nextSessionId || sessionIdEquals(activeNarrationSessionId(), nextSessionId)) {
      renderSessionControls();
      return;
    }

    const previousSessionId = activeNarrationSessionId();
    selectedSessionId = nextSessionId;
    renderSessionControls();
    renderActiveTrace();
    handleSessionSelectionPlayback(nextSessionId);
    syncPlaybackRateState();

    const res = await fetch("/api/sessions/active", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ id: nextSessionId }),
    });
    if (!res.ok) {
      selectedSessionId = previousSessionId || null;
      renderSessionControls();
      renderActiveTrace();
      handleSessionSelectionPlayback(selectedSessionId);
      return;
    }
    lastSessionsKey = "";
    refreshSessions();
  }

  let lastSessionsKey = "";
  async function refreshSessions() {
    try {
      const { sessions, active } = await fetch("/api/sessions").then(r => r.json());
      if (!sessions || sessions.length === 0) {
        lastSessionsKey = "";
        applySessionSnapshot([], null);
        return;
      }
      const key = sessions.map(s => `${s.id}:${s.label}:${s.request_count}:${s.last_query || ""}`).join("|") + `@${active || ""}`;
      if (key === lastSessionsKey) {
        sessionsCache = sessions;
        applyProxySessionActivity(sessionsCache);
        return;
      }
      lastSessionsKey = key;
      applySessionSnapshot(sessions, active);
    } catch {}
  }
  picker.addEventListener("change", async () => {
    await setActiveSession(picker.value);
  });
  refreshSessions();
  setInterval(refreshSessions, 2000);

  // ------------------------------------------------------------------ popout

  popout.addEventListener("click", () => {
    const url = location.origin + "/?mini=1";
    window.open(url, "voice-copilot-mini", "width=540,height=90,menubar=no,toolbar=no,location=no,status=no,resizable=yes");
  });

  // ------------------------------------------------------------------ settings (auto-save)

  if (form) {
    setupSettings();
  }

  function setupSettings() {
    const proxyProfilesReady = { current: false };
    const proxyProfileIds = [];
    const PROXY_ROUTE_OPTIONS = [
      ["anthropic", "anthropic"],
      ["openai", "openai"],
      ["openai-chatgpt", "openai-chatgpt (Codex on a ChatGPT plan)"],
      ["opencode-zen", "opencode-zen (OpenCode Zen)"],
      ["deepseek", "deepseek"],
      ["openrouter", "openrouter"],
      ["groq", "groq"],
      ["mistral", "mistral"],
      ["ollama", "ollama"],
      ["gemini", "gemini"],
    ];

    const setByPath = (obj, path, value) => {
      const parts = path.split(".");
      let cur = obj;
      for (let i = 0; i < parts.length - 1; i++) {
        cur[parts[i]] = cur[parts[i]] ?? {};
        cur = cur[parts[i]];
      }
      cur[parts[parts.length - 1]] = value;
    };
    const getByPath = (obj, path) =>
      path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
    const delByPath = (obj, path) => {
      const parts = path.split(".");
      let cur = obj;
      for (let i = 0; i < parts.length - 1; i++) {
        if (cur[parts[i]] == null) return;
        cur = cur[parts[i]];
      }
      delete cur[parts[parts.length - 1]];
    };
    const elValue = (el) => {
      if (el.type === "checkbox") return el.checked;
      if (el.type === "number")   return Number(el.value);
      return el.value;
    };
    const setElValue = (el, v) => {
      if (v === undefined || v === null) return;
      if (el.type === "checkbox") el.checked = !!v;
      else el.value = v;
    };

    async function loadConfig() {
      const cfg = await fetch("/api/config").then(r => r.json());
      for (const el of form.elements) {
        if (!el.name) continue;
        setElValue(el, getByPath(cfg, el.name));
      }
      applyHotkeyTitles(cfg);
      renderPerCli(cfg);
      return cfg;
    }

    async function saveConfig() {
      const cfg = await fetch("/api/config").then(r => r.json());
      for (const el of form.elements) {
        if (!el.name) continue;
        const v = elValue(el);
        if (el.type !== "checkbox" && el.type !== "number" && typeof v === "string" && v === "") {
          delByPath(cfg, el.name);
        } else {
          setByPath(cfg, el.name, v);
        }
      }
      collectPerCli(cfg);
      const res = await fetch("/api/config", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(cfg),
      });
      return res.ok;
    }

    const PER_CLI_NAMES = ["claude", "codex", "opencode", "gemini", "copilot"];

    function renderPerCli(cfg) {
      const host = qs("#commentator-per-cli");
      if (!host) return;
      const perCli = (cfg.commentator && cfg.commentator.per_cli) || {};
      host.innerHTML = "";
      for (const name of PER_CLI_NAMES) {
        const cur = perCli[name] || {};
        const row = document.createElement("label");
        row.className = "row per-cli-row";
        row.dataset.cli = name;
        row.innerHTML =
          `<span class="per-cli-name">${name}</span>` +
          `<select class="per-cli-mode">` +
          `<option value="default">default</option>` +
          `<option value="current">current (this CLI)</option>` +
          `<option value="api">API provider</option>` +
          `</select>` +
          `<input class="per-cli-model" placeholder="model (optional)" />` +
          `<select class="per-cli-supervisor">` +
          `<option value="default">default</option>` +
          `<option value="off">off</option>` +
          `<option value="watch">Supervisor</option>` +
          `<option value="guard">Supervisor+</option>` +
          `</select>` +
          `<input class="per-cli-supervisor-model" placeholder="supervisor model (optional)" />`;
        host.appendChild(row);
        row.querySelector(".per-cli-mode").value = cur.mode || "default";
        row.querySelector(".per-cli-model").value = cur.model || "";
        row.querySelector(".per-cli-supervisor").value = cur.supervisor_mode || "default";
        row.querySelector(".per-cli-supervisor-model").value = cur.supervisor_model || "";
      }
    }

    function collectPerCli(cfg) {
      if (!cfg.commentator) cfg.commentator = {};
      const out = {};
      for (const row of qsa("#commentator-per-cli .per-cli-row")) {
        const mode = row.querySelector(".per-cli-mode").value;
        const model = row.querySelector(".per-cli-model").value.trim();
        const supMode = row.querySelector(".per-cli-supervisor").value;
        const supModel = row.querySelector(".per-cli-supervisor-model").value.trim();
        const entry = {};
        if (mode !== "default") entry.mode = mode;
        if (model) entry.model = model;
        if (supMode !== "default") entry.supervisor_mode = supMode;
        if (supModel) entry.supervisor_model = supModel;
        if (Object.keys(entry).length === 0) continue; // all defaults → absent
        out[row.dataset.cli] = entry;
      }
      cfg.commentator.per_cli = out;
    }

    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
    ));

    function cliRowMarkup(profile) {
      const isShell = profile.kind === "shell";
      const routeOptions = PROXY_ROUTE_OPTIONS
        .map(([value, label]) => `<option value="${value}">${label}</option>`)
        .join("");
      const routeFields = isShell
        ? `<p class="hint cli-inline-hint">A shell gets every base-URL variable at once, so anything you start inside it is proxied and narrated.</p>`
        : `
            <label>Upstream route
              <select name="proxy_cli.profiles.${profile.id}.provider">${routeOptions}</select>
            </label>
            <label>Env / override variable
              <input name="proxy_cli.profiles.${profile.id}.base_url_env" placeholder="${profile.base_url_env || "OPENAI_BASE_URL"}" />
            </label>`;
      const routeHint = profile.id === "opencode"
        ? `<p class="hint cli-inline-hint">For OpenCode Zen models like <strong>MiniMax M2.5 Free</strong> keep the route on <strong>opencode-zen</strong> — we inject a temporary <code>OPENCODE_CONFIG_CONTENT</code> override instead of <code>OPENAI_BASE_URL</code>.</p>`
        : "";
      const shimActions = isShell ? "" : `
            <div class="actions cli-actions">
              <button type="button" data-cli-install="${profile.id}">Add PATH shim</button>
              <button type="button" data-cli-restore="${profile.id}">Remove shim</button>
              <span class="hint cli-inline-hint">routes <code>${escapeHtml(profile.command)}</code> through the proxy in every terminal, not just the ones launched here</span>
            </div>`;

      return `
        <span class="cli-badge" aria-hidden="true">${escapeHtml(profile.icon || profile.id.slice(0, 2).toUpperCase())}</span>
        <div class="cli-row-main">
          <div class="cli-row-title">
            <h3>${escapeHtml(profile.label)}</h3>
            <span class="cli-status" data-cli-status="${profile.id}">checking…</span>
          </div>
          <p class="cli-row-desc">${escapeHtml(profile.description || "")}</p>
          <div class="cli-activity" data-cli-activity-row="${profile.id}" hidden>
            <span class="cli-open-dot" data-cli-open-dot="${profile.id}" data-state="none"></span>
            <span data-cli-activity="${profile.id}"></span>
          </div>
        </div>
        <div class="cli-row-actions">
          <button type="button" class="cmd-chip" data-cli-copy="${profile.id}" title="Copy the terminal command">
            <code>vc ${profile.id}</code>
            <span class="material-symbols-rounded" aria-hidden="true">content_copy</span>
          </button>
          <a class="ghost cli-get" data-cli-site="${profile.id}" href="${profile.website_url || "#"}" target="_blank" rel="noreferrer noopener" hidden>Get it</a>
          <button type="button" class="primary" data-cli-open="${profile.id}">Launch</button>
          <span class="cli-action-status" data-cli-action-status="${profile.id}"></span>
        </div>
        <details class="cli-advanced">
          <summary><span class="material-symbols-rounded" aria-hidden="true">tune</span>Advanced</summary>
          ${routeFields}
          ${routeHint}
          <label>Binary override (optional)
            <input name="proxy_cli.profiles.${profile.id}.binary_path" placeholder="C:\\Tools\\${escapeHtml(profile.command)}.cmd" />
          </label>
          <label class="cli-session-row" data-cli-session-row="${profile.id}" hidden>Dialog / session
            <select class="session-picker session-picker--card" data-cli-session-picker="${profile.id}">
              <option value="">No active dialogs yet</option>
            </select>
          </label>
          <div class="cli-meta">
            <div class="cli-meta-row">
              <span class="cli-meta-label">Proxy URL</span>
              <code data-cli-proxy-url="${profile.id}"></code>
            </div>
            <div class="cli-meta-row">
              <span class="cli-meta-label">Resolved binary</span>
              <code data-cli-resolved="${profile.id}"></code>
            </div>
            <div class="cli-meta-row">
              <span class="cli-meta-label">Launch folder</span>
              <code data-cli-workdir="${profile.id}"></code>
            </div>
          </div>
          ${shimActions}
        </details>
      `;
    }

    function renderProxyCliProfiles(status) {
      if (!proxyCliList || proxyProfilesReady.current) return;
      proxyCliList.innerHTML = "";
      proxyProfileIds.length = 0;
      for (const profile of sortProxyProfiles(status.profiles || [])) {
        proxyProfileIds.push(profile.id);
        const card = document.createElement("article");
        card.className = "cli-row";
        card.dataset.cliCard = profile.id;
        card.dataset.cliSearch =
          `${profile.label} ${profile.id} ${profile.command} ${profile.description}`.toLowerCase();
        card.style.setProperty("--cli-accent", profile.accent || "var(--accent)");
        card.innerHTML = cliRowMarkup(profile);
        proxyCliList.appendChild(card);
      }
      proxyProfilesReady.current = true;
      renderSessionControls();
    }

    // One-off list furniture: the "not installed" separator and the
    // no-matches note. Both are re-appended on every reflow so they keep
    // their place in the list.
    function ensureCliDivider() {
      let divider = proxyCliList.querySelector(".cli-divider");
      if (!divider) {
        divider = document.createElement("div");
        divider.className = "cli-divider";
        divider.innerHTML =
          `<span>Not installed here</span>` +
          `<span class="cli-divider-hint">install one to launch it from this panel</span>`;
      }
      return divider;
    }

    function ensureCliEmpty() {
      let empty = proxyCliList.querySelector(".cli-empty");
      if (!empty) {
        empty = document.createElement("p");
        empty.className = "cli-empty hint";
        empty.textContent = "Nothing matches that filter.";
        empty.hidden = true;
      }
      return empty;
    }

    function applyCliFilter() {
      if (!proxyCliList) return;
      const query = (cliFilter?.value || "").trim().toLowerCase();
      let visible = 0;
      let visibleMissing = 0;
      for (const card of proxyCliList.querySelectorAll("[data-cli-card]")) {
        const show = !query || (card.dataset.cliSearch || "").includes(query);
        card.hidden = !show;
        if (!show) continue;
        visible += 1;
        if (card.classList.contains("cli-row--missing")) visibleMissing += 1;
      }
      const divider = proxyCliList.querySelector(".cli-divider");
      if (divider) divider.hidden = visibleMissing === 0;
      const empty = proxyCliList.querySelector(".cli-empty");
      if (empty) empty.hidden = visible !== 0;
    }

    function applyProxyCliStatus(status) {
      const proxyDown = status.proxy_available === false;
      if (proxyCliSummary) {
        proxyCliSummary.textContent = proxyDown
          ? "The proxy is not running — restart with `voice-copilot serve` to launch terminals from here."
          : !status.supported
            ? "Launching terminals from the panel is not supported on this platform."
            : `Launch opens a new terminal in ${status.resolved_working_directory || "the current folder"}, already routed through the proxy.`;
      }
      if (proxyCliWorkingDirectoryInput) {
        proxyCliWorkingDirectoryInput.placeholder =
          status.resolved_working_directory || "current folder";
      }

      const sortedProfiles = sortProxyProfiles(status.profiles || []);
      for (const profile of sortedProfiles) {
        const card = proxyCliList?.querySelector(`[data-cli-card="${profile.id}"]`);
        const available = !!profile.resolved_binary;
        if (card) card.classList.toggle("cli-row--missing", !available);

        const statusEl = document.querySelector(`[data-cli-status="${profile.id}"]`);
        const proxyUrlEl = document.querySelector(`[data-cli-proxy-url="${profile.id}"]`);
        const resolvedEl = document.querySelector(`[data-cli-resolved="${profile.id}"]`);
        const workdirEl = document.querySelector(`[data-cli-workdir="${profile.id}"]`);
        const installBtn = document.querySelector(`[data-cli-install="${profile.id}"]`);
        const restoreBtn = document.querySelector(`[data-cli-restore="${profile.id}"]`);
        const openBtn = document.querySelector(`[data-cli-open="${profile.id}"]`);
        const siteLink = document.querySelector(`[data-cli-site="${profile.id}"]`);

        if (statusEl) {
          const onPath = profile.installed && status.path_active;
          statusEl.dataset.state = !available ? "missing" : onPath ? "installed" : "ready";
          statusEl.textContent = !available ? "not installed" : onPath ? "on PATH" : "ready";
        }
        if (proxyUrlEl) proxyUrlEl.textContent = profile.proxy_url || "—";
        if (resolvedEl) resolvedEl.textContent = profile.resolved_binary || "not found";
        if (workdirEl) {
          workdirEl.textContent = profile.resolved_working_directory || "current folder";
        }
        if (installBtn) {
          installBtn.hidden = !available;
          installBtn.disabled = !status.supported || proxyDown;
        }
        if (restoreBtn) {
          restoreBtn.hidden = !available || !profile.installed;
          restoreBtn.disabled = !status.supported;
        }
        if (openBtn) {
          openBtn.hidden = !available;
          openBtn.disabled =
            !status.supported || proxyDown || !profile.resolved_working_directory;
        }
        if (siteLink) {
          siteLink.href = profile.website_url || "#";
          siteLink.hidden = available || !profile.website_url;
        }
      }

      // Reflow: available CLIs first, then the divider, then the rest.
      if (proxyCliList) {
        let dividerPlaced = false;
        for (const profile of sortedProfiles) {
          const card = proxyCliList.querySelector(`[data-cli-card="${profile.id}"]`);
          if (!card) continue;
          if (!profile.resolved_binary && !dividerPlaced) {
            dividerPlaced = true;
            proxyCliList.appendChild(ensureCliDivider());
          }
          proxyCliList.appendChild(card);
        }
        const divider = proxyCliList.querySelector(".cli-divider");
        if (divider && !dividerPlaced) divider.remove();
        proxyCliList.appendChild(ensureCliEmpty());
      }

      applyCliFilter();
    }

    async function loadProxyCliStatus({ initial = false } = {}) {
      if (!proxyCliList) return null;
      const status = await fetch("/api/proxy/cli-shims").then(r => r.json());
      if (initial) renderProxyCliProfiles(status);
      applyProxyCliStatus(status);
      return status;
    }

    // --- auto-save: debounce per-field change -----------------------------

    let saveTimer = null;
    let saveInFlight = false;
    let pendingChange = false;

    function scheduleSave() {
      pendingChange = true;
      showSave("saving…", "");
      if (saveTimer) clearTimeout(saveTimer);
      saveTimer = setTimeout(flush, 400);
    }
    async function flush() {
      if (saveInFlight) return;  // onfinally will re-check pendingChange
      if (!pendingChange) return;
      pendingChange = false;
      saveInFlight = true;
      try {
        const ok = await saveConfig();
        showSave(ok ? "saved" : "save failed", ok ? "ok" : "err");
      } catch {
        showSave("save failed", "err");
      } finally {
        saveInFlight = false;
        if (pendingChange) setTimeout(flush, 50);
      }
    }

    form.addEventListener("input", (e) => {
      if (!e.target.name) return;
      scheduleSave();
    });
    form.addEventListener("change", (e) => {
      if (!e.target.name) return;
      scheduleSave();
    });
    // Form has no submit button anymore — but guard against <Enter>.
    form.addEventListener("submit", (e) => e.preventDefault());

    // --- secrets ---------------------------------------------------------

    async function loadSecrets() {
      const { known, present } = await fetch("/api/secrets").then(r => r.json());
      secretsList.innerHTML = "";
      for (const name of known) {
        const row = document.createElement("div");
        row.className = "secret-row";
        row.innerHTML = `
          <label>${name} <span class="secret-state">${present[name] ? "✓ set" : "— empty"}</span></label>
          <div class="actions">
            <input type="password" placeholder="paste key, then Save" data-secret-input="${name}" autocomplete="off" />
            <button type="button" data-secret-save="${name}">Save</button>
            <button type="button" data-secret-clear="${name}">Clear</button>
            <span data-secret-status="${name}"></span>
          </div>
        `;
        secretsList.appendChild(row);
      }
    }

    secretsList.addEventListener("click", async (e) => {
      const saveName = e.target?.dataset?.secretSave;
      const clearName = e.target?.dataset?.secretClear;
      if (saveName) {
        const input  = secretsList.querySelector(`[data-secret-input="${saveName}"]`);
        const status = secretsList.querySelector(`[data-secret-status="${saveName}"]`);
        if (!input.value) { status.textContent = "nothing to save"; return; }
        status.textContent = "saving…";
        const res = await fetch("/api/secrets", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ name: saveName, value: input.value }),
        });
        if (res.ok) { input.value = ""; status.textContent = "saved ✓"; await loadSecrets(); }
        else {
          const err = await res.json().catch(() => ({ detail: "error" }));
          status.textContent = err.detail || "error";
        }
      } else if (clearName) {
        const status = secretsList.querySelector(`[data-secret-status="${clearName}"]`);
        status.textContent = "clearing…";
        await fetch(`/api/secrets/${encodeURIComponent(clearName)}`, { method: "DELETE" });
        status.textContent = "cleared";
        await loadSecrets();
      }
    });

    async function copyLaunchCommand(profileId, button) {
      const text = `vc ${profileId}`;
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        // Clipboard API can be blocked (non-secure origin, denied permission).
        const scratch = document.createElement("textarea");
        scratch.value = text;
        scratch.setAttribute("readonly", "");
        scratch.style.position = "fixed";
        scratch.style.opacity = "0";
        document.body.appendChild(scratch);
        scratch.select();
        try { document.execCommand("copy"); } catch {}
        scratch.remove();
      }
      button.classList.add("copied");
      clearTimeout(button._copiedTimer);
      button._copiedTimer = setTimeout(() => button.classList.remove("copied"), 1200);
    }

    proxyCliList?.addEventListener("click", async (e) => {
      const copyBtn = e.target?.closest?.("[data-cli-copy]");
      if (copyBtn) {
        await copyLaunchCommand(copyBtn.dataset.cliCopy, copyBtn);
        return;
      }

      const openBtn = e.target?.closest?.("[data-cli-open]");
      const installBtn = e.target?.closest?.("[data-cli-install]");
      const restoreBtn = e.target?.closest?.("[data-cli-restore]");
      const profileId =
        openBtn?.dataset.cliOpen || installBtn?.dataset.cliInstall || restoreBtn?.dataset.cliRestore;
      if (!profileId) return;

      const statusEl = proxyCliList.querySelector(`[data-cli-action-status="${profileId}"]`);
      if (!statusEl) return;

      if (openBtn) {
        statusEl.textContent = "opening…";
        await flush();
        const res = await fetch(
          `/api/proxy/cli-shims/${encodeURIComponent(profileId)}/launch`,
          { method: "POST" },
        );
        const out = await res.json().catch(() => ({ detail: "request failed" }));
        statusEl.textContent = res.ok ? "opened" : (out.detail || "request failed");
        return;
      }

      statusEl.textContent = installBtn ? "installing…" : "removing…";
      await flush();

      const res = await fetch(
        `/api/proxy/cli-shims/${encodeURIComponent(profileId)}/${installBtn ? "install" : "restore"}`,
        { method: "POST" },
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "request failed" }));
        statusEl.textContent = err.detail || "request failed";
        return;
      }
      await loadProxyCliStatus();
      statusEl.textContent = installBtn ? "on PATH" : "shim removed";
    });

    cliFilter?.addEventListener("input", applyCliFilter);

    proxyCliList?.addEventListener("change", async (e) => {
      const profileId = e.target?.dataset?.cliSessionPicker;
      if (!profileId) return;
      if (!e.target.value) {
        renderSessionControls();
        return;
      }
      await setActiveSession(e.target.value);
    });

    proxyCliWorkingDirectoryPicker?.addEventListener("click", async () => {
      if (!proxyCliWorkingDirectoryInput) return;
      if (proxyCliSummary) proxyCliSummary.textContent = "Opening folder picker…";
      const res = await fetch("/api/proxy/cli-shims/pick-directory", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ initial_dir: proxyCliWorkingDirectoryInput.value || "" }),
      });
      const out = await res.json().catch(() => ({ detail: "request failed" }));
      if (!res.ok) {
        if (proxyCliSummary) proxyCliSummary.textContent = out.detail || "request failed";
        return;
      }
      if (!out.path) {
        if (proxyCliSummary) proxyCliSummary.textContent = "Folder picker cancelled.";
        return;
      }
      proxyCliWorkingDirectoryInput.value = out.path;
      scheduleSave();
      await flush();
      await loadProxyCliStatus();
    });

    // --- provider tests --------------------------------------------------

    async function handleProviderTest(kind) {
      const statusEl = document.querySelector(`[data-test-status="${kind}"]`);
      if (kind === "llm") setOutputBox(llmTestOutput, "");
      statusEl.textContent = "testing…";
      await flush();  // persist pending edits first
      const cfg = await fetch("/api/config").then(r => r.json());
      let name, options;
      if (kind === "llm") {
        name = cfg.commentator.provider.name;
        options = cfg.commentator.provider.options || {};
      } else {
        name = cfg[kind].name;
        options = cfg[kind].options || {};
      }
      const res = await fetch("/api/providers/test", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ kind, name, options }),
      });
      const out = await readApiResponse(res);
      if (out.ok) {
        const detail = out.preview ? `"${out.preview}"` : out.bytes ? `${out.bytes} bytes` : out.note || "ok";
        statusEl.textContent = kind === "llm" ? "✓ replied" : `✓ ${detail}`;
        if (kind === "llm") {
          setOutputBox(llmTestOutput, out.response || out.preview || "(empty response)");
        }
      } else {
        statusEl.textContent = `✗ ${formatApiError(out)}`;
        if (kind === "llm") {
          setOutputBox(llmTestOutput, formatApiError(out));
        }
      }
    }
    qsa("[data-test-provider]").forEach(btn => {
      btn.addEventListener("click", () => handleProviderTest(btn.dataset.testProvider));
    });

    async function handleTtsPreviewTest() {
      const statusEl = document.querySelector('[data-test-status="tts"]');
      setOutputBox(ttsTestOutput, "");
      statusEl.textContent = "generating…";
      await flush();
      const res = await fetch("/api/providers/test-tts", { method: "POST" });
      const out = await readApiResponse(res);
      if (!res.ok || !out.ok) {
        const message = formatApiError(out);
        statusEl.textContent = `✗ ${message}`;
        setOutputBox(ttsTestOutput, message);
        return;
      }
      playPreviewAudio(out.audio_base64, out.format);
      statusEl.textContent = "✓ speaking test phrase";
      setOutputBox(ttsTestOutput, out.text || "");
    }

    async function submitSpeechInputTest() {
      const statusEl = document.querySelector('[data-test-status="stt"]');
      const blob = new Blob(speechTestChunks, { type: speechTestContainer === "ogg" ? "audio/ogg" : "audio/webm" });
      const audio = await blob.arrayBuffer();
      statusEl.textContent = "transcribing…";
      sttTestBtn.disabled = true;
      try {
        const res = await fetch(`/api/providers/test-stt?container=${encodeURIComponent(speechTestContainer)}`, {
          method: "POST",
          headers: { "content-type": "application/octet-stream" },
          body: audio,
        });
        const out = await readApiResponse(res);
        if (!res.ok || !out.ok) {
          statusEl.textContent = `✗ ${formatApiError(out)}`;
          setOutputBox(speechTranscript, formatApiError(out));
          return;
        }
        statusEl.textContent = out.text?.trim() ? "✓ transcribed" : "✓ no speech detected";
        setOutputBox(speechTranscript, out.text?.trim() || "(empty transcription)");
      } finally {
        sttTestBtn.disabled = false;
      }
    }

    async function startSpeechInputTest() {
      const statusEl = document.querySelector('[data-test-status="stt"]');
      if (speechTranscript) {
        speechTranscript.hidden = true;
        speechTranscript.textContent = "";
      }
      try {
        speechTestStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch {
        statusEl.textContent = "microphone access denied";
        return;
      }
      const mime = pickMimeType();
      speechTestContainer = containerForMimeType(mime || "audio/webm");
      speechTestChunks = [];
      speechTestRecorder = new MediaRecorder(speechTestStream, mime ? { mimeType: mime } : undefined);
      speechTestRecorder.ondataavailable = (event) => {
        if (event.data && event.data.size > 0) speechTestChunks.push(event.data);
      };
      speechTestRecorder.onstop = () => {
        const stream = speechTestStream;
        speechTestStream = null;
        if (stream) stream.getTracks().forEach(track => track.stop());
        submitSpeechInputTest().catch(() => {
          const sttStatusEl = document.querySelector('[data-test-status="stt"]');
          sttStatusEl.textContent = "✗ transcription failed";
        });
      };
      speechTestRecorder.start(150);
      speechTestRecording = true;
      sttTestBtn.textContent = "Stop recording";
      statusEl.textContent = "recording… press again to stop";
    }

    function stopSpeechInputTest() {
      if (!speechTestRecorder || speechTestRecorder.state === "inactive") return;
      speechTestRecording = false;
      sttTestBtn.textContent = "Record test";
      speechTestRecorder.stop();
    }

    ttsTestBtn?.addEventListener("click", () => {
      handleTtsPreviewTest().catch(() => {
        const statusEl = document.querySelector('[data-test-status="tts"]');
        statusEl.textContent = "✗ test failed";
      });
    });

    sttTestBtn?.addEventListener("click", () => {
      if (speechTestRecording) stopSpeechInputTest();
      else {
        startSpeechInputTest().catch(() => {
          const statusEl = document.querySelector('[data-test-status="stt"]');
          statusEl.textContent = "✗ test failed";
        });
      }
    });

    // Provider hint
    const providerSelect = qs("#commentator-provider");
    const providerHint   = qs("#provider-hint");
    const PROVIDER_HINTS = {
      "copilot-cli": "Calls `copilot -p '...' -s --allow-all` as subprocess. Model must be gpt-5-mini or gpt-4.1. Requires `copilot login`.",
      "github-copilot": "Requires GITHUB_COPILOT_TOKEN in API keys tab (or run `gh auth login`).",
      "openai-compat": "Set Base URL to your local Ollama/LM Studio endpoint. Use a non-reasoning model (llama3.1, qwen2.5, mistral).",
    };
    function updateProviderHint() {
      const hint = PROVIDER_HINTS[providerSelect?.value] || "";
      if (providerHint) providerHint.textContent = hint;
    }
    // Model and Base URL are one shared pair of inputs, but every provider wants
    // its own values (copilot-cli only takes gpt-5-mini/gpt-4.1, anthropic only
    // claude-*, ollama needs a base URL). Switching the provider used to strand
    // the previous one's values on the next — which is how a saved config ends up
    // with a pair like `anthropic` + `gpt-5-mini`. Remember them per provider so
    // switching back is lossless and switching forward never leaves a mismatch.
    const PROVIDER_OPTION_FIELDS = ["model", "base_url"];
    const PROVIDER_OPTIONS_KEY = "vc.commentator.provider-options";
    const providerOptionInputs = PROVIDER_OPTION_FIELDS
      .map((field) => [field, qs(`[name="commentator.provider.options.${field}"]`)])
      .filter(([, el]) => el);

    function readProviderOptionMemory() {
      // A convenience, never state: a blocked or wiped store just means the next
      // switch starts the provider empty, which is the same as a fresh install.
      try {
        const parsed = JSON.parse(localStorage.getItem(PROVIDER_OPTIONS_KEY) || "{}");
        return parsed && typeof parsed === "object" ? parsed : {};
      } catch {
        return {};
      }
    }

    function stashProviderOptions(name) {
      if (!name) return;
      const memory = readProviderOptionMemory();
      memory[name] = Object.fromEntries(providerOptionInputs.map(([f, el]) => [f, el.value]));
      try {
        localStorage.setItem(PROVIDER_OPTIONS_KEY, JSON.stringify(memory));
      } catch {
        // nothing to do — the inputs and config.yaml still hold the live values
      }
    }

    function restoreProviderOptions(name) {
      const entry = readProviderOptionMemory()[name] || {};
      for (const [field, el] of providerOptionInputs) el.value = entry[field] || "";
    }

    let lastProviderName = "";

    if (providerSelect) {
      providerSelect.addEventListener("change", () => {
        stashProviderOptions(lastProviderName);
        restoreProviderOptions(providerSelect.value);
        lastProviderName = providerSelect.value;
        updateProviderHint();
        // Setting .value in code fires no input event, so autosave needs the nudge.
        scheduleSave();
      });
      updateProviderHint();
    }

    function initProviderOptionMemory() {
      lastProviderName = providerSelect?.value || "";
      // Seed from the just-loaded config so the first switch away and back is lossless.
      stashProviderOptions(lastProviderName);
    }

    // "Auto" reuses the CLI you launched and needs no keys, so its provider
    // fields stay out of the way until you actually pick "API".
    const commentatorMode = qs('[name="commentator.mode"]');
    const commentatorApiFields = qs("#commentator-api-fields");
    function applyCommentatorMode() {
      if (commentatorApiFields) commentatorApiFields.hidden = commentatorMode?.value !== "api";
    }
    commentatorMode?.addEventListener("change", applyCommentatorMode);

    (async () => {
      await loadProxyCliStatus({ initial: true });
      await loadConfig();
      applyCommentatorMode();
      initProviderOptionMemory();
      updateProviderHint();
      await loadSecrets();
      await loadProxyCliStatus();
    })();
  }

  function showSave(text, level) {
    if (!saveInd) return;
    saveInd.textContent = text;
    saveInd.classList.toggle("ok",  level === "ok");
    saveInd.classList.toggle("err", level === "err");
    saveInd.classList.add("show");
    clearTimeout(showSave._t);
    showSave._t = setTimeout(() => saveInd.classList.remove("show"), 1400);
  }
})();
