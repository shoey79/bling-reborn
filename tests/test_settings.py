"""Settings file handling (Blink.ini)."""

import datetime as dt

from conftest import core


def test_defaults_are_written_and_survive_a_restart(tmp_path):
    st = core.Settings(tmp_path)
    st.set("SyncVideo", "Output", r"D:\Blink\Videos")
    again = core.Settings(tmp_path)
    assert again.get("SyncVideo", "Output") == r"D:\Blink\Videos"
    assert again.get_bool("Evm", "Enabled") is True
    assert again.get_bool("Evm", "MarkWatched") is False


def test_notepad_bom_is_tolerated(tmp_path):
    st = core.Settings(tmp_path)
    raw = st.ini_path.read_bytes().replace(b"Interval=60", b"Interval=120")
    st.ini_path.write_bytes("\ufeff".encode() + raw)
    assert core.Settings(tmp_path).get_int("SyncVideo", "Interval") == 120


def test_unreadable_file_is_backed_up_not_lost(tmp_path):
    st = core.Settings(tmp_path)
    st.ini_path.write_text("Output=no section header\n", encoding="utf-8")
    again = core.Settings(tmp_path)
    assert list(tmp_path.glob("Blink.ini.unreadable-*"))
    assert again.notes


def test_target_path_uses_bling_layout(tmp_path):
    ts = dt.datetime(2026, 11, 22, 18, 30, 5, tzinfo=dt.timezone.utc)
    p = core.target_path(tmp_path, ts, False, "Front: Door", ".mp4")
    assert p.relative_to(tmp_path).as_posix() == "2026/November/22/2026-11-22T18_30_05+00_00 - Front_ Door.mp4"


def test_evm_items_without_video_are_ignored():
    item = {"event_id": "x", "start_time": "2026-11-22T18:30:00Z",
            "visualizations": {"cloud_media_visualization": {"media": [{"file_family": "THUMBNAIL", "url": "u"}]}}}
    assert core.evm_to_clip(item, {}) is None


def test_upgrade_from_blink_sync_copies_settings_and_sign_in(tmp_path, monkeypatch):
    """Settings and sign-in from Blink Sync (<= 1.1.0) are picked up by Bling Reborn, newest copy first."""
    import json, os, time
    appdata, profile = tmp_path / "appdata", tmp_path / "profile"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(appdata))   # stands in for %LOCALAPPDATA% off Windows
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    monkeypatch.setenv("USERPROFILE", str(profile))

    old_docs = profile / "Documents" / "BlinkSync"            # 1.0.0
    old_docs.mkdir(parents=True)
    (old_docs / "Blink.ini").write_text("[SyncVideo]\nOutput=D:\\Old\n", encoding="utf-8")
    old_local = appdata / "BlinkSync"                        # 1.0.1 - 1.1.0 (newer)
    old_local.mkdir(parents=True)
    (old_local / "Blink.ini").write_text("[SyncVideo]\nOutput=D:\\Blink\\Videos\nLastUpdate=2026-11-19T19:36:32+00:00\n",
                                         encoding="utf-8")
    (old_local / "credentials.json").write_text(json.dumps({"refresh_token": "r", "hardware_id": "h"}), encoding="utf-8")
    past = time.time() - 3600
    os.utime(old_docs / "Blink.ini", (past, past))

    home = core.default_home()
    assert home == appdata / "BlingReborn"
    st = core.Settings(home, migrate=True)
    assert st.get("SyncVideo", "Output") == r"D:\Blink\Videos"
    assert st.get("SyncVideo", "LastUpdate") == "2026-11-19T19:36:32+00:00"
    assert core.load_creds(st.creds_path).get("refresh_token") == "r"
    assert any("BlinkSync" in n for n in st.notes)
    assert core.Settings(home, migrate=True).notes == []     # only once
