# Local Voice Assistant Kit

A realtime voice assistant you run **yourself**. Talk to it out loud, it listens, thinks, and answers with a natural voice — all in one window. Speech-to-text and text-to-speech always run locally on your machine. For the "brain" you choose: keep it 100% local, or plug in any OpenAI-compatible cloud API. Runs on **Windows 11/10, Linux, and macOS**.

```
browser mic -> LiveKit -> Silero VAD -> faster-whisper STT
    -> LLM API (localhost:5000, LM Studio, Ollama, ... or any cloud /v1)
    -> Piper TTS -> LiveKit -> browser speakers
```

- **STT**: faster-whisper (GPU via CUDA, or CPU)
- **LLM**: any OpenAI-compatible chat API — local (textgen / LM Studio / Ollama / llama.cpp) or cloud (OpenAI / Groq / OpenRouter / ...)
- **TTS**: Piper (offline, CPU, natural voices)
- **UI**: minimalist control page — live AI/mic audio visualization, conversation panel with text chat, full settings behind the gear icon, browsable chat history you can reopen

---

## 1. Requirements

| What | Minimum | Notes |
|---|---|---|
| OS | Windows 10/11 64-bit, Linux (Ubuntu 22.04+, Debian 12+, Fedora, Arch), macOS 12+ (Intel or Apple Silicon) | |
| Python | **3.11 or 3.12** (3.13 usually works) | Windows: check *"Add python.exe to PATH"* in the installer. Linux: needs the `python3-venv`/`python3-pip` packages. |
| RAM | 8 GB recommended | Whisper + the browser both use RAM. |
| Disk | 2–6 GB free | Whisper model (80 MB – 3 GB) + Piper voice (~60 MB) + packages. |
| GPU | Optional | NVIDIA card speeds up Whisper a lot (CUDA is set up automatically). CPU is fine for `tiny`/`base`/`small` models. macOS runs Whisper on CPU. |
| An LLM endpoint | Anything that speaks the OpenAI chat API | Local: textgen, LM Studio, Ollama, llama.cpp-server — no API key needed. Cloud: OpenAI, Groq, OpenRouter — set your key. |
| Browser | Chrome, Edge, or Firefox | Microphone permission required. |
| Free ports | 8090, 7880, 7881, 50000–50100, 5000 | 5000 only if you run textgen locally. |
| Internet | Only for setup downloads | Models and the LiveKit server binary. Also if you use a cloud LLM. |

---

## 2. Setup — Windows 11 / 10

