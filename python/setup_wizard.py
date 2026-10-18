"""One-time setup wizard for the local voice assistant kit.

Steps: deps (venv + packages), LiveKit server, config, Whisper model,
Piper voice. Downloads are resumable (.part files, mirror fallback).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
BIN_DIR = ROOT / "bin"
USER_DATA = ROOT / "user_data"
ENV_FILE = USER_DATA / ".env.local"
YAML_FILE = USER_DATA / "livekit.yaml"
WHISPER_BASE = USER_DATA / "models" / "whisper"
PIPER_BASE = USER_DATA / "models" / "piper"

DRY_RUN = False
AUTO = False

LIVEKIT_VERSION = "1.13.10"
LIVEKIT_URL = (
    "https://github.com/livekit/livekit/releases/download/v"
    + LIVEKIT_VERSION
    + "/livekit_"
    + LIVEKIT_VERSION
    + "_windows_amd64.zip"
)

WHISPER_SIZES_MB = {
    "tiny.en": 80, "base.en": 150, "small.en": 500, "small": 500,
    "medium.en": 1530, "medium": 1530, "large-v3-turbo": 1620, "large-v3": 3100,
}
WHISPER_ORDER = ["tiny.en", "base.en", "small.en", "small", "medium.en",
                 "medium", "large-v3-turbo", "large-v3"]
WHISPER_REPOS = {"large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo"}

PIPER_VOICES = [
    ("en_US-amy-medium", "en/en_US/amy/medium"),
    ("en_US-lessac-medium", "en/en_US/lessac/medium"),
    ("en_US-ryan-high", "en/en_US/ryan/high"),
    ("en_GB-alba-medium", "en/en_GB/alba/medium"),
]
PIPER_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"


def hr(title: str) -> None:
    print(f"\n==================== {title} ====================")


def ask(prompt: str, default: str = "") -> str:
    if AUTO:
        return default
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def venv_python() -> Path:
    return VENV_DIR / "Scripts" / "python.exe"


def run(cmd: list, **kw) -> int:
    print("+ " + " ".join(str(c) for c in cmd))
    if DRY_RUN:
        return 0
    return subprocess.run([str(c) for c in cmd], **kw).returncode


def write_env(updates: dict) -> None:
    if DRY_RUN:
        print(f"(dry-run) would update {ENV_FILE.name}: {updates}")
        return
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    cfg.update(updates)
    lines = [
        "# Local Voice Assistant settings",
        "# Edit with Notepad, or use the web UI gear icon.",
        "",
        "# ---- LiveKit server (must match livekit.yaml keys) ----",
        f"LIVEKIT_URL={cfg.get('LIVEKIT_URL', 'ws://127.0.0.1:7880')}",
        f"LIVEKIT_API_KEY={cfg.get('LIVEKIT_API_KEY', 'devkey')}",
        f"LIVEKIT_API_SECRET={cfg.get('LIVEKIT_API_SECRET', 'voicelocal-secret-1234567890abcdef')}",
        "",
        "# ---- LLM API (OpenAI-compatible, localhost) ----",
        "# textgen (oobabooga): http://127.0.0.1:5000/v1",
        f"TEXTGEN_BASE_URL={cfg.get('TEXTGEN_BASE_URL', 'http://127.0.0.1:5000/v1')}",
        "# Must match the --api-key set in textgen (user_data\\CMD_FLAGS.txt)",
        f"TEXTGEN_API_KEY={cfg.get('TEXTGEN_API_KEY', 'sk-1234')}",
        f"TEXTGEN_MODEL={cfg.get('TEXTGEN_MODEL', 'local-model')}",
        "# 1 = stream tokens (faster first text), 0 = full reply first",
        f"LLM_STREAMING={cfg.get('LLM_STREAMING', '0')}",
        "",
        "# ---- Whisper STT ----",
        f"WHISPER_MODEL={cfg.get('WHISPER_MODEL', 'small')}",
        f"WHISPER_DEVICE={cfg.get('WHISPER_DEVICE', 'auto')}",
        f"WHISPER_COMPUTE={cfg.get('WHISPER_COMPUTE', 'auto')}",
        f"WHISPER_LANGUAGE={cfg.get('WHISPER_LANGUAGE', 'en')}",
        "",
        "# ---- Piper TTS ----",
        f"TTS_VOICE={cfg.get('TTS_VOICE', 'en_US-amy-medium.onnx')}",
        f"TTS_SPEED={cfg.get('TTS_SPEED', '1.1')}",
        "",
        "# ---- Voice activity / turn taking ----",
        f"VAD_MIN_SILENCE={cfg.get('VAD_MIN_SILENCE', '0.55')}",
        f"VAD_ACTIVATION_THRESHOLD={cfg.get('VAD_ACTIVATION_THRESHOLD', '0.45')}",
        f"ENDPOINTING_DELAY={cfg.get('ENDPOINTING_DELAY', '0.7')}",
        f"ECHO_GUARD={cfg.get('ECHO_GUARD', '1')}",
        f"ECHO_GUARD_TAIL={cfg.get('ECHO_GUARD_TAIL', '0.8')}",
    ]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"saved {ENV_FILE}")


def write_livekit_yaml() -> None:
    if DRY_RUN:
        print(f"(dry-run) would write {YAML_FILE}")
        return
    YAML_FILE.parent.mkdir(parents=True, exist_ok=True)
    YAML_FILE.write_text(
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
        "  devkey: voicelocal-secret-1234567890abcdef\n",
        encoding="utf-8",
    )
    print(f"saved {YAML_FILE}")


def download_resumable(url: str, dest: Path, desc: str, min_bytes: int = 100,
                       attempts: int = 4) -> None:
    if DRY_RUN:
        print(f"  (dry-run) would download {desc} -> {dest.name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    got = part.stat().st_size if part.exists() else 0
    if dest.exists() and dest.stat().st_size >= min_bytes:
        print(f"  {desc}: already downloaded ({dest.stat().st_size / 1e6:.1f} MB)")
        return
    headers = {"User-Agent": "voice-kit-setup/1"}
    if got:
        headers["Range"] = f"bytes={got}-"
        print(f"  {desc}: resuming at {got / 1e6:.1f} MB")
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if got and e.code in (416, 400):
            part.unlink(missing_ok=True)
            return download_resumable(url, dest, desc, min_bytes, attempts)
        raise
    total = resp.headers.get("Content-Length")
    total = int(total) + got if total else None
    mode = "ab" if got else "wb"
    start = time.time()
    written = got
    try:
        with open(part, mode) as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                if total:
                    pct = written * 100 // total
                    speed = written / max(0.1, time.time() - start) / 1e6
                    print(f"\r  {desc}: {written / 1e6:.1f} / {total / 1e6:.1f} MB "
                          f"({pct}%) {speed:.1f} MB/s", end="", flush=True)
        print()
    except (socket_timeout(), ConnectionError, OSError) as exc:
        if attempts > 1:
            print(f"\n  {desc}: interrupted ({exc}) - retrying with resume...")
            time.sleep(2)
            return download_resumable(url, dest, desc, min_bytes, attempts - 1)
        raise
    if total and written < total:
        if attempts > 1:
            print(f"\n  {desc}: short transfer ({written} < {total}) - retrying...")
            time.sleep(2)
            return download_resumable(url, dest, desc, min_bytes, attempts - 1)
        part.unlink(missing_ok=True)
        raise IOError(f"{desc}: truncated download")
    part.replace(dest)
    print(f"  {desc}: done -> {dest.name}")


def socket_timeout():
    import socket
    return socket.timeout


def hf_list_files(base: str, repo: str) -> list:
    url = f"{base}/api/models/{repo}/tree/main?recursive=true"
    with urllib.request.urlopen(urllib.request.Request(
            url, headers={"User-Agent": "voice-kit-setup/1"}), timeout=30) as r:
        tree = json.loads(r.read().decode("utf-8"))
    return [f for f in tree if f.get("type") == "file"]


def pick_hf_endpoint(repo: str) -> str:
    primary = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    mirror = "https://hf-mirror.com"
    for base in (primary, mirror):
        try:
            files = hf_list_files(base, repo)
            if files:
                print(f"  repo OK at {base} ({len(files)} files)")
                return base
        except Exception as exc:
            print(f"  {base} failed ({exc})")
    raise SystemExit("Could not reach HuggingFace or the mirror - check your internet.")


def whisper_repo(model: str) -> str:
    return WHISPER_REPOS.get(model, f"Systran/faster-whisper-{model}")


def step_deps() -> None:
    hr("STEP 1] Python venv + packages")
    if not VENV_DIR.exists():
        run([sys.executable, "-m", "venv", VENV_DIR])
    py = venv_python()
    if DRY_RUN:
        print("(dry-run) pip installs skipped")
        return
    run([py, "-m", "pip", "install", "--upgrade", "pip"])
    pkgs = [
        "livekit-agents~=1.8", "livekit-plugins-openai~=1.8",
        "livekit-plugins-silero~=1.8", "faster-whisper", "piper-tts",
        "python-dotenv", "aiohttp",
    ]
    code = run([py, "-m", "pip", "install", *pkgs])
    if code != 0:
        raise SystemExit("Package installation failed - check your internet and retry.")
    run([py, "-m", "pip", "install", "nvidia-cublas-cu12", "nvidia-cudnn-cu12"])


def step_livekit() -> None:
    hr("STEP 2] LiveKit server")
    exe = BIN_DIR / "livekit-server.exe"
    if exe.exists() and exe.stat().st_size > 10_000_000:
        print(f"  already present: {exe}")
        return
    zpath = BIN_DIR / "livekit.zip"
    download_resumable(LIVEKIT_URL, zpath, "LiveKit server", min_bytes=1_000_000)
    if DRY_RUN:
        return
    with zipfile.ZipFile(zpath) as z:
        z.extractall(BIN_DIR)
    zpath.unlink(missing_ok=True)
    print(f"  extracted -> {exe}")



def step_config() -> None:
    hr("STEP 3] Configuration")
    write_livekit_yaml()
    key = ask("textgen API key (from --api-key in CMD_FLAGS.txt)", "sk-1234")
    base = ask("textgen API URL", "http://127.0.0.1:5000/v1")
    write_env({"TEXTGEN_API_KEY": key, "TEXTGEN_BASE_URL": base})
    print("  You can change every setting later in the web UI (gear icon).")


_GPU_PROBE_CODE = r"""
import json, os, site, sys
from pathlib import Path
for sp in site.getsitepackages():
    for sub in ("cudnn", "cublas"):
        d = Path(sp) / "nvidia" / sub / "bin"
        if d.is_dir():
            os.add_dll_directory(str(d))
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
out = {"cuda_devices": 0, "types": [], "error": ""}
try:
    import ctranslate2
    out["cuda_devices"] = ctranslate2.get_cuda_device_count()
    if out["cuda_devices"]:
        out["types"] = list(ctranslate2.get_supported_compute_types("cuda"))
