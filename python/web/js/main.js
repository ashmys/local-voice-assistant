"use strict";
/* main.js - boot: health polling, panel/modal wiring, UI heartbeat.

   The heartbeat is what lets the supervisor close the terminal when the app
   window is closed: beats every 2.5 s while the page is open; when they stop,
   server.py shuts LiveKit + the agent down. The pagehide beacon makes a real
   window close exit faster (a refresh re-beats immediately and cancels it). */

async function checkHealth() {
  try {
    const r = await fetch("/health");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const h = await r.json();
    const lk = !!(h.livekit && h.livekit.status === "up");
    const tg = !!(h.textgen && String(h.textgen.status || "").startsWith("up"));
    const gpu = String(h.gpu || "").startsWith("YES");
    const sttN = h.models ? Number(h.models.stt) || 0 : 0;
    const ttsN = h.models ? Number(h.models.tts) || 0 : 0;
    const lib = !!(h.lib_file && h.lib_file.exists);
    const ag = !!(h.children && h.children.agent === "up");
    const el = $("stchips");
    el.innerHTML = chip("livekit", lk, lk ? "up" : "down") + " &middot; " +
      chip("llm", tg, tg ? "up" : "down") + " &middot; " +
      chip("gpu", gpu, gpu ? "yes" : "no") + " &middot; " +
      chip("stt", sttN > 0, sttN) + " &middot; " +
      chip("tts", ttsN > 0, ttsN) + " &middot; " +
      chip("agent", ag, ag ? "up" : "down") +
      (lib ? "" : " &middot; lib missing");
    srvErr = !lk || !ag;
    srvDegraded = !srvErr && !tg;
  } catch (e) {
    $("stchips").innerHTML = "server unreachable";
    srvErr = true;
    srvDegraded = false;
  }
}

async function precheck() {
  const fatal = [], warn = [];
  if (!window.LivekitClient) fatal.push("livekit-client.js failed to load - Ctrl+F5 or re-extract the kit");
  let h = null;
  try {
    const r = await fetch("/health");
    if (!r.ok) throw new Error("HTTP " + r.status);
    h = await r.json();
  } catch (e) {
    fatal.push("backend unreachable (" + e.message + ")");
  }
  if (h) {
    if (h.venv === false) fatal.push(".venv missing - run setup.bat (Windows) or setup.sh");
    if (h.lib_file && !h.lib_file.exists) fatal.push("livekit-client.js missing on server - re-extract the kit");
    if (h.livekit && h.livekit.status !== "up") fatal.push("LiveKit server is down - restart start.bat / start.sh");
    if (h.children && h.children.agent && h.children.agent !== "up") warn.push("agent worker: " + h.children.agent);
    if (h.textgen && !String(h.textgen.status || "").startsWith("up")) warn.push("LLM API is down - the bot cannot reply until it starts");
    if (h.models) {
      if (!Number(h.models.stt)) warn.push("no Whisper model downloaded yet - run setup.bat or check settings");
      if (!Number(h.models.tts)) warn.push("no Piper voice downloaded yet - run setup.bat or check settings");
    }
  }
  if (!fatal.length && navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    try {
      const s = await navigator.mediaDevices.getUserMedia({ audio: true });
      s.getTracks().forEach((t) => t.stop());
    } catch (e) {
      fatal.push(micErrorText(e));
    }
  }
  return { fatal, warn };
}

// ---- side panel ----
const sideEl = $("side");
sideOpen = (() => {
  try {
    const saved = localStorage.getItem("panelOpen");
    if (saved === "1" || saved === "0") return saved === "1";
  } catch (e) {}
  return innerWidth >= 1100;
})();

function applySide() {
  sideEl.classList.toggle("hidden", !sideOpen);
  document.body.classList.toggle("panel-open", sideOpen);
  const tab = $("sidetab");
  tab.classList.toggle("open", sideOpen);
  tab.title = sideOpen ? "Hide conversation" : "Show conversation";
  tab.setAttribute("aria-expanded", String(sideOpen));
  try { localStorage.setItem("panelOpen", sideOpen ? "1" : "0"); } catch (e) {}
}

$("sidetab").addEventListener("click", () => {
  sideOpen = !sideOpen;
  applySide();
  if (sideOpen) chatList.scrollBottom();
});

// ---- modals / tabs ----
$("gearbtn").addEventListener("click", async () => {
  openModal("settingsheet");
  fillMicDevices();
  try {
    await loadConfigForm();
  } catch (e) {
    toast("Could not load settings: " + (e && e.message ? e.message : e));
  }
});
$("closegear").addEventListener("click", () => closeModal("settingsheet"));
document.querySelectorAll(".modal").forEach((m) => {
  m.addEventListener("click", (ev) => { if (ev.target === m) m.classList.remove("open"); });
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") document.querySelectorAll(".modal.open").forEach((m) => m.classList.remove("open"));
});

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tabpage").forEach((p) => p.classList.toggle("active", p.id === btn.dataset.tab));
  });
});

// ---- layout ----
addEventListener("resize", () => { try { fitCanvas(); applySide(); } catch (e) {} });
addEventListener("orientationchange", () => setTimeout(() => { fitCanvas(); applySide(); }, 250));

// ---- UI heartbeat (window-close detection on the server) ----
function beat() {
  try {
    fetch("/api/heartbeat", { method: "POST", body: "1", keepalive: true }).catch(() => {});
  } catch (e) {}
}
addEventListener("pagehide", () => {
  try {
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/api/window/closing", new Blob(["1"], { type: "text/plain" }));
    }
  } catch (e) {}
});

// ---- boot ----
fitCanvas();
applySide();
draw();
beat();
setInterval(beat, 2500);
checkHealth();
setInterval(checkHealth, 15000);
preloadSavedHistory();