1. **Install Python** 3.11 or 3.12 from [python.org](https://www.python.org/downloads/). On the first installer screen, tick **"Add python.exe to PATH"** — this is the step people most often miss.
2. **Extract the kit** somewhere safe. A path without spaces (e.g. `C:\voice-assistant`) is the least surprising.
3. **Double-click `setup.bat`.** It walks through everything:
   - creates `.venv` and installs all Python packages,
   - downloads the LiveKit server,
   - asks for your LLM API URL + key (Enter accepts the local defaults),
   - lets you pick a Whisper model (start with `small` — good accuracy, fits a 6 GB GPU) and a Piper voice,
   - tests your GPU and pre-downloads the chosen model.
   Downloads are **resumable**: if one stalls, press Ctrl+C and run `setup.bat` again — it continues where it stopped.
4. **Start your LLM** and load a model:
   - *textgen (oobabooga)*: enable the API (`--api` / `--api-key sk-1234` in `user_data\CMD_FLAGS.txt`),
   - *LM Studio*: Developer tab → Start Server (port 1234),
   - *Ollama*: `ollama serve` (port 11434).
5. **Double-click `start.bat`.** One console window supervises everything (LiveKit + agent + web UI) and opens `http://127.0.0.1:8090` automatically (set `AUTO_OPEN=0` in `user_data/.env.local` to disable).
6. Press the **round play button** at the bottom center and just talk. It turns into a red stop button while the call is live.

> Windows microphone privacy: if the mic never picks up sound, open **Settings → Privacy & security → Microphone** and allow desktop apps and your browser.

## 3. Setup — Linux (Ubuntu / Debian / Fedora / Arch)

```bash
# Ubuntu / Debian prerequisites (one time):
sudo apt install python3 python3-venv python3-pip
# Fedora: sudo dnf install python3      Arch: sudo pacman -S python

cd local-voice-assistant
chmod +x setup.sh start.sh
./setup.sh          # same wizard as Windows: venv, LiveKit server, models, config
./start.sh          # one window: LiveKit + agent + web UI on http://127.0.0.1:8090
```

- `setup.sh` downloads the correct LiveKit binary for your architecture (x86_64 / aarch64) automatically.
- **NVIDIA GPU**: install the driver first (`nvidia-smi` must work). The CUDA runtime libraries are installed by setup automatically.
- **Headless server?** The UI needs a browser — run it on your desktop, or open `http://<server-ip>:8090` from another machine (change `bind_addresses` in `user_data/livekit.yaml` and `LIVEKIT_URL` in `user_data/.env.local` accordingly; this exposes LiveKit on the network, so do it only on a trusted LAN).
- Firewalls usually don't block loopback; if you browse from another device, open ports 8090, 7880, 7881, 50000–50100.

## 4. Setup — macOS (Intel & Apple Silicon)

```bash
brew install python@3.12     # or use the python.org installer

cd local-voice-assistant
chmod +x setup.sh start.sh
./setup.sh
./start.sh
```

- Whisper runs on **CPU** (CUDA is not available on macOS) — pick `tiny.en`, `base.en`, or `small` for responsive turns; `large-v3` will feel sluggish.
- The **first time**, macOS asks for microphone permission for your browser — allow it (System Settings → Privacy & Security → Microphone if you missed the prompt).
- Piper and faster-whisper ship macOS wheels; if `pip` can't find a wheel for your exact setup, open an issue.

---

## 5. Point it at an LLM (local or cloud)

The assistant talks to **any OpenAI-compatible API**. Configure it in the web UI: **gear icon → Connection → Language model**, then *Save changes* (applies to the next conversation).

### Local (private by default)

| Server | Base URL | API key |
|---|---|---|
| textgen (oobabooga) | `http://127.0.0.1:5000/v1` | your `--api-key` (default `sk-1234`) |
| LM Studio | `http://127.0.0.1:1234/v1` | leave empty |
| Ollama | `http://127.0.0.1:11434/v1` | leave empty |
| llama.cpp server | `http://127.0.0.1:8080/v1` | leave empty |

If your server has no authentication, **leave the key empty** — the kit detects that and sends no Authorization header. Model name: local servers usually ignore it (`local-model` is fine); Ollama wants the model tag (`llama3.1`, `qwen2.5:7b`, ...).

### Cloud (works exactly the same way)

| Provider | Base URL | Example model |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` |
| OpenRouter | `https://openrouter.ai/api/v1` | `meta-llama/llama-3.3-70b-instruct:free` |
| Together / Fireworks / vLLM | `https://.../v1` | per their docs |

Enter the API key for that provider and a real model name. **The model picker** (gear icon → Connection → *Model picker*) fetches the live model list from whatever base URL you configured — localhost or cloud — and auto-selects your last used model, or a sensible default (on OpenRouter it prefers a `:free` model). You can still type any model id manually.

**OpenRouter notes:**

- Model ids look like `vendor/model:variant` — e.g. `meta-llama/llama-3.3-70b-instruct:free`, `openai/gpt-4o-mini`, `deepseek/deepseek-chat-v3.1:free`. There is **no** model called `openrouter/free`; the agent log warns `model '...' is NOT in this provider's model list` when the id does not exist.
- The `https://openrouter.ai/api/v1` endpoint is checked with your key; if the key is wrong the launcher banner and the agent log say `auth failed`.
- Free (`:free`) models are rate-limited by OpenRouter; if replies stall, pick a paid or a less busy free model.

**Privacy note:** with a cloud LLM, your *conversation text* goes to that provider; your voice still never leaves the machine (STT and TTS stay local).

`LLM streaming` (gear icon): *Full reply first* is the most stable mode; *Stream tokens* shows the text live while it is generated.

---

## 6. Using the web page

- **Standalone window**: on start the UI opens as its **own desktop window** (no browser tabs or address bar) via your **default browser** in app mode (Brave, Chrome, Edge, Vivaldi, Opera — any Chromium browser). Prefer a normal browser tab? Set `UI_WINDOW=0` in `user_data/.env.local`, or turn off all auto-opening with `AUTO_OPEN=0`.
- **Choose the browser**: the app window opens in the browser Windows already uses for links. Want Brave or Chrome specifically? Gear icon → **Advanced → Browser for the app window** (lists every installed browser, default marked), or set `UI_BROWSER=brave` (also `chrome`, `edge`, `vivaldi`, `opera`, `chromium`, `firefox`, or a full path to an .exe) in `user_data/.env.local`. Applies on the next `start.bat`.
- **The app window is remembered**: position, size, and whether it was windowed, maximized, or fullscreen are saved to `user_data/window_state.json` and restored on the next start. Arrange the window once — every future launch comes back the way you left it.
- **Closing the app window closes everything — instantly**: the window is tracked directly (not just by heartbeat); within about a second of clicking X the launcher stops LiveKit + the agent and the console window closes too. Set `CLOSE_WITH_WINDOW=0` in `user_data/.env.local` to keep the supervisor running instead.
- **Closing the terminal closes the app window too**: the browser is bound to the launcher's process tree, so killing the console (or Ctrl+C in it) takes the app window with it — nothing lingers.
- **Conversation panel (left)**: shows your messages and the assistant's answers as they happen; typed input is optional. Open/close it with the tab on the left edge — the panel remembers whether it was open or closed the next time you load the page.
- **The panel is your full chat history**: the assistant keeps one chat log per browser (room). Every finalized user message and reply — even an interrupted turn's partial reply, or an explicit error note when the LLM could not answer — is written to the `.jsonl` immediately (flushed to disk). Restart the app or reload the page and the same conversation continues — past messages are loaded back into the panel automatically (older ones stream in as you scroll up, so huge histories stay fast). Small `— date —` dividers show where one sitting ended and the next began.
- **Refresh button** (panel header): reloads the conversation straight from the saved `.jsonl` on disk — useful if you edited/cleared the file, suspect something is missing, or just came back from the history view. **Clear button**: asks for confirmation, then empties the panel **and** erases this conversation's saved chat log. **New button**: starts a fresh conversation (new log file) with the next call — the old one stays browsable in Chat history.
- **Chat history** (clock icon, top right): browse past conversations stored in `user_data/chat/`. Click one and it **opens in the conversation panel** so you can go back to it; the header shows an orange `history ✕` chip — click it to return to the live view. Starting a new call also returns you to the live view.
- **Center orb** reacts to the assistant's voice and rotates gently during a call; while no call is running it stays calm (no spinning, just a subtle breath). The bar above the call button shows your mic level.
- **Status card** (top left): dot + word (ready / connecting / live — listening / assistant speaking / server down), mic state, and service chips (livekit, llm, GPU, STT/TTS models, agent) refreshed every 15 s.
- **Call button preflight**: before connecting it checks LiveKit, the mic, and the library; warnings appear for a down LLM API or missing models instead of a silent stall.
- **Settings** (gear icon) tabs cover every key: LiveKit URL/key/secret, LLM API URL/key/model (+ **model picker** that lists the provider's real model ids), streaming mode, microphone device, Whisper model/device/compute/language/beam/temperature/filters/**initial prompt**, **microphone capture processing** (echo cancel / noise suppress / auto gain), Piper voice, TTS speed, turn-taking timing, echo guard, **app-window browser + profile**, log level. Saving applies to the **next** conversation.
- **Everything is selectable**: conversation text, history, settings hints — select and Ctrl+C anywhere.

## 7. Everyday use

- Change Whisper model / TTS voice / LLM endpoint without touching files: gear icon in the web page.
- Drop a CTranslate2 Whisper model into `user_data/models/whisper/<name>/` and it appears in the picker. Same for Piper voices in `user_data/models/piper/`.
- Conversation history lands in `user_data/chat/<date>-<room>.jsonl` (one JSON object per line).
- `scripts/setup_models.bat` (Windows) or `python python/setup_wizard.py --models-only` re-opens only the model download steps.
- `scripts/start_livekit.bat` runs only the LiveKit server for debugging.
- `.venv/` and `bin/` are generated by setup and can be deleted anytime — setup recreates them.

| Path | Purpose |
|---|---|
| `setup.bat` / `setup.sh` | One-time setup wizard (venv, packages, LiveKit server, models, config) |
| `start.bat` / `start.sh` | Everyday launcher — one window starts LiveKit + agent + web UI |
| `python/` | `server.py` (supervisor + web API), `agent.py` (voice pipeline), `local_stt.py` / `local_llm.py` / `local_tts.py` (adapters), `setup_wizard.py`, `web/` (UI) |
| `python/web/` | UI assets: `index.html` (markup), `app.css` (styles), `js/` — `core`, `chat` (windowed transcript + history), `audio` (orb/visuals), `settings`, `call`, `main` |
| `scripts/` | Helper bats: re-download models, run only the LiveKit server |
| `user_data/` | Yours to keep: `.env.local` (settings), `livekit.yaml`, `models/whisper/`, `models/piper/`, `chat/` (history), `window_state.json` (remembered app window), `browser-profile/` (the app window's own browser profile) |

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| Setup download stalls | Press Ctrl+C, run it again — it resumes. Mirror fallback is automatic. |
| `Python was not found in PATH` (Windows) | Reinstall Python and tick "Add python.exe to PATH", or install from the Microsoft Store and re-open the terminal. |
| `python3-venv` error (Linux) | `sudo apt install python3-venv python3-pip`, then run `./setup.sh` again. |
| Whisper shows `cpu` although you have a GPU | Gear icon → Device = `cuda`. If cuDNN errors appear, re-run setup step 1 (installs nvidia-cudnn-cu12). |
| Agent log: `LLM API rejected the key (HTTP 401)` | Gear icon → API key must match what your provider expects. Save; applies to the next conversation. |
| Agent log: `model '...' is NOT in this provider's model list` | The model id does not exist at that provider (e.g. `openrouter/free`). On **local** servers (textgen, LM Studio, llama.cpp) the agent now auto-uses the server's loaded model for the session; pick a real id in the Model picker to make it permanent. |
| Agent log: `LLM GENERATION FAILED (HTTP 500)` at startup | `/models` answered but a real generation failed — read the textgen/LM Studio console. With oobabooga: make sure a model is actually loaded in the Models tab and its llama.cpp backend did not crash. |
| Agent log: `textgen request failed: ...` mid-conversation | The LLM backend dropped the request (the bot says so out loud, and the chat log records `llm error - this turn was not answered`). Check the LLM server's console. If it shares the GPU with Whisper it may have run out of VRAM once Whisper loaded — lower its context size, or set `WHISPER_DEVICE=cpu`. One-off failures are retried automatically. |
| Agent log: `Config WHISPER_...='' is not a valid number` | Old saved settings had an empty numeric field. Fixed automatically (defaults are used); re-save your settings from the gear icon to clean the file. |
| Agent log: `LLM API NOT reachable` | Local: start textgen / LM Studio / Ollama with the API enabled. Cloud: re-check URL, key, model name. The agent retries by itself. |
| Agent hears itself / answers its own voice | Keep Echo guard ON (gear icon). Headphones avoid the issue entirely. |
| Text chat works but the bot never hears my voice | Status card says `mic on` but silent? Pick another input device (gear icon → Connection → Microphone, applies to the next call). Windows: Settings → Privacy & security → Microphone → allow desktop apps. macOS: System Settings → Privacy & Security → Microphone → allow your browser. A red `Mic is connected but silent` toast means the browser delivers no signal. |
| Text replies but no sound | Check the Piper voice exists in `user_data/models/piper/` (`<name>.onnx` + `<name>.onnx.json`). |
| `large-v3-turbo` / `large-v3` feels slow | On a 6 GB GPU (or any CPU) use `small` or `medium` — big models trade speed for accuracy. |
| Nothing responds at all | In the launcher window: `LLM API: up` and `LiveKit -> UP` must both show. Fix whichever is down first. |
| `could not parse config` in the log | The supervisor rewrites `user_data/livekit.yaml` automatically (old copy saved as `livekit.yaml.broken`). |
| STT is inaccurate with every model size | 1) Set **Language** explicitly (auto-detect misfires on short sentences). 2) Try **Noise suppress = Off** (and Auto gain = Off) — the browser's audio processing can mangle speech before Whisper sees it (gear icon → Speech → Microphone capture processing). 3) Add an **Initial prompt** with the names/jargon you say often. 4) Keep **VAD filter On** so breaths and clicks don't become phantom words. 5) `small`/`large-v3-turbo` with beam 1 is fast; beam 3–5 buys a little accuracy. |
| Agent never joins the room | The `agent` child must show `up` in the status card. Dispatch is automatic — the agent joins every room on this LiveKit server. Do not set `LIVEKIT_AGENT_NAME`; it switches the worker to named-only dispatch. |
| Port 8090 already in use | Another launcher window is open (or something else uses 8090). Close it or free the port. |

