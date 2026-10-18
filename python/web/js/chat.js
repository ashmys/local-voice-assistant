"use strict";
/* chat.js - windowed conversation list + chat history.

   The panel does NOT render the whole conversation: it keeps the full message
   array in memory and only mounts a window of DOM nodes at the tail, loading
   earlier chunks while you scroll up (IntersectionObserver on a sentinel).
   Live/streaming messages update in place via upsert(). */

const CHUNK = 60; // messages per rendered window

class ChatList {
  constructor(container) {
    this.el = container;
    this.items = [];          // full message array: {key, who, text, final, ts, system}
    this.start = 0;           // index of the first RENDERED item
    this.nodeByKey = new Map();
    this.itemByKey = new Map();
    this.pinned = true;       // auto-scroll while near the bottom
    this._loading = false;

    this.sentinel = document.createElement("button");
    this.sentinel.type = "button";
    this.sentinel.className = "loadearlier";
    this.sentinel.textContent = "Load earlier messages";
    this.sentinel.style.display = "none";
    this.sentinel.addEventListener("click", () => this.prependChunk());
    this.el.appendChild(this.sentinel);

    if ("IntersectionObserver" in window) {
      this._io = new IntersectionObserver((entries) => {
        for (const en of entries) {
          if (en.isIntersecting && this.start > 0) this.prependChunk();
        }
      }, { root: this.el, rootMargin: "160px 0px" });
      this._io.observe(this.sentinel);
    }
    this.el.addEventListener("scroll", () => {
      const el = this.el;
      this.pinned = el.scrollTop + el.clientHeight >= el.scrollHeight - 130;
    }, { passive: true });
  }

  reset() {
    this.items = [];
    this.start = 0;
    this.nodeByKey.clear();
    this.itemByKey.clear();
    this.pinned = true;
    for (const n of [...this.el.children]) {
      if (n !== this.sentinel) n.remove();
    }
    this.sentinel.style.display = "none";
  }

  setAll(items) {
    this.reset();
    this.items = items.slice();
    for (const it of this.items) this.itemByKey.set(it.key, it);
    this.start = Math.max(0, this.items.length - CHUNK);
    this._renderAll();
  }

  _renderAll() {
    for (const n of [...this.el.children]) {
      if (n !== this.sentinel) n.remove();
    }
    this.nodeByKey.clear();
    const frag = document.createDocumentFragment();
    for (let i = this.start; i < this.items.length; i++) {
      const n = this._makeNode(this.items[i]);
      this.nodeByKey.set(this.items[i].key, n);
      frag.appendChild(n);
    }
    this.el.appendChild(frag);
    this.sentinel.style.display = this.start > 0 ? "" : "none";
    this.scrollBottom();
  }

  prependChunk() {
    if (this.start <= 0 || this._loading) return;
    this._loading = true;
    try {
      const prevH = this.el.scrollHeight;
      const newStart = Math.max(0, this.start - CHUNK);
      const frag = document.createDocumentFragment();
      for (let i = newStart; i < this.start; i++) {
        const n = this._makeNode(this.items[i]);
        this.nodeByKey.set(this.items[i].key, n);
        frag.appendChild(n);
      }
      this.el.insertBefore(frag, this.sentinel.nextSibling);
      this.start = newStart;
      // keep the viewport anchored on the content the user was reading
      this.el.scrollTop += this.el.scrollHeight - prevH;
      this.sentinel.style.display = this.start > 0 ? "" : "none";
    } finally {
      this._loading = false;
    }
  }

  append(it) {
    if (this.itemByKey.has(it.key)) { this.upsert(it); return; }
    this.items.push(it);
    this.itemByKey.set(it.key, it);
    if (this.start > 0) {
      // The user is somewhere in unloaded history - snap the window to the
      // tail so live messages are always visible and contiguous.
      this.start = Math.max(0, this.items.length - CHUNK);
      this._renderAll();
      return;
    }
    const n = this._makeNode(it);
    this.nodeByKey.set(it.key, n);
    this.el.appendChild(n);
    if (this.pinned) this.scrollBottom();
  }

