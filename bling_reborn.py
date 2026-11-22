#!/usr/bin/env python3
"""
bling_reborn - download clips from Blink cameras (and optional snapshots) to local storage.

This module is the shared engine behind both the command line and the Bling Reborn
tray app (bling_reborn_tray.py). It replaces the sync agents in Bling
(lurume84/bling-desktop) and is built on blinkpy, so it uses Blink's current
OAuth 2.0 login.

Command line:
    python bling_reborn.py login            # interactive login (email, password, 2FA)
    python bling_reborn.py run              # run continuously
    python bling_reborn.py run --once       # one pass then exit
    python bling_reborn.py cameras          # list cameras on the account

    --home DIR   Folder holding Blink.ini, credentials.json and Logs\\
                 (default: %LOCALAPPDATA%\\BlingReborn)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import configparser
import dataclasses
import datetime as dt
import getpass
import json
import logging
import logging.handlers
import io
import urllib.parse
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import aiohttp
import yarl
from aiohttp import ClientSession
from blinkpy import api
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink

__version__ = "1.2.0"
APP_NAME = "Bling Reborn"
APP_FOLDER = "BlingReborn"

log = logging.getLogger("bling_reborn")

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

DEFAULTS = {
    "SyncVideo": {
        "Enabled": "true",
        "UseLocalTime": "false",
        "IncludeCameraName": "true",
        "Interval": "60",
        "Sleep": "20",
        "MaxPages": "100",
        "Output": "",            # filled in from the home folder at runtime
        "LastUpdate": "",
    },
    "SyncThumbnail": {
        "Enabled": "false",
        "UseLocalTime": "false",
        "Interval": "3600",
        "Sleep": "5",
        "Retries": "10",
        "Output": "",
    },
    "Evm": {
        "Enabled": "true",       # Blink's new (Ring-based) clip history, used by the app since Nov 2026
        "MarkWatched": "false",  # mark clips as watched in the Blink app once they're saved
        "BaseUrl": "https://api.blink.com/blink/",
        "PageSize": "50",
    },
    "App": {
        "NotifyNewClips": "false",
    },
}

MIN_VIDEO_INTERVAL = 30
MIN_THUMB_INTERVAL = 300
TOKEN_REFRESH_MARGIN = 300   # refresh the access token when < 5 minutes remain
AUTH_RETRY_DELAY = 300       # wait between attempts when authentication is failing
ERROR_RETRY_DELAY = 60
MAX_CLIP_FAILURES = 3        # give up on a single clip after this many failed downloads
CLIP_LIST_TIMEOUT = 90       # seconds; Blink's clip list can be slow (blinkpy only allows 10)
CLIP_LIST_ATTEMPTS = 2
CLIP_LIST_VERSIONS = (1, 2)  # Blink's v1 list froze on 19 Nov 2026 for some accounts; v2 is newer

IS_WINDOWS = sys.platform == "win32"


class AuthFailed(Exception):
    """Saved credentials can no longer be used; an interactive login is needed."""


# =========================================================================== paths & config

def documents_dir() -> Path:
    """The user's real Documents folder (follows folder redirection / OneDrive on Windows)."""
    if IS_WINDOWS:
        try:
            import ctypes
            import uuid
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            folder_id = GUID.from_buffer_copy(uuid.UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7").bytes_le)
            path = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None,
                                                          ctypes.byref(path)) == 0:
                try:
                    return Path(path.value)
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(path)
        except Exception:  # noqa: BLE001
            pass
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Documents"


def default_home() -> Path:
    """Settings, sign-in and logs. Kept in %LOCALAPPDATA% so they aren't affected by
    Documents redirection, OneDrive sync or Controlled Folder Access."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_FOLDER


def default_data_dir() -> Path:
    """Default home for downloaded clips and snapshots (Bling-style, under Documents)."""
    return documents_dir() / APP_FOLDER / "Download"


def legacy_homes() -> list[Path]:
    """Where earlier versions (when the app was called Blink Sync) kept Blink.ini and
    credentials.json: %LOCALAPPDATA%\\BlinkSync from 1.0.1 to 1.1.0, Documents\\BlinkSync in 1.0.0."""
    seen, out = set(), []
    old = "BlinkSync"
    candidates = [default_home().parent / old,
                  documents_dir() / old,
                  Path(os.environ.get("USERPROFILE") or Path.home()) / "Documents" / old]
    for p in candidates:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def replace_file(tmp: Path, target: Path, attempts: int = 20):
    """os.replace, retrying while antivirus/OneDrive/the indexer briefly hold the target open."""
    for i in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.25)


def write_text_safely(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        replace_file(tmp, path)
    except PermissionError:
        # last resort: overwrite in place (loses atomicity, but doesn't lose the change)
        path.write_text(text, encoding="utf-8")
        try:
            tmp.unlink()
        except OSError:
            pass


class Settings:
    """Blink.ini, using the same sections and keys as Bling."""

    def __init__(self, home: Path, migrate: bool = False):
        self.home = Path(home)
        self.notes: list[str] = []      # startup messages, logged once logging is set up
        self.ini_path = self.home / "Blink.ini"
        self.creds_path = self.home / "credentials.json"
        self.log_path = self.home / "Logs" / "bling_reborn.log"
        self._lock = threading.RLock()   # shared by the sync thread and the tray UI
        self.cp = configparser.ConfigParser(interpolation=None)
        self.cp.optionxform = str  # keep Bling's key capitalisation
        for section, values in DEFAULTS.items():
            self.cp[section] = dict(values)
        data = default_data_dir()
        self.cp["SyncVideo"]["Output"] = str(data / "Videos")
        self.cp["SyncThumbnail"]["Output"] = str(data / "Thumbnails")
        if migrate and not self.ini_path.exists():
            self._migrate_legacy()
        if self.ini_path.exists() and not self.reload():
            backup = self.ini_path.with_name(f"Blink.ini.unreadable-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(self.ini_path, backup)
            self.notes.append(f"Blink.ini couldn't be read; kept a copy as {backup.name} and reset it")
        self.save()   # write any missing keys so the file documents every option

    def _migrate_legacy(self):
        """Bring Blink.ini and the sign-in over from an earlier Blink Sync install (the newest one found)."""
        found = [d for d in legacy_homes() if (d / "Blink.ini").exists()]
        if not found:
            return
        src = max(found, key=lambda d: (d / "Blink.ini").stat().st_mtime)
        self.home.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / "Blink.ini", self.ini_path)
        creds = [d / "credentials.json" for d in [src] + found if (d / "credentials.json").exists()]
        if creds:
            shutil.copy2(creds[0], self.creds_path)
        self.notes.append(f"Moved settings{' and sign-in' if creds else ''} from {src} to {self.home}")

    def reload(self) -> bool:
        """Pick up edits made to Blink.ini while running. False if the file couldn't be parsed."""
        with self._lock:
            if not self.ini_path.exists():
                return True
            try:
                fresh = configparser.ConfigParser(interpolation=None)
                fresh.optionxform = str
                fresh.read(self.ini_path, encoding="utf-8-sig")   # tolerate Notepad's BOM
            except (configparser.Error, OSError, UnicodeDecodeError) as err:
                log.error("Could not read %s: %s", self.ini_path, err)
                return False
            for section in fresh.sections():          # only apply a file that parsed cleanly
                if not self.cp.has_section(section):
                    self.cp.add_section(section)
                for key, value in fresh[section].items():
                    self.cp[section][key] = value
            return True

    def get(self, section, key) -> str:
        with self._lock:
            return self.cp[section].get(key, DEFAULTS[section].get(key, ""))

    def get_bool(self, section, key) -> bool:
        with self._lock:
            try:
                return self.cp[section].getboolean(key, fallback=False)
            except ValueError:
                return DEFAULTS[section][key] == "true"

    def get_int(self, section, key) -> int:
        with self._lock:
            try:
                return self.cp[section].getint(key, fallback=int(DEFAULTS[section][key]))
            except ValueError:
                return int(DEFAULTS[section][key])

    def set(self, section, key, value: str, save: bool = True):
        with self._lock:
            self.cp[section][key] = str(value)
            if save:
                self.save()

    def save(self):
        with self._lock:
            buf = io.StringIO()
            self.cp.write(buf, space_around_delimiters=False)
            write_text_safely(self.ini_path, buf.getvalue())


