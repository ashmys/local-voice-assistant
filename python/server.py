"""One-window supervisor + web UI/API server for the local voice assistant.

Runs the LiveKit server and the voice agent as children (killed together via
a Windows Job Object), serves python/web/index.html and the config/model/chat
history API on http://127.0.0.1:8090.
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
    name = request.query.get("file", "")
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
                if rec.get("who") in ("user", "bot") and rec.get("text"):
                    msgs.append({
                        "who": rec["who"], "text": rec["text"],
                        "ts": rec.get("ts", ""), "id": rec.get("id", ""),
                    })
    except OSError as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"file": name, "messages": msgs})


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
        "TTS_VOICE", "TTS_SPEED", "VAD_MIN_SILENCE", "VAD_ACTIVATION_THRESHOLD",
        "ENDPOINTING_DELAY", "ECHO_GUARD", "ECHO_GUARD_TAIL", "LOG_LEVEL",
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


async def token(_request: web.Request) -> web.Response:
    identity = (_request.query.get("identity") or f"user-{os.urandom(2).hex()}").strip()
    if not re.fullmatch(r"[\w-]{1,64}", identity):
        return web.json_response({"error": "bad identity"}, status=400)
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
    try:
        resp = await handler(request)
        status = resp.status
    except web.HTTPException as e:
        status = e.status
        raise
    finally:
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
                args = [str(exe), f"--app={url}", "--window-size=1240,860"]
                if os.name == "nt":
                    subprocess.Popen(args, close_fds=True,
                                     creationflags=0x08000000)  # CREATE_NO_WINDOW
                else:
                    subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     start_new_session=True)
                log(f"opened standalone app window via {exe.name} at {url}")
                log("(set UI_WINDOW=0 to use a normal browser tab instead)")
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
    if setting("AUTO_OPEN", "1") == "1":
        threading.Thread(target=open_ui_when_ready, daemon=True,
                         name="ui-open").start()

    app = web.Application(middlewares=[log_and_nocache])
    app.router.add_get("/", index)
    app.router.add_get("/livekit-client.js", lib)
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
    app.router.add_get("/favicon.ico", favicon)
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
