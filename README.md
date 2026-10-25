# NEXUS

**A voice-controlled AI assistant with a cinematic holographic interface.**

NEXUS pairs the Gemini Live API with a sci-fi command-center GUI. Talk to it out loud, watch the audio-reactive hologram respond, and let it help you control your computer, all from one Python file that runs locally.

<!-- Add a screenshot or GIF here after your first push:
![NEXUS screenshot](screenshot.png)
-->

---

## Features

**Interface**
- Audio-reactive particle-core hologram
- Live CPU, RAM and network telemetry
- Interactive subsystem map and task activity feed
- Live weather panel for any city
- Several colour themes, plus reduced-motion and low-power modes

**Voice assistant**
- Real-time voice conversation using the Gemini Live API
- Six selectable voices: Charon, Orus, Fenrir, Puck, Kore and Aoede
- Polished British-English concierge personality
- Text chat input alongside voice
- Personalised: NEXUS asks what to call you on first run (leave it blank and it says "sir" or "boss")

**Computer control** (each category can be switched off in Settings)
- Open apps, websites, files and folders
- Type text and press keyboard shortcuts
- Create, copy, move, rename and organise files and folders
- Volume and media controls
- Web search and system info
- WhatsApp message drafting, with a read-back and confirmation before sending

**Optional Screen Vision**
- Lets NEXUS see your screen while you share it. Off by default.

---

## Privacy and Safety

- **Your API key is never stored in the code.** You paste it into the Settings panel, and it is saved in a private file in your user folder (`~/.nexus/gemini_config.json`). No environment variables are used.
- **Everything runs locally.** The GUI is served from your own computer at `127.0.0.1` and only accepts requests from its own page.
- **Dangerous actions are off by default.** Deleting files, permanent deletion and Recycle Bin cleanup are disabled until you turn them on. Irreversible actions need your explicit confirmation.
- **Screen Vision is off by default** and only works while you are actively sharing your screen.
- **Voice, text and screen frames are sent to Google** through the Gemini API while a session is running. Use it with that in mind.

> NEXUS can control your keyboard, apps and files. Review the permissions in Settings and only enable what you are comfortable with.

---

## Requirements

- Python 3.10 or newer
- A Gemini API key (free to create at [Google AI Studio](https://aistudio.google.com/apikey))
- Chrome or Edge, with microphone access allowed
- Windows is the best-supported platform. The interface and voice work elsewhere, but some desktop-control features (exact volume, WhatsApp, app discovery) are Windows-focused.

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>

# 2. (Recommended) create a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # macOS / Linux

# 3. Install dependencies
python -m pip install -r requirements.txt
```

## Usage

```bash
python Nexus_Complete.py
```

Your browser opens at **http://localhost:8765**.

1. On first run, paste your **Gemini API key** and (optionally) tell NEXUS what to call you.
2. Click **Start Nexus** and allow microphone access.
3. Start talking.

You can change your key, name, voice, theme and permissions any time in **Settings**.

### Example commands

- "Open Chrome and search for Python tutorials."
- "What's my CPU usage?"
- "Organise my Downloads folder."
- "Turn the volume down."
- "What's the weather like today?"

---

## Configuration

| Setting | Where | Notes |
| --- | --- | --- |
| Gemini API key | Settings panel | Saved to `~/.nexus/gemini_config.json` |
| Your name | First-run screen or Settings | Saved to `~/.nexus/profile.json` |
| Permissions | Settings panel | Saved to `~/.nexus/automation_settings.json` |
| Port | `NEXUS_PORT` environment variable | Defaults to `8765` |
| Skip auto-opening the browser | `NEXUS_NO_BROWSER=1` | Optional |

To wipe your saved data, use **Remove local key** in Settings, or delete the `~/.nexus` folder.

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| GUI opens but there is no voice | Check that `google-genai` is installed and your API key is saved |
| "Port already in use" | Close the other NEXUS window, or set `NEXUS_PORT` to a free port |
| Microphone not working | Allow mic access in your browser's site settings |
| A tool says a package is missing | Run `python -m pip install -r requirements.txt` again |
| Exact volume control fails on Windows | Install `pycaw` (it is included in `requirements.txt`) |

---

## Tech Stack

- **Backend:** Python, aiohttp, WebSockets
- **AI:** Google Gen AI SDK, Gemini Live API
- **Frontend:** vanilla HTML, CSS and JavaScript on a single canvas, with no external assets
- **Automation:** pyautogui, pyperclip, send2trash, psutil

---

## Contributing

Issues and pull requests are welcome. For larger changes, please open an issue first to discuss what you would like to change.

## License

No license has been chosen yet. Add a `LICENSE` file (for example MIT) to tell others how they may use this project.

## Disclaimer

NEXUS is an independent hobby project and is not affiliated with Google. It uses a preview Gemini model, so behaviour may change over time.