def setup_logging(log_path: Path, verbose: bool = False):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")]
    if sys.stdout is not None:            # pythonw / windowed exe has no console
        handlers.append(logging.StreamHandler(sys.stdout))
    root = logging.getLogger()
    for h in handlers:
        h.setFormatter(fmt)
    root.handlers[:] = handlers
    root.setLevel(logging.INFO)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("blinkpy").setLevel(logging.INFO if verbose else logging.WARNING)


# =========================================================================== credentials

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def _dpapi(data: bytes, protect: bool) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DataBlob()
        if protect:
            ok = _crypt32.CryptProtectData(ctypes.byref(blob_in), APP_FOLDER, None, None, None,
                                           _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        else:
            ok = _crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                             _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        if not ok:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def load_creds(path: Path) -> dict:
    """Read saved tokens. On Windows they are encrypted with DPAPI for the current user."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") == "dpapi":
            if not IS_WINDOWS:
                log.error("%s was encrypted on Windows and can't be read here", path)
                return {}
            data = json.loads(_dpapi(base64.b64decode(data["data"]), protect=False))
        return data
    except Exception as err:  # noqa: BLE001
        log.warning("Could not read %s: %s", path, err)
        return {}


def save_creds(path: Path, auth: Auth):
    data = dict(auth.login_attributes)
    data.pop("password", None)            # never persist the password; the refresh token is enough
    data.pop("2fa_code", None)
    payload = json.dumps(data, indent=2)
    if IS_WINDOWS:
        payload = json.dumps({"format": "dpapi",
                              "data": base64.b64encode(_dpapi(payload.encode(), True)).decode()})
    write_text_safely(path, payload)


class Session:
    """Owns the Blink object and keeps its OAuth tokens fresh and saved."""

    def __init__(self, settings: Settings, http: ClientSession):
        self.settings = settings
        self.http = http
        self.blink: Blink | None = None

    async def start(self):
        creds = load_creds(self.settings.creds_path)
        if not creds.get("refresh_token"):
            raise AuthFailed("Not signed in to Blink yet")
        blink = Blink(session=self.http)
        blink.auth = Auth(creds, no_prompt=True, session=self.http)
        try:
            ok = await blink.start()   # uses the saved refresh token (OAuth v2)
        except BlinkTwoFARequiredError:
            ok = False
        if not ok or not blink.auth.token:
            raise AuthFailed("Blink rejected the saved sign-in; please sign in again")
        save_creds(self.settings.creds_path, blink.auth)   # refresh tokens rotate; save the new one
        self.adopt(blink)

    def adopt(self, blink: Blink):
        self.blink = blink
        log.info("Signed in to Blink (%s), %d camera(s): %s", blink.auth.host,
                 len(blink.cameras), ", ".join(blink.cameras.keys()) or "none")

    async def ensure_token(self):
        """Refresh the access token before it expires (Blink tokens last ~1 hour)."""
        auth = self.blink.auth
        if auth.expiration_date and auth.expiration_date - time.time() > TOKEN_REFRESH_MARGIN:
            return
        log.debug("Refreshing access token")
        try:
            data = await api.oauth_refresh_token(auth, auth.refresh_token, auth.hardware_id)
        except Exception as err:  # noqa: BLE001 - network error, not a rejected token
            raise RuntimeError(f"Token refresh request failed: {err}") from err
        if not data or not data.get("access_token"):
            raise AuthFailed("Blink rejected the token refresh; please sign in again")
        auth.token = data["access_token"]
        auth.refresh_token = data.get("refresh_token", auth.refresh_token)
        auth.expires_in = data.get("expires_in", 3600)
        auth.expiration_date = time.time() + auth.expires_in
        save_creds(self.settings.creds_path, auth)


class LoginFlow:
    """Interactive sign-in, split in two steps so a GUI can ask for the 2FA code."""

    def __init__(self, settings: Settings, http: ClientSession):
        self.settings = settings
        self.http = http
        self.blink: Blink | None = None
        self.error = ""

    async def begin(self, email: str, password: str) -> str:
        """Returns 'ok', '2fa' (call complete() with the code) or 'failed' (see .error)."""
        old = load_creds(self.settings.creds_path)
        data = {"username": email.strip(), "password": password}
        if old.get("hardware_id"):
            data["hardware_id"] = old["hardware_id"]   # reuse device identity
        self.blink = Blink(session=self.http)
        self.blink.auth = Auth(data, no_prompt=True, session=self.http)
        try:
            ok = await self.blink.start()
        except BlinkTwoFARequiredError:
            return "2fa"
        except Exception as err:  # noqa: BLE001
            log.exception("Sign-in failed")
            self.error = f"Couldn't reach Blink: {err}"
            return "failed"
        return self._finish(ok, "Blink didn't accept that email and password.")

    async def complete(self, code: str) -> str:
        try:
            ok = await self.blink.send_2fa_code(code.strip())
        except Exception as err:  # noqa: BLE001
            log.exception("2FA verification failed")
            self.error = f"Couldn't reach Blink: {err}"
            return "failed"
        return self._finish(ok, "Blink didn't accept that code. Check it and try again.")

    def _finish(self, ok: bool, message: str) -> str:
        if ok and self.blink.auth.token:
            save_creds(self.settings.creds_path, self.blink.auth)
            return "ok"
        self.error = message
        return "failed"


# =========================================================================== helpers

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str) -> str:
    return INVALID_CHARS.sub("_", name).strip(" .") or "camera"


def parse_ts(value: str) -> dt.datetime | None:
    try:
        ts = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def target_path(root: Path, ts: dt.datetime, local: bool, camera: str | None, ext: str) -> Path:
    """Bling layout: <Output>\\YYYY\\MonthName\\DD\\<timestamp with : as _>[ - Camera].ext"""
    ts = ts.astimezone() if local else ts.astimezone(dt.timezone.utc)
    folder = root / f"{ts.year:04d}" / MONTHS[ts.month - 1] / f"{ts.day:02d}"
    stem = ts.isoformat(timespec="seconds").replace(":", "_")
    if camera:
        stem += f" - {safe_name(camera)}"
    return folder / f"{stem}{ext}"


async def write_response(response, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    body = await response.read()
    with open(tmp, "wb") as fh:
        fh.write(body)
    replace_file(tmp, target)
    return len(body)


def new_event_loop() -> asyncio.AbstractEventLoop:
    # aiohttp is happiest on the selector loop on Windows
    return asyncio.SelectorEventLoop() if IS_WINDOWS else asyncio.new_event_loop()


# =========================================================================== clip listing

def clip_time(c: dict) -> str:
    return c.get("created_at") or c.get("time") or c.get("timestamp") or ""


def clip_url(c: dict) -> str:
    for key in ("media", "media_url", "video", "clip", "url"):
        v = c.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def clip_key(c: dict) -> str:
    return str(c.get("id") or clip_url(c) or json.dumps(c, sort_keys=True))


def media_list(resp) -> tuple[list | None, str]:
    """Pull the clip list out of a media/changed response. Returns (clips or None, problem)."""
    if not isinstance(resp, dict):
        return None, f"unexpected response type {type(resp).__name__}"
    for key in ("media", "clips", "videos", "items"):
        if isinstance(resp.get(key), list):
            return resp[key], ""
    keys = ", ".join(sorted(resp.keys()))[:200]
    msg = resp.get("message") or resp.get("error") or ""
    return None, f"no clip list in response (keys: {keys}){' - ' + str(msg)[:200] if msg else ''}"


class ClipListError(Exception):
    """The clip list request failed; the message says how (timeout, DNS, HTTP status...)."""


async def fetch_clip_page(session: "Session", since: float, page: int,
                          timeout: float = CLIP_LIST_TIMEOUT,
                          attempts: int = CLIP_LIST_ATTEMPTS, version: int = 1) -> tuple[dict, dict]:
    """GET media/changed directly (not via blinkpy) so we control the timeout and can
    report exactly what went wrong. Returns (json, info) or raises ClipListError."""
    blink = session.blink
    url = (f"{blink.urls.base_url}/api/v{version}/accounts/{blink.account_id}"
           f"/media/changed?since={urllib.parse.quote(util_stamp(since), safe=':')}&page={page}")
    info = {"url": url.split("/api/", 1)[-1], "page": page, "version": version}
    last_err = None
    for attempt in range(1, attempts + 1):
        await session.ensure_token()
        start = time.monotonic()
        try:
            async with session.http.get(yarl.URL(url, encoded=True), headers=blink.auth.header,
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                body = await resp.read()
                info.update(status=resp.status, seconds=round(time.monotonic() - start, 1),
                            bytes=len(body), attempt=attempt)
                if resp.status == 401:
                    raise AuthFailed("Blink rejected the access token for the clip list")
                if resp.status >= 400:
                    raise ClipListError(f"HTTP {resp.status} {resp.reason}: {body[:300].decode(errors='replace')}")
                try:
                    return json.loads(body), info
                except ValueError:
                    raise ClipListError(f"HTTP {resp.status} but not JSON: {body[:300].decode(errors='replace')}")
        except asyncio.TimeoutError:
            last_err = f"no reply within {timeout:.0f}s"
        except aiohttp.ClientConnectorError as err:
            last_err = f"can't connect to {blink.urls.base_url}: {err.os_error or err}"
        except aiohttp.ClientError as err:
            last_err = f"{type(err).__name__}: {err}"
        info.update(seconds=round(time.monotonic() - start, 1), attempt=attempt, error=last_err)
        log.warning("Clip list page %d attempt %d failed after %.1fs: %s",
                    page, attempt, time.monotonic() - start, last_err)
    raise ClipListError(last_err or "unknown error")


def util_stamp(epoch: float) -> str:
    # same format blinkpy sends: 2026-11-19T19:36:32+0000
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


# --------------------------------------------------------------------------- EVM history
# From Blink Android app 62.0: clips now live in Ring's event-video service ("EVM"),
# reached through Blink's API gateway rather than rest-<tier>.immedia-semi.com.

class EvmError(Exception):
    pass


def evm_headers(session: "Session") -> dict:
    return {"Authorization": f"Bearer {session.blink.auth.token}",
            "X-Org-Id": "Blink",
            "x-api-no-file-concat": "true",
            "Accept": "application/json"}


async def evm_request(session: "Session", method: str, path: str, params: dict | None = None,
                      body: dict | None = None, timeout: float = 60) -> tuple[int, object, float]:
    settings = session.settings
    base = settings.get("Evm", "BaseUrl").rstrip("/") + "/"
    await session.ensure_token()
    start = time.monotonic()
    try:
        async with session.http.request(method, base + path, params=params, json=body,
                                        headers=evm_headers(session),
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            raw = await resp.read()
            secs = round(time.monotonic() - start, 1)
            try:
                data = json.loads(raw) if raw else None
            except ValueError:
                data = raw[:300].decode(errors="replace")
            if resp.status == 401:
                raise AuthFailed("Blink's clip history rejected the access token")
            return resp.status, data, secs
    except asyncio.TimeoutError:
        raise EvmError(f"no reply within {timeout:.0f}s from {base}{path}")
    except aiohttp.ClientError as err:
        raise EvmError(f"{type(err).__name__}: {err}")


def evm_video(item: dict) -> dict | None:
    """The cloud VIDEO entry of an EVM history item, if any."""
    vis = (item.get("visualizations") or {}).get("cloud_media_visualization") or {}
    for m in vis.get("media") or []:
        if (m.get("file_family") or m.get("file_type") or "").upper() == "VIDEO" and m.get("url"):
            return m
    return None


def evm_to_clip(item: dict, names: dict) -> dict | None:
    """Turn an EVM history item into the clip shape the downloader uses."""
    video = evm_video(item)
    if not video:
        return None
    device = item.get("device") or {}
    source = str(item.get("source_id") or device.get("id") or "")
    name = names.get(source) or device.get("description") or (f"Camera {source}" if source else "Unknown")
    return {"id": f"evm:{item.get('event_id')}", "event_id": item.get("event_id"),
            "created_at": item.get("start_time") or video.get("start_time") or "",
            "device_name": name, "media": video["url"], "evm": True,
            "e2ee": bool(video.get("is_e2ee")), "watched": item.get("is_watched"),
            "deleted": bool(item.get("delete_in_progress"))}


async def list_evm_clips(session: "Session", since: float, max_pages: int) -> tuple[list, dict]:
    st = session.settings
    info = {"fetched": 0, "problem": "", "seconds": 0.0}
    names = {}
    for name, cam in session.blink.cameras.items():   # in case EVM source ids match Blink camera ids
        if getattr(cam, "camera_id", None):
            names.setdefault(str(cam.camera_id), name)
    start = dt.datetime.fromtimestamp(max(since - 60, 0), dt.timezone.utc)
    if since <= 0:
        start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60)
    params = {"start_time": start.isoformat(timespec="seconds").replace("+00:00", "Z"),
              "end_time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
              "limit": str(max(st.get_int("Evm", "PageSize"), 1))}
    clips, key = [], None
    for _ in range(max_pages):
        if key:
            params["pagination_key"] = key
        try:
            status, data, secs = await evm_request(session, "GET", "evm/v2/history/devices", params=params)
        except EvmError as err:
            info["problem"] = str(err)
            break
        info["fetched"] += 1
        info["seconds"] += secs
        if status >= 400 or not isinstance(data, dict):
            info["problem"] = f"HTTP {status}: {str(data)[:300]}"
            break
        for item in data.get("items") or []:
            clip = evm_to_clip(item, names)
            if clip:
                clips.append(clip)
        key = data.get("pagination_key")
        if not key or not data.get("items"):
            break
    return clips, info


async def evm_mark_watched(session: "Session", event_ids: list[str]) -> bool:
    if not event_ids:
        return True
    try:
        status, data, _ = await evm_request(session, "POST", "evm/v2/events/watch",
                                            body={"event_ids": event_ids})
    except EvmError as err:
        log.warning("Couldn't mark %d clip(s) as watched: %s", len(event_ids), err)
        return False
    if status >= 400:
        log.warning("Couldn't mark %d clip(s) as watched: HTTP %s %s", len(event_ids), status, str(data)[:200])
        return False
    log.info("Marked %d clip(s) as watched in the Blink app", len(event_ids))
    return True


async def evm_fresh_url(session: "Session", event_id: str) -> str | None:
    try:
        status, data, _ = await evm_request(session, "GET", f"evm/v2/history/events/{event_id}")
    except EvmError as err:
        log.warning("Couldn't refresh the link for clip %s: %s", event_id, err)
        return None
    if status >= 400 or not isinstance(data, dict):
        return None
    video = evm_video(data)
    return video.get("url") if video else None


async def download_presigned(session: "Session", url: str, timeout: float = 300):
    """EVM clip URLs are pre-signed; send no Blink auth header (it would clash with the signature)."""
    return await session.http.get(url, timeout=aiohttp.ClientTimeout(total=timeout))


async def list_clips(session: "Session", since: float, max_pages: int) -> tuple[list, dict]:
    """All clips changed since `since`, from every clip source Blink offers.

    Blink's v1 list stopped receiving new clips on 19 Nov 2026 for some accounts; the
    app now reads Ring's EVM history instead. v1, v2 and EVM are all read and merged
    (de-duplicated by clip id). A source that fails is skipped while another works.
    """
    seen: set = set()
    clips: list = []
    info = {"fetched": 0, "problem": "", "by_version": {}}
    problems = []
    sources = len(CLIP_LIST_VERSIONS) + (1 if session.settings.get_bool("Evm", "Enabled") else 0)
    if session.settings.get_bool("Evm", "Enabled"):
        got, e_info = await list_evm_clips(session, since, max_pages)
        info["fetched"] += e_info["fetched"]
        info["by_version"]["evm"] = len(got)
        if e_info["problem"]:
            problems.append(f"evm {e_info['problem']}")
            log.warning("Couldn't read Blink's clip history (EVM): %s", e_info["problem"])
        for c in got:
            seen.add(clip_key(c))
        clips.extend(got)
    for version in CLIP_LIST_VERSIONS:
        got, v_info = await _list_clips_version(session, since, max_pages, version, seen)
        info["fetched"] += v_info["fetched"]
        info["by_version"][version] = len(got)
        if v_info["problem"]:
            problems.append(f"v{version} {v_info['problem']}")
        clips.extend(got)
    evm_keys = {(c["device_name"], round(parse_ts(c["created_at"]).timestamp()))
                for c in clips if c.get("evm") and parse_ts(c.get("created_at", ""))}
    if evm_keys:
        def dup(c):
            ts = parse_ts(clip_time(c))
            return (not c.get("evm") and ts is not None and
                    any((c.get("device_name"), round(ts.timestamp()) + d) in evm_keys for d in range(-3, 4)))
        clips = [c for c in clips if not dup(c)]
    if problems and len(problems) == sources:
        info["problem"] = "; ".join(problems)          # every source failed
    elif problems:
        log.debug("Clip list partly failed: %s", "; ".join(problems))
    if len(info["by_version"]) > 1 and any(info["by_version"].values()):
        log.debug("Clips by list version: %s", info["by_version"])
    return clips, info


async def _list_clips_version(session, since, max_pages, version, seen) -> tuple[list, dict]:
    """One list version, across pages. Starts at page 0 and de-duplicates, so it works
    whether Blink numbers pages from 0 or 1, and stops when a page adds nothing new."""
    clips = []
    info = {"fetched": 0, "problem": ""}
    for page in range(0, max_pages + 1):
        try:
            resp, req = await fetch_clip_page(session, since, page, version=version)
        except ClipListError as err:
            info["problem"] = f"page {page}: {err}"
            # v1 is the long-standing list; only shout about it, v2 failures are expected on some accounts
            (log.error if version == 1 else log.debug)(
                "Couldn't get the v%d clip list from Blink (page %d): %s", version, page, err)
            break
        info["fetched"] += 1
        if req.get("seconds", 0) > 10:
            log.info("Blink took %.0fs to return v%d clip list page %d", req["seconds"], version, page)
        items, problem = media_list(resp)
        if items is None:
            info["problem"] = f"page {page}: {problem}"
            (log.warning if version == 1 else log.debug)(
                "v%d clip list request failed on page %d: %s", version, page, problem)
            break
        new = [c for c in items if isinstance(c, dict) and clip_key(c) not in seen]
        for c in new:
            seen.add(clip_key(c))
        clips.extend(new)
        if not items:
            break
        # Page 1 may repeat page 0 (1-based paging), so only stop on an all-repeat page
        # after that; it also stops a server that ignores the page number from looping.
        if not new and page != 1:
            break
    return clips, info


def _find_lists(obj, path="", out=None, depth=0):
    """Locate lists of dicts anywhere in a JSON response (where clips might live)."""
    out = [] if out is None else out
    if depth > 4:
        return out
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj[:5]):
        out.append((path or "<root>", obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _find_lists(v, f"{path}.{k}" if path else k, out, depth + 1)
    return out


def _newest_time(items: list) -> str:
    times = [t for t in (clip_time(x) or x.get("updated_at", "") for x in items) if isinstance(t, str) and t]
    return max(times) if times else ""


async def probe_endpoint(session: "Session", path: str, timeout: float = 45) -> dict:
    """One read-only GET; summarise status, timing and any clip-like lists."""
    blink = session.blink
    url = f"{blink.urls.base_url}{path}"
    result: dict = {"path": path}
    await session.ensure_token()
    start = time.monotonic()
    try:
        async with session.http.get(yarl.URL(url, encoded=True), headers=blink.auth.header,
                                    timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            body = await resp.read()
            result.update(status=resp.status, seconds=round(time.monotonic() - start, 1), bytes=len(body))
            try:
                data = json.loads(body)
            except ValueError:
                result["body"] = body[:300].decode(errors="replace")
                return result
    except asyncio.TimeoutError:
        result.update(error=f"no reply within {timeout:.0f}s", seconds=round(time.monotonic() - start, 1))
        return result
    except aiohttp.ClientError as err:
        result.update(error=f"{type(err).__name__}: {err}")
        return result
    if isinstance(data, dict):
        result["keys"] = sorted(data.keys())[:40]
        if resp.status >= 400:
            result["body"] = json.dumps(data)[:400]
    lists = _find_lists(data)
    result["lists"] = [{"at": at, "count": len(items), "newest": _newest_time(items),
                        "item_keys": sorted(items[0].keys())[:40], "example": items[0]}
                       for at, items in lists[:4]]
    return result


def endpoint_probes(blink, since: float) -> list[str]:
    a = blink.account_id
    st = urllib.parse.quote(util_stamp(since), safe=":")
    paths = [
        f"/api/v1/accounts/{a}/media/changed?since={st}&page=1",
        f"/api/v1/accounts/{a}/media/changed?page=1",
        f"/api/v1/accounts/{a}/media/count",
        f"/api/v2/accounts/{a}/media/changed?since={st}&page=1",
        f"/api/v3/accounts/{a}/media/changed?since={st}&page=1",
        f"/api/v4/accounts/{a}/media/changed?since={st}&page=1",
        f"/api/v1/accounts/{a}/media?since={st}&page=1",
        f"/api/v2/videos/changed?since={st}&page=1",
        f"/api/v3/accounts/{a}/homescreen",
        # v2 replies carry an "event_purge_id", so clips may now be tracked as events
        f"/api/v1/accounts/{a}/events/changed?since={st}&page=1",
        f"/api/v2/accounts/{a}/events/changed?since={st}&page=1",
        f"/api/v3/accounts/{a}/events/changed?since={st}&page=1",
        f"/api/v2/accounts/{a}/events?since={st}&page=1",
        f"/api/v3/accounts/{a}/events?since={st}&page=1",
        f"/api/v2/accounts/{a}/media/events?since={st}&page=1",
        f"/api/v3/accounts/{a}/media/changed?since={st}&page=1&type=video",
        f"/api/v2/accounts/{a}/media/changed?since={st}&page=1&type=video",
        f"/api/v2/accounts/{a}/media/changed?since={st}&page=1&include_events=true",
    ]
    seen_networks = set()
    for cam in list(blink.cameras.values())[:6]:
        n, c = getattr(cam, "network_id", None), getattr(cam, "camera_id", None)
        if not (n and c):
            continue
        paths += [f"/api/v1/accounts/{a}/networks/{n}/cameras/{c}/videos",
                  f"/api/v1/accounts/{a}/networks/{n}/camera/{c}/videos"]
        if n not in seen_networks:
            seen_networks.add(n)
            paths.append(f"/api/v1/accounts/{a}/networks/{n}/media/changed?since={st}&page=1")
    return paths


async def write_diagnostics(session: "Session", settings: "Settings") -> Path:
    """Save raw Blink responses that show what the clip list looks like right now."""
    blink = session.blink
    report: dict = {
        "version": __version__,
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "host": getattr(blink.auth, "host", None),
        "settings": {k: settings.get("SyncVideo", k) for k in
                     ("Enabled", "LastUpdate", "MaxPages", "Interval", "Output")},
        "cameras": {name: {"type": getattr(c, "camera_type", None) or getattr(c, "product_type", None),
                           "network": getattr(c, "network_id", None)}
                    for name, c in blink.cameras.items()},
        "requests": [],
    }
    last = parse_ts(settings.get("SyncVideo", "LastUpdate"))
    day_ago = time.time() - 86400
    probes = [("since LastUpdate", last.timestamp() if last else 0),
              ("last 24 hours", day_ago),
              ("since beginning", 0)]
    # can the PC resolve and reach Blink at all?
    try:
        host = urllib.parse.urlsplit(blink.urls.base_url).hostname
        loop = asyncio.get_running_loop()
        addrs = await loop.getaddrinfo(host, 443)
        report["dns"] = {host: sorted({a[4][0] for a in addrs})}
    except Exception as err:  # noqa: BLE001
        report["dns"] = {"error": f"{type(err).__name__}: {err}"}
    for version, (label, since) in [(v, p) for v in CLIP_LIST_VERSIONS for p in probes]:
        for page in (0, 1, 2):
            entry = {"label": f"v{version} {label}", "since": util_time(since), "page": page}
            prev = report["requests"][-1] if report["requests"] else {}
            if page > 0 and (prev.get("error") or prev.get("skipped")):
                entry["skipped"] = "previous page failed"
                report["requests"].append(entry)
                continue
            try:
                try:
                    resp, req = await fetch_clip_page(session, since, page, attempts=1, version=version)
                    entry["request"] = req
                except ClipListError as err:
                    entry["error"] = str(err)
                    report["requests"].append(entry)
                    continue
                items, problem = media_list(resp)
                entry["problem"] = problem
                entry["count"] = None if items is None else len(items)
                if isinstance(resp, dict):
                    entry["response_keys"] = sorted(resp.keys())
                    trimmed = dict(resp)
                    if items is not None:
                        key = next(k for k in ("media", "clips", "videos", "items") if resp.get(k) is items)
                        trimmed[key] = items[:3]          # a few full examples are enough
                        entry["first_last_times"] = [clip_time(items[0]), clip_time(items[-1])] if items else []
                        entry["newest"] = max((clip_time(c) for c in items), default="")
                    entry["response"] = trimmed
                else:
                    entry["response"] = repr(resp)[:2000]
            except Exception as err:  # noqa: BLE001
                entry["error"] = f"{type(err).__name__}: {err}"
            report["requests"].append(entry)

    # other places Blink might now list clips (read-only GETs)
    # subscription / entitlement details (grandfathered accounts may be treated differently)
    try:
        await session.ensure_token()
        async with session.http.get(
                yarl.URL(f"{blink.urls.base_url}/api/v3/accounts/{blink.account_id}/homescreen", encoded=True),
                headers=blink.auth.header, timeout=aiohttp.ClientTimeout(total=45)) as resp:
            data = json.loads(await resp.read())
        report["account_details"] = {k: data.get(k) for k in
                                     ("subscriptions", "entitlements", "video_stats", "device_limits")}
    except Exception as err:  # noqa: BLE001
        report["account_details"] = {"error": f"{type(err).__name__}: {err}"}

    report["clip_list_summary"] = [
        f"{r['label']} page {r['page']}: " + (r.get("error") or r.get("skipped") or
                                              f"{r.get('count')} clips, newest {r.get('newest') or '-'}")
        for r in report["requests"]]
    # Blink's new clip history (EVM, via the API gateway) - what the app uses since Nov 2026
    report["evm"] = {"base_url": settings.get("Evm", "BaseUrl"), "requests": []}

    def _scrub(u):
        return u.split("?", 1)[0] + ("?<signature removed>" if "?" in u else "") if isinstance(u, str) else u

    def _sample(item):
        item = json.loads(json.dumps(item))
        vis = (item.get("visualizations") or {}).get("cloud_media_visualization") or {}
        for m in vis.get("media") or []:
            m["url"] = _scrub(m.get("url"))
        return item

    now = dt.datetime.now(dt.timezone.utc)
    windows = [("since LastUpdate", (last or now - dt.timedelta(days=1))), ("last 24 hours", now - dt.timedelta(days=1)),
               ("last 7 days", now - dt.timedelta(days=7))]
    cam_ids = ",".join(str(c.camera_id) for c in blink.cameras.values() if getattr(c, "camera_id", None))
    variants = [(lbl, {"start_time": t.isoformat(timespec="seconds").replace("+00:00", "Z"),
                       "end_time": now.isoformat(timespec="seconds").replace("+00:00", "Z"), "limit": "50"})
                for lbl, t in windows]
    if cam_ids:
        variants.append(("last 24 hours, source_ids = Blink camera ids",
                         dict(variants[1][1], source_ids=cam_ids)))
    for label, params in variants:
        entry = {"label": label, "params": params}
        try:
            status, data, secs = await evm_request(session, "GET", "evm/v2/history/devices", params=params)
            entry.update(status=status, seconds=secs)
            if isinstance(data, dict):
                items = data.get("items") or []
                entry.update(keys=sorted(data.keys()), count=len(items),
                             with_video=sum(1 for i in items if evm_video(i)),
                             newest=max((i.get("start_time") or "" for i in items), default=""),
                             has_more=bool(data.get("pagination_key")),
                             sources=sorted({f"{i.get('source_id')}={(i.get('device') or {}).get('description')}"
                                             for i in items}),
                             example=_sample(items[0]) if items else None)
            else:
                entry["body"] = str(data)[:400]
        except AuthFailed:
            raise
        except Exception as err:  # noqa: BLE001
            entry["error"] = f"{type(err).__name__}: {err}"
        report["evm"]["requests"].append(entry)
    try:
        status, data, _ = await evm_request(session, "GET", "evm/v2/history/unwatched/count")
        report["evm"]["unwatched_count"] = {"status": status, "body": data}
    except Exception as err:  # noqa: BLE001
        report["evm"]["unwatched_count"] = {"error": f"{type(err).__name__}: {err}"}
    report["evm_summary"] = [
        f"{r['label']}: " + (r.get("error") or (f"HTTP {r.get('status')} in {r.get('seconds')}s - "
                             + (f"{r.get('count')} events, {r.get('with_video')} with video, newest {r.get('newest') or '-'}"
                                if "count" in r else str(r.get("body"))[:120])))
        for r in report["evm"]["requests"]]

    report["probes"] = []
    for p in endpoint_probes(blink, day_ago):
        try:
            report["probes"].append(await probe_endpoint(session, p))
        except AuthFailed:
            raise
        except Exception as err:  # noqa: BLE001
            report["probes"].append({"path": p, "error": f"{type(err).__name__}: {err}"})
    def _summ(r):
        if r.get("error"):
            return r["error"]
        lists = ", ".join(f"{x['at']}={x['count']} (newest {x['newest'] or '?'})" for x in r.get("lists", []))
        return f"HTTP {r.get('status')} in {r.get('seconds')}s - {lists or 'no clip lists'}"
    report["probe_summary"] = [f"{r['path'].split('?')[0]}: {_summ(r)}" for r in report["probes"]]

    path = settings.log_path.parent / f"diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.json"
    write_text_safely(path, json.dumps(report, indent=2, default=str))
    log.info("Diagnostic report saved to %s", path)
    return path


def util_time(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="seconds")


# =========================================================================== video sync

class VideoSync:
    S = "SyncVideo"

    def __init__(self, engine: "SyncEngine"):
        self.engine = engine
        self.failures: dict[str, int] = {}

    def since(self) -> float:
        """Epoch seconds to ask Blink for clips from (the LastUpdate bookmark)."""
        st = self.engine.settings
        last = parse_ts(st.get(self.S, "LastUpdate"))
        if last is None:
            return 0
        now = dt.datetime.now(dt.timezone.utc)
        if last > now + dt.timedelta(minutes=10):
            # a bookmark in the future would hide every new clip; pull it back
            fixed = now - dt.timedelta(days=1)
            log.warning("LastUpdate %s is in the future; resetting it to %s",
                        st.get(self.S, "LastUpdate"), fixed.isoformat(timespec="seconds"))
            st.set(self.S, "LastUpdate", fixed.isoformat(timespec="seconds"))
            last = fixed
        return last.timestamp()

    def advance(self, ts: dt.datetime):
        """Move the bookmark forward (never backwards, never into the future)."""
        st = self.engine.settings
        now = dt.datetime.now(dt.timezone.utc)
        ts = min(ts.astimezone(dt.timezone.utc), now)
        current = parse_ts(st.get(self.S, "LastUpdate"))
        if current is None or ts > current:
            st.set(self.S, "LastUpdate", ts.isoformat(timespec="seconds"))

    async def run_once(self) -> int:
        """Download new clips. Returns the number saved."""
        eng, st, S = self.engine, self.engine.settings, self.S
        session = eng.session
        blink = session.blink
        out = Path(st.get(S, "Output"))
        local = st.get_bool(S, "UseLocalTime")
        with_cam = st.get_bool(S, "IncludeCameraName")
        sleep = max(st.get_int(S, "Sleep"), 0)

        since = self.since()
        clips, pages = await list_clips(session, since, max(st.get_int(S, "MaxPages"), 1))
        if pages.get("problem") and not clips:
            raise ClipListError(pages["problem"])     # shows as a problem, not "Up to date"

        todo, skipped = [], {"deleted": 0, "no video link": 0, "bad time": 0,
                             "already saved": 0, "gave up": 0}
        for c in clips:
            ts = parse_ts(clip_time(c))
            if c.get("deleted"):
                skipped["deleted"] += 1
                continue
            url = clip_url(c)
            if not url:
                skipped["no video link"] += 1
                continue
            if ts is None:
                skipped["bad time"] += 1
                continue
            if self.failures.get(clip_key(c), 0) >= MAX_CLIP_FAILURES:
                skipped["gave up"] += 1
                continue
            if c.get("e2ee"):
                skipped["encrypted"] = skipped.get("encrypted", 0) + 1
                if clip_key(c) not in self.failures:
                    log.warning("Skipping end-to-end encrypted clip %s (%s): it can only be played in the Blink app",
                                c.get("device_name"), clip_time(c))
                    self.failures[clip_key(c)] = MAX_CLIP_FAILURES
                continue
            camera = c.get("device_name") or c.get("camera_name") or "Unknown"
            target = target_path(out, ts, local, camera if with_cam else None, ".mp4")
            if target.exists():
                skipped["already saved"] += 1
                continue
            todo.append((ts, c, camera, target, url))
        todo.sort(key=lambda x: x[0])

        detail = ", ".join(f"{v} {k}" for k, v in skipped.items() if v)
        if clips and not todo and (skipped["no video link"] or skipped["bad time"]):
            # Blink returned clips we couldn't use: the response format has probably changed
            log.warning("Blink listed %d clip(s) but none could be used (%s). Example entry keys: %s. "
                        "Use 'Save diagnostic report' and send it for analysis.",
                        len(clips), detail, sorted(clips[0].keys()))
        else:
            by_v = ", ".join(f"{'v' if isinstance(v, int) else ''}{v}: {n} new"
                             for v, n in pages.get("by_version", {}).items())
            log.log(logging.INFO if todo else logging.DEBUG,
                    "Clip list: %d listed across %d page(s) [%s]%s", len(clips), pages["fetched"], by_v,
                    f" ({detail})" if detail else "")
        if not todo:
            log.debug("No new videos since %s", st.get(S, "LastUpdate") or "the beginning")
            return 0
        log.info("%d new video(s) since %s", len(todo), st.get(S, "LastUpdate") or "the beginning")

        downloaded = 0
        saved_events: list[str] = []
        for i, (ts, clip, camera, target, url) in enumerate(todo, 1):
            if not eng.active:
                log.info("Video sync interrupted (%d of %d done)", downloaded, len(todo))
                break
            eng.report("syncing", f"Downloading clip {i} of {len(todo)}")
            await session.ensure_token()
            try:
                if clip.get("evm"):
                    response = await download_presigned(session, url)
                    if response.status in (400, 401, 403) and clip.get("event_id"):
                        # pre-signed link expired (long backfill): ask for a fresh one
                        response.release()
                        fresh = await evm_fresh_url(session, clip["event_id"])
                        if not fresh:
                            raise RuntimeError(f"HTTP {response.status} and no fresh link available")
                        response = await download_presigned(session, fresh)
                else:
                    response = await blink.do_http_get(url)
                if response is None or response.status != 200:
                    raise RuntimeError(f"HTTP {getattr(response, 'status', 'no response')}")
                size = await write_response(response, target)
                log.info("Downloaded %s (%s, %.1f MB)", target, camera, size / 1e6)
                downloaded += 1
                if clip.get("evm") and clip.get("event_id"):
                    saved_events.append(clip["event_id"])
            except AuthFailed:
                raise
            except Exception as err:  # noqa: BLE001 - log and decide whether to retry
                key = clip_key(clip)
                self.failures[key] = self.failures.get(key, 0) + 1
                if self.failures[key] < MAX_CLIP_FAILURES:
                    log.warning("Download failed for %s (%s): %s - will retry next pass",
                                camera, clip_time(clip), err)
                    break   # keep LastUpdate here so the clip is retried
                log.error("Giving up on %s (%s) after %d attempts: %s",
                          camera, clip_time(clip), MAX_CLIP_FAILURES, err)

            self.advance(ts)
            if sleep and i < len(todo):
                await eng.pause_for(sleep)

        if saved_events and st.get_bool("Evm", "MarkWatched"):
            for n in range(0, len(saved_events), 50):
                await evm_mark_watched(session, saved_events[n:n + 50])
        if downloaded:
            log.info("Video sync: %d new clip(s) saved", downloaded)
        return downloaded


# =========================================================================== thumbnail sync

class ThumbnailSync:
    S = "SyncThumbnail"

    def __init__(self, engine: "SyncEngine"):
        self.engine = engine

    async def run_once(self) -> int:
        eng, st, S = self.engine, self.engine.settings, self.S
        blink = eng.session.blink
        out = Path(st.get(S, "Output"))
        local = st.get_bool(S, "UseLocalTime")
        sleep = max(st.get_int(S, "Sleep"), 1)
        retries = max(st.get_int(S, "Retries"), 1)
        saved = 0

        for name, cam in blink.cameras.items():
            if not eng.active:
                break
            eng.report("syncing", f"Snapshot: {name}")
            await eng.session.ensure_token()
            try:
                resp = await api.request_new_image(
                    blink, cam.network_id, cam.camera_id, camera_type=cam.camera_type)
                command = resp.get("id") if isinstance(resp, dict) else None
                if command:
                    for _ in range(retries):
                        await asyncio.sleep(sleep)
                        status = await api.request_command_status(blink, cam.network_id, command)
                        if isinstance(status, dict) and status.get("complete"):
                            break
                else:
                    await asyncio.sleep(sleep * retries)   # no command id; just give it time

                await blink.refresh(force_cache=True)
                response = await cam.get_media()
                if response is None or response.status != 200:
                    raise RuntimeError(f"HTTP {getattr(response, 'status', 'no response')}")
                target = target_path(out, dt.datetime.now(dt.timezone.utc), local, name, ".jpg")
                await write_response(response, target)
                log.info("Snapshot saved %s", target)
                saved += 1
            except Exception as err:  # noqa: BLE001
                log.error("Snapshot failed for %s: %s", name, err)
        return saved


# =========================================================================== engine

@dataclasses.dataclass
class Status:
    state: str = "starting"      # starting | syncing | ok | login | error | paused
    detail: str = ""
    last_check: float | None = None
    clips_this_session: int = 0


class SyncEngine:
    """The polling loop. Runs inside one asyncio loop; control methods must be called on it."""

    def __init__(self, settings: Settings, http: ClientSession,
                 on_status: Callable[[Status], None] | None = None,
                 on_event: Callable[[str, str], None] | None = None):
        self.settings = settings
        self.http = http
        self.session = Session(settings, http)
        self.videos = VideoSync(self)
        self.thumbs = ThumbnailSync(self)
        self.status = Status()
        self._on_status = on_status
        self._on_event = on_event
        self._wake = asyncio.Event()
        self._force = False
        self.paused = False
        self.stopping = False
        self._auth_notified = False

    # ---- control (call on the engine's loop)
    def trigger(self):
        """Run a pass now (Sync now / settings saved / signed in)."""
        self._force = True
        self._wake.set()

    def set_paused(self, paused: bool):
        self.paused = paused
        self._wake.set()

    def stop(self):
        self.stopping = True
        self._wake.set()

    def adopt_login(self, blink: Blink):
        self.session.adopt(blink)
        self._auth_notified = False
        self.trigger()

    @property
    def active(self) -> bool:
        return not (self.stopping or self.paused)

    # ---- reporting
    def report(self, state: str, detail: str = "", **kw):
        self.status = dataclasses.replace(self.status, state=state, detail=detail, **kw)
        if self._on_status:
            try:
                self._on_status(self.status)
            except Exception:  # noqa: BLE001
                log.exception("Status callback failed")

    def event(self, kind: str, message: str):
        if self._on_event:
            try:
                self._on_event(kind, message)
            except Exception:  # noqa: BLE001
                log.exception("Event callback failed")

    async def pause_for(self, seconds: float | None):
        """Sleep that ends early on trigger/pause/stop."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake.clear()

    # ---- main loop
    async def run(self, once: bool = False) -> int:
        st = self.settings
        next_video = next_thumb = 0.0

        while not self.stopping:
            if self.paused and not once:
                self.report("paused", "Syncing paused")
                await self.pause_for(None)
                continue
            force, self._force = self._force, False
            try:
                st.reload()
                if self.session.blink is None:
                    self.report("starting", "Signing in to Blink…")
                    await self.session.start()
                await self.session.ensure_token()

                video_on = st.get_bool("SyncVideo", "Enabled")
                thumb_on = st.get_bool("SyncThumbnail", "Enabled")
                now = time.time()
                if video_on and (force or now >= next_video):
                    self.report("syncing", "Checking for new clips…")
                    n = await self.videos.run_once()
                    if n:
                        self.status.clips_this_session += n
                        self.event("clips", f"{n} new clip{'s' if n != 1 else ''} downloaded")
                    next_video = time.time() + max(st.get_int("SyncVideo", "Interval"), MIN_VIDEO_INTERVAL)
                if thumb_on and (now >= next_thumb):
                    await self.thumbs.run_once()
                    next_thumb = time.time() + max(st.get_int("SyncThumbnail", "Interval"), MIN_THUMB_INTERVAL)
                self._auth_notified = False
                self.report("ok", "", last_check=time.time())
            except AuthFailed as err:
                log.error("%s", err)
                self.session.blink = None
                self.report("login", str(err))
                if not self._auth_notified:
                    self.event("auth", str(err))
                    self._auth_notified = True
                if once:
                    return 2
                await self.pause_for(AUTH_RETRY_DELAY)
                continue
            except ClipListError as err:   # already logged by list_clips
                self.report("error", f"Clip list: {err}")
                if once:
                    return 1
                await self.pause_for(ERROR_RETRY_DELAY)
                continue
            except Exception as err:  # noqa: BLE001 - keep running
                log.exception("Unexpected error; retrying shortly")
                self.report("error", str(err) or type(err).__name__)
                if once:
                    return 1
                await self.pause_for(ERROR_RETRY_DELAY)
                continue

            if once:
                return 0
            waits = [t for t, on in ((next_video, video_on), (next_thumb, thumb_on)) if on]
            if not waits:
                log.warning("Both SyncVideo and SyncThumbnail are disabled in %s", st.ini_path)
                waits = [time.time() + 300]
            await self.pause_for(max(min(waits) - time.time(), 1))
        return 0


# =========================================================================== command line

def log_startup(settings: Settings, app: str = "bling_reborn"):
    creds = load_creds(settings.creds_path)
    log.info("%s %s starting. Settings: %s | sign-in saved: %s | videos: %s", app, __version__,
             settings.ini_path, "yes" if creds.get("refresh_token") else "no",
             settings.get("SyncVideo", "Output"))
    for note in settings.notes:
        log.warning("%s", note)


async def cmd_login(settings: Settings) -> int:
    old = load_creds(settings.creds_path)
    email = input(f"Blink email [{old.get('username', '')}]: ").strip() or old.get("username", "")
    password = getpass.getpass("Blink password: ")
    async with ClientSession() as http:
        flow = LoginFlow(settings, http)
        result = await flow.begin(email, password)
        if result == "2fa":
            result = await flow.complete(input("Blink sent you a verification code. Enter it: "))
        if result != "ok":
            print(f"Sign-in failed: {flow.error}")
            return 1
        print(f"Signed in. Credentials saved to {settings.creds_path}")
        print("Cameras:", ", ".join(flow.blink.cameras.keys()) or "none")
        print(f"Settings: {settings.ini_path}")
    return 0


async def cmd_diagnose(settings: Settings) -> int:
    async with ClientSession() as http:
        session = Session(settings, http)
        await session.start()
        path = await write_diagnostics(session, settings)
        print(f"Diagnostic report saved to {path}")
    return 0


async def cmd_cameras(settings: Settings) -> int:
    async with ClientSession() as http:
        session = Session(settings, http)
        await session.start()
        for name, cam in session.blink.cameras.items():
            print(f"{name:30} type={cam.camera_type or cam.product_type!s:10} "
                  f"network={cam.network_id} id={cam.camera_id}")
    return 0


async def cmd_run(settings: Settings, once: bool) -> int:
    async with ClientSession() as http:
        return await SyncEngine(settings, http).run(once=once)


def main():
    parser = argparse.ArgumentParser(description="Download Blink clips to local storage.")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["login", "run", "cameras", "diagnose"])
    parser.add_argument("--once", action="store_true", help="run one pass then exit")
    parser.add_argument("--home", type=Path, default=default_home(),
                        help="folder for Blink.ini, credentials.json and logs")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()

    settings = Settings(args.home, migrate=(args.home == default_home()))
    setup_logging(settings.log_path, args.verbose)
    log_startup(settings)

    coro = {"login": lambda: cmd_login(settings),
            "cameras": lambda: cmd_cameras(settings),
            "diagnose": lambda: cmd_diagnose(settings),
            "run": lambda: cmd_run(settings, args.once)}[args.command]()
    prog = "bling-reborn-cli" if getattr(sys, "frozen", False) else "python bling_reborn.py"
    hint = f"Sign in from the {APP_NAME} tray icon, or run:  {prog} login"
    loop = new_event_loop()
    try:
        rc = loop.run_until_complete(coro)
        if rc == 2:
            log.error(hint)
        sys.exit(rc)
    except AuthFailed as err:
        log.error("%s. %s", err, hint)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(0)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
