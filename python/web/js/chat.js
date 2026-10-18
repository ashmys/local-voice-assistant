"use strict";
/* chat.js - windowed conversation list + chat history.

   The panel does NOT render the whole conversation: it keeps the full message
   array in memory and only mounts a window of DOM nodes at the tail, loading
   earlier chunks while you scroll up (IntersectionObserver on a sentinel).
   Live/streaming messages update in place via upsert().

   Bot bubbles can carry a collapsible "thinking" block (thinking mode):
   item = {key, who, text, final, ts, system, think, thinkFinal}. */

const CHUNK = 60; // messages per rendered window

class ChatList {
  constructor(container) {
    this.el = container;
    this.items = [];          // full message array
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
    if (it.text !== undefined) known.text = it.text;
    if (it.final !== undefined) known.final = it.final;
    if (it.think !== undefined) known.think = it.think;
    if (it.thinkFinal !== undefined) known.thinkFinal = it.thinkFinal;
    if (node) this._updateNode(node, known);
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
      if (it.text && it.text !== "session-start") {
        // a note in the log (e.g. an LLM error) - show it, not just the date
        div.textContent = when ? when + " \u2014 " + it.text : it.text;
      } else {
        div.textContent = when ? "\u2014 " + when + " \u2014" : "\u2014 new session \u2014";
      }
      return div;
    }
    div.className = "msg " + (it.who === "user" ? "you" : "bot");
    if (it.who === "bot" && it.think) {
      const det = document.createElement("details");
      det.className = "think";
      const sum = document.createElement("summary");
      sum.textContent = "thinking\u2026";
      const body = document.createElement("div");
      body.className = "thinkbody";
      body.textContent = it.think;
      det.appendChild(sum); det.appendChild(body);
      div.appendChild(det);
      // collapse once the real answer is streaming/complete
      det.open = !(it.text && it.final !== false) && it.thinkFinal !== true;
    }
    const txt = document.createElement("div");
    txt.className = "txt";
    txt.textContent = it.text || "";
    div.appendChild(txt);
    if (it.final === false) div.classList.add("pending");
    return div;
  }

  _updateNode(node, it) {
    if (it.system) return;
    const needsThink = it.who === "bot" && !!it.think;
    const det = node.querySelector(":scope > .think");
    if (needsThink && !det) {
      // structure changed (thinking appeared) - rebuild the bubble
      const fresh = this._makeNode(it);
      node.replaceWith(fresh);
      this.nodeByKey.set(it.key, fresh);
      if (it.final !== false && this.pinned) this.scrollBottom();
      return;
    }
    if (det) {
      const open = !(it.text && it.final !== false) && it.thinkFinal !== true;
      det.open = open;
      det.querySelector(".thinkbody").textContent = it.think || "";
      det.querySelector("summary").textContent = open ? "thinking\u2026" : "thought process";
    }
    const txt = node.querySelector(":scope > .txt");
    if (txt) txt.textContent = it.text || "";
    node.classList.toggle("pending", it.final === false);
    if (it.final !== false && this.pinned) this.scrollBottom();
  }
}

const chatList = new ChatList($("transcript"));

/* Live messages are ALWAYS kept here, even while the user is browsing an old
   session in history view - the history X button restores exactly this. */
let liveItems = [];

function trackLive(key, who, text, final, think, thinkFinal) {
  const found = liveItems.find((it) => it.key === key);
  if (found) {
    if (text !== undefined) found.text = text;
    if (final !== undefined) found.final = final;
    if (think !== undefined) found.think = think;
    if (thinkFinal !== undefined) found.thinkFinal = thinkFinal;
    return found;
  }
  const it = { key, who, text: text || "", final: final !== false,
               think: think || "", thinkFinal: thinkFinal === true };
  liveItems.push(it);
  return it;
}

function addBubble(who, text, id, final, think, thinkFinal) {
  const hasThink = typeof think === "string";
  if (!text && !hasThink) return null;
  if (!text || !text.trim()) {
    if (!hasThink) return null;
    text = "";
  }
  final = final !== false;
  if (who === "user" && final && id && !id.startsWith("loc")) {
    if (text === lastEcho.text && Date.now() - lastEcho.at < 8000) return null;
  }
  const key = id || ("m" + (++msgCounter));
  const it = trackLive(key, who, text, final, think, thinkFinal);
  chatList.upsert({ key, who, text: it.text, final: it.final,
                    think: it.think, thinkFinal: it.thinkFinal });
  if (final && it.text) pulseState(who === "bot" ? "speaking" : "thinking");
  return chatList.nodeByKey.get(key) || null;
}

function resetTranscript() {
  chatList.reset();
  liveItems = [];
  if (viewingFile) setHistMode(null);
}

function setHistMode(file) {
  viewingFile = file || null;
  $("histmode").classList.toggle("show", !!viewingFile);
}

/* Leaving history view goes back to the LIVE conversation: rebuild from the
   in-memory live items (everything received this sitting) and, if the current
   room has a saved log, merge what is already on disk behind them. */
