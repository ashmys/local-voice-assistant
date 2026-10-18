"use strict";
/* settings.js - config form, provider profiles, model picker, mic selection,
   auto-save and the restart-everything button.

   Auto-save: EVERY config field saves by itself shortly after you change it
   (debounced) - leaving the panel can no longer lose in-progress changes.
   Provider profiles (localStorage, per browser): a merged dropdown + free
   text field; picking a name loads its URL/key/model, editing those fields
   updates the active profile automatically, and profiles can be added or
   deleted from the panel. */

function addOpt(sel, value, label) {
  const o = document.createElement("option");
  o.value = value; o.textContent = label;
  sel.appendChild(o);
}

function ensureOpt(sel, val) {
  if (!sel) return;
  const has = Array.from(sel.options).some((o) => o.value === val);
  if (!has && val) addOpt(sel, val, val);
}

function cfgEls() {
  const out = {};
  for (const el of document.querySelectorAll("[id^=cfg-]")) out[el.id.slice(4)] = el;
  return out;
}

function updateRange(key) {
  const el = $("cfg-" + key), lab = $("val-" + key);
  if (el && lab && el.type === "range") lab.textContent = el.value;
}
document.addEventListener("input", (ev) => {
  if (ev.target.id && ev.target.id.startsWith("cfg-")) updateRange(ev.target.id.slice(4));
});

// ---- provider profiles (localStorage) --------------------------------------
const PROF_KEY = "providerProfiles";
const PROF_FIELDS = ["TEXTGEN_BASE_URL", "TEXTGEN_API_KEY", "TEXTGEN_MODEL"];
let activeProfile = "";

function loadProfiles() {
  try { return JSON.parse(localStorage.getItem(PROF_KEY) || "{}") || {}; }
  catch (e) { return {}; }
}
function storeProfiles(p) {
  try { localStorage.setItem(PROF_KEY, JSON.stringify(p)); } catch (e) {}
}
function renderProfileList() {
  const dl = $("profilelist");
  if (!dl) return;
  const profiles = loadProfiles();
  dl.innerHTML = "";
  Object.keys(profiles).sort().forEach((name) => {
    const o = document.createElement("option");
    o.value = name;
    dl.appendChild(o);
  });
}
function loadProfile(name) {
  const profiles = loadProfiles();
  const p = profiles[name];
  if (!p) return false;
  PROF_FIELDS.forEach((k) => {
    const el = $("cfg-" + k);
    if (el && typeof p[k] === "string") el.value = p[k];
  });
  activeProfile = name;
  try { localStorage.setItem("lastProfile", name); } catch (e) {}
  scheduleAutoSave();
  refreshLlmModels();
  toast("Profile '" + name + "' loaded");
  return true;
}
function saveProfile(name, silent) {
  name = (name || "").trim();
  if (!name) { if (!silent) toast("Type a name for the profile first"); return; }
  const profiles = loadProfiles();
  profiles[name] = {};
  PROF_FIELDS.forEach((k) => {
    const el = $("cfg-" + k);
    profiles[name][k] = el ? el.value : "";
  });
  storeProfiles(profiles);
  activeProfile = name;
  try { localStorage.setItem("lastProfile", name); } catch (e) {}
  renderProfileList();
  if (!silent) toast("Profile '" + name + "' saved");
}
function deleteProfile(name) {
  name = (name || activeProfile || "").trim();
  if (!name) { toast("No active profile to delete"); return; }
  const profiles = loadProfiles();
  if (!profiles[name]) { toast("No profile named '" + name + "'"); return; }
  delete profiles[name];
  storeProfiles(profiles);
  renderProfileList();
  if (activeProfile === name) activeProfile = "";
  toast("Profile '" + name + "' deleted");
}

$("profilename").addEventListener("input", () => {
  const name = $("profilename").value.trim();
  if (name && loadProfiles()[name]) loadProfile(name); // exact match -> apply
});
$("profsave").addEventListener("click", () => saveProfile($("profilename").value));
$("profdelete").addEventListener("click", () => deleteProfile($("profilename").value));

