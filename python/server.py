"""One-window supervisor + web UI/API server for the local voice assistant.

Runs the LiveKit server and the voice agent as children (killed together via
a Windows Job Object), serves python/web/index.html and the config/model/chat
history API on http://127.0.0.1:8090.

Extras:
- Remembers the app window's last position/size/mode (user_data/window_state.json)
  and restores it on the next start (Chrome/Edge app mode + Win32 tracking).
- Shuts everything down when the UI window is closed: the page sends a
  heartbeat, and the app-mode browser process is watched as well
  (CLOSE_WITH_WINDOW=1, set 0 in user_data/.env.local to disable).
"""
import asyncio
import ctypes
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

if os.name == "nt":
    import ctypes.wintypes

from aiohttp import web
from dotenv import load_dotenv
from livekit import api as lk_api

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent / "web"
ENV_FILE = ROOT / "user_data" / ".env.local"
CHAT_DIR = ROOT / "user_data" / "chat"
LIVEKIT_YAML = ROOT / "user_data" / "livekit.yaml"
LIVEKIT_EXE = ROOT / "bin" / ("livekit-server.exe" if os.name == "nt" else "livekit-server")
AGENT_SCRIPT = Path(__file__).resolve().parent / "agent.py"
WINDOW_STATE_FILE = ROOT / "user_data" / "window_state.json"

# --- UI-window lifecycle (heartbeat + close detection) -----------------------
HB_INTERVAL = 2.5        # the page beats every 2.5 s while visible
HB_GAP_SHUTDOWN = 600.0  # last-resort net: minutes of total silence (minimized
                         # pages can be timer-throttled/frozen by Chromium)
HB_GAP_PENDING = 6.0     # after a pagehide beacon: a REAL close fires it; a
                         # refresh re-beats within ~1.5 s and cancels it
_SHUT_LOCK = threading.Lock()
_SHUTTING = False
_LAST_BEAT = 0.0         # time.monotonic() of the last /api/heartbeat
_CLOSE_PENDING_AT = 0.0  # time.monotonic() of the last /api/window/closing
_WIN_MINIMIZED = False   # tracked app window is minimized (beats may pause)


def _monotonic() -> float:
    return time.monotonic()


def heartbeat_fresh(window: float = 3.0) -> bool:
    return _LAST_BEAT > 0 and (_monotonic() - _LAST_BEAT) <= window


def request_shutdown(reason: str) -> None:
    """Stop the children and exit this supervisor (closing the console too)."""
    global _SHUTTING
    if setting("CLOSE_WITH_WINDOW", "1") != "1":
        return
    if heartbeat_fresh():
        # Another UI page (a second window/tab) is still alive - keep running.
        log(f"window closed, but the UI is still sending heartbeats - staying up ({reason})")
        return
    with _SHUT_LOCK:
        if _SHUTTING:
            return
        _SHUTTING = True
    log(f"UI window closed ({reason}) - shutting down LiveKit + agent, bye!")
    try:
        stop_children()
    except Exception:
        pass
    os._exit(0)


def heartbeat_watcher() -> None:
    """Exit when the UI page goes quiet: fast path after a pagehide beacon
    (a real window close), slow backstop for everything else."""
    global _WIN_MINIMIZED
    while True:
        time.sleep(2.0)
        if _LAST_BEAT <= 0:
            continue  # nothing has opened the UI yet - nothing to watch
        if _WIN_MINIMIZED:
            continue  # tracked window is minimized - Chromium may freeze its timers
        gap = _monotonic() - _LAST_BEAT
        pending_recent = _CLOSE_PENDING_AT > 0 and (_monotonic() - _CLOSE_PENDING_AT) < 60
        threshold = HB_GAP_PENDING if pending_recent else HB_GAP_SHUTDOWN
        if gap >= threshold:
            request_shutdown(f"no UI heartbeat for {gap:.0f}s")


def venv_python() -> Path:
    """Interpreter inside the project venv (layout differs per OS)."""
    if os.name == "nt":
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"

load_dotenv(ENV_FILE)

HOST = "127.0.0.1"
PORT = 8090

CHILDREN = {}
_RESTART = {}
_OUT = {}


def log(msg: str) -> None:
    print(f"[  sup] {msg}", flush=True)


