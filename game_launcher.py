#!/usr/bin/env python3
"""Open the hosted Harbor Wars game in an app-style browser window."""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path


GAME_URL = "https://harborwars3d.free.nf/"
NETWORK_TIMEOUT = 8
APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HarborWarsLauncher"
PROFILE_DIR = APP_DIR / "BrowserProfile"
SETTINGS_FILE = APP_DIR / "launcher_settings.json"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "HarborWars"
LOGO_PATH = Path(__file__).resolve().parent / "assets" / "harborwars-logo.png"
ICON_PATH = Path(__file__).resolve().parent / "assets" / "harborwars.ico"
APP_USER_MODEL_ID = "HarborWars.Launcher"


def find_browser(preference: str) -> str | None:
    names = {
        "edge": ("msedge",),
        "chrome": ("chrome", "google-chrome"),
        "auto": ("msedge", "chrome", "google-chrome"),
    }[preference]
    for name in names:
        found = shutil.which(name)
        if found:
            return found

    if os.name == "nt":
        roots = [
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")),
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")),
            Path(os.environ.get("LOCALAPPDATA", "")),
        ]
        executable_names = {
            "edge": (Path("Microsoft/Edge/Application/msedge.exe"),),
            "chrome": (Path("Google/Chrome/Application/chrome.exe"),),
            "auto": (
                Path("Microsoft/Edge/Application/msedge.exe"),
                Path("Google/Chrome/Application/chrome.exe"),
            ),
        }[preference]
        for root in roots:
            for relative_path in executable_names:
                candidate = root / relative_path
                if candidate.is_file():
                    return str(candidate)
    return None


def load_download_directory() -> Path:
    """Return the user's persisted game download directory or the default."""
    try:
        settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return DEFAULT_DOWNLOAD_DIR
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read launcher settings: {error}") from error

    if not isinstance(settings, dict):
        raise RuntimeError(f"Launcher settings must be a JSON object: {SETTINGS_FILE}")
    directory = settings.get("download_directory")
    if not isinstance(directory, str) or not directory.strip():
        raise RuntimeError(f"Launcher settings have no valid download directory: {SETTINGS_FILE}")
    path = Path(directory).expanduser()
    if not path.is_absolute():
        raise RuntimeError(f"The saved download directory must be an absolute path: {path}")
    return path


def save_download_directory(directory: Path) -> Path:
    """Persist an existing folder to use for future game downloads."""
    selected_directory = directory.expanduser().resolve()
    if not selected_directory.is_dir():
        raise RuntimeError(f"The selected download folder does not exist: {selected_directory}")
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps({"download_directory": str(selected_directory)}, indent=2),
            encoding="utf-8",
        )
    except OSError as error:
        raise RuntimeError(f"Could not save launcher settings: {error}") from error
    return selected_directory


