# Blink Sync

[![Tests](https://github.com/shoey79/blink-sync/actions/workflows/tests.yml/badge.svg)](https://github.com/shoey79/blink-sync/actions/workflows/tests.yml)
[![Latest release](https://img.shields.io/github/v/release/shoey79/blink-sync)](https://github.com/shoey79/blink-sync/releases/latest)

A Windows tray app that downloads your Blink cloud clips (and, optionally, scheduled snapshots) to a local or network folder.
It's a replacement for [Bling](https://github.com/lurume84/bling-desktop), which stopped working when Blink changed its sign-in.
It uses [blinkpy](https://github.com/fronzbot/blinkpy), the library behind Home Assistant's Blink integration, to sign in with Blink's current OAuth 2.0 + two-factor flow,
and reads Blink's new clip history (since November 2026) directly. See [docs/blink-api.md](docs/blink-api.md).

## Download

Get `BlinkSync-Setup-<version>.exe` from the [latest release](https://github.com/shoey79/blink-sync/releases/latest) and run it.
The installer is built by GitHub Actions from this repository's code.
It isn't code-signed, so Windows SmartScreen may show "Windows protected your PC" the first time: choose **More info → Run anyway**.

## Building it yourself

On any Windows 10/11 PC, clone or download this repository and run in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

The result is `dist\BlinkSync-Setup-1.1.0.exe`.

`build.ps1` does the following:

- Finds Python 3.10 or later. If there isn't one, it installs Python 3.12 for the current user via winget.
- Builds in a private virtual environment under `build\`.
- Installs Inno Setup via winget if it's missing (version 6.3 or later is needed).

Options:

| Option | Effect |
|---|---|
| `-CertThumbprint <sha1>` | Code-signs both exes and the installer with a certificate from your store, e.g. one issued by an internal CA. |
| `-Version 1.1.0` | Overrides the version (default comes from `__version__` in `blink_sync.py`). |
| `-SkipInstaller` | Only builds the app folder (`build\pyinstaller\dist\BlinkSync`). |

Unsigned builds trigger a SmartScreen "unrecognised app" prompt the first time they run. Choose **More info → Run anyway**, or sign them.

Releases are built the same way by the [Release workflow](.github/workflows/release.yml) whenever a version tag (`v1.2.3`) is pushed.

## Install and first run

The installer is per-user: no admin rights are needed, and it installs to `%LOCALAPPDATA%\Programs\BlinkSync`.
It adds a Start menu shortcut and, optionally, starts Blink Sync when you sign in to Windows.

On first launch, the **Sign in to Blink** window opens. Enter your email and password, then the code Blink sends you.
After that the app works in the background and keeps itself signed in.

## Tray menu

The menu opens from the camera icon in the notification area. Left-click the icon to open the videos folder.

| Item | |
|---|---|
| *status line* | e.g. "Up to date · checked 18:05 · 3 clips downloaded since start", "Downloading clip 4 of 120", "Sign-in needed" |
| Open videos folder / Open snapshots folder | |
| Sync now | Checks immediately instead of waiting for the interval |
| Pause syncing | Stops checking until unticked |
| Sign in to Blink… | Use this if Blink ends the session |
| Settings… | All the options below, plus Start with Windows and new-clip notifications |
| View log | Opens `Logs\blink_sync.log` |
| Save diagnostic report | Saves Blink's raw clip-list responses to `Logs\diagnostics-<time>.json` and shows the file. Use it when clips stop downloading. It holds camera names and clip details but no sign-in token. |

The icon colour shows the state: blue when idle, green while syncing, amber when sign-in is needed, red on errors and grey when paused.
If Blink revokes the sign-in, you get a notification. Pick **Sign in to Blink…** and syncing resumes straight away.

## Files

Settings, the sign-in and logs live in `%LOCALAPPDATA%\BlinkSync`. This location isn't affected by Documents redirection, OneDrive sync or Controlled Folder Access:

| File | Purpose |
|---|---|
| `Blink.ini` | Settings, using the same sections and keys as Bling. It's re-read before every check, so manual edits work too. |
| `credentials.json` | OAuth refresh token and device ID, encrypted with Windows DPAPI so only your Windows account on this PC can read it. Your password is never stored. |
| `Logs\blink_sync.log` | Rotating log (5 × 5 MB). Each start logs which settings file is in use and whether a saved sign-in was found. |

Clips and snapshots go to `Documents\BlinkSync\Download\...` unless you choose other folders in Settings.
Version 1.0.0 kept settings in `Documents\BlinkSync`. 1.0.1 and later copy them across automatically on first start.

Clips are saved in Bling's layout:

```
<Output>\2026\October\18\2026-10-18T11_00_05+00_00 - Front Door.mp4
```

## Settings (`Blink.ini`)

```ini
[SyncVideo]
Enabled=true
UseLocalTime=false        ; true = name files/folders in this PC's time zone instead of UTC
IncludeCameraName=true    ; appends " - CameraName" so same-second clips from two cameras don't collide
Interval=60               ; seconds between checks (min 30)
Sleep=20                  ; seconds between downloads (Bling default; lower it to speed up the first backfill)
MaxPages=100              ; cap on pages fetched per check (~25 clips per page)
Output=D:\Videos          ; UNC paths (\\server\share\Blink) work if your account can reach them
LastUpdate=               ; written automatically; clear it to re-download everything Blink still holds

[SyncThumbnail]
Enabled=false             ; asks every camera for a fresh snapshot (uses battery on battery cameras)
UseLocalTime=false
Interval=3600             ; min 300
Sleep=5
Retries=10
Output=D:\Thumbnails

[Evm]
Enabled=true              ; Blink's new clip history (what the Blink app uses since 19 Nov 2026)
MarkWatched=false         ; mark clips as watched in the Blink app once they're saved
BaseUrl=https://api.blink.com/blink/
PageSize=50

[App]
NotifyNewClips=false
```

### Where clips come from

Since 19 November 2026 Blink stores new clips in Ring's event-video service ("EVM"), reached through
`https://api.blink.com/blink/evm/v2/history/devices`. The old `media/changed` list stopped receiving new
clips then. Blink Sync reads both and merges them, so older clips and new ones are covered. EVM clip links
are pre-signed and expire, so a clip that fails with an expired link is fetched again with a fresh one.
End-to-end encrypted clips can only be played in the Blink app and are skipped (with a log entry).

The first check downloads everything still in the Blink cloud. A clip that fails to download is retried on the next two checks, then skipped and logged.
Only cloud clips are downloaded. Clips stored only on a Sync Module 2 USB stick aren't included (Bling didn't support those either).

## Command line

The install folder also contains `blink-sync-cli.exe`, which uses the same settings and sign-in:

```powershell
blink-sync-cli login          # sign in from a console
blink-sync-cli run --once     # one pass, e.g. from a scheduled task (exit code 2 = sign-in needed)
blink-sync-cli cameras        # list cameras
blink-sync-cli diagnose       # same as the tray's Save diagnostic report
blink-sync-cli --home D:\BlinkSyncData run   # use a different settings/data folder
```

Don't run the CLI's `run` command and the tray app at the same time against the same folder.

## Uninstall

Use **Settings → Apps**. The uninstaller removes the startup entry and offers to delete the saved sign-in.
Downloaded videos and `Blink.ini` are always kept.

## Developing / running from source

```powershell
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pythonw blink_sync_tray.py      # tray app
.venv\Scripts\python blink_sync.py run        # command line
```

| File | |
|---|---|
| `blink_sync.py` | Sync engine, settings, sign-in and CLI |
| `blink_sync_tray.py` | Tray app and windows |
| `icons.py` | Draws the app/tray icons |
| `BlinkSync.spec` | PyInstaller build (both exes, one shared folder) |
| `installer.iss` | Inno Setup script |
| `build.ps1` | Builds everything |

If Blink changes its sign-in again, rebuild after `build.ps1` pulls the latest blinkpy (it always upgrades), and bump `__version__`.

## Reporting problems

Open an [issue](https://github.com/shoey79/blink-sync/issues/new/choose). A diagnostic report (tray icon → **Save diagnostic report**) helps most.
This repository is public, so remove account IDs, camera names and paths first; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Credits and disclaimer

Built on [blinkpy](https://github.com/fronzbot/blinkpy) (MIT), and inspired by [Bling](https://github.com/lurume84/bling-desktop), whose folder layout and settings it keeps.

Blink Sync is an unofficial project. It isn't affiliated with or endorsed by Blink, Ring or Amazon. It uses undocumented Blink APIs that can change at any time.
Use it only with your own Blink account. Released under the [MIT licence](LICENSE).