// ---- auto-save (in-progress changes are never lost) ------------------------
let autoSaveTimer = 0;
let autoSaving = false;

function scheduleAutoSave() {
  clearTimeout(autoSaveTimer);
  autoSaveTimer = setTimeout(() => { saveSettings(true); }, 800);
}

function markActiveProfileFromValues() {
  // when the three provider fields match a stored profile exactly, that
  // profile is considered active (edits keep syncing into it)
  const vals = PROF_FIELDS.map((k) => { const el = $("cfg-" + k); return el ? el.value : ""; });
  const profiles = loadProfiles();
  let match = "";
  for (const [name, p] of Object.entries(profiles)) {
    if (PROF_FIELDS.every((k, i) => (p[k] || "") === vals[i])) { match = name; break; }
  }
  activeProfile = match;
  return match;
}

async function saveSettings(silent) {
  const msg = $("savemsg");
  const config = {};
  for (const [k, el] of Object.entries(cfgEls())) config[k] = el.value;
  if (autoSaving) return;
  autoSaving = true;
  try {
    const r = await fetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config }),
    });
    if (!r.ok) {
      let why = "HTTP " + r.status;
      try { const j = await r.json(); if (j.error) why = j.error; } catch (e) {}
      throw new Error(why);
    }
    if (!silent) {
      msg.style.color = "var(--ok)";
      msg.textContent = "Saved - applies to your NEXT conversation.";
      toast("Settings saved");
    } else {
      const prof = markActiveProfileFromValues();
      if (prof) saveProfile(prof, true); // keep the active profile in sync, quietly
      msg.style.color = "var(--ok)";
      msg.textContent = "Auto-saved " + new Date().toLocaleTimeString() +
        " - applies to your NEXT conversation.";
    }
    checkHealth();
  } catch (e) {
    if (!silent) {
      msg.style.color = "var(--warn)";
      msg.textContent = "Save failed: " + (e && e.message ? e.message : e);
    } else {
      console.warn("auto-save failed", e);
    }
  } finally {
    autoSaving = false;
  }
}

