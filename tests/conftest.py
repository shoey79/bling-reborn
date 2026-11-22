"""Shared test helpers: a local fake Blink server and a sync engine wired to it.

Nothing here talks to Blink. All IDs and names are stand-ins.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest
from aiohttp import ClientSession, web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("PYSTRAY_BACKEND", "dummy")

import bling_reborn as core  # noqa: E402

ACCOUNT = 12345


def legacy_clip(i, when, camera="Garden"):
    return {"id": i, "created_at": when, "updated_at": when, "deleted": False, "device": "catalina",
            "device_id": 2000 + i, "device_name": camera, "network_id": 1001, "type": "video",
            "watched": False, "media": f"/api/v3/media/accounts/{ACCOUNT}/networks/1001/catalina/{i}.mp4"}


class FakeBlink:
    """In-process HTTP server standing in for Blink's REST host, API gateway and clip storage."""

    def __init__(self):
        self.v1 = []            # legacy clips (media/changed v1)
        self.v2 = None          # None = 404, list = clips
        self.v1_status = 200
        self.evm = None         # None = 404, list = EVM history items
        self.evm_page_size = 2
        self.watched = []
        self.storage_saw_auth = 0
        self.gateway_headers_ok = []
        self.url = None
        self._runner = None

    # ---- EVM items
    def evm_item(self, eid, when, source, name, e2ee=False, expired=False):
        return {"event_id": eid, "source_id": str(source), "start_time": when, "event_type": "motion",
                "is_watched": False, "device": {"id": source, "type": "camera", "description": name},
                "visualizations": {"cloud_media_visualization": {"media": [
                    {"file_family": "THUMBNAIL", "url": f"{self.url}/storage/{eid}.jpg?X-Amz-Signature=abc"},
                    {"file_family": "VIDEO", "file_type": "VIDEO", "is_e2ee": e2ee,
                     "url": f"{self.url}/storage/{eid}.mp4?X-Amz-Signature={'old' if expired else 'abc'}"}]}}}

    # ---- handlers
    def _page(self, items, req):
        since = core.parse_ts(req.query["since"].replace(" ", "+"))
        page = max(int(req.query["page"]), 1)                      # Blink pages are 1-based; 0 == 1
        sel = [c for c in items if core.parse_ts(c["created_at"]) >= since]
        return sel[(page - 1) * 200: page * 200]

    async def _v1(self, req):
        if self.v1_status != 200:
            return web.Response(status=self.v1_status, text="unavailable")
        return web.json_response({"limit": 200, "purge_id": 1, "refresh_count": 0, "media": self._page(self.v1, req)})

    async def _v2(self, req):
        if self.v2 is None:
            return web.Response(status=404, text="<h1>Not Found</h1>")
        return web.json_response({"limit": 200, "event_purge_id": 1, "media_purge_id": 2, "refresh_count": 0,
                                  "media": self._page(self.v2, req)})

    async def _legacy_clip(self, req):
        return web.Response(body=b"LEGACY-" + req.match_info["f"].encode())

    async def _evm_history(self, req):
        ok = req.headers.get("Authorization") == "Bearer tok" and req.headers.get("X-Org-Id") == "Blink"
        self.gateway_headers_ok.append(ok)
        if not ok:
            return web.json_response({"error": "unauthorized"}, status=403)
        if self.evm is None:
            return web.json_response({"message": "Not Found"}, status=404)
        start = core.parse_ts(req.query["start_time"])
        sel = [e for e in self.evm if core.parse_ts(e["start_time"]) >= start]
        offset = int(req.query.get("pagination_key") or 0)
        chunk = sel[offset: offset + self.evm_page_size]
        nxt = offset + self.evm_page_size
        return web.json_response({"items": chunk, "pagination_key": str(nxt) if nxt < len(sel) else None})

    async def _evm_event(self, req):
        e = json.loads(json.dumps(next(x for x in self.evm if x["event_id"] == req.match_info["id"])))
        e["visualizations"]["cloud_media_visualization"]["media"][1]["url"] = \
            f"{self.url}/storage/{e['event_id']}.mp4?X-Amz-Signature=fresh"
        return web.json_response(e)

    async def _evm_watch(self, req):
        self.watched += (await req.json())["event_ids"]
        return web.Response(status=200)

    async def _unwatched(self, req):
        return web.json_response({"count": 0})

    async def _homescreen(self, req):
        return web.json_response({"subscriptions": {}, "entitlements": {}, "video_stats": {}})

    async def _storage(self, req):
        if "Authorization" in req.headers:          # like S3: a second auth mechanism is rejected
            self.storage_saw_auth += 1
            return web.Response(status=400, text="Only one auth mechanism allowed")
        if req.query.get("X-Amz-Signature") == "old":
            return web.Response(status=403, text="Request has expired")
        return web.Response(body=b"EVM-" + req.match_info["f"].encode())

    async def start(self):
        app = web.Application()
        app.router.add_get(f"/api/v1/accounts/{ACCOUNT}/media/changed", self._v1)
        app.router.add_get(f"/api/v2/accounts/{ACCOUNT}/media/changed", self._v2)
        app.router.add_get("/api/v3/media/{tail:.*}/{f}", self._legacy_clip)
        app.router.add_get("/blink/evm/v2/history/devices", self._evm_history)
        app.router.add_get("/blink/evm/v2/history/events/{id}", self._evm_event)
        app.router.add_post("/blink/evm/v2/events/watch", self._evm_watch)
        app.router.add_get("/blink/evm/v2/history/unwatched/count", self._unwatched)
        app.router.add_get(f"/api/v3/accounts/{ACCOUNT}/homescreen", self._homescreen)
        app.router.add_get("/storage/{f}", self._storage)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    async def stop(self):
        await self._runner.cleanup()


class Harness:
    """A real SyncEngine pointed at the fake server, with settings in a temp folder."""

    def __init__(self, tmp_path, fake, http):
        self.fake = fake
        self.home = tmp_path / "home"
        self.out = tmp_path / "Videos"
        self.settings = st = core.Settings(self.home)
        core.setup_logging(st.log_path)
        st.set("SyncVideo", "Sleep", "0")
        st.set("SyncVideo", "Output", str(self.out))
        st.set("Evm", "BaseUrl", fake.url + "/blink/")
        base = fake.url

        class _Blink:
            cameras = {}
            urls = mock.Mock(base_url=base)
            account_id = ACCOUNT
            auth = mock.Mock(host="rest-example", token="tok", expiration_date=time.time() + 3600,
                             header={"Authorization": "Bearer tok"})

            async def do_http_get(self, path):
                return await http.get(base + path, headers={"Authorization": "Bearer tok"})

        self.blink = _Blink()
        self.engine = core.SyncEngine(st, http)
        self.engine.session.adopt(self.blink)

    def files(self):
        return sorted((p.name, p.read_bytes()) for p in self.out.rglob("*.mp4")) if self.out.exists() else []

    def log(self):
        return self.settings.log_path.read_text(encoding="utf-8")


def run_with_fake(tmp_path, scenario, setup=None):
    """Start a fake Blink, build a Harness, run `await scenario(h)`, tear down."""
    async def main():
        fake = FakeBlink()
        await fake.start()
        try:
            async with ClientSession() as http:
                h = Harness(tmp_path, fake, http)
                if setup:
                    setup(h)
                with mock.patch.object(core, "endpoint_probes", lambda b, s: []):
                    return await scenario(h)
        finally:
            await fake.stop()
    return asyncio.run(main())


@pytest.fixture
def run(tmp_path):
    return lambda scenario, setup=None: run_with_fake(tmp_path, scenario, setup)
