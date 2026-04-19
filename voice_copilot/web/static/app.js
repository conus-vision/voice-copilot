// Popup client: events over WebSocket text frames, audio over binary frames.
//
// Speaking (mic in):
//   hold the speak button (or Alt+Space) → MediaRecorder → binary frames to /ws
//   release → cmd:mic_end → server runs STT → USER_MESSAGE event back
//
// Listening (TTS out):
//   server sends {type:"audio_header",...} → binary frames → {type:"audio_end"}
//   we assemble a Blob and play via <audio>. Simple, no MSE.

(() => {
  const dot    = document.getElementById("conn-dot");
  const status = document.getElementById("status");
  const feed   = document.getElementById("feed");
  const player = document.getElementById("tts-player");

  let ws = null;
  let retryMs = 500;

  function setConn(state) {
    dot.dataset.state = state;
    if (state === "connected")        status.textContent = "connected";
    else if (state === "connecting")  status.textContent = "connecting…";
    else                              status.textContent = "disconnected — retrying…";
  }

  function appendEvent(msg) {
    const li = document.createElement("li");
    const kind = document.createElement("span"); kind.className = "kind"; kind.textContent = msg.kind;
    const txt  = document.createElement("span"); txt.className  = "txt";  txt.textContent  = JSON.stringify(msg.payload || {});
    li.appendChild(kind); li.appendChild(txt);
    feed.prepend(li);
    while (feed.children.length > 80) feed.removeChild(feed.lastChild);
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }
  function sendBytes(data) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(data);
  }

  // ------------------------------------------------------------------ TTS playback
  let currentUtt = null;     // {id, format, chunks: []}
  let playQueue  = [];       // pending Blob URLs to play sequentially
  let playing    = false;

  function queueBlobUrl(url) {
    playQueue.push(url);
    if (!playing) playNext();
  }
  function playNext() {
    const url = playQueue.shift();
    if (!url) { playing = false; return; }
    playing = true;
    player.src = url;
    player.play().catch(() => { /* autoplay may be blocked until first interaction */ });
    player.onended = () => { URL.revokeObjectURL(url); playNext(); };
  }
  function stopPlayback() {
    try { player.pause(); } catch {}
    playQueue.forEach(URL.revokeObjectURL);
    playQueue = [];
    playing = false;
    if (currentUtt) currentUtt.aborted = true;
  }

  // ------------------------------------------------------------------ WebSocket
  function connect() {
    setConn("connecting");
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.binaryType = "arraybuffer";

    ws.onopen  = () => { setConn("connected"); retryMs = 500; };
    ws.onclose = () => { setConn("disconnected"); setTimeout(connect, retryMs); retryMs = Math.min(retryMs*2, 5000); };
    ws.onerror = () => { /* onclose handles retry */ };

    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        let msg; try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === "event") {
          appendEvent(msg);
        } else if (msg.type === "audio_header") {
          currentUtt = { id: msg.utterance_id, format: msg.format || "mp3", chunks: [], aborted: false };
        } else if (msg.type === "audio_end") {
          if (!currentUtt) return;
          if (!currentUtt.aborted && !msg.aborted && !msg.error && currentUtt.chunks.length) {
            const mime = currentUtt.format === "mp3" ? "audio/mpeg"
                       : currentUtt.format === "ogg" ? "audio/ogg"
                       : currentUtt.format === "wav" ? "audio/wav"
                       : "audio/webm";
            const blob = new Blob(currentUtt.chunks, { type: mime });
            queueBlobUrl(URL.createObjectURL(blob));
          }
          currentUtt = null;
        } else if (msg.type === "audio_interrupt") {
          stopPlayback();
          currentUtt = null;
        }
      } else {
        // Binary frame — append to current utterance.
        if (currentUtt) currentUtt.chunks.push(ev.data);
      }
    };
  }
  connect();

  // ------------------------------------------------------------------ buttons
  const btn = (id, cmd, toggles) => {
    const el = document.getElementById(id);
    el.addEventListener("click", () => {
      send({ type: "cmd", cmd });
      if (toggles) el.classList.toggle("active");
    });
  };
  btn("btn-play",      "play");
  btn("btn-pause",     "pause");
  btn("btn-mute",      "mute", true);
  btn("btn-interrupt", "interrupt");

  // ------------------------------------------------------------------ mic capture (push-to-talk)
  const speak = document.getElementById("btn-speak");
  let recorder = null;
  let micStream = null;

  function pickMimeType() {
    const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/ogg"];
    for (const m of candidates) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) return m;
    }
    return "";
  }

  async function startSpeak() {
    speak.classList.add("active");
    stopPlayback();  // barge-in: pause any narration
    send({ type: "cmd", cmd: "speak_start" });
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      status.textContent = "microphone denied";
      return;
    }
    const mime = pickMimeType();
    const codec = mime.startsWith("audio/webm") ? "webm" : mime.startsWith("audio/ogg") ? "ogg" : "webm";
    send({ type: "cmd", cmd: "mic_start", codec });
    recorder = new MediaRecorder(micStream, mime ? { mimeType: mime } : undefined);
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) e.data.arrayBuffer().then(sendBytes);
    };
    recorder.start(150);  // 150ms chunks — low-latency-ish
  }

  function endSpeak() {
    speak.classList.remove("active");
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

  speak.addEventListener("mousedown", startSpeak);
  speak.addEventListener("mouseup",   endSpeak);
  speak.addEventListener("mouseleave", () => speak.classList.contains("active") && endSpeak());
  speak.addEventListener("touchstart", (e) => { e.preventDefault(); startSpeak(); }, { passive: false });
  speak.addEventListener("touchend",   (e) => { e.preventDefault(); endSpeak(); });

  let speaking = false;
  window.addEventListener("keydown", (e) => {
    if (e.altKey && e.code === "Space" && !speaking) { speaking = true; e.preventDefault(); startSpeak(); }
  });
  window.addEventListener("keyup", (e) => {
    if (speaking && (e.code === "Space" || e.key === "Alt")) { speaking = false; endSpeak(); }
  });
})();