  upsert(it) {
    const known = this.itemByKey.get(it.key);
    const node = this.nodeByKey.get(it.key);
    if (!known) { this.append(it); return; }
    known.text = it.text;
    known.final = it.final;
    if (node) {
      node.textContent = it.text || "";
      node.classList.toggle("pending", it.final === false);
      if (it.final !== false && this.pinned) this.scrollBottom();
    }
  }

  scrollBottom() {
    this.el.scrollTop = this.el.scrollHeight;
    this.pinned = true;
  }

  _makeNode(it) {
    const div = document.createElement("div");
    if (it.system) {
      div.className = "msg sys";
      const when = it.ts ? new Date(it.ts).toLocaleString() : "";
      div.textContent = when ? "\u2014 " + when + " \u2014" : "\u2014 new session \u2014";
      return div;
    }
    div.className = "msg " + (it.who === "user" ? "you" : "bot");
    div.textContent = it.text || "";
    if (it.final === false) div.classList.add("pending");
    return div;
  }
}

const chatList = new ChatList($("transcript"));

function addBubble(who, text, id, final) {
  if (!text || !text.trim()) return null;
  final = final !== false;
  if (who === "user" && final && id && !id.startsWith("loc")) {
    if (text === lastEcho.text && Date.now() - lastEcho.at < 8000) return null;
  }
  const key = id || ("m" + (++msgCounter));
  chatList.upsert({ key, who, text, final });
  if (final) pulseState(who === "bot" ? "speaking" : "thinking");
  return chatList.nodeByKey.get(key) || null;
}

function resetTranscript() {
  chatList.reset();
  if (viewingFile) setHistMode(null);
}

function setHistMode(file) {
  viewingFile = file || null;
  $("histmode").classList.toggle("show", !!viewingFile);
}

$("histmode").addEventListener("click", () => {
  resetTranscript();
  setHistMode(null);
  toast("Back to the live conversation view");
});

// ---- persistent room: one chat log per browser, resumed across restarts ----
function savedRoom() {
  try { return localStorage.getItem("chatRoom") || ""; } catch (e) { return ""; }
}
function storeRoom(r) {
  try {
    if (r) localStorage.setItem("chatRoom", r);
    else localStorage.removeItem("chatRoom");
  } catch (e) {}
}

async function preloadSavedHistory() {
  const r = savedRoom();
  if (!r) return;
  try {
    const d = await fetchJson("/api/chat/session?room=" + encodeURIComponent(r));
    const msgs = d.messages || [];
    if (msgs.length) {
      chatList.setAll(msgs.map((m, i) => ({
        key: "h" + i, who: m.who, text: m.text, final: true,
        ts: m.ts, system: m.who === "system",
      })));
    }
  } catch (e) {
    console.warn("history preload failed", e);
  }
}

// ---- panel buttons ----
$("clearlog").addEventListener("click", () => {
  if (!chatList.items.length && !currentRoom) { toast("Nothing to clear yet"); return; }
  openModal("clearsheet");
});
$("clearno").addEventListener("click", () => closeModal("clearsheet"));
$("clearno2").addEventListener("click", () => closeModal("clearsheet"));
$("clearyes").addEventListener("click", async () => {
  closeModal("clearsheet");
  resetTranscript();
  if (!currentRoom) { toast("Conversation cleared"); return; }
  try {
    const r = await fetch("/api/chat/clear", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ room: currentRoom }),
    });
    if (!r.ok) {
      let why = "HTTP " + r.status;
      try { const j = await r.json(); if (j.error) why = j.error; } catch (e) {}
      throw new Error(why);
    }
    const d = await r.json();
    toast(d.cleared && d.cleared.length ? "Cleared: panel + saved history" : "Panel cleared (no saved history for this session)");
  } catch (e) {
    toast("Panel cleared, but history cleanup failed: " + (e && e.message ? e.message : e));
  }
});

