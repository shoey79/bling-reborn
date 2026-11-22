# Changelog

## 1.2.0

- Renamed from Blink Sync to **Bling Reborn**, so the app's name no longer uses Blink's trademark. The program is now
  `BlingReborn.exe` (and `bling-reborn-cli.exe`), and settings, sign-in and logs live in `%LOCALAPPDATA%\BlingReborn`.

- Upgrading is automatic: installing over Blink Sync replaces it (same entry in Settings → Apps), moves the program to
  `%LOCALAPPDATA%\Programs\BlingReborn`, removes the old shortcuts, and keeps "Start with Windows" if it was on. Your
  settings and sign-in are copied across on first start, so there's no need to sign in again.

- The GitHub repository moved to [shoey79/bling-reborn](https://github.com/shoey79/bling-reborn); old links redirect.

## 1.1.0

- Downloads clips again after Blink moved clip storage on 19 November 2026. New clips are now read from Blink's
  current clip history at `api.blink.com`; the old `media/changed` list stopped receiving clips that day and is
  still read for older clips. Both are merged, so nothing is saved twice.

- Expired clip download links are refreshed automatically during long backfills.

- End-to-end encrypted clips are skipped with a log entry (only the Blink app can play them).

- New setting: **Mark clips as watched in the Blink app after downloading them** (`[Evm] MarkWatched`).

- The diagnostic report covers the new clip history.

## 1.0.6

- Reads both the v1 and v2 clip lists and merges them.

- Diagnostic report: v2 list over every time window, account subscription and entitlement details, more endpoint probes.

## 1.0.5

- New **Save diagnostic report** probes of other places Blink might list clips (read-only).

- The tray shows a problem instead of "Up to date" when the clip list can't be fetched.

## 1.0.4

- Fetches the clip list directly with a 90-second timeout (blinkpy allows only 10 seconds), with clearer
  errors for timeouts, DNS failures and HTTP errors.

- The `+` in the clip list's timestamp is sent URL-encoded.

## 1.0.3

- Clip paging works whether Blink numbers pages from 0 or 1.

- Warns when Blink's clip details change format instead of silently skipping clips.

- A `LastUpdate` bookmark in the future is reset automatically.

- New **Save diagnostic report** tray item and `blink-sync-cli diagnose`.

## 1.0.2

- The tray menu's status line could stay on "Downloading clip N of N" after a sync finished; the menu is now
  rebuilt each time it opens.

- Clearer wording: "N clips downloaded since start".

## 1.0.1

- Settings, sign-in and logs moved to `%LOCALAPPDATA%\BlinkSync` (copied across automatically), away from
  Documents redirection, OneDrive and Controlled Folder Access.

- Saves retry while another program briefly locks the file, and an unreadable `Blink.ini` is backed up rather
  than reset.

## 1.0.0

- First release: tray app and installer, sign-in with two-factor codes, Bling-style folders and settings,
  optional scheduled snapshots, DPAPI-encrypted sign-in.
