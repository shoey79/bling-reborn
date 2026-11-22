"""
Blink Sync tray app.

Sits in the Windows notification area, downloads new Blink clips in the background
(using the engine in blink_sync.py), and provides sign-in (with 2FA) and settings windows.

    BlinkSync.exe [--home DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pystray
from aiohttp import ClientSession
from PIL import ImageTk

import blink_sync as core
from icons import draw_icon

APP_NAME = "Blink Sync"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "BlinkSync"
MUTEX_NAME = "BlinkSyncTrayMutex"

log = logging.getLogger("blink_sync.tray")


# =========================================================================== Windows helpers

_mutex_handle = None


def acquire_single_instance() -> bool:
    """False if another copy of the tray app is already running for this user."""
    global _mutex_handle
    if not core.IS_WINDOWS:
        return True
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() != 183   # ERROR_ALREADY_EXISTS


def enable_dpi_awareness():
    if core.IS_WINDOWS:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001 - older Windows
            pass


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else sys.executable}" "{Path(__file__).resolve()}"'


def get_start_with_windows() -> bool:
    if not core.IS_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except OSError:
        return False


def set_start_with_windows(enabled: bool):
    if not core.IS_WINDOWS:
        return
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


def open_path(path: Path):
    if core.IS_WINDOWS:
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(path)])


# =========================================================================== engine thread

class BackgroundEngine:
    """Runs core.SyncEngine on its own thread and asyncio loop."""

    def __init__(self, settings: core.Settings, on_status, on_event):
        self.settings = settings
        self._on_status = on_status
        self._on_event = on_event
        self.loop = core.new_event_loop()
        self.engine: core.SyncEngine | None = None
        self.http: ClientSession | None = None
        self._flow: core.LoginFlow | None = None
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._thread, name="sync-engine", daemon=True)

    def start(self):
        self.thread.start()
        self._ready.wait(10)

    def _thread(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception:  # noqa: BLE001
            log.exception("Sync engine stopped unexpectedly")
            self._on_status(core.Status(state="error", detail="Sync engine stopped - see the log"))
        finally:
            self._ready.set()
            self.loop.close()

    async def _main(self):
        async with ClientSession() as http:
            self.http = http
            self.engine = core.SyncEngine(self.settings, http, self._on_status, self._on_event)
            self._ready.set()
            await self.engine.run()

    def _call(self, fn):
        if self.engine and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(fn)

    def sync_now(self):
        self._call(lambda: self.engine.trigger())

    def set_paused(self, paused: bool):
        self._call(lambda: self.engine.set_paused(paused))

    def stop(self, timeout: float = 5):
        self._call(lambda: self.engine.stop())
        self.thread.join(timeout)

    def diagnose(self) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(self._diagnose(), self.loop)

    async def _diagnose(self):
        session = self.engine.session
        if session.blink is None:
            await session.start()
        return await core.write_diagnostics(session, self.settings)

    # ---- sign-in (returns concurrent futures for the UI to poll)
    def login_begin(self, email: str, password: str) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(self._login_begin(email, password), self.loop)

    def login_complete(self, code: str) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(self._login_complete(code), self.loop)

    async def _login_begin(self, email, password):
        self._flow = core.LoginFlow(self.settings, self.http)
        return self._after_login(await self._flow.begin(email, password))

    async def _login_complete(self, code):
        return self._after_login(await self._flow.complete(code))

    def _after_login(self, result):
        if result == "ok":
            self.engine.adopt_login(self._flow.blink)
            return "ok", "", list(self._flow.blink.cameras.keys())
        return result, self._flow.error, []


# =========================================================================== tray

WM_RBUTTONUP = 0x0205   # what pystray listens for to open the menu


class TrayIcon(pystray.Icon):
    """pystray icon that rebuilds its menu at the moment it is opened.

    On Windows, pystray builds the right-click menu ahead of time and only rebuilds
    it when update_menu() is called. Rebuilding from the sync thread raced with the
    tray's own thread and could leave the status line stale. Rebuilding here runs on
    the tray's own thread, just before the menu is shown, so it always reflects the
    current status.
    """

    def _on_notify(self, wparam, lparam):
        if core.IS_WINDOWS and lparam == WM_RBUTTONUP:
            try:
                self._update_menu()
            except Exception:  # noqa: BLE001
                log.exception("Couldn't rebuild the tray menu")
        return super()._on_notify(wparam, lparam)


class TrayApp:
    def __init__(self, root: tk.Tk, settings: core.Settings):
        self.root = root
        self.settings = settings
        self.status = core.Status()
        self.paused = False
        self._ui_queue: queue.Queue = queue.Queue()
        self._images = {state: draw_icon(state, 64) for state in
                        ("ok", "syncing", "starting", "login", "error", "paused")}
        self._shown_state = None       # icon image currently displayed
        self._shown_title = None       # tooltip currently displayed
        self._status_dirty = threading.Event()
        self.window_icon = ImageTk.PhotoImage(draw_icon("ok", 64))
        self.login_window: LoginWindow | None = None
        self.settings_window: SettingsWindow | None = None
        root.report_callback_exception = self._tk_error

        self.icon = TrayIcon("BlinkSync", self._images["starting"], APP_NAME, menu=self._menu())
        self.engine = BackgroundEngine(settings, self._on_status, self._on_event)
        self.engine.start()
        self.icon.run_detached()
        root.after(150, self._pump)

        if not core.load_creds(settings.creds_path).get("refresh_token"):
            self.ui(self.show_login)

    # ---- thread plumbing: pystray and the engine call in from other threads
    def ui(self, fn):
        self._ui_queue.put(fn)

    def _pump(self):
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    log.exception("UI action failed")
        except queue.Empty:
            pass
        if self._status_dirty.is_set():
            self._apply_status()
        self.root.after(150, self._pump)

    def _apply_status(self):
        """Push the current status to the tray icon and tooltip (UI thread only).

        Anything that fails stays marked as out of date and is retried on the next
        tick, so a transient Windows error can't leave a stale status on screen.
        """
        self._status_dirty.clear()
        state = self.status.state
        title = f"{APP_NAME} - {self.status_text()}"[:120]
        try:
            if state != self._shown_state:
                self.icon.icon = self._images.get(state, self._images["ok"])
                self._shown_state = state
            if title != self._shown_title:
                self.icon.title = title
                self._shown_title = title
            self.refresh_menu()
        except Exception:  # noqa: BLE001
            log.debug("Tray icon update failed; will retry", exc_info=True)
            self._status_dirty.set()

    def refresh_menu(self):
        # On Windows the menu is rebuilt each time it's opened (see TrayIcon), so
        # there's nothing to do here. Other platforms need an explicit rebuild.
        if not core.IS_WINDOWS:
            self.icon.update_menu()

    def _tk_error(self, *args):
        log.error("UI error", exc_info=args)

    def _action(self, fn):
        return lambda icon, item: self.ui(fn)

    # ---- menu
    def _menu(self):
        item = pystray.MenuItem
        return pystray.Menu(
            item(lambda i: self.status_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Open videos folder", self._action(lambda: self.open_output("SyncVideo")), default=True),
            item("Open snapshots folder", self._action(lambda: self.open_output("SyncThumbnail")),
                 visible=lambda i: self.settings.get_bool("SyncThumbnail", "Enabled")),
            item("Sync now", self._action(self.engine_sync_now),
                 enabled=lambda i: not self.paused and self.status.state != "login"),
            item("Pause syncing", self._action(self.toggle_pause), checked=lambda i: self.paused),
            pystray.Menu.SEPARATOR,
            item("Sign in to Blink…", self._action(self.show_login)),
            item("Settings…", self._action(self.show_settings)),
            item("View log", self._action(lambda: open_path(self.settings.log_path))),
            item("Save diagnostic report", self._action(self.save_diagnostics)),
            pystray.Menu.SEPARATOR,
            item("Exit", self._action(self.quit)),
        )

    def status_text(self) -> str:
        s = self.status
        if s.state == "ok":
            text = "Up to date"
            if s.last_check:
                text += f" · checked {time.strftime('%H:%M', time.localtime(s.last_check))}"
            if s.clips_this_session:
                n = s.clips_this_session
                text += f" · {n} clip{'s' if n != 1 else ''} downloaded since start"
            return text
        if s.state == "syncing":
            return s.detail or "Syncing…"
        if s.state == "login":
            return "Sign-in needed"
        if s.state == "error":
            return f"Problem: {(s.detail or 'see log')[:70]}"
        if s.state == "paused":
            return "Paused"
        return s.detail or "Starting…"

    # ---- engine callbacks (engine thread)
    def _on_status(self, status: core.Status):
        # Only record it here; the UI thread applies it (see _apply_status)
        self.status = status
        self._status_dirty.set()

    def _on_event(self, kind: str, message: str):
        if kind == "auth":
            if self.login_window is None and core.load_creds(self.settings.creds_path).get("refresh_token"):
                self.notify("Blink needs you to sign in again. Right-click the Blink Sync icon "
                            "and choose Sign in to Blink.")
        elif kind == "clips" and self.settings.get_bool("App", "NotifyNewClips"):
            self.notify(message)

    def notify(self, message: str):
        try:
            self.icon.notify(message, APP_NAME)
        except Exception:  # noqa: BLE001
            log.debug("Notification failed", exc_info=True)

    # ---- actions (UI thread)
    def engine_sync_now(self):
        self.engine.sync_now()

    def toggle_pause(self):
        self.paused = not self.paused
        self.engine.set_paused(self.paused)
        self._status_dirty.set()

    def open_output(self, section: str):
        path = Path(self.settings.get(section, "Output"))
        try:
            path.mkdir(parents=True, exist_ok=True)
            open_path(path)
        except OSError as err:
            messagebox.showerror(APP_NAME, f"Can't open {path}:\n{err}")

    def show_login(self):
        if self.login_window and self.login_window.winfo_exists():
            bring_to_front(self.login_window)
        else:
            self.login_window = LoginWindow(self)

    def show_settings(self):
        if self.settings_window and self.settings_window.winfo_exists():
            bring_to_front(self.settings_window)
        else:
            self.settings_window = SettingsWindow(self)

    def save_diagnostics(self):
        self.notify("Creating a diagnostic report. If Blink is slow to answer this can take a few minutes.")
        future = self.engine.diagnose()

        def check():
            if not future.done():
                self.root.after(300, check)
                return
            try:
                path = future.result()
            except Exception as err:  # noqa: BLE001
                log.exception("Diagnostic report failed")
                messagebox.showerror(APP_NAME, f"Couldn't create the diagnostic report:\n{err}")
                return
            self.notify(f"Diagnostic report saved: {path.name}")
            if core.IS_WINDOWS:
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                open_path(path.parent)

        check()

    def signed_in(self, cameras: list[str]):
        names = ", ".join(cameras) if cameras else "no cameras found"
        self.notify(f"Signed in to Blink. Cameras: {names}")

    def quit(self):
        log.info("Exiting")
        for w in (self.login_window, self.settings_window):
            if w and w.winfo_exists():
                w.destroy()
        self.engine.stop()
        self.icon.stop()
        self.root.after(0, self.root.destroy)


def bring_to_front(win: tk.Toplevel):
    win.deiconify()
    win.lift()
    win.attributes("-topmost", True)
    win.after(300, lambda: win.attributes("-topmost", False))
    win.focus_force()


class Dialog(tk.Toplevel):
    def __init__(self, app: TrayApp, title: str):
        super().__init__(app.root)
        self.app = app
        self.title(title)
        self.resizable(False, False)
        self.iconphoto(False, app.window_icon)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.body = ttk.Frame(self, padding=16)
        self.body.grid(sticky="nsew")

    def center(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 3
        self.geometry(f"+{x}+{y}")
        bring_to_front(self)


# =========================================================================== sign-in window

class LoginWindow(Dialog):
    def __init__(self, app: TrayApp):
        super().__init__(app, "Sign in to Blink")
        b = self.body
        self.stage = "credentials"
        ttk.Label(b, text="Sign in with your Blink account.").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(b, text="Email").grid(row=1, column=0, sticky="w", pady=3, padx=(0, 10))
        self.email = ttk.Entry(b, width=36)
        self.email.grid(row=1, column=1, pady=3)
        ttk.Label(b, text="Password").grid(row=2, column=0, sticky="w", pady=3, padx=(0, 10))
        self.password = ttk.Entry(b, width=36, show="•")
        self.password.grid(row=2, column=1, pady=3)

        self.code_label = ttk.Label(b, text="Code")
        self.code = ttk.Entry(b, width=36)
        self.code_label.grid(row=3, column=0, sticky="w", pady=3, padx=(0, 10))
        self.code.grid(row=3, column=1, pady=3)
        self.code_label.grid_remove()
        self.code.grid_remove()

        self.message = ttk.Label(b, text="", wraplength=330, justify="left")
        self.message.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(b)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.ok = ttk.Button(buttons, text="Sign in", command=self.submit, default="active")
        self.ok.pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left")
        self.bind("<Return>", lambda e: self.submit())
        self.bind("<Escape>", lambda e: self.destroy())

        saved = core.load_creds(app.settings.creds_path).get("username", "")
        self.email.insert(0, saved)
        (self.password if saved else self.email).focus_set()
        self.center()

    def say(self, text: str, error: bool = False):
        self.message.configure(text=text, foreground="#b91c1c" if error else "")

    def busy(self, busy: bool, text: str = ""):
        state = "disabled" if busy else "normal"
        for w in (self.ok, self.email, self.password, self.code):
            w.configure(state=state)
        if text:
            self.say(text)

    def submit(self):
        if str(self.ok["state"]) == "disabled":
            return
        if self.stage == "credentials":
            email, password = self.email.get().strip(), self.password.get()
            if not email or not password:
                self.say("Enter your Blink email and password.", error=True)
                return
            self.busy(True, "Signing in…")
            self._wait(self.app.engine.login_begin(email, password))
        else:
            code = self.code.get().strip()
            if not code:
                self.say("Enter the code Blink sent you.", error=True)
                return
            self.busy(True, "Checking the code…")
            self._wait(self.app.engine.login_complete(code))

    def _wait(self, future: concurrent.futures.Future):
        if not future.done():
            self.after(200, self._wait, future)
            return
        if not self.winfo_exists():
            return
        self.busy(False)
        try:
            result, error, cameras = future.result()
        except Exception as err:  # noqa: BLE001
            log.exception("Sign-in failed")
            result, error, cameras = "failed", str(err), []

        if result == "ok":
            self.app.signed_in(cameras)
            self.destroy()
        elif result == "2fa":
            self.stage = "code"
            self.email.configure(state="disabled")
            self.password.configure(state="disabled")
            self.code_label.grid()
            self.code.grid()
            self.ok.configure(text="Verify")
            self.say("Blink has sent a verification code to your email or phone. Enter it here.")
            self.code.focus_set()
        else:
            if self.stage == "code":
                self.email.configure(state="disabled")
                self.password.configure(state="disabled")
            self.say(error or "Sign-in failed.", error=True)


# =========================================================================== settings window

class SettingsWindow(Dialog):
    def __init__(self, app: TrayApp):
        super().__init__(app, f"{APP_NAME} settings")
        st = app.settings
        st.reload()
        b = self.body
        b.columnconfigure(0, weight=1)
        self.vars: dict[tuple[str, str], tk.Variable] = {}

        def check(parent, section, key, text, row):
            v = tk.BooleanVar(value=st.get_bool(section, key))
            self.vars[(section, key)] = v
            ttk.Checkbutton(parent, text=text, variable=v).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)

        def number(parent, section, key, text, row, lo, hi):
            v = tk.StringVar(value=str(st.get_int(section, key)))
            self.vars[(section, key)] = v
            ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 10))
            ttk.Spinbox(parent, from_=lo, to=hi, textvariable=v, width=8).grid(row=row, column=1, sticky="w", pady=2)

        def folder(parent, section, key, row):
            v = tk.StringVar(value=st.get(section, key))
            self.vars[(section, key)] = v
            ttk.Label(parent, text="Save to").grid(row=row, column=0, sticky="w", pady=2, padx=(0, 10))
            ttk.Entry(parent, textvariable=v, width=44).grid(row=row, column=1, sticky="we", pady=2)
            ttk.Button(parent, text="Browse…", command=lambda: self.browse(v)).grid(row=row, column=2, padx=(6, 0))

        video = ttk.LabelFrame(b, text="Video clips", padding=10)
        video.grid(row=0, column=0, sticky="we")
        check(video, "SyncVideo", "Enabled", "Download new clips", 0)
        folder(video, "SyncVideo", "Output", 1)
        number(video, "SyncVideo", "Interval", "Check every (seconds)", 2, core.MIN_VIDEO_INTERVAL, 86400)
        number(video, "SyncVideo", "Sleep", "Pause between downloads (seconds)", 3, 0, 600)
        check(video, "SyncVideo", "IncludeCameraName", "Add the camera name to file names", 4)
        check(video, "SyncVideo", "UseLocalTime", "Use local time for file and folder names (otherwise UTC)", 5)

        snaps = ttk.LabelFrame(b, text="Snapshots", padding=10)
        snaps.grid(row=1, column=0, sticky="we", pady=(10, 0))
        check(snaps, "SyncThumbnail", "Enabled",
              "Take a snapshot from every camera on a schedule (uses battery on battery cameras)", 0)
        folder(snaps, "SyncThumbnail", "Output", 1)
        number(snaps, "SyncThumbnail", "Interval", "Every (seconds)", 2, core.MIN_THUMB_INTERVAL, 86400)
        check(snaps, "SyncThumbnail", "UseLocalTime", "Use local time for file and folder names", 3)

        for frame in (video, snaps):
            frame.columnconfigure(0, minsize=270)
            frame.columnconfigure(1, weight=1)

        general = ttk.LabelFrame(b, text="General", padding=10)
        general.grid(row=2, column=0, sticky="we", pady=(10, 0))
        self.startup = tk.BooleanVar(value=get_start_with_windows())
        ttk.Checkbutton(general, text="Start Blink Sync when I sign in to Windows", variable=self.startup,
                        state="normal" if core.IS_WINDOWS else "disabled").grid(row=0, column=0, sticky="w", pady=2)
        check(general, "App", "NotifyNewClips", "Show a notification when new clips are downloaded", 1)
        check(general, "Evm", "MarkWatched", "Mark clips as watched in the Blink app after downloading them", 2)
        info = ttk.Frame(general)
        info.grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Label(info, text=f"Settings and logs: {st.home}", foreground="#6b7280").pack(side="left")
        ttk.Button(info, text="Open", command=lambda: open_path(st.home)).pack(side="left", padx=(6, 0))

        buttons = ttk.Frame(b)
        buttons.grid(row=3, column=0, sticky="e", pady=(12, 0))
        ttk.Label(buttons, text=f"Version {core.__version__}", foreground="#6b7280").pack(side="left", padx=(0, 16))
        ttk.Button(buttons, text="Save", command=self.save, default="active").pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left")
        self.bind("<Escape>", lambda e: self.destroy())
        self.center()

    def browse(self, var: tk.StringVar):
        path = filedialog.askdirectory(parent=self, initialdir=var.get() or str(Path.home()), mustexist=False)
        if path:
            var.set(str(Path(path)))

    def save(self):
        st = self.app.settings
        values = {}
        for (section, key), var in self.vars.items():
            if isinstance(var, tk.BooleanVar):
                values[(section, key)] = "true" if var.get() else "false"
            else:
                values[(section, key)] = var.get().strip()

        for (section, key) in [("SyncVideo", "Interval"), ("SyncVideo", "Sleep"), ("SyncThumbnail", "Interval")]:
            try:
                n = int(values[(section, key)])
                if n < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror(APP_NAME, "Intervals and pauses must be whole numbers of seconds.", parent=self)
                return
        for section in ("SyncVideo", "SyncThumbnail"):
            out = values[(section, "Output")]
            if values[(section, "Enabled")] == "true":
                if not out:
                    messagebox.showerror(APP_NAME, "Choose a folder to save to.", parent=self)
                    return
                try:
                    Path(out).mkdir(parents=True, exist_ok=True)
                except OSError as err:
                    messagebox.showerror(APP_NAME, f"Can't use {out}:\n{err}", parent=self)
                    return

        try:
            with st._lock:
                st.reload()   # keep the engine's latest LastUpdate
                for (section, key), value in values.items():
                    st.set(section, key, value, save=False)
                st.save()
        except OSError as err:
            log.exception("Saving settings failed")
            messagebox.showerror(APP_NAME, f"Couldn't save settings to {st.ini_path}:\n{err}", parent=self)
            return
        try:
            set_start_with_windows(self.startup.get())
        except OSError as err:
            messagebox.showwarning(APP_NAME, f"Couldn't change the startup setting:\n{err}", parent=self)
        log.info("Settings saved")
        self.app.engine.sync_now()
        self.app._status_dirty.set()
        self.destroy()


# =========================================================================== main

def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--home", type=Path, default=core.default_home())
    args, _ = parser.parse_known_args()

    settings = core.Settings(args.home, migrate=(args.home == core.default_home()))
    core.setup_logging(settings.log_path)
    core.log_startup(settings, APP_NAME)

    if not acquire_single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, "Blink Sync is already running. Look for its icon in the "
                                      "notification area (you may need to click the ^ arrow).")
        return

    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    root.title(APP_NAME)
    TrayApp(root, settings)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - windowed app: make sure failures reach the log
        log.exception("Fatal error")
        try:
            messagebox.showerror(APP_NAME, "Blink Sync hit an unexpected error. Details are in the log.")
        except Exception:  # noqa: BLE001
            pass
        raise
