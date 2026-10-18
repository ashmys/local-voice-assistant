"use strict";
/* settings.js - config form, model picker and microphone device selection. */

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
  refreshLlmModels(); // auto-select the last used model (or a default) if available
}

let modelFetchTimer = 0;

async function refreshLlmModels() {
  const sel = $("llmmodels"), hint = $("modelhint");
  if (!sel) return;
  const url = ($("cfg-TEXTGEN_BASE_URL").value || "").trim();
  const key = ($("cfg-TEXTGEN_API_KEY").value || "").trim();
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
    let pick = "";
    if (cur && list.indexOf(cur) >= 0) {
      pick = cur; // last used / saved model is still available
    } else {
      pick = list.find((m) => m.indexOf(":free") >= 0)
        || (list.indexOf("openrouter/auto") >= 0 ? "openrouter/auto" : "")
        || list[0];
    }
    sel.value = pick;
    if (pick !== cur) {
      $("cfg-TEXTGEN_MODEL").value = pick;
      hint.textContent = "Auto-selected '" + pick + "' (" + list.length + " models found). Pick another or type your own, then Save changes.";
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
    $("modelhint").textContent = "Picked '" + v + "' - click Save changes to apply it.";
  }
});
["cfg-TEXTGEN_BASE_URL", "cfg-TEXTGEN_API_KEY"].forEach((id) => {
  $(id).addEventListener("change", () => {
    clearTimeout(modelFetchTimer);
    modelFetchTimer = setTimeout(refreshLlmModels, 500);
  });
});

$("savebtn").addEventListener("click", async () => {
  const msg = $("savemsg");
  msg.style.color = "var(--ok)";
  msg.textContent = "saving...";
  const config = {};
  for (const [k, el] of Object.entries(cfgEls())) config[k] = el.value;
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
    msg.textContent = "Saved - applies to your NEXT conversation.";
    toast("Settings saved");
    checkHealth();
  } catch (e) {
    msg.style.color = "var(--warn)";
    msg.textContent = "Save failed: " + (e && e.message ? e.message : e);
  }
});
