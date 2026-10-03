// Web client for the /v1 chat API: home, chat and voice views. No framework.
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const app = $("app");
  const log = $("log");
  const chips = $("chips");
  const chatForm = $("chat-ask");
  const chatInput = $("chat-text");
  const homeForm = $("home-ask");
  const homeInput = $("home-text");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const PACE = reduceMotion ? 0 : 420;
  const STORE_KEY = "advisor.bookings.v1";

  const TOPIC_LABEL = {
    "KYC/Onboarding": "KYC & onboarding",
    "SIP/Mandates": "SIPs & mandates",
    "Statements/Tax Docs": "Statements & tax docs",
    "Withdrawals & Timelines": "Withdrawals & timelines",
    "Account Changes/Nominee": "Account changes & nominee",
  };
  const INLINE_ACTION_STATES = new Set(["confirm_slot", "cancel_confirm", "disclaimer_ack", "await_pivot"]);

  const state = {
    sessionId: null,
    busy: false,
    done: false,
    lastReply: null,
    starting: null,
  };

  // ---------------------------------------------------------------- helpers
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const esc = (s) =>
    String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const plain = (s) => String(s).replace(/\*\*/g, "");
  const topicLabel = (t) => (t ? TOPIC_LABEL[t] || t : null);

  function rich(s) {
    return esc(s)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/https?:\/\/[^\s<,]+/g, (url) => {
        let label = "Open link";
        try { label = new URL(url.replace(/&amp;/g, "&")).hostname.replace(/^www\./, ""); } catch (_) { /* keep */ }
        return `<a href="${url}" target="_blank" rel="noopener">${esc(label)}</a>`;
      });
  }

  function splitSlot(s) {
    const m = /^(\w+), (\d{1,2} \w+)(?: \d{4})?, (.+?)(?: IST)?$/.exec(s || "");
    return m ? { day: m[1], date: m[2], time: m[3] } : { day: s || "", date: "", time: "" };
  }

  const ist = (opts) => new Intl.DateTimeFormat("en-IN", { timeZone: "Asia/Kolkata", ...opts });
  function istParts() {
    const now = new Date();
    const hour = Number(ist({ hour: "numeric", hour12: false }).format(now)) % 24;
    const weekday = ist({ weekday: "short" }).format(now);
    return { now, hour, weekday };
  }

  // ---------------------------------------------------------------- local bookings (codes only, no PII)
  function loadBookings() {
    try { return JSON.parse(localStorage.getItem(STORE_KEY)) || []; } catch (_) { return []; }
  }
  function saveBooking(entry) {
    if (!entry.code) return;
    const list = loadBookings().filter((b) => b.code !== entry.code);
    const prev = loadBookings().find((b) => b.code === entry.code) || {};
    list.unshift({ ...prev, ...entry, ts: Date.now() });
    try { localStorage.setItem(STORE_KEY, JSON.stringify(list.slice(0, 5))); } catch (_) { /* storage full or blocked */ }
  }

  // ---------------------------------------------------------------- home
  function renderHome() {
    const { now, hour, weekday } = istParts();
    $("greeting").textContent = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
    $("today").textContent = ist({ weekday: "long", day: "numeric", month: "long" }).format(now) + " · IST";
    $("ist-clock").textContent = ist({ hour: "numeric", minute: "2-digit" }).format(now) + " IST";

    const open = !["Sat", "Sun"].includes(weekday) && hour >= 9 && hour < 18;
    const status = $("desk-status");
    status.textContent = open ? "Advisors in now" : "Desk closed · book ahead";
    status.classList.toggle("closed", !open);

    const list = $("desk-list");
    const bookings = loadBookings();
    if (!bookings.length) {
      list.innerHTML = `<li class="empty">Bookings you make on this device show up here with their code.</li>`;
    } else {
      list.innerHTML = bookings.slice(0, 3).map((b) => {
        const s = splitSlot(b.slot);
        const dot = b.status === "cancelled" ? "off" : b.waitlist ? "wait" : "";
        const when = b.status === "cancelled" ? "Cancelled" : b.waitlist ? "Waitlist · an advisor will propose a time" : `${s.day}, ${s.date} · ${s.time}`;
        return `<li><span class="dot ${dot}"></span><span><b>${esc(topicLabel(b.topic) || "Advisor call")}</b><small>${esc(when)}</small></span><button type="button" class="code" data-code="${esc(b.code)}" aria-label="Copy code ${esc(b.code)}">${esc(b.code)}</button></li>`;
      }).join("");
    }

    const tiles = $("tiles");
    tiles.querySelector(".resume")?.remove();
    if (state.sessionId && !state.done && log.children.length) {
      const resume = document.createElement("button");
      resume.className = "tile resume";
      resume.innerHTML = `<span class="t">Continue your conversation</span>${icon("arrow")}`;
      resume.addEventListener("click", () => show("chat"));
      tiles.prepend(resume);
    }
  }

  $("desk-list").addEventListener("click", (e) => {
    const b = e.target.closest("[data-code]");
    if (!b) return;
    copy(b.dataset.code, b);
  });

  function copy(text, el) {
    navigator.clipboard?.writeText(text).then(() => {
      const prev = el.innerHTML;
      el.textContent = "Copied";
      setTimeout(() => { el.innerHTML = prev; }, 1400);
    }).catch(() => {});
  }

  // ---------------------------------------------------------------- views
  function show(view) {
    app.dataset.view = view;
    if (view === "home") renderHome();
    if (view === "chat") {
      requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
      if (!state.busy && !state.done) chatInput.focus({ preventScroll: true });
    }
  }

  // ---------------------------------------------------------------- API
  async function api(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const err = new Error(`HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  function ensureSession() {
    if (state.sessionId && !state.done) return Promise.resolve(null);
    if (state.starting) return state.starting;
    resetThread();
    setBusy(true);
    const typing = addTyping();
    state.starting = api("/v1/sessions")
      .then(async (reply) => {
        typing.remove();
        state.sessionId = reply.session_id;
        await deliver(reply);
        return reply;
      })
      .catch(() => {
        typing.remove();
        addSystem("I couldn't reach the advisor desk. Check your connection and try again.", () => ensureSession());
        throw new Error("start-failed");
      })
      .finally(() => {
        state.starting = null;
        setBusy(false);
      });
    return state.starting;
  }

  // `shown` is what the user said (voice transcript); `text` is what the agent receives.
  async function send(text, { fromVoice = false, shown = null } = {}) {
    text = String(text || "").trim();
    if (!text || state.busy) return null;
    try { await ensureSession(); } catch (_) { return null; }
    if (state.done) return null;
    lockInteractive(text);
    addUser(shown || text);
    chatInput.value = "";
    homeInput.value = "";
    setBusy(true);
    const typing = addTyping();
    try {
      const reply = await api(`/v1/sessions/${state.sessionId}/messages`, { user_text: text });
      typing.remove();
      await deliver(reply, { fromVoice });
      return reply;
    } catch (err) {
      typing.remove();
      if (err.status === 404) {
        state.sessionId = null;
        addSystem("That chat timed out, so I've started a fresh one.");
        try { return await ensureSession(); } catch (_) { return null; }
      }
      addSystem("That didn't go through. Your message wasn't lost.", () => send(text, { fromVoice, shown }));
      return null;
    } finally {
      setBusy(false);
      if (!state.done && app.dataset.view === "chat") chatInput.focus({ preventScroll: true });
    }
  }

  // ---------------------------------------------------------------- thread rendering
  function scrollEnd() { log.scrollTop = log.scrollHeight; }

  function addUser(text) {
    const div = document.createElement("div");
    div.className = "msg me";
    div.textContent = text;
    log.appendChild(div);
    scrollEnd();
  }

  function agentShell() {
    const div = document.createElement("div");
    div.className = "msg agent";
    div.innerHTML = `<span class="avatar">${icon("mark")}</span><div class="bubble"></div>`;
    log.appendChild(div);
    return div;
  }

  function addTyping() {
    const div = agentShell();
    div.classList.add("typing");
    div.querySelector(".bubble").innerHTML = "<i></i><i></i><i></i>";
    div.setAttribute("aria-label", "Advisor desk is typing");
    scrollEnd();
    return div;
  }

  function addSystem(text, retry) {
    const div = agentShell();
    div.classList.add("sys");
    const bubble = div.querySelector(".bubble");
    bubble.innerHTML = `<p>${esc(text)}</p>`;
    if (retry) {
      const b = document.createElement("button");
      b.className = "btn";
      b.type = "button";
      b.textContent = "Try again";
      b.addEventListener("click", () => { div.remove(); retry(); });
      bubble.appendChild(b);
    }
    scrollEnd();
  }

  function slotRow(slot, label, idx) {
    const s = splitSlot(slot);
    const b = document.createElement("button");
    b.type = "button";
    b.className = "slot";
    b.setAttribute("aria-pressed", "false");
    b.dataset.say = label;
    b.innerHTML = `<span class="dot"></span><span><b>${esc(s.day)}, ${esc(s.date)}</b><small>Option ${idx + 1} · advisor call</small></span><span class="time">${esc(s.time)} IST</span>`;
    b.setAttribute("aria-label", `Option ${idx + 1}: ${slot}`);
    return b;
  }

  // Builds one message's HTML/DOM, turning slot offers and the secure link into real controls.
  function messageNode(text, reply) {
    const p = document.createElement("div");
    const d = reply.details || {};
    const offered = d.offered || [];
    const labels = reply.suggestions || [];

    const two = /^(.*?)1\) \*\*(.+?)\*\* or 2\) \*\*(.+?)\*\*\.?\s*(.*)$/.exec(text);
    if (two) {
      const s1 = offered[0] || two[2];
      const s2 = offered[1] || two[3];
      p.innerHTML = `<p>${rich(two[1].replace(/:\s*$/, "."))}</p>`;
      const rows = document.createElement("div");
      rows.className = "slots";
      rows.append(slotRow(s1, labels[0] || "The first one", 0), slotRow(s2, labels[1] || "The second one", 1));
      p.appendChild(rows);
      if (two[4]) p.insertAdjacentHTML("beforeend", `<p>${rich(two[4])}</p>`);
      return p;
    }
    const one = reply.state === "offer_slots" && offered.length === 1 && /^I've got \*\*(.+?)\*\* free\.\s*(.*)$/.exec(text);
    if (one) {
      p.innerHTML = `<p>I've got one time open.</p>`;
      const rows = document.createElement("div");
      rows.className = "slots";
      rows.append(slotRow(offered[0], labels[0] || "yes", 0));
      p.appendChild(rows);
      if (one[2]) p.insertAdjacentHTML("beforeend", `<p>${rich(one[2])}</p>`);
      return p;
    }
    if (reply.secure_url && text.includes(reply.secure_url)) {
      const lead = text.replace(reply.secure_url, "").replace(/[:\s]+$/, ".");
      p.innerHTML = `<p>${rich(lead)}</p><div class="actions inline"><a class="btn primary" href="${esc(reply.secure_url)}" target="_blank" rel="noopener">${icon("lock")}Add contact details</a></div>`;
      return p;
    }
    p.innerHTML = `<p>${rich(text)}</p>`;
    return p;
  }

  function ticketCard(reply, { compact = false } = {}) {
    const d = reply.details || {};
    const code = reply.booking_code || (d.flow !== "book" ? d.code : null);
    const cancelled = reply.messages.some((m) => /is cancelled and the advisor/.test(m));
    const tag = cancelled ? ["Cancelled", ""] : d.waitlist ? ["Waitlist", "wait"] : reply.booking_code ? ["Tentative", ""] : ["In progress", ""];
    const slot = d.slot || d.current_slot;
    const el = document.createElement("div");
    el.className = "ticketcard";
    el.innerHTML = `
      <div class="row1"><strong>${d.flow === "reschedule" ? "Moving a booking" : d.flow === "cancel" ? "Cancelling" : "Your booking"}</strong><span class="tag ${tag[1]}">${tag[0]}</span></div>
      ${code ? `<p class="bigcode">${esc(code)}</p>` : ""}
      ${compact && slot ? `<p class="when">${esc(slot)}</p>` : ""}`;
    if (!compact) {
      const steps = [
        ["Topic", topicLabel(d.topic)],
        ["Preferred time", d.preference],
        d.flow === "reschedule" && d.current_slot ? ["Currently", d.current_slot, d.slot ? "old" : ""] : null,
        [d.flow === "reschedule" ? "New slot" : "Slot", d.waitlist ? "Waitlist — an advisor will propose a time" : d.slot || (d.offered?.length ? `${d.offered.length} option${d.offered.length > 1 ? "s" : ""} offered` : null)],
        ["Booking code", reply.booking_code || (d.flow !== "book" && d.code) || null],
      ].filter(Boolean);
      const ol = document.createElement("ol");
      ol.className = "steps";
      ol.innerHTML = steps.map(([k, v, cls]) =>
        `<li class="${v ? "done" : ""}"><span class="s">${v ? icon("check") : ""}</span><span><small>${k}</small><b class="${cls || ""}">${v ? esc(v) : "—"}</b></span></li>`).join("");
      el.appendChild(ol);
    }
    if ((reply.secure_url && !compact) || code) {
      const acts = document.createElement("div");
      acts.className = "acts";
      if (reply.secure_url && !compact) {
        acts.insertAdjacentHTML("beforeend", `<a class="btn" href="${esc(reply.secure_url)}" target="_blank" rel="noopener">${icon("lock")}Add contact details</a>`);
      }
      if (code) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "btn glass";
        b.innerHTML = `${icon("copy")}Copy code`;
        b.addEventListener("click", () => copy(code, b));
        acts.appendChild(b);
      }
      el.appendChild(acts);
    }
    return el;
  }

  function renderTicket(reply) {
    const d = reply.details || {};
    const relevant = ["book", "reschedule", "cancel"].includes(d.flow) && (d.topic || d.code || reply.booking_code);
    const side = $("ticket");
    const strip = $("ticket-strip");
    if (!relevant) {
      side.innerHTML = `<div class="ticketcard"><div class="row1"><strong>Your booking</strong></div><p class="when">Topic, time and your booking code collect here as we go. All times are IST.</p></div>`;
      strip.hidden = true;
      return;
    }
    side.replaceChildren(ticketCard(reply));
    const bits = [topicLabel(d.topic), d.slot ? splitSlot(d.slot).day + " " + splitSlot(d.slot).time : d.preference, reply.booking_code].filter(Boolean);
    $("ticket-summary").textContent = bits.join(" · ") || "Your booking";
    strip.querySelector(".ticket-slot-mobile").replaceChildren(ticketCard(reply));
    strip.hidden = false;
  }

  function renderChips(reply) {
    chips.innerHTML = "";
    chatForm.hidden = false;
    chatInput.disabled = false;
    if (reply.done) {
      chatForm.hidden = true;
      const again = document.createElement("button");
      again.type = "button";
      again.className = "chip new";
      again.textContent = "Start a new conversation";
      again.addEventListener("click", newConversation);
      const home = document.createElement("button");
      home.type = "button";
      home.className = "chip";
      home.textContent = "Back to home";
      home.addEventListener("click", () => show("home"));
      chips.append(again, home);
      return;
    }
    if (INLINE_ACTION_STATES.has(reply.state)) return;
    let labels = reply.suggestions?.length ? reply.suggestions : reply.quick_replies || [];
    if (reply.state === "offer_slots") labels = labels.slice((reply.details?.offered || []).length);
    labels.forEach((q) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.textContent = q;
      b.addEventListener("click", () => send(q));
      chips.appendChild(b);
    });
  }

  function inlineActions(reply) {
    if (reply.done || !INLINE_ACTION_STATES.has(reply.state)) return null;
    const labels = reply.suggestions?.length ? reply.suggestions : reply.quick_replies || [];
    if (!labels.length) return null;
    const wrap = document.createElement("div");
    wrap.className = "actions";
    labels.forEach((q, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn" + (i === 0 ? (reply.state === "cancel_confirm" ? " danger" : " primary") : "");
      b.dataset.say = q;
      b.textContent = q;
      wrap.appendChild(b);
    });
    return wrap;
  }

  // After the user answers, earlier slot rows and inline actions stop being live.
  function lockInteractive(chosenText) {
    log.querySelectorAll(".slot:not([disabled]), .actions .btn:not([disabled])").forEach((b) => {
      b.disabled = true;
      if (b.dataset.say === chosenText) b.setAttribute("aria-pressed", "true");
    });
    log.querySelectorAll(".actions:not(.inline)").forEach((g) => {
      if (!g.querySelector('[aria-pressed="true"]')) g.remove();
    });
  }

  log.addEventListener("click", (e) => {
    const b = e.target.closest("[data-say]");
    if (b && !b.disabled) send(b.dataset.say);
  });

  async function deliver(reply, { fromVoice = false } = {}) {
    state.lastReply = reply;
    const msgs = reply.messages || [];
    let lastShell = null;
    for (let i = 0; i < msgs.length; i++) {
      if (i > 0 && PACE && !fromVoice) {
        const t = addTyping();
        await sleep(PACE);
        t.remove();
      }
      lastShell = agentShell();
      lastShell.querySelector(".bubble").appendChild(messageNode(msgs[i], reply));
      const bookedNow = reply.booking_code && /^(You're all set|Done! Your|Nothing's free)/.test(msgs[i]);
      if (bookedNow) lastShell.querySelector(".bubble").appendChild(ticketCard(reply, { compact: true }));
      scrollEnd();
    }
    const acts = inlineActions(reply);
    if (acts && lastShell) lastShell.appendChild(acts);
    state.done = Boolean(reply.done);
    renderTicket(reply);
    renderChips(reply);
    remember(reply);
    scrollEnd();
    voice.onReply(reply);
  }

  function remember(reply) {
    const d = reply.details || {};
    const cancelled = reply.messages.find((m) => /booking ([A-Z]{2}-[A-Z0-9]{4}) is cancelled/.test(m));
    if (cancelled) {
      saveBooking({ code: /([A-Z]{2}-[A-Z0-9]{4})/.exec(cancelled)[1], status: "cancelled" });
      return;
    }
    if (reply.booking_code && (d.slot || d.waitlist)) {
      saveBooking({ code: reply.booking_code, slot: d.slot, topic: d.topic, waitlist: Boolean(d.waitlist), status: d.waitlist ? "waitlist" : "tentative" });
    }
  }

  function resetThread() {
    log.innerHTML = "";
    chips.innerHTML = "";
    chatForm.hidden = false;
    state.done = false;
    state.lastReply = null;
    renderTicket({ messages: [], details: {} });
  }

  function newConversation() {
    voice.stop();
    state.sessionId = null;
    state.done = false;
    show("chat");
    ensureSession().catch(() => {});
  }

  function setBusy(busy) {
    state.busy = busy;
    chatInput.disabled = busy;
    chatForm.querySelectorAll("button[type=submit]").forEach((b) => { b.disabled = busy; });
    chips.querySelectorAll("button").forEach((b) => { b.disabled = busy; });
  }

  // ---------------------------------------------------------------- voice controller
  const voice = (() => {
    const { BrowserVoiceEngine, createEngine, unlockAudio } = window.AdvisorVoice;
    const orb = $("orb");
    const vstate = $("vstate");
    const heard = $("heard");
    const line = $("agent-line");
    const note = $("vnote");
    const pauseBtn = $("v-pause");
    const PII_HINT = "Don't say phone, email or account numbers — I'll send a secure link for those.";
    let engine = null;
    let fallbackEngine = null;
    const ready = createEngine({ lang: "en-IN" }).then((e) => { engine = e; return e; });
    let mode = "idle";
    let gen = 0;

    const LABEL = {
      idle: "Tap the circle to talk",
      listening: "Listening — tap when you're done",
      thinking: "Thinking…",
      speaking: "Speaking — tap to interrupt",
      paused: "Paused",
      ended: "That's everything",
    };
    function set(m) {
      mode = m;
      orb.dataset.state = m;
      vstate.textContent = LABEL[m];
      orb.setAttribute("aria-label", m === "listening" ? "Done talking" : m === "speaking" ? "Interrupt and talk" : "Start talking");
      const paused = m === "paused";
      pauseBtn.innerHTML = icon(paused ? "play" : "pause");
      pauseBtn.setAttribute("aria-label", paused ? "Resume" : "Pause");
      if (m !== "listening") orb.style.setProperty("--lvl", "0");
    }

    function describe() {
      if (engine.kind === "cloud") {
        note.textContent = `Natural voice by Google Cloud (Indian English). ${PII_HINT}`;
      } else if (engine.supported.stt) {
        note.textContent = `Using your browser's speech engine. ${PII_HINT}`;
      } else {
        note.textContent = "Voice input isn't available in this browser (try Chrome or Edge). You'll still hear replies — use the keyboard to answer.";
      }
      orb.toggleAttribute("data-live", engine.kind === "cloud");
    }

    // Google speech is unavailable (API off, quota, no credentials): carry on with the browser's.
    function useBrowserVoice(detail) {
      if (engine.kind === "browser") return;
      engine.release();
      engine = new BrowserVoiceEngine({ lang: "en-IN" });
      describe();
      note.textContent = `Google voice isn't available right now${detail ? ` (${detail.replace(/\.$/, "")})` : ""}, so I've switched to your browser's voice. ${PII_HINT}`;
    }

    function caption(reply) {
      const last = (reply.messages || []).slice(-2).map(plain).join(" ");
      line.textContent = last.replace(/https?:\/\/\S+/g, "(link on screen)");
    }

    async function say(reply) {
      const my = gen;
      caption(reply);
      set("speaking");
      try {
        await engine.speak(reply);
      } catch (err) {
        if (my !== gen) return;
        if (err.status === 503) useBrowserVoice(err.detail);
        fallbackEngine = fallbackEngine || new BrowserVoiceEngine({ lang: "en-IN" });
        await (engine.kind === "browser" ? engine : fallbackEngine).speak(reply);
      }
      if (my !== gen || app.dataset.view !== "voice") return;
      if (reply.done) { set("ended"); return; }
      listen();
    }

    function listen() {
      if (!engine.supported.stt) { set("idle"); return; }
      const my = ++gen;
      engine.cancelSpeech();
      fallbackEngine?.cancelSpeech();
      heard.innerHTML = "";
      set("listening");
      engine.listen({
        onInterim(finalText, interim) {
          heard.innerHTML = `${esc(finalText)}<span class="interim">${esc(interim)}</span>`;
        },
        onLevel(level) {
          if (my === gen) orb.style.setProperty("--lvl", level.toFixed(2));
        },
        onProcessing() {
          if (my !== gen) return;
          set("thinking");
          vstate.textContent = "Got it — one moment…";
        },
        async onFinal(text, shownText) {
          if (my !== gen) return;
          heard.textContent = shownText || text;
          set("thinking");
          const reply = await send(text, { fromVoice: true, shown: shownText });
          if (!reply && my === gen && app.dataset.view === "voice") set("idle");
        },
        onEnd() { if (my === gen) set("idle"); },
        onError(err, detail) {
          if (my !== gen) return;
          set("idle");
          if (err === "not-allowed" || err === "service-not-allowed") {
            note.textContent = "Microphone access is blocked. Allow it in your browser settings, or type instead.";
          } else if (err === "no-speech") {
            vstate.textContent = "I didn't catch that — tap to try again";
          } else if (err === "unavailable") {
            useBrowserVoice(detail);
            vstate.textContent = "Tap the circle and say that again";
          } else if (err !== "aborted") {
            note.textContent = "Voice input hit a snag. Tap the circle to try again, or type instead.";
          }
        },
      });
    }

    function stop() {
      gen++;
      if (engine) {
        engine.release();
        engine.cancelSpeech();
      }
      fallbackEngine?.cancelSpeech();
      set("idle");
    }

    async function enter() {
      unlockAudio();
      show("voice");
      heard.textContent = "";
      set("thinking");
      await ready;
      describe();
      let started = null;
      try { started = await ensureSession(); } catch (_) { set("idle"); return; }
      // A freshly started session is spoken by onReply; an existing one replays its last turn.
      if (started) return;
      if (state.lastReply) say(state.lastReply);
      else set("idle");
    }

    orb.addEventListener("click", () => {
      unlockAudio();
      if (!engine) return;
      if (mode === "listening") { engine.finishListening(); return; }
      if (mode === "thinking") return;
      if (mode === "ended") { newConversation(); enter(); return; }
      gen++;
      listen();
    });
    pauseBtn.addEventListener("click", () => {
      unlockAudio();
      if (!engine) return;
      if (mode === "paused") { listen(); return; }
      stop();
      set("paused");
    });
    $("v-end").addEventListener("click", () => { stop(); show("chat"); });
    $("v-type").addEventListener("click", () => { stop(); show("chat"); chatInput.focus(); });

    return {
      enter,
      stop,
      onReply(reply) {
        if (engine && app.dataset.view === "voice" && mode !== "paused") say(reply);
      },
    };
  })();

  // ---------------------------------------------------------------- wiring
  $("tiles").addEventListener("click", (e) => {
    const t = e.target.closest("[data-say]");
    if (!t) return;
    if (state.done) state.sessionId = null;
    show("chat");
    send(t.dataset.say);
  });
  homeForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = homeInput.value;
    if (!text.trim()) { homeInput.focus(); return; }
    if (state.done) state.sessionId = null;
    show("chat");
    send(text);
  });
  chatForm.addEventListener("submit", (e) => {
    e.preventDefault();
    send(chatInput.value);
  });
  document.querySelectorAll("[data-voice]").forEach((b) => b.addEventListener("click", () => voice.enter()));
  $("back").addEventListener("click", () => show("home"));
  $("new-chat").addEventListener("click", newConversation);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && app.dataset.view === "voice") { voice.stop(); show("chat"); }
  });

  renderHome();
  renderTicket({ messages: [], details: {} });
  setInterval(() => { if (app.dataset.view === "home") renderHome(); }, 30000);
})();