$("histmode").addEventListener("click", async () => {
  setHistMode(null);
  chatList.reset();
  const r = currentRoom || savedRoom();
  // start with what we have in memory right now
  const memory = liveItems.slice();
  let saved = [];
  try {
    if (r) saved = await fetchSavedHistory(r);
  } catch (e) { /* disk may be empty - memory still shows */ }
  // merge: saved log first (dedup by key against live ids u*/b*), then memory
  const seen = new Set(memory.map((it) => it.key));
  const merged = historyItems(saved).filter((it) => !seen.has(it.key)).concat(memory);
  chatList.setAll(merged);
  // re-apply live streaming state on top of the saved snapshot
  chatList.scrollBottom();
  toast(memory.length ? "Back to the current conversation"
                      : "Back to the live view (this conversation is empty)");
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

// Turn saved .jsonl records into panel items. Keys use the agent's message id
// (u3 / b12) for the LATEST occurrence so live messages merge seamlessly with
// the reloaded log instead of appearing twice; older repeats get unique keys.
function historyItems(msgs) {
  const taken = new Set();
  const out = new Array(msgs.length);
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i] || {};
    let key = m.id || "";
    if (!key || taken.has(key)) key = "h:" + i;
    taken.add(key);
    out[i] = { key, who: m.who, text: m.text, final: true,
               ts: m.ts, system: m.who === "system",
               think: m.think || "", thinkFinal: true };
  }
  return out;
}

async function fetchSavedHistory(room) {
  const d = await fetchJson("/api/chat/session?room=" + encodeURIComponent(room));
  if (d.error) throw new Error(d.error);
  return d.messages || [];
}

// ---- refresh button = MANUAL CHECK OF EVERYTHING ---------------------------
// (auto checks keep running every 15 s; this button runs the same checks
// right now on demand, plus a chat-log write probe and a log reload)
async function manualCheckAll() {
  const btn = $("refreshhist");
  btn.disabled = true;
  try {
    let c = null;
    try { c = await fetchJson("/api/check"); } catch (e) { /* fall through */ }
    checkHealth();
    let parts = [];
    if (c) {
      const lk = c.livekit && c.livekit.status === "up";
      const tg = c.textgen && String(c.textgen.status || "").startsWith("up");
      const ag = c.children && c.children.agent === "up";
      const cl = c.chat_log && c.chat_log.writable;
      parts = [
        "livekit " + (lk ? "up" : "DOWN"),
        "llm " + (tg ? "up" : "DOWN"),
        "agent " + (ag ? "up" : "down"),
        "chat log " + (cl ? "writable" : "WRITE BLOCKED"),
      ];
      if (c.textgen && !tg) parts.push("(" + (c.textgen.status || "?") + ")");
      if (c.chat_log && !cl) parts.push("(" + (c.chat_log.error || "?") + ")");
    }
    // reload the visible conversation from disk
    try {
      if (viewingFile) {
        const d = await fetchJson("/api/chat/session?file=" + encodeURIComponent(viewingFile));
        if (d.error) throw new Error(d.error);
        chatList.setAll(historyItems(d.messages || []));
        chatList.scrollBottom();
      } else {
        const r = currentRoom || savedRoom();
        if (r) {
          const msgs = await fetchSavedHistory(r);
          const pending = liveItems.filter((it) => it.final === false);
          setHistMode(null);
          chatList.setAll(mergedLiveSaved(msgs));
          pending.forEach((it) => chatList.append(it));
          chatList.scrollBottom();
        }
      }
    } catch (e) { parts.push("log reload failed: " + (e && e.message ? e.message : e)); }
    toast(parts.length ? "Checked: " + parts.join(" \u00b7 ") : "Checked (server unreachable)", 4200);
  } finally {
    btn.disabled = false;
  }
}

function mergedLiveSaved(msgs) {
  // saved log behind, live in-memory items on top (dedup by key)
  const seen = new Set(liveItems.map((it) => it.key));
  return historyItems(msgs).filter((it) => !seen.has(it.key)).concat(liveItems.slice());
}

$("refreshhist").addEventListener("click", () => { manualCheckAll(); });

async function preloadSavedHistory() {
  const r = savedRoom();
  if (!r) return;
  try {
    const msgs = await fetchSavedHistory(r);
    if (msgs.length) {
      chatList.setAll(mergedLiveSaved(msgs));
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
  // 1) wipe the MODEL context (what the LLM remembers), not just the UI
  try {
    if (room && live) {
      await room.localParticipant.publishData(
        new TextEncoder().encode(JSON.stringify({ cmd: "clear-context" })),
        { reliable: true, topic: "ctx-clear" });
    }
  } catch (e) { console.warn("ctx-clear failed", e); }
  // 2) wipe the UI panel
  resetTranscript();
  // 3) erase this room's saved chat log on disk
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
    toast(d.cleared && d.cleared.length ? "Cleared: panel + saved history + model context"
                                        : "Panel cleared (no saved history for this session)");
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
    chatList.reset();
    chatList.setAll(historyItems(msgs));
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
