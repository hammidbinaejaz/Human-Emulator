"use strict";

// Push-to-talk: record in the browser, stop automatically when you stop talking,
// and transcribe on this Mac (Apple SpeechAnalyzer via /api/voice/transcribe).
window.Voice = (() => {
  const TARGET_RATE = 16000;
  const hud = document.getElementById("voice-hud");
  const stateEl = document.getElementById("voice-state");
  const textEl = document.getElementById("voice-text");
  const wave = document.getElementById("voice-wave");
  const mic = document.getElementById("mic");
  let session = null;
  let hideTimer = null;
  const handlers = { onText: () => {}, onError: () => {} };

  function show(mode, label, text = "") {
    clearTimeout(hideTimer);
    hud.hidden = false;
    hud.dataset.mode = mode;
    stateEl.textContent = label;
    textEl.textContent = text;
    mic.classList.toggle("live", mode === "listening");
  }

  function hide(after = 0) {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      hud.hidden = true;
      mic.classList.remove("live");
    }, after);
  }

  function encodeWav(chunks, rate) {
    const total = chunks.reduce((n, c) => n + c.length, 0);
    const ratio = rate / TARGET_RATE;
    const length = Math.floor(total / ratio);
    const pcm = new Int16Array(length);
    const flat = new Float32Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      flat.set(chunk, offset);
      offset += chunk.length;
    }
    for (let i = 0; i < length; i++) {
      const from = Math.floor(i * ratio);
      const to = Math.min(total, Math.floor((i + 1) * ratio));
      let sum = 0;
      for (let j = from; j < to; j++) sum += flat[j];
      const value = Math.max(-1, Math.min(1, sum / Math.max(1, to - from)));
      pcm[i] = value < 0 ? value * 0x8000 : value * 0x7fff;
    }
    const buffer = new ArrayBuffer(44 + pcm.byteLength);
    const view = new DataView(buffer);
    const text = (at, s) => [...s].forEach((ch, i) => view.setUint8(at + i, ch.charCodeAt(0)));
    text(0, "RIFF");
    view.setUint32(4, 36 + pcm.byteLength, true);
    text(8, "WAVE");
    text(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, TARGET_RATE, true);
    view.setUint32(28, TARGET_RATE * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    text(36, "data");
    view.setUint32(40, pcm.byteLength, true);
    new Int16Array(buffer, 44).set(pcm);
    return new Blob([buffer], { type: "audio/wav" });
  }

  function drawWave() {
    if (!session) return;
    const ctx = wave.getContext("2d");
    const { width, height } = wave;
    const data = new Uint8Array(session.analyser.fftSize);
    session.analyser.getByteTimeDomainData(data);
    ctx.clearRect(0, 0, width, height);
    ctx.lineWidth = 2;
    ctx.strokeStyle = "rgba(212, 255, 58, 0.9)";
    ctx.beginPath();
    for (let i = 0; i < data.length; i++) {
      const x = (i / (data.length - 1)) * width;
      const y = height / 2 + ((data[i] - 128) / 128) * height * 0.9;
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.stroke();
    hud.style.setProperty("--level", session.level.toFixed(3));
    session.frame = requestAnimationFrame(drawWave);
  }

  function teardown() {
    if (!session) return null;
    const s = session;
    session = null;
    cancelAnimationFrame(s.frame);
    s.proc.onaudioprocess = null;
    s.proc.disconnect();
    s.source.disconnect();
    s.stream.getTracks().forEach((t) => t.stop());
    s.ctx.close();
    return s;
  }

  async function finish() {
    const s = teardown();
    if (!s) return;
    if (!s.heard) {
      show("idle", "I didn't hear anything");
      hide(1400);
      return;
    }
    show("thinking", "Transcribing on this Mac…");
    try {
      const response = await fetch("/api/voice/transcribe", {
        method: "POST",
        headers: { "Content-Type": "audio/wav", "X-Case-Client": "dashboard" },
        body: encodeWav(s.chunks, s.rate),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || "Transcription failed");
      if (!body.text) {
        show("idle", "Didn't catch that — try again");
        hide(1600);
        return;
      }
      show("heard", `Heard in ${(body.ms / 1000).toFixed(1)}s · on-device`, `“${body.text}”`);
      hide(1500);
      handlers.onText(body.text, s.followUp);
    } catch (error) {
      show("idle", error.message);
      hide(2600);
      handlers.onError(error.message);
    }
  }

  async function start({ followUp = false } = {}) {
    if (session) return finish();
    if (!navigator.mediaDevices?.getUserMedia) {
      handlers.onError("This browser cannot use the microphone.");
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch {
      handlers.onError("Microphone access is blocked — allow it for this page in your browser.");
      return;
    }
    const ctx = new AudioContext();
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    source.connect(analyser);
    source.connect(proc);
    proc.connect(ctx.destination);
    const s = {
      ctx, stream, source, analyser, proc, chunks: [], rate: ctx.sampleRate,
      started: performance.now(), heard: false, lastVoice: 0, floor: 0, samples: 0, level: 0, followUp, frame: 0,
    };
    session = s;
    proc.onaudioprocess = (event) => {
      if (session !== s) return;
      const data = event.inputBuffer.getChannelData(0);
      s.chunks.push(new Float32Array(data));
      let sum = 0;
      for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
      const rms = Math.sqrt(sum / data.length);
      const now = performance.now();
      const elapsed = now - s.started;
      s.level = Math.min(1, rms * 9);
      if (elapsed < 300) {
        s.floor = (s.floor * s.samples + rms) / (s.samples + 1);
        s.samples += 1;
        return;
      }
      if (rms > Math.max(0.018, s.floor * 2.6)) {
        s.heard = true;
        s.lastVoice = now;
        if (Math.random() < 0.5) window.Edges?.token();
      }
      if ((s.heard && now - s.lastVoice > 1100) || elapsed > 15000 || (!s.heard && elapsed > (followUp ? 5000 : 8000))) {
        finish();
      }
    };
    show("listening", followUp ? "Listening for your answer…" : "Listening…", "Speak naturally — I stop when you pause.");
    drawWave();
  }

  function cancel() {
    if (teardown()) show("idle", "Cancelled");
    hide(500);
  }

  mic.addEventListener("click", () => start());
  document.addEventListener("keydown", (event) => {
    if (event.code === "Space" && event.altKey && !event.metaKey && !event.ctrlKey) {
      event.preventDefault();
      start();
    } else if (event.key === "Escape" && session) {
      cancel();
    }
  });
  hud.addEventListener("click", (event) => {
    if (event.target === hud) (session ? finish() : hide());
  });

  return {
    start,
    cancel,
    encodeWav,
    on(name, fn) {
      handlers[name] = fn;
    },
    get listening() {
      return Boolean(session);
    },
  };
})();
