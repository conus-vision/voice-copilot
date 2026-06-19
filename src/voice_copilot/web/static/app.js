// Unified client for voice-copilot's web UI.
//
// One page, multiple tabs. Controls live in the topbar at all times so the
// user can pause / resume / speak / interrupt regardless of which tab is
// active. The same page runs in two modes:
//   * full  — everything below the topbar is visible (Trace + settings)
//   * mini  — a compact popup-window view: only topbar + controls remain
//     (triggered by ?mini=1 or by opening via the popout button).

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
  const rateBtns   = [...qsa("[data-playback-rate]")];
  const popout     = qs("#btn-popout");
  const picker     = qs("#session-picker");
  const trace      = qs("#trace");
  const tracePause = qs("#btn-trace-pause");
  const traceClear = qs("#btn-trace-clear");
  const traceAuto  = qs("#trace-autoscroll");
  const traceStats = qs("#trace-stats");
  const saveInd    = qs("#save-indicator");
  const form       = qs("#settings-form");
  const secretsList = qs("#secrets-list");
  const proxyCliList = qs("#proxy-cli-list");
  const proxyCliSummary = qs("#proxy-cli-summary");
  const proxyCliWorkingDirectoryInput = qs('[name="proxy_cli.working_directory"]');
  const proxyCliWorkingDirectoryStatus = qs('[data-cli-working-directory-status]');
  const proxyCliWorkingDirectoryPicker = qs("[data-cli-pick-global-dir]");
  const ttsTestBtn = qs("#btn-test-tts");
  const sttTestBtn = qs("#btn-test-stt");
  const ttsTestOutput = qs("#speech-test-tts-output");
  const speechTranscript = qs("#speech-test-transcript");
  const llmTestOutput = qs("#llm-test-output");

  // ------------------------------------------------------------------ proxy port in Instructions tab

  fetch("/api/info").then(r => r.json()).then(({ proxy_port }) => {
    if (!proxy_port) return;
    document.querySelectorAll(".pport").forEach(el => { el.textContent = proxy_port; });
  }).catch(() => {});

  // ------------------------------------------------------------------ tabs

  function activateTab(name) {
    qsa(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
    qsa(".panel").forEach(p => p.classList.toggle("active", p.dataset.panel === name));
    try { localStorage.setItem("vc.tab", name); } catch {}
  }
  qsa(".tab").forEach(t => t.addEventListener("click", () => activateTab(t.dataset.tab)));
  try {
    const saved = localStorage.getItem("vc.tab");
    const restored = saved === "tts" || saved === "stt" ? "speech" : saved;
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

  function applyPlayButton() {
    // DOM-only update — no broadcast. Called from bc.onmessage to avoid ping-pong.
    setIcon(playpause, paused ? "play_arrow" : "pause");
    playpause.classList.toggle("active", !paused);
    setButtonHint(playpause, paused ? "Resume narration" : "Pause narration");
  }

  function applyMuteButton() {
    player.muted = muted;
    muteBtn.classList.toggle("active", muted);
    setIcon(muteBtn, muted ? "volume_off" : "volume_up");
    setButtonHint(muteBtn, muted ? "Unmute narration" : "Mute narration");
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

  function profileIdForSession(session) {
    const raw = `${session?.cli_id || ""} ${session?.label || ""} ${session?.user_agent || ""}`.toLowerCase();
    if (raw.includes("claude")) return "claude";
    if (raw.includes("codex")) return "codex";
    if (raw.includes("copilot") || raw.includes("github cli") || raw.includes("gh ")) return "copilot";
    if (raw.includes("aider")) return "aider";
    if (raw.includes("opencode")) return "opencode";
    if (raw.includes("kimi")) return "kimi";
    return null;
  }

  function setCliActivity(profileId, state, text) {
    const textEl = document.querySelector(`[data-cli-activity="${profileId}"]`);
    if (textEl) textEl.textContent = text;
    const openDotEl = document.querySelector(`[data-cli-open-dot="${profileId}"]`);
    if (openDotEl) openDotEl.dataset.state = state;
  }

  function applyProxySessionActivity(sessions = []) {
    qsa("[data-cli-activity]").forEach((el) => {
      const profileId = el.dataset.cliActivity;
      setCliActivity(profileId, "none", "No proxy traffic yet.");
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
    player.play().catch(() => {});
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
    player.play().catch(() => {});
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
        player.play().catch(() => {});
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
        player.play().catch(() => {});
        playing = true;
      } else if (!selectionPausedSessionId && !playing && currentPlaybackItem?.url && player.paused) {
        player.play().catch(() => {});
        playing = true;
      } else if (!selectionPausedSessionId && !playing) {
        playNext();
      } else if (!selectionPausedSessionId && player.paused && player.src) {
        player.play().catch(() => {});
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

  muteBtn.addEventListener("click", () => {
    muted = !muted;
    send({ type: "cmd", cmd: muted ? "mute" : "unmute" });
    applyMuteButton();
    bc.postMessage({ type: "state", paused, muted });
  });
  interrupt.addEventListener("click", () => send({ type: "cmd", cmd: "interrupt" }));
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
          if (!isMini && isAgentQuery(msg)) stopPlaybackForSession(messageSessionId(msg));
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

  // ------------------------------------------------------------------ trace

  const TRACE_MAX = 500;
  const traceStates = new Map();

  function getTraceState(sessionId) {
    const key = sessionKey(sessionId);
    let state = traceStates.get(key);
    if (!state) {
      state = { items: [], mergeIndexByKey: new Map() };
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

  function buildTraceRow(item) {
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

  function renderActiveTrace() {
    if (!trace) return;
    const state = getTraceState(activeNarrationSessionId());
    trace.innerHTML = "";
    state.items.forEach((item) => trace.appendChild(buildTraceRow(item)));
    traceStats.textContent = `${state.items.length} items`;
    if (traceAuto.checked) trace.scrollTop = trace.scrollHeight;
  }

  function clearActiveTrace() {
    traceStates.set(sessionKey(activeNarrationSessionId()), { items: [], mergeIndexByKey: new Map() });
    renderActiveTrace();
  }

  tracePause.addEventListener("click", () => {
    tracePaused = !tracePaused;
    applyTracePauseButton();
  });
  traceClear.addEventListener("click", () => {
    clearActiveTrace();
  });

  function traceAppend(msg) {
    if (tracePaused) return;
    const item = classifyForTrace(msg);
    if (!item) return;

    const sessionId = messageSessionId(msg);
    const state = getTraceState(sessionId);

    if (item.mergeKey && state.mergeIndexByKey.has(item.mergeKey)) {
      const existing = state.items[state.mergeIndexByKey.get(item.mergeKey)];
      existing.text += item.text;
      existing.streaming = !!item.streaming;
      if (!item.streaming) state.mergeIndexByKey.delete(item.mergeKey);
      if (sessionIdEquals(sessionId, activeNarrationSessionId())) renderActiveTrace();
      return;
    }

    state.items.push({
      ...item,
      sessionId,
      timeLabel: new Date().toLocaleTimeString(),
    });
    if (item.mergeKey && item.streaming) state.mergeIndexByKey.set(item.mergeKey, state.items.length - 1);
    if (state.items.length > TRACE_MAX) {
      state.items.splice(0, state.items.length - TRACE_MAX);
      rebuildTraceMergeIndex(state);
    }

    if (sessionIdEquals(sessionId, activeNarrationSessionId())) renderActiveTrace();
  }

  function classifyForTrace(msg) {
    const p = msg.payload || {};
    switch (msg.kind) {
      case "user.message": {
        const text = (p.text || "").trim();
        if (!text) return null;
        // skip user talking to voice-copilot itself (STT output)
        if (p.delivery !== "observed") return null;
        return { cls: "query", tag: "USER", text };
      }
      case "agent.thinking":
        return p.text ? { cls: "thinking", tag: "THINKING", text: p.text } : null;
      case "agent.text":
        return p.text ? { cls: "answer", tag: "AGENT", text: p.text } : null;
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
      ["opencode-zen", "opencode-zen (OpenCode Zen)"],
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
      const res = await fetch("/api/config", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(cfg),
      });
      return res.ok;
    }

    function renderProxyCliProfiles(status) {
      if (!proxyCliList || proxyProfilesReady.current) return;
      proxyCliList.innerHTML = "";
      proxyProfileIds.length = 0;
      for (const profile of sortProxyProfiles(status.profiles || [])) {
        proxyProfileIds.push(profile.id);
        const routeOptions = PROXY_ROUTE_OPTIONS
          .map(([value, label]) => `<option value="${value}">${label}</option>`)
          .join("");
        const routeHint = profile.id === "opencode"
          ? '<p class="hint cli-inline-hint">For OpenCode Zen models like <strong>MiniMax M2.5 Free</strong>, choose <strong>opencode-zen</strong>. voice-copilot injects a temporary <code>OPENCODE_CONFIG_CONTENT</code> override instead of relying on <code>OPENAI_BASE_URL</code>.</p>'
          : "";
        const card = document.createElement("section");
        card.className = "cli-card";
        card.dataset.cliCard = profile.id;
        card.innerHTML = `
          <div class="cli-card-head">
            <div>
              <h3>${profile.label}</h3>
              <p class="hint">${profile.description}</p>
            </div>
            <span class="cli-status" data-cli-status="${profile.id}">checking…</span>
          </div>

          <details class="cli-advanced">
            <summary>Advanced</summary>

            <label>Current upstream route
              <select name="proxy_cli.profiles.${profile.id}.provider">
                ${routeOptions}
              </select>
            </label>

            ${routeHint}

            <label>CLI proxy env / override var
              <input name="proxy_cli.profiles.${profile.id}.base_url_env" placeholder="${profile.base_url_env || "OPENAI_BASE_URL"}" />
            </label>

            <label>Binary override (optional)
              <input name="proxy_cli.profiles.${profile.id}.binary_path" placeholder="C:\\Tools\\${profile.command}.cmd" />
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
                <span class="cli-meta-label">Shim path</span>
                <code data-cli-shim="${profile.id}"></code>
              </div>
              <div class="cli-meta-row">
                <span class="cli-meta-label">Launch folder</span>
                <code data-cli-workdir="${profile.id}"></code>
              </div>
            </div>
          </details>

          <div class="cli-activity">
            <span data-cli-activity="${profile.id}">No proxy traffic yet.</span>
          </div>

          <div class="actions cli-actions">
            <button type="button" data-cli-open="${profile.id}">Open CLI</button>
            <button type="button" data-cli-install="${profile.id}">Install proxy</button>
            <button type="button" data-cli-restore="${profile.id}">Restore</button>
            <a class="ghost" data-cli-site="${profile.id}" href="${profile.website_url || "#"}" target="_blank" rel="noreferrer noopener" hidden>Get CLI</a>
            <span data-cli-action-status="${profile.id}"></span>
          </div>

          <div class="cli-open-state" data-cli-open-state-row="${profile.id}" hidden>
            <span class="cli-open-dot" data-cli-open-dot="${profile.id}" data-state="none"></span>
            <span data-cli-open-state="${profile.id}"></span>
          </div>
        `;
        proxyCliList.appendChild(card);
      }
      proxyProfilesReady.current = true;
      renderSessionControls();
    }

    function applyProxyCliStatus(status) {
      if (!proxyCliSummary) return;
      if (status.proxy_available === false) {
        proxyCliSummary.textContent = "Proxy is not running. Restart with voice-copilot serve --proxy or voice-copilot proxy.";
      } else if (!status.supported) {
        proxyCliSummary.textContent = "Automatic CLI proxy install is currently supported on Windows only.";
      } else {
        proxyCliSummary.textContent = status.path_active
          ? `PATH shim directory is active: ${status.shim_dir}`
          : `Install adds ${status.shim_dir} to your user PATH, and Open CLI launches a proxied terminal in the shared folder above.`;
      }

      if (proxyCliWorkingDirectoryStatus) {
        proxyCliWorkingDirectoryStatus.textContent = status.resolved_working_directory || "current voice-copilot folder";
      }

      const sortedProfiles = sortProxyProfiles(status.profiles || []);
      for (const profile of sortedProfiles) {
        const card = proxyCliList?.querySelector(`[data-cli-card="${profile.id}"]`);
        if (card) card.classList.toggle("cli-card--missing", !profile.resolved_binary);
        const statusEl = document.querySelector(`[data-cli-status="${profile.id}"]`);
        const actionEl = document.querySelector(`[data-cli-action-status="${profile.id}"]`);
        const proxyUrlEl = document.querySelector(`[data-cli-proxy-url="${profile.id}"]`);
        const resolvedEl = document.querySelector(`[data-cli-resolved="${profile.id}"]`);
        const shimEl = document.querySelector(`[data-cli-shim="${profile.id}"]`);
        const workdirEl = document.querySelector(`[data-cli-workdir="${profile.id}"]`);
        const installBtn = document.querySelector(`[data-cli-install="${profile.id}"]`);
        const restoreBtn = document.querySelector(`[data-cli-restore="${profile.id}"]`);
        const openBtn = document.querySelector(`[data-cli-open="${profile.id}"]`);
        const siteLink = document.querySelector(`[data-cli-site="${profile.id}"]`);

        if (statusEl) {
          let state = "missing";
          let label = "binary missing";
          if (profile.installed) {
            state = status.path_active ? "installed" : "ready";
            label = status.path_active ? "installed" : "shim ready";
          } else if (profile.resolved_binary) {
            state = "ready";
            label = "ready";
          }
          statusEl.dataset.state = state;
          statusEl.textContent = label;
        }
        if (actionEl) actionEl.textContent = "";
        if (proxyUrlEl) proxyUrlEl.textContent = profile.proxy_url || "—";
        if (resolvedEl) resolvedEl.textContent = profile.resolved_binary || "not found";
        if (shimEl) shimEl.textContent = profile.shim_path || "—";
        if (workdirEl) workdirEl.textContent = profile.resolved_working_directory || "current voice-copilot folder";
        if (installBtn) {
          installBtn.hidden = !profile.resolved_binary;
          installBtn.disabled = !status.supported || status.proxy_available === false;
        }
        if (restoreBtn) {
          restoreBtn.hidden = !profile.resolved_binary;
          restoreBtn.disabled = !status.supported;
        }
        if (openBtn) {
          openBtn.hidden = !profile.resolved_binary;
          openBtn.disabled = !status.supported || !profile.resolved_binary || status.proxy_available === false || !profile.resolved_working_directory;
        }
        if (siteLink) {
          siteLink.href = profile.website_url || "#";
          siteLink.hidden = !!profile.resolved_binary || !profile.website_url;
        }
      }

      for (const profile of sortedProfiles) {
        const card = proxyCliList?.querySelector(`[data-cli-card="${profile.id}"]`);
        if (card) proxyCliList.appendChild(card);
      }
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

    proxyCliList?.addEventListener("click", async (e) => {
      const installId = e.target?.dataset?.cliInstall;
      const restoreId = e.target?.dataset?.cliRestore;
      const openId = e.target?.dataset?.cliOpen;
      const profileId = installId || restoreId || openId;
      if (!profileId) return;

      const statusEl = proxyCliList.querySelector(`[data-cli-action-status="${profileId}"]`);
      if (!statusEl) return;

      if (openId) {
        const openStateRow = proxyCliList.querySelector(`[data-cli-open-state-row="${profileId}"]`);
        const openStateEl = proxyCliList.querySelector(`[data-cli-open-state="${profileId}"]`);
        statusEl.textContent = "opening…";
        await flush();
        const res = await fetch(
          `/api/proxy/cli-shims/${encodeURIComponent(profileId)}/launch`,
          { method: "POST" },
        );
        const out = await res.json().catch(() => ({ detail: "request failed" }));
        if (!res.ok) {
          statusEl.textContent = out.detail || "request failed";
          return;
        }
        if (openStateEl) {
          openStateEl.textContent = out.working_directory
            ? `opened in ${short(out.working_directory, 48)}`
            : "opened";
        }
        if (openStateRow) openStateRow.hidden = false;
        statusEl.textContent = "opened";
        return;
      }

      statusEl.textContent = installId ? "installing…" : "restoring…";
      await flush();

      const res = await fetch(
        `/api/proxy/cli-shims/${encodeURIComponent(profileId)}/${installId ? "install" : "restore"}`,
        { method: "POST" },
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "request failed" }));
        statusEl.textContent = err.detail || "request failed";
        return;
      }
      await loadProxyCliStatus();
      statusEl.textContent = installId ? "installed" : "restored";
    });

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
    if (providerSelect) {
      providerSelect.addEventListener("change", updateProviderHint);
      updateProviderHint();
    }

    (async () => {
      await loadProxyCliStatus({ initial: true });
      await loadConfig();
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
