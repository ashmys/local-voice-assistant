"use strict";
/* call.js - LiveKit connection, call button and text chat input. */

async function connect() {
  if (!window.LivekitClient) throw new Error("livekit-client.js missing");
  // Reuse this browser's room so the conversation (and its saved chat log)
  // continues across reloads and app restarts instead of starting a new file.
  const wantRoom = savedRoom();
  const qs = wantRoom ? "&room=" + encodeURIComponent(wantRoom) : "";
  const r = await fetch("/token?identity=user-" + Math.random().toString(16).slice(2, 6) + qs);
  if (!r.ok) {
    let why = "HTTP " + r.status;
    try { const j = await r.json(); if (j.error) why = j.error; } catch (e) {}
    throw new Error("token request failed: " + why);
  }
  const d = await r.json();
  if (!d.url || !d.token) throw new Error(d.error || "bad token response");
  if ((d.room || "") !== wantRoom) resetTranscript(); // server gave a different room
  currentRoom = d.room || "";
  storeRoom(currentRoom);
  // Browser-side audio processing: WebRTC noise suppression / AGC can mangle
  // the signal before Whisper ever sees it - user-tunable in Settings.
  let caps = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  try {
    const cr = await fetch("/api/config");
    if (cr.ok) {
      const c = ((await cr.json()).config) || {};
      caps.echoCancellation = c.CAP_ECHO_CANCEL !== "0";
      caps.noiseSuppression = c.CAP_NOISE_SUPPRESS !== "0";
      caps.autoGainControl = c.CAP_AUTO_GAIN !== "0";
    }
  } catch (e) {}
  const dev = (() => { try { return localStorage.getItem("micDeviceId") || ""; } catch (e) { return ""; } })();
  room = new LK.Room({
    adaptiveStream: false, dynacast: false,
    audioCaptureDefaults: {
      echoCancellation: caps.echoCancellation,
      noiseSuppression: caps.noiseSuppression,
      autoGainControl: caps.autoGainControl,
      deviceId: dev ? { ideal: dev } : undefined,
    },
  });
  room.on(LK.RoomEvent.DataReceived, (payload, participant, kind, topic) => {
    if (topic !== "transcript") return;
    try {
      const m = JSON.parse(new TextDecoder().decode(payload));
      if (m && typeof m.text === "string") addBubble(m.who, m.text, m.id, m.final !== false);
    } catch (e) {
      console.warn("bad transcript packet", e);
    }
  });
  room.on(LK.RoomEvent.TrackSubscribed, (track) => {
    if (track.kind === "audio") {
      try {
        const el = track.attach();
        el.style.display = "none";
        document.body.appendChild(el);
      } catch (e) {
        console.warn("attach failed", e);
      }
      hookAgentAudio(track);
    }
  });
  room.on(LK.RoomEvent.MediaDevicesError, (e) => {
    toast(micErrorText(e), 4000);
  });
  room.on(LK.RoomEvent.Disconnected, () => {
    if (live) { live = false; setLiveUi(false); toast("Call ended"); }
  });
  try {
    await room.prepareConnection(d.url, d.token);
  } catch (e) {
    console.warn("prepareConnection skipped", e);
  }
  await room.connect(d.url, d.token);
  try {
    await room.localParticipant.setMicrophoneEnabled(true);
  } catch (e) {
    throw new Error(micErrorText(e));
  }
  const pub = room.localParticipant.getTrackPublication(LK.Track.Source.Microphone);
  if (!pub || !pub.track || !pub.track.mediaStreamTrack) {
    throw new Error("microphone track was not published - stop and start the call again");
  }
  micPubOk = true;
  micWarned = false;
  micSilenceSince = 0;
  try {
    micStream = new MediaStream([pub.track.mediaStreamTrack]);
    hookMicAudio(micStream);
  } catch (e) {
    console.warn("mic analyser failed", e);
  }
}

async function startCall() {
  $("callbtn").disabled = true;
  setState("checking");
  let pc;
  try {
    pc = await precheck();
  } catch (e) {
    pc = { fatal: [String(e && e.message || e)], warn: [] };
  }
  if (pc.fatal.length) {
    setState("blocked");
    toast("Cannot start: " + pc.fatal[0] + (pc.fatal.length > 1 ? " (+" + (pc.fatal.length - 1) + " more, see console)" : ""), 4200);
    console.warn("precheck fatal:", pc.fatal, "warnings:", pc.warn);
    $("callbtn").disabled = false;
    return;
  }
  pc.warn.forEach((w) => toast(w, 3600));
  setState("connecting");
  try {
    await connect();
    live = true;
    setLiveUi(true);
    setState("listening");
    toast("Connected - just talk (pause to hand over the turn)");
  } catch (e) {
    console.error(e);
    setState("ready");
    micPubOk = false;
    toast("Could not connect: " + (e && e.message ? e.message : e), 4000);
  }
  $("callbtn").disabled = false;
}

async function stopCall() {
  live = false;
  micPubOk = false;
  micWarned = false;
  clearTimeout(stateTimer);
  try { if (room) await room.disconnect(); } catch (e) { console.warn("disconnect", e); }
  cleanupAudio();
  setLiveUi(false);
  setState("ready");
}

function setLiveUi(on) {
  const b = $("callbtn");
  b.classList.toggle("live", on);
  b.innerHTML = on ? ICON_STOP : ICON_PLAY;
  b.title = on ? "Stop the call" : "Start the call";
  if (!on) { setState("ready"); }
}

$("callbtn").addEventListener("click", () => {
  try { (live ? stopCall() : startCall()); } catch (e) { console.error(e); toast("Button error: " + e.message); }
});

$("chatform").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = $("chatinput");
  const text = (input.value || "").trim();
  if (!text) return;
  if (!room || !live) { toast("Start a call first"); return; }
  input.value = "";
  const localId = "loc" + (++msgCounter);
  addBubble("user", text, localId, true);
  lastEcho = { text, at: Date.now() };
  try {
    await room.localParticipant.publishData(
      new TextEncoder().encode(JSON.stringify({ text })),
      { reliable: true, topic: "chat-input" });
  } catch (e) {
    toast("Send failed: " + (e && e.message ? e.message : e));
  }
});
