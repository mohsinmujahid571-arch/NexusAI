"""NEXUS Lite — cinematic holographic command center and voice assistant.

A 1280x720-first sci-fi interface with an audio-reactive particle-core hologram,
live CPU/RAM/network telemetry, an interactive subsystem map, and task activity.
Canvas rendering is frame-limited and uses a small 2D particle cloud rather than a
3D engine or remote visual assets.

Run:
    pip install -U google-genai aiohttp psutil ddgs pyautogui pyperclip send2trash
    # Optional: pip install pycaw
    # First run: enter your Gemini API key in the NEXUS setup screen.
    python nexus_reference_holographic_ui_github_ready.py

Open http://localhost:8765 in Chrome or Edge, then click Start Nexus.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import tempfile
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import get_type_hints
from urllib.parse import quote_plus

try:
    from aiohttp import web, WSMsgType, ClientSession, ClientTimeout
except ImportError as exc:  # the local web UI needs aiohttp to serve the page
    raise SystemExit(
        "NEXUS needs aiohttp to start its local GUI. Run:\n"
        "  python -m pip install -U aiohttp\n"
        f"\nMissing import: {exc}"
    ) from exc

# The visual interface should still launch if the optional Gemini SDK is absent.
# This is useful for previewing the HUD and lets the status endpoint explain how
# to enable voice, instead of the entire app exiting during import.
try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
    GENAI_IMPORT_ERROR = ""
except ImportError as exc:
    genai = None
    types = None
    GENAI_AVAILABLE = False
    GENAI_IMPORT_ERROR = str(exc)

# ---------------------------------------------------------------------------
# Core configuration
# ---------------------------------------------------------------------------
APP_NAME = "NEXUS"
GEMINI_CONFIG_PATH = Path.home() / ".nexus" / "gemini_config.json"

def _load_saved_gemini_api_key() -> str:
    """Load a user-entered key from the per-user config outside the repository."""
    try:
        payload = json.loads(GEMINI_CONFIG_PATH.read_text(encoding="utf-8"))
        value = payload.get("api_key", "") if isinstance(payload, dict) else ""
        return value.strip() if isinstance(value, str) else ""
    except (OSError, ValueError, TypeError):
        return ""

# The key only ever comes from the Settings panel (saved locally by the GUI).
# What NEXUS calls the user. Asked once on first run; blank means "sir" / "boss".
PROFILE_PATH = Path.home() / ".nexus" / "profile.json"
_PROFILE_LOCK = threading.RLock()


def _clean_user_name(value: object) -> str:
    """Validate a display name. Strict on purpose: it ends up inside the AI prompt."""
    if not isinstance(value, str):
        raise ValueError("Enter a name, or leave it blank.")
    name = re.sub(r"\s+", " ", value).strip()
    if not name:
        return ""
    if not re.fullmatch(r"[\w .'-]{1,40}", name):
        raise ValueError("Use only letters, spaces, apostrophes, hyphens or dots (max 40 characters).")
    return name


def _load_user_name() -> str:
    try:
        payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        return _clean_user_name(payload.get("user_name", "")) if isinstance(payload, dict) else ""
    except (OSError, ValueError, TypeError):
        return ""


def _save_user_name(name: str) -> None:
    with _PROFILE_LOCK:
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_PATH.write_text(json.dumps({"user_name": name}), encoding="utf-8")


USER_NAME = _load_user_name()

API_KEY = _load_saved_gemini_api_key()
API_KEY_SOURCE = "local" if API_KEY else "missing"
MODEL = "gemini-3.1-flash-live-preview"
DEFAULT_VOICE = "Charon"
ALLOWED_VOICES = {"Charon", "Orus", "Fenrir", "Puck", "Kore", "Aoede"}
PORT = int(os.environ.get("NEXUS_PORT", "8765"))
IN_RATE = 16000
OUT_RATE = 24000
GREET_ON_START = True

# These preferences are also displayed in the Settings panel. They are stored
# locally and checked by each computer-control tool before it does any work.
AUTOMATION_SETTINGS_PATH = Path.home() / ".nexus" / "automation_settings.json"
AUTOMATION_SETTINGS_DEFAULT = {
    "automation_enabled": True,
    "allow_app_control": True,
    "allow_file_operations": True,
    "allow_file_deletion": False,
    "allow_permanent_file_deletion": False,
    "allow_recycle_bin_cleanup": False,
    "allow_media_control": True,
    "allow_web_actions": True,
    "screen_vision_enabled": False,
}
_AUTOMATION_SETTINGS_LOCK = threading.RLock()


def _load_automation_settings() -> dict[str, bool]:
    values = dict(AUTOMATION_SETTINGS_DEFAULT)
    try:
        stored = json.loads(AUTOMATION_SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            for key in values:
                if type(stored.get(key)) is bool:
                    values[key] = stored[key]
    except (OSError, ValueError, TypeError):
        pass
    return values


AUTOMATION_SETTINGS = _load_automation_settings()

# Irreversible file operations are staged here. Recoverable moves to the OS
# Recycle Bin/Trash execute directly when their Settings permission is enabled.
_PENDING_FILE_ACTIONS: dict[str, dict[str, str]] = {}
_PENDING_FILE_ACTIONS_LOCK = threading.RLock()


def _automation_guard(feature: str) -> str | None:
    """Return an explanatory block message when a Settings permission is off."""
    with _AUTOMATION_SETTINGS_LOCK:
        settings = dict(AUTOMATION_SETTINGS)
    if not settings.get("automation_enabled", True):
        return "Desktop automation is disabled in NEXUS Settings. Enable it before using computer-control tools."
    if not settings.get(feature, True):
        labels = {
            "allow_app_control": "app, keyboard, and typing control",
            "allow_file_operations": "file and folder access",
            "allow_file_deletion": "move-to-Recycle-Bin actions",
            "allow_permanent_file_deletion": "permanent file deletion",
            "allow_recycle_bin_cleanup": "permanent Recycle Bin cleanup",
            "allow_media_control": "volume and media controls",
            "allow_web_actions": "website opening and browser searches",
        }
        return f"{labels.get(feature, feature)} are disabled in NEXUS Settings. Enable that permission to continue."
    return None


def _update_automation_settings(payload: object) -> dict[str, bool]:
    if not isinstance(payload, dict):
        raise ValueError("Settings payload must be an object.")
    with _AUTOMATION_SETTINGS_LOCK:
        for key in AUTOMATION_SETTINGS_DEFAULT:
            value = payload.get(key)
            if type(value) is bool:
                AUTOMATION_SETTINGS[key] = value
        try:
            AUTOMATION_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            AUTOMATION_SETTINGS_PATH.write_text(
                json.dumps(AUTOMATION_SETTINGS, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            raise ValueError(f"Could not save local automation settings: {exc}") from exc
        return dict(AUTOMATION_SETTINGS)


SYSTEM_PROMPT = """
You are NEXUS, the personal intelligence core of this app: witty, calm, highly capable,
and designed for a restrained cinematic sci-fi interface. Your default persona is a
polished British AI concierge. Address the user as __USER_ADDRESS__ occasionally, not in every reply.

BRITISH VOICE AND LANGUAGE — HIGH PRIORITY:
- Speak in natural, contemporary British English with a refined southern-English,
  Received-Pronunciation-adjacent accent. Aim for a distinctly British sound, never an
  American one, while remaining natural rather than exaggerated or theatrical.
- Use measured pacing, precise consonants, elegant but conversational intonation, and
  a composed, mature, quietly confident delivery with subtle dry wit.
- Keep that accent consistent through the entire conversation, including short replies,
  tool confirmations, numbers, dates, and names where pronunciation permits. Do not drift
  into an American accent just because the user uses American wording.
- Prefer British vocabulary and spelling in text (for example, organise, colour, centre,
  whilst where natural, and mobile phone). Do not awkwardly force British idioms into every line.
- Use restrained British service phrasing naturally—such as "Certainly", "Very good",
  or "Right away"—without repeating catchphrases or using the user's form of address in every reply.
- Keep the character original: polished British sci-fi assistant energy, not an imitation
  of a particular actor or an exact performance of an existing fictional character.

Keep spoken answers concise and natural. When asked to perform a supported computer
action, use the relevant tool instead of merely describing the steps.

COMPUTER SAFETY:
- Use only the specific computer actions exposed as tools. Never run arbitrary shell
  commands, scripts, or model-supplied terminal commands. Never claim to do unsupported actions.
- Type text or press keys only when the user explicitly asks for that action. If the
  target app/window is unclear, ask a short clarification first.
- Never type passwords, API keys, payment details, or other secrets into an app.
- For WhatsApp, first read back the contact and exact message and ask: "Shall I send it,
  __USER_CALL__?" Only call the tool with confirmed=true after the user explicitly agrees to that
  exact message. Never claim to have inspected the WhatsApp contact list.
- Never delete files, overwrite existing files, discard unsaved work, or change security
  settings. Ask first before consequential actions not explicitly covered by a safe tool.
- File tools operate only inside the user's home directory, do not follow hidden items,
  and do not intentionally overwrite. For ambiguous source files, list/search first and
  ask which one the user means. Do not move/rename several files unless the user clearly
  requested the batch operation.
- Use open_folder for known folders, copy_file for safe non-overwriting file copies, and move_folder only when the user clearly names one source folder and destination.
- Use control_volume for system volume and control_media for play/pause, track navigation,
  and media seeking. Seeking with rewind/forward keys requires the intended player to be
  focused; ask a quick clarification if that is not clear. Never run arbitrary shell commands.
- The user explicitly requesting an ordinary app, file move, new folder, or volume change
  authorizes that specific action; no broader automation is implied.
- Report tool failures honestly. Do not claim success unless a tool confirms it.
- For current or changeable facts, use web_search rather than memory. Say one short line
  before searching, then answer in your own words. Do not read out web addresses.
- If asked to sort Downloads, call organize_downloads and report its actual result.

FILE & APP ACCESS:
- File tools only manage non-hidden files/folders inside the current user's home directory. Never attempt to bypass disabled permissions or access outside that scope.
- For organizing a non-Downloads folder, call organize_folder with the requested existing folder path. It sorts only direct files, skips hidden items and links, and never deletes or intentionally overwrites files.
- For ordinary deletion, call request_move_to_recycle_bin. If the file-operation and Recycle Bin permissions are enabled, it moves the exact requested item to the OS Recycle Bin/Trash immediately; no per-item approval is needed because it remains recoverable when supported. Report the actual tool result.
- Only irreversible actions need review: call request_permanent_delete when the user explicitly asks to permanently delete a specific file/folder, or request_empty_recycle_bin to permanently empty the bin. Those tools queue the action. The user must review it in Settings; permanent file deletion requires typing DELETE and emptying the bin requires typing EMPTY. Never claim completion until the local endpoint confirms it.
- Respect the File & App Access switches. If file operations, recycle-bin deletion, permanent deletion, or cleanup is disabled, explain which switch must be enabled. Never substitute permanent deletion for moving an item to the Recycle Bin.
""".strip()

LIVE_PROMPT = SYSTEM_PROMPT + """

SCREEN VISION — CURRENT PERMISSION IS AUTHORITATIVE:
- You may describe the user's screen only while Screen Vision is enabled AND a screen-sharing stream is active and sending frames.
- The latest Screen Vision state update overrides all older frames. When you receive a Screen Vision disabled update, immediately treat every previously received frame as stale and unusable; you have NO current access to the screen.
- While disabled, never infer, guess, or claim what is currently on the screen based on earlier images. If asked whether you can see the screen or what is on it, say: "No, I don't have access to your screen right now because Screen Vision is disabled." Then ask them to enable it if they want screen-based help.
- A Screen Vision state update is an internal permission/context update, not a request to narrate or act. Do not respond to the update itself; wait for the user's next request.
- Screen frames may contain private information; discuss only what is relevant to the user's request.

You are in a live voice conversation. Unless the user explicitly asks for another language,
respond in British English and maintain the refined southern-British / RP-adjacent accent
specified in the system instructions. This applies to every generated audio response, not
only greetings. Avoid American pronunciation, American idioms, and exaggerated caricature.
Do not use spoken markdown, bullet lists, or decorative symbols. When a tool completes,
confirm the result in one short, natural British-English sentence. The user interface visual
responds to voice activity and assistant audio; do not claim to see things outside the tools.
"""

# ---------------------------------------------------------------------------
# Tools — intentionally limited, allowlisted computer actions
# ---------------------------------------------------------------------------
def get_current_time() -> str:
    """Get the current date and time on the computer running NEXUS."""
    return datetime.now().astimezone().strftime("%A, %d %B %Y, %I:%M %p %Z")


def open_website(url: str) -> str:
    """Open a website in the default browser. Args: url, such as youtube.com."""
    blocked = _automation_guard("allow_web_actions")
    if blocked:
        return blocked
    value = (url or "").strip()
    if not value:
        return "No website was provided."
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    if not re.match(r"^https?://[^\s]+$", value, re.I):
        return "That website address does not look valid."
    try:
        opened = webbrowser.open(value, new=2)
        return f"Opened {value}" if opened else f"Asked the browser to open {value}."
    except Exception as exc:
        return f"Couldn't open that website: {exc}"


def search_google(query: str) -> str:
    """Search Google in the browser. Args: query, the search terms."""
    blocked = _automation_guard("allow_web_actions")
    if blocked:
        return blocked
    value = (query or "").strip()
    if not value:
        return "No search query was provided."
    try:
        webbrowser.open("https://www.google.com/search?q=" + quote_plus(value), new=2)
        return f"Searching Google for: {value}"
    except Exception as exc:
        return f"Google search could not be opened: {exc}"


APP_COMMANDS = {
    "windows": {
        "notepad": "notepad", "calculator": "calc", "paint": "mspaint",
        "file explorer": "explorer", "task manager": "taskmgr", "chrome": "chrome",
        "edge": "msedge", "firefox": "firefox", "spotify": "spotify",
        "discord": "discord", "vlc": "vlc", "steam": "steam", "obsidian": "obsidian",
        "vscode": "code", "wordpad": "write", "terminal": "wt",
        "volume mixer": "sndvol", "settings": "ms-settings:",
    },
    "darwin": {
        "calculator": "Calculator", "notes": "Notes", "chrome": "Google Chrome",
        "safari": "Safari", "firefox": "Firefox", "spotify": "Spotify",
        "discord": "Discord", "vlc": "VLC", "obsidian": "Obsidian",
        "vscode": "Visual Studio Code", "textedit": "TextEdit", "terminal": "Terminal",
        "finder": "Finder",
    },
    "linux": {
        "calculator": "gnome-calculator", "files": "nautilus", "chrome": "google-chrome",
        "firefox": "firefox", "spotify": "spotify", "discord": "discord",
        "vlc": "vlc", "steam": "steam", "obsidian": "obsidian",
        "vscode": "code", "terminal": "x-terminal-emulator", "text editor": "gedit",
        "file manager": "xdg-open",
    },
}


def open_app(app_name: str) -> str:
    """Open an application from the built-in allowlist. Args: app_name, e.g. notepad, calculator, chrome, spotify, or vscode."""
    blocked = _automation_guard("allow_app_control")
    if blocked:
        return blocked
    system = platform.system().lower()
    apps = APP_COMMANDS.get(system, {})
    key = (app_name or "").lower().strip()
    try:
        if key in apps:
            command = apps[key]
            if system == "windows":
                if key == "settings":
                    subprocess.Popen(["cmd", "/c", "start", "", "ms-settings:"], shell=False)
                else:
                    subprocess.Popen(["cmd", "/c", "start", "", command], shell=False)
            elif system == "darwin":
                subprocess.Popen(["open", "-a", command])
            else:
                subprocess.Popen([command])
            return f"Opened {app_name}."

        # Safe fallbacks for applications not in the short built-in list:
        # Windows Start Menu shortcuts, macOS app names, or a single executable
        # on PATH. No user-supplied shell commands or arguments are executed.
        if not key or len(key) > 100 or any(ch in key for ch in "\x00\r\n"):
            return "Please provide the application's display name."
        if system == "windows":
            roots = [
                Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
                Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
            ]
            matches = []
            for root in roots:
                if not root.is_dir():
                    continue
                for shortcut in root.rglob("*.lnk"):
                    if key.casefold() in shortcut.stem.casefold():
                        matches.append(shortcut)
                        if len(matches) >= 200:
                            break
                if len(matches) >= 200:
                    break
            exact = [shortcut for shortcut in matches if shortcut.stem.casefold() == key.casefold()]
            target = exact[0] if len(exact) == 1 else (matches[0] if len(matches) == 1 else None)
            if target:
                os.startfile(str(target))  # type: ignore[attr-defined]
                return f"Opened {target.stem} from the Start Menu."
            if matches:
                choices = ", ".join(shortcut.stem for shortcut in matches[:5])
                return f"I found several matching apps for '{app_name}': {choices}. Say the full app name to choose one."
        elif system == "darwin":
            if "/" not in app_name and "\\" not in app_name:
                subprocess.Popen(["open", "-a", app_name])
                return f"Asked macOS to open {app_name}."
        else:
            if re.fullmatch(r"[A-Za-z0-9_.+ -]{1,100}", app_name.strip()):
                executable = shutil.which(app_name.strip())
                if executable:
                    subprocess.Popen([executable])
                    return f"Opened {app_name}."
        return f"I couldn't find an installed app named '{app_name}'. Try its Start Menu/app name or exact command name."
    except Exception as exc:
        return f"Couldn't open {app_name}: {exc}"


def open_path(path: str) -> str:
    """Open an existing non-hidden file or folder inside the user's home directory. Args: path, the full path."""
    blocked = _automation_guard("allow_app_control")
    if blocked:
        return blocked
    try:
        target = Path(path).expanduser().resolve(strict=True)
        home = Path.home().resolve()
        if target != home and home not in target.parents:
            return "For safety, I can only open files and folders inside your home directory."
        if target.name.startswith("."):
            return "For safety, hidden files and folders are not opened by this tool."
        if platform.system() == "Windows":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return f"Opened {target.name}."
    except FileNotFoundError:
        return "That path doesn't exist. Check the path and try again."
    except Exception as exc:
        return f"Couldn't open that path: {exc}"


def type_text(text: str) -> str:
    """Type or paste exact text into the currently focused app. Only use when explicitly requested; maximum 2000 characters. Args: text."""
    blocked = _automation_guard("allow_app_control")
    if blocked:
        return blocked
    if not text:
        return "No text provided."
    if len(text) > 2000:
        return "Typing is limited to 2000 characters per action."
    try:
        import pyautogui
    except ImportError:
        return "Typing support isn't installed. Run: pip install pyautogui pyperclip"
    pyautogui.FAILSAFE = True
    try:
        try:
            import pyperclip
            pyperclip.copy(text)
            pyautogui.hotkey("command" if platform.system() == "Darwin" else "ctrl", "v")
        except ImportError:
            if not text.isascii():
                return "Unicode typing needs pyperclip. Run: pip install pyperclip"
            pyautogui.write(text, interval=0.003)
        return f"Typed {len(text)} characters into the currently focused app."
    except Exception as exc:
        return f"Typing failed: {exc}"


SAFE_HOTKEYS = {
    "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+a", "ctrl+z", "ctrl+y", "ctrl+s",
    "ctrl+f", "ctrl+p", "ctrl+left", "ctrl+right", "ctrl+shift+left",
    "ctrl+shift+right", "alt+tab", "win+d", "ctrl+shift+esc", "enter", "escape",
    "tab", "space", "home", "end", "delete",
}


def press_hotkey(keys: str) -> str:
    """Press an allowlisted shortcut in the currently focused app. Args: keys, e.g. ctrl+s, ctrl+c, alt+tab, enter, or escape."""
    blocked = _automation_guard("allow_app_control")
    if blocked:
        return blocked
    normalized = "+".join(p.strip().lower() for p in (keys or "").split("+") if p.strip())
    normalized = normalized.replace("control+", "ctrl+")
    if normalized not in SAFE_HOTKEYS:
        return "That shortcut isn't allowed. Try ctrl+s, ctrl+c, ctrl+v, alt+tab, win+d, enter, or escape."
    try:
        import pyautogui
    except ImportError:
        return "Keyboard automation isn't installed. Run: pip install pyautogui"
    pyautogui.FAILSAFE = True
    mapping = {"ctrl": "command"} if platform.system() == "Darwin" else {"win": "win"}
    parts = [mapping.get(part, part) for part in normalized.split("+")]
    try:
        pyautogui.press(parts[0]) if len(parts) == 1 else pyautogui.hotkey(*parts)
        return f"Pressed {normalized}."
    except Exception as exc:
        return f"Shortcut failed: {exc}"


def create_text_file(filename: str, content: str = "", folder: str = "Documents") -> str:
    """Create a new file (never overwrites) in Desktop, Documents, Downloads, or Documents/Nexus. Allowed extensions: txt, md, py, json, html, css, js, csv. Args: filename, content, folder."""
    blocked = _automation_guard("allow_file_operations")
    if blocked:
        return blocked
    allowed_folders = {"desktop": "Desktop", "documents": "Documents", "downloads": "Downloads"}
    folder_key = (folder or "Documents").strip().lower()
    try:
        base = _known_folder_path("Nexus" if folder_key == "nexus" else folder_key.title()) if folder_key in {*allowed_folders, "nexus"} else None
    except Exception as exc:
        return f"Couldn't resolve the destination folder: {exc}"
    if base is None:
        return "Folder must be Desktop, Documents, Downloads, or Nexus."
    name = Path(filename or "").name
    if name != filename or name in {"", ".", ".."} or name.startswith("."):
        return "Use a simple filename without folders or hidden-file names."
    if Path(name).suffix.lower() not in {".txt", ".md", ".py", ".json", ".html", ".css", ".js", ".csv"}:
        return "Allowed file types: .txt, .md, .py, .json, .html, .css, .js, .csv."
    if len(content or "") > 50000:
        return "File content is limited to 50000 characters."
    try:
        base.mkdir(parents=True, exist_ok=True)
        target = (base / name).resolve()
        if base.resolve() not in target.parents:
            return "That filename isn't allowed."
        with target.open("x", encoding="utf-8", newline="") as handle:
            handle.write(content or "")
        return f"Created {target.name} in {base}. Existing files are never overwritten."
    except FileExistsError:
        return "A file with that name already exists. Choose another name; I won't overwrite it."
    except Exception as exc:
        return f"Couldn't create the file: {exc}"


_DOWNLOAD_CATEGORIES = {
    "Images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic", ".tif", ".tiff", ".ico", ".raw"},
    "Documents": {".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".epub", ".pages"},
    "Spreadsheets": {".xls", ".xlsx", ".ods", ".csv", ".tsv"},
    "Presentations": {".ppt", ".pptx", ".odp", ".key"},
    "Videos": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".mpeg", ".mpg", ".3gp"},
    "Audio": {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma", ".aiff"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".iso"},
    "Installers": {".exe", ".msi", ".msix", ".appx", ".deb", ".rpm", ".dmg", ".pkg", ".apk"},
    "Code": {".py", ".pyw", ".js", ".ts", ".html", ".htm", ".css", ".json", ".xml", ".yaml", ".yml", ".toml", ".ipynb", ".sh", ".bat", ".cmd", ".ps1", ".java", ".c", ".h", ".cpp", ".cs", ".go", ".rs", ".php", ".sql"},
    "Fonts": {".ttf", ".otf", ".woff", ".woff2"}, "Shortcuts": {".lnk", ".url", ".webloc"},
}
_INCOMPLETE_SUFFIXES = (".crdownload", ".part", ".partial", ".download", ".tmp", ".opdownload")


def _downloads_directory() -> Path:
    if platform.system() == "Windows":
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _kind = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
            resolved = Path(os.path.expandvars(value)).expanduser()
            if resolved.is_dir():
                return resolved.resolve()
        except (ImportError, OSError, FileNotFoundError):
            pass
    return (Path.home() / "Downloads").resolve()


def _windows_known_folder(guid: str, fallback: str) -> Path:
    """Read a Windows known-folder path, falling back to the conventional home path."""
    if platform.system() == "Windows":
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _kind = winreg.QueryValueEx(key, guid)
            return Path(os.path.expandvars(value)).expanduser().resolve()
        except (ImportError, OSError, FileNotFoundError):
            pass
    return (Path.home() / fallback).resolve()


def _desktop_directory() -> Path:
    return _windows_known_folder("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "Desktop")


def _documents_directory() -> Path:
    return _windows_known_folder("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", "Documents")


def _download_category(path: Path) -> str:
    for category, extensions in _DOWNLOAD_CATEGORIES.items():
        if path.suffix.lower() in extensions:
            return category
    return "Miscellaneous"


def _unique_target(folder: Path, filename: str) -> Path:
    candidate = folder / filename
    if not candidate.exists():
        return candidate
    original = Path(filename)
    for number in range(2, 10000):
        candidate = folder / f"{original.stem} ({number}){original.suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Couldn't find a free filename for {filename}")


def organize_downloads() -> str:
    """Organize regular files directly inside Downloads into folders by file type. Never deletes files or intentionally overwrites; skips hidden files, folders, links, and incomplete downloads."""
    blocked = _automation_guard("allow_file_operations")
    if blocked:
        return blocked
    downloads = _downloads_directory()
    if not downloads.is_dir():
        return f"I couldn't find your Downloads folder at {downloads}. No files were changed."
    groups: dict[str, list[Path]] = {}
    skipped_hidden = skipped_incomplete = skipped_other = 0
    try:
        for item in downloads.iterdir():
            try:
                if item.name.startswith("."):
                    skipped_hidden += 1
                elif item.is_symlink() or not item.is_file():
                    skipped_other += 1
                elif item.name.lower().endswith(_INCOMPLETE_SUFFIXES):
                    skipped_incomplete += 1
                else:
                    groups.setdefault(_download_category(item), []).append(item)
            except OSError:
                skipped_other += 1
    except OSError as exc:
        return f"Couldn't read Downloads: {exc}. No files were changed."
    if not groups:
        return ("Your Downloads folder has no files to sort. Existing folders were left alone. "
                f"Skipped {skipped_hidden} hidden item(s) and {skipped_incomplete} incomplete download(s).")
    moved = 0
    moved_by_category: dict[str, int] = {}
    errors: list[str] = []
    for category, items in sorted(groups.items()):
        destination = downloads / category
        try:
            destination.mkdir(exist_ok=True)
            moved_by_category[category] = 0
        except OSError as exc:
            errors.append(f"{category}: couldn't create folder ({exc})")
            continue
        for source in items:
            try:
                if not source.exists() or source.is_symlink() or not source.is_file():
                    errors.append(f"Skipped {source.name}: it changed before it could be moved")
                    continue
                shutil.move(str(source), str(_unique_target(destination, source.name)))
                moved += 1
                moved_by_category[category] += 1
            except Exception as exc:
                errors.append(f"{source.name}: {exc}")
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(moved_by_category.items()) if v)
    result = f"Downloads organization complete. Sorted {moved} file(s) in {downloads}."
    if summary:
        result += " " + summary + "."
    result += " No files were deleted; existing files were not intentionally overwritten."
    if skipped_incomplete:
        result += f" Left {skipped_incomplete} incomplete download(s) untouched."
    if skipped_hidden:
        result += f" Left {skipped_hidden} hidden item(s) untouched."
    if skipped_other:
        result += f" Left {skipped_other} folder(s), links, or special item(s) untouched."
    if errors:
        result += " Some items couldn't be moved: " + "; ".join(errors[:5])
        if len(errors) > 5:
            result += f"; and {len(errors) - 5} more."
    return result


def organize_folder(folder_path: str) -> str:
    """Sort direct files in any existing non-hidden folder inside the user's home directory into file-type folders. Never deletes or intentionally overwrites; skips hidden entries, links, and incomplete downloads. Args: folder_path, an existing folder path or known folder name."""
    blocked = _automation_guard("allow_file_operations")
    if blocked:
        return blocked
    try:
        target = _known_folder_path(folder_path)
        if not target.is_dir():
            return f"'{folder_path}' isn't an existing folder. No files were changed."
        home = _home_root()
        if target.resolve() == home:
            return "For safety, choose a specific folder inside your home directory rather than organizing the entire home root."
        groups: dict[str, list[Path]] = {}
        skipped_hidden = skipped_incomplete = skipped_other = 0
        for item in target.iterdir():
            try:
                if item.name.startswith("."):
                    skipped_hidden += 1
                elif item.is_symlink() or not item.is_file():
                    skipped_other += 1
                elif item.name.lower().endswith(_INCOMPLETE_SUFFIXES):
                    skipped_incomplete += 1
                else:
                    groups.setdefault(_download_category(item), []).append(item)
            except OSError:
                skipped_other += 1
        moved = 0
        by_category: dict[str, int] = {}
        errors: list[str] = []
        for category, items in sorted(groups.items()):
            destination = target / category
            try:
                if destination.exists() and (not destination.is_dir() or destination.is_symlink()):
                    errors.append(f"{category}: a non-folder already occupies the category name")
                    continue
                destination.mkdir(exist_ok=True)
                for source in items:
                    try:
                        if not source.exists() or source.is_symlink() or not source.is_file():
                            errors.append(f"Skipped {source.name}: it changed before it could be moved")
                            continue
                        target_path = _unique_target(destination, source.name)
                        shutil.move(str(source), str(target_path))
                        moved += 1
                        by_category[category] = by_category.get(category, 0) + 1
                    except Exception as exc:
                        errors.append(f"{source.name}: {exc}")
            except OSError as exc:
                errors.append(f"{category}: couldn't create category folder ({exc})")
        if not moved and not errors:
            return f"No eligible files to organize in {target}. Existing folders and files were left alone."
        summary = ", ".join(f"{category}: {count}" for category, count in sorted(by_category.items()))
        result = f"Organized {moved} direct file(s) in {target}."
        if summary:
            result += " " + summary + "."
        result += " No files were deleted or intentionally overwritten."
        if skipped_hidden:
            result += f" Left {skipped_hidden} hidden item(s) untouched."
        if skipped_incomplete:
            result += f" Left {skipped_incomplete} incomplete download(s) untouched."
        if skipped_other:
            result += f" Left {skipped_other} folders, links, or special items untouched."
        if errors:
            result += " Some items couldn't be moved: " + "; ".join(errors[:5])
            if len(errors) > 5:
                result += f"; and {len(errors) - 5} more."
        return result
    except Exception as exc:
        return f"Couldn't organize that folder: {exc}"


def open_folder(folder: str = "Desktop") -> str:
    """Open a known user folder such as Desktop, Downloads, Documents, Pictures, Videos, Music, or Home in the file manager."""
    blocked = _automation_guard("allow_app_control")
    if blocked:
        return blocked
    try:
        target = _known_folder_path(folder or "Desktop")
        if not target.exists() or not target.is_dir():
            return f"Folder '{folder}' does not exist yet."
        if target.name.startswith("."):
            return "Hidden folders are not opened by NEXUS."
        if platform.system() == "Windows":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(target)])
        else:
            opener = shutil.which("xdg-open")
            if not opener:
                return "The xdg-open file-manager helper is not installed."
            subprocess.Popen([opener, str(target)])
        return f"Opened {target} in the file manager."
    except Exception as exc:
        return f"Couldn't open folder: {exc}"


def copy_file(source_path: str, destination_folder: str) -> str:
    """Copy one ordinary file by full path or unique filename into an existing folder inside the user's home directory. Never overwrites a destination file. Args: source_path, destination_folder."""
    blocked = _automation_guard("allow_file_operations")
    if blocked:
        return blocked
    try:
        source = _locate_user_file(source_path)
        if not source.is_file() or source.is_symlink():
            return "Only an ordinary individual file can be copied."
        destination = _known_folder_path(destination_folder)
        if not destination.is_dir():
            return f"Destination folder {destination} does not exist."
        target = _unique_target(destination, source.name)
        shutil.copy2(str(source), str(target), follow_symlinks=False)
        return f"Copied '{source.name}' to {target}. The original file was left unchanged."
    except Exception as exc:
        return f"Couldn't copy that file: {exc}"


def move_folder(source_path: str, destination_folder: str) -> str:
    """Move one folder inside the user's home directory into an existing destination folder. Never merges with or replaces an existing folder; will not move the home folder or a folder into itself. Args: source_path, destination_folder."""
    blocked = _automation_guard("allow_file_operations")
    if blocked:
        return blocked
    try:
        source = _resolve_home_path(source_path, must_exist=True)
        home = _home_root()
        if source == home:
            return "For safety, NEXUS will not move the home folder."
        if not source.is_dir() or source.is_symlink():
            return "The source must be an ordinary folder inside your home directory."
        destination = _known_folder_path(destination_folder)
        if not destination.is_dir():
            return f"Destination folder {destination} does not exist."
        if destination == source or source in destination.parents:
            return "A folder cannot be moved into itself or one of its own subfolders."
        if destination.resolve() == source.parent.resolve():
            return f"'{source.name}' is already inside {destination}; nothing was changed."
        target = _unique_target(destination, source.name)
        shutil.move(str(source), str(target))
        return f"Moved folder '{source.name}' to {target}. No files were deleted or overwritten."
    except Exception as exc:
        return f"Couldn't move that folder: {exc}"


def get_system_info() -> str:
    """Get basic computer information: operating system, Python version, and machine type."""
    return f"OS: {platform.system()} {platform.release()}, Machine: {platform.machine()}, Python: {platform.python_version()}"


def _home_root() -> Path:
    return Path.home().resolve()


def _resolve_home_path(raw_path: str, must_exist: bool = True) -> Path:
    """Resolve a path without allowing it to escape the user's home directory."""
    raw = (raw_path or "").strip()
    if not raw:
        raise ValueError("No path was provided.")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path.home() / candidate
    resolved = candidate.resolve(strict=must_exist)
    home = _home_root()
    if resolved != home and home not in resolved.parents:
        raise ValueError("For safety, NEXUS can only manage items inside your home directory.")
    relative_parts = resolved.relative_to(home).parts if resolved != home else ()
    if any(part.startswith(".") for part in relative_parts):
        raise ValueError("Hidden files and folders are not managed by NEXUS.")
    # Reject a directly supplied symlink rather than silently following it.
    try:
        if candidate.is_symlink():
            raise ValueError("For safety, NEXUS does not operate on symbolic links.")
    except OSError:
        pass
    return resolved


def _known_folder_path(folder: str) -> Path:
    """Resolve a common folder name or an existing custom folder under home."""
    names = {
        "home": Path.home(), "desktop": _desktop_directory(),
        "downloads": _downloads_directory(), "documents": _documents_directory(),
        "pictures": Path.home() / "Pictures", "photos": Path.home() / "Pictures",
        "videos": Path.home() / "Videos", "music": Path.home() / "Music",
        "nexus": _documents_directory() / "Nexus",
    }
    key = (folder or "").strip().lower()
    if key in names:
        target = names[key]
        # Existing OS-redirected Desktop/Documents paths may be inside OneDrive.
        target = target.resolve()
        home = _home_root()
        if target != home and home not in target.parents:
            raise ValueError("That known folder resolves outside your home directory.")
        return target
    return _resolve_home_path(folder, must_exist=True)


def _locate_user_file(value: str) -> Path:
    """Find a supplied path or an unambiguous filename in common user folders."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("No source file was provided.")
    supplied = Path(raw).expanduser()
    if supplied.is_absolute() or len(supplied.parts) > 1:
        return _resolve_home_path(raw, must_exist=True)
    home = Path.home()
    found: list[Path] = []
    for folder_name in ("Desktop", "Downloads", "Documents", "Pictures", "Videos", "Music"):
        try:
            folder = _known_folder_path(folder_name)
            candidate = folder / supplied.name
            if candidate.is_file() and not candidate.is_symlink() and not candidate.name.startswith("."):
                resolved = _resolve_home_path(str(candidate), must_exist=True)
                if resolved not in found:
                    found.append(resolved)
        except (OSError, ValueError):
            continue
    try:
        direct = _resolve_home_path(str(home / supplied.name), must_exist=True)
        if direct.is_file() and direct not in found:
            found.append(direct)
    except (OSError, ValueError):
        pass
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        return_found = ", ".join(str(x) for x in found[:5])
        raise ValueError(f"That filename exists in more than one location. Specify the full source path: {return_found}")
    raise FileNotFoundError(f"Couldn't find '{raw}' in your home folder or common Desktop/Downloads/Documents folders.")


def _queue_file_action(kind: str, display: str, path: str = "") -> str:
    with _PENDING_FILE_ACTIONS_LOCK:
        if len(_PENDING_FILE_ACTIONS) >= 10:
            return "There are already 10 permanent actions waiting for review. Review or cancel one in NEXUS Settings first."
        for action in _PENDING_FILE_ACTIONS.values():
            if action.get("kind") == kind and action.get("path", "") == path:
                return f"That action is already waiting for approval in NEXUS Settings: {display}. Nothing has changed."
        action_id = uuid.uuid4().hex[:12]
        _PENDING_FILE_ACTIONS[action_id] = {
            "id": action_id, "kind": kind, "display": display, "path": path,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    return f"Approval required: {display}. Nothing has been changed yet. Open NEXUS Settings → Pending File Actions and review it there."


def request_move_to_recycle_bin(path: str) -> str:
    """Move one file or folder inside the user's home directory to the system Recycle Bin/Trash immediately. Requires the File & Folder Access and Move to Recycle Bin permissions; no per-item approval is required because the move is recoverable where supported. Args: path."""
    blocked = _automation_guard("allow_file_operations") or _automation_guard("allow_file_deletion")
    if blocked:
        return blocked
    raw = (path or "").strip()
    try:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path.home() / candidate
        # Refuse if any path component supplied by the user is a symbolic link.
        probe = candidate
        while probe != probe.parent:
            if probe.exists() and probe.is_symlink():
                return "For safety, NEXUS won't send symbolic links to the Recycle Bin. Nothing was changed."
            probe = probe.parent
        target = _resolve_home_path(raw, must_exist=True)
        home = _home_root()
        if target == home:
            return "For safety, NEXUS won't move your entire home folder to the Recycle Bin."
        protected_roots = {"desktop", "documents", "downloads", "pictures", "videos", "music", "onedrive", "appdata"}
        if target.parent.resolve() == home and target.name.casefold() in protected_roots:
            return f"For safety, NEXUS won't delete the top-level {target.name} folder. Choose a specific item inside it instead."
        if not (target.is_file() or target.is_dir()) or target.is_symlink():
            return "Choose a regular file or folder inside your home directory. Nothing was changed."
        try:
            from send2trash import send2trash
        except ImportError:
            return "The send2trash package is required. Install it with: python -m pip install send2trash. Nothing was changed."
        send2trash(str(target))
        return f"Moved '{target.name}' to the system Recycle Bin/Trash. No approval was needed; the item remains recoverable where the OS supports it."
    except Exception as exc:
        return f"Couldn't move the item to Recycle Bin/Trash: {exc}"


def request_permanent_delete(path: str) -> str:
    """Queue the permanent deletion of one non-hidden file or folder inside the user's home directory. Requires the separate permanent-deletion permission and explicit approval in Settings with the exact word DELETE. Ordinary deletion should use request_move_to_recycle_bin instead. Args: path."""
    blocked = _automation_guard("allow_file_operations") or _automation_guard("allow_permanent_file_deletion")
    if blocked:
        return blocked
    raw = (path or "").strip()
    try:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path.home() / candidate
        probe = candidate
        while probe != probe.parent:
            if probe.exists() and probe.is_symlink():
                return "For safety, NEXUS won't permanently delete symbolic links. Nothing was changed."
            probe = probe.parent
        target = _resolve_home_path(raw, must_exist=True)
        home = _home_root()
        if target == home:
            return "For safety, NEXUS won't permanently delete your home folder."
        protected_roots = {"desktop", "documents", "downloads", "pictures", "videos", "music", "onedrive", "appdata"}
        if target.parent.resolve() == home and target.name.casefold() in protected_roots:
            return f"For safety, NEXUS won't permanently delete the top-level {target.name} folder. Choose a specific item inside it instead."
        if not (target.is_file() or target.is_dir()) or target.is_symlink():
            return "Choose a regular file or folder inside your home directory. Nothing was changed."
        return _queue_file_action("permanent_delete_file", f"PERMANENTLY DELETE: {target}", str(target))
    except Exception as exc:
        return f"Couldn't stage permanent deletion: {exc}"


def request_empty_recycle_bin() -> str:
    """Queue a request to empty the system Recycle Bin/Trash permanently. Requires the cleanup permission and separate user approval in Settings with the exact word EMPTY. Args: none."""
    blocked = _automation_guard("allow_file_operations") or _automation_guard("allow_recycle_bin_cleanup")
    if blocked:
        return blocked
    return _queue_file_action("empty_recycle_bin", "Permanently empty the system Recycle Bin / Trash")


def _empty_system_recycle_bin() -> str:
    """Empty the current user's OS Recycle Bin/Trash after an explicit UI confirmation."""
    system = platform.system()
    if system == "Windows":
        import ctypes
        flags = 0x0001 | 0x0002 | 0x0004  # no confirmation/progress UI/sound; UI already confirmed
        shell32 = ctypes.windll.shell32
        shell32.SHEmptyRecycleBinW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
        shell32.SHEmptyRecycleBinW.restype = ctypes.c_long
        result = shell32.SHEmptyRecycleBinW(None, None, flags)
        if result != 0:
            raise OSError(f"Windows could not empty the Recycle Bin (code {result}).")
        return "Windows Recycle Bin emptied successfully."
    if system == "Darwin":
        completed = subprocess.run(
            ["osascript", "-e", 'tell application "Finder" to empty the trash'],
            capture_output=True, text=True, timeout=90, check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "Finder declined the request.").strip()
            raise RuntimeError(detail[:220])
        return "macOS Trash emptied successfully."
    # Linux/XDG desktop: empty the standard per-user trash only.
    data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))).expanduser()
    trash = data_home / "Trash"
    files_dir, info_dir = trash / "files", trash / "info"
    removed = 0
    for directory in (files_dir, info_dir):
        if not directory.is_dir() or directory.is_symlink():
            continue
        for item in directory.iterdir():
            if item.is_symlink() or item.is_file():
                item.unlink(missing_ok=True)
                removed += 1
            elif item.is_dir():
                shutil.rmtree(item)
                removed += 1
    return f"Standard user Trash emptied successfully ({removed} trash entries). Other mounted-volume trash locations, if any, may remain."


def _execute_pending_file_action(action_id: str) -> tuple[bool, str]:
    with _PENDING_FILE_ACTIONS_LOCK:
        action = dict(_PENDING_FILE_ACTIONS.get(action_id) or {})
    if not action:
        return False, "That pending action no longer exists. Refresh the Settings panel."
    if action["kind"] == "trash_item":
        blocked = _automation_guard("allow_file_operations") or _automation_guard("allow_file_deletion")
        if blocked:
            return False, blocked
        try:
            target = _resolve_home_path(action.get("path", ""), must_exist=True)
            if target == _home_root() or target.is_symlink():
                return False, "The target is no longer safe to operate on. The action remains pending."
            try:
                from send2trash import send2trash
            except ImportError:
                return False, "The send2trash package is required. Install it with: python -m pip install send2trash. This action is still pending."
            send2trash(str(target))
            message = f"Moved '{target.name}' to the system Recycle Bin/Trash."
        except Exception as exc:
            return False, f"Couldn't move the item to Recycle Bin/Trash: {exc}"
    elif action["kind"] == "permanent_delete_file":
        blocked = _automation_guard("allow_file_operations") or _automation_guard("allow_permanent_file_deletion")
        if blocked:
            return False, blocked
        try:
            raw_path = action.get("path", "")
            candidate = Path(raw_path).expanduser()
            probe = candidate
            while probe != probe.parent:
                if probe.exists() and probe.is_symlink():
                    return False, "The target now contains a symbolic link. Permanent deletion was blocked."
                probe = probe.parent
            target = _resolve_home_path(raw_path, must_exist=True)
            if target == _home_root() or target.is_symlink():
                return False, "The target is no longer safe to operate on. The action remains pending."
            protected_roots = {"desktop", "documents", "downloads", "pictures", "videos", "music", "onedrive", "appdata"}
            if target.parent.resolve() == _home_root() and target.name.casefold() in protected_roots:
                return False, "Permanent deletion of this top-level user folder is blocked. The action remains pending."
            if target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            else:
                return False, "Target is no longer a regular file or folder. The action remains pending."
            message = f"Permanently deleted '{target.name}'."
        except Exception as exc:
            return False, f"Couldn't permanently delete the item: {exc}"
    elif action["kind"] == "empty_recycle_bin":
        blocked = _automation_guard("allow_file_operations") or _automation_guard("allow_recycle_bin_cleanup")
        if blocked:
            return False, blocked
        try:
            message = _empty_system_recycle_bin()
        except Exception as exc:
            return False, f"Couldn't empty the Recycle Bin/Trash: {exc}"
    else:
        return False, "Unknown pending action type."
    with _PENDING_FILE_ACTIONS_LOCK:
        _PENDING_FILE_ACTIONS.pop(action_id, None)
    return True, message


def list_folder_contents(folder: str = "Desktop", limit: int = 40) -> str:
    """List files and folders in Desktop, Downloads, Documents, or another existing folder inside the user's home directory. Args: folder, limit (max 80)."""
    blocked = _automation_guard("allow_file_operations")
    if blocked:
        return blocked
    try:
        target = _known_folder_path(folder)
        if not target.is_dir():
            return f"'{folder}' is not an existing folder."
        count = max(1, min(80, int(limit)))
        entries: list[str] = []
        for item in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.casefold())):
            if item.name.startswith(".") or item.is_symlink():
                continue
            try:
                if item.is_dir():
                    entries.append(f"[Folder] {item.name}/")
                elif item.is_file():
                    entries.append(f"[File] {item.name} ({item.stat().st_size} bytes)")
            except OSError:
                continue
            if len(entries) >= count:
                break
        if not entries:
            return f"{target} is empty (or contains only hidden/unsupported entries)."
        return f"Contents of {target} (up to {count} entries):\n" + "\n".join(entries)
    except Exception as exc:
        return f"Couldn't list that folder: {exc}"


def find_files(query: str, folder: str = "Home", limit: int = 20) -> str:
    """Find files/folders by name within Home, Desktop, Downloads, or Documents. Searches at most three folder levels and returns full paths. Args: query, folder, limit (max 40)."""
    blocked = _automation_guard("allow_file_operations")
    if blocked:
        return blocked
    term = (query or "").strip().casefold()
    if len(term) < 2:
        return "Search needs at least two characters."
    try:
        root = _known_folder_path(folder or "Home")
        if not root.is_dir():
            return f"'{folder}' is not an existing folder."
        cap = max(1, min(40, int(limit)))
        found: list[str] = []
        scan_deadline = time.monotonic() + 2.0
        scanned_dirs = 0
        skip_dirs = {"AppData", "node_modules", ".git", "venv", ".venv", "__pycache__", "WindowsApps", "site-packages"}
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            if time.monotonic() > scan_deadline or scanned_dirs >= 500:
                break
            scanned_dirs += 1
            current_path = Path(current)
            depth = len(current_path.relative_to(root).parts)
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in skip_dirs and not (current_path / d).is_symlink()]
            if depth >= 3:
                dirs[:] = []
            for name in dirs + files:
                if name.startswith(".") or term not in name.casefold():
                    continue
                candidate = current_path / name
                if candidate.is_symlink():
                    continue
                found.append(str(candidate))
                if len(found) >= cap:
                    break
            if len(found) >= cap:
                break
        if found:
            cap_note = " Search was time-limited for responsiveness." if time.monotonic() >= scan_deadline or scanned_dirs >= 500 else ""
            return f"Found {len(found)} match(es) under {root}:{cap_note}\n" + "\n".join(found)
        return f"No matches for '{query}' under {root} (search limited to three folder levels and about two seconds)."
    except Exception as exc:
        return f"File search failed: {exc}"


def create_folder(folder_name: str, parent_folder: str = "Desktop") -> str:
    """Create a new folder in Desktop, Downloads, Documents, or another existing folder inside home. Never merges with or replaces an existing folder. Args: folder_name, parent_folder."""
    blocked = _automation_guard("allow_file_operations")
    if blocked:
        return blocked
    name = (folder_name or "").strip()
    if not name or name in {".", ".."} or Path(name).name != name or "/" in name or "\\" in name or name.startswith("."):
        return "Use a simple folder name without path separators or a leading dot."
    try:
        parent = _known_folder_path(parent_folder)
        if not parent.is_dir():
            return f"The parent folder {parent} doesn't exist."
        target = parent / name
        if target.exists():
            return f"A file or folder named '{name}' already exists in {parent}; nothing was changed."
        target.mkdir()
        return f"Created folder: {target}"
    except Exception as exc:
        return f"Couldn't create that folder: {exc}"


def move_file(source_path: str, destination_folder: str) -> str:
    """Move one file by full path or unique filename into an existing folder inside home (e.g. Desktop or Downloads). Never overwrites a destination file. Args: source_path, destination_folder."""
    blocked = _automation_guard("allow_file_operations")
    if blocked:
        return blocked
    try:
        source = _locate_user_file(source_path)
        if not source.is_file():
            return "Only individual files can be moved by this tool; use a file path, not a folder."
        destination = _known_folder_path(destination_folder)
        if not destination.is_dir():
            return f"Destination folder {destination} doesn't exist. Create it first or choose Desktop, Downloads, or Documents."
        if source.parent.resolve() == destination.resolve():
            return f"'{source.name}' is already in {destination}; nothing was changed."
        target = _unique_target(destination, source.name)
        shutil.move(str(source), str(target))
        return f"Moved '{source.name}' to {target}. No file was deleted or overwritten."
    except Exception as exc:
        return f"Couldn't move that file: {exc}"


def rename_file(source_path: str, new_name: str) -> str:
    """Rename one file inside the user's home folder. Use only after the user explicitly requested the rename. Does not overwrite an existing file. Args: source_path, new_name."""
    blocked = _automation_guard("allow_file_operations")
    if blocked:
        return blocked
    name = (new_name or "").strip()
    if not name or name in {".", ".."} or Path(name).name != name or "/" in name or "\\" in name or name.startswith("."):
        return "Use a simple new filename without path separators or a leading dot."
    try:
        source = _locate_user_file(source_path)
        if not source.is_file():
            return "Only individual files can be renamed by this tool."
        target = source.parent / name
        if target.exists():
            return f"'{name}' already exists; nothing was changed."
        source.rename(target)
        return f"Renamed '{source.name}' to '{target.name}' at {target.parent}."
    except Exception as exc:
        return f"Couldn't rename that file: {exc}"


def control_volume(action: str, steps: int = 1, level: int = -1) -> str:
    """Control system volume. Actions: up, down, toggle_mute, or set. Steps is 1-15; level for set is 0-100 (exact set on macOS/Linux and Windows if pycaw is installed). Args: action, steps, level."""
    blocked = _automation_guard("allow_media_control")
    if blocked:
        return blocked
    act = (action or "").strip().lower().replace("-", "_").replace(" ", "_")
    if act not in {"up", "down", "toggle_mute", "mute_toggle", "set"}:
        return "Choose volume action: up, down, toggle_mute, or set (with level 0–100)."
    steps = max(1, min(15, int(steps or 1)))
    if act == "set":
        if not 0 <= int(level) <= 100:
            return "For set volume, supply a level from 0 to 100."
        value = int(level)
    else:
        value = -1
    system = platform.system()
    try:
        if system == "Windows":
            if act == "set":
                try:
                    from pycaw.pycaw import AudioUtilities
                    endpoint = AudioUtilities.GetSpeakers().EndpointVolume
                    endpoint.SetMasterVolumeLevelScalar(value / 100.0, None)
                    return f"System volume set to {value}%."
                except ImportError:
                    return "Exact volume setting on Windows needs the optional pycaw package. Run: python -m pip install pycaw. Volume up/down and mute toggle work without it."
            import ctypes
            vkeys = {"up": 0xAF, "down": 0xAE, "toggle_mute": 0xAD, "mute_toggle": 0xAD}
            vk = vkeys[act]
            count = 1 if act in {"toggle_mute", "mute_toggle"} else steps
            for _ in range(count):
                ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
                ctypes.windll.user32.keybd_event(vk, 0, 0x0002, 0)
            return "System volume " + ("increased" if act == "up" else "decreased" if act == "down" else "mute toggled") + "."
        if system == "Darwin":
            if act == "set":
                script = f"set volume output volume {value}"
            elif act in {"toggle_mute", "mute_toggle"}:
                current = subprocess.run(["osascript", "-e", "output muted of (get volume settings)"], capture_output=True, text=True, timeout=5, check=True).stdout.strip().lower()
                script = "set volume without output muted" if current == "true" else "set volume with output muted"
            else:
                current = int(subprocess.run(["osascript", "-e", "output volume of (get volume settings)"], capture_output=True, text=True, timeout=5, check=True).stdout.strip())
                new_value = max(0, min(100, current + (steps * 5 if act == "up" else -steps * 5)))
                script = f"set volume output volume {new_value}"
            subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5, check=True)
            return f"System volume set to {value}%." if act == "set" else ("System mute toggled." if act in {"toggle_mute", "mute_toggle"} else f"System volume set to {new_value}%.")
        # Linux: prefer WirePlumber's wpctl; fall back to PulseAudio's pactl.
        wpctl = shutil.which("wpctl")
        pactl = shutil.which("pactl")
        if not (wpctl or pactl):
            return "Linux volume control needs wpctl (WirePlumber) or pactl (PulseAudio)."
        if wpctl:
            if act == "set": arg = str(value / 100)
            elif act in {"toggle_mute", "mute_toggle"}: arg = "toggle"
            else: arg = f"{steps * 5}%{'+' if act == 'up' else '-'}"
            command = [wpctl, "set-volume", "@DEFAULT_AUDIO_SINK@", arg] if act not in {"toggle_mute", "mute_toggle"} else [wpctl, "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"]
        else:
            if act == "set": arg = f"{value}%"
            elif act in {"toggle_mute", "mute_toggle"}: arg = "toggle"
            else: arg = f"{'+' if act == 'up' else '-'}{steps * 5}%"
            command = [pactl, "set-sink-volume", "@DEFAULT_SINK@", arg] if act not in {"toggle_mute", "mute_toggle"} else [pactl, "set-sink-mute", "@DEFAULT_SINK@", "toggle"]
        subprocess.run(command, capture_output=True, text=True, timeout=5, check=True)
        return f"System volume set to {value}%." if act == "set" else ("System mute toggled." if act in {"toggle_mute", "mute_toggle"} else "System volume increased." if act == "up" else "System volume decreased.")
    except FileNotFoundError:
        return "The volume-control program is unavailable on this computer."
    except Exception as exc:
        return f"Volume control failed: {exc}"


def control_media(action: str, seconds: int = 10) -> str:
    """Control the current media player: play_pause, next, previous, stop, rewind, or forward. Rewind/forward uses keyboard seeks and requires the intended player to be focused on Windows; seconds is clamped to 5–60. Args: action, seconds."""
    blocked = _automation_guard("allow_media_control")
    if blocked:
        return blocked
    act = (action or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"pause": "play_pause", "play": "play_pause", "toggle": "play_pause", "back": "previous", "prev": "previous", "skip": "next", "rewind_10_seconds": "rewind", "forward_10_seconds": "forward", "seek_back": "rewind", "seek_forward": "forward"}
    act = aliases.get(act, act)
    if act not in {"play_pause", "next", "previous", "stop", "rewind", "forward"}:
        return "Choose media action: play_pause, next, previous, stop, rewind, or forward."
    seconds = max(5, min(60, int(seconds or 10)))
    system = platform.system()
    try:
        if system == "Windows":
            if act in {"rewind", "forward"}:
                try:
                    import pyautogui
                except ImportError:
                    return "Seeking needs pyautogui. Run: python -m pip install pyautogui. Focus the video player first."
                key = "left" if act == "rewind" else "right"
                presses = max(1, min(12, round(seconds / 5)))
                pyautogui.FAILSAFE = True
                pyautogui.press(key, presses=presses, interval=0.08)
                return f"Pressed {key} {presses} time(s) in the focused player; seek behavior depends on that app."
            import ctypes
            vk = {"next": 0xB0, "previous": 0xB1, "stop": 0xB2, "play_pause": 0xB3}[act]
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk, 0, 0x0002, 0)
            return {"next": "Skipped to the next media track.", "previous": "Returned to the previous media track.", "stop": "Sent the media stop command.", "play_pause": "Toggled media play/pause."}[act]
        if system == "Darwin":
            if act in {"rewind", "forward"}:
                script = f'tell application "Music" to set player position to (player position {"-" if act == "rewind" else "+"} {seconds})'
            else:
                command = {"play_pause": "playpause", "next": "next track", "previous": "previous track", "stop": "pause"}[act]
                script = f'tell application "Music" to {command}'
            subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=7, check=True)
            return f"Sent '{act}' to Apple Music. If you use another player, make it the active player first."
        playerctl = shutil.which("playerctl")
        if playerctl:
            if act in {"rewind", "forward"}:
                result = subprocess.run([playerctl, "position"], capture_output=True, text=True, timeout=5, check=True)
                position = max(0.0, float(result.stdout.strip()) + (seconds if act == "forward" else -seconds))
                subprocess.run([playerctl, "position", str(position)], capture_output=True, text=True, timeout=5, check=True)
            else:
                action_map = {"play_pause": "play-pause", "next": "next", "previous": "previous", "stop": "stop"}
                subprocess.run([playerctl, action_map[act]], capture_output=True, text=True, timeout=5, check=True)
            return f"Media command completed: {act.replace('_', ' ')}."
        if act in {"rewind", "forward"}:
            try:
                import pyautogui
                key = "left" if act == "rewind" else "right"
                pyautogui.press(key, presses=max(1, min(12, round(seconds / 5))), interval=0.08)
                return f"Pressed {key} to seek in the focused player; behavior depends on that app."
            except ImportError:
                pass
        return "Linux media control needs playerctl. Install it with your distribution's package manager."
    except Exception as exc:
        return f"Media control failed: {exc}. Check that a media player is running."


def web_search(query: str) -> str:
    """Search the web for current information, recent events, prices, scores, weather, releases, or facts that may have changed. Args: query, a short search-engine query."""
    blocked = _automation_guard("allow_web_actions")
    if blocked:
        return blocked
    try:
        from ddgs import DDGS
    except ImportError:
        return "Web search isn't installed. Run: pip install ddgs"
    lines: list[str] = []
    try:
        with DDGS() as ddgs:
            try:
                for result in ddgs.news(query, max_results=4):
                    date = (result.get("date") or "")[:10]
                    lines.append(f"[news {date}] {result.get('title', '')}: {(result.get('body') or '')[:300]}")
            except Exception:
                pass
            for result in ddgs.text(query, max_results=5):
                lines.append(f"{result.get('title', '')}: {(result.get('body') or '')[:300]}")
    except Exception as exc:
        return f"Search failed: {exc}"
    if not lines:
        return "No results found."
    return f"Search results (today is {datetime.now().astimezone().strftime('%A %d %B %Y')}):\n" + "\n".join(lines)


WHATSAPP_CONTACTS = {"brother": "Ali Bhai", "mom": "Mama"}  # Edit to match saved contact names
WHATSAPP_ALLOW_ANY_NAME = True
WHATSAPP_PRESS_SEND = True
WHATSAPP_OPEN_DELAY = 3.0
WHATSAPP_SEARCH_DELAY = 1.2


def send_whatsapp_message(contact: str, message: str, confirmed: bool = False) -> str:
    """Send a WhatsApp message using the desktop app. Must first read back the exact contact and message and ask for explicit confirmation; only then call with confirmed=true. Args: contact, message (max 500 chars), confirmed."""
    blocked = _automation_guard("allow_app_control")
    if blocked:
        return blocked
    # Only a real JSON boolean True counts as confirmation; strings like "true" do not.
    confirmed = confirmed is True
    key = (contact or "").lower().strip()
    if key in WHATSAPP_CONTACTS:
        search_name = WHATSAPP_CONTACTS[key]
    elif WHATSAPP_ALLOW_ANY_NAME and (contact or "").strip():
        search_name = contact.strip()
    else:
        return f"I can't message '{contact}'. Known nicknames: {', '.join(WHATSAPP_CONTACTS)}"
    message = (message or "").strip()
    if not message:
        return "No message provided."
    if len(message) > 500:
        return "WhatsApp messages are limited to 500 characters."
    if not confirmed:
        return f"Not sent yet. Ask the user: send '{message}' to {search_name} on WhatsApp? Call again with confirmed=true only after explicit yes."
    system = platform.system()
    if system not in ("Windows", "Darwin"):
        return "WhatsApp desktop automation only supports Windows and macOS."
    try:
        import pyautogui
        import pyperclip
    except ImportError:
        return "Keyboard automation isn't installed. Run: pip install pyautogui pyperclip"
    pyautogui.FAILSAFE = True
    modifier = "command" if system == "Darwin" else "ctrl"
    old_clipboard = None
    try:
        try:
            old_clipboard = pyperclip.paste()
        except Exception:
            pass
        subprocess.Popen(["cmd", "/c", "start", "", "whatsapp:"], shell=False) if system == "Windows" else subprocess.Popen(["open", "-a", "WhatsApp"])
        time.sleep(WHATSAPP_OPEN_DELAY)
        pyautogui.hotkey(modifier, "f")
        time.sleep(0.5)
        pyperclip.copy(search_name)
        pyautogui.hotkey(modifier, "v")
        time.sleep(WHATSAPP_SEARCH_DELAY)
        pyautogui.press("enter")
        time.sleep(0.8)
        pyperclip.copy(message)
        pyautogui.hotkey(modifier, "v")
        time.sleep(0.4)
        if WHATSAPP_PRESS_SEND:
            pyautogui.press("enter")
            return f"Sent your message to {search_name} on WhatsApp."
        return f"Typed the message for {search_name} but did not press send (test mode)."
    except Exception as exc:
        return f"WhatsApp automation failed: {exc}"
    finally:
        if old_clipboard is not None:
            try:
                pyperclip.copy(old_clipboard)
            except Exception:
                pass


TOOLS = [get_current_time, open_website, search_google, open_app, open_path, type_text,
         press_hotkey, create_text_file, organize_downloads, organize_folder, list_folder_contents, find_files,
         create_folder, move_file, copy_file, move_folder, rename_file, request_move_to_recycle_bin,
         request_permanent_delete, request_empty_recycle_bin, open_folder, control_volume, control_media,
         get_system_info, web_search, send_whatsapp_message]
TOOL_MAP = {fn.__name__: fn for fn in TOOLS}
_TYPE_MAP = {str: "STRING", int: "INTEGER", float: "NUMBER", bool: "BOOLEAN"}


def build_declaration(fn) -> dict:
    properties: dict[str, dict] = {}
    required: list[str] = []
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}
    for name, param in inspect.signature(fn).parameters.items():
        annotation = hints.get(name, param.annotation)
        if isinstance(annotation, str):
            annotation = {"str": str, "int": int, "float": float, "bool": bool}.get(annotation, str)
        properties[name] = {"type": _TYPE_MAP.get(annotation, "STRING")}
        if param.default is inspect.Parameter.empty:
            required.append(name)
    # Make the tool execution contract explicit: run_tools sends results after
    # each declared action finishes, so use blocking calls instead of relying on
    # the model's default behavior.
    declaration = {
        "name": fn.__name__,
        "description": inspect.getdoc(fn) or fn.__name__,
        "behavior": "BLOCKING",
    }
    if properties:
        declaration["parameters"] = {"type": "OBJECT", "properties": properties, "required": required}
    return declaration


TOOL_DECLARATIONS = [build_declaration(fn) for fn in TOOLS]

# ---------------------------------------------------------------------------
# Full GUI — original reactor-core sci-fi HUD, no third-party JS libraries
# ---------------------------------------------------------------------------
HTML = r'''<!DOCTYPE html>
<html lang="en" class="reference-ui-light">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#030a11"><title>NEXUS // PERSONAL AI</title>
<style>
:root{--a:#55e8ff;--a-rgb:85,232,255;--gold:#f6bb70;--bg:#030810;--panel:rgba(4,17,28,.78);--line:rgba(85,232,255,.2);--ink:#e4faff;--dim:#7597a8;--glow:rgba(85,232,255,.22);font-family:'Chakra Petch','Segoe UI',sans-serif;color:var(--ink);color-scheme:dark}
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;min-height:100%;overflow:hidden;background:var(--bg)}body{background:radial-gradient(ellipse at 50% 48%,#071b2b 0,#030a12 42%,#02050a 82%);isolation:isolate}body:before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.36;background-image:linear-gradient(rgba(var(--a-rgb),.04) 1px,transparent 1px),linear-gradient(90deg,rgba(var(--a-rgb),.04) 1px,transparent 1px);background-size:34px 34px;mask-image:radial-gradient(ellipse at center,#000 0,transparent 84%)}body:after{content:'';position:fixed;inset:0;z-index:8;pointer-events:none;box-shadow:inset 0 0 115px rgba(0,0,0,.65)}canvas{position:fixed;inset:0;width:100%;height:100%;z-index:1;pointer-events:none}
button,input,select{font:inherit}button{cursor:pointer;color:var(--ink);border:1px solid rgba(var(--a-rgb),.32);background:linear-gradient(180deg,rgba(8,35,49,.9),rgba(3,13,22,.94));padding:10px 15px;border-radius:5px;font-size:.68rem;letter-spacing:.12em;text-transform:uppercase;transition:border-color .18s,background .18s,box-shadow .18s,transform .18s}button:hover{border-color:var(--a);box-shadow:0 0 20px var(--glow);background:rgba(var(--a-rgb),.13);transform:translateY(-1px)}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--a);outline-offset:3px}button:disabled{opacity:.45;cursor:not-allowed;transform:none}.low-power .panel{backdrop-filter:none;box-shadow:none}.low-power body:before{opacity:.18}.low-power body:after{box-shadow:none}.low-power button:hover{box-shadow:none;transform:none}.low-power .brandmark,.low-power #start .bigmark{box-shadow:inset 0 0 8px var(--glow)}
#app{position:relative;z-index:2;height:100%;width:100%}.topbar{position:absolute;left:0;right:0;top:0;height:76px;padding:12px 20px;display:flex;align-items:center;border-bottom:1px solid rgba(var(--a-rgb),.12);background:linear-gradient(180deg,rgba(2,8,14,.76),rgba(2,8,14,.13));z-index:4}.brand{display:flex;align-items:center;gap:12px;min-width:240px}.brandmark{width:46px;height:46px;border:1px solid rgba(var(--a-rgb),.76);border-radius:50%;display:grid;place-items:center;position:relative;box-shadow:0 0 22px var(--glow),inset 0 0 15px var(--glow)}.brandmark:before,.brandmark:after{content:'';position:absolute;border:1px dashed rgba(var(--a-rgb),.45);border-radius:50%;inset:5px}.brandmark:after{inset:-5px;border-style:solid;border-color:transparent var(--a) transparent transparent;animation:spin 8s linear infinite}.brandmark svg{width:28px;height:28px;filter:drop-shadow(0 0 5px var(--a))}.brandname{font-size:1.12rem;font-weight:600;letter-spacing:.24em;line-height:1.1}.brandtag{font: .57rem 'Share Tech Mono',monospace;letter-spacing:.16em;color:var(--dim);margin-top:5px}.top-center{position:absolute;left:50%;transform:translateX(-50%);display:flex;gap:24px;align-items:center;white-space:nowrap}.top-item{display:flex;align-items:center;gap:8px;font: .61rem 'Share Tech Mono',monospace;letter-spacing:.12em;color:#a8c8d5}.led{width:6px;height:6px;background:var(--a);border-radius:50%;box-shadow:0 0 9px var(--a);flex:none}.led.amber{background:var(--gold);box-shadow:0 0 8px var(--gold)}.top-right{margin-left:auto;display:flex;gap:10px;align-items:center}.status-chip{display:flex;gap:8px;align-items:center;border:1px solid var(--line);border-radius:4px;padding:8px 10px;background:rgba(2,12,20,.7);font: .62rem 'Share Tech Mono',monospace;text-transform:uppercase;letter-spacing:.08em}.iconbtn{width:36px;height:36px;padding:0;display:grid;place-items:center;font-size:1rem;letter-spacing:0}
.side{position:absolute;top:93px;bottom:89px;width:clamp(205px,19vw,270px);display:flex;flex-direction:column;gap:10px;overflow:hidden;z-index:3}.left{left:18px}.right{right:18px}.panel{background:linear-gradient(145deg,rgba(5,23,36,.86),rgba(3,11,19,.72));border:1px solid rgba(var(--a-rgb),.18);border-radius:5px;position:relative;padding:13px 14px;box-shadow:inset 0 1px rgba(255,255,255,.025),0 10px 25px rgba(0,0,0,.16);backdrop-filter:blur(12px);min-height:0}.panel:before{content:'';position:absolute;width:22px;height:2px;left:12px;top:-1px;background:var(--a);box-shadow:0 0 8px var(--a)}.eyebrow{display:flex;align-items:center;justify-content:space-between;gap:8px;color:#8eafbe;text-transform:uppercase;font: .61rem 'Share Tech Mono',monospace;letter-spacing:.12em;margin-bottom:11px}.eyebrow span:last-child{font-size:.55rem;color:#567c8e}.clock{font:clamp(1.7rem,2vw,2.35rem) 'Share Tech Mono',monospace;letter-spacing:.04em;color:#e6fbff;text-shadow:0 0 16px var(--glow);font-variant-numeric:tabular-nums}.date{font-size:.72rem;color:#9ab7c5;margin-top:5px}.timezone{font:.56rem 'Share Tech Mono',monospace;color:var(--a);margin-top:10px;letter-spacing:.13em}.weather-top{display:flex;align-items:center;gap:12px}.weather-icon{font-size:2.15rem;filter:drop-shadow(0 0 9px var(--glow));width:48px;text-align:center}.temp{font:1.85rem 'Share Tech Mono',monospace;line-height:1.1}.condition{font-size:.72rem;color:#a5c1ce;margin-top:3px;text-transform:capitalize}.city{font:.6rem 'Share Tech Mono',monospace;letter-spacing:.14em;color:var(--a);margin-top:13px;text-transform:uppercase}.weather-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:13px}.metric{padding:8px 9px;border:1px solid rgba(var(--a-rgb),.1);background:rgba(0,8,14,.28)}.metric small{font:.54rem 'Share Tech Mono',monospace;color:#668b9c;display:block;text-transform:uppercase;letter-spacing:.1em;margin-bottom:5px}.metric strong{font:.82rem 'Share Tech Mono',monospace;color:#d5f4ff;font-weight:400}.sysrow{display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:.7rem;color:#819fae;margin-top:10px}.sysrow b{font: .68rem 'Share Tech Mono',monospace;font-weight:400;color:#d0eef8}.meter{height:3px;background:rgba(var(--a-rgb),.12);margin-top:7px;overflow:hidden}.meter span{display:block;height:100%;width:0;background:var(--a);box-shadow:0 0 10px var(--a);transition:width .4s}.mini-note{font:.56rem 'Share Tech Mono',monospace;line-height:1.6;color:#5e8191;margin-top:10px}
.right{gap:10px}.conversation{display:flex;flex-direction:column;min-height:0;flex:1}.feed{overflow:auto;min-height:70px;display:flex;flex-direction:column;gap:10px;padding-right:4px;scrollbar-width:thin;scrollbar-color:rgba(var(--a-rgb),.24) transparent}.feed:empty:after{content:'SYSTEM READY\A\AAwaiting voice input';white-space:pre-wrap;text-align:center;margin:auto;color:#6f94a4;font:.65rem 'Share Tech Mono',monospace;line-height:2}.logline{border-left:2px solid rgba(var(--a-rgb),.7);padding:2px 0 2px 9px;font-size:.68rem;line-height:1.5;color:#d4edf6;overflow-wrap:anywhere;white-space:pre-wrap}.logline.you{border-color:#577384;color:#a4bbc8}.logline.tool{border-color:var(--gold);color:#f6d0a1;font-size:.63rem}.logline.sys{border-color:var(--a);color:#83b4c7;font: .6rem 'Share Tech Mono',monospace}.logline.hide{display:none}.automation{flex:0 0 auto}.automation-state{font-size:.76rem;color:#c2edf6}.automation-last{font:.59rem 'Share Tech Mono',monospace;color:#789eae;line-height:1.5;margin-top:8px;overflow-wrap:anywhere}.count{color:var(--a);font: .72rem 'Share Tech Mono',monospace}
#core-title{position:absolute;z-index:2;left:50%;top:49%;transform:translate(-50%,-50%);width:220px;text-align:center;pointer-events:none;text-shadow:0 0 18px var(--glow)}#core-title strong{display:block;letter-spacing:.33em;font-size:.9rem;font-weight:500;padding-left:.33em}#core-state{display:flex;justify-content:center;align-items:center;gap:8px;margin-top:9px;font: .59rem 'Share Tech Mono',monospace;letter-spacing:.13em;color:#91c2d1;text-transform:uppercase}#core-state .led{width:5px;height:5px}#caption{position:absolute;z-index:3;left:50%;bottom:75px;transform:translateX(-50%);width:min(540px,42vw);min-height:25px;text-align:center;font-size:.8rem;line-height:1.5;color:#dff8ff;text-shadow:0 0 16px var(--glow);pointer-events:none}#caption.you{color:#92aab7;text-shadow:none}#controls{position:absolute;z-index:4;left:50%;bottom:17px;transform:translateX(-50%);display:flex;align-items:center;justify-content:center;gap:9px;white-space:nowrap}#start{position:absolute;z-index:10;inset:0;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:17px;text-align:center;padding:25px;background:radial-gradient(ellipse at 50% 48%,rgba(6,31,46,.97),rgba(2,8,14,.985) 52%,#010307 95%)}#start:before{content:'';position:absolute;width:min(42vw,350px);aspect-ratio:1;border-radius:50%;border:1px solid rgba(var(--a-rgb),.17);box-shadow:0 0 110px var(--glow),inset 0 0 60px rgba(var(--a-rgb),.07);animation:breathe 5s ease-in-out infinite;pointer-events:none}#start .bigmark{z-index:1;width:74px;height:74px;border-radius:50%;border:1px solid var(--a);display:grid;place-items:center;box-shadow:0 0 35px var(--glow),inset 0 0 26px var(--glow)}#start .bigmark svg{width:45px;filter:drop-shadow(0 0 8px var(--a))}#start h1{z-index:1;font-size:clamp(2.1rem,4vw,3.3rem);font-weight:400;letter-spacing:.32em;padding-left:.32em;margin:0;text-shadow:0 0 35px var(--glow)}#start p{z-index:1;color:#8bacbc;max-width:500px;font-size:.84rem;line-height:1.8;margin:0}#start button{z-index:1;padding:13px 28px;letter-spacing:.16em}#start .sub{font:.58rem 'Share Tech Mono',monospace;letter-spacing:.15em;color:#628799;text-transform:uppercase}#err{z-index:1;color:#ff9d9d;max-width:600px;font-size:.77rem;line-height:1.6}
#settings-scrim{position:absolute;inset:0;z-index:19;background:rgba(0,5,10,.68);backdrop-filter:blur(4px);opacity:0;pointer-events:none;transition:opacity .25s}#settings-scrim.open{opacity:1;pointer-events:auto}#settings{position:absolute;z-index:20;right:0;top:0;bottom:0;width:min(420px,94vw);padding:22px 22px 28px;background:linear-gradient(155deg,rgba(5,22,34,.99),rgba(2,9,16,.99));border-left:1px solid rgba(var(--a-rgb),.34);box-shadow:-20px 0 60px rgba(0,0,0,.45);transform:translateX(103%);transition:transform .28s ease;display:flex;flex-direction:column;gap:15px;overflow-y:auto}#settings.open{transform:translateX(0)}.settings-head{display:flex;align-items:center;justify-content:space-between;padding-bottom:15px;border-bottom:1px solid rgba(var(--a-rgb),.17)}.settings-head h2{margin:0;font-size:1rem;font-weight:500;letter-spacing:.18em}.settings-head p{color:#7494a5;font:.58rem 'Share Tech Mono',monospace;margin:6px 0 0}.setting{padding:13px;border:1px solid rgba(var(--a-rgb),.14);background:rgba(0,8,14,.28);border-radius:4px}.setting label,.setting .label{display:flex;justify-content:space-between;gap:12px;align-items:center;font-size:.74rem;color:#d4ebf4;margin-bottom:10px}.setting small{display:block;color:#7394a5;font-size:.61rem;line-height:1.5;margin-top:8px}.setting select,.setting input[type=text]{width:100%;padding:10px;background:#04111b;color:var(--ink);border:1px solid rgba(var(--a-rgb),.25);border-radius:3px;font-size:.72rem}.setting input[type=range]{width:100%;accent-color:var(--a)}.setting input[type=checkbox]{accent-color:var(--a);width:16px;height:16px}.swatches{display:flex;gap:9px}.swatch{width:30px;height:30px;border-radius:50%;padding:0;border:2px solid rgba(255,255,255,.22);box-shadow:none}.swatch.active{outline:2px solid #effcff;outline-offset:3px}.settings-actions{display:flex;gap:8px;margin-top:auto}.settings-actions button{flex:1}.muted{color:#7596a7!important;font-size:.61rem!important}
.toast{position:absolute;z-index:30;left:50%;top:88px;transform:translate(-50%,-10px);opacity:0;pointer-events:none;border:1px solid rgba(var(--a-rgb),.4);background:rgba(2,14,22,.95);padding:10px 14px;border-radius:4px;color:#dff9ff;font:.66rem 'Share Tech Mono',monospace;transition:opacity .2s,transform .2s;max-width:min(90vw,560px);text-align:center}.toast.show{opacity:1;transform:translate(-50%,0)}
@keyframes spin{to{transform:rotate(360deg)}}@keyframes breathe{0%,100%{transform:scale(.96);opacity:.45}50%{transform:scale(1.04);opacity:.95}}
@media(max-width:1120px){.top-center{gap:12px}.top-item{font-size:.54rem}.side{width:220px}.left{left:12px}.right{right:12px}.panel{padding:11px}.weather-grid{gap:5px}.brand{min-width:195px}}
@media(max-width:850px){.top-center{display:none}.side{top:auto;bottom:78px;width:calc(50% - 22px);max-height:200px;gap:6px}.left{left:9px}.right{right:9px}.left .system-panel{display:none}.panel{padding:9px}.eyebrow{margin-bottom:7px;font-size:.54rem}.clock{font-size:1.4rem}.weather-grid{margin-top:8px}.metric{padding:6px}.conversation{height:175px;flex:0 0 175px}.automation{display:none}#core-title{top:39%;}#caption{width:calc(100% - 20px);bottom:55px;font-size:.7rem}#controls{bottom:10px;gap:6px}#controls button{padding:8px 10px;font-size:.55rem}.topbar{height:67px;padding:9px 12px}.brandmark{width:39px;height:39px}.brandname{font-size:.96rem}.brandtag{font-size:.5rem}.brand{min-width:0;gap:9px}.top-right{gap:6px}.status-chip{font-size:.55rem;padding:7px}.iconbtn{width:32px;height:32px}}
@media(max-width:490px){.brandtag{display:none}.topbar{padding:8px}.status-chip{padding:6px;font-size:.5rem}.side{width:calc(50% - 13px);bottom:80px;max-height:168px}.right{height:168px}.left .weather-panel{padding:8px}.weather-icon{font-size:1.45rem;width:30px}.temp{font-size:1.3rem}.condition{font-size:.58rem}.city{font-size:.5rem;margin-top:7px}.weather-grid{display:none}.clock{font-size:1.13rem}.date{font-size:.58rem}.timezone{font-size:.48rem;margin-top:5px}.conversation{height:168px;flex-basis:168px}.feed{gap:7px}.logline{font-size:.58rem;padding-left:6px}.eyebrow{font-size:.49rem;letter-spacing:.07em}.panel{padding:8px}.right{gap:5px}#core-title{top:35%;width:160px}#core-title strong{font-size:.73rem}#core-state{font-size:.48rem;margin-top:7px}#caption{bottom:52px;font-size:.64rem}#controls{gap:4px}#controls button{padding:8px 7px;font-size:.49rem;letter-spacing:.04em}#start h1{font-size:2rem}.brandmark{width:34px;height:34px}#settings{padding:17px;gap:10px}.setting{padding:10px}}
@media(max-height:620px) and (min-width:851px){.topbar{height:62px}.side{top:70px;bottom:66px;gap:6px}.panel{padding:8px 10px}.eyebrow{margin-bottom:6px}.clock{font-size:1.5rem}.weather-grid{margin-top:7px}.metric{padding:5px}.sysrow{margin-top:5px}#caption{bottom:52px}#controls{bottom:7px}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.001ms!important;animation-iteration-count:1!important;scroll-behavior:auto!important;transition:none!important}}

/* NEXUS visual layout — pure black, white/cyan holographic interface */
:root{--a:#8af7ff;--a-rgb:138,247,255;--gold:#d8ffff;--bg:#000;--panel:rgba(7,12,14,.91);--line:rgba(138,247,255,.19);--ink:#f1ffff;--dim:#82999c;--glow:rgba(138,247,255,.19);font-family:'Chakra Petch','Segoe UI',sans-serif;color:var(--ink);color-scheme:dark}
*{box-sizing:border-box}html,body{background:#000!important;color:var(--ink);width:100%;height:100%;overflow:hidden}body{background:#000!important;isolation:isolate}body:before{opacity:.12;background-image:linear-gradient(rgba(138,247,255,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(138,247,255,.035) 1px,transparent 1px);background-size:44px 44px;mask-image:radial-gradient(ellipse at 42% 48%,#000 0,transparent 78%)}body:after{box-shadow:inset 0 0 100px rgba(0,0,0,.72);z-index:8}#hud-canvas{z-index:1;pointer-events:none}#app{position:relative;z-index:2;width:100%;height:100%;overflow:hidden;background:transparent}
button,input,select{font:inherit}button{color:#eaffff;border:1px solid rgba(138,247,255,.2);background:rgba(8,15,17,.82);border-radius:10px;padding:9px 12px;letter-spacing:.04em;text-transform:none;box-shadow:none;backdrop-filter:blur(16px)}button:hover{border-color:rgba(138,247,255,.65);background:rgba(138,247,255,.07);box-shadow:0 0 18px rgba(138,247,255,.09);transform:translateY(-1px)}button:disabled{opacity:.36;cursor:not-allowed;transform:none;box-shadow:none}
.topbar{position:fixed;z-index:12;left:0;right:0;top:0;height:78px;padding:0 24px;display:grid;grid-template-columns:minmax(170px,1fr) auto minmax(190px,1fr);align-items:center;gap:18px;border-bottom:1px solid rgba(255,255,255,.07);background:linear-gradient(180deg,rgba(0,0,0,.94),rgba(0,0,0,.64));backdrop-filter:blur(18px)}
.thinking-pill{justify-self:start;display:flex;align-items:center;gap:12px;min-width:176px;padding:10px 14px;border:1px solid rgba(138,247,255,.16);border-radius:13px;background:rgba(7,12,13,.85);box-shadow:inset 0 0 24px rgba(138,247,255,.025)}.thinking-copy{display:flex;flex-direction:column;gap:3px;min-width:0}.thinking-copy strong{font-family:'Share Tech Mono',monospace;font-size:.75rem;letter-spacing:.22em;font-weight:400;color:#f7ffff}.thinking-copy small{font-family:'Share Tech Mono',monospace;font-size:.54rem;letter-spacing:.09em;color:#86a5a8;white-space:nowrap}.thinking-pill>.led{margin-left:auto}.thinking-wave{height:24px;width:24px;display:flex;align-items:center;justify-content:center;gap:2px}.thinking-wave i{display:block;width:2px;height:7px;border-radius:4px;background:#c7ffff;box-shadow:0 0 6px rgba(138,247,255,.65);animation:thinkingWave 1.15s ease-in-out infinite}.thinking-wave i:nth-child(2){height:15px;animation-delay:.12s}.thinking-wave i:nth-child(3){height:21px;animation-delay:.24s}.thinking-wave i:nth-child(4){height:12px;animation-delay:.36s}.thinking-wave i:nth-child(5){height:5px;animation-delay:.48s}
.floating-nav{justify-self:center;display:flex;align-items:center;gap:5px;padding:7px;border:1px solid rgba(255,255,255,.12);border-radius:17px;background:rgba(14,17,18,.84);box-shadow:0 9px 30px rgba(0,0,0,.25),inset 0 1px rgba(255,255,255,.045);backdrop-filter:blur(25px);white-space:nowrap}.palette-btn{display:flex;align-items:center;gap:9px;padding:9px 12px;border-color:rgba(255,255,255,.08);border-radius:11px;background:rgba(255,255,255,.035);color:#c4d3d4;font-size:.69rem}.search-glyph{font-size:1.2rem;line-height:.7;color:#a2fcff}.floating-nav kbd,.palette-modal kbd{font: .56rem 'Share Tech Mono',monospace;color:#88a0a2;border:1px solid rgba(255,255,255,.13);border-radius:5px;padding:3px 5px;background:rgba(255,255,255,.025)}.nav-divider{width:1px;height:24px;background:rgba(255,255,255,.1);margin:0 4px}.nav-link{border:0;background:transparent;color:#8d9b9c;font-size:.68rem;padding:9px 11px;border-radius:10px}.nav-link.active,.nav-link:hover{color:#f0ffff;background:rgba(255,255,255,.075);box-shadow:none;transform:none}
.top-right{justify-self:end;display:flex;align-items:center;gap:10px}.status-chip{display:flex;align-items:center;gap:8px;border:1px solid rgba(255,255,255,.09);padding:9px 11px;border-radius:10px;color:#9aafaf;font: .59rem 'Share Tech Mono',monospace;letter-spacing:.08em;background:rgba(8,12,13,.8);white-space:nowrap}.led{width:6px;height:6px;border-radius:50%;display:inline-block;background:#536568;box-shadow:0 0 7px rgba(138,247,255,.15);flex:0 0 auto}.led:not(.amber){transition:background .2s,box-shadow .2s}.status-chip .led:not(.amber),.thinking-pill .led:not(.amber){background:#9afcff;box-shadow:0 0 9px rgba(138,247,255,.75)}.led.amber{background:#fff!important;box-shadow:0 0 10px #fff!important}.gesture-toggle{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:10px;font-size:.63rem;color:#92a5a6;background:rgba(8,12,13,.78);border-color:rgba(255,255,255,.1);white-space:nowrap}.gesture-dot{width:5px;height:5px;background:#4b5b5c;border-radius:50%}.gesture-toggle[aria-pressed=true] .gesture-dot{background:#c8ffff;box-shadow:0 0 9px #a3fcff}.toggle-track{width:25px;height:14px;border-radius:99px;background:#202829;border:1px solid #354244;padding:2px;display:flex;align-items:center;transition:.2s}.toggle-track i{width:8px;height:8px;border-radius:50%;background:#768586;transition:.2s}.gesture-toggle[aria-pressed=true] .toggle-track{background:rgba(138,247,255,.18);border-color:rgba(138,247,255,.55)}.gesture-toggle[aria-pressed=true] .toggle-track i{transform:translateX(10px);background:#d9ffff;box-shadow:0 0 7px #8af7ff}
.led.amber{background:#fff}.left-dock{position:absolute;z-index:3;left:22px;top:103px;bottom:104px;width:224px;display:flex;flex-direction:column;gap:12px;overflow:auto;scrollbar-width:none;pointer-events:auto}.left-dock::-webkit-scrollbar{display:none}.glass-card{padding:14px 15px;border:1px solid rgba(255,255,255,.095);border-radius:15px;background:linear-gradient(145deg,rgba(8,13,14,.82),rgba(2,4,5,.85));box-shadow:0 15px 50px rgba(0,0,0,.27),inset 0 1px rgba(255,255,255,.025);backdrop-filter:blur(18px)}.card-eyebrow{display:flex;justify-content:space-between;align-items:center;gap:8px;color:#7e9698;font: .55rem 'Share Tech Mono',monospace;letter-spacing:.14em}.card-index{color:#4e6567}.clock{margin-top:15px;color:#f3ffff;font:300 2.1rem 'Chakra Petch',sans-serif;letter-spacing:.04em;line-height:1.06;font-variant-numeric:tabular-nums}.date{margin-top:8px;color:#a1b6b7;font-size:.68rem;line-height:1.4}.timezone{display:flex;align-items:center;gap:7px;margin-top:14px;padding-top:11px;border-top:1px solid rgba(255,255,255,.07);color:#6d8688;font: .52rem 'Share Tech Mono',monospace;letter-spacing:.035em}.tiny-live{display:inline-block;width:5px;height:5px;flex:0 0 auto;border-radius:50%;background:#a2ffff;box-shadow:0 0 7px rgba(138,247,255,.6)}.weather-card .card-eyebrow button{padding:1px 5px;border:0;background:transparent;color:#a1ffff;font-size:1rem}.weather-top{display:flex;align-items:center;gap:12px;margin-top:13px}.weather-icon{font-size:2.2rem;line-height:1;color:#dcffff;text-shadow:0 0 20px rgba(138,247,255,.25)}.temp{font-size:1.9rem;line-height:1;font-weight:300;letter-spacing:.03em;color:#f5ffff}.condition{font-size:.62rem;color:#9cb0b1;margin-top:4px}.city{margin-top:12px;color:#80a0a2;font: .55rem 'Share Tech Mono',monospace;letter-spacing:.08em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.weather-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:13px}.metric{border-top:1px solid rgba(255,255,255,.075);padding-top:7px;display:flex;flex-direction:column;gap:4px}.metric small{font-size:.57rem;color:#768c8e}.metric strong{font: .8rem 'Share Tech Mono',monospace;font-weight:400;color:#e1ffff}.weather-footer{display:flex;justify-content:space-between;gap:5px;margin-top:12px;padding-top:9px;border-top:1px solid rgba(255,255,255,.065);color:#506769;font: .48rem 'Share Tech Mono',monospace;letter-spacing:.04em}.telemetry-card{display:none}
#main-stage{position:absolute;z-index:2;inset:79px 0 90px;pointer-events:none}.orb-overline{position:absolute;left:calc((100vw - min(356px, 28vw))/2);top:23px;transform:translateX(-50%);display:flex;align-items:center;gap:9px;white-space:nowrap;color:#71898a;font: .53rem 'Share Tech Mono',monospace;letter-spacing:.18em}.orb-overline-line{width:50px;height:1px;background:linear-gradient(90deg,rgba(138,247,255,.0),rgba(138,247,255,.45))}.core-title-placeholder{display:none}#core-title{position:absolute;left:calc((100vw - min(356px, 28vw))/2);top:calc(50% + min(226px, 25vh));transform:translate(-50%,-50%);width:260px;text-align:center;color:#d8ffff;pointer-events:none;text-shadow:0 0 17px rgba(138,247,255,.15)}#core-title>strong{font: .9rem 'Share Tech Mono',monospace;font-weight:400;letter-spacing:.26em;color:#dfffff}#core-title>strong span{color:#789c9e}#core-state{display:flex;justify-content:center;align-items:center;gap:8px;margin-top:9px;color:#7c9b9d;font: .54rem 'Share Tech Mono',monospace;letter-spacing:.12em}#caption{position:absolute;left:calc((100vw - min(356px, 28vw))/2);top:calc(50% + min(270px, 30vh));transform:translateX(-50%);width:min(560px,52vw);min-height:25px;text-align:center;display:flex;align-items:center;justify-content:center;padding:0 12px;color:#8ba5a7;font-size:.73rem;line-height:1.45;letter-spacing:.035em;text-shadow:0 0 15px rgba(138,247,255,.1)}#caption.you{color:#aab7b8}#caption.nexus{color:#e3ffff}
.chat-panel{position:absolute;z-index:6;right:20px;top:93px;bottom:100px;width:min(356px,28vw);display:flex;flex-direction:column;min-width:285px;overflow:hidden;border:1px solid rgba(255,255,255,.105);border-radius:17px;background:linear-gradient(150deg,rgba(9,13,14,.96),rgba(2,4,5,.96));box-shadow:0 24px 70px rgba(0,0,0,.48),inset 0 1px rgba(255,255,255,.035);backdrop-filter:blur(22px)}.chat-header{height:67px;flex:0 0 auto;display:flex;align-items:center;justify-content:space-between;gap:9px;padding:0 15px;border-bottom:1px solid rgba(255,255,255,.075)}.chat-heading{display:flex;align-items:center;gap:10px}.chat-title-mark{width:29px;height:29px;border:1px solid rgba(138,247,255,.3);border-radius:9px;display:grid;place-items:center;color:#d4ffff;background:rgba(138,247,255,.04);font-size:1.05rem;box-shadow:inset 0 0 12px rgba(138,247,255,.04)}.chat-heading>div{display:flex;flex-direction:column;gap:4px}.chat-heading strong{font: .73rem 'Share Tech Mono',monospace;letter-spacing:.13em;color:#f1ffff}.chat-heading small{font: .49rem 'Share Tech Mono',monospace;letter-spacing:.07em;color:#627779}.new-chat-btn{font-size:.62rem;padding:7px 9px;border-color:rgba(255,255,255,.1);color:#bdcccc;background:rgba(255,255,255,.025);white-space:nowrap}.chat-tabs{height:43px;flex:0 0 auto;display:flex;align-items:stretch;gap:5px;padding:0 13px;border-bottom:1px solid rgba(255,255,255,.065)}.chat-tab{position:relative;padding:0 12px;border:0;border-radius:0;background:transparent;color:#667a7b;font: .57rem 'Share Tech Mono',monospace;letter-spacing:.15em}.chat-tab.active{color:#dfffff;background:transparent}.chat-tab.active:after{content:'';position:absolute;left:10px;right:10px;bottom:-1px;height:2px;background:#bbffff;box-shadow:0 0 10px rgba(138,247,255,.45)}.chat-tabs #feed-count{align-self:center;margin-left:auto;color:#526768;font: .48rem 'Share Tech Mono',monospace;letter-spacing:.05em;white-space:nowrap}.chat-feed-wrap{position:relative;flex:1;min-height:0;display:flex;overflow:hidden}.feed{flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;align-items:stretch;gap:10px;padding:15px 13px 18px;scrollbar-width:thin;scrollbar-color:rgba(138,247,255,.2) transparent}.logline{max-width:94%;position:relative;margin:0;padding:10px 11px;border:1px solid rgba(255,255,255,.065);border-radius:13px;background:rgba(255,255,255,.025);color:#c4d4d5;font-size:.69rem;line-height:1.55;white-space:pre-wrap;overflow-wrap:anywhere}.logline.you{align-self:flex-end;border-color:rgba(138,247,255,.17);border-radius:13px 13px 3px 13px;background:linear-gradient(120deg,rgba(138,247,255,.10),rgba(138,247,255,.035));color:#e4ffff}.logline.nexus{align-self:flex-start;border-color:rgba(255,255,255,.08);border-radius:3px 13px 13px 13px;background:rgba(255,255,255,.035);color:#e9f7f7}.logline.sys{max-width:100%;align-self:stretch;text-align:center;padding:7px 9px;border:1px solid rgba(255,255,255,.045);border-radius:7px;background:rgba(255,255,255,.018);color:#6f8889;font: .55rem 'Share Tech Mono',monospace;letter-spacing:.025em}.logline.tool{max-width:100%;border-color:rgba(138,247,255,.12);border-left:2px solid rgba(138,247,255,.6);border-radius:7px;background:rgba(138,247,255,.035);color:#a8d4d5;font: .57rem 'Share Tech Mono',monospace}.logline.hide{display:none!important}.chat-automation{display:flex;align-items:flex-start;gap:9px;flex:0 0 auto;padding:10px 13px;border-top:1px solid rgba(255,255,255,.06);background:rgba(255,255,255,.012)}.automation-spark{color:#b7ffff;font-size:.9rem;margin-top:1px}.automation-copy{min-width:0;display:flex;flex-direction:column;gap:4px}.automation-copy strong{font: .51rem 'Share Tech Mono',monospace;letter-spacing:.06em;color:#91b3b4}.automation-copy small{color:#5f7576;font-size:.56rem;line-height:1.4;overflow-wrap:anywhere}.side-composer{display:flex;align-items:center;gap:7px;flex:0 0 auto;margin:9px 11px 0;padding:5px 5px 5px 10px;border:1px solid rgba(255,255,255,.105);border-radius:12px;background:rgba(255,255,255,.025)}.side-composer input{width:100%;min-width:0;border:0;outline:0;background:transparent;color:#eaffff;font-size:.68rem;padding:7px 0}.side-composer input::placeholder{color:#596d6e}.side-composer button{width:31px;height:31px;display:grid;place-items:center;padding:0;border:1px solid rgba(138,247,255,.22);border-radius:9px;background:rgba(138,247,255,.08);color:#e5ffff;font-size:1.05rem}.chat-footnote{display:flex;align-items:center;gap:6px;flex:0 0 auto;padding:10px 13px 11px;color:#4f6364;font: .45rem 'Share Tech Mono',monospace;letter-spacing:.035em;white-space:nowrap;overflow:hidden}.chat-footnote #session-label{overflow:hidden;text-overflow:ellipsis}.history-view{flex:1;min-height:0;overflow:auto;padding:13px;display:flex;flex-direction:column;gap:9px;scrollbar-width:thin;scrollbar-color:rgba(138,247,255,.2) transparent}.history-card{display:block;width:100%;text-align:left;padding:12px;border-radius:12px;background:rgba(255,255,255,.025);border:1px solid rgba(255,255,255,.075)}.history-card strong{display:block;font-size:.68rem;font-weight:500;color:#e3ffff;margin-bottom:6px}.history-card small{display:block;font-size:.57rem;color:#7d9697;line-height:1.45}.history-empty{padding:25px 12px;text-align:center;color:#738b8c;font-size:.68rem;line-height:1.6}
.bottom-composer-wrap{position:absolute;z-index:7;left:calc((100vw - min(356px, 28vw))/2);transform:translateX(-50%);bottom:14px;width:min(780px,calc(100vw - min(356px, 28vw) - 280px));min-width:440px}.composer-context{display:flex;align-items:center;gap:8px;padding:0 10px 8px;color:#739192;font: .52rem 'Share Tech Mono',monospace;letter-spacing:.12em}.context-dot{width:5px;height:5px;border-radius:50%;background:#9afaff;box-shadow:0 0 7px rgba(138,247,255,.5)}.composer-context-right{margin-left:auto;color:#435657}.bottom-composer{display:flex;align-items:center;gap:11px;min-height:62px;padding:8px 9px 8px 17px;border:1px solid rgba(255,255,255,.16);border-radius:19px;background:linear-gradient(105deg,rgba(16,21,22,.9),rgba(5,8,9,.94));box-shadow:0 15px 46px rgba(0,0,0,.52),inset 0 1px rgba(255,255,255,.06),0 0 24px rgba(138,247,255,.025);backdrop-filter:blur(22px)}.composer-leading{font-size:1.2rem;color:#78999a}.bottom-composer input{flex:1;min-width:0;outline:none;border:0;background:transparent;color:#eaffff;font-size:.78rem;padding:13px 0}.bottom-composer input::placeholder{color:#647677}.composer-actions{display:flex;align-items:center;gap:6px}.composer-icon{width:35px;height:35px;display:grid;place-items:center;padding:0;border:1px solid rgba(255,255,255,.09);border-radius:10px;background:rgba(255,255,255,.025);color:#93b0b1;font-size:.86rem}.composer-icon.danger{color:#ffb9b9;border-color:rgba(255,100,100,.35)}.stop-icon{font-size:.65rem}.voice-start{padding:10px 12px;border-radius:10px;border:1px solid rgba(138,247,255,.3);background:rgba(138,247,255,.08);color:#dfffff;font: .58rem 'Share Tech Mono',monospace;letter-spacing:.1em;white-space:nowrap}.send-button{width:38px;height:38px;border-radius:11px;padding:0;display:grid;place-items:center;background:#ddffff;color:#020707;border:1px solid #ddffff;font-size:1.25rem;font-weight:500;box-shadow:0 0 20px rgba(138,247,255,.16)}.send-button:hover{background:#fff;color:#000;box-shadow:0 0 22px rgba(138,247,255,.25)}.composer-hint{display:flex;justify-content:space-between;gap:10px;padding:8px 10px 0;color:#3e5354;font: .45rem 'Share Tech Mono',monospace;letter-spacing:.06em}
#settings-scrim{position:fixed;z-index:20;inset:0;background:rgba(0,0,0,.68);backdrop-filter:blur(5px)}#settings{position:fixed;z-index:21;top:0;right:0;bottom:0;height:100%;max-height:100%;width:min(420px,94vw);overflow:auto;background:linear-gradient(150deg,#080d0e,#020404);border-left:1px solid rgba(138,247,255,.17);box-shadow:-25px 0 80px rgba(0,0,0,.65);padding:20px 23px;scrollbar-width:thin;scrollbar-color:rgba(138,247,255,.2) transparent}.settings-head h2{font: .9rem 'Share Tech Mono',monospace;letter-spacing:.16em;color:#eaffff}.settings-head p{font: .55rem 'Share Tech Mono',monospace;letter-spacing:.09em;color:#738d8e;margin-top:7px}.setting{padding:17px 0;border-bottom:1px solid rgba(255,255,255,.07)}.setting label,.setting .label{display:flex;align-items:center;justify-content:space-between;gap:10px;color:#d6e6e6;font-size:.72rem}.setting small{display:block;color:#718687;font-size:.61rem;line-height:1.55;margin-top:8px}.setting input[type=text],.setting select{width:100%;margin-top:10px;padding:10px 11px;color:#eaffff;background:#090f10;border:1px solid rgba(255,255,255,.13);border-radius:9px}.setting input[type=range]{width:100%;margin-top:15px;accent-color:#a8ffff}.setting input[type=checkbox]{accent-color:#9cfaff;width:15px;height:15px}.setting .screen-vision-status{font:.55rem 'Share Tech Mono',monospace;letter-spacing:.06em;color:#9cfaff}.swatches{display:flex;gap:10px;margin-top:13px}.swatch{width:27px;height:27px;padding:0;border-radius:50%;border:2px solid transparent;box-shadow:0 0 0 1px rgba(255,255,255,.25)}.swatch.active{outline:2px solid #dfffff;outline-offset:3px}.muted{font: .5rem 'Share Tech Mono',monospace;color:#718687}.settings-actions{display:flex;justify-content:flex-end;gap:9px;padding:19px 0 8px}.settings-actions button{font: .58rem 'Share Tech Mono',monospace;letter-spacing:.08em}.settings-actions #settings-apply{color:#001011;background:#dfffff;border-color:#dfffff}
#start{position:fixed;z-index:30;inset:0;display:none;align-items:center;justify-content:center;flex-direction:column;gap:14px;background:rgba(0,0,0,.97);padding:28px;text-align:center}#start h1{font-size:2rem;letter-spacing:.25em;font-weight:400}#start p{max-width:31em;font-size:.78rem;line-height:1.7;color:#8aa4a5}#start .bigmark{width:62px;height:62px;color:#dfffff;filter:drop-shadow(0 0 13px rgba(138,247,255,.4))}#start .bigmark svg{width:100%;height:100%}#start #err{max-width:37em;color:#ffd0d0}.sub{font: .54rem 'Share Tech Mono',monospace;letter-spacing:.13em;color:#4f6b6d}
.palette-scrim{position:fixed;inset:0;z-index:40;display:none;align-items:flex-start;justify-content:center;padding:13vh 18px 20px;background:rgba(0,0,0,.7);backdrop-filter:blur(9px)}.palette-scrim.open{display:flex}.palette-modal{width:min(540px,100%);border:1px solid rgba(255,255,255,.15);border-radius:17px;background:linear-gradient(140deg,#0c1112,#030505);box-shadow:0 28px 100px rgba(0,0,0,.75),0 0 35px rgba(138,247,255,.04);overflow:hidden}.palette-search-row{display:flex;align-items:center;gap:12px;padding:16px;border-bottom:1px solid rgba(255,255,255,.09);color:#baffff}.palette-search-row>span{font-size:1.45rem}.palette-search-row input{flex:1;min-width:0;background:transparent;border:0;outline:0;color:#f2ffff;font-size:.82rem}.palette-search-row input::placeholder{color:#627677}.palette-label{padding:13px 16px 6px;color:#5c7576;font: .51rem 'Share Tech Mono',monospace;letter-spacing:.15em}.palette-options{padding:6px 8px 10px;display:flex;flex-direction:column;gap:3px}.command-option{display:flex;align-items:center;gap:12px;width:100%;padding:10px 11px;text-align:left;background:transparent;border-color:transparent;border-radius:10px}.command-option>span:first-child{width:30px;height:30px;display:grid;place-items:center;border:1px solid rgba(255,255,255,.08);border-radius:8px;color:#baffff}.command-option>span:nth-child(2){display:flex;flex-direction:column;gap:4px;flex:1}.command-option strong{font-size:.7rem;font-weight:500;color:#e3ffff}.command-option small{font-size:.59rem;color:#738788}.command-option kbd{opacity:.5}.command-option:hover{background:rgba(255,255,255,.045);border-color:rgba(255,255,255,.05);transform:none;box-shadow:none}.palette-bottom{display:flex;justify-content:space-between;gap:8px;padding:12px 16px;border-top:1px solid rgba(255,255,255,.07);color:#4d6465;font: .48rem 'Share Tech Mono',monospace;letter-spacing:.04em}.toast{position:fixed;z-index:60;left:50%;top:90px;max-width:min(650px,90vw);transform:translate(-50%,-9px);opacity:0;pointer-events:none;padding:11px 16px;border:1px solid rgba(138,247,255,.25);border-radius:12px;background:rgba(5,11,12,.95);color:#dfffff;font: .67rem 'Share Tech Mono',monospace;line-height:1.45;box-shadow:0 13px 42px rgba(0,0,0,.45);transition:opacity .2s,transform .2s;backdrop-filter:blur(16px)}.toast.show{opacity:1;transform:translate(-50%,0)}[hidden]{display:none!important}
@keyframes thinkingWave{0%,100%{transform:scaleY(.45);opacity:.55}50%{transform:scaleY(1);opacity:1}}
@media(max-width:1250px){.topbar{padding:0 14px;grid-template-columns:minmax(150px,1fr) auto minmax(170px,1fr);gap:8px}.thinking-pill{min-width:153px;padding:8px 10px;gap:8px}.thinking-copy small{max-width:108px;overflow:hidden;text-overflow:ellipsis}.floating-nav{gap:1px}.palette-btn{padding:8px}.left-dock{left:13px;width:203px}.chat-panel{right:13px}.bottom-composer-wrap{min-width:380px;width:min(700px,calc(100vw - min(356px,28vw) - 240px))}}
@media(max-width:900px){.topbar{height:68px;grid-template-columns:auto 1fr auto;padding:0 12px}.thinking-pill{min-width:0;border:0;background:transparent;padding:5px}.thinking-copy small{display:none}.thinking-copy strong{font-size:.68rem}.thinking-wave{width:20px}.floating-nav{justify-self:center}.palette-btn span:not(.search-glyph),.palette-btn kbd{display:none}.palette-btn{width:38px;height:37px;justify-content:center;padding:0}.nav-divider{margin:0 2px}.nav-link{padding:9px 8px;font-size:.6rem}.status-chip{display:none}.gesture-toggle{padding:7px 8px;gap:5px}.left-dock{top:78px;left:10px;right:10px;bottom:auto;width:auto;flex-direction:row;gap:8px;overflow:visible}.time-card,.weather-card{flex:1;min-width:0;padding:10px 11px}.time-card .clock{margin-top:8px;font-size:1.35rem}.time-card .date{margin-top:4px}.time-card .timezone{margin-top:8px;padding-top:7px;font-size:.45rem}.weather-top{margin-top:7px;gap:7px}.weather-icon{font-size:1.5rem}.temp{font-size:1.3rem}.weather-grid{margin-top:8px;gap:5px}.metric{padding-top:5px}.metric small{font-size:.5rem}.metric strong{font-size:.65rem}.weather-card .city{margin-top:6px;font-size:.47rem}.weather-footer{margin-top:6px;padding-top:6px;font-size:.43rem}.telemetry-card{display:none}.chat-panel{top:auto;right:12px;bottom:90px;height:38vh;width:min(390px,calc(100vw - 24px));min-width:0}.bottom-composer-wrap{left:50%;bottom:13px;width:calc(100vw - 24px);min-width:0}.composer-hint span:nth-child(2){display:none}#main-stage{inset:68px 0 90px}.orb-overline{top:145px;left:50%}#core-title{left:50%;top:calc(45% + min(150px,20vh))}#caption{left:50%;top:calc(45% + min(190px,24vh));width:min(430px,90vw)} }
@media(max-width:520px){.topbar{height:61px;padding:0 7px;gap:5px}.thinking-wave{display:none}.thinking-pill{padding:5px}.thinking-copy strong{font-size:.58rem;letter-spacing:.15em}.floating-nav{padding:4px;border-radius:12px}.nav-link{padding:8px 6px;font-size:.53rem}.nav-divider{height:20px}.gesture-toggle{font-size:0;gap:4px;padding:7px}.gesture-dot{display:none}.toggle-track{width:22px}.gesture-toggle[aria-pressed=true] .toggle-track i{transform:translateX(7px)}.left-dock{top:67px;left:7px;right:7px;gap:6px}.time-card,.weather-card{padding:8px;border-radius:12px}.card-eyebrow{font-size:.44rem;letter-spacing:.08em}.time-card .clock{font-size:1.06rem;letter-spacing:0}.time-card .date{font-size:.52rem}.time-card .timezone{font-size:0;gap:0}.time-card .timezone .tiny-live{width:4px;height:4px}.weather-top{gap:5px}.weather-icon{font-size:1.15rem}.temp{font-size:1.05rem}.condition{font-size:.48rem;max-width:100px}.weather-card .city{font-size:.4rem}.weather-grid{gap:4px}.metric small{font-size:.43rem}.metric strong{font-size:.54rem}.weather-footer{display:none}.chat-panel{right:7px;bottom:87px;width:calc(100vw - 14px);height:34vh;max-height:305px}.chat-header{height:49px;padding:0 10px}.chat-title-mark{width:24px;height:24px}.chat-heading strong{font-size:.63rem}.chat-heading small{font-size:.42rem}.chat-tabs{height:34px;padding:0 7px}.chat-tab{font-size:.49rem;padding:0 8px}.feed{padding:9px;gap:7px}.logline{font-size:.62rem;padding:8px 9px}.chat-automation{padding:7px 9px}.automation-copy strong{font-size:.46rem}.automation-copy small{font-size:.5rem}.side-composer{margin:6px 7px 0}.chat-footnote{padding:7px 9px;font-size:.39rem}.bottom-composer-wrap{width:calc(100vw - 14px);bottom:9px}.composer-context{font-size:.44rem;padding:0 7px 6px}.bottom-composer{min-height:51px;border-radius:15px;padding:5px 6px 5px 10px;gap:7px}.composer-leading{display:none}.bottom-composer input{font-size:.7rem;padding:10px 0}.composer-actions{gap:4px}.composer-icon{width:29px;height:29px;border-radius:8px}.voice-start{font-size:0;padding:0;width:31px;height:31px;display:grid;place-items:center}.voice-start:after{content:'♫';font-size:.9rem}.send-button{width:32px;height:32px;border-radius:9px}.composer-hint{font-size:.39rem;padding:6px 5px 0}.composer-hint span:last-child{display:none}.orb-overline{top:140px;font-size:.43rem;gap:5px;letter-spacing:.1em}.orb-overline-line{width:21px}#main-stage{inset:61px 0 85px}#core-title{top:calc(43% + 115px);width:200px}#core-title>strong{font-size:.68rem}#core-state{font-size:.47rem;margin-top:6px}#caption{top:calc(43% + 146px);font-size:.59rem;width:92vw;max-height:2.9em;overflow:hidden}.palette-scrim{padding-top:9vh}.palette-bottom{font-size:.39rem}.command-option small{font-size:.53rem}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.001ms!important;animation-iteration-count:1!important;scroll-behavior:auto!important;transition-duration:.001ms!important}}
@media(max-width:520px){#core-title,#caption{display:none!important}.chat-panel{bottom:108px;height:32vh;max-height:270px}.orb-overline{top:190px;left:50%}}
@media(max-width:520px) and (max-height:720px){.orb-overline{top:145px}.chat-panel{height:31vh;max-height:235px} }


/* NEXUS LITE: keep the same layout while reducing GPU-heavy effects. */
.panel,.glass-panel,.chat-panel,.bottom-composer,.palette-modal,.toast,#settings,#settings-scrim,.palette-scrim,.floating-nav,.thinking-pill,.status-chip {
  -webkit-backdrop-filter:none!important;backdrop-filter:none!important;
}
body:after{display:none!important}
body:before{opacity:.055!important;mask-image:none!important}
.panel,.chat-panel,.glass-panel{box-shadow:inset 0 1px rgba(255,255,255,.025)!important}
.brandmark,.brandmark:after,#start:before{animation:none!important}
.brandmark,#start .bigmark{box-shadow:0 0 12px rgba(138,247,255,.10),inset 0 0 8px rgba(138,247,255,.06)!important}
.brandmark svg,.weather-icon{filter:none!important}
button,.send-button,.panel,.floating-nav,.palette-modal,.toast{transition:background .12s,border-color .12s!important}
@media(prefers-reduced-motion:reduce){canvas{display:none!important}}


/* Holographic Command Center modules */
.holo-deck{position:absolute;inset:0;z-index:5;pointer-events:none;font-family:'Share Tech Mono',monospace}
.holo-panel{position:absolute;width:clamp(196px,17vw,232px);padding:11px 12px;border:1px solid rgba(138,247,255,.19);border-radius:13px;background:linear-gradient(145deg,rgba(5,15,18,.91),rgba(2,6,8,.88));box-shadow:inset 0 1px rgba(255,255,255,.035),0 14px 36px rgba(0,0,0,.26);pointer-events:auto;overflow:hidden;color:#eaffff}
.holo-panel:before{content:'';position:absolute;left:12px;top:-1px;width:34px;height:2px;background:#bafcff;box-shadow:0 0 9px rgba(138,247,255,.65)}
.holo-panel-head{height:17px;display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:.53rem;letter-spacing:.12em;color:#8ca6a8;white-space:nowrap}
.holo-live-state{display:flex;align-items:center;gap:5px;font-size:.46rem;color:#d5ffff;letter-spacing:.05em}
.holo-panel .tiny-live{width:4px;height:4px;display:inline-block;border-radius:50%;background:#bafcff;box-shadow:0 0 8px rgba(138,247,255,.8);flex:none}
.model-panel{left:clamp(242px,calc(50% - 510px),360px);top:54px}
.model-viewport{height:121px;position:relative;overflow:hidden;margin:4px -2px 2px;background:radial-gradient(ellipse at center,rgba(138,247,255,.07),transparent 67%);border:1px solid rgba(138,247,255,.08);border-radius:8px}
#model-canvas{position:absolute;inset:0;z-index:0;pointer-events:none;display:block;width:100%;height:100%}.model-reticle{position:absolute;inset:12px 24px;border:1px solid rgba(138,247,255,.08);border-radius:50%;pointer-events:none}.model-reticle:before,.model-reticle:after{content:'';position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);border:1px dashed rgba(138,247,255,.10);border-radius:50%;width:67%;height:67%}.model-reticle:after{width:1px;height:calc(100% + 10px);border:0;border-left:1px dashed rgba(138,247,255,.12);border-radius:0}
.model-readout{position:absolute;left:8px;right:8px;bottom:5px;display:flex;justify-content:space-between;gap:5px;font-size:.43rem;letter-spacing:.08em;color:#8cb8bc;pointer-events:none}.model-readout span:first-child{color:#e0ffff}
.model-selectors{display:flex;gap:5px;margin-top:7px}.model-select{flex:1;min-width:0;padding:6px 4px;border-radius:6px;border:1px solid rgba(138,247,255,.13);background:rgba(138,247,255,.025);color:#7f9b9e;font: .48rem 'Share Tech Mono',monospace;letter-spacing:.07em;box-shadow:none;transform:none}.model-select:hover,.model-select.active{color:#ecffff;border-color:rgba(138,247,255,.43);background:rgba(138,247,255,.09);box-shadow:inset 0 0 14px rgba(138,247,255,.04);transform:none}
.system-panel{right:calc(min(356px,28vw) + 28px);top:54px;width:clamp(196px,17vw,232px)}
.system-map{height:103px;position:relative;margin:2px 0 1px}.system-lines{position:absolute;inset:0;width:100%;height:100%;overflow:visible}.system-lines path{fill:none;stroke:rgba(138,247,255,.32);stroke-width:1;stroke-dasharray:4 4;animation:diagramFlow 4s linear infinite}.system-lines circle{fill:#bafcff;filter:drop-shadow(0 0 2px #8af7ff)}
.system-node{position:absolute;z-index:2;width:60px;min-height:24px;padding:4px 3px;display:flex;align-items:center;justify-content:center;gap:4px;border:1px solid rgba(138,247,255,.2);border-radius:6px;background:rgba(3,10,12,.95);color:#88a4a5;font: .46rem 'Share Tech Mono',monospace;letter-spacing:.03em;box-shadow:none;transform:none;white-space:nowrap}
.system-node i{width:4px;height:4px;border-radius:50%;background:#4e686b;flex:none}.system-node:hover,.system-node.selected{background:rgba(138,247,255,.11);border-color:rgba(186,252,255,.68);color:#f0ffff;box-shadow:0 0 13px rgba(138,247,255,.10);transform:none}.system-node.online i{background:#bafcff;box-shadow:0 0 7px rgba(138,247,255,.8)}.system-node.busy i{background:#fff;box-shadow:0 0 8px #fff;animation:nodePulse 1s ease-in-out infinite}.node-voice{left:0;top:4px}.node-vision{left:0;bottom:1px}.node-core{left:50%;top:calc(50% - 12px);transform:translateX(-50%);width:58px;border-color:rgba(186,252,255,.4);color:#efffff}.node-core:hover,.node-core.selected{transform:translateX(-50%)}.node-tools{right:0;top:4px}.node-telemetry{right:0;bottom:1px;width:66px}
.system-detail{border-top:1px solid rgba(138,247,255,.09);padding-top:7px;margin-top:3px}.system-detail>div{display:flex;align-items:center;gap:5px;min-width:0}.system-detail b{font-size:.48rem;letter-spacing:.08em;color:#c7f8f9;font-weight:400;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.system-detail span{font-size:.43rem;margin-left:auto;color:#8ba7a8;white-space:nowrap}.system-detail p{font: .5rem/1.45 'Chakra Petch','Segoe UI',sans-serif;color:#82999b;margin:5px 0 0;min-height:24px}
.tasks-panel{right:calc(min(356px,28vw) + 28px);top:280px;width:clamp(196px,17vw,232px)}.task-list{display:flex;flex-direction:column;gap:1px;margin-top:5px}.task-row{position:relative;display:grid;grid-template-columns:5px minmax(0,1fr) auto;align-items:center;column-gap:7px;min-height:27px;padding:3px 2px 5px;border-bottom:1px solid rgba(138,247,255,.055)}.task-indicator{width:4px;height:4px;border-radius:50%;background:#405657;grid-column:1;grid-row:1 / span 2}.task-copy{grid-column:2;grid-row:1;min-width:0;display:flex;align-items:baseline;gap:6px;overflow:hidden}.task-copy b{font-size:.53rem;font-weight:400;letter-spacing:.06em;color:#b4cccc;white-space:nowrap}.task-copy small{display:none;font: .42rem 'Chakra Petch','Segoe UI',sans-serif;color:#5f7a7c;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.task-status{grid-column:3;grid-row:1;color:#607777;font-size:.48rem;letter-spacing:.04em}.task-bar{grid-column:2 / 4;grid-row:2;height:2px;background:rgba(138,247,255,.06);overflow:hidden;margin-top:4px}.task-bar i{display:block;width:18%;height:100%;background:rgba(138,247,255,.35);transform:translateX(-120%)}.task-row.active .task-indicator{background:#d8ffff;box-shadow:0 0 8px rgba(138,247,255,.9);animation:nodePulse 1.2s ease-in-out infinite}.task-row.active .task-copy b,.task-row.active .task-status{color:#e8ffff}.task-row.active .task-bar i{width:38%;background:#bbffff;animation:taskSweep 1.8s ease-in-out infinite}.task-row.busy .task-bar i{width:70%;animation:taskSweep .85s ease-in-out infinite}.task-footer{display:flex;align-items:center;gap:6px;margin-top:8px;font-size:.42rem;letter-spacing:.04em;color:#607b7d;white-space:nowrap}.task-footer #task-frame{margin-left:auto;color:#486467}.telemetry-foot{border-top:1px solid rgba(138,247,255,.08);padding-top:7px;margin-top:8px;font: .45rem 'Share Tech Mono',monospace;color:#678a8c;letter-spacing:.05em}
.telemetry-mini{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:4px;margin-top:7px;padding-top:6px;border-top:1px solid rgba(138,247,255,.075)}.telemetry-mini>div{display:flex;flex-direction:column;gap:3px;min-width:0}.telemetry-mini span{font-size:.39rem;color:#688789;letter-spacing:.06em}.telemetry-mini b{font: .5rem 'Share Tech Mono',monospace;font-weight:400;color:#ceffff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
@keyframes diagramFlow{to{stroke-dashoffset:-32}}@keyframes nodePulse{0%,100%{opacity:.45}50%{opacity:1}}@keyframes taskSweep{0%{transform:translateX(-120%)}100%{transform:translateX(360%)}}
@media(max-width:900px){.holo-deck{inset:0;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));align-items:start;gap:6px;padding:0 8px;top:205px;bottom:auto;height:174px}.holo-panel{position:relative;inset:auto;width:auto;min-width:0;height:100%;padding:8px 7px;border-radius:10px}.model-panel,.system-panel,.tasks-panel{left:auto;right:auto;top:auto}.holo-panel-head{font-size:.42rem;letter-spacing:.04em}.model-viewport{height:88px}.model-selectors{gap:2px}.model-select{font-size:.39rem;padding:5px 1px}.system-map{height:83px;margin-top:5px}.system-node{width:43px;min-height:22px;font-size:.37rem;gap:2px;padding:3px 1px}.node-core{width:42px;top:calc(50% - 11px)}.node-telemetry{width:48px}.system-detail{display:none}.task-list{margin-top:5px;gap:0}.task-row[data-task="telemetry"]{display:none}.telemetry-mini{gap:2px;margin-top:5px;padding-top:4px}.telemetry-mini span{font-size:.34rem}.telemetry-mini b{font-size:.4rem}.task-row{min-height:24px;padding:2px 0 3px;column-gap:4px}.task-copy{display:block}.task-copy b{font-size:.39rem;display:block}.task-copy small{display:none}.task-status{font-size:.38rem}.task-bar{margin-top:2px}.task-footer{font-size:0;gap:4px;margin-top:5px}.task-footer #task-frame{display:none}.telemetry-foot{font-size:.39rem;line-height:1.35}.system-panel .holo-panel-head span:last-child{font-size:.39rem}}
@media(max-width:520px){.holo-deck{top:188px;height:145px;gap:4px;padding:0 6px}.holo-panel{padding:6px 5px;border-radius:8px}.holo-panel-head{font-size:.35rem;letter-spacing:0}.model-viewport{height:66px;margin-top:4px}.model-select{font-size:.34rem;padding:4px 0}.model-readout{font-size:.33rem;left:3px;right:3px}.model-reticle{inset:8px 15px}.system-map{height:68px;margin-top:5px}.system-node{width:36px;min-height:19px;font-size:.32rem}.node-core{width:35px;top:calc(50% - 9px)}.node-telemetry{width:41px}.task-row{min-height:21px;grid-template-columns:3px minmax(0,1fr) auto;column-gap:3px}.telemetry-mini{margin-top:4px;padding-top:3px}.telemetry-mini span{font-size:.29rem}.telemetry-mini b{font-size:.34rem}.task-copy b,.task-status{font-size:.33rem}.task-footer .tiny-live{width:3px;height:3px}.telemetry-foot{font-size:.33rem}.system-panel .holo-panel-head span:last-child{display:none}}
@media(prefers-reduced-motion:reduce){.system-lines path,.system-node.busy i,.task-row.active .task-indicator,.task-row.active .task-bar i,.task-row.busy .task-bar i{animation:none!important}.model-reticle:before,.model-reticle:after{animation:none!important}}


/* NEXUS LIGHT UI — contained panels, no overlap, system-font fallback. */
:root{--deck-h:clamp(218px,27vh,246px);font-family:system-ui,"Segoe UI",sans-serif}
html,body{width:100%;height:100%;min-height:0;overflow:hidden;background:#020506!important}
body{background:radial-gradient(ellipse at 52% 34%,#071214 0,#020506 62%,#000 100%)!important}
body:before{opacity:.045!important;background-size:56px 56px!important}
body:after{display:none!important}
#app{width:100%;height:100%;min-height:0;overflow:hidden}
#hud-canvas{position:fixed;inset:0;width:100%;height:100%;z-index:1;pointer-events:none;opacity:.7}
canvas#model-canvas{position:absolute;inset:0;width:100%;height:100%;z-index:0;pointer-events:none}
.topbar{height:68px;padding:0 16px;gap:12px;background:rgba(2,5,6,.94);backdrop-filter:none!important;border-bottom-color:rgba(138,247,255,.10)}
.thinking-pill{min-width:150px;padding:7px 10px;gap:8px;background:#050a0b;box-shadow:none}
.thinking-wave i{box-shadow:none;animation-duration:1.8s}
.floating-nav{padding:5px;gap:2px;background:#080d0e;box-shadow:none;backdrop-filter:none!important}
.palette-btn{padding:7px 9px}.nav-link{padding:8px 9px}
.top-right{gap:7px}.status-chip{padding:7px 9px;background:#050809}.gesture-toggle{padding:7px 9px;background:#050809}
.left-dock{position:fixed;z-index:3;top:80px;bottom:104px;left:16px;width:clamp(184px,16vw,214px);gap:8px;overflow-y:auto;overflow-x:hidden;scrollbar-width:thin;scrollbar-color:#1a3336 transparent}
.glass-card{padding:12px 13px;border-radius:10px;background:#05090a;border-color:rgba(138,247,255,.12);box-shadow:none;backdrop-filter:none!important}
.time-card .clock{font-size:clamp(1.45rem,2vw,1.95rem);margin-top:10px}
.time-card .timezone{margin-top:9px;padding-top:8px}
.weather-top{margin-top:10px}.weather-grid{gap:6px;margin-top:9px}.metric{padding-top:6px}.weather-footer{margin-top:8px;padding-top:7px}
.telemetry-card{display:none!important}
#main-stage{position:fixed;z-index:2;top:68px;bottom:112px;left:calc(clamp(184px,16vw,214px) + 32px);right:calc(clamp(270px,25vw,325px) + 28px);overflow:hidden;pointer-events:none}
.orb-overline{left:50%;top:12px;transform:translateX(-50%);font-size:.52rem;letter-spacing:.12em}
#core-title{left:50%;top:34%;transform:translate(-50%,-50%);width:210px;text-shadow:none}
#core-title strong{font-size:.86rem;letter-spacing:.26em}
#core-state{margin-top:7px;font-size:.54rem;text-shadow:none}
#caption{left:50%;bottom:calc(var(--deck-h) + 14px);top:auto;transform:translateX(-50%);width:min(520px,94%);min-height:20px;font-size:.72rem;text-shadow:none;color:#a9c8c9}
.holo-deck{position:absolute;inset:auto 0 0;z-index:5;height:var(--deck-h);display:grid;grid-template-columns:1.08fr 1fr 1fr;gap:10px;padding:0;font-family:ui-monospace,"SFMono-Regular",Consolas,monospace;pointer-events:none}
.holo-panel{position:relative;inset:auto;width:auto;height:100%;min-width:0;min-height:0;margin:0;padding:11px;border:1px solid rgba(138,247,255,.15);border-radius:10px;background:#050a0b;box-shadow:none;backdrop-filter:none!important;overflow:hidden;color:#eaffff;pointer-events:auto}
.holo-panel:before{width:22px;box-shadow:none}
.holo-panel-head{height:17px;font-size:.53rem;letter-spacing:.07em;color:#8da5a6}
.holo-live-state{font-size:.46rem}
.holo-panel .tiny-live{box-shadow:none}
.model-viewport{height:calc(100% - 49px);min-height:108px;max-height:142px;margin:5px 0 1px;border-color:rgba(138,247,255,.08);background:#030707}
.model-reticle{inset:10px 20px}.model-reticle:before,.model-reticle:after{border-style:solid;opacity:.7}
.model-selectors{gap:5px;margin-top:6px}
.model-select{min-height:28px;padding:5px 3px;border-radius:6px;font-size:.51rem;letter-spacing:.03em;background:#070d0e;box-shadow:none;backdrop-filter:none!important}
.model-select:hover,.model-select.active{box-shadow:none;background:#0b191b}
.model-readout{font-size:.42rem;letter-spacing:.04em}
.system-panel,.tasks-panel{left:auto;right:auto;top:auto;bottom:auto;width:auto}
.system-map{height:clamp(78px,10vh,92px);margin:3px 0 1px}
.system-lines path{animation:none;stroke-dasharray:3 5}
.system-node{min-height:25px;width:64px;font-size:.48rem;border-radius:5px;background:#030708;box-shadow:none;backdrop-filter:none!important}
.node-core{width:58px;top:calc(50% - 12px);transform:translateX(-50%)}
.node-core:hover,.node-core.selected{transform:translateX(-50%)}
.node-telemetry{width:70px}
.system-node:hover,.system-node.selected{box-shadow:none}
.system-node.busy i{animation:none}
.system-detail{padding-top:7px;margin-top:3px}
.system-detail b{font-size:.49rem}.system-detail span{font-size:.45rem}.system-detail p{font-size:.55rem;line-height:1.4;min-height:0}
.tasks-panel{display:flex;flex-direction:column}
.task-list{gap:0;margin-top:4px}
.task-row{min-height:23px;padding:2px 1px 3px;column-gap:6px;grid-template-columns:4px minmax(0,1fr) auto}
.task-copy b{font-size:.52rem;letter-spacing:.035em}.task-copy small{display:none}.task-status{font-size:.47rem;letter-spacing:.02em}
.task-bar{height:2px;margin-top:3px}.task-row.active .task-indicator,.task-row.active .task-bar i,.task-row.busy .task-bar i{animation:none}
.task-row.active .task-bar i{transform:none;width:40%}.task-row.busy .task-bar i{width:68%}
.telemetry-mini{gap:7px;margin-top:auto;padding-top:7px;border-top:1px solid rgba(138,247,255,.09)}
.telemetry-mini span{font-size:.42rem}.telemetry-mini b{font-size:.53rem}
.task-footer{margin-top:7px;font-size:.44rem;letter-spacing:.02em;white-space:normal}
#chat-panel{position:fixed;z-index:6;top:80px;right:12px;bottom:82px;height:auto;max-height:none;width:clamp(270px,25vw,325px);min-width:0;border-radius:10px;border-color:rgba(138,247,255,.13);background:#050909;box-shadow:none;backdrop-filter:none!important}
.chat-header{height:55px;padding:0 11px}.chat-title-mark{box-shadow:none}.chat-tabs{height:36px;padding:0 9px}
.chat-feed-wrap{min-height:0}.feed{padding:10px;gap:7px}.logline{padding:8px 9px;border-radius:9px;font-size:.67rem}
.chat-automation{padding:8px 10px}.automation-spark{font-size:.8rem}.side-composer{margin:7px 8px 0;border-radius:8px;background:#070c0d;backdrop-filter:none!important}
.chat-footnote{padding:8px 10px;font-size:.43rem}
.bottom-composer-wrap{left:calc((100vw - clamp(270px,25vw,325px))/2);width:min(700px,calc(100vw - clamp(270px,25vw,325px) - 230px));min-width:0;bottom:10px}
.bottom-composer{background:#060b0c;backdrop-filter:none!important;border-radius:12px;box-shadow:none}
.composer-context{padding-bottom:6px}.composer-hint{padding-top:5px}
#settings-scrim,.palette-scrim{backdrop-filter:none!important}
#settings{backdrop-filter:none!important;box-shadow:-8px 0 22px rgba(0,0,0,.35)}
.palette-modal{backdrop-filter:none!important;box-shadow:0 10px 36px rgba(0,0,0,.45)}
button,.send-button,.panel,.floating-nav,.palette-modal,.toast{transition:background .12s,border-color .12s!important}
button:hover{transform:none!important;box-shadow:none!important}
@media(max-width:1120px){
 .left-dock{display:none!important}
 #main-stage{left:14px;right:calc(clamp(260px,31vw,300px) + 20px);bottom:112px;overflow:hidden}
 #chat-panel{right:10px;width:clamp(260px,31vw,300px)}
 .bottom-composer-wrap{left:calc((100vw - clamp(260px,31vw,300px))/2);width:calc(100vw - clamp(260px,31vw,300px) - 70px)}
 .topbar{padding:0 12px;gap:7px}
 .thinking-pill{min-width:132px}.thinking-copy small{max-width:90px;overflow:hidden;text-overflow:ellipsis}
}
@media(max-width:1000px) and (min-width:761px){
 #hud-canvas{display:none!important}
 #main-stage{left:14px;right:calc(clamp(260px,31vw,300px) + 20px);top:68px;bottom:112px;overflow-y:auto;overflow-x:hidden;scrollbar-width:thin;scrollbar-color:#183335 transparent;display:flex;flex-direction:column;gap:8px}
 .orb-overline{position:relative;left:auto;top:auto;transform:none;order:0;justify-content:center;min-height:22px;margin-top:3px}
 #core-title{position:relative;left:auto;top:auto;transform:none;order:1;width:auto;margin:8px auto 0}
 #caption{position:relative;left:auto;bottom:auto;top:auto;transform:none;order:2;width:100%;margin:0 auto 8px;min-height:0}
 .holo-deck{position:relative;inset:auto;order:3;height:auto;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));grid-auto-rows:minmax(205px,auto);gap:9px;padding:0 0 12px}
 .holo-panel{height:auto;min-height:205px;padding:10px}
 .model-viewport{height:120px;min-height:100px}
 .system-panel{min-height:205px}.system-map{height:84px}
 .tasks-panel{grid-column:1/-1;min-height:175px}
 .task-row{min-height:22px}.telemetry-mini{margin-top:8px}
 .bottom-composer-wrap{left:calc((100vw - clamp(260px,31vw,300px))/2);width:calc(100vw - clamp(260px,31vw,300px) - 70px)}
}
@media(max-width:760px){
 html,body{overflow:hidden}
 .topbar{height:60px;grid-template-columns:auto 1fr auto;padding:0 8px;gap:5px}
 .thinking-pill{min-width:0;border:0;background:transparent;padding:4px;gap:5px}.thinking-wave{display:none}.thinking-copy strong{font-size:.62rem;letter-spacing:.14em}.thinking-copy small{display:none}
 .floating-nav{justify-self:center;gap:0;padding:3px;border-radius:9px}.palette-btn{width:32px;height:32px;padding:4px;justify-content:center}.palette-btn span:not(.search-glyph),.palette-btn kbd{display:none}.nav-divider{height:20px;margin:0 2px}.nav-link{padding:7px 6px;font-size:.53rem;white-space:nowrap}
 .top-right{gap:4px}.status-chip{padding:6px;font-size:.46rem;gap:4px}.gesture-toggle{padding:6px;font-size:0;gap:3px}.gesture-dot{display:none}.toggle-track{width:22px}
 .left-dock{display:none!important}
 #main-stage{position:fixed;top:60px;left:9px;right:9px;bottom:88px;overflow-y:auto;overflow-x:hidden;display:flex;flex-direction:column;gap:7px;padding:4px 0 10px;scrollbar-width:thin;scrollbar-color:#183335 transparent;pointer-events:auto}
 #hud-canvas{display:none!important}
 .orb-overline{position:relative;left:auto;top:auto;transform:none;order:0;justify-content:center;min-height:20px;font-size:.43rem;letter-spacing:.07em;margin:0}
 .orb-overline-line{width:18px}
 #core-title{position:relative;left:auto;top:auto;transform:none;order:1;width:auto;margin:5px auto 0;text-shadow:none}
 #core-title strong{font-size:.7rem;letter-spacing:.22em}
 #core-state{font-size:.45rem;margin-top:4px}
 #caption{position:relative;left:auto;top:auto;bottom:auto;transform:none;order:2;width:100%;min-height:0;margin:0 auto 5px;font-size:.59rem;line-height:1.4;max-height:2.8em;overflow:hidden}
 .holo-deck{position:relative;inset:auto;order:3;height:auto;display:grid;grid-template-columns:minmax(0,1fr);grid-auto-rows:auto;gap:9px;padding:0 0 10px;pointer-events:auto}
 .holo-panel{position:relative;inset:auto;width:100%;height:auto;min-height:0;padding:11px;border-radius:9px}
 .holo-panel-head{font-size:.53rem}.model-panel{min-height:212px}.model-viewport{height:136px;min-height:130px;max-height:none}.model-select{font-size:.49rem;min-height:28px}
 .system-panel{min-height:180px}.system-map{height:86px}.system-detail p{font-size:.53rem}.tasks-panel{min-height:190px}
 .task-row{min-height:23px}.task-copy b,.task-status{font-size:.49rem}.telemetry-mini{margin-top:8px;padding-top:7px}.telemetry-mini span{font-size:.4rem}.telemetry-mini b{font-size:.5rem}
 #core-title,#caption{display:block!important}
 #chat-panel{display:none;top:68px;left:9px;right:9px;bottom:84px;width:auto;height:auto;max-height:none;border-radius:11px;z-index:24}
 #app.mobile-chat-open #chat-panel{display:flex!important}
 .chat-header{height:49px}.chat-tabs{height:34px}.feed{padding:9px}.chat-footnote{font-size:.4rem}
 .bottom-composer-wrap{position:fixed;left:10px;right:10px;bottom:7px;transform:none;width:auto;min-width:0;z-index:23}
 .composer-context{font-size:.4rem;padding:0 6px 5px}.composer-context-right{display:none}
 .bottom-composer{min-height:46px;border-radius:10px;padding:4px 5px 4px 9px;gap:5px}.bottom-composer input{min-width:0;font-size:.68rem}.composer-leading{display:none}.composer-actions{gap:3px}.composer-icon{width:28px;height:28px}.voice-start{font-size:0;padding:0;width:30px;height:30px;display:grid;place-items:center}.voice-start:after{content:'♫';font-size:.85rem}.send-button{width:30px;height:30px}
 .composer-hint{font-size:.36rem;padding:4px 3px 0}.composer-hint span:nth-child(2){display:none}.composer-hint span:last-child{display:none}
 #settings{width:100vw;padding:16px;gap:10px}.setting{padding:10px}
}
@media(prefers-reduced-motion:reduce){.thinking-wave i,.task-row.active .task-indicator,.system-lines path{animation:none!important}}


/* CINEMATIC HOLOGRAM MODE — 1280×720-first, one hero projection, minimal overlays. */
:root{--deck-h:0px!important;--hero-cyan:#82f7ff}
html,body,#app{width:100%;height:100%;min-height:0;overflow:hidden!important}
body{background:radial-gradient(ellipse at 50% 44%,#07171d 0,#020709 48%,#010203 100%)!important}
body:before{opacity:.065!important;background-size:62px 62px!important}
body:after{display:none!important}
#hud-canvas{opacity:.12!important}
.topbar{height:56px!important;min-height:56px!important;padding:0 16px!important;gap:10px!important;background:rgba(2,6,8,.86)!important;border-bottom:1px solid rgba(130,247,255,.13)!important;backdrop-filter:none!important}
.thinking-pill{min-width:140px!important;background:transparent!important;border:0!important;padding:5px 8px!important;box-shadow:none!important}
.thinking-copy strong{font-size:.69rem!important;letter-spacing:.18em!important}
.thinking-copy small{font-size:.45rem!important;letter-spacing:.11em!important}
.floating-nav{padding:3px!important;gap:1px!important;border-radius:7px!important;background:rgba(4,12,15,.78)!important;border:1px solid rgba(130,247,255,.13)!important;box-shadow:none!important}
.palette-btn{padding:6px 8px!important;border-radius:4px!important;background:transparent!important;font-size:.57rem!important}
.nav-link{padding:7px 9px!important;border-radius:4px!important;font-size:.57rem!important}
.status-chip{padding:6px 8px!important;border-radius:4px!important;background:transparent!important;font-size:.54rem!important}
.gesture-toggle{display:none!important}
.left-dock{display:none!important}
#main-stage{position:fixed!important;z-index:2!important;top:56px!important;bottom:82px!important;left:0!important;right:0!important;inset:56px 0 82px!important;overflow:hidden!important;pointer-events:none!important}
.orb-overline{display:none!important;position:absolute!important;left:50%!important;top:8px!important;transform:translateX(-50%)!important;color:rgba(172,235,240,.64)!important;font-size:.48rem!important;letter-spacing:.2em!important;gap:9px!important}
.orb-overline-line{width:35px!important;background:linear-gradient(90deg,transparent,rgba(130,247,255,.42))!important}
.holo-deck{position:absolute!important;inset:0!important;height:auto!important;display:block!important;padding:0!important;pointer-events:none!important}
.holo-panel{position:absolute!important;box-sizing:border-box!important;min-width:0!important;min-height:0!important;margin:0!important;border:1px solid rgba(130,247,255,.18)!important;border-radius:3px!important;background:linear-gradient(135deg,rgba(4,14,18,.76),rgba(2,6,9,.55))!important;box-shadow:inset 0 1px rgba(220,255,255,.035)!important;backdrop-filter:none!important;overflow:hidden!important;color:#e9ffff!important;pointer-events:auto!important}
.holo-panel:before{left:0!important;top:0!important;width:28px!important;height:1px!important;background:#bdfcff!important;box-shadow:none!important}
.holo-panel-head{height:18px!important;font-size:.5rem!important;letter-spacing:.14em!important;color:#83a5a8!important}
.holo-live-state{font-size:.43rem!important;letter-spacing:.08em!important}
.holo-panel .tiny-live{box-shadow:0 0 5px rgba(130,247,255,.55)!important}
.model-panel{left:50%!important;right:auto!important;top:0!important;bottom:0!important;transform:translateX(-50%)!important;width:min(650px,58vw)!important;height:100%!important;padding:0!important;border:0!important;border-radius:0!important;background:transparent!important;box-shadow:none!important;overflow:visible!important;pointer-events:none!important}
.model-panel:before{display:none!important}
.model-panel>.holo-panel-head{position:absolute!important;left:50%!important;top:9px!important;transform:translateX(-50%)!important;width:max-content!important;max-width:90%!important;z-index:3!important;justify-content:center!important;gap:12px!important;font-size:.48rem!important;letter-spacing:.16em!important;color:rgba(180,240,243,.7)!important;pointer-events:none!important}
.model-viewport{position:absolute!important;inset:25px 0 53px!important;height:auto!important;min-height:0!important;max-height:none!important;margin:0!important;border:0!important;border-radius:0!important;overflow:visible!important;background:radial-gradient(ellipse at 50% 50%,rgba(51,200,215,.10),rgba(2,10,13,.02) 48%,transparent 73%)!important}
#model-canvas{position:absolute!important;inset:0!important;display:block!important;width:100%!important;height:100%!important;pointer-events:none!important;z-index:1!important}
.model-reticle{inset:8% 9%!important;border:0!important;opacity:.9!important}
.model-reticle:before{width:80%!important;height:74%!important;border:1px solid rgba(130,247,255,.065)!important;border-radius:50%!important}
.model-reticle:after{width:1px!important;height:98%!important;border:0!important;border-left:1px dashed rgba(130,247,255,.08)!important;border-radius:0!important}
.model-readout{left:8%!important;right:8%!important;bottom:3px!important;font-size:.43rem!important;letter-spacing:.16em!important;color:rgba(144,211,215,.65)!important}
.model-readout span:first-child{color:#d5ffff!important}
.model-selectors{position:absolute!important;z-index:4!important;left:50%!important;bottom:9px!important;transform:translateX(-50%)!important;width:min(320px,78%)!important;display:flex!important;gap:5px!important;margin:0!important;pointer-events:auto!important}
.model-select{flex:1!important;min-width:0!important;min-height:25px!important;padding:5px 7px!important;border:1px solid rgba(130,247,255,.20)!important;border-radius:2px!important;background:rgba(3,12,15,.73)!important;color:#719a9f!important;font: .48rem ui-monospace,Consolas,monospace!important;letter-spacing:.11em!important;box-shadow:none!important;transition:background .12s,border-color .12s!important}
.model-select.active,.model-select:hover{color:#efffff!important;border-color:rgba(180,252,255,.72)!important;background:rgba(38,142,153,.16)!important;box-shadow:inset 0 -1px rgba(180,252,255,.32)!important}
.system-panel{left:18px!important;right:auto!important;top:50%!important;bottom:auto!important;transform:translateY(-50%)!important;width:min(196px,17vw)!important;height:auto!important;padding:10px 11px!important}
.system-map{height:96px!important;margin:6px 0 2px!important}
.system-lines path{stroke:rgba(130,247,255,.27)!important;stroke-dasharray:2 7!important;animation:diagramFlow 7s linear infinite!important}
.system-lines circle{filter:none!important;opacity:.8!important}
.system-node{width:57px!important;min-height:23px!important;padding:3px 2px!important;border-radius:2px!important;background:rgba(2,8,10,.88)!important;font-size:.44rem!important;box-shadow:none!important;backdrop-filter:none!important}
.node-core{width:52px!important;top:calc(50% - 12px)!important;transform:translateX(-50%)!important}
.node-core:hover,.node-core.selected{transform:translateX(-50%)!important}
.node-telemetry{width:62px!important}
.system-detail{padding-top:7px!important;margin-top:4px!important;border-top-color:rgba(130,247,255,.11)!important}
.system-detail b{font-size:.45rem!important;letter-spacing:.055em!important}
.system-detail span{font-size:.42rem!important}
.system-detail p{font-size:.49rem!important;line-height:1.4!important;min-height:0!important;color:#779699!important}
.tasks-panel{right:18px!important;left:auto!important;top:50%!important;bottom:auto!important;transform:translateY(-50%)!important;width:min(202px,17.5vw)!important;height:auto!important;padding:10px 11px!important}
.task-list{margin-top:6px!important;gap:0!important}
.task-row{min-height:24px!important;padding:4px 1px!important;grid-template-columns:4px minmax(0,1fr) auto!important;column-gap:6px!important;border-bottom-color:rgba(130,247,255,.07)!important}
.task-indicator{box-shadow:none!important}
.task-copy{display:block!important;min-width:0!important}
.task-copy b{font-size:.46rem!important;letter-spacing:.035em!important}
.task-copy small{display:none!important}
.task-status{font-size:.42rem!important;letter-spacing:.015em!important}
.task-bar{height:1px!important;margin-top:3px!important;background:rgba(130,247,255,.08)!important}
.task-row.active .task-indicator{box-shadow:0 0 4px rgba(180,255,255,.6)!important}
.task-row.active .task-bar i{background:#a6f9ff!important}
.task-row.active .task-indicator,.task-row.active .task-bar i,.task-row.busy .task-bar i{animation:taskSweep 2.4s ease-in-out infinite!important}
.telemetry-mini{gap:5px!important;margin-top:9px!important;padding-top:8px!important;border-top-color:rgba(130,247,255,.12)!important}
.telemetry-mini span{font-size:.39rem!important;letter-spacing:.04em!important}
.telemetry-mini b{font-size:.45rem!important}
.task-footer{margin-top:8px!important;font-size:.39rem!important;letter-spacing:.025em!important;white-space:normal!important;color:#608589!important}
.task-footer #task-frame{display:none!important}
#core-title{display:none!important}
#caption{position:fixed!important;left:50%!important;top:auto!important;bottom:70px!important;transform:translateX(-50%)!important;width:min(560px,70vw)!important;min-height:14px!important;padding:0 8px!important;text-align:center!important;display:block!important;color:rgba(194,238,240,.74)!important;text-shadow:0 0 10px rgba(130,247,255,.08)!important;font-size:.61rem!important;line-height:1.3!important;letter-spacing:.04em!important;overflow:hidden!important;white-space:nowrap!important;text-overflow:ellipsis!important;pointer-events:none!important}
.bottom-composer-wrap{position:fixed!important;left:50%!important;right:auto!important;bottom:8px!important;transform:translateX(-50%)!important;width:min(500px,calc(100vw - 30px))!important;min-width:0!important;z-index:7!important}
.composer-context{padding:0 8px 4px!important;font-size:.42rem!important;letter-spacing:.12em!important;color:#62868a!important}
.composer-context-right{display:none!important}
.bottom-composer{min-height:43px!important;padding:4px 5px 4px 12px!important;gap:8px!important;border-radius:3px!important;border-color:rgba(130,247,255,.25)!important;background:rgba(2,8,10,.92)!important;box-shadow:none!important;backdrop-filter:none!important}
.bottom-composer input{font-size:.68rem!important;padding:8px 0!important}
.composer-leading{font-size:.95rem!important;color:#6f999e!important}
.composer-icon{width:29px!important;height:29px!important;border-radius:3px!important;background:transparent!important}
.voice-start{padding:7px 9px!important;border-radius:3px!important;font-size:.46rem!important;background:rgba(45,145,157,.13)!important}
.send-button{width:30px!important;height:30px!important;border-radius:3px!important;box-shadow:none!important}
.composer-hint{display:none!important}
.chat-panel{display:none!important;top:66px!important;right:12px!important;bottom:70px!important;width:300px!important;max-width:calc(100vw - 24px)!important;min-width:0!important;background:rgba(2,8,10,.97)!important;border:1px solid rgba(130,247,255,.2)!important;border-radius:3px!important;box-shadow:none!important;backdrop-filter:none!important}
#app.chat-open #chat-panel{display:flex!important}
#settings{backdrop-filter:none!important;box-shadow:none!important;background:rgba(2,8,10,.98)!important}
.glass-card,.holo-panel,.bottom-composer,.chat-panel,.palette-modal{backdrop-filter:none!important}
@media(max-width:1100px) and (min-width:761px){
 .model-panel{width:58vw!important}
 .system-panel{left:10px!important;width:16.5vw!important;padding:9px!important}
 .tasks-panel{right:10px!important;width:17vw!important;padding:9px!important}
 .system-node{width:49px!important;font-size:.39rem!important}.node-core{width:45px!important}.node-telemetry{width:54px!important}
 .system-detail p{font-size:.44rem!important}
}
@media(max-width:760px){
 .topbar{height:55px!important;min-height:55px!important;padding:0 7px!important;gap:4px!important}
 .thinking-pill{min-width:0!important;padding:4px!important}.thinking-copy small,.thinking-wave{display:none!important}.thinking-copy strong{font-size:.57rem!important}
 .floating-nav{gap:0!important;padding:2px!important}.palette-btn{width:29px!important;justify-content:center!important;padding:4px!important}.palette-btn span:not(.search-glyph),.palette-btn kbd,.nav-divider{display:none!important}.nav-link{padding:7px 6px!important;font-size:.49rem!important}
 .top-right{gap:3px!important}.status-chip{padding:5px 6px!important;font-size:.42rem!important}
 #main-stage{top:55px!important;bottom:76px!important;left:0!important;right:0!important;inset:55px 0 76px!important;position:fixed!important;display:flex!important;flex-direction:column!important;align-items:stretch!important;overflow-y:auto!important;overflow-x:hidden!important;pointer-events:auto!important;padding:3px 9px 10px!important;scrollbar-width:thin!important;scrollbar-color:#15353a transparent!important}
 .orb-overline{display:flex!important;position:relative!important;left:auto!important;top:auto!important;transform:none!important;justify-content:center!important;order:0!important;margin:0!important;min-height:22px!important;font-size:.39rem!important}
 .holo-deck{position:relative!important;inset:auto!important;order:1!important;height:auto!important;display:grid!important;grid-template-columns:minmax(0,1fr)!important;grid-auto-rows:auto!important;gap:8px!important;margin:0!important;padding:0 0 10px!important;pointer-events:auto!important}
 .model-panel{position:relative!important;left:auto!important;right:auto!important;top:auto!important;bottom:auto!important;transform:none!important;width:100%!important;height:340px!important;min-height:340px!important;padding:0!important;overflow:hidden!important;border:1px solid rgba(130,247,255,.18)!important;background:rgba(2,8,10,.56)!important}
 .model-panel>.holo-panel-head{top:8px!important;font-size:.43rem!important}
 .model-viewport{inset:26px 0 52px!important}
 .model-selectors{bottom:8px!important;width:min(300px,88%)!important}
 .system-panel,.tasks-panel{position:relative!important;left:auto!important;right:auto!important;top:auto!important;bottom:auto!important;transform:none!important;width:100%!important;height:auto!important;padding:10px!important}
 .system-map{height:90px!important}.system-detail p{font-size:.49rem!important}
 .tasks-panel{min-height:190px!important}.task-row{min-height:23px!important}
 #caption{position:relative!important;left:auto!important;bottom:auto!important;top:auto!important;transform:none!important;order:2!important;width:100%!important;max-width:100%!important;white-space:normal!important;margin:0 auto 6px!important;font-size:.55rem!important}
 #core-title{display:none!important}
 .bottom-composer-wrap{left:7px!important;right:7px!important;bottom:6px!important;transform:none!important;width:auto!important}
 .composer-context{display:none!important}.bottom-composer{min-height:43px!important}.bottom-composer input{font-size:.64rem!important;flex:1!important;width:0!important}.composer-actions{flex:none!important;gap:4px!important}.voice-start{font-size:0!important;padding:0!important;width:30px!important;min-width:30px!important;height:30px!important;display:grid!important;place-items:center!important}.voice-start:after{content:'♫'!important;font-size:.85rem!important}.send-button{width:32px!important;min-width:32px!important;height:32px!important;flex:none!important;padding:0!important}
 .chat-panel{left:7px!important;right:7px!important;top:61px!important;bottom:64px!important;width:auto!important;max-width:none!important;z-index:24!important}
 #app.chat-open #chat-panel,#app.mobile-chat-open #chat-panel{display:flex!important}
}
@media(prefers-reduced-motion:reduce){#hud-canvas{display:none!important}.model-reticle:before,.model-reticle:after,.system-lines path{animation:none!important}.task-row.active .task-indicator,.task-row.active .task-bar i,.task-row.busy .task-bar i{animation:none!important}}



/* REFERENCE-INSPIRED NEXUS UI: three fixed zones, sharp panels, restrained effects.
   This stylesheet intentionally preserves all existing feature IDs and JS handlers. */
:root{
 --left-rail:clamp(190px,18.5vw,226px);
 --right-rail:clamp(300px,30.4vw,378px);
 --ui-gap:8px;
 --ui-bg:#020607;
 --ui-panel:#050b0e;
 --ui-border:rgba(100,226,238,.15);
 --ui-line:rgba(75,226,241,.58);
 --ui-muted:#66868d;
 font-family:system-ui,"Segoe UI",sans-serif!important;
}
html,body,#app{width:100%;height:100%;min-height:0;overflow:hidden!important;background:var(--ui-bg)!important}
body{background:radial-gradient(ellipse at 52% 37%,#07141a 0,#020607 55%,#010203 100%)!important}
body:before{opacity:.045!important;background-size:52px 52px!important}
body:after{display:none!important}
#hud-canvas{display:none!important;opacity:0!important}
button,input,select{font-family:inherit!important}
button{border-radius:3px!important;box-shadow:none!important;transition:background .12s,border-color .12s,color .12s!important}
button:hover{transform:none!important;box-shadow:none!important}
.panel,.glass-card,.holo-panel,.chat-panel,.floating-nav,.bottom-composer,.palette-modal{backdrop-filter:none!important;box-shadow:none!important}

/* Thin top rail like the reference. */
.topbar{position:fixed!important;z-index:12!important;left:0!important;right:0!important;top:0!important;height:36px!important;min-height:36px!important;padding:0 9px!important;display:flex!important;gap:9px!important;border:0!important;border-bottom:1px solid rgba(91,214,226,.13)!important;background:#05090b!important}
.thinking-pill{min-width:150px!important;height:32px!important;padding:3px 8px!important;gap:7px!important;border:0!important;border-radius:0!important;background:transparent!important;box-shadow:none!important}
.thinking-wave{width:19px!important}
.thinking-wave i{animation-duration:2.3s!important;box-shadow:none!important}
.thinking-copy strong{font-size:.68rem!important;letter-spacing:.17em!important}
.thinking-copy small{font-size:.43rem!important;letter-spacing:.08em!important;color:#4c777e!important}
.floating-nav{position:absolute!important;left:50%!important;top:3px!important;transform:translateX(-50%)!important;display:flex!important;align-items:center!important;gap:1px!important;padding:2px!important;border:1px solid rgba(91,214,226,.12)!important;border-radius:3px!important;background:#070d0f!important;white-space:nowrap!important}
.palette-btn{height:27px!important;padding:4px 7px!important;border:0!important;border-radius:2px!important;background:transparent!important;font-size:.54rem!important;letter-spacing:.035em!important;color:#8aa8ad!important}
.search-glyph{font-size:1rem!important}
.floating-nav kbd{display:none!important}
.nav-divider{height:17px!important;margin:0 2px!important;background:rgba(91,214,226,.12)!important}
.nav-link{padding:6px 9px!important;border:0!important;border-radius:2px!important;background:transparent!important;color:#607d82!important;font-size:.53rem!important;letter-spacing:.08em!important}
.nav-link.active,.nav-link:hover{background:rgba(53,170,184,.11)!important;color:#dffcff!important;box-shadow:inset 0 -1px #45b5c1!important}
.top-right{margin-left:auto!important;gap:5px!important}
.status-chip{padding:5px 7px!important;border:1px solid rgba(91,214,226,.12)!important;border-radius:2px!important;background:#04090b!important;font-size:.49rem!important;letter-spacing:.06em!important}
.gesture-toggle{display:flex!important;padding:4px 6px!important;gap:4px!important;border:1px solid rgba(91,214,226,.12)!important;border-radius:2px!important;background:#04090b!important;font-size:.45rem!important;letter-spacing:.04em!important}
.toggle-track{width:22px!important;height:12px!important}

/* Left feed uses the existing time, weather, and telemetry data. */
.left-dock{display:flex!important;position:fixed!important;z-index:4!important;left:6px!important;top:42px!important;bottom:7px!important;right:auto!important;width:var(--left-rail)!important;flex-direction:column!important;gap:6px!important;overflow-y:auto!important;overflow-x:hidden!important;padding:0!important;scrollbar-width:thin!important;scrollbar-color:#17343a transparent!important}
.glass-card{flex:0 0 auto!important;min-width:0!important;padding:9px 10px!important;border:1px solid var(--ui-border)!important;border-radius:3px!important;background:#050a0d!important;overflow:hidden!important}
.card-eyebrow{font-size:.48rem!important;letter-spacing:.11em!important;color:#5e9aa2!important}
.card-index{font-size:.43rem!important;color:#3b666c!important}
.time-card .clock{margin-top:8px!important;font-size:clamp(1.1rem,1.55vw,1.55rem)!important;letter-spacing:.035em!important;line-height:1.1!important;color:#e5feff!important;text-shadow:none!important}
.time-card .date{margin-top:4px!important;font-size:.57rem!important;line-height:1.35!important;color:#829fa4!important}
.time-card .timezone{margin-top:8px!important;padding-top:7px!important;font-size:.43rem!important;letter-spacing:.035em!important;color:#5c8389!important;white-space:normal!important;line-height:1.35!important}
.weather-top{margin-top:8px!important;gap:8px!important}
.weather-icon{width:31px!important;font-size:1.4rem!important;text-align:left!important;color:#b8faff!important;text-shadow:none!important}
.temp{font-size:1.28rem!important;line-height:1!important;font-weight:400!important;color:#e7feff!important}
.condition{margin-top:4px!important;font-size:.55rem!important;line-height:1.3!important;color:#9bb6ba!important}
.weather-card .city{margin-top:8px!important;font-size:.45rem!important;letter-spacing:.045em!important;color:#77a0a6!important}
.weather-grid{display:grid!important;grid-template-columns:1fr 1fr!important;gap:4px 7px!important;margin-top:9px!important}
.metric{padding:5px 0 0!important;border-top:1px solid rgba(103,224,235,.08)!important;background:transparent!important}
.metric small{margin-bottom:2px!important;font-size:.43rem!important;color:#52767c!important}
.metric strong{font-size:.63rem!important;color:#bedadd!important}
.weather-footer{margin-top:8px!important;padding-top:6px!important;font-size:.4rem!important;color:#44646a!important}
.weather-card .card-eyebrow button{padding:0 3px!important;font-size:.88rem!important}
.telemetry-card{display:block!important;flex:1 0 170px!important;min-height:150px!important;overflow:hidden!important}
.telemetry-card .sysrow{margin-top:7px!important;font-size:.57rem!important;color:#709198!important}
.telemetry-card .sysrow b{font-size:.55rem!important;color:#c5f9ff!important}
.telemetry-card .meter{height:2px!important;margin-top:4px!important;background:rgba(97,220,231,.10)!important}
.telemetry-card .meter span{box-shadow:none!important;transition:width .3s!important}
.telemetry-foot{margin-top:8px!important;padding-top:6px!important;border-top:1px solid rgba(103,224,235,.08)!important;font-size:.4rem!important;color:#50757b!important}

/* Center stage: upper topology + core, lower visual hub. */
#main-stage{position:fixed!important;z-index:2!important;top:42px!important;bottom:64px!important;left:calc(var(--left-rail) + 13px)!important;right:calc(var(--right-rail) + 13px)!important;inset:42px calc(var(--right-rail) + 13px) 64px calc(var(--left-rail) + 13px)!important;display:block!important;overflow:hidden!important;padding:0!important;pointer-events:none!important}
.orb-overline{display:none!important}
.holo-deck{position:absolute!important;inset:0!important;height:auto!important;display:grid!important;grid-template-columns:minmax(0,.78fr) minmax(0,1fr)!important;grid-template-rows:minmax(0,.47fr) minmax(0,.53fr)!important;gap:6px!important;margin:0!important;padding:0!important;pointer-events:none!important}
.holo-panel{position:relative!important;left:auto!important;right:auto!important;top:auto!important;bottom:auto!important;transform:none!important;width:auto!important;height:auto!important;min-width:0!important;min-height:0!important;margin:0!important;padding:9px!important;border:1px solid rgba(82,218,232,.17)!important;border-radius:3px!important;background:linear-gradient(145deg,rgba(5,14,17,.95),rgba(3,8,10,.96))!important;box-shadow:none!important;overflow:hidden!important;color:#e6fdff!important;pointer-events:auto!important}
.holo-panel:before{left:0!important;top:0!important;width:25px!important;height:1px!important;background:#82f6ff!important;box-shadow:none!important}
.holo-panel-head{position:relative!important;left:auto!important;top:auto!important;transform:none!important;width:100%!important;height:19px!important;min-height:19px!important;display:flex!important;align-items:center!important;justify-content:space-between!important;gap:8px!important;margin:0!important;padding:0 2px 5px!important;border-bottom:1px solid rgba(83,220,232,.09)!important;font-size:.47rem!important;letter-spacing:.09em!important;color:#6fa1a7!important;white-space:nowrap!important}
.holo-live-state{font-size:.4rem!important;color:#5b858a!important}
.holo-panel .tiny-live{box-shadow:none!important}
.system-panel{grid-column:1!important;grid-row:1!important;display:flex!important;flex-direction:column!important;gap:0!important;background:#04090b!important}
.system-panel>.holo-panel-head{flex:0 0 20px!important}
#system-map{position:relative!important;flex:1 1 auto!important;width:100%!important;min-height:110px!important;height:auto!important;margin:4px 0 3px!important;overflow:hidden!important}
.system-lines{position:absolute!important;inset:0!important;width:100%!important;height:100%!important;overflow:visible!important}
.system-lines path{fill:none!important;stroke:rgba(79,224,238,.47)!important;stroke-width:1.25!important;stroke-dasharray:4 5!important;animation:none!important}
.system-lines circle{fill:#92f8ff!important;stroke:none!important;opacity:.75!important;filter:none!important}
.system-node{position:absolute!important;z-index:2!important;left:0!important;right:auto!important;width:72px!important;min-width:0!important;min-height:24px!important;height:24px!important;padding:3px 5px!important;border:1px solid rgba(91,223,235,.14)!important;border-radius:2px!important;background:#070f12!important;color:#91b7bd!important;font-size:.44rem!important;letter-spacing:.055em!important;text-align:left!important;box-shadow:none!important;backdrop-filter:none!important;white-space:nowrap!important}
.system-node i{display:inline-block!important;width:4px!important;height:4px!important;margin-right:4px!important;border-radius:50%!important;background:#527a80!important;box-shadow:none!important}
.system-node.online i{background:#6df1cf!important}
.system-node.busy i{background:#ffd17f!important;animation:none!important}
.system-node.selected{border-color:rgba(125,245,255,.52)!important;color:#ecffff!important;background:#0a1b20!important;box-shadow:inset 2px 0 rgba(104,240,250,.7)!important}
.node-voice{top:calc(14% - 12px)!important}
.node-vision{top:calc(39% - 12px)!important}
.node-tools{top:calc(65% - 12px)!important}
.node-telemetry{top:calc(91% - 12px)!important}
.node-core{left:auto!important;right:0!important;top:calc(52% - 12px)!important;width:62px!important;text-align:center!important;transform:none!important;background:#061318!important;border-color:rgba(91,223,235,.36)!important}
.node-core:hover,.node-core.selected{transform:none!important}
.system-detail{flex:0 0 auto!important;min-height:34px!important;margin:0!important;padding:5px 1px 0!important;border-top:1px solid rgba(91,223,235,.09)!important;overflow:hidden!important}
.system-detail>div{display:flex!important;align-items:center!important;gap:5px!important;min-width:0!important}
.system-detail b{min-width:0!important;font-size:.43rem!important;letter-spacing:.035em!important;color:#a4dce1!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important}
.system-detail span{margin-left:auto!important;font-size:.39rem!important;color:#5f9196!important;white-space:nowrap!important}
.system-detail p{margin:3px 0 0!important;min-height:0!important;font-size:.46rem!important;line-height:1.25!important;color:#5c7d83!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important}
.model-panel{grid-column:2!important;grid-row:1!important;display:flex!important;flex-direction:column!important;padding:9px!important;background:radial-gradient(ellipse at 50% 52%,rgba(11,66,76,.22),rgba(3,9,11,.96) 65%)!important;border-color:rgba(90,222,235,.3)!important}
.model-panel:before{display:block!important;width:34px!important;background:#a3fbff!important}
.model-panel>.holo-panel-head{flex:0 0 20px!important;justify-content:center!important;gap:9px!important}
.model-viewport{position:relative!important;inset:auto!important;flex:1 1 auto!important;width:100%!important;height:auto!important;min-height:105px!important;max-height:none!important;margin:2px 0 0!important;border:0!important;border-radius:0!important;overflow:hidden!important;background:radial-gradient(ellipse at 50% 48%,rgba(39,145,160,.12),rgba(2,8,10,.04) 58%,transparent 80%)!important}
#model-canvas{position:absolute!important;inset:0!important;display:block!important;width:100%!important;height:100%!important;z-index:1!important;pointer-events:none!important}
.model-reticle{inset:6% 9%!important;border:0!important;opacity:.8!important}
.model-reticle:before{width:78%!important;height:75%!important;border:1px solid rgba(106,228,240,.075)!important;border-radius:50%!important}
.model-reticle:after{width:1px!important;height:92%!important;border:0!important;border-left:1px dashed rgba(106,228,240,.09)!important;border-radius:0!important}
.model-readout{left:6%!important;right:6%!important;bottom:2px!important;font-size:.4rem!important;letter-spacing:.07em!important;color:#507a80!important}
.model-readout span:first-child{color:#8fdae0!important}
#core-title{position:relative!important;left:auto!important;top:auto!important;transform:none!important;z-index:3!important;display:flex!important;flex:0 0 18px!important;align-items:center!important;justify-content:center!important;gap:9px!important;width:auto!important;min-height:18px!important;margin:0!important;padding:0!important;text-align:center!important;pointer-events:none!important;text-shadow:none!important;color:#8ab9be!important}
#core-title>strong{font-size:.48rem!important;letter-spacing:.12em!important;font-weight:500!important;color:#9acdd2!important}
#core-state{display:flex!important;align-items:center!important;justify-content:center!important;gap:5px!important;margin:0!important;font-size:.41rem!important;letter-spacing:.04em!important;color:#6c9da2!important;white-space:nowrap!important}
#core-state .led{width:4px!important;height:4px!important;box-shadow:none!important}
.model-selectors{position:relative!important;left:auto!important;bottom:auto!important;transform:none!important;z-index:4!important;display:flex!important;flex:0 0 25px!important;gap:4px!important;width:100%!important;margin:1px 0 0!important;pointer-events:auto!important}
.model-select{flex:1 1 0!important;min-width:0!important;min-height:23px!important;height:23px!important;padding:3px 4px!important;border:1px solid rgba(92,218,231,.15)!important;border-radius:2px!important;background:#050d10!important;color:#60888d!important;font: .43rem ui-monospace,Consolas,monospace!important;letter-spacing:.10em!important;box-shadow:none!important}
.model-select.active,.model-select:hover{border-color:rgba(124,245,255,.55)!important;background:#0a1c21!important;color:#e5ffff!important;box-shadow:inset 0 -1px rgba(124,245,255,.23)!important}

/* The lower panel remains the working task monitor; a subtle isometric graphic is only decoration. */
.tasks-panel{grid-column:1/-1!important;grid-row:2!important;display:block!important;background:#04090b!important}
.tasks-panel>.holo-panel-head{position:absolute!important;left:9px!important;right:9px!important;top:8px!important;width:auto!important;z-index:4!important;height:20px!important}
.hub-graphic{position:absolute!important;left:3%!important;top:22%!important;width:24%!important;height:58%!important;perspective:360px!important;transform-style:preserve-3d!important;pointer-events:none!important;opacity:.95!important}
.hub-plane{position:absolute!important;left:50%!important;top:50%!important;display:block!important;width:82%!important;height:51%!important;border:1px solid rgba(59,220,238,.6)!important;background-image:linear-gradient(90deg,transparent 24%,rgba(72,219,236,.12) 25%,transparent 26%,transparent 49%,rgba(72,219,236,.12) 50%,transparent 51%,transparent 74%,rgba(72,219,236,.12) 75%,transparent 76%),linear-gradient(transparent 24%,rgba(72,219,236,.12) 25%,transparent 26%,transparent 49%,rgba(72,219,236,.12) 50%,transparent 51%,transparent 74%,rgba(72,219,236,.12) 75%,transparent 76%)!important;background-color:rgba(17,87,99,.05)!important;transform-style:preserve-3d!important}
.hub-plane-base{transform:translate(-50%,-25%) rotateX(58deg) rotateZ(45deg)!important;opacity:.4!important}
.hub-plane-mid{transform:translate(-50%,-50%) rotateX(58deg) rotateZ(45deg)!important;opacity:.62!important}
.hub-plane-top{transform:translate(-50%,-76%) rotateX(58deg) rotateZ(45deg)!important;opacity:.95!important}
.hub-bar{position:absolute!important;z-index:3!important;top:12%!important;display:block!important;width:8%!important;height:32%!important;border:1px solid rgba(100,240,252,.68)!important;background:linear-gradient(90deg,rgba(9,44,55,.82),rgba(38,168,188,.5),rgba(10,54,66,.78))!important;transform:skewY(-25deg) rotateX(8deg)!important;box-shadow:none!important}
.hub-bar-1{left:32%!important;height:24%!important;top:23%!important}
.hub-bar-2{left:43%!important;height:30%!important;top:17%!important}
.hub-bar-3{left:54%!important;height:36%!important;top:11%!important}
.hub-bar-4{left:65%!important;height:27%!important;top:19%!important}
.hub-scan{position:absolute!important;left:12%!important;right:12%!important;top:49%!important;height:1px!important;background:linear-gradient(90deg,transparent,rgba(127,249,255,.7),transparent)!important;box-shadow:none!important}
#caption{position:absolute!important;left:29%!important;right:43%!important;top:35%!important;bottom:auto!important;transform:none!important;width:auto!important;min-height:0!important;max-height:6.3em!important;overflow:hidden!important;margin:0!important;padding:0!important;text-align:left!important;display:-webkit-box!important;-webkit-line-clamp:4!important;-webkit-box-orient:vertical!important;color:#a2c4c8!important;text-shadow:none!important;font-size:.63rem!important;line-height:1.55!important;letter-spacing:0!important;pointer-events:none!important;overflow-wrap:anywhere!important}
#caption.you{color:#6f969c!important}
.task-list{position:absolute!important;left:54%!important;right:10px!important;top:36px!important;bottom:65px!important;display:flex!important;flex-direction:column!important;justify-content:space-evenly!important;gap:2px!important;margin:0!important;padding:0!important;overflow:hidden!important}
.task-row{position:relative!important;display:grid!important;grid-template-columns:4px minmax(0,1fr) auto!important;grid-template-rows:auto 2px!important;align-items:center!important;column-gap:7px!important;row-gap:4px!important;min-height:30px!important;padding:3px 1px 4px!important;border-bottom:1px solid rgba(87,211,223,.08)!important;background:transparent!important}
.task-indicator{grid-column:1!important;grid-row:1!important;width:4px!important;height:4px!important;background:#34565b!important;box-shadow:none!important;animation:none!important}
.task-row.active .task-indicator{background:#9bffff!important;box-shadow:none!important;animation:none!important}
.task-copy{grid-column:2!important;grid-row:1!important;min-width:0!important;display:flex!important;flex-direction:column!important;gap:2px!important}
.task-copy b{font-size:.47rem!important;letter-spacing:.06em!important;color:#96bfc3!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important}
.task-copy small{display:block!important;font-size:.42rem!important;line-height:1.2!important;color:#4f747a!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important}
.task-status{grid-column:3!important;grid-row:1!important;font-size:.4rem!important;letter-spacing:.035em!important;color:#628d92!important;white-space:nowrap!important}
.task-row.active .task-copy b,.task-row.active .task-status{color:#d0fbff!important}
.task-bar{grid-column:2/4!important;grid-row:2!important;display:block!important;height:2px!important;margin:0!important;background:rgba(83,214,226,.07)!important;overflow:hidden!important}
.task-bar i{display:block!important;height:100%!important;width:30%!important;background:#4b98a1!important;transform:none!important;animation:none!important;box-shadow:none!important}
.task-row.active .task-bar i{width:42%!important;background:#8bf6ff!important;animation:taskSweep 2.8s ease-in-out infinite!important}
.task-row.busy .task-bar i{width:66%!important;background:#baffff!important;animation:taskSweep 1.8s ease-in-out infinite!important}
.telemetry-mini{position:absolute!important;left:9px!important;right:9px!important;bottom:29px!important;display:grid!important;grid-template-columns:repeat(4,minmax(0,1fr))!important;gap:5px!important;margin:0!important;padding:6px 1px 0!important;border-top:1px solid rgba(87,211,223,.10)!important}
.telemetry-mini>div{min-width:0!important;display:flex!important;flex-direction:row!important;align-items:center!important;justify-content:center!important;gap:7px!important}
.telemetry-mini span{font-size:.42rem!important;letter-spacing:.07em!important;color:#4f777c!important}
.telemetry-mini b{font-size:.5rem!important;font-weight:400!important;color:#c1f9ff!important}
.task-footer{position:absolute!important;left:10px!important;right:10px!important;bottom:7px!important;display:flex!important;align-items:center!important;gap:6px!important;margin:0!important;padding:0!important;font-size:.39rem!important;letter-spacing:.04em!important;color:#4b6e73!important;white-space:nowrap!important;overflow:hidden!important}
.task-footer #task-frame{display:inline!important;margin-left:auto!important}

/* Put the voice panel in its reference position; typed chat and history still work. */
#chat-panel{position:fixed!important;display:flex!important;z-index:7!important;top:42px!important;right:6px!important;bottom:7px!important;left:auto!important;width:var(--right-rail)!important;max-width:none!important;min-width:0!important;height:auto!important;max-height:none!important;overflow:hidden!important;border:1px solid rgba(90,215,228,.16)!important;border-radius:3px!important;background:#050a0d!important;box-shadow:none!important;backdrop-filter:none!important}
.chat-header{height:44px!important;padding:0 9px!important;border-bottom:1px solid rgba(83,214,226,.10)!important}
.chat-title-mark{width:23px!important;height:23px!important;border-radius:2px!important;background:#071417!important;box-shadow:none!important}
.chat-heading strong{font-size:.58rem!important;letter-spacing:.12em!important}
.chat-heading small{font-size:.4rem!important;color:#4f777d!important}
.new-chat-btn{padding:5px 7px!important;border:1px solid rgba(91,214,226,.13)!important;border-radius:2px!important;background:#071114!important;font-size:.45rem!important}
.chat-tabs{height:33px!important;padding:0 7px!important;gap:1px!important;border-bottom:1px solid rgba(83,214,226,.09)!important}
.chat-tab{padding:0 9px!important;font-size:.47rem!important;letter-spacing:.09em!important}
.chat-tab.active{color:#c4f9ff!important}
.chat-tab.active:after{height:1px!important;box-shadow:none!important}
.chat-tabs #feed-count{font-size:.39rem!important;color:#4b7278!important}
.chat-feed-wrap{flex:1 1 auto!important;min-height:0!important;overflow:hidden!important}
.feed{gap:6px!important;padding:9px 8px 12px!important;scrollbar-width:thin!important;scrollbar-color:#1a363b transparent!important}
.feed:empty:after{font-size:.52rem!important;line-height:1.8!important;letter-spacing:.08em!important;color:#55767c!important}
.logline{max-width:96%!important;padding:7px 8px!important;border-radius:3px!important;font-size:.61rem!important;line-height:1.45!important;background:#071013!important;border-color:rgba(91,214,226,.09)!important}
.logline.you{background:#09171b!important;border-color:rgba(91,214,226,.17)!important}
.logline.nexus{background:#070d10!important}
.logline.sys{font-size:.45rem!important}
.logline.tool{font-size:.48rem!important}
.chat-automation{gap:7px!important;padding:8px!important;border-top:1px solid rgba(83,214,226,.09)!important;background:#04080a!important}
.automation-spark{font-size:.72rem!important;color:#6bdce7!important}
.automation-copy strong{font-size:.43rem!important;letter-spacing:.04em!important;color:#78a8ad!important}
.automation-copy small{font-size:.48rem!important;color:#53767c!important}
.side-composer{margin:7px 7px 0!important;padding:4px 4px 4px 8px!important;gap:5px!important;border:1px solid rgba(91,214,226,.18)!important;border-radius:3px!important;background:#030809!important}
.side-composer input{font-size:.61rem!important;padding:7px 0!important}
.side-composer input::placeholder{color:#4e6d72!important}
.side-composer button{width:27px!important;height:27px!important;border-radius:2px!important;background:#0b242a!important;border-color:rgba(91,214,226,.23)!important;color:#d9fdff!important;box-shadow:none!important}
.chat-footnote{padding:7px 8px!important;font-size:.38rem!important;letter-spacing:.025em!important;color:#416167!important}

/* Keep the existing global voice controls, but de-duplicate the text composer visually. */
.bottom-composer-wrap{position:fixed!important;z-index:8!important;left:calc(var(--left-rail) + (100vw - var(--left-rail) - var(--right-rail))/2 + 6px)!important;right:auto!important;bottom:7px!important;transform:translateX(-50%)!important;width:auto!important;min-width:0!important;max-width:calc(100vw - var(--left-rail) - var(--right-rail) - 30px)!important}
.composer-context,.composer-hint,.composer-leading,#command-input,#command-send{display:none!important}
.bottom-composer{display:flex!important;width:max-content!important;min-width:0!important;min-height:0!important;margin:0 auto!important;padding:3px!important;gap:4px!important;border:1px solid rgba(92,217,229,.13)!important;border-radius:3px!important;background:#04090b!important;box-shadow:none!important}
.composer-actions{display:flex!important;gap:4px!important}
.composer-icon{width:29px!important;height:27px!important;border-radius:2px!important;border:1px solid rgba(92,217,229,.12)!important;background:#071114!important;color:#8db4ba!important;font-size:.7rem!important}
.composer-icon.danger{color:#ffb8b8!important}
.voice-start{height:27px!important;min-width:100px!important;padding:6px 9px!important;border:1px solid rgba(92,217,229,.25)!important;border-radius:2px!important;background:#0b252b!important;font-size:.46rem!important;letter-spacing:.1em!important;color:#d8fcff!important}

/* Settings, palette, start/retry overlay retain their existing behavior. */
#settings{z-index:30!important;background:#050a0d!important;box-shadow:-15px 0 50px rgba(0,0,0,.35)!important;backdrop-filter:none!important}
#settings-scrim{z-index:29!important;backdrop-filter:none!important;background:rgba(0,3,5,.76)!important}
.palette-modal,.palette-scrim{backdrop-filter:none!important}
#start{z-index:40!important}

@media(max-width:1000px) and (min-width:761px){
 :root{--left-rail:164px;--right-rail:270px}
 .left-dock{left:5px!important}.chat-panel{right:5px!important}
 #main-stage{left:calc(var(--left-rail) + 9px)!important;right:calc(var(--right-rail) + 9px)!important}
 .holo-deck{grid-template-columns:minmax(0,.72fr) minmax(0,1fr)!important}
 .system-node{width:59px!important;font-size:.4rem!important}
 .node-core{width:52px!important}
 .hub-graphic{left:1%!important;width:27%!important}
 #caption{left:29%!important;right:45%!important;font-size:.55rem!important}
 .task-list{left:52%!important;right:5px!important}
 .telemetry-mini>div{gap:3px!important}
 .telemetry-mini span{font-size:.38rem!important}.telemetry-mini b{font-size:.44rem!important}
 .gesture-toggle{font-size:0!important;padding:4px!important}.gesture-dot{display:none!important}
}
@media(max-width:760px){
 :root{--left-rail:0px;--right-rail:0px}
 .topbar{height:44px!important;min-height:44px!important;padding:0 5px!important;gap:3px!important}
 .thinking-pill{min-width:72px!important;width:auto!important;padding:3px!important}
 .thinking-wave{display:none!important}.thinking-copy strong{font-size:.55rem!important}.thinking-copy small{display:none!important}
 .floating-nav{left:50%!important;top:6px!important;max-width:calc(100vw - 160px)!important;overflow:hidden!important}
 .palette-btn{display:none!important}.nav-divider{display:none!important}.nav-link{padding:5px 6px!important;font-size:.46rem!important}
 .top-right{gap:3px!important}.gesture-toggle{display:none!important}.status-chip{padding:4px 5px!important;font-size:.4rem!important}
 .left-dock{position:fixed!important;left:5px!important;right:5px!important;top:48px!important;bottom:auto!important;width:auto!important;height:67px!important;display:flex!important;flex-direction:row!important;gap:5px!important;overflow:hidden!important}
 .glass-card{flex:1 1 0!important;min-width:0!important;padding:6px!important}
 .time-card .clock{font-size:1.05rem!important;margin-top:5px!important}.time-card .date{display:none!important}.time-card .timezone{margin-top:4px!important;padding-top:4px!important;font-size:.34rem!important}
 .weather-top{margin-top:5px!important}.weather-icon{width:22px!important;font-size:1.05rem!important}.temp{font-size:1rem!important}.condition{font-size:.43rem!important}.weather-card .city,.weather-grid,.weather-footer{display:none!important}
 .telemetry-card{display:none!important}
 #main-stage{position:fixed!important;inset:122px 5px 70px!important;left:5px!important;right:5px!important;top:122px!important;bottom:70px!important;overflow-y:auto!important;overflow-x:hidden!important;scrollbar-width:thin!important;pointer-events:auto!important}
 .holo-deck{position:relative!important;inset:auto!important;display:grid!important;grid-template-columns:minmax(0,1fr)!important;grid-template-rows:auto!important;gap:6px!important;height:auto!important;padding:0 0 8px!important;pointer-events:auto!important}
 .holo-panel{position:relative!important;display:flex!important;flex-direction:column!important;grid-column:1!important;grid-row:auto!important;min-height:0!important;height:auto!important;padding:8px!important}
 .system-panel{order:2!important;min-height:180px!important}.model-panel{order:1!important;height:300px!important;min-height:300px!important}.tasks-panel{order:3!important;height:280px!important;min-height:280px!important}
 .model-viewport{min-height:190px!important}.model-selectors{flex-basis:25px!important}.system-map{min-height:110px!important}.system-detail{min-height:34px!important}
 .hub-graphic{left:1%!important;top:23%!important;width:28%!important;height:55%!important}
 #caption{left:30%!important;right:42%!important;top:34%!important;font-size:.5rem!important;max-height:5.5em!important}
 .task-list{left:55%!important;right:5px!important;top:34px!important;bottom:65px!important}
 .task-row{min-height:28px!important}.task-copy small{display:none!important}
 .telemetry-mini{left:7px!important;right:7px!important;bottom:28px!important}.telemetry-mini>div{gap:3px!important}.telemetry-mini span{font-size:.38rem!important}.telemetry-mini b{font-size:.43rem!important}
 .task-footer{font-size:.35rem!important}
 #chat-panel{display:none!important;top:48px!important;left:5px!important;right:5px!important;bottom:62px!important;width:auto!important;max-height:none!important;z-index:24!important}
 #app.mobile-chat-open #chat-panel{display:flex!important}
 .bottom-composer-wrap{left:50%!important;right:auto!important;bottom:5px!important;transform:translateX(-50%)!important;max-width:calc(100vw - 14px)!important}
 .composer-icon{width:27px!important;height:26px!important}.voice-start{height:26px!important;min-width:94px!important}
}
@media(prefers-reduced-motion:reduce){.task-row.active .task-bar i,.task-row.busy .task-bar i{animation:none!important}}

\n/* Rounded finish: soften edges without changing layout or behavior. */
:root{--radius-xs:6px;--radius-sm:8px;--radius-md:12px;--radius-lg:16px}
button,.iconbtn,.palette-btn,.nav-link,.status-chip,.floating-nav{border-radius:8px!important}
.panel,.glass-card,.holo-panel:not(.model-panel),.system-panel,.tasks-panel,.chat-panel,#chat-panel,.conversation,.automation,.palette-modal,.settings-card,.setting{border-radius:12px!important}
.metric,.weather-grid .metric,.system-detail,.task-row,.logline{border-radius:7px!important}
.system-node,.node-core,.node-telemetry{border-radius:8px!important}
.model-select,.model-selectors button{border-radius:8px!important}
.bottom-composer,.side-composer,.chat-input,.chat-field,.message-input{border-radius:13px!important}
.composer-icon,.send-button,.voice-start{border-radius:9px!important}
#settings{border-radius:16px 0 0 16px!important}
.toast,.toast-message{border-radius:10px!important}
.palette-modal input,.palette-modal button,.setting select,.setting input[type=text]{border-radius:8px!important}
/* Keep the large hologram clean; only round its subtle viewport boundary. */
.model-viewport{border-radius:18px!important}
.holo-panel:not(.model-panel),.panel,.glass-card,#chat-panel{border-color:rgba(130,247,255,.16)!important}
button:focus-visible,input:focus-visible,select:focus-visible{border-radius:8px!important}
@media(max-width:760px){#chat-panel{border-radius:14px!important}.holo-panel:not(.model-panel),.panel,.glass-card{border-radius:11px!important}.model-panel{border-radius:16px!important}}


/* Particle-core refinement: visual feedback without a heavyweight 3D engine. */
html.reference-ui-light .panel,
html.reference-ui-light .holo-panel,
html.reference-ui-light .chat-panel,
html.reference-ui-light .glass-card,
html.reference-ui-light .palette-modal,
html.reference-ui-light #settings,
html.reference-ui-light #settings-scrim { backdrop-filter:none!important; -webkit-backdrop-filter:none!important; }
html.reference-ui-light body:before { opacity:.055!important; }
html.reference-ui-light body:after { box-shadow:none!important; }
.model-viewport[data-model="core"] .model-reticle { display:none!important; }
#core-state .led { transition:background-color .16s ease,box-shadow .16s ease; }
#core-state[data-state="listening"] .led {
 background:#62aaff!important; box-shadow:0 0 4px #62aaff,0 0 11px rgba(61,142,255,.72)!important;
 animation:nexus-listen-pulse 1.45s ease-in-out infinite;
}
#core-state[data-state="speaking"] .led {
 background:#efffff!important; box-shadow:0 0 5px #efffff,0 0 14px rgba(93,220,255,.95)!important;
 animation:nexus-speak-pulse .34s ease-in-out infinite alternate;
}
#core-state[data-state="thinking"] .led {
 background:#f6bb70!important; box-shadow:0 0 9px rgba(246,187,112,.8)!important;
 animation:nexus-listen-pulse .9s ease-in-out infinite;
}
.model-viewport[data-voice-state="listening"] { background:radial-gradient(ellipse at 50% 52%,rgba(29,83,170,.105),rgba(3,8,16,.02) 65%,transparent 82%)!important; }
.model-viewport[data-voice-state="speaking"] { background:radial-gradient(ellipse at 50% 52%,rgba(75,180,255,.15),rgba(3,8,16,.02) 65%,transparent 82%)!important; }
@keyframes nexus-listen-pulse { 0%,100% { opacity:.64; transform:scale(.88); } 50% { opacity:1; transform:scale(1.12); } }
@keyframes nexus-speak-pulse { from { opacity:.64; transform:scale(.92); } to { opacity:1; transform:scale(1.2); } }
@media(prefers-reduced-motion:reduce) { #core-state .led { animation:none!important; } }


.pending-file-action{margin-top:10px;padding:12px;border:1px solid rgba(255,170,110,.28);background:rgba(34,18,10,.24);border-radius:10px;display:flex;flex-direction:column;gap:7px}.pending-file-action>b{font-size:.63rem;letter-spacing:.08em;color:#ffd2ad}.pending-file-action>small{overflow-wrap:anywhere;color:#9fb5bd;font-size:.61rem;line-height:1.5}.pending-action-buttons{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px}.pending-action-buttons button{padding:8px 10px;border:1px solid rgba(138,247,255,.24);border-radius:8px;background:rgba(9,24,30,.85);color:#d9faff;font: .57rem 'Share Tech Mono',monospace;letter-spacing:.04em;cursor:pointer}.pending-action-buttons button:first-child{border-color:rgba(255,170,110,.46);color:#ffe1c8}.pending-action-buttons button:disabled{opacity:.5;cursor:wait}
@media(max-width:490px){.pending-action-buttons button{flex:1}}

.accent-profile-value{display:flex;align-items:center;gap:8px;min-height:36px;padding:9px 11px;border:1px solid rgba(var(--a-rgb),.25);border-radius:10px;background:rgba(var(--a-rgb),.045);color:#c5f6fa;font:.58rem 'Share Tech Mono',monospace;letter-spacing:.05em}.accent-live-dot{width:6px;height:6px;flex:none;border-radius:50%;background:#91f6ec;box-shadow:0 0 8px rgba(145,246,236,.7)}

/* First-run Gemini API-key setup: key is never placed in localStorage or returned by the API. */
#gemini-key-scrim{position:fixed;inset:0;z-index:1000;display:none;align-items:center;justify-content:center;padding:18px;background:rgba(1,5,10,.88)}
#gemini-key-scrim.open{display:flex}
.gemini-key-dialog{width:min(520px,100%);border:1px solid rgba(138,247,255,.28);border-radius:18px;padding:26px;background:linear-gradient(155deg,rgba(7,22,33,.99),rgba(2,8,14,.99));box-shadow:0 20px 80px rgba(0,0,0,.55);color:#e5faff}
.gemini-key-kicker{font:.58rem 'Share Tech Mono',monospace;letter-spacing:.16em;color:#8af7ff;margin-bottom:10px}
.gemini-key-dialog h2{font-size:1.35rem;font-weight:500;letter-spacing:.035em;margin:0 0 10px}
.gemini-key-dialog p{color:#91aebc;font-size:.78rem;line-height:1.65;margin:8px 0 15px}
.gemini-key-dialog a{color:#9bf5ff;text-decoration:none}.gemini-key-dialog a:hover{text-decoration:underline}
.gemini-key-field{display:flex;gap:8px;align-items:stretch}.gemini-key-field input{min-width:0;flex:1;padding:12px 13px;background:#020a10;color:#edfcff;border:1px solid rgba(138,247,255,.28);border-radius:10px;font: .75rem 'Share Tech Mono',monospace;outline:none}.gemini-key-field input:focus{border-color:#8af7ff;box-shadow:0 0 0 2px rgba(138,247,255,.09)}
.gemini-key-field button,.gemini-key-actions button{padding:10px 13px;border:1px solid rgba(138,247,255,.24);border-radius:10px;background:rgba(138,247,255,.07);color:#dffcff;font:.62rem 'Share Tech Mono',monospace;cursor:pointer}
.gemini-key-actions{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-top:17px}.gemini-key-actions .primary{background:#bffaff;color:#001115;border-color:#bffaff;font-weight:700}.gemini-key-actions button:disabled{opacity:.5;cursor:wait}
#gemini-key-feedback{min-height:1.2em;margin:10px 0 0;font-size:.69rem;color:#9bf5ff}#gemini-key-feedback.error{color:#ffb9a7}
.gemini-key-status{display:flex;gap:8px;align-items:center;font:.59rem 'Share Tech Mono',monospace;color:#9bf5ff;margin-top:8px;overflow-wrap:anywhere}
.gemini-key-settings-field{margin-top:10px}
@media(max-width:490px){.gemini-key-dialog{padding:20px}.gemini-key-dialog h2{font-size:1.1rem}.gemini-key-actions{align-items:stretch;flex-direction:column-reverse}.gemini-key-actions button{width:100%}}

</style>
</head><body>
<canvas id="hud-canvas" aria-hidden="true"></canvas>
<div id="app">
 <header class="topbar">
  <div class="thinking-pill" aria-live="polite">
   <span class="thinking-wave" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></span>
   <div class="thinking-copy"><strong>NEXUS</strong><small id="sys-label">CORE STANDBY</small></div>
   <i class="led" id="top-led"></i>
  </div>
  <nav class="floating-nav" aria-label="Primary navigation">
   <button id="palette-open" class="palette-btn" type="button"><span class="search-glyph" aria-hidden="true">⌕</span><span>Search / Palette</span><kbd>Ctrl K</kbd></button>
   <span class="nav-divider"></span>
   <button class="nav-link active" id="nav-dashboard" type="button">Dashboard</button>
   <button class="nav-link" id="nav-chat" type="button">Chat</button>
   <button class="nav-link" id="settings-open" type="button">Settings</button>
  </nav>
  <div class="top-right">
   <div class="status-chip"><i class="led" id="status-led"></i><span id="status">OFFLINE</span></div>
   <button class="gesture-toggle" id="gestures-toggle" type="button" aria-pressed="false" title="Gesture indicator only; camera recognition is not configured"><span class="gesture-dot"></span>Gestures<span class="toggle-track"><i></i></span></button>
  </div>
 </header>

 <aside class="left-dock" aria-label="Lahore time and weather">
  <section class="glass-card time-card">
   <div class="card-eyebrow"><span>LOCAL TIME</span><span class="card-index">PK / 01</span></div>
   <div class="clock" id="clock">--:--:--</div>
   <div class="date" id="date">Synchronising Lahore time…</div>
   <div class="timezone"><span class="tiny-live"></span> LAHORE, PAKISTAN · UTC +05:00</div>
  </section>
  <section class="glass-card weather-card">
   <div class="card-eyebrow"><span>LOCAL ENVIRONMENT FEED</span><button id="weather-refresh-button" type="button" aria-label="Refresh weather">↻</button></div>
   <div class="weather-top"><div class="weather-icon" id="weather-icon">◌</div><div><div class="temp" id="weather-temp">--°</div><div class="condition" id="weather-condition">Connecting to weather feed…</div></div></div>
   <div class="city" id="weather-city">LAHORE, PAKISTAN</div>
   <div class="weather-grid">
    <div class="metric"><small>Feels like</small><strong id="weather-feels">--°C</strong></div>
    <div class="metric"><small>Humidity</small><strong id="weather-hum">--%</strong></div>
    <div class="metric"><small>Wind</small><strong id="weather-wind">-- km/h</strong></div>
    <div class="metric"><small>Rain</small><strong id="weather-rain">-- mm</strong></div>
   </div>
   <div class="weather-footer"><span id="weather-refresh">LIVE FEED</span><span>OPEN-METEO</span></div>
  </section>
  <section class="glass-card telemetry-card" aria-label="Local system telemetry">
   <div class="card-eyebrow"><span>LOCAL TELEMETRY</span><span>SYS / 03</span></div>
   <div class="sysrow"><span>Processor</span><b id="vCpu">N/A</b></div><div class="meter"><span id="bCpu"></span></div>
   <div class="sysrow"><span>Memory</span><b id="vRam">N/A</b></div><div class="meter"><span id="bRam"></span></div>
   <div class="sysrow"><span>Network ↓</span><b id="vNetDown">-- KB/s</b></div><div class="meter"><span id="bNetDown"></span></div>
   <div class="sysrow"><span>Network ↑</span><b id="vNetUp">-- KB/s</b></div><div class="meter"><span id="bNetUp"></span></div>
   <div class="sysrow"><span>Session time</span><b id="sess">00:00</b></div><div class="sysrow"><span>Actions run</span><b id="acts">0</b></div>
   <div class="telemetry-foot" id="telemetry-state">LOCAL TELEMETRY · CONNECTING</div>
  </section>
 </aside>

 <main id="main-stage" aria-label="NEXUS core visualizer">
  <div class="orb-overline"><span class="tiny-live"></span> NEURAL INTERFACE <span class="orb-overline-line"></span><span>LIVE SIGNAL</span></div>
  <div class="holo-deck" aria-label="Holographic command center">
   <section class="holo-panel model-panel" aria-label="Rotating 3D holographic core">
    <div class="holo-panel-head"><span>HOLOGRAPHIC PROJECTION</span><span class="holo-live-state"><i class="tiny-live"></i> LIVE RENDER</span></div>
    <div class="model-viewport" data-model="core" data-voice-state="off"><canvas id="model-canvas" aria-label="Animated three-dimensional holographic quantum reactor core"></canvas><div class="model-reticle" aria-hidden="true"></div><div class="model-readout"><span id="model-label">QUANTUM CORE</span><span id="model-coords">X 00 · Y 00 · Z 00</span></div></div>
    <div id="core-title"><strong>NEXUS</strong><div id="core-state" data-state="off"><i class="led" id="core-led"></i><span id="core-status">STANDING BY</span></div></div>
    <div class="model-selectors" role="group" aria-label="Select holographic model">
     <button type="button" class="model-select active" data-model="core" aria-pressed="true">CORE</button>
     <button type="button" class="model-select" data-model="field" aria-pressed="false">FIELD</button>
     <button type="button" class="model-select" data-model="shield" aria-pressed="false">SHIELD</button>
    </div>
   </section>
   <section class="holo-panel system-panel" aria-label="Interactive system diagram">
    <div class="holo-panel-head"><span>SYSTEM TOPOLOGY</span><span id="topology-state">5 NODES</span></div>
    <div class="system-map" id="system-map">
     <svg class="system-lines" viewBox="0 0 210 104" preserveAspectRatio="none" aria-hidden="true"><path d="M31 14 L89 14 L170 52 M31 39 L89 39 L170 52 M31 65 L89 65 L170 52 M31 91 L89 91 L170 52"/><circle cx="31" cy="14" r="2"/><circle cx="31" cy="39" r="2"/><circle cx="31" cy="65" r="2"/><circle cx="31" cy="91" r="2"/><circle cx="170" cy="52" r="4"/></svg>
     <button class="system-node node-voice" type="button" data-node="voice" aria-pressed="false"><i></i>VOICE</button>
     <button class="system-node node-vision" type="button" data-node="vision" aria-pressed="false"><i></i>VISION</button>
     <button class="system-node node-core selected" type="button" data-node="core" aria-pressed="true"><i></i>CORE</button>
     <button class="system-node node-tools" type="button" data-node="automation" aria-pressed="false"><i></i>TOOLS</button>
     <button class="system-node node-telemetry" type="button" data-node="telemetry" aria-pressed="false"><i></i>SYS I/O</button>
    </div>
    <div class="system-detail"><div><i class="tiny-live"></i><b id="system-detail-title">NEURAL CORE</b><span id="system-detail-state">ONLINE</span></div><p id="system-detail-copy">Main interface loop and state coordinator.</p></div>
   </section>
   <section class="holo-panel tasks-panel" aria-label="Animated active task monitor">
    <div class="holo-panel-head"><span>VISUAL HUB</span><span id="task-active-count">01 ACTIVE</span></div>
    <div class="hub-graphic" aria-hidden="true">
     <span class="hub-plane hub-plane-base"></span>
     <span class="hub-plane hub-plane-mid"></span>
     <span class="hub-plane hub-plane-top"></span>
     <span class="hub-bar hub-bar-1"></span><span class="hub-bar hub-bar-2"></span><span class="hub-bar hub-bar-3"></span><span class="hub-bar hub-bar-4"></span>
     <span class="hub-scan"></span>
    </div>
        <div id="caption" aria-live="polite">Core interface ready. Start the voice link or type a message below.</div>
        <div class="task-list" id="task-list">
     <div class="task-row" data-task="core"><span class="task-indicator"></span><div class="task-copy"><b>CORE LOOP</b><small>Interface state</small></div><span class="task-status">READY</span><span class="task-bar"><i></i></span></div>
     <div class="task-row" data-task="voice"><span class="task-indicator"></span><div class="task-copy"><b>VOICE LINK</b><small>Gemini Live</small></div><span class="task-status">STANDBY</span><span class="task-bar"><i></i></span></div>
     <div class="task-row" data-task="vision"><span class="task-indicator"></span><div class="task-copy"><b>SCREEN VISION</b><small>Consent-based capture</small></div><span class="task-status">OFF</span><span class="task-bar"><i></i></span></div>
     <div class="task-row" data-task="automation"><span class="task-indicator"></span><div class="task-copy"><b>DESKTOP TOOLS</b><small>Allowlisted actions</small></div><span class="task-status">READY</span><span class="task-bar"><i></i></span></div>
     <div class="task-row" data-task="telemetry"><span class="task-indicator"></span><div class="task-copy"><b>SYSTEM TELEMETRY</b><small>CPU · RAM · Network</small></div><span class="task-status">SYNC</span><span class="task-bar"><i></i></span></div>
    </div>
    <div class="telemetry-mini" aria-label="Live CPU memory and network readings">
     <div><span>CPU</span><b id="cmd-cpu">--%</b></div><div><span>RAM</span><b id="cmd-ram">--%</b></div><div><span>NET ↓</span><b id="cmd-net-down">-- KB/s</b></div><div><span>NET ↑</span><b id="cmd-net-up">-- KB/s</b></div>
    </div>
    <div class="task-footer"><span class="tiny-live"></span><span id="task-footer-text">MONITORING LOCAL SYSTEM</span><span id="task-frame">FRAME 0001</span></div>
   </section>
  </div>
  
 </main>

 <aside class="chat-panel" id="chat-panel" aria-label="Nexus chat panel">
  <div class="chat-header">
   <div class="chat-heading"><span class="chat-title-mark">◈</span><div><strong>NEXUS CHAT</strong><small>PERSONAL INTELLIGENCE</small></div></div>
   <button class="new-chat-btn" id="new-chat" type="button">＋ New</button>
  </div>
  <div class="chat-tabs" role="tablist" aria-label="Conversation views">
   <button id="tab-chat" class="chat-tab active" type="button" role="tab" aria-selected="true">CHAT</button>
   <button id="tab-history" class="chat-tab" type="button" role="tab" aria-selected="false">HISTORY</button>
   <span id="feed-count">0 EVENTS</span>
  </div>
  <div class="chat-feed-wrap">
   <div class="feed" id="log-feed" aria-live="polite" aria-label="Current conversation"></div>
   <div id="history-view" class="history-view" hidden></div>
  </div>
  <div class="chat-automation"><span class="automation-spark">✳</span><div class="automation-copy"><strong id="automation-state">SYSTEM EVENTS · STANDING BY</strong><small id="automation-last">Awaiting command from operator</small></div></div>
  <form class="side-composer" id="chat-form">
   <input id="chat-input" type="text" maxlength="2000" autocomplete="off" placeholder="Message Nexus…" aria-label="Message Nexus">
   <button id="chat-send" type="submit" aria-label="Send message">↑</button>
  </form>
  <div class="chat-footnote"><span class="tiny-live"></span> PRIVATE LOCAL INTERFACE <span>·</span> <span id="session-label">VOICE LINK READY</span></div>
 </aside>

 <div class="bottom-composer-wrap">
  <div class="composer-context"><span class="context-dot"></span><span id="composer-placeholder">NEXUS IS STANDING BY</span><span class="composer-context-right">NEXUS / 01</span></div>
  <form class="bottom-composer" id="command-form">
   <span class="composer-leading" aria-hidden="true">⌘</span>
   <input id="command-input" type="text" maxlength="2000" autocomplete="off" placeholder="Ask Nexus anything…" aria-label="Ask Nexus anything">
   <div class="composer-actions">
    <button id="mute" class="composer-icon" type="button" disabled title="Mute microphone" aria-label="Mute microphone">◉</button>
    <button id="disconnect" class="composer-icon stop-icon" type="button" disabled title="Disconnect voice" aria-label="Disconnect voice">■</button>
    <button id="start-inline" class="voice-start" type="button">START VOICE</button>
    <button id="command-send" class="send-button" type="submit" aria-label="Send message" title="Send message">↑</button>
   </div>
  </form>
  <div class="composer-hint"><span>ENTER TO SEND</span><span>YOUR AI · YOUR MACHINE</span><span>MIC INPUT REQUIRES PERMISSION</span></div>
 </div>

 <div id="start" style="display:none"><div class="bigmark"><svg viewBox="0 0 80 80" fill="none" stroke="currentColor"><path d="M22 59V21L58 59V21" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/><circle cx="40" cy="40" r="35" stroke-opacity=".4"/><circle cx="40" cy="40" r="28" stroke-opacity=".25" stroke-dasharray="2 5"/></svg></div><h1>NEXUS</h1><p>Initialise the voice link to enable live conversation.</p><button id="go" type="button">INITIALISE NEXUS</button><div class="sub">LOCAL INTERFACE // GEMINI LIVE LINK</div><p id="err" role="status"></p></div>

 <div id="settings-scrim"></div>
 <aside id="settings" aria-label="NEXUS settings" aria-hidden="true">
  <div class="settings-head"><div><h2>SYSTEM SETTINGS</h2><p>CONFIGURE YOUR NEXUS INTERFACE</p></div><button class="iconbtn" id="settings-close" aria-label="Close settings">×</button></div>
  <div class="setting"><div class="label">Interface accent <span class="muted">VISUAL</span></div><div class="swatches"><button class="swatch active" data-theme="cyan" style="background:#55e8ff" aria-label="Cyan accent"></button><button class="swatch" data-theme="blue" style="background:#64a4ff" aria-label="Blue accent"></button><button class="swatch" data-theme="amber" style="background:#ffbd70" aria-label="Amber accent"></button><button class="swatch" data-theme="violet" style="background:#bb9cff" aria-label="Violet accent"></button><button class="swatch" data-theme="green" style="background:#64ffd0" aria-label="Green accent"></button></div><small>Changes the core, waveform, borders and interface glow.</small></div>
  <div class="setting"><label for="intensity">Core animation intensity <b id="intensity-value">75%</b></label><input id="intensity" type="range" min="20" max="100" value="75"><small>Lower values create a calmer, less active display.</small></div>
  <div class="setting"><label for="voice">Assistant voice</label><select id="voice"><option>Charon</option><option>Orus</option><option>Fenrir</option><option>Puck</option><option>Kore</option><option>Aoede</option></select><small>British English accent is enabled by default. Voice changes apply to the next connection.</small></div><div class="setting"><label>Accent profile</label><div class="accent-profile-value" role="status"><span class="accent-live-dot"></span> BRITISH ENGLISH · REFINED SOUTHERN UK</div><small>Measured, articulate delivery with British phrasing and spelling.</small></div>
  <div class="setting" id="gemini-key-setting"><div class="label">GEMINI API KEY <span class="muted">PRIVATE · LOCAL</span></div><div id="gemini-key-status-settings" class="gemini-key-status" role="status">Checking key configuration…</div><div class="gemini-key-field gemini-key-settings-field"><input id="gemini-key-input-settings" type="password" placeholder="Paste Gemini API key" autocomplete="new-password" spellcheck="false" aria-label="Gemini API key"><button id="gemini-key-toggle-settings" type="button" aria-label="Show or hide API key">SHOW</button></div><div class="settings-actions" style="margin-top:10px"><button id="gemini-key-save-settings" type="button">SAVE API KEY</button><button id="gemini-key-forget-settings" type="button">REMOVE LOCAL KEY</button></div><small>New users can paste their key here—no code edits required. The key is saved in your private local NEXUS config, not in the project or browser storage. Get a key from <a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener noreferrer" style="color:#9bf5ff">Google AI Studio</a>.</small></div>
  <div class="setting" id="user-name-setting"><label for="user-name-settings">What NEXUS calls you</label><input id="user-name-settings" type="text" maxlength="40" placeholder="Leave blank for sir / boss" autocomplete="off" spellcheck="false"><div class="settings-actions" style="margin-top:10px"><button id="user-name-save-settings" type="button">SAVE NAME</button></div><small>Applies the next time you start the voice link. Saved privately on this computer.</small></div>
  <div class="setting"><label for="weather-input">Weather location</label><input id="weather-input" type="text" value="Lahore" maxlength="60" autocomplete="off"><small>Uses live Open-Meteo weather data; an internet connection is required.</small></div>
  <div class="setting"><label for="transcripts">Show conversation transcript <input id="transcripts" type="checkbox" checked></label><small>Hide spoken transcripts while retaining system events.</small></div>
  <div class="setting"><label for="motion">Reduce motion <input id="motion" type="checkbox"></label><small>Reduces nonessential movement and animation.</small></div>
  <div class="setting"><label for="low-power">Low-power mode <input id="low-power" type="checkbox"></label><small>Reduces orb detail, canvas resolution, frame rate, and visual effects to help older PCs.</small></div>
  <div class="setting"><label for="greeting">Speak greeting when connected <input id="greeting" type="checkbox" checked></label><small>Announce when the voice session connects.</small></div>
  <div class="setting"><label for="screen-vision">Enable Screen Vision <input id="screen-vision" type="checkbox"></label><small>Lets Gemini inspect a screen or window you explicitly choose. The browser asks for sharing permission; frames are resized and sent at up to 1 frame per second while the live voice link is connected. Off by default.</small><small id="screen-vision-status" class="screen-vision-status" role="status">OFF · SCREEN VISION DISABLED</small></div>
  <div class="setting"><div class="label">DESKTOP AUTOMATION <span class="muted">PERMISSIONS</span></div><small>These controls are enforced by the local Python tools, not just visual preferences. Changes are saved on this computer.</small></div>
  <div class="setting"><label for="automation-enabled">Enable desktop automation <input id="automation-enabled" type="checkbox" checked></label><small>Master switch for computer-control actions.</small></div>
  <div class="setting"><label for="allow-app-control">Apps, typing & keyboard shortcuts <input id="allow-app-control" type="checkbox" checked></label><small>Allows approved apps to open and the assistant to type or use approved shortcuts.</small></div>
  <div class="setting"><label for="allow-file-operations">File & folder access <input id="allow-file-operations" type="checkbox" checked></label><small>Master permission for reading, searching, opening, creating, moving, renaming, and organizing non-hidden items in your home folder (including Documents and Downloads). Turn this off to block all NEXUS file tools.</small></div>
  <div class="setting"><label for="allow-file-deletion">Move files/folders to Recycle Bin <input id="allow-file-deletion" type="checkbox"></label><small>When enabled, NEXUS can move requested files/folders to the OS Recycle Bin/Trash immediately—no one-by-one approval prompts. This is recoverable where supported.</small></div>
  <div class="setting"><label for="allow-permanent-file-deletion">Allow permanent file/folder deletion <input id="allow-permanent-file-deletion" type="checkbox"></label><small>Off by default. Permanent deletion of a specific item always waits for review here and requires typing DELETE. Prefer Recycle Bin for ordinary deletions.</small></div>
  <div class="setting"><label for="allow-recycle-bin-cleanup">Allow Recycle Bin cleanup <input id="allow-recycle-bin-cleanup" type="checkbox"></label><small>Allows a request to permanently empty the system Recycle Bin/Trash. You must approve it here and type EMPTY before anything is removed. Off by default.</small></div>
  <div class="setting" id="pending-file-actions-setting"><div class="label">PERMANENT ACTIONS <span class="muted">REVIEW REQUIRED</span></div><small>Only irreversible actions appear here. Recycle Bin moves and normal file organization don't require per-item approval.</small><div id="pending-file-actions" aria-live="polite"><span class="muted">No pending permanent actions.</span></div></div>
  <div class="setting"><label for="allow-media-control">System volume & media <input id="allow-media-control" type="checkbox" checked></label><small>Allows volume up/down/mute and media play/pause, track navigation, rewind/forward.</small></div>
  <div class="setting"><label for="allow-web-actions">Open websites & search <input id="allow-web-actions" type="checkbox" checked></label><small>Controls website opening, browser searches, and web research.</small></div>
  <div class="settings-actions"><button id="settings-cancel" type="button">CLOSE</button><button id="settings-apply" type="button">APPLY SETTINGS</button></div>
 </aside>

 <div id="gemini-key-scrim" aria-hidden="true">
  <section class="gemini-key-dialog" role="dialog" aria-modal="true" aria-labelledby="gemini-key-title">
   <div class="gemini-key-kicker">NEXUS // FIRST-RUN SETUP</div>
   <h2 id="gemini-key-title">Connect your Gemini API key</h2>
   <p>NEXUS needs your own Google Gemini API key for live voice and AI features. Paste it below once; NEXUS saves it in a private file in your user profile—not in this Python file, GitHub, or browser storage.</p>
   <p><a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener noreferrer">Get a Gemini API key from Google AI Studio ↗</a></p>
   <div class="gemini-key-field"><input id="gemini-key-input-first-run" type="password" placeholder="Paste your Gemini API key" autocomplete="new-password" spellcheck="false" aria-label="Gemini API key"><button id="gemini-key-toggle-first-run" type="button">SHOW</button></div>
   <p style="margin:16px 0 8px">What should NEXUS call you? (optional)</p>
   <div class="gemini-key-field"><input id="user-name-first-run" type="text" maxlength="40" placeholder="Your name — or leave blank for sir / boss" autocomplete="given-name" spellcheck="false" aria-label="What should NEXUS call you"></div>
   <div id="gemini-key-feedback" role="status" aria-live="polite"></div>
   <div class="gemini-key-actions"><button id="gemini-key-skip" type="button">CONTINUE IN PREVIEW MODE</button><button id="gemini-key-save-first-run" class="primary" type="button">SAVE KEY & CONTINUE</button></div>
  </section>
 </div>
 <div id="palette-scrim" class="palette-scrim" aria-hidden="true">
  <section id="palette-modal" class="palette-modal" role="dialog" aria-modal="true" aria-label="Command palette">
   <div class="palette-search-row"><span>⌕</span><input id="palette-input" type="text" placeholder="Search commands…" autocomplete="off"><kbd>ESC</kbd></div>
   <div class="palette-label">QUICK COMMANDS</div>
   <div id="palette-options" class="palette-options">
    <button class="command-option" type="button" data-command="dashboard"><span>◈</span><span><strong>Open Dashboard</strong><small>Return to the core visualizer</small></span><kbd>↵</kbd></button>
    <button class="command-option" type="button" data-command="chat"><span>▤</span><span><strong>Open Nexus Chat</strong><small>Jump to the conversation panel</small></span><kbd>↵</kbd></button>
    <button class="command-option" type="button" data-command="settings"><span>⚙</span><span><strong>System Settings</strong><small>Change voice, theme and weather location</small></span><kbd>↵</kbd></button>
    <button class="command-option" type="button" data-command="weather"><span>☼</span><span><strong>Refresh Lahore Weather</strong><small>Request the latest available conditions</small></span><kbd>↵</kbd></button>
    <button class="command-option" type="button" data-command="voice"><span>♫</span><span><strong>Start / Disconnect Voice</strong><small>Toggle the live Gemini voice session</small></span><kbd>↵</kbd></button>
   </div>
   <div class="palette-bottom"><span>↑ ↓ TO NAVIGATE</span><span>ENTER TO SELECT</span><span>ESC TO CLOSE</span></div>
  </section>
 </div>
 <div class="toast" id="toast" role="status"></div>
</div>
<script>
'use strict';
// Gemini Live output audio is signed 16-bit PCM at 24 kHz. This constant is
// JavaScript-side: Python's OUT_RATE is not automatically visible in the browser.
const OUT_RATE = 24000;
const $ = (s) => document.querySelector(s);
const canvas = $('#hud-canvas'), ctx = canvas.getContext('2d');
const THEMES = {cyan:['#55e8ff','85,232,255'],blue:['#64a4ff','100,164,255'],amber:['#ffbd70','255,189,112'],violet:['#bb9cff','187,156,255'],green:['#64ffd0','100,255,208']};
const STATES = {off:'OFFLINE',idle:'STANDING BY',listening:'LISTENING',thinking:'PROCESSING',speaking:'SPEAKING',error:'LINK ERROR'};
let settingsRevision=0;
let settings = {theme:'cyan',intensity:75,voice:'Charon',city:'Lahore',transcripts:true,reduceMotion:false,lowPower:false,greeting:true,automation_enabled:true,allow_app_control:true,allow_file_operations:true,allow_file_deletion:false,allow_permanent_file_deletion:false,allow_recycle_bin_cleanup:false,allow_media_control:true,allow_web_actions:true,screen_vision_enabled:false};
try { const saved = JSON.parse(localStorage.getItem('nexus-settings-v2') || '{}'); settings = {...settings,...saved}; } catch (_) {}
const CHAT_HISTORY_KEY='nexus-history-v1', CHAT_DRAFT_KEY='nexus-current-v1';
const keyScrim=$('#gemini-key-scrim');
function showGeminiKeySetup(){if(!keyScrim)return;keyScrim.classList.add('open');keyScrim.setAttribute('aria-hidden','false');setTimeout(()=>$('#gemini-key-input-first-run')?.focus(),80)}
function hideGeminiKeySetup(){if(!keyScrim)return;keyScrim.classList.remove('open');keyScrim.setAttribute('aria-hidden','true')}
function setKeyFeedback(message,isError=false){const node=$('#gemini-key-feedback');if(node){node.textContent=message;node.classList.toggle('error',!!isError)}}
async function refreshGeminiKeyStatus(){
 try{const r=await fetch('/api/key',{cache:'no-store'});const d=await r.json();const text=d.configured?('Saved at: '+(d.path||'local config file')):'Not configured';
  const settingsNode=$('#gemini-key-status-settings');if(settingsNode)settingsNode.textContent=text+(d.sdk_available?'':' · Google Gen AI SDK missing');
  if(!d.configured)showGeminiKeySetup();else hideGeminiKeySetup();
 }catch(_){const n=$('#gemini-key-status-settings');if(n)n.textContent='Could not check local key status';}
}
async function saveGeminiKeyFrom(inputId,buttonId,firstRun=false){
 const input=$('#'+inputId),button=$('#'+buttonId);const key=(input?.value||'').trim();
 if(!key){if(firstRun)setKeyFeedback('Paste your Gemini API key to continue.',true);else notify('Paste a Gemini API key first.');input?.focus();return}
 if(/[\s]/.test(key)||key.length<12||key.length>512){if(firstRun)setKeyFeedback('That key format looks invalid. Paste the complete key without spaces.',true);else notify('Check the API key: use the full key without spaces.');return}
 if(button){button.disabled=true;button.dataset.originalText=button.textContent;button.textContent='SAVING…'}
 if(firstRun)setKeyFeedback('Saving to your local NEXUS configuration…');
 try{
  if(firstRun){const nameInput=$('#user-name-first-run');await saveProfileName(nameInput?nameInput.value:'')}
  const r=await fetch('/api/key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:key})});
  let d={};try{d=await r.json()}catch(_){}
  if(!r.ok||!d.ok)throw new Error(d.error||'Could not save the API key.');
  if(input)input.value='';if(firstRun)hideGeminiKeySetup();
  await refreshGeminiKeyStatus();
  const message=d.sdk_available?'Gemini API key saved locally. Start/restart the voice link to use it.':'Key saved locally. Install google-genai to enable live voice.';
  if(firstRun){hideGeminiKeySetup();notify(message)}else notify(message);
  if(!firstRun)$('#gemini-key-status-settings').textContent=d.sdk_available?'Saved on this computer · reconnect voice to apply':'Saved locally · Google Gen AI SDK missing';
 }catch(e){const msg=e&&e.message?e.message:'Could not save the API key.';if(firstRun)setKeyFeedback(msg,true);else notify(msg)}
 finally{if(button){button.disabled=false;button.textContent=button.dataset.originalText|| (firstRun?'SAVE KEY & CONTINUE':'SAVE API KEY')}}
}
async function loadProfile(){try{const r=await fetch('/api/profile',{cache:'no-store'});const d=await r.json();const v=d.user_name||'';['#user-name-first-run','#user-name-settings'].forEach(sel=>{const n=$(sel);if(n&&!n.value)n.value=v})}catch(_){}}
async function saveProfileName(name){const r=await fetch('/api/profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_name:name})});let d={};try{d=await r.json()}catch(_){}if(!r.ok||!d.ok)throw new Error(d.error||'Could not save your name.');return d}
function bindKeyVisibility(inputId,buttonId){const input=$('#'+inputId),button=$('#'+buttonId);if(!input||!button)return;button.addEventListener('click',()=>{const showing=input.type==='password';input.type=showing?'text':'password';button.textContent=showing?'HIDE':'SHOW';})}
$('#gemini-key-save-first-run').addEventListener('click',()=>saveGeminiKeyFrom('gemini-key-input-first-run','gemini-key-save-first-run',true));
$('#gemini-key-save-settings').addEventListener('click',()=>saveGeminiKeyFrom('gemini-key-input-settings','gemini-key-save-settings',false));
$('#gemini-key-skip').addEventListener('click',hideGeminiKeySetup);
bindKeyVisibility('gemini-key-input-first-run','gemini-key-toggle-first-run');bindKeyVisibility('gemini-key-input-settings','gemini-key-toggle-settings');
$('#gemini-key-forget-settings').addEventListener('click',async()=>{if(!window.confirm('Remove the saved Gemini key? You will need to paste it again in Settings to use voice.'))return;try{const r=await fetch('/api/key',{method:'DELETE'});const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||'Could not remove key.');await refreshGeminiKeyStatus();if(!d.configured)showGeminiKeySetup();notify('Local Gemini API key removed.')}catch(e){notify(e.message||'Could not remove local key.')}});
$('#user-name-save-settings').addEventListener('click',async()=>{try{const d=await saveProfileName($('#user-name-settings').value);notify(d.user_name?('Got it. NEXUS will call you '+d.user_name+'. Reconnect voice to apply.'):'Name cleared. NEXUS will say sir or boss. Reconnect voice to apply.')}catch(e){notify(e.message||'Could not save your name.')}});
void refreshGeminiKeyStatus();void loadProfile();
// Migrate conversation history from older builds without exposing the old branding in the UI.
try {
 const oldHistoryKey='brahma-history-v1', oldDraftKey='brahma-current-v1';
 if(!localStorage.getItem(CHAT_HISTORY_KEY)&&localStorage.getItem(oldHistoryKey))localStorage.setItem(CHAT_HISTORY_KEY,localStorage.getItem(oldHistoryKey));
 if(!localStorage.getItem(CHAT_DRAFT_KEY)&&localStorage.getItem(oldDraftKey))localStorage.setItem(CHAT_DRAFT_KEY,localStorage.getItem(oldDraftKey));
 localStorage.removeItem(oldHistoryKey);localStorage.removeItem(oldDraftKey);
} catch (_) {}
let chatArchives=[]; let sessionDraft=[]; let activeChatView='chat'; let ignoreTypedTranscript='';
try{chatArchives=JSON.parse(localStorage.getItem(CHAT_HISTORY_KEY)||'[]');if(!Array.isArray(chatArchives))chatArchives=[];}catch(_){chatArchives=[];}
try{const prior=JSON.parse(localStorage.getItem(CHAT_DRAFT_KEY)||'[]');if(Array.isArray(prior)&&prior.length){chatArchives.unshift({time:new Date().toISOString(),items:prior});chatArchives=chatArchives.slice(0,20);localStorage.setItem(CHAT_HISTORY_KEY,JSON.stringify(chatArchives));localStorage.removeItem(CHAT_DRAFT_KEY);}}catch(_){}
let ws=null, capCtx=null, playCtx=null, micAnalyser=null, outAnalyser=null, stream=null, micNode=null;
let screenStream=null, screenVideo=null, screenCanvas=null, screenVisionTimer=null, screenCaptureBusy=false;
let muted=false, live=false, failed=false, toolBusy=false, sessionStart=0, actionCount=0, turns=0, cur={you:null,nexus:null}, feedCount=0;
let sources=[], nextAudioTime=0, playbackQueue=[], playbackDraining=false, playbackEpoch=0, pendingPcmByte=null;
let audioState='off', lastMicLoud=0, micLevel=0, outLevel=0, spin=0, particles=[];
let W=innerWidth,H=innerHeight,D=1, audioReady=false, toastTimer=null, weatherTimer=null, weatherAbort=null;
let accentRGB='138,247,255';
const reducePreference = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
function notify(message){const node=$('#toast');node.textContent=message;node.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>node.classList.remove('show'),3000)}
function persistChatDraft(){try{sessionDraft=Array.from($('#log-feed').querySelectorAll('.logline')).map(p=>({cls:Array.from(p.classList).find(c=>['you','nexus','sys','tool'].includes(c))||'sys',text:p.textContent,at:p.dataset.at||new Date().toISOString(),transcript:p.dataset.transcript==='1'}));localStorage.setItem(CHAT_DRAFT_KEY,JSON.stringify(sessionDraft));}catch(_){}}
function entry(cls,text,transcript=false){const p=document.createElement('p');p.className='logline '+cls+(transcript&&!settings.transcripts?' hide':'');p.textContent=text;p.dataset.at=new Date().toISOString();p.dataset.transcript=transcript?'1':'0';$('#log-feed').appendChild(p);$('#log-feed').scrollTop=$('#log-feed').scrollHeight;feedCount++;$('#feed-count').textContent=feedCount+' EVENTS';while($('#log-feed').children.length>160){$('#log-feed').firstElementChild.remove()}persistChatDraft();return p}
function renderHistory(){const root=$('#history-view');root.replaceChildren();if(!chatArchives.length){const empty=document.createElement('div');empty.className='history-empty';empty.textContent='No saved conversations yet. Start a chat and use + New to archive it here.';root.appendChild(empty);return;}chatArchives.forEach((thread,index)=>{const items=Array.isArray(thread.items)?thread.items:[];const firstUser=items.find(x=>x.cls==='you'&&x.text);const firstAssistant=items.find(x=>x.cls==='nexus'&&x.text);const card=document.createElement('button');card.type='button';card.className='history-card';const strong=document.createElement('strong');strong.textContent=(firstUser?firstUser.text:firstAssistant?firstAssistant.text:'Nexus session').slice(0,88);const small=document.createElement('small');let when='Previous session';try{when=new Date(thread.time).toLocaleString([],{dateStyle:'medium',timeStyle:'short'});}catch(_){}small.textContent=when+' · '+items.length+' events'+(firstAssistant?' · '+firstAssistant.text.slice(0,72):'');card.append(strong,small);card.addEventListener('click',()=>showHistoryThread(index));root.appendChild(card);});}
function showHistoryThread(index){const thread=chatArchives[index];if(!thread)return;const root=$('#history-view');root.replaceChildren();const back=document.createElement('button');back.type='button';back.className='history-card';back.textContent='← All history';back.addEventListener('click',renderHistory);root.appendChild(back);(thread.items||[]).forEach(item=>{if(!item.text)return;const line=document.createElement('div');line.className='logline '+(['you','nexus','sys','tool'].includes(item.cls)?item.cls:'sys');line.textContent=item.text;root.appendChild(line);});} 
function archiveCurrentThread(){persistChatDraft();if(sessionDraft.length){chatArchives.unshift({time:new Date().toISOString(),items:sessionDraft.slice()});chatArchives=chatArchives.slice(0,20);try{localStorage.setItem(CHAT_HISTORY_KEY,JSON.stringify(chatArchives));}catch(_){}}sessionDraft=[];try{localStorage.removeItem(CHAT_DRAFT_KEY)}catch(_){}renderHistory();}
function caption(who,text){const n=$('#caption');n.className=who||'';n.textContent=text||''}
function applyTheme(){const pair=THEMES[settings.theme]||THEMES.cyan;accentRGB=pair[1];document.documentElement.style.setProperty('--a',pair[0]);document.documentElement.style.setProperty('--a-rgb',pair[1]);document.querySelectorAll('.swatch').forEach(b=>b.classList.toggle('active',b.dataset.theme===settings.theme));$('#intensity').value=settings.intensity;$('#intensity-value').textContent=settings.intensity+'%';$('#voice').value=settings.voice;$('#weather-input').value=settings.city;$('#transcripts').checked=settings.transcripts;$('#motion').checked=settings.reduceMotion;$('#low-power').checked=!!settings.lowPower;$('#greeting').checked=settings.greeting;$('#screen-vision').checked=!!settings.screen_vision_enabled;$('#automation-enabled').checked=!!settings.automation_enabled;$('#allow-app-control').checked=!!settings.allow_app_control;$('#allow-file-operations').checked=!!settings.allow_file_operations;$('#allow-file-deletion').checked=!!settings.allow_file_deletion;$('#allow-permanent-file-deletion').checked=!!settings.allow_permanent_file_deletion;$('#allow-recycle-bin-cleanup').checked=!!settings.allow_recycle_bin_cleanup;$('#allow-media-control').checked=!!settings.allow_media_control;$('#allow-web-actions').checked=!!settings.allow_web_actions;document.documentElement.classList.toggle('low-power',!!settings.lowPower);document.querySelectorAll('.logline').forEach(p=>p.classList.toggle('hide',p.dataset.transcript==='1'&&!settings.transcripts));updateScreenVisionStatus();resize();}
function openSettings(){$('#settings').classList.add('open');$('#settings-scrim').classList.add('open');$('#settings').setAttribute('aria-hidden','false');void loadPendingFileActions()}
async function loadPendingFileActions(){
 const root=$('#pending-file-actions');if(!root)return;
 try{
  const response=await fetch('/api/file-actions',{cache:'no-store'});if(!response.ok)throw new Error('Pending-action list unavailable');
  const payload=await response.json();const actions=Array.isArray(payload.actions)?payload.actions:[];root.replaceChildren();
  if(!actions.length){const empty=document.createElement('small');empty.className='muted';empty.textContent='No pending permanent actions.';root.appendChild(empty);return;}
  actions.forEach(action=>{
   const card=document.createElement('div');card.className='pending-file-action';
   const title=document.createElement('b');title.textContent=action.kind==='empty_recycle_bin'?'PERMANENTLY EMPTY RECYCLE BIN / TRASH':'PERMANENTLY DELETE FILE / FOLDER';
   const detail=document.createElement('small');detail.textContent=action.display||'Pending permanent action';
   const buttons=document.createElement('div');buttons.className='pending-action-buttons';
   const approve=document.createElement('button');approve.type='button';approve.textContent='REVIEW & APPROVE';
   approve.addEventListener('click',async()=>{
    let confirmation='';
    if(action.kind==='empty_recycle_bin'){
     if(!window.confirm('This permanently removes items from your system Recycle Bin/Trash. This cannot generally be undone. Continue?'))return;
     const phrase=window.prompt('Type EMPTY to confirm permanent cleanup:');if((phrase||'').trim()!=='EMPTY'){notify('Recycle Bin cleanup cancelled.');return;}confirmation='EMPTY';
    }else if(action.kind==='permanent_delete_file'){
     if(!window.confirm('This permanently deletes the exact file/folder shown below. This cannot generally be undone. Continue?'))return;
     const phrase=window.prompt('Type DELETE to confirm permanent deletion:');if((phrase||'').trim()!=='DELETE'){notify('Permanent deletion cancelled.');return;}confirmation='DELETE';
    }else { notify('Unknown pending action type; nothing was changed.'); return; }
    approve.disabled=true;
    try{const result=await fetch('/api/file-actions/'+encodeURIComponent(action.id),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'approve',confirmation})});const data=await result.json();if(!result.ok||!data.ok)throw new Error(data.result||data.error||'Action failed');notify(data.result||'Action completed.');await loadPendingFileActions();}
    catch(err){notify(err.message||'Action failed.');approve.disabled=false;}
   });
   const cancel=document.createElement('button');cancel.type='button';cancel.textContent='CANCEL';cancel.addEventListener('click',async()=>{cancel.disabled=true;try{const res=await fetch('/api/file-actions/'+encodeURIComponent(action.id),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'cancel'})});const data=await res.json();if(!res.ok||!data.ok)throw new Error(data.error||'Could not cancel');await loadPendingFileActions();notify('Pending action cancelled.');}catch(err){notify(err.message||'Could not cancel action');cancel.disabled=false;}});
   buttons.append(approve,cancel);card.append(title,detail,buttons);root.appendChild(card);
  });
 }catch(_){root.textContent='Pending-action service unavailable. Check that the local NEXUS server is running.';}
}
function closeSettings(){ $('#settings').classList.remove('open');$('#settings-scrim').classList.remove('open');$('#settings').setAttribute('aria-hidden','true') }
function persistUiSettings(){try{localStorage.setItem('nexus-settings-v2',JSON.stringify(settings))}catch(_){}}
function updateScreenVisionStatus(message){
 const node=$('#screen-vision-status');
 if(message){node.textContent=message;return;}
 if(!settings.screen_vision_enabled){node.textContent='OFF · SCREEN VISION DISABLED';return;}
 if(!screenStream){node.textContent='ENABLED · APPLY SETTINGS TO CHOOSE A SCREEN';return;}
 node.textContent=live?'SHARING · GEMINI LIVE INPUT ACTIVE':'SHARING · CONNECT VOICE LINK TO SEND FRAMES';
}
function screenVisionPermissionPayload(enabled){return {automation_enabled:!!settings.automation_enabled,allow_app_control:!!settings.allow_app_control,allow_file_operations:!!settings.allow_file_operations,allow_file_deletion:!!settings.allow_file_deletion,allow_permanent_file_deletion:!!settings.allow_permanent_file_deletion,allow_recycle_bin_cleanup:!!settings.allow_recycle_bin_cleanup,allow_media_control:!!settings.allow_media_control,allow_web_actions:!!settings.allow_web_actions,screen_vision_enabled:!!enabled};}
function sendScreenVisionState(enabled){
 if(!ws||ws.readyState!==WebSocket.OPEN)return false;
 try{ws.send(JSON.stringify({type:'screen_vision_state',enabled:!!enabled}));return true;}catch(_){return false;}
}
async function syncScreenVisionPermission(enabled){
 try{
  const response=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(screenVisionPermissionPayload(enabled))});
  const payload=await response.json();
  if(!response.ok||!payload.ok)throw new Error(payload.error||'Screen Vision permission could not be saved');
 }catch(e){notify('Screen Vision state could not sync with NEXUS: '+(e&&e.message?e.message:'server unavailable'));}
}
function stopScreenVision(updateSetting=false,announce=false){
 if(screenVisionTimer!==null){clearInterval(screenVisionTimer);screenVisionTimer=null;}
 const previous=screenStream;screenStream=null;
 if(previous){for(const track of previous.getTracks()){try{track.stop()}catch(_){}}}
 if(screenVideo){try{screenVideo.pause();screenVideo.srcObject=null}catch(_){}screenVideo=null;}
 if(screenCanvas){screenCanvas.width=1;screenCanvas.height=1;screenCanvas=null;}
 screenCaptureBusy=false;
 if(updateSetting){settings.screen_vision_enabled=false;$('#screen-vision').checked=false;persistUiSettings();sendScreenVisionState(false);void syncScreenVisionPermission(false);}
 updateScreenVisionStatus();
 if(announce){notify('Screen sharing stopped. No more frames will be sent.');entry('sys','Screen Vision stopped');}
}
async function sendScreenVisionFrame(){
 if(screenCaptureBusy||!settings.screen_vision_enabled||!screenStream||!live||!ws||ws.readyState!==WebSocket.OPEN||ws.bufferedAmount>500000)return;
 const activeStream=screenStream, video=screenVideo;
 if(!video||video.readyState<2||!video.videoWidth||!video.videoHeight)return;
 screenCaptureBusy=true;
 try{
  const longest=Math.max(video.videoWidth,video.videoHeight),scale=Math.min(1,768/longest);
  const width=Math.max(1,Math.round(video.videoWidth*scale)),height=Math.max(1,Math.round(video.videoHeight*scale));
  if(!screenCanvas)screenCanvas=document.createElement('canvas');
  screenCanvas.width=width;screenCanvas.height=height;
  const context=screenCanvas.getContext('2d',{alpha:false});if(!context)throw new Error('Canvas capture is unavailable');
  context.drawImage(video,0,0,width,height);
  const blob=await new Promise(resolve=>screenCanvas.toBlob(resolve,'image/jpeg',0.68));
  if(!blob||blob.size>500000||!settings.screen_vision_enabled||activeStream!==screenStream||!live||!ws||ws.readyState!==WebSocket.OPEN)return;
  const bytes=new Uint8Array(await blob.arrayBuffer());let binary='';
  for(let offset=0;offset<bytes.length;offset+=0x8000)binary+=String.fromCharCode(...bytes.subarray(offset,Math.min(offset+0x8000,bytes.length)));
  const packet=JSON.stringify({type:'screen_frame',data:btoa(binary)});
  if(packet.length<=700000&&ws.readyState===WebSocket.OPEN&&ws.bufferedAmount<500000)ws.send(packet);
 }catch(e){entry('sys','Screen Vision frame skipped: '+(e&&e.message?e.message:String(e)));}
 finally{screenCaptureBusy=false;}
}
async function startScreenVision(){
 if(screenStream||!settings.screen_vision_enabled)return;
 if(!navigator.mediaDevices||typeof navigator.mediaDevices.getDisplayMedia!=='function'){
  settings.screen_vision_enabled=false;$('#screen-vision').checked=false;persistUiSettings();updateScreenVisionStatus('UNAVAILABLE · USE A SUPPORTED BROWSER');void syncScreenVisionPermission(false);notify('Screen Vision requires a browser that supports screen sharing on localhost.');return;
 }
 updateScreenVisionStatus('WAITING FOR SCREEN SELECTION…');
 let chosenStream=null;
 try{
  // Invoke getDisplayMedia immediately from the Apply Settings click gesture.
  chosenStream=await navigator.mediaDevices.getDisplayMedia({video:{frameRate:{ideal:1,max:1}},audio:false});
  if(!settings.screen_vision_enabled){chosenStream.getTracks().forEach(track=>track.stop());updateScreenVisionStatus();return;}
  screenStream=chosenStream;screenVideo=document.createElement('video');screenVideo.muted=true;screenVideo.playsInline=true;screenVideo.srcObject=screenStream;
  await screenVideo.play();
  const videoTrack=screenStream.getVideoTracks()[0];
  if(videoTrack)videoTrack.addEventListener('ended',()=>{if(screenStream===chosenStream)stopScreenVision(true,true)},{once:true});
  sendScreenVisionState(true);
  screenVisionTimer=setInterval(()=>{void sendScreenVisionFrame()},1000);
  updateScreenVisionStatus();entry('sys','Screen Vision enabled · user-selected display capture');notify('Screen Vision enabled · selected screen frames are sent only while the voice link is connected');
 }catch(e){
  if(chosenStream){for(const track of chosenStream.getTracks()){try{track.stop()}catch(_){}}}
  if(screenStream===chosenStream)stopScreenVision(false,false);
  settings.screen_vision_enabled=false;$('#screen-vision').checked=false;persistUiSettings();sendScreenVisionState(false);updateScreenVisionStatus('OFF · SCREEN SHARING NOT STARTED');void syncScreenVisionPermission(false);
  const reason=e&&e.name==='NotAllowedError'?'sharing permission was cancelled or denied':(e&&e.message?e.message:'screen capture failed');
  notify('Screen Vision not enabled: '+reason+'.');
 }
}

async function saveSettings(){
 settingsRevision++;
 const previousVoice=settings.voice;
 const previousScreenVisionEnabled=!!settings.screen_vision_enabled;
 settings.theme=document.querySelector('.swatch.active')?.dataset.theme||settings.theme;
 settings.intensity=Number($('#intensity').value)||75;settings.voice=$('#voice').value;
 const city=$('#weather-input').value.trim();settings.city=city||'Lahore';settings.transcripts=$('#transcripts').checked;settings.reduceMotion=$('#motion').checked;settings.lowPower=$('#low-power').checked;settings.greeting=$('#greeting').checked;
 settings.screen_vision_enabled=$('#screen-vision').checked;
 settings.automation_enabled=$('#automation-enabled').checked;settings.allow_app_control=$('#allow-app-control').checked;settings.allow_file_operations=$('#allow-file-operations').checked;settings.allow_file_deletion=$('#allow-file-deletion').checked;settings.allow_permanent_file_deletion=$('#allow-permanent-file-deletion').checked;settings.allow_recycle_bin_cleanup=$('#allow-recycle-bin-cleanup').checked;settings.allow_media_control=$('#allow-media-control').checked;settings.allow_web_actions=$('#allow-web-actions').checked;
 persistUiSettings();applyTheme();
 // Revoke vision before awaiting network operations; the server receives an ordered state event.
 if(previousScreenVisionEnabled&&!settings.screen_vision_enabled){
  sendScreenVisionState(false);
  stopScreenVision(false,false);
  updateScreenVisionStatus();
  entry('sys','Screen Vision disabled · previous frames are no longer valid context');
  notify("Screen Vision disabled — I no longer have access to your screen.");
 }
 // Screen capture must be requested directly by this click, before any awaited network call.
 if(settings.screen_vision_enabled){if(!screenStream)void startScreenVision();}
 else if(screenStream)stopScreenVision(false,false);
 closeSettings();loadWeather(true);
 try{
  const response=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(screenVisionPermissionPayload(settings.screen_vision_enabled))});
  const payload=await response.json();if(!response.ok||!payload.ok)throw new Error(payload.error||'Settings could not be saved');
  Object.assign(settings,payload.settings||{});persistUiSettings();applyTheme();
  notify(live&&previousVoice!==settings.voice?'Settings applied · voice change takes effect on next connection':'Settings applied');
 }catch(e){notify('UI settings saved, but server permissions did not sync: '+(e&&e.message?e.message:'server unavailable'));}
}
async function loadAutomationSettings(){
 const requestRevision=settingsRevision;
 try{const response=await fetch('/api/settings',{cache:'no-store'});if(!response.ok||requestRevision!==settingsRevision)return;const payload=await response.json();if(payload&&payload.settings&&requestRevision===settingsRevision){Object.assign(settings,payload.settings);try{localStorage.setItem('nexus-settings-v2',JSON.stringify(settings))}catch(_){};applyTheme();}}
 catch(_){/* The GUI can still be previewed while the local server is restarting. */}
}
$('#settings-open').addEventListener('click',openSettings);$('#settings-close').addEventListener('click',closeSettings);$('#settings-cancel').addEventListener('click',closeSettings);$('#settings-scrim').addEventListener('click',closeSettings);$('#settings-apply').addEventListener('click',saveSettings);
$('#screen-vision').addEventListener('change',()=>{
 if($('#screen-vision').checked)return;
 if(!settings.screen_vision_enabled&&!screenStream)return;
 settings.screen_vision_enabled=false;persistUiSettings();
 sendScreenVisionState(false);
 stopScreenVision(false,false);
 updateScreenVisionStatus();
 void syncScreenVisionPermission(false);
 entry('sys','Screen Vision disabled immediately · model notified that prior frames are stale');
 notify("Screen Vision disabled — I no longer have access to your screen.");
});
document.querySelectorAll('.swatch').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('.swatch').forEach(x=>x.classList.remove('active'));b.classList.add('active');settings.theme=b.dataset.theme;applyTheme()}));
$('#settings').querySelectorAll('input,select').forEach(control=>control.addEventListener('input',()=>{settingsRevision++;}));
$('#intensity').addEventListener('input',e=>$('#intensity-value').textContent=e.target.value+'%');
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeSettings()});

function setState(state){
 audioState=state;
 const label=STATES[state]||STATES.off;
 $('#status').textContent=label;$('#core-status').textContent=label;
 $('#status-led').classList.toggle('amber',state==='thinking');
 $('#top-led').classList.toggle('amber',state==='thinking');
 $('#core-led').classList.toggle('amber',state==='thinking');
 $('#sys-label').textContent=label;$('#session-label').textContent=live?'LIVE VOICE SESSION':'VOICE LINK READY';
 const coreState=$('#core-state');if(coreState)coreState.dataset.state=state;
 const viewport=$('.model-viewport');if(viewport)viewport.dataset.voiceState=state;
 const cp=$('#composer-placeholder');if(cp)cp.textContent=state==='listening'?'NEXUS IS LISTENING':state==='speaking'?'NEXUS IS SPEAKING':state==='thinking'?'NEXUS IS THINKING':state==='error'?'VOICE LINK NEEDS ATTENTION':state==='off'?'VOICE LINK OFFLINE':'NEXUS IS STANDING BY';
}
function syncButtons(){ $('#mute').disabled=!live;$('#disconnect').disabled=!live;$('#start-inline').disabled=live;$('#mute').textContent=muted?'◉':'◉';$('#mute').title=muted?'Unmute microphone':'Mute microphone';$('#mute').classList.toggle('danger',muted);$('#start-inline').textContent=live?'VOICE ACTIVE':'START VOICE';$('#start-inline').title=live?'Voice session connected':'Start the live voice session'}
function elapsed(){if(!sessionStart)return '00:00';const n=Math.max(0,Math.floor((Date.now()-sessionStart)/1000));return String(Math.floor(n/60)).padStart(2,'0')+':'+String(n%60).padStart(2,'0')}
function updateClock(){try{const now=new Date();$('#clock').textContent=new Intl.DateTimeFormat('en-PK',{timeZone:'Asia/Karachi',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(now);$('#date').textContent=new Intl.DateTimeFormat('en-PK',{timeZone:'Asia/Karachi',weekday:'long',day:'2-digit',month:'long',year:'numeric'}).format(now);$('#sess').textContent=elapsed()}catch(e){$('#clock').textContent=new Date().toLocaleTimeString();}}
setInterval(updateClock,1000);updateClock();
const weatherCodes={0:['Clear sky','☀'],1:['Mainly clear','🌤'],2:['Partly cloudy','⛅'],3:['Overcast','☁'],45:['Fog','🌫'],48:['Rime fog','🌫'],51:['Light drizzle','🌦'],53:['Drizzle','🌦'],55:['Dense drizzle','🌧'],56:['Freezing drizzle','🌧'],57:['Freezing drizzle','🌧'],61:['Light rain','🌦'],63:['Rain','🌧'],65:['Heavy rain','🌧'],66:['Freezing rain','🌧'],67:['Freezing rain','🌧'],71:['Light snow','🌨'],73:['Snow','🌨'],75:['Heavy snow','❄'],77:['Snow grains','❄'],80:['Rain showers','🌦'],81:['Rain showers','🌧'],82:['Violent showers','⛈'],85:['Snow showers','🌨'],86:['Heavy snow showers','❄'],95:['Thunderstorm','⛈'],96:['Thunderstorm with hail','⛈'],99:['Thunderstorm with hail','⛈']};
async function loadWeather(force=false){
 clearTimeout(weatherTimer);const city=(settings.city||'Lahore').trim()||'Lahore';$('#weather-city').textContent=city.toUpperCase()+' // LIVE FEED';$('#weather-refresh').textContent='SYNC…';
 if(weatherAbort){try{weatherAbort.abort()}catch(_){}}weatherAbort=new AbortController();
 try{
  const response=await fetch('/api/weather?city='+encodeURIComponent(city)+(force?'&refresh=1':''),{cache:'no-store',signal:weatherAbort.signal});if(!response.ok)throw new Error('weather endpoint '+response.status);
  const d=await response.json();if(!d.ok)throw new Error(d.error||'weather unavailable');
  $('#weather-temp').textContent=Math.round(d.temperature)+'°';$('#weather-condition').textContent=d.condition||'Conditions unavailable';$('#weather-icon').textContent=d.icon||'◌';$('#weather-feels').textContent=Math.round(d.feels_like)+'°C';$('#weather-hum').textContent=Math.round(d.humidity)+'%';$('#weather-wind').textContent=Math.round(d.wind_speed)+' km/h';$('#weather-rain').textContent=Number(d.precipitation||0).toFixed(1)+' mm';$('#weather-city').textContent=(d.city||city).toUpperCase()+', '+(d.country||'')+' · LIVE';$('#weather-refresh').textContent='UPDATED '+new Date(d.fetched_at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
 }catch(e){if(e.name!=='AbortError'){$('#weather-icon').textContent='◌';$('#weather-temp').textContent='--°';$('#weather-condition').textContent='Weather feed unavailable';$('#weather-city').textContent=city.toUpperCase()+' // OFFLINE';$('#weather-refresh').textContent='NO SIGNAL';$('#weather-feels').textContent='--°C';$('#weather-hum').textContent='--%';$('#weather-wind').textContent='-- km/h';$('#weather-rain').textContent='-- mm'}}
 weatherTimer=setTimeout(()=>loadWeather(false),10*60*1000);
}

async function start(){
 $('#err').textContent='';if(live)return;failed=false;toolBusy=false;
 $('#go').disabled=true;$('#go').textContent='ESTABLISHING LINK…';$('#start-inline').disabled=true;
 try{
  // Unlock Web Audio during the original button click, before the first await.
  // Requesting 24 kHz for the AudioContext itself is not portable; the PCM
  // AudioBuffers below are explicitly tagged 24 kHz and the browser resamples.
  if(!playCtx||playCtx.state==='closed'){
   playCtx=new (window.AudioContext||window.webkitAudioContext)();
   outAnalyser=playCtx.createAnalyser();outAnalyser.fftSize=512;outAnalyser.smoothingTimeConstant=0.72;outAnalyser.connect(playCtx.destination);
  }
  const resumePlayback=playCtx.resume().catch(()=>{});
  const statusResponse=await fetch('/api/status',{cache:'no-store'});if(!statusResponse.ok)throw new Error('NEXUS status endpoint returned HTTP '+statusResponse.status);const statusInfo=await statusResponse.json();
  if(!statusInfo.sdk_available)throw new Error('Google Gen AI SDK is missing. Stop NEXUS and run: python -m pip install -U google-genai');
  if(!statusInfo.api_key_configured)throw new Error('Gemini API key is not configured. Open Settings → Gemini API Key, or use the first-run setup screen. The dashboard still works in preview mode.');
  if(!statusInfo.client_ready)throw new Error(statusInfo.message||'Gemini client is not ready. Check the key and SDK, then restart NEXUS.');
  if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia)throw new Error('Microphone access is unavailable. Use a recent Chrome or Edge browser on localhost.');
  await resumePlayback;
  stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:1}});
  capCtx=new (window.AudioContext||window.webkitAudioContext)({sampleRate:16000});await capCtx.resume();
  const worklet=`class NexusPCM extends AudioWorkletProcessor{constructor(){super();this.acc=0;this.sum=0;this.count=0;this.frame=[];this.rate=16000;this.sourceRate=sampleRate}process(inputs){const ch=inputs&&inputs[0]&&inputs[0][0];if(ch){for(let i=0;i<ch.length;i++){const v=ch[i];this.sum+=v;this.count++;this.acc+=this.rate;if(this.acc>=this.sourceRate){const x=Math.max(-1,Math.min(1,this.sum/Math.max(1,this.count)));this.frame.push(x<0?x*32768:x*32767);this.acc-=this.sourceRate;this.sum=0;this.count=0;if(this.frame.length>=1600){const out=new Int16Array(this.frame.splice(0,1600));this.port.postMessage(out.buffer,[out.buffer])}}}}return true}}registerProcessor('nexus-pcm',NexusPCM)`;
  await capCtx.audioWorklet.addModule(URL.createObjectURL(new Blob([worklet],{type:'application/javascript'})));
  const src=capCtx.createMediaStreamSource(stream);micAnalyser=capCtx.createAnalyser();micAnalyser.fftSize=256;src.connect(micAnalyser);micNode=new AudioWorkletNode(capCtx,'nexus-pcm');src.connect(micNode);const silent=capCtx.createGain();silent.gain.value=0;micNode.connect(silent).connect(capCtx.destination);
  micNode.port.onmessage=e=>{if(ws&&ws.readyState===WebSocket.OPEN&&!muted&&ws.bufferedAmount<256000)ws.send(e.data)};
  $('#start').style.display='none';$('#go').disabled=false;$('#go').textContent='INITIALISE NEXUS';hush();live=true;failed=false;muted=false;sessionStart=Date.now();setState('thinking');syncButtons();entry('sys','Microphone link established');entry('sys','Connecting to Gemini Live');caption('nexus','Establishing secure voice link…');connect();
 }catch(e){hush();await cleanupAudio();live=false;failed=true;setState('error');$('#start').style.display='none';$('#err').textContent='Could not initialise audio: '+(e&&e.message?e.message:String(e))+' — allow microphone access and try again.';$('#go').disabled=false;$('#go').textContent='RETRY CONNECTION';$('#start-inline').disabled=false;syncButtons();notify($('#err').textContent);caption('you',$('#err').textContent)}
}
function connect(){
 const params=new URLSearchParams({voice:settings.voice,greeting:settings.greeting?'1':'0'});const protocol=location.protocol==='https:'?'wss:':'ws:';
 try{ws=new WebSocket(protocol+'//'+location.host+'/ws?'+params.toString());ws.binaryType='arraybuffer'}catch(e){connectionFailed('Could not create WebSocket: '+e.message);return}
 ws.onopen=()=>{setState('idle');updateScreenVisionStatus();if(settings.screen_vision_enabled&&screenStream)sendScreenVisionState(true);caption('nexus','Link established. NEXUS is online.');entry('sys','Gemini Live connection established')};
 ws.onmessage=e=>{
  if(e.data instanceof ArrayBuffer){playAudio(e.data);return}
  let m;try{m=JSON.parse(e.data)}catch(_){entry('sys','Ignored malformed server event');return}
  if(m.type==='transcript'){
   const cls=m.who==='you'?'you':'nexus';if(m.who==='you'&&ignoreTypedTranscript&&String(m.text||'').trim()===ignoreTypedTranscript){ignoreTypedTranscript='';return}if(!cur[m.who]){cur[m.who]=entry(cls,'',true);cur[m.who].dataset.transcript='1'}cur[m.who].textContent+=m.text;persistChatDraft();$('#log-feed').scrollTop=$('#log-feed').scrollHeight;caption(cls,cur[m.who].textContent)
  }else if(m.type==='turn'){cur={you:null,nexus:null};turns++}
  else if(m.type==='stats'){const cpu=clamp(Number(m.cpu)||0,0,100),ram=clamp(Number(m.ram)||0,0,100);$('#vCpu').textContent=cpu+'%';$('#bCpu').style.width=cpu+'%';$('#vRam').textContent=ram+'%';$('#bRam').style.width=ram+'%'}
  else if(m.type==='tool'){toolBusy=true;actionCount++;$('#acts').textContent=actionCount;entry('tool','ACTION // '+m.name+(m.args?' · '+m.args:''));$('#automation-state').textContent='Executing safe action…'}
  else if(m.type==='tool_result'){$('#automation-state').textContent=m.ok?'Action completed':'Action needs attention';$('#automation-last').textContent=(m.name||'Tool')+': '+(m.result||'No details');if(m.name==='request_permanent_delete'||m.name==='request_empty_recycle_bin'){openSettings();void loadPendingFileActions();}}
  else if(m.type==='tool_done'){toolBusy=false}
  else if(m.type==='interrupted'){hush();setState('listening')}
  else if(m.type==='error'){connectionFailed(m.msg||'Unknown Gemini error')}
 }
 ws.onerror=()=>{if(live)entry('sys','WebSocket reported a connection problem')};
 ws.onclose=()=>{if(live&&!failed){live=false;hush();setState('off');syncButtons();caption('you','Voice link closed. Reinitialise NEXUS to reconnect.');$('#start').style.display='none';$('#err').textContent='The voice connection closed. Check the terminal/API quota, then retry.';updateScreenVisionStatus();$('#go').disabled=false;$('#go').textContent='RECONNECT NEXUS';$('#start-inline').disabled=false;notify('Voice connection closed. Check your Gemini key or quota, then reconnect.');cleanupAudio()}};
}
function connectionFailed(message){
 if(failed)return;failed=true;live=false;hush();entry('tool','LINK ERROR // '+message);caption('you',message);$('#err').textContent=message;setState('error');syncButtons();notify(message);
 $('#start').style.display='none';$('#go').disabled=false;$('#go').textContent='RETRY CONNECTION';$('#start-inline').disabled=false;
 try{if(ws&&ws.readyState<WebSocket.CLOSING)ws.close(1011,'NEXUS session error')}catch(_){}
 void cleanupAudio();
}
function playAudio(buf){
 const ctx=playCtx;if(!ctx||ctx.state==='closed'||!buf)return;
 try{
  const incoming=buf instanceof ArrayBuffer?new Uint8Array(buf):ArrayBuffer.isView(buf)?new Uint8Array(buf.buffer,buf.byteOffset,buf.byteLength):null;
  if(!incoming||!incoming.byteLength)return;
  // PCM16 samples are two bytes each. Carry an odd trailing byte across chunks
  // rather than throwing a RangeError or shifting every subsequent sample.
  let bytes=incoming;
  if(pendingPcmByte!==null){const joined=new Uint8Array(incoming.byteLength+1);joined[0]=pendingPcmByte;joined.set(incoming,1);bytes=joined;pendingPcmByte=null;}
  if(bytes.byteLength&1){pendingPcmByte=bytes[bytes.byteLength-1];bytes=bytes.subarray(0,bytes.byteLength-1);}
  if(!bytes.byteLength)return;
  const view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength),f=new Float32Array(bytes.byteLength/2);
  for(let i=0;i<f.length;i++)f[i]=view.getInt16(i*2,true)/32768;
  const ab=ctx.createBuffer(1,f.length,OUT_RATE);ab.copyToChannel(f,0);
  playbackQueue.push({buffer:ab,epoch:playbackEpoch});
  // Keep queueing ordered while a suspended AudioContext is being resumed.
  void drainPlaybackQueue();
 }catch(e){entry('sys','Audio playback error: '+(e&&e.message?e.message:String(e)));}
}
async function drainPlaybackQueue(){
 if(playbackDraining)return;playbackDraining=true;
 try{
  const ctx=playCtx;if(!ctx||ctx.state==='closed')return;
  if(ctx.state!=='running')await ctx.resume();
  while(playbackQueue.length){
   // A reconnect may replace the AudioContext while resume() is pending. Do not
   // consume the new session's queue from an old drain loop.
   if(ctx!==playCtx||ctx.state==='closed')break;
   const item=playbackQueue.shift();if(item.epoch!==playbackEpoch)continue;
   const source=ctx.createBufferSource();source.buffer=item.buffer;source.connect(outAnalyser||ctx.destination);
   source.onended=()=>{sources=sources.filter(x=>x!==source)};
   // A small lead-in absorbs delivery jitter between model audio chunks.
   const startAt=Math.max(nextAudioTime,ctx.currentTime+0.035);
   source.start(startAt);nextAudioTime=startAt+item.buffer.duration;sources.push(source);setState('speaking');
  }
 }catch(e){entry('sys','Audio playback could not start: '+(e&&e.message?e.message:String(e)));}
 finally{playbackDraining=false;if(playbackQueue.length&&playCtx&&playCtx.state==='running')void drainPlaybackQueue();}
}
function hush(){playbackEpoch++;playbackQueue=[];pendingPcmByte=null;sources.forEach(s=>{try{s.stop()}catch(_){}});sources=[];nextAudioTime=0}
async function cleanupAudio(){
 try{if(micNode){micNode.port.onmessage=null;micNode.disconnect();micNode=null}}catch(_){}
 try{if(stream){stream.getTracks().forEach(t=>t.stop());stream=null}}catch(_){}
 for(const ac of [capCtx,playCtx]){try{if(ac&&ac.state!=='closed')await ac.close()}catch(_){}}
 capCtx=null;playCtx=null;micAnalyser=null;outAnalyser=null;audioReady=false;
}
async function disconnect(){failed=false;toolBusy=false;live=false;hush();updateScreenVisionStatus();try{if(ws&&ws.readyState<2)ws.close(1000,'Operator disconnected')}catch(_){}ws=null;await cleanupAudio();setState('off');syncButtons();$('#start').style.display='none';$('#err').textContent='Disconnected by operator. Start NEXUS again when ready.';$('#go').disabled=false;$('#go').textContent='INITIALISE NEXUS';$('#start-inline').disabled=false;caption('you','Voice link disconnected.');entry('sys','Operator disconnected the voice link');notify('Voice link disconnected')}
$('#go').addEventListener('click',start);$('#start-inline').addEventListener('click',start);$('#disconnect').addEventListener('click',disconnect);$('#mute').addEventListener('click',()=>{muted=!muted;syncButtons();caption('you',muted?'Microphone muted.':'Microphone active.');entry('sys',muted?'System Events: Microphone muted':'System Events: Microphone unmuted');notify(muted?'Microphone muted · audio is not being sent':'Microphone unmuted')});
window.addEventListener('beforeunload',()=>{try{if(ws)ws.close()}catch(_){}try{if(stream)stream.getTracks().forEach(t=>t.stop())}catch(_){}try{if(screenStream)screenStream.getTracks().forEach(t=>t.stop())}catch(_){}});

function clamp(v,a,b){return Math.min(b,Math.max(a,v))}
function resize(){D=Math.min(window.devicePixelRatio||1,1);W=innerWidth;H=innerHeight;canvas.width=Math.floor(W*D);canvas.height=Math.floor(H*D);canvas.style.width=W+'px';canvas.style.height=H+'px';ctx.setTransform(D,0,0,D,0,0);particles=Array.from({length:Math.max(settings.lowPower?4:6,Math.round((settings.lowPower?8:12)*settings.intensity/75))},()=>({a:Math.random()*Math.PI*2,r:.9+Math.random()*.5,s:(Math.random()-.5)*.0025,size:.5+Math.random()*1.2,p:Math.random()*7}))}
window.addEventListener('resize',resize);resize();
const freq=new Uint8Array(128),outFreq=new Uint8Array(128);
let lastDrawAt=0;
function draw(now){
 const referenceUI=document.documentElement.classList.contains('reference-ui-light');
 const sampleInterval=referenceUI?(settings.lowPower?160:85):(settings.lowPower?100:50);
 if(referenceUI)window.setTimeout(()=>requestAnimationFrame(draw),sampleInterval);
 else requestAnimationFrame(draw);
 if(document.hidden||now-lastDrawAt<sampleInterval)return;
 lastDrawAt=now;
 const reduced=settings.reduceMotion||reducePreference;
 const motion=(reduced?.17:1)*(Number(settings.intensity||75)/75);
 let state='idle';micLevel=0;outLevel=0;
 if(failed)state='error';else if(!live)state='off';else if(sources.length){state='speaking';if(outAnalyser){outAnalyser.getByteFrequencyData(outFreq);for(let i=0;i<48;i++)outLevel+=outFreq[i];outLevel/=48*255;}}
 else if(toolBusy)state='thinking';else if(muted)state='idle';else{if(micAnalyser){micAnalyser.getByteFrequencyData(freq);for(let i=0;i<44;i++)micLevel+=freq[i];micLevel/=44*255;}if(micLevel>.035)lastMicLoud=now;state='listening';}
  if(state!==audioState)setState(state);
 if(referenceUI)return;
 const accent=accentRGB;
 const compact=W<1001;const cx=W*.5;const cy=H*.49;
 const radius=Math.max(54,Math.min(W*.17,H*.29,220));
 const loud=state==='speaking'?outLevel:state==='listening'?micLevel:0;
 const t=now*.001*motion;const beat=1+loud*.13+Math.sin(t*.75)*.008;
 ctx.clearRect(0,0,W,H);
 const glow=ctx.createRadialGradient(cx,cy,0,cx,cy,radius*2.2);glow.addColorStop(0,`rgba(${accent},${.105+loud*.16})`);glow.addColorStop(.42,`rgba(${accent},${.035+loud*.055})`);glow.addColorStop(1,'rgba(0,0,0,0)');ctx.fillStyle=glow;ctx.fillRect(cx-radius*2.2,cy-radius*2.2,radius*4.4,radius*4.4);
 // Small high-intensity singularity at the centre.
 const singular=ctx.createRadialGradient(cx,cy,0,cx,cy,Math.max(20,radius*.25));singular.addColorStop(0,'rgba(255,255,255,.16)');singular.addColorStop(.14,`rgba(${accent},${.14+loud*.12})`);singular.addColorStop(1,'rgba(0,0,0,0)');ctx.fillStyle=singular;ctx.fillRect(cx-radius*.3,cy-radius*.3,radius*.6,radius*.6);
 const yaw=t*.25;const pitch=-.22+Math.sin(t*.21)*.045;
 function project(u,v){
  const major=radius*.59*(1+.018*Math.sin(u*4-t*1.3)*motion);
  const minor=radius*.245*(1+loud*.12*Math.sin(u*5-v*3-t*3)*motion);
  const ripple=(Math.sin(u*7+t*1.4+v*2)+Math.sin(u*11-t*.85-v*4)*.45)*radius*.012*motion*(.3+loud*1.7);
  const rr=major+(minor*Math.cos(v))+ripple;
  let x=rr*Math.cos(u),y=rr*Math.sin(u),z=minor*Math.sin(v)+Math.sin(u*5+v*3-t*2)*radius*.012*motion;
  const x1=x*Math.cos(yaw)+z*Math.sin(yaw),z1=-x*Math.sin(yaw)+z*Math.cos(yaw);
  const y2=y*Math.cos(pitch)-z1*Math.sin(pitch),z2=y*Math.sin(pitch)+z1*Math.cos(pitch);
  const perspective=radius*4.8,scale=perspective/(perspective-z2);
  return {x:cx+x1*scale,y:cy+y2*scale,z:z2,scale};
 }
 ctx.save();ctx.globalCompositeOperation='lighter';ctx.lineCap='round';ctx.lineJoin='round';
 // Thin elliptic guide orbits rotate at different angles around the toroidal core.
 for(let k=0;k<3;k++){
  ctx.save();ctx.translate(cx,cy);ctx.rotate([-.55,.28,1.08][k]+t*(k%2?.05:-.035));ctx.beginPath();ctx.ellipse(0,0,radius*(1.03+k*.11),radius*(.25+k*.045),0,0,Math.PI*2);ctx.strokeStyle=k===1?'rgba(255,255,255,.23)':`rgba(${accent},${.17-k*.025})`;ctx.lineWidth=k===1?1:.7;ctx.setLineDash(k===2?[3,8]:[1,6]);ctx.lineDashOffset=-t*8*(k+1);ctx.stroke();ctx.setLineDash([]);ctx.restore();
 }
 // Torus latitude lines: the surface looks like a flowing doughnut, not a flat circle.
 const ringCount=settings.lowPower?7:10,ringSegments=settings.lowPower?34:52;
 for(let r=0;r<ringCount;r++){
  const v=r/ringCount*Math.PI*2;ctx.beginPath();let avgDepth=0;
  for(let j=0;j<=ringSegments;j++){const u=j/ringSegments*Math.PI*2;const p=project(u,v);avgDepth+=p.z;if(j===0)ctx.moveTo(p.x,p.y);else ctx.lineTo(p.x,p.y);}
  avgDepth/=ringSegments+1;const alpha=Math.max(.12,Math.min(.58,.29+avgDepth/(radius*4)+loud*.15));ctx.strokeStyle=r%5===0?`rgba(255,255,255,${Math.min(.8,alpha+.23)})`:`rgba(${accent},${alpha})`;ctx.lineWidth=(r%5===0?1.15:.62)+loud*.45;ctx.shadowColor=`rgb(${accent})`;ctx.shadowBlur=settings.lowPower?0:(r%5===0?2:0)+loud*2;ctx.stroke();
 }
 // Cross-section meridians provide the dense wireframe mesh.
 const meridians=settings.lowPower?9:14,meridianSegments=settings.lowPower?16:24;
 for(let m=0;m<meridians;m++){
  const u=m/meridians*Math.PI*2+t*.018*(m%2?-1:1);ctx.beginPath();let depth=0;
  for(let j=0;j<=meridianSegments;j++){const v=j/meridianSegments*Math.PI*2;const p=project(u,v);depth+=p.z;if(j===0)ctx.moveTo(p.x,p.y);else ctx.lineTo(p.x,p.y);}
  depth/=meridianSegments+1;ctx.strokeStyle=m%6===0?`rgba(255,255,255,${.42+loud*.15})`:`rgba(${accent},${Math.max(.12,Math.min(.43,.23+depth/(radius*5)))})`;ctx.lineWidth=m%6===0?1:.55;ctx.shadowBlur=m%6===0?2:0;ctx.stroke();
 }
 ctx.shadowBlur=0;
 // A luminous inner torus edge and small moving tracking nodes.
 for(let n=0;n<(settings.lowPower?8:12);n++){
  const nodeCount=settings.lowPower?8:12;const u=(n/nodeCount*Math.PI*2+t*.15)%(Math.PI*2),v=(n%3===0?Math.PI*.5:Math.sin(n*1.7)*1.1)+t*.08;const p=project(u,v);const front=Math.max(.25,Math.min(1,.5+p.z/(radius*1.3)));const rr=n%6===0?2.5:1.25;
  ctx.beginPath();ctx.fillStyle=n%6===0?'rgba(255,255,255,.95)':`rgba(${accent},${.42+.48*front})`;ctx.shadowColor=`rgb(${accent})`;ctx.shadowBlur=n%6===0?5:1;ctx.arc(p.x,p.y,rr+(n%6===0?loud*1.8:0),0,Math.PI*2);ctx.fill();
  if(n%6===0){ctx.beginPath();ctx.strokeStyle=`rgba(${accent},.3)`;ctx.lineWidth=.6;ctx.moveTo(p.x-9,p.y);ctx.lineTo(p.x+9,p.y);ctx.moveTo(p.x,p.y-9);ctx.lineTo(p.x,p.y+9);ctx.stroke();}
 }
 ctx.shadowBlur=0;
 // Floating dust and sparks on the outside of the torus.
 for(let i=0;i<particles.length;i++){
  const p=particles[i];if(!reduced)p.a+=p.s*(1+loud*3.2)*motion;const orbit=radius*(.9+p.r*.37+loud*.018);const x=cx+Math.cos(p.a+t*.02)*orbit,y=cy+Math.sin(p.a+t*.02)*orbit*.63;
  ctx.beginPath();ctx.fillStyle=i%9===0?'rgba(255,255,255,.82)':`rgba(${accent},${.18+.47*Math.abs(Math.sin(t+p.p))})`;ctx.shadowColor=`rgb(${accent})`;ctx.shadowBlur=i%9===0?3:0;ctx.arc(x,y,p.size*(.7+loud*.5),0,Math.PI*2);ctx.fill();
 }
 // Thin audio trace through the centre; it responds to incoming voice and playback.
 const activeBins=state==='speaking'?outFreq:freq;const waveW=radius*.54;ctx.beginPath();ctx.lineWidth=1.25;ctx.strokeStyle='rgba(245,255,255,.87)';ctx.shadowColor=`rgb(${accent})`;ctx.shadowBlur=2+loud*4;
 const waveSamples=settings.lowPower?48:72;for(let i=0;i<=waveSamples;i++){const n=i/waveSamples,x=cx+(n*2-1)*waveW;const b=Math.min(55,Math.floor(Math.abs(n-.5)*110)+2);const spectral=(state==='speaking'||state==='listening')?(activeBins[b]||0)/255:0;const env=Math.pow(Math.max(0,Math.sin(n*Math.PI)),.55);const y=cy+(Math.sin(n*115-t*4)*(.6+loud*2)+Math.sin(n*42+t*1.7)*(1+loud*3)+Math.sin(n*21-t)*spectral*radius*.08)*env;if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);}
 ctx.stroke();ctx.shadowBlur=0;ctx.beginPath();ctx.fillStyle='rgba(255,255,255,.97)';ctx.shadowColor=`rgb(${accent})`;ctx.shadowBlur=5+loud*5;ctx.arc(cx,cy,1.7+loud*1.5,0,Math.PI*2);ctx.fill();ctx.restore();
}


function setChatTab(tab){activeChatView=tab;const isChat=tab==='chat';$('#tab-chat').classList.toggle('active',isChat);$('#tab-history').classList.toggle('active',!isChat);$('#tab-chat').setAttribute('aria-selected',String(isChat));$('#tab-history').setAttribute('aria-selected',String(!isChat));$('#log-feed').style.display=isChat?'flex':'none';$('#history-view').hidden=isChat;if(!isChat)renderHistory();}
function activateNav(name){document.querySelectorAll('.nav-link').forEach(b=>b.classList.remove('active'));const b=$('#nav-'+name);if(b)b.classList.add('active');$('#app').classList.toggle('chat-open',name==='chat'&&window.innerWidth>760);if(name==='dashboard')$('#app').classList.remove('mobile-chat-open');}
function sendTextMessage(input){const text=String(input.value||'').trim();if(!text)return;if(!live||!ws||ws.readyState!==WebSocket.OPEN){notify('Connect the voice link first. Typed chat uses the live Gemini session.');return;}if(text.length>2000){notify('Messages are limited to 2,000 characters.');return;}try{ws.send(JSON.stringify({type:'text',text}));ignoreTypedTranscript=text;cur.you=null;entry('you',text,true);caption('you',text);input.value='';$('#command-input').value='';$('#chat-input').value='';setChatTab('chat');}catch(e){notify('Could not send message: '+(e&&e.message?e.message:String(e)));}}
function openPalette(){const scrim=$('#palette-scrim');scrim.classList.add('open');scrim.setAttribute('aria-hidden','false');$('#palette-input').value='';document.querySelectorAll('.command-option').forEach(b=>b.hidden=false);setTimeout(()=>$('#palette-input').focus(),20);}
function closePalette(){const scrim=$('#palette-scrim');scrim.classList.remove('open');scrim.setAttribute('aria-hidden','true');}
$('#tab-chat').addEventListener('click',()=>setChatTab('chat'));$('#tab-history').addEventListener('click',()=>setChatTab('history'));
$('#new-chat').addEventListener('click',()=>{archiveCurrentThread();$('#log-feed').replaceChildren();feedCount=0;$('#feed-count').textContent='0 EVENTS';cur={you:null,nexus:null};setChatTab('chat');entry('sys','New Nexus chat view started.');entry('nexus','Ready when you are. Type a message below or activate the voice link.');activateNav('chat');notify(live?'New chat view created. The current Live session context remains active.':'New chat view created.');});
$('#chat-form').addEventListener('submit',e=>{e.preventDefault();sendTextMessage($('#chat-input'));});$('#command-form').addEventListener('submit',e=>{e.preventDefault();sendTextMessage($('#command-input'));});
$('#palette-open').addEventListener('click',openPalette);$('#palette-scrim').addEventListener('click',e=>{if(e.target===$('#palette-scrim'))closePalette()});
$('#palette-input').addEventListener('input',e=>{const query=e.target.value.trim().toLowerCase();document.querySelectorAll('.command-option').forEach(b=>b.hidden=!b.textContent.toLowerCase().includes(query));});
document.querySelectorAll('.command-option').forEach(b=>b.addEventListener('click',()=>{const action=b.dataset.command;closePalette();if(action==='dashboard'){activateNav('dashboard');notify('Dashboard view active');}else if(action==='chat'){activateNav('chat');setChatTab('chat');if(window.innerWidth<=760)$('#app').classList.add('mobile-chat-open');$('#chat-input').focus();}else if(action==='settings'){activateNav('settings');openSettings();}else if(action==='weather'){loadWeather(true);notify('Refreshing weather for '+settings.city);}else if(action==='voice'){if(live)disconnect();else start();}}));
$('#nav-dashboard').addEventListener('click',()=>{$('#app').classList.remove('mobile-chat-open','chat-open');activateNav('dashboard');setChatTab('chat');caption('nexus','Core interface ready. Start the voice link or type a message below.');});
$('#nav-chat').addEventListener('click',()=>{activateNav('chat');setChatTab('chat');if(window.innerWidth<=760)$('#app').classList.add('mobile-chat-open');$('#chat-input').focus();});
$('#settings-open').addEventListener('click',()=>activateNav('settings'));
$('#weather-refresh-button').addEventListener('click',()=>loadWeather(true));
$('#gestures-toggle').addEventListener('click',()=>{const btn=$('#gestures-toggle'),active=btn.getAttribute('aria-pressed')!=='true';btn.setAttribute('aria-pressed',String(active));notify(active?'Gesture status indicator enabled. Camera-based gesture recognition is not configured.':'Gesture status indicator disabled.');});
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();openPalette();}else if((e.ctrlKey||e.metaKey)&&e.shiftKey&&e.code==='Space'){e.preventDefault();if(live)disconnect();else start();}else if((e.ctrlKey||e.metaKey)&&e.shiftKey&&e.key.toLowerCase()==='j'){e.preventDefault();$('#command-input').focus();}else if(e.key==='Escape'){closePalette();closeSettings();}});

// Holographic Command Center: local telemetry, a rotatable wireframe model,
// interactive subsystem topology, and live task-state visualization.
let selectedHoloModel = 'core';
let selectedSystemNode = 'core';
let telemetryOnline = false;
let telemetryFailure = '';
let telemetrySampleCount = 0;
let holoFrame = 0;
let lastModelFrame = 0;
let modelCanvas = $('#model-canvas');
let modelCtx = modelCanvas ? modelCanvas.getContext('2d') : null;
let modelDpr = 1, modelW = 0, modelH = 0;

const MODEL_LABELS = {core:'QUANTUM CORE', field:'MAGNETIC FIELD ARRAY', shield:'PHOTON SHIELD'};
const SYSTEM_DETAILS = {
 core:{title:'NEURAL CORE',copy:'Main interface loop and state coordinator.'},
 voice:{title:'VOICE LINK',copy:'Gemini Live voice connection and two-way audio stream.'},
 vision:{title:'SCREEN VISION',copy:'Only the screen explicitly selected by the operator can be shared.'},
 automation:{title:'DESKTOP TOOLS',copy:'Allowlisted local actions, controlled by the saved permission switches.'},
 telemetry:{title:'SYSTEM I/O',copy:'Local CPU, memory, and aggregate network throughput samples.'}
};
function formatRate(value){
 const rate=Math.max(0,Number(value)||0);
 if(rate>=1024*1024)return (rate/(1024*1024)).toFixed(2)+' MB/s';
 if(rate>=1024)return (rate/1024).toFixed(1)+' KB/s';
 return Math.round(rate)+' B/s';
}
function setMeter(id,value,max=100){const node=$('#'+id);if(node)node.style.width=clamp((Number(value)||0)/max*100,0,100)+'%';}
function updateLocalTelemetry(d){
 const cpu=clamp(Number(d.cpu_percent)||0,0,100),ram=clamp(Number(d.ram_percent)||0,0,100);
 $('#vCpu').textContent=Math.round(cpu)+'%';$('#vRam').textContent=Math.round(ram)+'%';setMeter('bCpu',cpu);setMeter('bRam',ram);
 const rx=Math.max(0,Number(d.net_rx_bps)||0),tx=Math.max(0,Number(d.net_tx_bps)||0);
 $('#vNetDown').textContent=formatRate(rx);$('#vNetUp').textContent=formatRate(tx);
 $('#cmd-cpu').textContent=Math.round(cpu)+'%';$('#cmd-ram').textContent=Math.round(ram)+'%';$('#cmd-net-down').textContent=formatRate(rx);$('#cmd-net-up').textContent=formatRate(tx);
 // Scale the compact network bars to 10 MB/s, while the labels retain real rates.
 setMeter('bNetDown',rx,10*1024*1024);setMeter('bNetUp',tx,10*1024*1024);
 $('#telemetry-state').textContent='LIVE LOCAL SAMPLE · '+(d.sampled_at?new Date(d.sampled_at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'NOW');
 telemetryOnline=true;telemetryFailure='';telemetrySampleCount++;
}
async function pollLocalTelemetry(){
 try{
  const response=await fetch('/api/telemetry',{cache:'no-store'});
  const payload=await response.json();
  if(!response.ok||!payload.ok)throw new Error(payload.error||('HTTP '+response.status));
  updateLocalTelemetry(payload);
 }catch(error){
  telemetryOnline=false;telemetryFailure=error&&error.message?error.message:'Telemetry unavailable';
  const note=$('#telemetry-state');if(note)note.textContent='TELEMETRY OFFLINE · '+telemetryFailure.slice(0,46);
 }
 renderSystemDiagram();renderTaskActivity();
}
function resizeModelCanvas(){
 if(!modelCanvas||!modelCtx)return;
 const rect=modelCanvas.getBoundingClientRect();if(!rect.width||!rect.height)return;
 modelDpr=Math.min(window.devicePixelRatio||1,1);modelW=rect.width;modelH=rect.height;
 modelCanvas.width=Math.max(1,Math.round(modelW*modelDpr));modelCanvas.height=Math.max(1,Math.round(modelH*modelDpr));
 modelCtx.setTransform(modelDpr,0,0,modelDpr,0,0);
}
function geometryForModel(kind){
 const verts=[],edges=[],edgeSeen=new Set();
 const vertex=(x,y,z)=>{verts.push([x,y,z]);return verts.length-1;};
 const edge=(a,b,style='')=>{if(a===b)return;const key=Math.min(a,b)+':'+Math.max(a,b);if(!edgeSeen.has(key)){edgeSeen.add(key);edges.push([a,b,style]);}};
 const addTorus=(major,minor,majorSteps,minorSteps,rx=0,ry=0,rz=0,style='')=>{
  const grid=[],sx=Math.sin(rx),cx=Math.cos(rx),sy=Math.sin(ry),cy=Math.cos(ry),sz=Math.sin(rz),cz=Math.cos(rz);
  for(let i=0;i<majorSteps;i++){
   const u=2*Math.PI*i/majorSteps,row=[];
   for(let j=0;j<minorSteps;j++){
    const v=2*Math.PI*j/minorSteps,r=major+minor*Math.cos(v);
    let x=r*Math.cos(u),y=r*Math.sin(u),z=minor*Math.sin(v);
    const y1=y*cx-z*sx,z1=y*sx+z*cx;
    const x2=x*cy+z1*sy,z2=-x*sy+z1*cy;
    const x3=x2*cz-y1*sz,y3=x2*sz+y1*cz;
    row.push(vertex(x3,y3,z2));
   }
   grid.push(row);
  }
  for(let i=0;i<majorSteps;i++)for(let j=0;j<minorSteps;j++){
   edge(grid[i][j],grid[i][(j+1)%minorSteps],style);
   edge(grid[i][j],grid[(i+1)%majorSteps][j],style);
  }
 };
 if(kind==='core'){
  // A technical reactor: interlocking magnetic toroids around a faceted plasma nucleus.
  // Each ring is genuinely 3D mesh geometry and rotates with the perspective projection.
  addTorus(.735,.102,32,6,.68,.16,.12,'outline');
  addTorus(.705,.052,28,5,1.18,-.12,-.28,'core');
  addTorus(.445,.036,24,4,.20,1.02,.24,'core');
  // Two thin far-field hoops create scale and depth without a bulky spherical cage.
  addTorus(.98,.006,36,3,1.07,.34,.18,'outline');
  addTorus(.92,.006,32,3,.18,1.24,-.22,'outline');
  // Central containment sphere: latitude/longitude lines plus sparse meridians.
  const rows=7,cols=20,r=.286,sphere=[];
  for(let ri=1;ri<rows;ri++){
   const phi=Math.PI*ri/rows,row=[];
   for(let ci=0;ci<cols;ci++){
    const theta=2*Math.PI*ci/cols;
    row.push(vertex(r*Math.sin(phi)*Math.cos(theta),r*Math.cos(phi),r*.88*Math.sin(phi)*Math.sin(theta)));
   }
   sphere.push(row);
  }
  for(let ri=0;ri<sphere.length;ri++)for(let ci=0;ci<cols;ci++){
   edge(sphere[ri][ci],sphere[ri][(ci+1)%cols],ri%2===0?'core':'outline');
   if(ri+1<sphere.length)edge(sphere[ri][ci],sphere[ri+1][ci],ci%4===0?'core':'outline');
  }
  const north=vertex(0,r,0),south=vertex(0,-r,0);
  for(let ci=0;ci<cols;ci++){
   edge(north,sphere[0][ci],ci%4===0?'core':'outline');
   edge(south,sphere[sphere.length-1][ci],ci%4===0?'core':'outline');
  }
  // Precision braces and collector pins around the core's equator.
  const hubRing=[];
  for(let i=0;i<12;i++){const a=2*Math.PI*i/12;hubRing.push(vertex(Math.cos(a)*.355,Math.sin(a)*.355,.01));}
  for(let i=0;i<12;i++){
   edge(hubRing[i],hubRing[(i+1)%12],'core');
   if(i%2===0)edge(vertex(Math.cos(i*Math.PI/6)*.285,Math.sin(i*Math.PI/6)*.285,.015),hubRing[i],'core');
  }
 }else if(kind==='field'){
  // An abstract magnetic-field mode: intersecting orbital hoops around a quiet center.
  addTorus(.70,.008,42,3,.05,.05,0,'outline');
  addTorus(.70,.008,42,3,1.16,.05,.05,'core');
  addTorus(.70,.008,42,3,.05,1.16,.05,'outline');
  addTorus(.47,.045,24,4,.62,.30,.20,'core');
  const n=12;
  for(let i=0;i<n;i++){
   const a=2*Math.PI*i/n;
   const p=vertex(.30*Math.cos(a),.30*Math.sin(a),.06*Math.sin(a*2));
   const q=vertex(.82*Math.cos(a),.82*Math.sin(a),.06*Math.sin(a*2));
   edge(p,q,i%3===0?'core':'outline');
  }
 }else if(kind==='shield'){
  for(const scale of [1,.66]){
   const base=verts.length,pts=[[0,-.95,0],[0,.9,0],[-.72,0,0],[.72,0,0],[0,0,-.56],[0,0,.56]];
   pts.forEach(p=>vertex(p[0]*scale,p[1]*scale,p[2]*scale));
   const es=[[0,2],[0,3],[0,4],[0,5],[1,2],[1,3],[1,4],[1,5],[2,4],[4,3],[3,5],[5,2]];es.forEach(e=>edge(base+e[0],base+e[1]));
  }
  for(let i=0;i<6;i++)edge(i,i+6);
 
 }
 return {verts,edges};
}
const MODEL_GEOMETRY={core:geometryForModel('core'),field:geometryForModel('field'),shield:geometryForModel('shield')};
function createCoreParticleCloud(count){
 const points=[];
 for(let i=0;i<count;i++){
  // Most points sit on the shell to preserve the spherical silhouette; the rest
  // occupy the volume and give the center image its deep, granular appearance.
  const shell=Math.random()<.72;
  const r=shell?.80+Math.random()*.20:Math.cbrt(Math.random())*.80;
  const z=Math.random()*2-1,theta=Math.random()*Math.PI*2,flat=Math.sqrt(Math.max(0,1-z*z));
  points.push({x:r*flat*Math.cos(theta),y:r*z,z:r*flat*Math.sin(theta),
   size:.68+Math.random()*1.35,phase:Math.random()*Math.PI*2,glint:Math.random()});
 }
 return points;
}
const CORE_PARTICLE_CLOUD=createCoreParticleCloud(2100);
let lastCoordsUpdate=0;
function drawParticleCore(c,w,h,now,cx,cy,radius,state,loud,elapsed){
 const speaking=state==='speaking',listening=state==='listening',thinking=state==='thinking';
 const low=!!settings.lowPower;
 const pulse=.5+.5*Math.sin(elapsed*(speaking?5.1:listening?2.4:1.35));
 // One ambient gradient instead of thousands of per-particle blur/shadow operations.
 const aura=c.createRadialGradient(cx,cy,radius*.05,cx,cy,radius*1.36);
 aura.addColorStop(0,`rgba(23,76,177,${.12+pulse*.025+loud*.09})`);
 aura.addColorStop(.56,`rgba(18,62,153,${.075+loud*.045})`);
 aura.addColorStop(.86,`rgba(24,91,207,${.065+loud*.045})`);
 aura.addColorStop(1,'rgba(0,0,0,0)');
 c.fillStyle=aura;c.fillRect(cx-radius*1.4,cy-radius*1.4,radius*2.8,radius*2.8);
 // A barely visible spherical body keeps gaps between the dots feeling volumetric.
 const body=c.createRadialGradient(cx-radius*.18,cy-radius*.23,radius*.06,cx,cy,radius*1.02);
 body.addColorStop(0,'rgba(7,28,67,.36)');body.addColorStop(.68,'rgba(4,18,49,.17)');body.addColorStop(1,'rgba(12,51,122,.035)');
 c.fillStyle=body;c.beginPath();c.arc(cx,cy,radius,0,Math.PI*2);c.fill();
 // Low-contrast scan pulses: stronger during output, gently breathing while listening.
 if(speaking||listening||thinking){
  const ringCount=speaking?3:1;
  for(let i=0;i<ringCount;i++){
   const phase=((elapsed*(speaking?1.18:listening?.42:.68)+i/ringCount)%1);
   const rr=radius*(.18+phase*.82);
   const alpha=(1-phase)*(speaking?.24:listening?.12:.10)+loud*.045;
   c.beginPath();c.arc(cx,cy,rr,0,Math.PI*2);
   c.strokeStyle=speaking?`rgba(128,224,255,${alpha})`:thinking?`rgba(246,187,112,${alpha*.72})`:`rgba(69,142,255,${alpha})`;
   c.lineWidth=speaking?1.0:.65;c.stroke();
  }
 }
 const yaw=elapsed*(settings.reduceMotion||reducePreference?0:settings.lowPower?.12:.24);
 const pitch=-.10+((settings.reduceMotion||reducePreference)?0:Math.sin(elapsed*.31)*.035);
 const cyaw=Math.cos(yaw),syaw=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch);
 const count=low?780:CORE_PARTICLE_CLOUD.length;
 c.save();c.globalCompositeOperation='lighter';
 const palette=speaking?['rgb(43,126,235)','rgb(103,191,255)','rgb(204,247,255)']:
  listening?['rgb(21,72,204)','rgb(45,128,255)','rgb(110,186,255)']:
  thinking?['rgb(44,80,194)','rgb(87,139,250)','rgb(165,194,255)']:
  ['rgb(16,53,177)','rgb(29,91,210)','rgb(85,151,255)'];
 for(let i=0;i<count;i++){
  const p=CORE_PARTICLE_CLOUD[i];
  // Slight audio pressure deforms the cloud radially without costly simulation.
  const drift=1+(speaking?loud*.018:listening?loud*.009:0)*Math.sin(p.phase+elapsed*3.2);
  const x=p.x*drift,y=p.y*drift,z=p.z*drift;
  const xr=x*cyaw+z*syaw,zr=-x*syaw+z*cyaw;
  const yr=y*cp-zr*sp,zf=y*sp+zr*cp;
  const perspective=1/(1-zf*.17);
  const px=cx+xr*radius*perspective,py=cy+yr*radius*perspective;
  const distance=Math.hypot(px-cx,py-cy)/radius;
  if(distance>1.035)continue;
  const depth=(zf+1)*.5;
  const rim=Math.max(0,Math.min(1,(distance-.60)/.40));
  const twinkle=.82+.18*Math.sin(elapsed*(speaking?7.5:2.1)+p.phase);
  const alpha=Math.max(.08,Math.min(.92,(.21+depth*.43+rim*.22+p.glint*.14+loud*(speaking?.25:listening?.12:0))*twinkle));
  const color=p.glint>.91?palette[2]:depth>.57?palette[1]:palette[0];
  c.fillStyle=color;c.globalAlpha=alpha*(thinking?.93:1);
  const size=p.size*(.70+depth*.36+loud*(speaking?.50:listening?.18:0));
  c.fillRect(px-size*.5,py-size*.5,size,size);
 }
 c.globalAlpha=1;
 // Central white plasma nucleus, with one glow pass rather than glow on every point.
 const nucleus=radius*(.155+(speaking?loud*.028:listening?loud*.012:0)+pulse*.004);
 const coreGlow=c.createRadialGradient(cx-radius*.018,cy-radius*.025,1,cx,cy,nucleus*2.05);
 coreGlow.addColorStop(0,'rgba(255,255,255,.97)');
 coreGlow.addColorStop(.10,'rgba(219,242,255,.92)');
 coreGlow.addColorStop(.24,`rgba(113,179,255,${.78+(speaking?.12:listening?.06:0)})`);
 coreGlow.addColorStop(.53,`rgba(43,112,255,${.34+loud*.10})`);
 coreGlow.addColorStop(1,'rgba(17,77,220,0)');
 c.fillStyle=coreGlow;c.beginPath();c.arc(cx,cy,nucleus*2.05,0,Math.PI*2);c.fill();
 c.fillStyle='rgba(255,255,255,.97)';c.beginPath();c.arc(cx,cy,Math.max(2.2,radius*.032+loud*(speaking?2.4:1.0)),0,Math.PI*2);c.fill();
 c.restore();
}
function drawHolographicModel(now){
 if(!modelCanvas||!modelCtx)return;
 if(document.hidden){holoFrame=setTimeout(()=>drawHolographicModel(performance.now()),280);return;}
 const frameInterval=(settings.lowPower?135:(settings.reduceMotion||reducePreference?145:66));
 if(now-lastModelFrame<frameInterval){holoFrame=setTimeout(()=>drawHolographicModel(performance.now()),Math.max(12,frameInterval-(now-lastModelFrame)));return;}
 lastModelFrame=now;
 const w=modelW||modelCanvas.clientWidth,h=modelH||modelCanvas.clientHeight;
 if(!w||!h){holoFrame=setTimeout(()=>drawHolographicModel(performance.now()),250);return;}
 const c=modelCtx;c.clearRect(0,0,w,h);
 const reduced=settings.reduceMotion||reducePreference;
 const elapsed=now*.001*(settings.lowPower?.72:1.0);
 const cx=w*.5,cy=h*.49+((reduced)?0:Math.sin(elapsed*.55)*1.2);
 const scale=Math.max(24,Math.min(w*.41,h*.455))*(selectedHoloModel==='shield'?.86:1);
 const state=audioState||'off';
 const loud=state==='speaking'?Math.max(0,Number(outLevel)||0):state==='listening'?Math.max(0,Number(micLevel)||0):0;
 if(selectedHoloModel==='core'){
  drawParticleCore(c,w,h,now,cx,cy,scale,state,loud,elapsed);
 }else{
  // Other selectable modes retain the lightweight geometric projection.
  const geometry=MODEL_GEOMETRY[selectedHoloModel]||MODEL_GEOMETRY.field;
  const yaw=reduced?0:elapsed*.32,pitch=-.10+Math.sin(elapsed*.35)*.05;
  c.save();c.translate(cx,cy);c.strokeStyle='rgba(91,168,255,.14)';c.lineWidth=.6;
  c.beginPath();c.ellipse(0,0,scale*1.12,scale*.31,elapsed*.12,0,Math.PI*2);c.stroke();c.restore();
  const sy=Math.sin(yaw),cyaw=Math.cos(yaw),sp=Math.sin(pitch),cp=Math.cos(pitch);
  const points=geometry.verts.map(v=>{const x=v[0]*cyaw+v[2]*sy,z1=-v[0]*sy+v[2]*cyaw,y=v[1]*cp-z1*sp,z=v[1]*sp+z1*cp,pr=1/(1-z*.11);return{x:cx+x*scale*pr,y:cy-y*scale*pr,z};});
  c.save();c.globalCompositeOperation='lighter';c.lineCap='round';
  for(const [a,b,style] of geometry.edges){const p=points[a],q=points[b],depth=(p.z+q.z)*.5;c.beginPath();c.moveTo(p.x,p.y);c.lineTo(q.x,q.y);c.strokeStyle=style==='core'?`rgba(224,250,255,${.25+Math.max(0,depth)*.36})`:`rgba(82,174,255,${.18+Math.max(0,depth)*.24})`;c.lineWidth=style==='core'?.8:.48;c.stroke();}
  c.restore();
 }
 if(now-lastCoordsUpdate>520){
  $('#model-coords').textContent='X '+String(Math.round(Math.sin(elapsed)*45)).padStart(2,'0')+' · Y '+String(Math.round(Math.cos(elapsed*.8)*45)).padStart(2,'0')+' · Z 00';
  lastCoordsUpdate=now;
 }
 holoFrame=setTimeout(()=>drawHolographicModel(performance.now()),frameInterval);
}
function chooseModel(kind){if(!MODEL_LABELS[kind])return;selectedHoloModel=kind;$('#model-label').textContent=MODEL_LABELS[kind];const viewport=$('.model-viewport');if(viewport)viewport.dataset.model=kind;document.querySelectorAll('.model-select').forEach(button=>{const active=button.dataset.model===kind;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});}
function getSystemNodeState(id){
 if(id==='core')return {state:failed?'ATTENTION':'ONLINE',mode:failed?'busy':'online'};
 if(id==='voice')return {state:live?'CONNECTED':'STANDBY',mode:live?'online':''};
 if(id==='vision')return settings.screen_vision_enabled?(screenStream?{state:'SHARING',mode:'online'}:{state:'ARMED',mode:''}):{state:'OFF',mode:''};
 if(id==='automation')return settings.automation_enabled?{state:toolBusy?'RUNNING':'READY',mode:toolBusy?'busy':'online'}:{state:'LOCKED',mode:''};
 return telemetryOnline?{state:'LIVE',mode:'online'}:{state:'RETRY',mode:''};
}
function renderSystemDiagram(){
 if(!$('#system-map'))return;
 document.querySelectorAll('.system-node').forEach(node=>{
  const id=node.dataset.node,info=getSystemNodeState(id),chosen=id===selectedSystemNode;
  node.classList.toggle('selected',chosen);node.classList.toggle('online',info.mode==='online');node.classList.toggle('busy',info.mode==='busy');node.setAttribute('aria-pressed',String(chosen));
 });
 const details=SYSTEM_DETAILS[selectedSystemNode]||SYSTEM_DETAILS.core,st=getSystemNodeState(selectedSystemNode);
 $('#system-detail-title').textContent=details.title;$('#system-detail-state').textContent=st.state;$('#system-detail-copy').textContent=details.copy;
 const count=[live,settings.screen_vision_enabled&&!!screenStream,toolBusy,telemetryOnline].filter(Boolean).length;
 $('#topology-state').textContent=(count?String(count).padStart(2,'0'):'00')+' LIVE';
}
function chooseSystemNode(id){if(!SYSTEM_DETAILS[id])return;selectedSystemNode=id;renderSystemDiagram();}
function renderTaskActivity(){
 if(!$('#task-list'))return;
 const visionActive=!!settings.screen_vision_enabled&&!!screenStream;
 const items=[
  {id:'core',active:!failed,busy:toolBusy||audioState==='thinking',status:failed?'ERROR':toolBusy||audioState==='thinking'?'PROCESSING':live?audioState.toUpperCase():'READY'},
  {id:'voice',active:live,busy:audioState==='listening'||audioState==='speaking',status:live?(audioState==='speaking'?'SPEAKING':audioState==='listening'?'LISTENING':audioState==='thinking'?'THINKING':'LINKED'):'STANDBY'},
  {id:'vision',active:visionActive,busy:false,status:visionActive?'SHARING':settings.screen_vision_enabled?'ARMED':'OFF'},
  {id:'automation',active:toolBusy,busy:toolBusy,status:!settings.automation_enabled?'LOCKED':toolBusy?'EXECUTING':'READY'},
  {id:'telemetry',active:telemetryOnline,busy:false,status:telemetryOnline?'LIVE':'RETRY'}
 ];
 items.forEach(item=>{const row=document.querySelector('.task-row[data-task="'+item.id+'"]');if(!row)return;row.classList.toggle('active',item.active);row.classList.toggle('busy',item.busy);const status=row.querySelector('.task-status');if(status)status.textContent=item.status;});
 const activeCount=items.filter(item=>item.active&&(item.busy||['voice','vision','telemetry'].includes(item.id))).length;
 $('#task-active-count').textContent=String(activeCount).padStart(2,'0')+' ACTIVE';
 $('#task-footer-text').textContent=toolBusy?'EXECUTING DESKTOP ACTION':visionActive?'SCREEN STREAM ACTIVE':live?'VOICE SESSION LIVE':'MONITORING LOCAL SYSTEM';
 $('#task-frame').textContent='FRAME '+String(Math.floor(Date.now()/1000)%10000).padStart(4,'0');
}
document.querySelectorAll('.model-select').forEach(button=>button.addEventListener('click',()=>chooseModel(button.dataset.model)));
document.querySelectorAll('.system-node').forEach(button=>button.addEventListener('click',()=>chooseSystemNode(button.dataset.node)));
window.addEventListener('resize',resizeModelCanvas);
resizeModelCanvas();
drawHolographicModel(performance.now());
void pollLocalTelemetry();
setInterval(pollLocalTelemetry,4000);
setInterval(()=>{renderSystemDiagram();renderTaskActivity();},1000);

requestAnimationFrame(draw);
applyTheme();void loadAutomationSettings();loadWeather(true);
renderHistory();entry('nexus','Hello. I am Nexus. Start the voice link or type a message to begin.');
</script></body></html>'''

# ---------------------------------------------------------------------------
# API routes and Gemini Live server
# ---------------------------------------------------------------------------
async def index(_request: web.Request) -> web.Response:
    return web.Response(text=HTML, content_type="text/html", headers={"Cache-Control": "no-store"})


async def automation_settings_handler(request: web.Request) -> web.Response:
    if request.method == "POST":
        try:
            payload = await request.json()
            current = _update_automation_settings(payload)
            return web.json_response({"ok": True, "settings": current}, headers={"Cache-Control": "no-store"})
        except (ValueError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400, headers={"Cache-Control": "no-store"})
    with _AUTOMATION_SETTINGS_LOCK:
        current = dict(AUTOMATION_SETTINGS)
    return web.json_response({"ok": True, "settings": current}, headers={"Cache-Control": "no-store"})


async def file_actions_handler(request: web.Request) -> web.Response:
    """Read pending destructive actions or approve/cancel one from the local UI."""
    allowed_origins = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}
    origin = request.headers.get("Origin")
    if origin and origin not in allowed_origins:
        return web.json_response({"ok": False, "error": "Use the local NEXUS page."}, status=403)
    if request.method == "GET":
        with _PENDING_FILE_ACTIONS_LOCK:
            actions = [dict(a, display=a.get("display", "")) for a in _PENDING_FILE_ACTIONS.values()]
        return web.json_response({"ok": True, "actions": actions}, headers={"Cache-Control": "no-store"})
    action_id = request.match_info.get("action_id", "")
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid JSON request."}, status=400)
    action = payload.get("action") if isinstance(payload, dict) else None
    if action == "cancel":
        with _PENDING_FILE_ACTIONS_LOCK:
            existed = _PENDING_FILE_ACTIONS.pop(action_id, None) is not None
        if not existed:
            return web.json_response({"ok": False, "error": "That action no longer exists."}, status=404)
        return web.json_response({"ok": True, "result": "Pending action cancelled."}, headers={"Cache-Control": "no-store"})
    if action != "approve":
        return web.json_response({"ok": False, "error": "Action must be approve or cancel."}, status=400)
    with _PENDING_FILE_ACTIONS_LOCK:
        pending = dict(_PENDING_FILE_ACTIONS.get(action_id) or {})
    if not pending:
        return web.json_response({"ok": False, "error": "That action no longer exists."}, status=404)
    # Enforce the irreversible-action phrase on the server too, not only in UI prompts.
    required_phrase = {"permanent_delete_file": "DELETE", "empty_recycle_bin": "EMPTY"}.get(pending.get("kind"))
    if required_phrase and payload.get("confirmation") != required_phrase:
        return web.json_response({"ok": False, "error": f"Confirmation phrase {required_phrase} is required. Nothing was changed."}, status=403)
    if pending.get("kind") not in {"permanent_delete_file", "empty_recycle_bin"}:
        return web.json_response({"ok": False, "error": "Only irreversible actions can be approved here."}, status=400)
    ok, result = await asyncio.to_thread(_execute_pending_file_action, action_id)
    return web.json_response({"ok": ok, "result": result}, status=200 if ok else 400, headers={"Cache-Control": "no-store"})


async def telemetry_handler(request: web.Request) -> web.Response:
    """Return current local CPU, memory, and aggregate network throughput."""
    try:
        import psutil
    except ImportError:
        return web.json_response({"ok": False, "error": "psutil is not installed"}, status=503,
                                 headers={"Cache-Control": "no-store"})
    try:
        now = time.monotonic()
        cpu_percent = round(psutil.cpu_percent(interval=None), 1)
        memory = psutil.virtual_memory()
        network = psutil.net_io_counters(pernic=False)
        telemetry_state = request.app.get("telemetry_state", {})
        previous = telemetry_state.get("prev")
        rx_bps = tx_bps = 0.0
        if previous:
            prev_time, prev_recv, prev_sent = previous
            elapsed = max(0.001, now - prev_time)
            rx_bps = max(0.0, (network.bytes_recv - prev_recv) / elapsed)
            tx_bps = max(0.0, (network.bytes_sent - prev_sent) / elapsed)
        telemetry_state["prev"] = (now, network.bytes_recv, network.bytes_sent)
        return web.json_response({
            "ok": True,
            "cpu_percent": cpu_percent,
            "ram_percent": round(memory.percent, 1),
            "ram_used_bytes": int(memory.used),
            "ram_total_bytes": int(memory.total),
            "net_rx_bps": round(rx_bps, 1),
            "net_tx_bps": round(tx_bps, 1),
            "sampled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"Telemetry unavailable: {str(exc)[:140]}"},
                                 status=500, headers={"Cache-Control": "no-store"})


def _save_gemini_api_key(key: str) -> None:
    """Persist a key in the current user's private config, outside the repository."""
    GEMINI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Restrict the config directory and file permissions on POSIX platforms.
    try:
        os.chmod(GEMINI_CONFIG_PATH.parent, 0o700)
    except OSError:
        pass
    fd, temp_name = tempfile.mkstemp(prefix=".gemini-", suffix=".tmp", dir=str(GEMINI_CONFIG_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"api_key": key}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        os.replace(temp_name, GEMINI_CONFIG_PATH)
        try:
            os.chmod(GEMINI_CONFIG_PATH, 0o600)
        except OSError:
            pass
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass


def _remove_saved_gemini_api_key() -> None:
    try:
        GEMINI_CONFIG_PATH.unlink(missing_ok=True)
    except OSError as exc:
        raise ValueError(f"Could not remove local Gemini key: {exc}") from exc


async def gemini_key_handler(request: web.Request) -> web.Response:
    """Local-only GUI onboarding; never return the API key itself to JavaScript."""
    global API_KEY, API_KEY_SOURCE
    allowed_origins = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}
    origin = request.headers.get("Origin")
    if origin and origin not in allowed_origins:
        return web.json_response({"ok": False, "error": "Use the local NEXUS page to configure the API key."}, status=403)

    if request.method == "GET":
        return web.json_response({
            "ok": True,
            "configured": bool(API_KEY),
            "source": API_KEY_SOURCE,
            "path": str(GEMINI_CONFIG_PATH),
            "sdk_available": GENAI_AVAILABLE,
            "client_ready": bool(request.app.get("client")),
        }, headers={"Cache-Control": "no-store"})

    if request.method == "DELETE":
        try:
            await asyncio.to_thread(_remove_saved_gemini_api_key)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)
        API_KEY = ""
        API_KEY_SOURCE = "missing"
        new_client = None
        old_client = request.app.get("client")
        if old_client and old_client is not new_client:
            request.app.setdefault("retired_clients", []).append(old_client)
        request.app["client"] = new_client
        return web.json_response({"ok": True, "configured": bool(API_KEY), "source": API_KEY_SOURCE}, headers={"Cache-Control": "no-store"})

    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid JSON request."}, status=400)
    key = payload.get("api_key") if isinstance(payload, dict) else None
    if not isinstance(key, str):
        return web.json_response({"ok": False, "error": "Enter a Gemini API key."}, status=400)
    key = key.strip()
    if len(key) < 12 or len(key) > 512 or any(ch.isspace() for ch in key):
        return web.json_response({"ok": False, "error": "The key format looks invalid. Paste the complete key without spaces."}, status=400)

    # Save first so a user can configure NEXUS while offline or before installing the SDK.
    try:
        await asyncio.to_thread(_save_gemini_api_key, key)
    except OSError as exc:
        return web.json_response({"ok": False, "error": f"Could not save the key locally: {exc}"}, status=500)

    API_KEY = key
    API_KEY_SOURCE = "local"
    new_client = None
    if GENAI_AVAILABLE:
        try:
            new_client = genai.Client(api_key=key)
        except Exception as exc:
            return web.json_response({"ok": True, "configured": True, "sdk_available": True,
                                      "client_ready": False,
                                      "message": f"Key saved locally; client initialisation failed ({type(exc).__name__})."}, headers={"Cache-Control": "no-store"})
    old_client = request.app.get("client")
    if old_client and old_client is not new_client:
        request.app.setdefault("retired_clients", []).append(old_client)
    request.app["client"] = new_client
    return web.json_response({
        "ok": True,
        "configured": True,
        "source": API_KEY_SOURCE,
        "sdk_available": GENAI_AVAILABLE,
        "client_ready": bool(new_client),
        "message": "Key saved on this computer. Start the voice link to test the connection.",
    }, headers={"Cache-Control": "no-store"})


async def profile_handler(request: web.Request) -> web.Response:
    """Local-only: read/save what NEXUS should call the user."""
    global USER_NAME
    allowed_origins = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}
    origin = request.headers.get("Origin")
    if origin and origin not in allowed_origins:
        return web.json_response({"ok": False, "error": "Use the local NEXUS page."}, status=403)
    if request.method == "GET":
        return web.json_response({"ok": True, "user_name": USER_NAME}, headers={"Cache-Control": "no-store"})
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid JSON request."}, status=400)
    try:
        name = _clean_user_name(payload.get("user_name", "") if isinstance(payload, dict) else None)
        await asyncio.to_thread(_save_user_name, name)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except OSError as exc:
        return web.json_response({"ok": False, "error": f"Could not save your name: {exc}"}, status=500)
    with _PROFILE_LOCK:
        USER_NAME = name
    return web.json_response({"ok": True, "user_name": name}, headers={"Cache-Control": "no-store"})


async def status_handler(request: web.Request) -> web.Response:
    client_ready = bool(API_KEY and GENAI_AVAILABLE and request.app.get("client"))
    if not GENAI_AVAILABLE:
        message = "Google Gen AI SDK is missing. Run: python -m pip install -U google-genai"
    elif not API_KEY:
        message = "Gemini API key is missing. Enter it in the NEXUS first-run setup screen or Settings."
    elif not client_ready:
        message = "Gemini client could not initialise. Check the SDK install and API key, then restart NEXUS."
    else:
        message = "Voice link ready."
    return web.json_response({
        "api_key_configured": bool(API_KEY),
        "sdk_available": GENAI_AVAILABLE,
        "client_ready": client_ready,
        "model": MODEL,
        "message": message,
    }, headers={"Cache-Control": "no-store"})


async def weather_handler(request: web.Request) -> web.Response:
    raw_city = (request.query.get("city") or "Lahore").strip()
    city = re.sub(r"\s+", " ", raw_city)
    if not city or len(city) > 60 or not re.fullmatch(r"[\w\s,.'-]+", city, flags=re.UNICODE):
        return web.json_response({"ok": False, "error": "Enter a valid city name."}, status=400)
    cache: dict = request.app["weather_cache"]
    cache_key = city.casefold()
    now = time.time()
    cached = cache.get(cache_key)
    if cached and now - cached[0] < 600 and request.query.get("refresh") != "1":
        return web.json_response(cached[1], headers={"Cache-Control": "no-store"})
    timeout = ClientTimeout(total=9, connect=4, sock_read=5)
    try:
        async with ClientSession(timeout=timeout) as http:
            async with http.get("https://geocoding-api.open-meteo.com/v1/search",
                                params={"name": city, "count": 1, "language": "en", "format": "json"}) as response:
                if response.status != 200:
                    raise RuntimeError(f"geocoding service returned HTTP {response.status}")
                place_data = await response.json(content_type=None)
            results = place_data.get("results") or []
            if not results:
                return web.json_response({"ok": False, "error": f"No city found for '{city}'."}, status=404)
            place = results[0]
            async with http.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": place["latitude"], "longitude": place["longitude"],
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,is_day",
                "timezone": "auto", "forecast_days": 1,
            }) as response:
                if response.status != 200:
                    raise RuntimeError(f"weather service returned HTTP {response.status}")
                weather_data = await response.json(content_type=None)
        current = weather_data.get("current") or {}
        code = int(current.get("weather_code", -1))
        desc, icon = _weather_description(code, int(current.get("is_day", 1)))
        fetched = datetime.now().astimezone().isoformat(timespec="seconds")
        result = {
            "ok": True, "city": place.get("name", city), "country": place.get("country", ""),
            "temperature": current.get("temperature_2m"), "feels_like": current.get("apparent_temperature"),
            "humidity": current.get("relative_humidity_2m"), "wind_speed": current.get("wind_speed_10m"),
            "precipitation": current.get("precipitation", 0), "condition": desc, "icon": icon,
            "fetched_at": fetched, "timezone": weather_data.get("timezone", ""),
        }
        if result["temperature"] is None:
            raise RuntimeError("weather provider returned incomplete current conditions")
        cache[cache_key] = (now, result)
        return web.json_response(result, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"Weather service unavailable: {str(exc)[:180]}"}, status=502)


def _weather_description(code: int, is_day: int) -> tuple[str, str]:
    # WMO weather interpretation codes used by Open-Meteo.
    if code == 0: return ("Clear sky", "☀" if is_day else "☾")
    if code == 1: return ("Mainly clear", "🌤" if is_day else "☾")
    if code == 2: return ("Partly cloudy", "⛅")
    if code == 3: return ("Overcast", "☁")
    if code in (45, 48): return ("Fog", "🌫")
    if code in (51, 53, 55, 56, 57): return ("Drizzle", "🌦")
    if code in (61, 63, 66): return ("Rain", "🌧")
    if code == 65 or code == 67: return ("Heavy rain", "🌧")
    if code in (71, 73, 75, 77): return ("Snow", "❄")
    if code in (80, 81, 82): return ("Rain showers", "🌦")
    if code in (85, 86): return ("Snow showers", "🌨")
    if code in (95, 96, 99): return ("Thunderstorm", "⛈")
    return ("Conditions unavailable", "◌")


def build_live_prompt() -> str:
    """Fill in how NEXUS should address the user (read fresh on every voice connection)."""
    with _PROFILE_LOCK:
        name = USER_NAME
    address = name or 'sir (or, now and then, "boss")'
    call = name or "sir"
    return LIVE_PROMPT.replace("__USER_ADDRESS__", address).replace("__USER_CALL__", call)


def make_config(voice: str) -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=build_live_prompt(),
        tools=[{"function_declarations": TOOL_DECLARATIONS}],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )


async def run_tools(session, tool_call, tell) -> None:
    responses = []
    for fc in tool_call.function_calls:
        args = dict(fc.args or {})
        safe_args = {k: (f"<{len(v)} characters>" if k in {"text", "content", "message"} and isinstance(v, str) else v)
                     for k, v in args.items()}
        await tell(type="tool", name=fc.name, args=", ".join(f"{k}={v}" for k, v in safe_args.items()))
        fn = TOOL_MAP.get(fc.name)
        try:
            if not fn:
                result = f"Unknown tool: {fc.name}"
            else:
                result = await asyncio.to_thread(fn, **args)
        except Exception as exc:
            result = f"Tool failed: {exc}"
        result_text = str(result)
        failure_words = ("failed", "couldn't", "can't", "isn't allowed", "isn't installed", "doesn't exist", "already exists", "must be", "limited to", "no text provided", "no website")
        ok = not any(word in result_text.lower() for word in failure_words)
        await tell(type="tool_result", name=fc.name, result=result_text[:220], ok=ok)
        responses.append(types.FunctionResponse(name=fc.name, id=fc.id, response={"result": result_text}))
    await session.send_tool_response(function_responses=responses)
    await tell(type="tool_done")


def friendly_error(exc: Exception) -> str:
    message = str(exc)
    low = message.lower()
    if "1011" in message or "429" in message or "quota" in low or "resource_exhausted" in low:
        return "Gemini rate limit or quota reached. Check your API plan, wait, then reconnect."
    if "api key" in low or "unauthenticated" in low or "401" in message or "403" in message:
        return "Gemini rejected the API key or access. Check the key in NEXUS Settings and confirm Gemini API access is enabled."
    if "model" in low and ("not found" in low or "unsupported" in low):
        return "Gemini Live model unavailable. Check NEXUS_GEMINI_MODEL and the current Live API model list."
    return f"Connection problem: {message[:220]}"


MAX_SCREEN_FRAME_BYTES = 500_000


def _decode_screen_frame(encoded: object) -> bytes:
    """Validate one base64-encoded JPEG frame from the local browser client."""
    if not isinstance(encoded, str) or not encoded or len(encoded) > 4 * ((MAX_SCREEN_FRAME_BYTES + 2) // 3):
        raise ValueError("Invalid or oversized screen frame.")
    try:
        frame = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("Screen frame was not valid base64.") from exc
    if not frame or len(frame) > MAX_SCREEN_FRAME_BYTES:
        raise ValueError("Screen frame is empty or oversized.")
    if not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9"):
        raise ValueError("Screen frame must be a JPEG image.")
    return frame


async def ws_handler(request: web.Request) -> web.StreamResponse:
    allowed_origins = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}
    if request.headers.get("Origin") not in allowed_origins:
        return web.Response(status=403, text="Forbidden: use the local NEXUS page.")
    if not API_KEY:
        return web.Response(status=503, text="Gemini API key is not configured. Enter it in NEXUS Settings or the first-run setup screen.")
    if not GENAI_AVAILABLE:
        return web.Response(status=503, text="Google Gen AI SDK is missing. Run: python -m pip install -U google-genai")
    if not request.app.get("client"):
        return web.Response(status=503, text="Gemini client is not ready. Check the API key in NEXUS Settings and ensure google-genai is installed.")
    voice = request.query.get("voice", DEFAULT_VOICE)
    if voice not in ALLOWED_VOICES:
        voice = DEFAULT_VOICE
    greet = request.query.get("greeting", "1") == "1"
    ws = web.WebSocketResponse(max_msg_size=1024 * 1024, heartbeat=25)
    await ws.prepare(request)

    async def tell(**message):
        if not ws.closed:
            try:
                await ws.send_str(json.dumps(message, ensure_ascii=False))
            except (ConnectionError, RuntimeError):
                pass
    try:
        client = request.app["client"]
        async with client.aio.live.connect(model=MODEL, config=make_config(voice)) as session:
            if greet:
                await session.send_realtime_input(text="Greet me in one short sentence as NEXUS and say the system is online.")

            async def from_browser():
                last_screen_frame_at = 0.0
                # Screen frames are accepted only after this browser WebSocket confirms
                # an active, user-authorized display capture. Persisted settings alone
                # never grant this session screen access.
                screen_vision_session_enabled = False
                async for msg in ws:
                    if msg.type == WSMsgType.BINARY:
                        await session.send_realtime_input(audio=types.Blob(data=msg.data, mime_type=f"audio/pcm;rate={IN_RATE}"))
                    elif msg.type == WSMsgType.TEXT:
                        try:
                            payload = json.loads(msg.data)
                            if isinstance(payload, dict) and payload.get('type') == 'text':
                                text_value = str(payload.get('text', '')).strip()
                                if not text_value:
                                    continue
                                if len(text_value) > 2000:
                                    await tell(type='error', msg='Messages are limited to 2,000 characters.')
                                    continue
                                await session.send_realtime_input(text=text_value)
                            elif isinstance(payload, dict) and payload.get('type') == 'screen_vision_state':
                                # This state message is sent only by the local UI when the
                                # user toggles sharing or the browser ends the capture.
                                requested = payload.get('enabled') is True
                                screen_vision_session_enabled = requested
                                if not requested:
                                    await session.send_realtime_input(text=(
                                        "NEXUS INTERNAL SCREEN-VISION PERMISSION UPDATE. This is a state update, "
                                        "not a user request: Screen Vision is now DISABLED and screen capture has stopped. "
                                        "You have no current access to the user's screen. Disregard all earlier screen frames "
                                        "as stale; do not describe or infer the current screen from them. Do not speak in "
                                        "response to this state update. On the user's next screen-related question, say: "
                                        "'No, I don't have access to your screen right now because Screen Vision is disabled.'"
                                    ))
                                await tell(type='screen_vision_state', enabled=screen_vision_session_enabled)
                            elif isinstance(payload, dict) and payload.get('type') == 'screen_frame':
                                with _AUTOMATION_SETTINGS_LOCK:
                                    screen_vision_allowed = bool(AUTOMATION_SETTINGS.get('screen_vision_enabled', False))
                                if not screen_vision_allowed or not screen_vision_session_enabled:
                                    continue
                                now = time.monotonic()
                                if now - last_screen_frame_at < 1.0:
                                    continue
                                try:
                                    frame_data = _decode_screen_frame(payload.get('data'))
                                except ValueError:
                                    continue
                                await session.send_realtime_input(video=types.Blob(data=frame_data, mime_type='image/jpeg'))
                                last_screen_frame_at = now
                        except (json.JSONDecodeError, TypeError, ValueError):
                            await tell(type='error', msg='Ignored malformed chat input.')
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                        break

            async def from_gemini():
                while True:
                    async for response in session.receive():
                        if response.tool_call:
                            await run_tools(session, response.tool_call, tell)
                            continue
                        content = response.server_content
                        if not content:
                            continue
                        if content.interrupted:
                            await tell(type="interrupted")
                            continue
                        if content.model_turn:
                            for part in content.model_turn.parts:
                                if part.inline_data and part.inline_data.data:
                                    # Gemini Live speech output is raw PCM16 little-endian at 24 kHz.
                                    # Do not accidentally feed other inline MIME types to the PCM player.
                                    mime = (getattr(part.inline_data, "mime_type", None) or "").lower()
                                    if (not mime or mime.startswith("audio/pcm")) and not ws.closed:
                                        await ws.send_bytes(part.inline_data.data)
                        if content.input_transcription and content.input_transcription.text:
                            await tell(type="transcript", who="you", text=content.input_transcription.text)
                        if content.output_transcription and content.output_transcription.text:
                            await tell(type="transcript", who="nexus", text=content.output_transcription.text)
                        if content.turn_complete:
                            await tell(type="turn")
                    await asyncio.sleep(0)

            async def stats():
                try:
                    import psutil
                except ImportError:
                    while not ws.closed:
                        await asyncio.sleep(30)
                else:
                    while not ws.closed:
                        try:
                            await tell(type="stats", cpu=round(psutil.cpu_percent()), ram=round(psutil.virtual_memory().percent))
                        except Exception:
                            pass
                        await asyncio.sleep(2)

            tasks = [asyncio.create_task(from_browser()), asyncio.create_task(from_gemini()), asyncio.create_task(stats())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = friendly_error(exc)
        print(f"NEXUS session error: {message}")
        await tell(type="error", msg=message)
    finally:
        await ws.close()
    return ws


async def on_cleanup(app: web.Application) -> None:
    clients = [app.get("client")] + list(app.get("retired_clients", []))
    seen = set()
    for client in clients:
        if client is None or id(client) in seen:
            continue
        seen.add(id(client))
        try:
            await client.aio.aclose()
        except Exception:
            pass


def main() -> None:
    global PORT
    if not API_KEY:
        print("NOTE: No Gemini API key configured yet. NEXUS will open the GUI setup screen and remain in preview mode until one is saved.")
    if not GENAI_AVAILABLE:
        print("NOTE: Google Gen AI SDK is missing; the GUI will still open in preview mode.")
        print("To enable voice later, run: python -m pip install -U google-genai")
        print(f"Import detail: {GENAI_IMPORT_ERROR}")
    app = web.Application(client_max_size=1024 * 1024)
    client = None
    if API_KEY and GENAI_AVAILABLE:
        try:
            client = genai.Client(api_key=API_KEY)
        except Exception as exc:
            print(f"NOTE: Gemini client could not initialise; opening preview mode: {type(exc).__name__}")
    app["client"] = client
    app["retired_clients"] = []
    app["weather_cache"] = {}
    app["telemetry_state"] = {"prev": None}
    app.add_routes([web.get("/", index), web.get("/api/key", gemini_key_handler),
                    web.post("/api/key", gemini_key_handler), web.delete("/api/key", gemini_key_handler),
                    web.get("/api/profile", profile_handler), web.post("/api/profile", profile_handler),
                    web.get("/api/status", status_handler),
                    web.get("/api/telemetry", telemetry_handler),
                    web.get("/api/settings", automation_settings_handler),
                    web.post("/api/settings", automation_settings_handler),
                    web.get("/api/file-actions", file_actions_handler),
                    web.post("/api/file-actions/{action_id}", file_actions_handler),
                    web.get("/api/weather", weather_handler), web.get("/ws", ws_handler)])
    app.on_cleanup.append(on_cleanup)
    url = f"http://localhost:{PORT}"
    print(f"NEXUS GUI running at {url}  |  Ctrl+C to quit")
    print(f"Gemini Live model: {MODEL}")
    print(f"API key file: {GEMINI_CONFIG_PATH} ({'found' if API_KEY else 'not saved yet'})")
    if os.environ.get("NEXUS_NO_BROWSER") != "1":
        threading.Timer(0.9, lambda: webbrowser.open(url, new=2)).start()
    try:
        web.run_app(app, host="127.0.0.1", port=PORT, print=None, access_log=None)
    except OSError as exc:
        print(f"Could not start the local server on port {PORT}: {exc}")
        print("Close the other NEXUS process or set NEXUS_PORT to a free port.")


if __name__ == "__main__":
    main()