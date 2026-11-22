# Working on Blink Sync

This repository is public. Never publish details of a real Blink account or network in code, comments,
docs, changelogs, commit messages or test data:

- No real account IDs, network or camera IDs, camera or system names, Blink region hosts taken from a
  user's logs (e.g. a specific `rest-xxxx` tier), serial numbers, email addresses, file shares or paths.
- No tokens, refresh tokens, hardware IDs or pre-signed clip URLs (they carry credentials), even expired.
- Use stand-ins instead: account `12345`, networks `1001`/`1002`, cameras `Front Door`, `Garden`,
  `Driveway`, paths `D:\Blink\Videos` or `\\server.example.local\cctv`, host `rest-example`.
- When a fix comes from a user's diagnostic report or log, rewrite the example before it goes in the
  changelog, docs or tests.

## Releases

- Version lives in `__version__` in `blink_sync.py`; bump it and add a `CHANGELOG.md` entry for every release.
- Push a tag `vX.Y.Z` matching `__version__`; the Release workflow builds the installer on Windows and
  publishes a GitHub Release with `BlinkSync-Setup-X.Y.Z.exe` and the changelog section as release notes.
- Changelog: newest first, a blank line between top-level entries.

## Changelog entries for GitHub issues

- Link the issue and thank whoever raised it, e.g. `([#4](https://github.com/shoey79/blink-sync/issues/4), thanks
  to [name](https://github.com/name))`. Don't write a bare `@name` in commit messages (it notifies them).

## Blink API notes

`docs/blink-api.md` records what's known about the endpoints used, including what was learned from the
Blink Android app. Keep it current when an endpoint changes.

## Tests

`python -m pytest -q tests` (needs `pip install -r requirements.txt pytest`). The tests run a local fake
Blink server; they never contact Blink.