$("newchat").addEventListener("click", () => {
  const fresh = "voice-" + Math.random().toString(16).slice(2, 10);
  storeRoom(fresh);
  currentRoom = "";
  resetTranscript();
  toast("New conversation - starts with your next call (the old one stays in Chat history)");
});

// ---- history modal (sessions render in chunks too) ----
const HIST_CHUNK = 25;
let histSessions = [];

$("histbtn").addEventListener("click", () => {
  openModal("histsheet");
  try { loadHistoryList(); } catch (e) { toast("History error: " + e.message); }
});
$("closehist").addEventListener("click", () => closeModal("histsheet"));

function renderMoreSessions() {
  const body = $("histbody");
  const sentinel = $("histsentinel");
  const from = body.querySelectorAll(".sess").length;
  const next = histSessions.slice(from, from + HIST_CHUNK);
  next.forEach((s) => {
    const div = document.createElement("div");
    div.className = "sess";
    const when = new Date((s.mtime || 0) * 1000).toLocaleString();
    const title = document.createElement("div");
    title.className = "stitle";
    title.textContent = (s.turns || 0) + " messages - " + (s.room || "room");
    const date = document.createElement("div");
    date.className = "sdate";
    date.textContent = when;
    const prev = document.createElement("div");
    prev.className = "sprev";
    prev.textContent = s.first || "(no user text)";
    div.appendChild(title); div.appendChild(date); div.appendChild(prev);
    div.title = "Open this conversation in the panel";
    div.addEventListener("click", () => {
      loadHistoryIntoPanel(s.file).catch((e) => toast("History error: " + e.message));
    });
    body.insertBefore(div, sentinel);
  });
  if (sentinel) {
    sentinel.style.display = from + next.length < histSessions.length ? "" : "none";
  }
}

async function loadHistoryList() {
  const body = $("histbody");
  body.innerHTML = '<div class="empty">loading...</div>';
  try {
    const d = await fetchJson("/api/chat/sessions");
    if (d.error) throw new Error(d.error);
    histSessions = d.sessions || [];
    if (!histSessions.length) {
      body.innerHTML = '<div class="empty">No conversations yet.<br>They appear here after you talk.</div>';
      return;
    }
    body.innerHTML = "";
    const sentinel = document.createElement("button");
    sentinel.type = "button";
    sentinel.id = "histsentinel";
    sentinel.className = "loadearlier";
    sentinel.textContent = "Show older conversations";
    sentinel.style.display = "none";
    sentinel.addEventListener("click", renderMoreSessions);
    body.appendChild(sentinel);
    if ("IntersectionObserver" in window) {
      new IntersectionObserver((entries) => {
        for (const en of entries) {
          if (en.isIntersecting) renderMoreSessions();
        }
      }, { root: body, rootMargin: "200px 0px" }).observe(sentinel);
    }
    renderMoreSessions();
  } catch (e) {
    body.innerHTML = "";
    const err = document.createElement("div");
    err.className = "empty";
    err.textContent = "Failed to load: " + (e && e.message ? e.message : e);
    body.appendChild(err);
  }
}

async function loadHistoryIntoPanel(file) {
  const body = $("histbody");
  body.innerHTML = '<div class="empty">loading...</div>';
  try {
    const d = await fetchJson("/api/chat/session?file=" + encodeURIComponent(file));
    if (d.error) throw new Error(d.error);
    const msgs = d.messages || [];
    resetTranscript();
    chatList.setAll(msgs.map((m, i) => ({
      key: "h" + i, who: m.who, text: m.text, final: true,
      ts: m.ts, system: m.who === "system",
    })));
    setHistMode(file);
    sideOpen = true;
    applySide();
    closeModal("histsheet");
    if (msgs.length) chatList.scrollBottom();
    else toast("This session has no messages");
  } catch (e) {
    body.innerHTML = "";
    const err = document.createElement("div");
    err.className = "empty";
    err.textContent = "Failed: " + (e && e.message ? e.message : e);
    body.appendChild(err);
    throw e;
  }
}
