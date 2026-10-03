// Voice engine seam. The UI talks only to this interface:
//   engine.kind                 -> "cloud" | "browser"
//   engine.supported            -> { stt: bool, tts: bool }
//   engine.listen({ onInterim, onFinal(userText, shownText), onEnd, onError(code, detail),
//                   onProcessing, onLevel(0..1) })   -> captures one utterance
//   engine.finishListening()    -> "I'm done talking": submit what was heard so far
//   engine.stopListening()      -> abandon the current utterance
//   engine.speak(reply)         -> Promise; resolves when speech ends or is cancelled,
//                                  rejects with err.status / err.detail if the engine failed
//   engine.cancelSpeech()
//   engine.release()            -> let go of the microphone
// CloudVoiceEngine uses the server's Google Cloud Speech-to-Text / Text-to-Speech routes
// (/v1/voice/*). BrowserVoiceEngine is the Web Speech API fallback.
(function () {
  "use strict";

  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  let sharedCtx = null;

  // Browsers only allow audio after a user gesture: call this synchronously in click handlers.
  function unlockAudio() {
    if (!AudioCtx) return null;
    if (!sharedCtx) sharedCtx = new AudioCtx();
    if (sharedCtx.state === "suspended") sharedCtx.resume().catch(() => {});
    return sharedCtx;
  }

  class VoiceError extends Error {
    constructor(status, detail) {
      super(detail || `voice request failed (${status})`);
      this.status = status;
      this.detail = detail || "";
    }
  }

  async function failure(res) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch (_) { /* not JSON */ }
    return new VoiceError(res.status, typeof detail === "string" ? detail : "");
  }

  // ------------------------------------------------------------------ audio helpers
  function downsample(samples, from, to) {
    if (from === to) return samples;
    const ratio = from / to;
    const out = new Float32Array(Math.floor(samples.length / ratio));
    for (let i = 0; i < out.length; i++) {
      const start = Math.floor(i * ratio);
      const end = Math.min(samples.length, Math.floor((i + 1) * ratio));
      let sum = 0;
      for (let j = start; j < end; j++) sum += samples[j];
      out[i] = sum / Math.max(1, end - start);
    }
    return out;
  }

  function encodeWav(samples, rate) {
    const buf = new ArrayBuffer(44 + samples.length * 2);
    const v = new DataView(buf);
    const text = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    text(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); text(8, "WAVE");
    text(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true);
    v.setUint16(34, 16, true); text(36, "data"); v.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Blob([buf], { type: "audio/wav" });
  }

  function concat(chunks) {
    const out = new Float32Array(chunks.reduce((n, c) => n + c.length, 0));
    let o = 0;
    for (const c of chunks) { out.set(c, o); o += c.length; }
    return out;
  }

  // ------------------------------------------------------------------ Google Cloud engine
  class CloudVoiceEngine {
    constructor({ lang = "en-IN", maxSeconds = 15, base = "" } = {}) {
      this.kind = "cloud";
      this.lang = lang;
      this.base = base;
      this.maxMs = maxSeconds * 1000;
      this.supported = {
        stt: Boolean(AudioCtx && navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
        tts: Boolean(AudioCtx),
      };
      this._stream = null;
      this._turn = null;
      this._capture = null;
      this._speech = null;
    }

    async _mic() {
      const live = this._stream && this._stream.getAudioTracks().some((t) => t.readyState === "live");
      if (!live) {
        this._stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
      }
      return this._stream;
    }

    listen(h = {}) {
      this.stopListening();
      const turn = {};
      this._turn = turn;
      this._capture_(turn, h).catch(() => { if (this._turn === turn) h.onError?.("audio-capture"); });
    }

    async _capture_(turn, h) {
      let stream;
      try {
        stream = await this._mic();
      } catch (e) {
        if (this._turn !== turn) return;
        this._turn = null;
        const blocked = e && (e.name === "NotAllowedError" || e.name === "SecurityError");
        h.onError?.(blocked ? "not-allowed" : "audio-capture");
        return;
      }
      if (this._turn !== turn) return;
      const ctx = unlockAudio();
      if (ctx.state === "suspended") await ctx.resume().catch(() => {});

      const size = 2048;
      const src = ctx.createMediaStreamSource(stream);
      const proc = ctx.createScriptProcessor(size, 1, 1);
      const mute = ctx.createGain();
      mute.gain.value = 0;
      src.connect(proc); proc.connect(mute); mute.connect(ctx.destination);

      const rate = ctx.sampleRate;
      const chunkMs = (size / rate) * 1000;
      const preRoll = Math.ceil(300 / chunkMs);
      const chunks = [];
      const pre = [];
      let floor = 0, calibrated = 0, started = false, silentMs = 0, speechMs = 0, elapsed = 0;
      let closed = false;

      const close = () => {
        if (closed) return false;
        closed = true;
        proc.onaudioprocess = null;
        try { src.disconnect(); proc.disconnect(); mute.disconnect(); } catch (_) { /* gone */ }
        this._capture = null;
        h.onLevel?.(0);
        return true;
      };

      const submit = async () => {
        if (!close() || this._turn !== turn) return;
        if (!started || speechMs < 200) { this._turn = null; h.onError?.("no-speech"); return; }
        h.onProcessing?.();
        const wav = encodeWav(downsample(concat(chunks), rate, 16000), 16000);
        try {
          const res = await fetch(`${this.base}/v1/voice/transcribe`, {
            method: "POST", headers: { "Content-Type": "audio/wav" }, body: wav,
          });
          if (this._turn !== turn) return;
          this._turn = null;
          if (!res.ok) {
            const err = await failure(res);
            h.onError?.(res.status === 503 ? "unavailable" : "network", err.detail);
            return;
          }
          const data = await res.json();
          if (!data.user_text) { h.onError?.("no-speech"); return; }
          h.onFinal?.(data.user_text, data.transcript || data.user_text);
        } catch (_) {
          if (this._turn === turn) { this._turn = null; h.onError?.("network"); }
        }
      };

      this._capture = { submit, abort: close };

      proc.onaudioprocess = (e) => {
        const data = new Float32Array(e.inputBuffer.getChannelData(0));
        let sum = 0;
        for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
        const rms = Math.sqrt(sum / data.length);
        elapsed += chunkMs;

        if (calibrated < 250) {
          floor = calibrated ? (floor + rms) / 2 : rms;
          calibrated += chunkMs;
          pre.push(data);
          return;
        }
        const threshold = Math.max(0.012, Math.min(floor * 3, 0.06));
        h.onLevel?.(Math.min(1, rms / (threshold * 4)));

        if (!started) {
          pre.push(data);
          if (pre.length > preRoll) pre.shift();
          if (rms > threshold) {
            started = true;
            chunks.push(...pre);
            speechMs += chunkMs;
          } else {
            floor = floor * 0.95 + rms * 0.05;
            if (elapsed > 8000) submit();
          }
          return;
        }
        chunks.push(data);
        if (rms > threshold) { silentMs = 0; speechMs += chunkMs; } else silentMs += chunkMs;
        if (silentMs > 900 || elapsed > this.maxMs) submit();
      };
    }

    finishListening() { this._capture?.submit(); }

    stopListening() {
      this._turn = null;
      this._capture?.abort();
    }

    release() {
      this.stopListening();
      this._stream?.getTracks().forEach((t) => t.stop());
      this._stream = null;
    }

    async speak(reply) {
      const messages = (reply && reply.messages ? reply.messages : [String(reply || "")]).filter(Boolean);
      if (!messages.length) return;
      this.cancelSpeech();
      const job = { abort: new AbortController(), source: null, done: null };
      this._speech = job;
      let buffer;
      try {
        const res = await fetch(`${this.base}/v1/voice/speak`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ messages: messages.slice(0, 10), secure_url: reply.secure_url || null }),
          signal: job.abort.signal,
        });
        if (!res.ok) throw await failure(res);
        const bytes = await res.arrayBuffer();
        if (this._speech !== job) return;
        buffer = await unlockAudio().decodeAudioData(bytes);
      } catch (err) {
        if (this._speech !== job || (err && err.name === "AbortError")) return;
        this._speech = null;
        throw err instanceof VoiceError ? err : new VoiceError(0, "Couldn't play the reply.");
      }
      if (this._speech !== job) return;
      const ctx = unlockAudio();
      if (ctx.state === "suspended") await ctx.resume().catch(() => {});
      await new Promise((resolve) => {
        const src = ctx.createBufferSource();
        src.buffer = buffer;
        src.connect(ctx.destination);
        job.source = src;
        job.done = resolve;
        src.onended = () => { if (this._speech === job) this._speech = null; resolve(); };
        src.start();
      });
    }

    cancelSpeech() {
      const job = this._speech;
      this._speech = null;
      if (!job) return;
      job.abort.abort();
      if (job.source) {
        job.source.onended = null;
        try { job.source.stop(); } catch (_) { /* not started */ }
      }
      job.done?.();
    }
  }

  // ------------------------------------------------------------------ Web Speech fallback
  class BrowserVoiceEngine {
    constructor({ lang = "en-IN" } = {}) {
      this.kind = "browser";
      this.lang = lang;
      const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
      this._Rec = Rec || null;
      this._rec = null;
      this._voice = null;
      this.supported = { stt: Boolean(Rec), tts: "speechSynthesis" in window };
      if (this.supported.tts) {
        const pick = () => {
          const voices = window.speechSynthesis.getVoices();
          this._voice =
            voices.find((v) => v.lang === lang) ||
            voices.find((v) => v.lang && v.lang.startsWith("en-GB")) ||
            voices.find((v) => v.lang && v.lang.startsWith("en")) ||
            null;
        };
        pick();
        window.speechSynthesis.addEventListener?.("voiceschanged", pick);
      }
    }

    listen({ onInterim, onFinal, onEnd, onError } = {}) {
      if (!this._Rec) {
        onError?.("unsupported");
        return;
      }
      this.stopListening();
      const rec = new this._Rec();
      rec.lang = this.lang;
      rec.interimResults = true;
      rec.continuous = false;
      rec.maxAlternatives = 1;
      let finalText = "";
      rec.onresult = (e) => {
        let interim = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const r = e.results[i];
          if (r.isFinal) finalText += r[0].transcript;
          else interim += r[0].transcript;
        }
        onInterim?.(finalText, interim);
      };
      rec.onerror = (e) => {
        this._rec = null;
        onError?.(e.error || "error");
      };
      rec.onend = () => {
        if (this._rec !== rec) return;
        this._rec = null;
        const text = finalText.trim();
        if (text) onFinal?.(text, text);
        else onEnd?.();
      };
      this._rec = rec;
      try {
        rec.start();
      } catch (err) {
        this._rec = null;
        onError?.("start-failed");
      }
    }

    finishListening() {
      try { this._rec?.stop(); } catch (_) { /* already stopped */ }
    }

    stopListening() {
      const rec = this._rec;
      this._rec = null;
      if (rec) {
        rec.onresult = rec.onend = rec.onerror = null;
        try { rec.abort(); } catch (_) { /* already stopped */ }
      }
    }

    release() { this.stopListening(); }

    speak(reply) {
      const text = toSpoken(reply && reply.messages ? reply.messages.join(" ") : reply);
      if (!this.supported.tts || !text) return Promise.resolve();
      this.cancelSpeech();
      return new Promise((resolve) => {
        const u = new SpeechSynthesisUtterance(text);
        u.lang = this.lang;
        if (this._voice) u.voice = this._voice;
        u.rate = 1.02;
        u.onend = u.onerror = () => resolve();
        this._pending = resolve;
        window.speechSynthesis.speak(u);
      });
    }

    cancelSpeech() {
      if (!this.supported.tts) return;
      window.speechSynthesis.cancel();
      const done = this._pending;
      this._pending = null;
      done?.();
    }
  }

  // Turns chat copy into something that sounds right when read aloud (browser voices only;
  // the cloud engine gets SSML from the server's output converter).
  function toSpoken(text) {
    return String(text || "")
      .replace(/\*\*/g, "")
      .replace(/https?:\/\/\S+/g, "I've put the link on your screen")
      .replace(/\b([A-Z]{2})-([A-Z0-9]{4})\b/g, (_, a, b) => [...a, ...b].join(", "))
      .replace(/\s*\(IST\)/g, "")
      .replace(/\bIST\b/g, "India Standard Time")
      .replace(/\(yes \/ no\)/g, "Please say yes or no.")
      .replace(/\bKYC\b/g, "K Y C")
      .replace(/\bSIPs?\b/g, (m) => (m.endsWith("s") ? "S I Ps" : "S I P"));
  }

  // Picks Google Cloud speech when the server has it configured, otherwise the browser's.
  async function createEngine({ lang = "en-IN" } = {}) {
    let cfg = null;
    try {
      const res = await fetch("/v1/voice/config");
      if (res.ok) cfg = await res.json();
    } catch (_) { /* offline: browser fallback */ }
    if (cfg && cfg.provider === "google") {
      const cloud = new CloudVoiceEngine({
        lang: cfg.language || lang,
        maxSeconds: Math.min(15, cfg.max_utterance_s || 15),
      });
      if (cloud.supported.stt && cloud.supported.tts) {
        cloud.info = cfg;
        return cloud;
      }
    }
    const browser = new BrowserVoiceEngine({ lang });
    browser.reason = cfg && cfg.reason;
    return browser;
  }

  window.AdvisorVoice = { CloudVoiceEngine, BrowserVoiceEngine, createEngine, unlockAudio, toSpoken, encodeWav, downsample };
})();
