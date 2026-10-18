"use strict";
/* audio.js - WebAudio analysers, status card rendering and the orb/mic canvas. */

function getCtx(which) {
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return null;
  if (which === "agent") {
    agentCtx = agentCtx || new AC();
    if (agentCtx.state === "suspended") agentCtx.resume().catch(() => {});
    return agentCtx;
  }
  micCtx = micCtx || new AC();
  if (micCtx.state === "suspended") micCtx.resume().catch(() => {});
  return micCtx;
}

function hookAgentAudio(track) {
  try {
    if (!track || !track.mediaStreamTrack) throw new Error("no media stream track");
    const ctx = getCtx("agent");
    if (!ctx) throw new Error("WebAudio unavailable");
    const src = ctx.createMediaStreamSource(new MediaStream([track.mediaStreamTrack]));
    agentAn = ctx.createAnalyser();
    agentAn.fftSize = 128;
    src.connect(agentAn);
    agentData = new Uint8Array(agentAn.frequencyBinCount);
  } catch (e) {
    console.warn("agent analyser failed", e);
  }
}

function hookMicAudio(stream) {
  try {
    const ctx = getCtx("mic");
    if (!ctx) throw new Error("WebAudio unavailable");
    const src = ctx.createMediaStreamSource(stream);
    micAn = ctx.createAnalyser();
    micAn.fftSize = 128;
    src.connect(micAn);
    micData = new Uint8Array(micAn.frequencyBinCount);
  } catch (e) {
    console.warn("mic analyser failed", e);
  }
}

function cleanupAudio() {
  try { micStream && micStream.getTracks().forEach((t) => t.stop()); } catch (e) {}
  micStream = null; micAn = null; agentAn = null; agentData = null; micData = null;
  clearCanvas($("stage")); clearCanvas($("miccanvas"));
}

function clearCanvas(c) {
  try {
    const ctx = c.getContext("2d");
    ctx.clearRect(0, 0, c.width, c.height);
  } catch (e) {}
}

function micLevel() {
  if (!micAn || !micData) return 0;
  let m = 0;
  for (let i = 0; i < micData.length; i++) { if (micData[i] > m) m = micData[i]; }
  return m / 255;
}

function agentLevel() {
  if (!agentAn || !agentData) return 0;
  let sum = 0;
  for (let i = 0; i < agentData.length; i++) sum += agentData[i];
  return sum / (agentData.length * 255);
}

function renderStatus(ml) {
  let cls = "", word = "ready", pulse = false, micTxt = "";
  if (!live) {
    if (srvErr) { cls = "err"; word = "server down"; }
    else if (srvDegraded) { cls = "warn"; word = "degraded"; pulse = true; }
    else { cls = "ok"; word = "ready"; }
  } else {
    let muted = false;
    try {
      const p = room && room.localParticipant
        ? room.localParticipant.getTrackPublication(LK.Track.Source.Microphone) : null;
      muted = !!(p && p.isMuted);
    } catch (e) {}
    micTxt = muted ? "mic muted" : (micPubOk ? (ml > 0.06 ? "mic active" : "mic on") : "mic off");
    const ag = agentLevel();
    if (ag > 0.07) { cls = "speak"; word = "assistant speaking"; pulse = true; }
    else if (ml > 0.09) { cls = "listen"; word = "listening"; pulse = true; }
    else { cls = "ok"; word = "live - listening"; }
  }
  const key = cls + "|" + (pulse ? 1 : 0) + "|" + word + "|" + micTxt;
  if (key === stCache) return;
  stCache = key;
  $("stdot").className = cls + (pulse ? " pulse" : "");
  $("stword").textContent = word;
  $("stmic").textContent = micTxt;
}

const stage = $("stage"), sctx = stage.getContext("2d");
const micc = $("miccanvas"), mctx = micc.getContext("2d");

function fitCanvas() {
  try {
    const r = stage.getBoundingClientRect();
    stage.width = Math.max(1, Math.floor(r.width * devicePixelRatio));
    stage.height = Math.max(1, Math.floor(r.height * devicePixelRatio));
    micc.width = Math.max(1, Math.floor(micc.clientWidth * devicePixelRatio));
    micc.height = Math.max(1, Math.floor(micc.clientHeight * devicePixelRatio));
  } catch (e) {
    console.warn("fitCanvas failed", e);
  }
}