## 9. Ports

| Port | Service |
|---|---|
| 8090 | Web UI + API (this kit) |
| 7880 | LiveKit signaling |
| 7881, 50000–50100 | LiveKit RTC |
| 5000 | textgen API, if used (URL must end in `/v1`) |
| 1234 / 11434 / 8080 | LM Studio / Ollama / llama.cpp, if used |

## 10. Privacy

Default setup: every component runs on your machine — browser, LiveKit, Whisper, Piper, and your local LLM. No telemetry, no cloud calls. If you configure a **cloud LLM provider**, only the conversation *text* is sent to that provider (this is exactly like chatting on their website); your audio, STT, and TTS still never leave your machine.

## 11. Credits

This kit only exists because a lot of people chose to share their work. Thank you!

**Core building blocks**

| Project | Role in this kit | License |
|---|---|---|
| [LiveKit](https://github.com/livekit/livekit) | Local realtime media server (WebRTC SFU) + [livekit-agents](https://github.com/livekit/agents) Python SDK | Apache-2.0 |
| [livekit-client (JS)](https://github.com/livekit/client-sdk-js) | Browser side of the voice loop | Apache-2.0 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) + [CTranslate2](https://github.com/OpenNMT/CTranslate2) | Fast local Whisper inference | MIT |
| [Whisper](https://github.com/openai/whisper) (OpenAI) | The speech recognition models themselves (incl. `large-v3-turbo`) | MIT |
| [Silero VAD](https://github.com/snakers4/silero-vad) | Voice activity detection / turn taking | MIT |
| [Piper](https://github.com/rhasspy/piper) (Rhasspy / OHF-voice) | Offline neural text-to-speech | MIT |
| Piper voices | e.g. `en_US-amy-medium`, `en_US-lessac-medium` — by the [Piper voices](https://github.com/rhasspy/piper/blob/master/VOICES.md) community | per-voice (CC0/CC-BY) |
| [aiohttp](https://github.com/aio-libs/aiohttp) | Supervisor web server + API | Apache-2.0 |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | Settings file handling | BSD-3-Clause |
| [NVIDIA CUDA wheels](https://pypi.org/project/nvidia-cudnn-cu12/) (`nvidia-cudnn-cu12`, `nvidia-cublas-cu12`) | GPU acceleration for Whisper on Windows/Linux | NVIDIA license |

**Also thanks to**

- **Hugging Face Hub** — hosts the Whisper model downloads (with mirror fallback).
- **OpenRouter, Groq, OpenAI** and every OpenAI-compatible server project — **LM Studio**, **Ollama**, **llama.cpp**, **oobabooga text-generation-webui** — for the "brain" options, local or cloud.
- The **Python** and **Chromium** projects, which quietly carry the rest of the stack.
- Everyone who filed issues and tested dev builds of this kit.

If you are an author of one of the projects above and want the credit entry changed or removed, please open an issue.

