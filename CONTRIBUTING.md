# Contributing to Bling Reborn

Bug reports, ideas and pull requests are welcome.

## Reporting a bug or asking for something

Open an [issue](https://github.com/shoey79/bling-reborn/issues/new/choose) and pick the form that fits.
Security problems go through the [security policy](SECURITY.md) instead.

The most useful thing to attach to a bug is a **diagnostic report** (tray icon → **Save diagnostic report**)
and the relevant part of the log (tray icon → **View log**). Clean them first (below).

## Keep your account private

This repository is public. Before posting logs, diagnostic reports or screenshots, replace or remove:

| Instead of your real… | Use |
|---|---|
| Account ID | `12345` |
| Network / camera IDs | `1001`, `2001` |
| Camera and system names | `Front Door`, `Garden`, `Home` |
| Folder paths and shares | `D:\Blink\Videos`, `\\server.example.local\cctv` |
| Blink host (`rest-xxxx`) | `rest-example` |

Never post `credentials.json`, tokens or clip links (they contain credentials). Diagnostic reports remove
the signature from clip links, but check anyway.

## Developing

```powershell
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt pytest
.venv\Scripts\python -m pytest -q tests
.venv\Scripts\pythonw bling_reborn_tray.py
```

| File | |
|---|---|
| `bling_reborn.py` | Sync engine, Blink API calls, settings, sign-in and CLI |
| `bling_reborn_tray.py` | Tray app and windows |
| `icons.py` | Draws the app/tray icons |
| `BlingReborn.spec`, `installer.iss`, `build.ps1` | Windows build and installer |
| `tests/` | Tests against a local fake Blink server |
| `docs/blink-api.md` | What's known about the Blink endpoints used |

For a pull request: add a `CHANGELOG.md` entry, keep tests passing, and keep real account details out of
everything (code, comments, tests, docs).