// every cfg- field auto-saves shortly after a change
document.addEventListener("change", (ev) => {
  const id = ev.target && ev.target.id;
  if (!id) return;
  if (id.startsWith("cfg-")) scheduleAutoSave();
});
document.addEventListener("input", (ev) => {
  const id = ev.target && ev.target.id;
  if (!id) return;
  if (id.startsWith("cfg-") &&
      (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA")) {
    scheduleAutoSave();
  }
});

// ---- microphone device -----------------------------------------------------
async function fillMicDevices() {
  const sel = $("micdevice");
  if (!sel || !navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
  try {
    const devs = await navigator.mediaDevices.enumerateDevices();
    const cur = (() => { try { return localStorage.getItem("micDeviceId") || ""; } catch (e) { return ""; } })();
    sel.innerHTML = '<option value="">System default</option>';
    let found = false;
    devs.forEach((d) => {
      if (d.kind !== "audioinput") return;
      const label = d.label || ("microphone " + sel.options.length);
      addOpt(sel, d.deviceId, label);
      if (d.deviceId && d.deviceId === cur) found = true;
    });
    if (found) sel.value = cur;
  } catch (e) {
    console.warn("enumerateDevices failed", e);
  }
}

$("micdevice").addEventListener("change", () => {
  try { localStorage.setItem("micDeviceId", $("micdevice").value || ""); } catch (e) {}
  toast("Microphone saved - applies to the next call");
});

async function fillBrowserPicker(cfg) {
  const sel = $("cfg-UI_BROWSER");
  if (!sel) return;
  let browsers = [];
  try {
    const d = await fetchJson("/api/browsers");
    browsers = d.browsers || [];
  } catch (e) {
    console.warn("browser list failed", e);
  }
  // keep the static first option (System default) and the saved value
  const keep = sel.value;
  sel.innerHTML = '<option value="">System default (auto)</option>';
  browsers.forEach((b) => {
    if (!b.id || b.id === "custom") return;
    const label = b.name + (b.default ? " \u2014 default" : "") +
      (b.chromium ? "" : " (normal window)");
    if (!Array.from(sel.options).some((o) => o.value === b.id)) {
      addOpt(sel, b.id, label);
    }
  });
  const saved = cfg.UI_BROWSER || "";
  if (saved) {
    ensureOpt(sel, saved);
    sel.value = saved;
  } else if (keep) {
    sel.value = keep;
  }
}

async function loadConfigForm() {
  const [cfgR, sttR, ttsR] = await Promise.all([
    fetchJson("/api/config"),
    fetchJson("/api/models/stt"),
    fetchJson("/api/models/tts"),
  ]);
  const cfg = cfgR.config || {};
  fillBrowserPicker(cfg); // async, fills the dropdown when the list arrives
  for (const [k, el] of Object.entries(cfgEls())) {
    if (cfg[k] !== undefined && cfg[k] !== "") {
      if (el.tagName === "SELECT") ensureOpt(el, cfg[k]);
      el.value = cfg[k];
    } else if (el.tagName === "SELECT" && !el.value) {
      // leave the first option (a sane default) selected
    }
    if (el.type === "range") updateRange(k);
  }
  const sel = $("cfg-WHISPER_MODEL");
  sel.innerHTML = "";
  (sttR.local_folders || []).forEach((m) => addOpt(sel, m.name, m.name + " (folder, " + m.size_mb + " MB)"));
  (sttR.presets || []).forEach((m) => addOpt(sel, m.name, m.name + " (" + m.size_mb + " MB)" + (m.local ? " \u2713" : "")));
  if (cfg.WHISPER_MODEL) {
    ensureOpt(sel, cfg.WHISPER_MODEL);
    sel.value = cfg.WHISPER_MODEL;
  }
  const vsel = $("cfg-TTS_VOICE");
  vsel.innerHTML = "";
  (ttsR.voices || []).forEach((v) => addOpt(vsel, v.name, v.name + (v.local ? " \u2713" : " (download needed)")));
  if (cfg.TTS_VOICE) {
    ensureOpt(vsel, cfg.TTS_VOICE);
    vsel.value = cfg.TTS_VOICE;
  }
  renderProfileList();
  const last = (() => { try { return localStorage.getItem("lastProfile") || ""; } catch (e) { return ""; } })();
  const profName = $("profilename");
  if (last && loadProfiles()[last]) {
    profName.value = last;
    loadProfile(last);
  }
  markActiveProfileFromValues();
  refreshLlmModels(); // auto-select the last used model (or a default) if available
}

let modelFetchTimer = 0;
let modelFetchStartedAt = 0;   // user edits to the model field after this
                               // moment must NOT be clobbered by the picker
let lastAutoPick = "";         // the value WE last auto-filled - the only one
                               // the picker may replace on the next refresh

async function refreshLlmModels() {
  const sel = $("llmmodels"), hint = $("modelhint");
  if (!sel) return;
  const url = ($("cfg-TEXTGEN_BASE_URL").value || "").trim();
  const key = ($("cfg-TEXTGEN_API_KEY").value || "").trim();
  modelFetchStartedAt = Date.now();
  sel.innerHTML = '<option value="">loading...</option>';
  hint.textContent = "Fetching model list from " + (url || "(no base URL)") + " ...";
  try {
    const r = await fetch("/api/llm/models", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, key }),
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    const list = d.models || [];
    sel.innerHTML = "";
    if (!list.length) {
      addOpt(sel, "", "(no models returned)");
      hint.textContent = "The provider returned no models - type the model id manually above.";
      return;
    }
    list.forEach((id) => addOpt(sel, id, id));
    const cur = ($("cfg-TEXTGEN_MODEL").value || "").trim();
    const userEdited = modelEditAt > modelFetchStartedAt;
    let pick = "";
    if (cur && list.indexOf(cur) >= 0) {
      pick = cur; // last used / saved model is still available
    } else if (cur && userEdited) {
      // the user typed their own model while the list was loading - never
      // overwrite what they just entered
      hint.textContent = "'" + cur + "' is not in the provider's list (" +
        list.length + " models) - keeping your entry.";
      return;
    } else if (cur && cur !== lastAutoPick) {
      // a real saved/typed id that is not on the list: keep it, just inform
      hint.textContent = "'" + cur + "' is not in the provider's list (" +
        list.length + " models) - keeping it. Pick below to change.";
      return;
    } else {
      pick = list.find((m) => m.indexOf(":free") >= 0)
        || (list.indexOf("openrouter/auto") >= 0 ? "openrouter/auto" : "")
        || list[0];
    }
    lastAutoPick = pick;
    sel.value = pick;
    if (pick !== cur) {
      $("cfg-TEXTGEN_MODEL").value = pick;
      scheduleAutoSave();
      hint.textContent = "Auto-selected '" + pick + "' (" + list.length + " models found). Pick another or type your own.";
    } else {
      hint.textContent = "Using your last model '" + cur + "' (" + list.length + " available).";
    }
  } catch (e) {
    sel.innerHTML = '<option value="">(unavailable)</option>';
    hint.textContent = "Could not list models (" + (e && e.message ? e.message : e) + ") - type the model id manually above.";
  }
}

$("refreshmodels").addEventListener("click", refreshLlmModels);
$("llmmodels").addEventListener("change", () => {
  const v = $("llmmodels").value;
  if (v) {
    $("cfg-TEXTGEN_MODEL").value = v;
    scheduleAutoSave();
    $("modelhint").textContent = "Picked '" + v + "' - saved automatically.";
  }
});
let modelEditAt = 0;
["cfg-TEXTGEN_BASE_URL", "cfg-TEXTGEN_API_KEY"].forEach((id) => {
  $(id).addEventListener("change", () => {
    clearTimeout(modelFetchTimer);
    modelFetchTimer = setTimeout(refreshLlmModels, 500);
  });
});
$("cfg-TEXTGEN_MODEL").addEventListener("input", () => { modelEditAt = Date.now(); });

$("savebtn").addEventListener("click", () => saveSettings(false));

// ---- restart everything (with confirmation) ---------------------------------
$("restartbtn").addEventListener("click", () => openModal("restartsheet"));
$("restartno").addEventListener("click", () => closeModal("restartsheet"));
$("restartno2").addEventListener("click", () => closeModal("restartsheet"));
$("restartyes").addEventListener("click", async () => {
  closeModal("restartsheet");
  const btn = $("restartbtn");
  btn.disabled = true;
  toast("Restarting LiveKit + agent...");
  try {
    const r = await fetch("/api/restart", { method: "POST", body: "{}" });
    if (!r.ok) {
      let why = "HTTP " + r.status;
      try { const j = await r.json(); if (j.error) why = j.error; } catch (e) {}
      throw new Error(why);
    }
    // wait for the agent child to come back (models reload; give it a while)
    const deadline = Date.now() + 90000;
    let back = false;
    while (Date.now() < deadline) {
      await new Promise((res) => setTimeout(res, 2000));
      try {
        const h = await (await fetch("/health")).json();
        if (h.children && h.children.agent === "up") { back = true; break; }
      } catch (e) { /* server may be mid-restart */ }
    }
    toast(back ? "Restart complete - everything is back up"
               : "Restart sent, but the agent has not come back yet - watch the console");
    checkHealth();
  } catch (e) {
    toast("Restart failed: " + (e && e.message ? e.message : e));
  } finally {
    btn.disabled = false;
  }
});