except Exception as e:
    out["error"] = f"ctranslate2: {e}"
print("PROBE_JSON:" + json.dumps(out), flush=True)
sys.exit(0)
"""


def probe_gpu(timeout: int = 300) -> dict:
    if DRY_RUN:
        return {"cuda_devices": 1, "types": ["float16"], "load_ok": True, "error": ""}
    py = venv_python()
    if not py.exists():
        return {"cuda_devices": 0, "types": [], "load_ok": False,
                "error": "venv not built yet"}
    code = _GPU_PROBE_CODE
    try:
        r = subprocess.run(
            [str(py), "-c", code], capture_output=True, text=True,
            timeout=timeout, cwd=str(ROOT))
        for line in (r.stdout or "").splitlines():
            if line.startswith("PROBE_JSON:"):
                return json.loads(line[len("PROBE_JSON:"):])
        return {"cuda_devices": 0, "types": [], "load_ok": False,
                "error": (r.stderr or "no probe output")[-200:]}
    except subprocess.TimeoutExpired:
        return {"cuda_devices": 0, "types": [], "load_ok": False,
                "error": f"probe timed out after {timeout}s"}


def step_whisper_prefetch() -> None:
    hr("STEP 4] Whisper STT model pre-download + GPU test")
    print("  Pick the model to pre-download (speed vs quality):")
    for i, name in enumerate(WHISPER_ORDER, 1):
        mark = " (already downloaded)" if (WHISPER_BASE / name / "model.bin").is_file() else ""
        print(f"   {i}) {name:<16} ~{WHISPER_SIZES_MB[name]} MB{mark}")
    default_idx = WHISPER_ORDER.index("small") + 1
    choice = ask("Number", str(default_idx))
    try:
        model = WHISPER_ORDER[int(choice) - 1]
    except (ValueError, IndexError):
        model = "small"
    print(f"  Selected: {model}")
    write_env({"WHISPER_MODEL": model})

    if DRY_RUN:
        print("(dry-run) download skipped")
        return
    dest_dir = WHISPER_BASE / model
    bin_file = dest_dir / "model.bin"
    if bin_file.exists() and bin_file.stat().st_size > 50_000_000:
        print("  model.bin already downloaded - skipping")
    else:
        repo = whisper_repo(model)
        base = pick_hf_endpoint(repo)
        files = hf_list_files(base, repo)
        print(f"  downloading {len(files)} files from {repo} ...")
        for finfo in files:
            fpath = finfo["path"]
            url = f"{base}/{repo}/resolve/main/{fpath}"
            download_resumable(url, dest_dir / Path(fpath).name,
                               Path(fpath).name, min_bytes=100)

    probe = probe_gpu()
    if probe.get("cuda_devices"):
        print(f"  GPU: CUDA available ({probe.get('types')}) - Whisper will use it")
    else:
        print(f"  GPU: not usable ({probe.get('error', 'not found')}) - "
              "Whisper will run on CPU (fine for tiny/base/small)")
    print("  Test-loading the model (first run may take a minute)...")
    py = venv_python()
    code = (
        "import sys; sys.path.insert(0, r'%s');\n"
        "from local_stt import create_stt;\n"
        "create_stt(model=r'%s', device='auto', compute_type='auto');\n"
        "print('WHISPER LOAD TEST OK')"
    ) % (str(Path(__file__).resolve().parent), str(dest_dir))
    try:
        r = subprocess.run([str(py), "-c", code], cwd=str(ROOT), timeout=600)
        if r.returncode != 0:
            print("  Load test failed - check the error above. The kit still runs")
            print("  if you pick a smaller model via the web UI.")
    except subprocess.TimeoutExpired:
        print("  Load test timed out - try a smaller model.")


def step_piper() -> None:
    hr("STEP 5] Piper TTS voice")
    print("  Pick a voice:")
    for i, (name, _sub) in enumerate(PIPER_VOICES, 1):
        have = (PIPER_BASE / f"{name}.onnx").is_file()
        print(f"   {i}) {name}{' (already downloaded)' if have else ''}")
    choice = ask("Number", "1")
    try:
        name, sub = PIPER_VOICES[int(choice) - 1]
    except (ValueError, IndexError):
        name, sub = PIPER_VOICES[0]
    print(f"  Selected: {name}")
    write_env({"TTS_VOICE": name + ".onnx"})
    if DRY_RUN:
        return
    for suffix in (".onnx", ".onnx.json"):
        url = f"{PIPER_BASE_URL}/{sub}/{name}{suffix}"
        download_resumable(url, PIPER_BASE / f"{name}{suffix}", name + suffix)
    print("  Test-synthesizing...")
    py = venv_python()
    code = (
        "from piper import PiperVoice;\n"
        "v = PiperVoice.load(r'%s');\n"
        "list(v.synthesize('Hello, the voice works.'));\n"
        "print('PIPER TEST OK')"
    ) % str(PIPER_BASE / f"{name}.onnx")
    try:
        r = subprocess.run([str(py), "-c", code], cwd=str(ROOT), timeout=300)
        if r.returncode != 0:
            print("  Piper test failed - check the error above.")
    except subprocess.TimeoutExpired:
        print("  Piper test timed out.")


def done() -> None:
    hr("ALL DONE")
    print("  Start everything with  start.bat")
    print("  Then open  http://127.0.0.1:8090  and press Start conversation.")


def main() -> None:
    global DRY_RUN, AUTO
    flags = set(sys.argv[1:])
    DRY_RUN = "--dry-run" in flags
    AUTO = "--auto" in flags
    models_only = "--models-only" in flags

    print("=" * 60)
    print(" LOCAL VOICE ASSISTANT - setup")
    print("=" * 60)

    if models_only:
        step_whisper_prefetch()
        step_piper()
        done()
        return

    while True:
        hr("MENU")
        print("  1) Python venv + packages")
        print("  2) LiveKit server")
        print("  3) Configuration (textgen key etc.)")
        print("  4) Whisper STT model")
        print("  5) Piper TTS voice")
        print("  0) Run ALL steps, then exit")
        choice = ask("Choose", "0")
        if DRY_RUN and AUTO:
            step_deps(); step_livekit(); step_config()
            step_whisper_prefetch(); step_piper(); done()
            return
        if choice == "1":
            step_deps()
        elif choice == "2":
            step_livekit()
        elif choice == "3":
            step_config()
        elif choice == "4":
            step_whisper_prefetch()
        elif choice == "5":
            step_piper()
        elif choice == "0":
            step_deps(); step_livekit(); step_config()
            step_whisper_prefetch(); step_piper(); done()
            return


if __name__ == "__main__":
    main()