def configure_browser_profile(download_directory: Path | None = None) -> None:
    """Keep this launcher's website storage separate and enable Save As prompts."""
    selected_directory = download_directory or load_download_directory()
    default_profile = PROFILE_DIR / "Default"
    default_profile.mkdir(parents=True, exist_ok=True)
    selected_directory.mkdir(parents=True, exist_ok=True)
    preferences_file = default_profile / "Preferences"
    try:
        preferences = json.loads(preferences_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        preferences = {}
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read the app browser preferences: {error}") from error

    if not isinstance(preferences, dict):
        preferences = {}
    download = preferences.setdefault("download", {})
    if not isinstance(download, dict):
        download = {}
        preferences["download"] = download
    download["default_directory"] = str(selected_directory)
    download["prompt_for_download"] = True
    try:
        preferences_file.write_text(
            json.dumps(preferences, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as error:
        raise RuntimeError(f"Could not save app browser preferences: {error}") from error


def profile_process_ids() -> list[int]:
    """Return only Edge/Chrome processes using this launcher's isolated profile."""
    if os.name == "nt":
        if not profile_is_running():
            return []
        escaped_profile = str(PROFILE_DIR).replace("'", "''")
        command = (
            f"$profile = '{escaped_profile}'; "
            "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' OR Name='chrome.exe'\" | "
            "Where-Object { $_.Name -in @('msedge.exe','chrome.exe') "
            "-and $_.CommandLine -and $_.CommandLine.Contains($profile) } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(
                "Could not check whether the Harbor Wars app is open."
            ) from error
        if result.returncode != 0:
            raise RuntimeError("Could not check whether the Harbor Wars app is open.")
        return [int(line) for line in result.stdout.splitlines() if line.strip().isdigit()]

    if any((PROFILE_DIR / name).exists() for name in ("SingletonLock", "lockfile")):
        return [-1]
    return []


def focus_profile_window(process_ids: list[int]) -> bool:
    """Bring an existing game app window forward rather than opening a duplicate."""
    if os.name != "nt" or not process_ids:
        return False

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    enum_windows = user32.EnumWindows
    enum_windows.argtypes = [callback_type, wintypes.LPARAM]
    enum_windows.restype = wintypes.BOOL
    get_window_pid = user32.GetWindowThreadProcessId
    get_window_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    get_window_pid.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    candidates = set(process_ids)
    window = wintypes.HWND()

    @callback_type
    def visit(hwnd: int, _lparam: int) -> bool:
        nonlocal window
        pid = wintypes.DWORD()
        get_window_pid(hwnd, ctypes.byref(pid))
        if pid.value in candidates and user32.IsWindowVisible(hwnd):
            window = hwnd
            return False
        return True

    enum_windows(visit, 0)
    if not window:
        return False
    user32.ShowWindow(window, 9)
    return bool(user32.SetForegroundWindow(window))


def profile_is_running() -> bool:
    """Refuse to clear profile files while Chromium may still be using them."""
    if os.name == "nt":
        lockfile = PROFILE_DIR / "lockfile"
        if not lockfile.exists():
            return False
        try:
            with lockfile.open("r+b"):
                return False
        except PermissionError:
            return True
        except OSError:
            return True
    return bool(profile_process_ids())


def clear_app_storage() -> None:
    """Remove only the dedicated browser profile; downloaded files are preserved."""
    if profile_is_running():
        raise RuntimeError("Close the Harbor Wars app window before clearing saved data.")
    if PROFILE_DIR.exists():
        try:
            shutil.rmtree(PROFILE_DIR)
        except OSError as error:
            raise RuntimeError(
                f"Could not clear app data. Close the game browser and retry: {error}"
            ) from error


def open_downloads(download_directory: Path | None = None) -> None:
    selected_directory = download_directory or load_download_directory()
    selected_directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(selected_directory)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(selected_directory)])
    else:
        subprocess.Popen(["xdg-open", str(selected_directory)])


def create_windows_shortcuts() -> list[str]:
    """Create per-user Desktop and Start Menu shortcuts for this launcher."""
    if os.name != "nt":
        raise RuntimeError("Desktop and Start Menu shortcuts are currently supported on Windows.")

    if getattr(sys, "frozen", False):
        target = Path(sys.executable).resolve()
        arguments = ""
        working_directory = target.parent
    else:
        python_windowed = Path(sys.executable).with_name("pythonw.exe")
        target = (
            python_windowed.resolve()
            if os.name == "nt" and python_windowed.is_file()
            else Path(sys.executable).resolve()
        )
        script = Path(__file__).resolve()
        arguments = subprocess.list2cmdline([str(script)])
        working_directory = script.parent

    def encode(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    script = f"""
$ErrorActionPreference = 'Stop'
function Decode([string]$value) {{
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($value))
}}
$target = Decode '{encode(str(target))}'
$arguments = Decode '{encode(arguments)}'
$workingDirectory = Decode '{encode(str(working_directory))}'
$icon = Decode '{encode(str(ICON_PATH.resolve()))}'
$desktop = [Environment]::GetFolderPath('DesktopDirectory')
$programs = Join-Path ([Environment]::GetFolderPath('Programs')) 'Harbor Wars'
$null = New-Item -ItemType Directory -Path $programs -Force
$shell = New-Object -ComObject WScript.Shell
$shortcuts = @(
    (Join-Path $desktop 'Harbor Wars 3D.lnk'),
    (Join-Path $programs 'Harbor Wars 3D.lnk')
)
foreach ($path in $shortcuts) {{
    $shortcut = $shell.CreateShortcut($path)
    $shortcut.TargetPath = $target
    $shortcut.Arguments = $arguments
    $shortcut.WorkingDirectory = $workingDirectory
    $shortcut.IconLocation = "$icon,0"
    $shortcut.Description = 'Launch Harbor Wars 3D'
    $shortcut.Save()
    Write-Output $path
}}
"""
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded_script,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Could not create Harbor Wars shortcuts: {error}") from error
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            "Could not create Harbor Wars shortcuts."
            + (f"\n\n{details}" if details else "")
        )
    shortcuts = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(shortcuts) != 2:
        raise RuntimeError("Windows did not confirm both launcher shortcuts were created.")
    return shortcuts


class FullscreenEscapeToggle:
    """Map Escape to the browser's fullscreen toggle while the game is focused."""

    def __init__(self) -> None:
        self._process_ids: frozenset[int] = frozenset()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._startup_error: OSError | None = None
        if os.name == "nt":
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            if not self._ready.wait(timeout=5):
                raise RuntimeError("Could not initialize the fullscreen keyboard shortcut.")
            if self._startup_error is not None:
                raise RuntimeError(
                    f"Could not initialize the fullscreen keyboard shortcut: {self._startup_error}"
                ) from self._startup_error

    def set_process_ids(self, process_ids: list[int]) -> None:
        self._process_ids = frozenset(process_ids)

    def _run(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hook_id = None
        hook_thread_id = kernel32.GetCurrentThreadId()
        self._thread_id = hook_thread_id
        hook_proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            ctypes.c_int,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.CallNextHookEx.argtypes = [
            wintypes.HHOOK,
            ctypes.c_int,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL

        class KeyboardEvent(ctypes.Structure):
            _fields_ = [
                ("vk_code", wintypes.DWORD),
                ("scan_code", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("extra_info", ctypes.c_size_t),
            ]

        escape_down = False

        @hook_proc_type
        def keyboard_hook(code: int, message: int, data: int) -> int:
            nonlocal escape_down
            if code >= 0:
                event = ctypes.cast(data, ctypes.POINTER(KeyboardEvent)).contents
                if event.vk_code == 0x1B:
                    if message in (0x0100, 0x0104):
                        if not escape_down:
                            foreground = user32.GetForegroundWindow()
                            process_id = wintypes.DWORD()
                            user32.GetWindowThreadProcessId(
                                foreground,
                                ctypes.byref(process_id),
                            )
                            if process_id.value in self._process_ids:
                                escape_down = True
                                user32.keybd_event(0x7A, 0, 0, 0)
                                user32.keybd_event(0x7A, 0, 0x0002, 0)
                        if escape_down:
                            return 1
                    elif message in (0x0101, 0x0105) and escape_down:
                        escape_down = False
                        return 1
            return user32.CallNextHookEx(hook_id, code, message, data)

        try:
            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int,
                hook_proc_type,
                wintypes.HINSTANCE,
                wintypes.DWORD,
            ]
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            hook_id = user32.SetWindowsHookExW(
                13,
                keyboard_hook,
                kernel32.GetModuleHandleW(None),
                0,
            )
            if not hook_id:
                self._startup_error = ctypes.WinError()
                return
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except OSError as error:
            self._startup_error = error
        finally:
            if hook_id:
                user32.UnhookWindowsHookEx(hook_id)
            self._ready.set()

    def close(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            return
        if not ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0):
            raise ctypes.WinError()
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            raise RuntimeError("The fullscreen keyboard shortcut did not shut down cleanly.")


def check_network() -> tuple[bool, str]:
    request = urllib.request.Request(
        GAME_URL,
        headers={"User-Agent": "HarborWarsLauncher/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as response:
            if 200 <= response.status < 400:
                return True, ""
            return False, f"The game server returned HTTP {response.status}."
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        return False, str(reason)


def launch(
    browser: str | None,
    performance_mode: bool,
    existing_processes: list[int] | None = None,
    download_directory: Path | None = None,
) -> tuple[bool, list[int]]:
    if not browser:
        raise RuntimeError("Install Microsoft Edge or Google Chrome to launch the managed game app.")
    existing = existing_processes if existing_processes is not None else profile_process_ids()
    if existing:
        focus_profile_window(existing)
        return False, existing
    selected_directory = download_directory or load_download_directory()
    configure_browser_profile(selected_directory)
    options: dict[str, object] = {}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        user32 = ctypes.windll.user32
        screen_width = user32.GetSystemMetrics(0)
        screen_height = user32.GetSystemMetrics(1)
        window_width = max(1, screen_width // 2)
        window_x = (screen_width - window_width) // 2
        window_args = [
            f"--window-size={window_width},{screen_height}",
            f"--window-position={window_x},0",
        ]
    else:
        window_args = ["--window-size=960,720"]
    browser_args = [
        browser,
        f"--app={GAME_URL}",
        f"--user-data-dir={PROFILE_DIR}",
        *window_args,
        "--start-fullscreen",
    ]
    if performance_mode:
        browser_args.extend(
            [
                "--disable-frame-rate-limit",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
            ]
        )
    process = subprocess.Popen(browser_args, **options)
    return True, [process.pid]


class LauncherApp:
    def __init__(self, browser: str | None, performance_mode: bool):
        import tkinter as tk
        from tkinter import messagebox, ttk

        self.tk = tk
        self.messagebox = messagebox
        self.browser = browser
        self.download_directory = load_download_directory()
        self.download_directory_text = tk.StringVar(value=str(self.download_directory))
        if os.name == "nt":
            shell32 = ctypes.windll.shell32
            shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
            shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
            result = shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
            if result != 0:
                raise OSError(result, "Could not register the Harbor Wars app identity.")
        self.root = tk.Tk()
        self.fullscreen_toggle = FullscreenEscapeToggle()
        self.root.title("Harbor Wars Launcher")
        if ICON_PATH.is_file():
            if os.name == "nt":
                self.root.iconbitmap(str(ICON_PATH))
            elif LOGO_PATH.is_file():
                self.root.iconphoto(True, tk.PhotoImage(file=str(LOGO_PATH)))
        self.root.geometry("500x650")
        self.root.resizable(False, False)
        self.root.configure(bg="#071B32")

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("App.TFrame", background="#071B32")
        style.configure("Card.TFrame", background="#102B49")
        style.configure("Plain.TFrame", background="#071B32")
        style.configure(
            "Title.TLabel",
            background="#102B49",
            foreground="#FFFFFF",
            font=("Segoe UI", 21, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background="#102B49",
            foreground="#B8C9DC",
            font=("Segoe UI", 10),
        )
        style.configure("Card.TLabel", background="#102B49")
        style.configure(
            "Section.TLabel",
            background="#102B49",
            foreground="#FFFFFF",
            font=("Segoe UI", 11, "bold"),
        )
        style.configure(
            "Body.TLabel",
            background="#102B49",
            foreground="#D6E1EE",
            font=("Segoe UI", 9),
        )
        style.configure(
            "Muted.TLabel",
            background="#071B32",
            foreground="#A9BCD1",
            font=("Segoe UI", 9),
        )
        style.configure(
            "Status.TLabel",
            background="#102B49",
            foreground="#F5B544",
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "Primary.TButton",
            background="#F5A623",
            foreground="#10243A",
            font=("Segoe UI", 11, "bold"),
            padding=(12, 12),
            borderwidth=0,
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#FFC35A"), ("disabled", "#7B8794")],
            foreground=[("disabled", "#E1E5EA")],
        )
        style.configure(
            "Action.TButton",
            background="#1B3B5D",
            foreground="#FFFFFF",
            font=("Segoe UI", 9, "bold"),
            padding=(10, 9),
            borderwidth=0,
        )
        style.map("Action.TButton", background=[("active", "#285078")])
        style.configure(
            "Quiet.TButton",
            background="#071B32",
            foreground="#B8C9DC",
            font=("Segoe UI", 9),
            padding=(8, 6),
            borderwidth=0,
        )
        style.map("Quiet.TButton", background=[("active", "#102B49")])
        style.configure(
            "App.TCheckbutton",
            background="#071B32",
            foreground="#D6E1EE",
            font=("Segoe UI", 9),
        )
        style.map("App.TCheckbutton", background=[("active", "#071B32")])

        frame = ttk.Frame(self.root, style="App.TFrame", padding=(22, 20))
        frame.pack(fill="both", expand=True)

        header = ttk.Frame(frame, style="Card.TFrame", padding=16)
        header.pack(fill="x", pady=(0, 12))
        if LOGO_PATH.is_file():
            self.logo = tk.PhotoImage(file=str(LOGO_PATH)).subsample(6, 6)
            ttk.Label(header, image=self.logo, style="Card.TLabel").pack(
                side="left",
                padx=(0, 14),
            )
        title_block = ttk.Frame(header, style="Card.TFrame")
        title_block.pack(side="left", fill="both", expand=True)
        ttk.Label(
            title_block,
            text="Harbor Wars 3D",
            style="Title.TLabel",
        ).pack(anchor="w", pady=(4, 4))
        ttk.Label(
            title_block,
            text="Your command center is ready.",
            style="Subtitle.TLabel",
        ).pack(anchor="w")

        status_card = ttk.Frame(frame, style="Card.TFrame", padding=(15, 11))
        status_card.pack(fill="x", pady=(0, 12))
        ttk.Label(status_card, text="GAME STATUS", style="Subtitle.TLabel").pack(anchor="w")
        self.status = tk.StringVar(value="Ready when you are.")
        ttk.Label(
            status_card,
            textvariable=self.status,
            style="Status.TLabel",
            wraplength=410,
        ).pack(anchor="w", pady=(4, 0))

        ttk.Button(
            frame,
            text="PLAY HARBOR WARS",
            style="Primary.TButton",
            command=self.play,
        ).pack(fill="x", pady=(0, 6))
        ttk.Label(
            frame,
            text="Opens in a separate app window; your regular browser stays untouched.",
            style="Muted.TLabel",
            wraplength=440,
            justify="center",
        ).pack(fill="x", pady=(0, 12))

        downloads_card = ttk.Frame(frame, style="Card.TFrame", padding=(15, 12))
        downloads_card.pack(fill="x", pady=(0, 11))
        ttk.Label(downloads_card, text="YOUR DOWNLOADS", style="Section.TLabel").pack(
            anchor="w",
        )
        ttk.Label(
            downloads_card,
            textvariable=self.download_directory_text,
            style="Body.TLabel",
            wraplength=420,
            justify="left",
        ).pack(anchor="w", pady=(5, 0))
        ttk.Button(
            downloads_card,
            text="Choose download folder…",
            style="Action.TButton",
            command=self.choose_download_directory,
        ).pack(fill="x", pady=(10, 0))

        actions = ttk.Frame(frame, style="Plain.TFrame")
        actions.pack(fill="x", pady=(0, 10))
        ttk.Button(
            actions,
            text="Open downloads",
            style="Action.TButton",
            command=self.show_downloads,
        ).pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(
            actions,
            text="Clear saved data",
            style="Action.TButton",
            command=self.clear_data,
        ).pack(side="left", fill="x", expand=True, padx=(5, 0))
        ttk.Button(
            actions,
            text="Create shortcuts",
            style="Action.TButton",
            command=self.create_shortcuts,
        ).pack(side="left", fill="x", expand=True, padx=(5, 0))

        self.experimental_performance = tk.BooleanVar(value=performance_mode)
        ttk.Checkbutton(
            frame,
            text="Try experimental performance settings",
            variable=self.experimental_performance,
            style="App.TCheckbutton",
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text="May improve smoothness, but can make the browser less stable.",
            style="Muted.TLabel",
        ).pack(anchor="w", padx=(24, 0), pady=(2, 8))

        toggle_help = (
            "In game, press Esc to switch between fullscreen and half-screen."
            if os.name == "nt"
            else "In game, press F11 to toggle fullscreen."
        )
        ttk.Label(
            frame,
            text=toggle_help,
            style="Muted.TLabel",
            wraplength=440,
            justify="center",
        ).pack(fill="x", pady=(4, 2))
        ttk.Button(
            frame,
            text="Exit launcher",
            style="Quiet.TButton",
            command=self.root.destroy,
        ).pack(anchor="e", pady=(2, 0))
        self.result_queue: queue.Queue[tuple[list[int] | None, str]] = queue.Queue()
        self.checking = False
        self.game_open = False

    def choose_download_directory(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(
            parent=self.root,
            title="Choose Harbor Wars download folder",
            initialdir=str(self.download_directory),
            mustexist=True,
        )
        if not selected:
            return
        try:
            self.download_directory = save_download_directory(Path(selected))
        except RuntimeError as error:
            self.messagebox.showerror(
                "Could not save download folder",
                str(error),
                parent=self.root,
            )
            return
        self.download_directory_text.set(str(self.download_directory))
        self.status.set("Download folder saved for the next game launch.")

    def play(self) -> None:
        if self.checking:
            return
        self.checking = True
        self.status.set("Opening Harbor Wars…")

        def find_existing_game() -> None:
            try:
                self.result_queue.put((profile_process_ids(), ""))
            except RuntimeError as error:
                self.result_queue.put((None, str(error)))

        threading.Thread(target=find_existing_game, daemon=True).start()
        self.root.after(100, self.finish_launch_check)

    def finish_launch_check(self) -> None:
        try:
            processes, details = self.result_queue.get_nowait()
        except queue.Empty:
            self.root.after(100, self.finish_launch_check)
            return
        self.checking = False
        if processes is None:
            self.messagebox.showerror("Launcher error", details, parent=self.root)
            self.status.set("Could not check whether the game is already open.")
            return
        try:
            started, process_ids = launch(
                self.browser,
                self.experimental_performance.get(),
                processes,
                self.download_directory,
            )
            self.fullscreen_toggle.set_process_ids(process_ids)
            self.game_open = True
            self.status.set(
                (
                    "Game opened. Press Esc to toggle fullscreen and half-screen."
                    if os.name == "nt"
                    else "Game opened. Press F11 to toggle fullscreen."
                )
                if started
                else "Harbor Wars is already open and has been brought forward."
            )
            self.root.after(3000, self.refresh_game_status)
        except (OSError, RuntimeError) as error:
            self.status.set("Could not launch the game.")
            self.messagebox.showerror("Launcher error", str(error), parent=self.root)

    def refresh_game_status(self) -> None:
        if not self.game_open:
            return
        try:
            if not profile_is_running():
                self.game_open = False
                self.status.set("Game closed. Select Play to open it again.")
                return
        except RuntimeError:
            pass
        self.root.after(3000, self.refresh_game_status)

    def show_downloads(self) -> None:
        try:
            open_downloads(self.download_directory)
        except (OSError, RuntimeError) as error:
            self.messagebox.showerror("Downloads", str(error), parent=self.root)

    def create_shortcuts(self) -> None:
        try:
            create_windows_shortcuts()
            self.status.set("Desktop and Start Menu shortcuts are ready.")
            self.messagebox.showinfo(
                "Shortcuts created",
                "Harbor Wars 3D shortcuts were added to your Desktop and Start Menu.",
                parent=self.root,
            )
        except RuntimeError as error:
            self.messagebox.showerror("Could not create shortcuts", str(error), parent=self.root)

    def clear_data(self) -> None:
        confirmed = self.messagebox.askyesno(
            "Clear saved game data?",
            "This clears the app's saved website data, including local storage, cookies, "
            "and site settings. Downloaded files are not deleted.\n\n"
            "Close the Harbor Wars app window first. Continue?",
            parent=self.root,
        )
        if not confirmed:
            return
        try:
            clear_app_storage()
            self.status.set("Saved game data cleared. Downloads were kept.")
            self.messagebox.showinfo(
                "Saved data cleared",
                "The app's browser storage was cleared. Downloaded files were kept.",
                parent=self.root,
            )
        except RuntimeError as error:
            self.messagebox.showerror("Could not clear saved data", str(error), parent=self.root)

    def run(self) -> None:
        try:
            self.root.mainloop()
        finally:
            self.fullscreen_toggle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch hosted Harbor Wars 3D.")
    parser.add_argument(
        "--browser",
        choices=("auto", "edge", "chrome"),
        default="auto",
        help="Browser to use (default: Edge, then Chrome, then the system default).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check if the hosted game is reachable without opening it.",
    )
    parser.add_argument(
        "--performance",
        action="store_true",
        help="Opt in to experimental browser frame-rate/background flags; these may reduce stability.",
    )
    args = parser.parse_args()

    if args.check:
        connected, details = check_network()
        if connected:
            print(f"Hosted game is reachable: {GAME_URL}")
            return 0
        print(f"Network error: {details}", file=sys.stderr)
        return 1

    try:
        import tkinter
    except ImportError as error:
        print(f"Launcher error: Tkinter is required to show the launcher window: {error}", file=sys.stderr)
        return 1

    try:
        browser = find_browser(args.browser)
        if not browser:
            raise RuntimeError("Microsoft Edge or Google Chrome is required.")
        LauncherApp(browser, performance_mode=args.performance).run()
    except (ImportError, OSError, RuntimeError, tkinter.TclError) as error:
        print(f"Launcher error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
