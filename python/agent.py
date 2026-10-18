"""Local realtime voice assistant agent: VAD -> Whisper -> textgen -> Piper.

Transcripts are pushed over the data channel (topic "transcript") the moment
they exist: user text when the STT finalizes, reply text when the LLM finishes.
Nothing waits for the voice to play. Conversation history is appended to
user_data/chat/<timestamp>-<room>.jsonl.
"""
import asyncio
import difflib
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "user_data" / ".env.local"
CHAT_DIR = ROOT / "user_data" / "chat"

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ENV_FILE)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("voice-agent")

from local_llm import create_textgen_llm, mask_key, relay_llm  # noqa: E402
from local_stt import create_stt  # noqa: E402
from local_tts import create_piper_tts  # noqa: E402

from livekit import agents  # noqa: E402
from livekit.agents import (  # noqa: E402
    Agent,
    AgentServer,
    AgentSession,
    stt as lk_stt,
)
from livekit.plugins import openai, silero  # noqa: E402

try:  # livekit-agents >= 1.8: RoomOptions replaces RoomInput/RoomOutputOptions
    from livekit.agents import RoomOptions as _RoomOptions
except ImportError:  # older releases still ship the split options
    _RoomOptions = None


class _QuietDeprecations(logging.Filter):
    """Hide known one-time deprecation notices from livekit.agents.

    The one-window supervisor deliberately launches `agent.py dev` for
    automatic room dispatch, so these warnings do not apply to this project.
    They are pure console noise for local users - real warnings still pass.
    """

    _MSGS = (
        "dev mode is deprecated",
        "in-process auto-reload has been removed",
        "RoomInputOptions and RoomOutputOptions are deprecated",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(m in msg for m in self._MSGS)


logging.getLogger("livekit.agents").addFilter(_QuietDeprecations())

INSTRUCTIONS = (
    "You are a helpful AI voice assistant. Keep replies short and natural for "
    "spoken conversation: usually 1-3 sentences, plain conversational text. "
    "Never use markdown, code blocks, bullet points, or emoji - your words are "
    "spoken out loud. If the user asks for something long, summarize it first "
    "and offer to give more detail."
)

CONFIG_DEFAULTS = {
    "LIVEKIT_URL": "ws://127.0.0.1:7880",
    "LIVEKIT_API_KEY": "devkey",
    "LIVEKIT_API_SECRET": "voicelocal-secret-1234567890abcdef",
    "TEXTGEN_BASE_URL": "http://127.0.0.1:5000/v1",
    "TEXTGEN_API_KEY": "sk-1234",
    "TEXTGEN_MODEL": "local-model",
    "LLM_STREAMING": "0",
    "WHISPER_MODEL": "small",
    "WHISPER_DEVICE": "auto",
    "WHISPER_COMPUTE": "auto",
    "WHISPER_LANGUAGE": "en",
    "WHISPER_BEAM": "1",
    "WHISPER_VAD_FILTER": "0",
    "WHISPER_TEMPERATURE": "0.0",
    "WHISPER_MIN_LOGPROB": "-1.0",
    "WHISPER_MAX_COMPRATIO": "2.4",
    "TTS_VOICE": "en_US-amy-medium.onnx",
    "TTS_SPEED": "1.1",
    "VAD_MIN_SILENCE": "0.55",
    "VAD_ACTIVATION_THRESHOLD": "0.45",
    "ENDPOINTING_DELAY": "0.7",
    "ECHO_GUARD": "1",
    "ECHO_GUARD_TAIL": "0.8",
}

HEAVY_KEYS = (
    "WHISPER_MODEL",
    "WHISPER_DEVICE",
    "WHISPER_COMPUTE",
    "TTS_VOICE",
    "TTS_SPEED",
)


def load_config() -> dict:
    cfg = dict(CONFIG_DEFAULTS)
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    for k in cfg:
        env_v = os.environ.get(k)
        if env_v:
            cfg[k] = env_v
    return cfg


def models_fingerprint(cfg: dict) -> str:
    return "|".join(cfg.get(k, "") for k in HEAVY_KEYS)


class EchoGuard:
    def __init__(self, tail: float = 0.8, threshold: float = 0.55) -> None:
        self._tail = max(0.0, tail)
        self._threshold = threshold
        self._speaking = False
        self._quiet_at = 0.0
        self._last_spoken = ""

    def on_agent_state(self, ev) -> None:
        state = getattr(ev, "new_state", "")
        if state == "speaking":
            self._speaking = True
        elif state in ("listening", "thinking", "idle"):
            self._speaking = False
            self._quiet_at = time.time()

    def note_spoken(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self._last_spoken = text

    def in_window(self) -> bool:
        return self._speaking or (time.time() - self._quiet_at) <= self._tail

    def check(self, text: str):
        if not text or not text.strip():
            return False, ""
        if not self.in_window():
            return False, ""
        if not self._last_spoken:
            return False, ""
        sim = echo_similarity(text, self._last_spoken)
        if sim >= self._threshold:
            return True, "matches the assistant's last words (similarity %.2f)" % sim
        return False, ""


def echo_similarity(a: str, b: str) -> float:
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    if not a or not b:
        return 0.0
    ta = set(re.findall(r"\w+", a))
    tb = set(re.findall(r"\w+", b))
    union = ta | tb
    jac = len(ta & tb) / len(union) if union else 0.0
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    return max(jac, seq)


def build_echo_guard(cfg: dict):
    if cfg.get("ECHO_GUARD", "1") != "1":
        logger.info("Echo guard disabled by settings (ECHO_GUARD=0)")
        return None
    tail = float(cfg.get("ECHO_GUARD_TAIL", "0.8"))
    logger.info("Echo guard ON: agent-speech window + %.2fs tail", tail)
    return EchoGuard(tail=tail)


class GuardedSTT(lk_stt.STT):
    def __init__(self, inner, guard: EchoGuard) -> None:
        super().__init__(
            capabilities=lk_stt.STTCapabilities(streaming=False, interim_results=False)
        )
        self._inner = inner
        self._guard = guard

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        ev = await self._inner.recognize(
            buffer, language=language, conn_options=conn_options
        )
        text = ""
        if ev.alternatives:
            text = (ev.alternatives[0].text or "").strip()
        if text:
            drop, why = self._guard.check(text)
            if drop:
                logger.warning(
                    "ECHO GUARD dropped transcript %r (%s) - the mic picked up "
                    "the assistant's own voice",
                    text, why,
                )
                return lk_stt.SpeechEvent(
                    type=lk_stt.SpeechEventType.FINAL_TRANSCRIPT,
                    alternatives=[lk_stt.SpeechData(
                        language=(ev.alternatives[0].language or language or "en"),
                        text="", start_time=0.0, end_time=0.0)],
                )
        return ev


class TranscriptHub:
    def __init__(self, room, history_path=None) -> None:
        self._room = room
        self._seq = 0
        self._tasks = set()
        self._history = None
        if history_path is not None:
            try:
                CHAT_DIR.mkdir(parents=True, exist_ok=True)
                self._history = open(history_path, "a", encoding="utf-8")
                logger.info("Chat log: %s", history_path)
            except OSError as exc:
                logger.warning("Chat log disabled (%s)", exc)

    def _write_history(self, who: str, text: str, msg_id: str) -> None:
        if self._history is None:
            return
        try:
            line = json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "who": who,
                "text": text,
                "id": msg_id,
            }, ensure_ascii=False)
            self._history.write(line + "\n")
            self._history.flush()
        except OSError as exc:
            logger.warning("chat history write failed (%s)", exc)

    def _send(self, who: str, text: str, msg_id: str, final: bool) -> None:
        self._write_history(who, text, msg_id)
        payload = json.dumps({"who": who, "text": text, "id": msg_id, "final": final})
        try:
            coro = self._room.local_participant.publish_data(
                payload, reliable=True, topic="transcript")
        except Exception as exc:
            logger.warning("transcript publish failed (%s)", exc)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            logger.warning("transcript publish skipped - no event loop yet")
            return
        task = loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_pub_done)

    def _on_pub_done(self, task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            logger.warning("transcript publish failed (%s)", task.exception())

    def user(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self._seq += 1
            msg_id = f"u{self._seq}"
            self._send("user", text, msg_id, True)
            logger.info("user said: %s", text)

    def bot(self, text: str, msg_id: str = "", final: bool = True) -> None:
        text = (text or "").strip()
        if text:
            self._send("bot", text, msg_id, final)

    def close(self) -> None:
        if self._history is not None:
            try:
                self._history.close()
            except OSError:
                pass
            self._history = None


def build_vad(cfg: dict):
    return silero.VAD.load(
        min_speech_duration=0.05,
        min_silence_duration=float(cfg.get("VAD_MIN_SILENCE", "0.55")),
        prefix_padding_duration=0.3,
        activation_threshold=float(cfg.get("VAD_ACTIVATION_THRESHOLD", "0.45")),
        deactivation_threshold=0.35,
    )


def build_stt(cfg: dict):
    return create_stt(
        model=cfg.get("WHISPER_MODEL", "small"),
        device=cfg.get("WHISPER_DEVICE", "auto"),
        compute_type=cfg.get("WHISPER_COMPUTE", "auto"),
        language=cfg.get("WHISPER_LANGUAGE", "en"),
        beam_size=int(cfg.get("WHISPER_BEAM", "1")),
        vad_filter=cfg.get("WHISPER_VAD_FILTER", "0") == "1",
        temperature=float(cfg.get("WHISPER_TEMPERATURE", "0.0")),
        min_logprob=float(cfg.get("WHISPER_MIN_LOGPROB", "-1.0")),
        max_compratio=float(cfg.get("WHISPER_MAX_COMPRATIO", "2.4")),
    )


def build_tts(cfg: dict):
    return create_piper_tts(
        voice_name=cfg.get("TTS_VOICE", "en_US-amy-medium.onnx"),
        speed=float(cfg.get("TTS_SPEED", "1.1")),
    )


def build_llm(cfg: dict, publish_reply=None):
    base = cfg.get("TEXTGEN_BASE_URL", "http://127.0.0.1:5000/v1")
    key = cfg.get("TEXTGEN_API_KEY", "sk-1234")
    model = cfg.get("TEXTGEN_MODEL", "local-model")
    streaming = cfg.get("LLM_STREAMING", "0") == "1"
    logger.info("textgen LLM: %s (key %s, model=%s, %s)",
                base, mask_key(key), model,
                "token streaming" if streaming else "full-reply mode")
    if streaming:
        inner = openai.LLM(base_url=base, api_key=key, model=model)
        if publish_reply:
            return relay_llm(inner, publish_reply)
        return inner
    return create_textgen_llm(base_url=base, api_key=key, model=model,
                              on_reply=publish_reply)


_MODELS = {"fp": None, "vad": None, "stt": None, "tts": None, "last_good": {}}
_MODELS_LOCK = threading.Lock()


def get_models(cfg: dict):
    fp = models_fingerprint(cfg)
    with _MODELS_LOCK:
        if _MODELS["fp"] == fp and _MODELS["vad"] is not None:
            return _MODELS["vad"], _MODELS["stt"], _MODELS["tts"]
        t0 = time.time()
        logger.info("Loading voice models (this happens once per settings change)...")
        _MODELS["vad"] = build_vad(cfg)
        _MODELS["stt"] = build_stt(cfg)
        _MODELS["tts"] = build_tts(cfg)
        _MODELS["fp"] = fp
        _MODELS["last_good"] = {k: cfg.get(k, "") for k in HEAVY_KEYS}
        logger.info("Voice models ready in %.1fs.", time.time() - t0)
        return _MODELS["vad"], _MODELS["stt"], _MODELS["tts"]


def warm_openai(cfg: dict) -> None:
    try:
        import openai.lib.streaming.chat  # noqa: F401
    except Exception:
        pass
    try:
        build_llm(cfg)
    except Exception:
        logger.exception("Prewarm: could not build the LLM client yet")


def prewarm_models() -> None:
    def _run() -> None:
        try:
            warm_openai(load_config())
            get_models(load_config())
        except Exception:
            logger.exception("Prewarm failed - models will load at the first "
                             "conversation start instead.")
    threading.Thread(target=_run, name="model-prewarm", daemon=True).start()


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=INSTRUCTIONS)


def new_history_path(room_name: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^\w.-]+", "_", room_name or "room")
    return CHAT_DIR / f"{stamp}-{safe}.jsonl"


server = AgentServer(num_idle_processes=1)


@server.rtc_session()
async def voice_agent(ctx: agents.JobContext) -> None:
    hub = None
    try:
        await ctx.connect()
        logger.info("Agent joined room %s", ctx.room.name)
        cfg = load_config()
        try:
            vad, stt, tts = await asyncio.to_thread(get_models, cfg)
        except Exception:
            logger.exception("Loading models with the new settings FAILED - "
                             "retrying with the previous settings. Check the "
                             "model/voice names in the web UI.")
            prev = dict(cfg)
            prev.update(_MODELS.get("last_good") or {k: CONFIG_DEFAULTS.get(k, "") for k in HEAVY_KEYS})
            vad, stt, tts = await asyncio.to_thread(get_models, prev)

        hub = TranscriptHub(ctx.room, new_history_path(ctx.room.name))
        guard = build_echo_guard(cfg)

        def publish_bot(text: str, msg_id: str = "", final: bool = True) -> None:
            hub.bot(text, msg_id, final)
            if guard is not None and final:
                guard.note_spoken(text)

        llm = build_llm(cfg, publish_reply=publish_bot)

        stream_stt = lk_stt.StreamAdapter(stt=stt, vad=vad)
        if guard is not None:
            stream_stt = lk_stt.StreamAdapter(stt=GuardedSTT(stt, guard), vad=vad)

        session = AgentSession(
            vad=vad,
            stt=stream_stt,
            llm=llm,
            tts=tts,
            turn_handling={
                "turn_detection": None,
                "endpointing": {
                    "mode": "fixed",
                    "min_delay": float(cfg.get("ENDPOINTING_DELAY", "0.7")),
                    "max_delay": 6.0,
                },
                "interruption": {
                    "mode": "vad",
                    "min_duration": 0.6,
                    "min_words": 2,
                },
            },
        )

        if _RoomOptions is not None:
            await session.start(
                room=ctx.room,
                agent=Assistant(),
                room_options=_RoomOptions(
                    audio_enabled=True, transcription_enabled=False
                ),
            )
        else:
            from livekit.agents import RoomOutputOptions

            await session.start(
                room=ctx.room,
                agent=Assistant(),
                room_output_options=RoomOutputOptions(
                    audio_enabled=True, transcription_enabled=False
                ),
            )
        logger.info("Session started for room %s", ctx.room.name)

        def on_user_transcript(ev) -> None:
            if ev.is_final and getattr(ev, "transcript", ""):
                hub.user(ev.transcript)

        session.on("user_input_transcribed")(on_user_transcript)

        def on_data(ev) -> None:
            topic = getattr(ev, "topic", "") or ""
            if topic != "chat-input":
                return
            raw = getattr(ev, "data", b"")
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            try:
                payload = json.loads(bytes(raw).decode("utf-8"))
            except Exception:
                return
            text = (payload.get("text") or "").strip() if isinstance(payload, dict) else ""
            if not text:
                return
            hub.user(text)
            asyncio.ensure_future(reply_to_text(session, text))

        ctx.room.on("data_received")(on_data)

        if guard is not None:
            session.on("agent_state_changed")(guard.on_agent_state)

            def _on_item(ev, _guard=guard) -> None:
                item = getattr(ev, "item", None)
                role = str(getattr(item, "role", ""))
                if role != "assistant":
                    return
                txt = getattr(item, "text_content", None)
                if not txt:
                    parts = [c for c in (getattr(item, "content", None) or [])
                             if isinstance(c, str)]
                    txt = " ".join(parts)
                _guard.note_spoken(txt)

            session.on("conversation_item_added")(_on_item)

        await session.generate_reply(
            instructions="Greet the user in one short friendly sentence and offer to help."
        )
    except Exception as exc:
        logger.exception("Agent session for room %s ended with an error", ctx.room.name)
        logger.error("The conversation stopped, but the agent worker is still alive.")
        msg = str(exc).lower()
        if "before connecting" in msg:
            logger.error("FIX -> the room was used before ctx.connect(). Update agent.py.")
        elif "Connection" in type(exc).__name__ or "connect" in msg:
            logger.error("FIX -> textgen or LiveKit was unreachable. Start it again,")
            logger.error("         then press Start conversation in the browser.")
        else:
            logger.error("FIX -> read the traceback above. Then press Start conversation")
            logger.error("         in the browser to open a fresh session.")
    finally:
        if hub is not None:
            hub.close()


async def reply_to_text(session, text: str) -> None:
    try:
        await session.generate_reply(user_input=text)
    except Exception:
        logger.exception("text chat reply failed")


def preflight() -> None:
    cfg = load_config()
    logger.info("-" * 60)
    logger.info("PREFLIGHT: checking dependencies...")

    lk = cfg.get("LIVEKIT_URL", "ws://127.0.0.1:7880").replace("ws://", "").replace("wss://", "").split("/")[0]
    host, _, port = lk.partition(":")
    host = host or "127.0.0.1"
    port = int(port or 7880)
    try:
        socket.create_connection((host, port), timeout=3).close()
        logger.info("PREFLIGHT: LiveKit server OK at %s:%s", host, port)
    except OSError as e:
        logger.error("PREFLIGHT: LiveKit server NOT reachable at %s:%s (%s).", host, port, e)
        logger.error("PREFLIGHT: FIX -> it usually auto-starts with server.py; if you run")
        logger.error("PREFLIGHT:        it manually, start it first, then restart the agent.")

    base = cfg.get("TEXTGEN_BASE_URL", "http://127.0.0.1:5000/v1")
    key = cfg.get("TEXTGEN_API_KEY", "sk-1234")
    model = cfg.get("TEXTGEN_MODEL", "local-model")
    url = base.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=5) as r:
            logger.info("PREFLIGHT: textgen API OK at %s (HTTP %s)", base, r.status)
            logger.info("PREFLIGHT: using model name '%s' (textgen serves whatever is loaded in its UI)", model)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            logger.error("PREFLIGHT: textgen API key MISMATCH (HTTP %s).", e.code)
            logger.error("PREFLIGHT: FIX -> web UI -> gear icon -> set 'API key' to match")
            logger.error("PREFLIGHT:        textgen's --api-key, then click 'Save changes'.")
            logger.error("PREFLIGHT:        That is all - the new key applies to your NEXT")
            logger.error("PREFLIGHT:        conversation automatically (no restart needed).")
        else:
            logger.error("PREFLIGHT: textgen API returned HTTP %s at %s - check the textgen window.", e.code, base)
    except Exception as e:
        logger.error("PREFLIGHT: textgen API NOT reachable at %s (%s).", base, e)
        logger.error("PREFLIGHT: FIX -> start textgen with --api enabled (CMD_FLAGS.txt), test with:")
        logger.error("PREFLIGHT:        curl http://127.0.0.1:5000/docs   (must return a page)")
        logger.error("PREFLIGHT:        The agent retries by itself if textgen blips later.")
    logger.info("-" * 60)


if __name__ == "__main__":
    try:
        preflight()
        prewarm_models()
        agents.cli.run_app(server)
    except KeyboardInterrupt:
        sys.exit(0)
