"""Clip listing and downloading against a fake Blink (legacy list, v2 list and EVM history)."""

import json

from conftest import core, legacy_clip


def _legacy_only(h):
    h.settings.set("Evm", "Enabled", "false")


def test_legacy_paging_is_1_based_and_complete(run):
    def setup(h):
        _legacy_only(h)
        h.fake.v1 = [legacy_clip(i, f"2026-11-19T10:{i:02d}:00+00:00") for i in range(30)]

    async def scenario(h):
        n = await h.engine.videos.run_once()
        return n, h.settings.get("SyncVideo", "LastUpdate"), len(h.files())

    n, last, files = run(scenario, setup)
    assert (n, files) == (30, 30)
    assert last == "2026-11-19T10:29:00+00:00"


def test_second_pass_downloads_nothing_new(run):
    def setup(h):
        _legacy_only(h)
        h.fake.v1 = [legacy_clip(1, "2026-11-19T10:00:00+00:00")]

    async def scenario(h):
        await h.engine.videos.run_once()
        return await h.engine.videos.run_once(), len(h.files())

    assert run(scenario, setup) == (0, 1)


def test_v2_clips_are_merged_with_v1(run):
    def setup(h):
        _legacy_only(h)
        h.settings.set("SyncVideo", "LastUpdate", "2026-11-19T19:36:32+00:00")
        h.fake.v1 = [legacy_clip(1, "2026-11-19T19:36:32+00:00")]
        h.fake.v2 = h.fake.v1 + [legacy_clip(2, "2026-11-21T08:00:00+00:00", "Front Door")]

    async def scenario(h):
        await h.engine.videos.run_once()
        return [f for f, _ in h.files()]

    assert run(scenario, setup) == ["2026-11-19T19_36_32+00_00 - Garden.mp4",
                                    "2026-11-21T08_00_00+00_00 - Front Door.mp4"]


def test_evm_history_end_to_end(run):
    """Paging, pre-signed links without auth, expired-link refresh, e2ee skip, de-dup with v1, mark watched."""
    def setup(h):
        f = h.fake
        h.settings.set("SyncVideo", "LastUpdate", "2026-11-19T19:36:32+00:00")
        h.settings.set("Evm", "MarkWatched", "true")
        f.v1 = [legacy_clip(1, "2026-11-19T19:36:32+00:00", "Garden")]
        f.evm = [f.evm_item("e1", "2026-11-19T19:36:32Z", 2001, "Garden"),            # same clip as v1
                 f.evm_item("e2", "2026-11-21T08:00:00Z", 2002, "Front Door"),
                 f.evm_item("e3", "2026-11-22T18:30:00Z", 2003, "Driveway", expired=True),
                 f.evm_item("e4", "2026-11-22T19:00:00Z", 2004, "Garage", e2ee=True)]

    async def scenario(h):
        rc = await h.engine.run(once=True)
        first = h.files()
        await h.engine.run(once=True)
        return rc, first, h.files(), h

    rc, first, second, h = run(scenario, setup)
    assert rc == 0
    assert first == [("2026-11-19T19_36_32+00_00 - Garden.mp4", b"EVM-e1.mp4"),
                     ("2026-11-21T08_00_00+00_00 - Front Door.mp4", b"EVM-e2.mp4"),
                     ("2026-11-22T18_30_00+00_00 - Driveway.mp4", b"EVM-e3.mp4")]
    assert second == first                                  # nothing downloaded twice
    assert all(h.fake.gateway_headers_ok)                   # Bearer + X-Org-Id on every gateway call
    assert h.fake.storage_saw_auth == 0                     # no Blink token sent to pre-signed links
    assert h.fake.watched == ["e1", "e2", "e3"]             # only saved clips marked watched
    assert "end-to-end encrypted" in h.log()
    assert h.settings.get("SyncVideo", "LastUpdate") == "2026-11-22T18:30:00+00:00"


def test_mark_watched_is_off_by_default(run):
    def setup(h):
        h.fake.evm = [h.fake.evm_item("e1", "2026-11-21T08:00:00Z", 2001, "Garden")]
        h.settings.set("SyncVideo", "LastUpdate", "2026-11-20T00:00:00+00:00")

    async def scenario(h):
        await h.engine.run(once=True)
        return len(h.files()), h.fake.watched

    assert run(scenario, setup) == (1, [])


def test_one_source_failing_is_not_an_error(run):
    def setup(h):
        h.fake.v1 = [legacy_clip(1, "2026-11-19T10:00:00+00:00")]   # EVM and v2 return 404

    async def scenario(h):
        rc = await h.engine.run(once=True)
        return rc, h.engine.status.state, len(h.files())

    assert run(scenario, setup) == (0, "ok", 1)


def test_all_sources_failing_shows_a_problem_not_up_to_date(run):
    def setup(h):
        h.fake.v1_status = 503

    async def scenario(h):
        rc = await h.engine.run(once=True)
        return rc, h.engine.status.state, h.engine.status.detail

    rc, state, detail = run(scenario, setup)
    assert (rc, state) == (1, "error")
    assert "evm" in detail and "v1" in detail and "503" in detail


def test_unusable_entries_are_warned_about(run):
    def setup(h):
        _legacy_only(h)
        clip = legacy_clip(1, "2026-11-21T08:00:00+00:00")
        clip["video_link_renamed"] = clip.pop("media")
        h.fake.v1 = [clip]

    async def scenario(h):
        await h.engine.videos.run_once()
        return h.log()

    log = run(scenario, setup)
    assert "none could be used" in log and "video_link_renamed" in log


def test_future_bookmark_is_pulled_back(run):
    def setup(h):
        _legacy_only(h)
        h.settings.set("SyncVideo", "LastUpdate", "2099-01-01T00:00:00+00:00")

    async def scenario(h):
        await h.engine.videos.run_once()
        return core.parse_ts(h.settings.get("SyncVideo", "LastUpdate"))

    assert run(scenario, setup).year < 2099


def test_diagnostic_report_scrubs_clip_link_signatures(run):
    def setup(h):
        h.fake.evm = [h.fake.evm_item("e1", "2026-11-21T08:00:00Z", 2001, "Garden")]
        h.fake.v1 = [legacy_clip(1, "2026-11-19T10:00:00+00:00")]

    async def scenario(h):
        path = await core.write_diagnostics(h.engine.session, h.settings)
        return path.read_text(encoding="utf-8")

    text = run(scenario, setup)
    report = json.loads(text)
    assert "X-Amz-Signature" not in text
    assert report["evm_summary"] and report["clip_list_summary"]
    example = next(r["example"] for r in report["evm"]["requests"] if r.get("example"))
    assert example["visualizations"]["cloud_media_visualization"]["media"][1]["url"].endswith("<signature removed>")