def _harden_streams() -> None:
    """Never crash the supervisor because the console can't encode a glyph."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except Exception:
            pass


def env_dict() -> dict:
    cfg = {}
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return cfg


def setting(key: str, default: str = "") -> str:
    return env_dict().get(key, os.environ.get(key, default))


JOB_OBJECT_EXTENDED_LIMIT = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


def setup_job_object():
    if os.name != "nt":
        return None
    try:
        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.SetInformationJobObject.restype = ctypes.c_int
        k32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k32.AssignProcessToJobObject.restype = ctypes.c_int
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        expected = ctypes.sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
        if expected != 144:
            log(f"job struct size {expected} unexpected (want 144) - skipping Job Object")
            return None

        job = k32.CreateJobObjectW(None, None)
        if not job:
            log(f"CreateJobObjectW failed (winerror {ctypes.get_last_error()}) "
                "- children stay alive after window close")
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT,
                                           ctypes.byref(info),
                                           ctypes.sizeof(info)):
            log(f"SetInformationJobObject failed (winerror {ctypes.get_last_error()}) "
                "- children stay alive after window close")
            k32.CloseHandle(job)
            return None

        log("Job Object active: closing this window shuts down all children")
        return job
    except Exception as exc:
        log(f"Job Object setup failed ({exc}) - continuing without it")
        return None


JOB = None
_K32 = None


def assign_job(proc) -> None:
    if JOB is None or _K32 is None or os.name != "nt":
        return
    try:
        if not _K32.AssignProcessToJobObject(JOB, int(proc._handle)):
            log(f"AssignProcessToJobObject failed (winerror {ctypes.get_last_error()})")
    except Exception as exc:
        log(f"job assign failed: {exc}")


def spawn(name: str, argv: list) -> None:
    log(f"[{name}] starting: {' '.join(str(a) for a in argv)}")
    # Force UTF-8 stdio on the pipes: on Windows the default for a Python
    # child is the legacy codepage (cp1252 etc.), so Piper's IPA phoneme
    # logs reached this reader as invalid bytes and printed as '<EFBFBD>'.
    child_env = dict(os.environ)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    child_env.setdefault("PYTHONUTF8", "1")
    try:
        proc = subprocess.Popen(
            [str(a) for a in argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(ROOT),
            env=child_env,
        )
    except FileNotFoundError:
        log(f"[{name}] FAILED: executable not found: {argv[0]}")
        log(f"[{name}] FIX -> re-run setup.bat, then start.bat again")
        return
    except OSError as exc:
        log(f"[{name}] FAILED to start ({exc})")
        return
    CHILDREN[name] = proc
    _OUT[name] = deque(maxlen=80)
    assign_job(proc)

    def pump() -> None:
        for line in proc.stdout:
            text = line.rstrip()
            ring = _OUT.get(name)
            if ring is not None:
                ring.append(text)
            print(f"[{name}] {text}", flush=True)

    threading.Thread(target=pump, daemon=True, name=f"{name}-pump").start()

    def watch() -> None:
        code = proc.wait()
        print(f"[  sup] [{name}] exited with code {code}", flush=True)
        CHILDREN.pop(name, None)
        if name == "lk" and code != 0 and child_saw(name, "could not parse config"):
            heal_yaml()
            _RESTART[name] = 1
        if _RESTART.get(name):
            delay = _RESTART[name]
            _RESTART[name] = min(delay * 2, 30)
            log(f"[{name}] restarting in {delay}s...")
            time.sleep(delay)
            spawn(name, argv)

    threading.Thread(target=watch, daemon=True, name=f"{name}-watch").start()


def child_saw(name: str, needle: str) -> bool:
    return any(needle in line for line in _OUT.get(name, ()))


def minimal_yaml() -> str:
    key = setting("LIVEKIT_API_KEY", "devkey") or "devkey"
    secret = (setting("LIVEKIT_API_SECRET", "")
              or "voicelocal-secret-1234567890abcdef")
    return (
        "# Local LiveKit server config - loopback only, no internet exposure\n"
        "port: 7880\n"
        "bind_addresses:\n"
        "  - 127.0.0.1\n"
        "\n"
        "rtc:\n"
        "  tcp_port: 7881\n"
        "  port_range_start: 50000\n"
        "  port_range_end: 50100\n"
        "  use_external_ip: false\n"
        "\n"
        "# Local development keys (must match .env.local)\n"
        "keys:\n"
        f"  {key}: {secret}\n"
    )


def heal_yaml() -> None:
    backup = LIVEKIT_YAML.with_name("livekit.yaml.broken")
    try:
        if LIVEKIT_YAML.exists():
            shutil.copyfile(LIVEKIT_YAML, backup)
            log(f"saved your old config as {backup.name}")
    except OSError as exc:
        log(f"could not back up livekit.yaml ({exc})")
    try:
        LIVEKIT_YAML.write_text(minimal_yaml(), encoding="utf-8")
        log("livekit.yaml was rejected by livekit-server - wrote a clean minimal "
            "config it cannot misparse")
        log("the agent joins rooms automatically; no room_config entry is needed")
    except OSError as exc:
        log(f"FAILED to regenerate livekit.yaml ({exc})")
        log("FIX -> open user_data/livekit.yaml in Notepad and simplify it, or delete it")


def stop_children() -> None:
    _RESTART.clear()
    for name, proc in list(CHILDREN.items()):
        try:
            proc.terminate()
        except OSError:
            pass


def _tcp_alive(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def livekit_target():
    url = setting("LIVEKIT_URL", "ws://127.0.0.1:7880")
    target = url.replace("ws://", "").replace("wss://", "").split("/")[0]
    host, _, port = target.partition(":")
    return host or "127.0.0.1", int(port or 7880)


def check_gpu():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        line = (out.stdout or "").strip().splitlines()[0] if out.stdout else ""
        if out.returncode == 0 and line:
            return f"YES - {line} "
    except Exception:
        pass
    return "no"


def check_textgen():
    base = setting("TEXTGEN_BASE_URL", "http://127.0.0.1:5000/v1")
    key = setting("TEXTGEN_API_KEY", "")
    headers = {"Authorization": "Bearer " + key} if key else {}
    try:
        req = urllib.request.Request(base.rstrip("/") + "/models", headers=headers)
        with urllib.request.urlopen(req, timeout=4) as r:
            return f"up (HTTP {r.status})"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "auth failed (check the API key)"
        return f"down (HTTP {e.code})"
    except Exception:
        return "down (start your LLM API)"


def banner() -> None:
    lk_host, lk_port = livekit_target()
    up = False
    for _ in range(10):
        if _tcp_alive(lk_host, lk_port):
            up = True
            break
        time.sleep(0.5)
    lib = WEB_DIR / "livekit-client.js"
    log("=" * 68)
    log("LOCAL VOICE ASSISTANT - one-window mode")
    log(f"UI + API       : http://{HOST}:{PORT}")
    log(f"python         : {platform.python_version()}  pid: {os.getpid()}")
    log(f"LiveKit        : ws://{lk_host}:{lk_port} -> {'UP' if up else 'DOWN'}")
    log(f"LLM API        : {check_textgen()}")
    log(f"GPU (nvidia)   : {check_gpu()}")
    log(f"livekit-client : {'OK' if lib.is_file() else 'MISSING - re-extract the kit'}")
    log("Children       : livekit-server (auto-restart) + agent worker (auto-restart)")
    log("Stop           : Ctrl+C here, or just close this window - nothing lingers")
    log("=" * 68)


WHISPER_SIZES_MB = {
    "tiny.en": 80, "base.en": 150, "small.en": 500, "small": 500,
    "medium.en": 1530, "medium": 1530, "large-v3-turbo": 1620, "large-v3": 3100,
}


def whisper_dir() -> Path:
    return ROOT / "user_data" / "models" / "whisper"


def piper_dir() -> Path:
    return ROOT / "user_data" / "models" / "piper"


def list_stt_models() -> dict:
    local = []
    base = whisper_dir()
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if d.is_dir() and (d / "model.bin").is_file():
                local.append({
                    "name": d.name,
                    "size_mb": round((d / "model.bin").stat().st_size / 1e6),
                    "local": True,
                })
    presets = []
    for name, mb in WHISPER_SIZES_MB.items():
        downloaded = bool(local_model_dir(name))
        presets.append({"name": name, "size_mb": mb, "local": downloaded})
    return {"presets": presets, "local_folders": local}


def local_model_dir(name: str):
    d = whisper_dir() / name
    return d if (d / "model.bin").is_file() else None


def list_tts_models() -> dict:
    voices = []
    base = piper_dir()
    if base.is_dir():
        for f in sorted(base.glob("*.onnx")):
            voices.append({"name": f.name, "local": True})
    suggestions = [
        "en_US-amy-medium.onnx", "en_US-lessac-medium.onnx",
        "en_US-ryan-high.onnx", "en_GB-alba-medium.onnx",
    ]
    for s in suggestions:
        if all(v["name"] != s for v in voices):
            voices.append({"name": s, "local": False})
    return {"voices": voices}


def parse_room_from_history(path: Path) -> str:
    m = re.match(r"\d{8}-\d{6}-(.+)\.jsonl$", path.name)
    return m.group(1) if m else "room"


async def api_chat_sessions(_request: web.Request) -> web.Response:
    sessions = []
    if CHAT_DIR.is_dir():
        for f in sorted(CHAT_DIR.glob("*.jsonl"), key=lambda p: p.name, reverse=True):
            turns = 0
            first_user = ""
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if rec.get("who") in ("user", "bot"):
                            turns += 1
                            if rec.get("who") == "user" and not first_user:
                                first_user = rec.get("text", "")[:80]
            except OSError:
                continue
            if turns:
                try:
                    mtime = int(f.stat().st_mtime)
                except OSError:
                    mtime = 0
                sessions.append({
                    "file": f.name,
                    "room": parse_room_from_history(f),
                    "turns": turns,
                    "first": first_user,
                    "mtime": mtime,
                })
    return web.json_response({"sessions": sessions[:100]})


async def api_chat_session(request: web.Request) -> web.Response:
    # ?file=<name>.jsonl  OR  ?room=<room> (picks that room's newest log).
    # System markers (session-start dividers) are passed through so the UI can
    # show where one sitting ended and the next began.
    name = request.query.get("file", "")
    room = request.query.get("room", "")
    f = None
    if room:
        if not re.fullmatch(r"[\w-]{1,64}", room or ""):
            return web.json_response({"error": "bad room name"}, status=400)
        cands = sorted(CHAT_DIR.glob(f"*-{room}.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        f = cands[0] if cands else None
        if f is None:
            return web.json_response({"file": "", "messages": []})
    else:
        if not re.fullmatch(r"[\w.-]+\.jsonl", name or ""):
            return web.json_response({"error": "bad file name"}, status=400)
        f = CHAT_DIR / name
    if not f.is_file():
        return web.json_response({"error": "not found"}, status=404)
    msgs = []
    try:
        with open(f, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                who = rec.get("who")
                if who in ("user", "bot") and rec.get("text"):
                    msgs.append({
                        "who": who, "text": rec["text"],
                        "ts": rec.get("ts", ""), "id": rec.get("id", ""),
                    })
                elif who == "system":
                    msgs.append({
                        "who": "system", "text": rec.get("text", ""),
                        "ts": rec.get("ts", ""), "id": "",
                    })
    except OSError as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"file": f.name, "messages": msgs})


async def api_chat_clear(request: web.Request) -> web.Response:
    """Empty the history file(s) of the current room (panel 'clear' button)."""
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid json"}, status=400)
    room = (body.get("room") or "").strip() if isinstance(body, dict) else ""
    if not re.fullmatch(r"[\w-]{1,64}", room or ""):
        return web.json_response({"error": "bad room name"}, status=400)
    if not CHAT_DIR.is_dir():
        return web.json_response({"ok": True, "cleared": []})
    name_re = re.compile(r"^\d{8}-\d{6}-[\w.-]+\.jsonl$")
    cleared = []
    for f in sorted(CHAT_DIR.glob(f"*-{room}.jsonl")):
        if not name_re.match(f.name):
            continue
        try:
            # Truncate instead of delete: the agent may still hold the file
            # open in append mode; its next write simply starts fresh.
            with open(f, "w", encoding="utf-8"):
                pass
            cleared.append(f.name)
        except OSError as exc:
            log(f"could not clear {f.name}: {exc}")
    if cleared:
        log(f"chat history cleared for room {room}: {', '.join(cleared)}")
    return web.json_response({"ok": True, "cleared": cleared})


async def api_config_get(_request: web.Request) -> web.Response:
    return web.json_response({"config": env_dict()})


async def api_config_post(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    updates = body.get("config")
    if not isinstance(updates, dict):
        return web.json_response({"error": "missing config object"}, status=400)
    current = env_dict()
    allowed = set(current) | {
        "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
        "TEXTGEN_BASE_URL", "TEXTGEN_API_KEY", "TEXTGEN_MODEL", "LLM_STREAMING",
        "WHISPER_MODEL", "WHISPER_DEVICE", "WHISPER_COMPUTE", "WHISPER_LANGUAGE",
        "WHISPER_BEAM", "WHISPER_VAD_FILTER", "WHISPER_TEMPERATURE",
        "WHISPER_MIN_LOGPROB", "WHISPER_MAX_COMPRATIO",
        "WHISPER_INITIAL_PROMPT",
        "CAP_ECHO_CANCEL", "CAP_NOISE_SUPPRESS", "CAP_AUTO_GAIN",
        "TTS_VOICE", "TTS_SPEED", "VAD_MIN_SILENCE", "VAD_ACTIVATION_THRESHOLD",
        "ENDPOINTING_DELAY", "ECHO_GUARD", "ECHO_GUARD_TAIL", "LOG_LEVEL",
        "AUTO_OPEN", "UI_WINDOW", "CLOSE_WITH_WINDOW",
    }
    for k, v in updates.items():
        if k not in allowed or not isinstance(v, str):
            continue
        current[k] = v.strip()
    lines = ["# Local Voice Assistant settings (saved from the web UI)",
             "# Applies to the NEXT conversation after you click Save."]
    for k, v in sorted(current.items()):
        lines.append(f"{k}={v}")
    try:
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        log(f"config save FAILED: {exc}")
        return web.json_response({"error": f"could not write {ENV_FILE.name}: {exc}"},
                                 status=500)
    log("config saved (applies to the next conversation)")
    return web.json_response({"ok": True, "config": current})


async def api_heartbeat(_request: web.Request) -> web.Response:
    global _LAST_BEAT, _CLOSE_PENDING_AT
    _LAST_BEAT = _monotonic()
    _CLOSE_PENDING_AT = 0.0  # a fresh beat means the page did NOT close
    return web.Response(status=204)


async def api_window_closing(_request: web.Request) -> web.Response:
    global _CLOSE_PENDING_AT
    _CLOSE_PENDING_AT = _monotonic()
    return web.Response(status=204)


async def api_models_stt(_request: web.Request) -> web.Response:
    return web.json_response(list_stt_models())


async def api_models_tts(_request: web.Request) -> web.Response:
    return web.json_response(list_tts_models())


async def api_llm_models(request: web.Request) -> web.Response:
    """Proxy GET {base}/models of the configured LLM provider.

    Done server-side so the browser never fights CORS and never needs the
    key directly. Body: {"url": optional base url, "key": optional key} -
    falls back to the saved settings for anything omitted.
    """
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    base = str(body.get("url") or setting("TEXTGEN_BASE_URL",
                                          "http://127.0.0.1:5000/v1") or "").strip()
    if "key" in body and body.get("key") is not None:
        key = str(body.get("key")).strip()
    else:
        key = setting("TEXTGEN_API_KEY", "").strip()
    if not re.match(r"^https?://", base):
        return web.json_response({"error": "base URL must start with http:// or https://",
                                  "models": []})
    url = base.rstrip("/") + "/models"
    headers = {"Authorization": "Bearer " + key} if key else {}

    def _fetch():
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    try:
        data = await asyncio.to_thread(_fetch)
    except urllib.error.HTTPError as e:
        why = "auth failed (check the API key)" if e.code in (401, 403) \
            else f"HTTP {e.code}"
        return web.json_response({"error": why, "models": []})
    except Exception as exc:
        return web.json_response({"error": str(exc)[:160], "models": []})
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
    elif isinstance(data, list):
        items = data
    else:
        items = []
    models = []
    for m in items:
        if isinstance(m, dict):
            mid = m.get("id") or m.get("name") or m.get("model")
            if mid:
                models.append(str(mid))
        elif isinstance(m, str):
            models.append(m)
    models = sorted(set(models))
    return web.json_response({"base": base, "count": len(models), "models": models})


def count_local_models() -> dict:
    stt = 0
    base = whisper_dir()
    if base.is_dir():
        stt = sum(1 for d in base.iterdir() if d.is_dir() and (d / "model.bin").is_file())
    tts = len(list(piper_dir().glob("*.onnx"))) if piper_dir().is_dir() else 0
    return {"stt": stt, "tts": tts}


async def health(_request: web.Request) -> web.Response:
    lk_host, lk_port = livekit_target()
    lk_up = await asyncio.to_thread(_tcp_alive, lk_host, lk_port)
    textgen_state = await asyncio.to_thread(check_textgen)
    gpu_state = await asyncio.to_thread(check_gpu)
    try:
        kids = {name: ("up" if p.poll() is None else f"exit {p.returncode}")
                for name, p in CHILDREN.items()}
    except Exception:
        kids = {}
    lib_ok = (WEB_DIR / "livekit-client.js").is_file()
    return web.json_response({
        "ui": "ok",
        "python": platform.python_version(),
        "livekit": {"status": "up" if lk_up else "down", "url": setting("LIVEKIT_URL")},
        "textgen": {"status": textgen_state, "url": setting("TEXTGEN_BASE_URL")},
        "gpu": gpu_state,
        "models": count_local_models(),
        "children": kids,
        "venv": venv_python().exists(),
        "lib_file": {"exists": lib_ok},
    })


async def index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html")


async def lib(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "livekit-client.js")


async def favicon(_request: web.Request) -> web.Response:
    return web.Response(status=204)


async def token(request: web.Request) -> web.Response:
    identity = (request.query.get("identity") or f"user-{os.urandom(2).hex()}").strip()
    if not re.fullmatch(r"[\w-]{1,64}", identity):
        return web.json_response({"error": "bad identity"}, status=400)
    # The UI re-asks for ITS room (localStorage) so conversations continue in
    # the same chat log across reloads and app restarts. No param = new room.
    room = (request.query.get("room") or "").strip()
    if room:
        if not re.fullmatch(r"[\w-]{1,64}", room):
            return web.json_response({"error": "bad room name"}, status=400)
    else:
        room = f"voice-{os.urandom(4).hex()}"
    key = setting("LIVEKIT_API_KEY", "devkey")
    secret = setting("LIVEKIT_API_SECRET", "")
    if not key or not secret:
        log("token REFUSED: LIVEKIT_API_KEY/SECRET missing - check user_data/.env.local")
        return web.json_response(
            {"error": "server has no LIVEKIT_API_KEY/SECRET configured"}, status=500)
    tk = (
        lk_api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(lk_api.VideoGrants(room_join=True, room=room))
    )
    jwt = tk.to_jwt()
    log(f"token issued identity={identity} room={room} (agent auto-joins every room)")
    return web.json_response({"url": setting("LIVEKIT_URL"), "token": jwt,
                              "room": room, "identity": identity})


@web.middleware
async def log_and_nocache(request: web.Request, handler):
    status = 500
    try:
        resp = await handler(request)
        status = resp.status
    except web.HTTPException as e:
        status = e.status
        raise
    finally:
        # Heartbeats arrive every ~2.5 s - logging them would drown the console.
        if request.path not in ("/api/heartbeat", "/api/window/closing"):
            log(f"{request.method} {request.path_qs} -> {status}")
        try:
            resp.headers["Cache-Control"] = "no-store"
        except UnboundLocalError:
            pass
    return resp


def find_app_browser():
    """A Chromium browser we can launch in --app mode (own window, no tabs).

    Windows 11 always ships Edge; Chrome is checked first because users
    often have it. Mic permissions work the same as in the normal browser.
    """
    cands = []
    if os.name == "nt":
        pf = os.environ.get("ProgramFiles", "C:\\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        lap = os.environ.get("LocalAppData", "")
        for root in (lap, pf, pf86):
            if not root:
                continue
            cands.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
            cands.append(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    elif sys.platform == "darwin":
        for name in ("Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser"):
            cands.append(Path("/Applications") / name / "Contents" / "MacOS" / name)
    else:
        for exe in ("google-chrome", "google-chrome-stable", "microsoft-edge",
                    "microsoft-edge-stable", "chromium", "chromium-browser",
                    "brave-browser"):
            p = shutil.which(exe)
            if p:
                cands.append(Path(p))
    for exe in cands:
        try:
            if exe.is_file() and os.access(str(exe), os.X_OK):
                return exe
        except OSError:
            continue
    return None


# --- Window state: remember position/size/mode -------------------------------


def load_window_state() -> dict:
    try:
        with open(WINDOW_STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            return st
    except Exception:
        pass
    return {}


def save_window_state(st: dict) -> None:
    try:
        WINDOW_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = WINDOW_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, WINDOW_STATE_FILE)
    except Exception as exc:
        log(f"could not save window state: {exc}")


if os.name == "nt":
    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.wintypes.DWORD),
                    ("rcMonitor", _RECT), ("rcWork", _RECT),
                    ("dwFlags", ctypes.wintypes.DWORD)]

_WINDOW_TITLE = "Local Voice Assistant"


def _win32():
    if os.name != "nt":
        return None
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.IsWindow.argtypes = [ctypes.wintypes.HWND]
    u.IsWindow.restype = ctypes.c_bool
    u.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
    u.IsWindowVisible.restype = ctypes.c_bool
    u.IsZoomed.argtypes = [ctypes.wintypes.HWND]
    u.IsZoomed.restype = ctypes.c_bool
    u.IsIconic.argtypes = [ctypes.wintypes.HWND]
    u.IsIconic.restype = ctypes.c_bool
    u.GetWindowTextLengthW.argtypes = [ctypes.wintypes.HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
    u.GetWindowRect.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(_RECT)]
    u.GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)]
    u.GetWindow.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT]
    u.GetWindow.restype = ctypes.wintypes.HWND
    u.MonitorFromWindow.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.DWORD]
    u.MonitorFromWindow.restype = ctypes.wintypes.HMONITOR
    u.GetMonitorInfoW.argtypes = [ctypes.wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
    u.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
    try:
        u.GetDpiForWindow.restype = ctypes.c_uint
        u.GetDpiForWindow.argtypes = [ctypes.wintypes.HWND]
    except AttributeError:
        pass
    return u


def _window_title(u, hwnd) -> str:
    n = u.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value or ""


def _find_ui_window(u, pid: int, deadline: float):
    """Find the app window: first by browser pid, then by exact page title
    (covers Edge/Chrome delegating the launch to an existing instance)."""
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    u.EnumWindows.argtypes = [WNDENUMPROC, ctypes.wintypes.LPARAM]
    while _monotonic() < deadline:
        by_pid, by_title = [], []

        def _cb(hwnd, _lparam, _by_pid=by_pid, _by_title=by_title):
            try:
                if not u.IsWindowVisible(hwnd):
                    return True
                wpid = ctypes.wintypes.DWORD(0)
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
                title = _window_title(u, hwnd)
                if not title or u.GetWindow(hwnd, 4):  # GW_OWNER: skip owned popups
                    return True
                if pid and wpid.value == pid:
                    _by_pid.append((hwnd, title))
                if title == _WINDOW_TITLE:
                    _by_title.append(hwnd)
            except Exception:
                pass
            return True

        cb = WNDENUMPROC(_cb)
        u.EnumWindows(cb, 0)
        if by_pid:
            return by_pid[0][0], True
        if by_title:
            return by_title[0], False
        time.sleep(0.5)
    return None, False


def _sample_window(u, hwnd) -> dict:
    rc = _RECT()
    if not u.GetWindowRect(hwnd, ctypes.byref(rc)):
        return {}
    zoomed = bool(u.IsZoomed(hwnd))
    fullscreen = False
    try:
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if u.GetMonitorInfoW(u.MonitorFromWindow(hwnd, 2), ctypes.byref(mi)):
            m = mi.rcMonitor
            tol = 8
            fullscreen = (abs(rc.left - m.left) <= tol and abs(rc.top - m.top) <= tol
                          and abs(rc.right - m.right) <= tol and abs(rc.bottom - m.bottom) <= tol)
    except Exception:
        pass
    try:
        dpi = u.GetDpiForWindow(hwnd) or 96
    except Exception:
        dpi = 96
    mode = "fullscreen" if fullscreen else ("maximized" if zoomed else "windowed")
    return {"mode": mode, "x": int(rc.left), "y": int(rc.top),
            "w": int(rc.right - rc.left), "h": int(rc.bottom - rc.top),
            "dpi": int(dpi)}


def track_window(proc, saved_mode: str) -> None:
    """Follow the app window: restore its last mode, keep window_state.json
    fresh, and shut everything down when the window/process disappears."""
    global _WIN_MINIMIZED
    u = _win32()
    if u is None:
        return
    pid = proc.pid if hasattr(proc, "pid") else 0
    hwnd, owned = _find_ui_window(u, pid, _monotonic() + 15)
    if not hwnd:
        # Delegated to an already-running browser instance (our launcher process
        # exits immediately) - the heartbeat watcher covers the close case.
        log("could not track the app window (browser reused?) - heartbeat close-detection only")
        return
    if saved_mode in ("maximized", "fullscreen") and not u.IsZoomed(hwnd):
        u.ShowWindow(hwnd, 3)  # SW_MAXIMIZE - some Chromium builds ignore the flag
    log("app window tracked - window position/size/mode will be remembered")
    last = None
    try:
        while True:
            time.sleep(3.0)
            if proc.poll() is not None:
                time.sleep(1.5)
                request_shutdown("app window closed (browser process exited)")
                return
            if not u.IsWindow(hwnd):
                time.sleep(1.5)
                request_shutdown("app window closed")
                return
            _WIN_MINIMIZED = bool(u.IsIconic(hwnd))
            if _WIN_MINIMIZED:
                continue  # minimized - keep the last sane geometry, beats may pause
            try:
                st = _sample_window(u, hwnd)
            except Exception:
                continue
            if st and st != last:
                save_window_state(st)
                last = st
    finally:
        _WIN_MINIMIZED = False


def open_ui_when_ready() -> None:
    url = f"http://{HOST}:{PORT}/"
    deadline = time.time() + 20
    while time.time() < deadline:
        if _tcp_alive(HOST, PORT, timeout=0.5):
            break
        time.sleep(0.25)
    if setting("AUTO_OPEN", "1") != "1":
        log(f"UI ready at {url} (AUTO_OPEN=0 - not opening anything)")
        return
    # Preferred: a standalone desktop window via Chrome/Edge --app mode.
    if setting("UI_WINDOW", "1") == "1":
        exe = find_app_browser()
        if exe is not None:
            try:
                st = load_window_state()
                mode = str(st.get("mode") or "windowed")
                args = [str(exe), f"--app={url}"]
                if mode == "maximized":
                    args.append("--start-maximized")
                elif mode == "fullscreen":
                    args.append("--start-fullscreen")
                else:
                    mode = "windowed"
                    w, h = int(st.get("w") or 0), int(st.get("h") or 0)
                    x, y = int(st.get("x") or 0), int(st.get("y") or 0)
                    dpi = int(st.get("dpi") or 96) or 96
                    if 400 <= w <= 12000 and 300 <= h <= 8000:
                        # Win32 reports physical px; Chromium flags want DIPs.
                        scale = 96.0 / dpi
                        args.append(f"--window-size={round(w * scale)},{round(h * scale)}")
                        if -w + 100 <= x <= 20000 and -40 <= y <= 20000:
                            args.append(f"--window-position={round(x * scale)},{round(y * scale)}")
                    else:
                        args.append("--window-size=1240,860")
                if os.name == "nt":
                    proc = subprocess.Popen(args, close_fds=True,
                                            creationflags=0x08000000)  # CREATE_NO_WINDOW
                else:
                    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL,
                                            start_new_session=True)
                log(f"opened standalone app window via {exe.name} at {url} "
                    f"(mode: {mode})")
                if mode == "windowed" and len(args) == 3:
                    log("(first run - default size; the window you arrange is "
                        "remembered for next time)")
                log("(set UI_WINDOW=0 to use a normal browser tab instead)")
                if os.name == "nt":
                    threading.Thread(target=track_window, args=(proc, mode),
                                     daemon=True, name="win-track").start()
                return
            except Exception as exc:
                log(f"could not launch the app window ({exc}) - using the browser")
        else:
            log("no Chrome/Edge found for a standalone window - using the browser")
    try:
        if os.name == "nt":
            os.startfile(url)
        else:
            import webbrowser

            webbrowser.open(url)
        log(f"opened {url} in your browser (set AUTO_OPEN=0 to disable)")
    except Exception as exc:
        log(f"could not auto-open browser ({exc}) - open {url} manually")


def build_app() -> web.Application:
    app = web.Application(middlewares=[log_and_nocache])
    app.router.add_get("/", index)
    app.router.add_get("/livekit-client.js", lib)
    app.router.add_static("/static/", path=str(WEB_DIR))
    app.router.add_get("/health", health)
    app.router.add_get("/token", token)
    app.router.add_get("/api/config", api_config_get)
    app.router.add_post("/api/config", api_config_post)
    app.router.add_get("/api/models/stt", api_models_stt)
    app.router.add_get("/api/models/tts", api_models_tts)
    app.router.add_post("/api/llm/models", api_llm_models)
    app.router.add_get("/api/chat/sessions", api_chat_sessions)
    app.router.add_get("/api/chat/session", api_chat_session)
    app.router.add_post("/api/chat/clear", api_chat_clear)
    app.router.add_post("/api/heartbeat", api_heartbeat)
    app.router.add_post("/api/window/closing", api_window_closing)
    app.router.add_get("/favicon.ico", favicon)
    return app


def main() -> None:
    global JOB, _K32
    _harden_streams()
    if not venv_python().exists():
        raise SystemExit("[  sup] .venv missing - run setup.bat (Windows) or setup.sh (Linux/macOS) first")
    if not AGENT_SCRIPT.is_file():
        raise SystemExit(f"[  sup] {AGENT_SCRIPT.name} missing - re-extract the kit")
    if not (WEB_DIR / "index.html").is_file():
        log("WARNING: python/web/index.html missing - UI will 404")

    JOB = setup_job_object()
    _K32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None

    agent_python = venv_python() if venv_python().exists() else Path(sys.executable)
    _RESTART["lk"] = 3
    _RESTART["agent"] = 3
    if LIVEKIT_EXE.exists():
        spawn("lk", [LIVEKIT_EXE, "--config", LIVEKIT_YAML])
    else:
        log("bin/livekit-server.exe missing - run setup.bat")
    spawn("agent", [agent_python, "-u", AGENT_SCRIPT, "dev"])

    try:
        banner()
    except Exception as exc:
        log(f"startup banner check failed ({exc}) - continuing")

    log("dispatch: automatic - the agent worker joins every room (no yaml or token setup)")
    if setting("CLOSE_WITH_WINDOW", "1") == "1":
        log("window close    : closing the app window (or its tab) stops everything too")
        log("                  (set CLOSE_WITH_WINDOW=0 in user_data/.env.local to disable)")
    if setting("AUTO_OPEN", "1") == "1":
        threading.Thread(target=open_ui_when_ready, daemon=True,
                         name="ui-open").start()
    threading.Thread(target=heartbeat_watcher, daemon=True,
                     name="hb-watch").start()

    app = build_app()
    try:
        web.run_app(app, host=HOST, port=PORT, print=None)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        log(f"CANNOT BIND {HOST}:{PORT} ({exc})")
        log("FIX -> is another start.bat window open? Close it, or free port 8090.")
        sys.exit(1)
    finally:
        stop_children()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        stop_children()
        sys.exit(0)