let phase = 0, spin = 0;
function draw() {
  phase += 0.015;
  if (live) spin += 0.015; // the ring only rotates while a call is live
  try {
    if (agentAn && agentData) agentAn.getByteFrequencyData(agentData);
    if (micAn && micData) micAn.getByteFrequencyData(micData);
    const ml = micLevel();
    if (live && micPubOk && !micWarned) {
      if (ml > 0.06) {
        micSilenceSince = 0;
      } else if (!micSilenceSince) {
        micSilenceSince = Date.now();
      } else if (Date.now() - micSilenceSince > 6000) {
        micWarned = true;
        toast("Mic is connected but silent - pick another input device in Settings, or check Windows microphone privacy settings", 5200);
      }
    }
    renderStatus(ml);
    drawOrb();
    drawMic();
  } catch (e) {
    console.warn("draw failed", e);
  }
  rafId = requestAnimationFrame(draw);
}

function drawOrb() {
  const w = stage.width, h = stage.height;
  sctx.clearRect(0, 0, w, h);
  const cx = w / 2;
  const cy = h / 2;
  const base = Math.min(w, h) * 0.13;
  const level = agentLevel();
  const idle = live ? 0.12 : 0.05;
  const breath = 1 + Math.sin(phase * 1.6) * (live ? 0.05 : 0.03);
  const r = base * (0.9 + level * 1.6) * breath;

  const glow = sctx.createRadialGradient(cx, cy, r * 0.1, cx, cy, r * 2.6);
  glow.addColorStop(0, "rgba(110,168,255," + (0.16 + level * 0.5) + ")");
  glow.addColorStop(1, "rgba(110,168,255,0)");
  sctx.fillStyle = glow;
  sctx.fillRect(cx - r * 3, cy - r * 3, r * 6, r * 6);

  const bars = 72;
  for (let i = 0; i < bars; i++) {
    const ang = (i / bars) * Math.PI * 2 + spin * 0.4;
    const v = agentData
      ? agentData[Math.floor(i / bars * agentData.length * 0.7)] / 255
      : 0;
    const amp = idle + v * 1.1;
    const len = r * (0.35 + amp * 0.85);
    const x1 = cx + Math.cos(ang) * r * 0.72;
    const y1 = cy + Math.sin(ang) * r * 0.72;
    const x2 = cx + Math.cos(ang) * (r * 0.72 + len);
    const y2 = cy + Math.sin(ang) * (r * 0.72 + len);
    const hue = 215 + Math.sin(i * 0.35 + spin) * 28;
    sctx.strokeStyle = "hsla(" + hue + ", 90%, " + (62 + v * 22) + "%, " + (0.35 + v * 0.65) + ")";
    sctx.lineWidth = 2.4 * devicePixelRatio;
    sctx.lineCap = "round";
    sctx.beginPath();
    sctx.moveTo(x1, y1);
    sctx.lineTo(x2, y2);
    sctx.stroke();
  }
  sctx.beginPath();
  sctx.arc(cx, cy, r * 0.66, 0, Math.PI * 2);
  sctx.fillStyle = "rgba(16,22,40,0.9)";
  sctx.fill();
  sctx.strokeStyle = "rgba(140,180,255," + (0.3 + level * 0.6) + ")";
  sctx.lineWidth = 1.6 * devicePixelRatio;
  sctx.stroke();
}

function drawMic() {
  const w = micc.width, h = micc.height;
  mctx.clearRect(0, 0, w, h);
  const bars = 48, gap = 3 * devicePixelRatio;
  const bw = (w - gap * (bars - 1)) / bars;
  for (let i = 0; i < bars; i++) {
    const v = micData
      ? micData[Math.floor(i / bars * micData.length * 0.6)] / 255
      : 0;
    const bh = Math.max(2 * devicePixelRatio, v * h * 0.92);
    const x = i * (bw + gap);
    const y = (h - bh) / 2;
    mctx.fillStyle = "rgba(110,168,255," + (0.25 + v * 0.7) + ")";
    const rr = Math.min(bw / 2, 3 * devicePixelRatio);
    mctx.beginPath();
    if (mctx.roundRect) mctx.roundRect(x, y, bw, bh, rr);
    else mctx.rect(x, y, bw, bh);
    mctx.fill();
  }
}
