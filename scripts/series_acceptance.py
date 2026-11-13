"""Controlled series scan/download/archive acceptance; no live site or account.

Run: .venv/bin/python scripts/series_acceptance.py
Real Chromium sees only fulfilled fixture pages. The actual Downloader sends
HTTP bytes only to an ephemeral loopback origin through a script-local transport.
Only the small JSON report survives; synthetic media and SQLite are removed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import re
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import httpx
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backend.downloader as downloader_module  # noqa: E402
from backend.app import create_app  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.downloader import Downloader  # noqa: E402
from backend.scanner import Scanner  # noqa: E402
from scripts.download_acceptance import inspect_and_decode, media_command, sha256  # noqa: E402

ORIGIN = "https://lustflix.tv"
PAGE = ORIGIN + "/#/watch/C03/5"
KEYS = [f"C03:{number}" for number in range(1, 7)]
CLIENT = httpx.AsyncClient
MEDIA_PATH = re.compile(r"/api/media/fixture/([1-5])/([A-Za-z0-9_.-]+)")

HTML = """<!doctype html><title>Controlled C03 series</title>
<section aria-label="Episodes" data-episode-count="6" id="episodes"></section>
<video id="player" preload="none" width="128" height="72"></video>
<script>
document.querySelector('#episodes').innerHTML = Array.from({length:6},(_,i) =>
  `<a href="/#/watch/C03/${i+1}"><h4>Episode ${i+1}</h4>
   ${i===5?'<p>Members only</p>':''}</a>`).join('');
function render() {
  const number = Number(location.hash.split('/').at(-1));
  window.fixtureVisit(number);
  const prefix = `/api/media/fixture/${number}/`;
  const player = document.querySelector('#player');
  player.src = prefix + 'master.m3u8?token=fixture';
  player.innerHTML = `<source src="${prefix}stream_0.m3u8?token=fixture">
    <source src="${prefix}stream_audio0.m3u8?token=fixture">`;
}
addEventListener('hashchange',render); render();
</script>"""


def make_sources(root):
    sources = {}
    for number in range(1, 6):
        folder = root / str(number)
        folder.mkdir(parents=True)
        media_command(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=128x72:rate=10:duration=3",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "10",
                "-f",
                "hls",
                "-hls_time",
                "1",
                "-hls_list_size",
                "0",
                "-hls_segment_filename",
                str(folder / "video_%03d.ts"),
                str(folder / "stream_0.m3u8"),
            ]
        )
        media_command(
            [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={330 + number * 70}:duration=3",
                "-vn",
                "-c:a",
                "aac",
                "-f",
                "hls",
                "-hls_time",
                "1",
                "-hls_list_size",
                "0",
                "-hls_segment_filename",
                str(folder / "audio_%03d.ts"),
                str(folder / "stream_audio0.m3u8"),
            ]
        )
        (folder / "master.m3u8").write_text(
            "#EXTM3U\n#EXT-X-VERSION:3\n"
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",LANGUAGE="en",'
            'DEFAULT=YES,AUTOSELECT=YES,URI="stream_audio0.m3u8"\n'
            "#EXT-X-STREAM-INF:BANDWIDTH=220000,RESOLUTION=128x72,"
            'CODECS="avc1.64000a,mp4a.40.2",AUDIO="audio"\nstream_0.m3u8\n'
        )
        sources.update(
            {
                f"/api/media/fixture/{number}/{path.name}": path.read_bytes()
                for path in folder.iterdir()
            }
        )
    return sources


@contextlib.contextmanager
def local_origin(sources):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            body = sources.get(path)
            status = 200 if body is not None and MEDIA_PATH.fullmatch(path) else 404
            body = body if status == 200 else b""
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Content-Type",
                "application/vnd.apple.mpegurl" if path.endswith(".m3u8") else "video/mp2t",
            )
            self.send_header("ETag", '"' + hashlib.sha256(body).hexdigest() + '"')
            self.end_headers()
            self.wfile.write(body)
            requests.append({"path": path, "status": status, "bytes": len(body)})

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
    )
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


class FixtureTransport(httpx.AsyncBaseTransport):
    """Preserve remote identity while transporting actual bytes over loopback HTTP."""

    def __init__(self, port):
        self.port = port
        self.transport = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        assert request.method == "GET"
        assert request.url.scheme == "https" and request.url.host == "lustflix.tv"
        assert MEDIA_PATH.fullmatch(request.url.path), "Unobserved or locked media requested"
        destination = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        headers = request.headers.copy()
        headers["Host"] = f"127.0.0.1:{self.port}"
        local_request = httpx.Request(
            request.method,
            destination,
            headers=headers,
            stream=request.stream,
            extensions=request.extensions,
        )
        return await self.transport.handle_async_request(local_request)

    async def aclose(self):
        await self.transport.aclose()


class FixturePolicy:
    async def validate(self, url):
        parsed = urlsplit(url)
        assert parsed.scheme == "https" and parsed.hostname == "lustflix.tv"
        assert parsed.port is None and not parsed.username and not parsed.password
        assert parsed.path == "/" or MEDIA_PATH.fullmatch(parsed.path)
        return url


class FixtureBrowser:
    def __init__(self, context):
        self.context = context

    async def open_session(self, _site):
        return {"open": True}

    async def context_for(self, _site_id):
        return self.context

    async def create_scan_context(self, _site):
        return await self.context.browser.new_context(
            storage_state=await self.context.storage_state(indexed_db=True),
            service_workers="block",
            accept_downloads=False,
        )

    async def cookies_for(self, _site_id, _url):
        return []

    async def close(self):
        # Chromium belongs to the outer fixture so Store/TaskManager can be rebuilt.
        pass


async def until(predicate, timeout=90):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)


async def exercise(root, sources, port, requests):
    visits, forbidden_browser_requests = [], []
    policy = FixturePolicy()
    settings = Settings(data_dir=root / "workspace", admin_token="", allowed_hosts=["testserver"])
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(headless=True)
        try:
            context = await chromium.new_context(service_workers="block")
            await context.expose_binding(
                "fixtureVisit", lambda _source, number: visits.append(number)
            )

            async def fulfill(route):
                parsed = urlsplit(route.request.url)
                if parsed.hostname != "lustflix.tv" or route.request.method != "GET":
                    forbidden_browser_requests.append(route.request.url)
                    await route.abort()
                elif parsed.path == "/":
                    await route.fulfill(status=200, content_type="text/html", body=HTML)
                elif parsed.path in sources:
                    await route.fulfill(
                        status=200,
                        content_type="application/vnd.apple.mpegurl"
                        if parsed.path.endswith(".m3u8")
                        else "video/mp2t",
                        body=sources[parsed.path],
                    )
                elif parsed.path == "/favicon.ico":
                    await route.fulfill(status=204)
                else:
                    forbidden_browser_requests.append(route.request.url)
                    await route.abort()

            await context.route("**/*", fulfill)
            await context.route_web_socket("**/*", lambda route: route.close())
            new_context = chromium.new_context

            async def fixture_context(**kwargs):
                created = await new_context(**kwargs)
                await created.expose_binding(
                    "fixtureVisit", lambda _source, number: visits.append(number)
                )
                await created.route("**/*", fulfill)
                await created.route_web_socket("**/*", lambda route: route.close())
                return created

            chromium.new_context = fixture_context
            browser = FixtureBrowser(context)
            scanner = Scanner(browser, policy)

            def fixture_client(*args, **kwargs):
                kwargs.pop("proxy", None)
                return CLIENT(*args, **kwargs, transport=FixtureTransport(port))

            isolated_httpx = SimpleNamespace(**{**vars(httpx), "AsyncClient": fixture_client})
            with patch.object(downloader_module, "httpx", isolated_httpx):
                app = create_app(
                    settings,
                    browser=browser,
                    scanner=scanner,
                    policy=policy,
                    downloader=Downloader(settings.data_dir, browser, policy),
                )
                async with (
                    app.router.lifespan_context(app),
                    CLIENT(
                        transport=httpx.ASGITransport(app=app),
                        base_url="http://testserver",
                    ) as client,
                ):
                    response = await client.post(
                        "/api/scans",
                        json={
                            "siteId": "lustflix",
                            "url": PAGE,
                            "depth": 1,
                            "limit": 6,
                            "kinds": ["video"],
                        },
                    )
                    assert response.status_code == 200, response.text
                    scan_id = response.json()["id"]
                    await until(lambda: not app.state.tasks.scan_tasks)
                    state = app.state.store.read()
                    scan = next(item for item in state["scans"] if item["id"] == scan_id)
                    catalog = scan["catalog"]
                    assert scan["status"] == "completed", scan
                    assert scan["pagesVisited"] == 5 and set(visits) == {1, 2, 3, 4, 5}
                    assert catalog["expectedCount"] == catalog["enumeratedCount"] == 6
                    assert catalog["resolvedCount"] == 5 and catalog["blockedCount"] == 1
                    assert catalog["entries"][5]["mediaStatus"] == "needs-user"
                    assert len(state["assets"]) >= 10, "Master and variants were not discovered"
                    locked = await client.post(
                        f"/api/scans/{scan_id}/episodes", json={"episodeKeys": ["C03:6"]}
                    )
                    assert locked.status_code == 409
                    response = await client.post(
                        f"/api/scans/{scan_id}/downloads", json={"episodeKeys": KEYS}
                    )
                    assert response.status_code == 200, response.text
                    queued = response.json()
                    assert queued["added"] == 5 and queued["unavailableDetails"] == [
                        {"episodeKey": "C03:6", "reason": "needs-user"},
                    ], queued
                    await until(
                        lambda: all(
                            job["status"] in {"completed", "failed", "needs-user"}
                            for job in app.state.store.read()["jobs"]
                        )
                    )
                    before = app.state.store.read()
                    assert len(before["jobs"]) == 5
                    assert all(job["status"] == "completed" for job in before["jobs"]), before[
                        "jobs"
                    ]
                    assert before["scans"][0]["catalog"]["downloadedCount"] == 5
                    paths = {
                        item["id"]: item["_path"]
                        for item in before["assets"]
                        if item["status"] == "downloaded"
                    }

                rebuilt = create_app(
                    settings,
                    browser=browser,
                    scanner=scanner,
                    policy=policy,
                    downloader=Downloader(settings.data_dir, browser, policy),
                )
                assert rebuilt.state.store is not app.state.store
                assert rebuilt.state.tasks is not app.state.tasks
                async with (
                    rebuilt.router.lifespan_context(rebuilt),
                    CLIENT(
                        transport=httpx.ASGITransport(app=rebuilt),
                        base_url="http://testserver",
                    ) as client,
                ):
                    after = rebuilt.state.store.read()
                    assert after["scans"][0]["catalog"] == before["scans"][0]["catalog"]
                    repeat = await client.post(
                        f"/api/scans/{scan_id}/downloads", json={"episodeKeys": KEYS}
                    )
                    assert repeat.json()["added"] == 0 and repeat.json()["duplicates"] == 5
                    response = await client.get(f"/api/scans/{scan_id}/archive")
                    assert response.status_code == 200, response.text
                    outputs = []
                    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                        assert archive.testzip() is None
                        manifest_name = next(
                            name for name in archive.namelist() if name.endswith("manifest.json")
                        )
                        manifest = json.loads(archive.read(manifest_name))
                        assert manifest["savedCount"] == 5 and manifest["expectedCount"] == 6
                        assert (
                            manifest["entries"][5]["state"] == "needs-user"
                            and not manifest["allSaved"]
                        )
                        for entry in manifest["entries"][:5]:
                            number = entry["ordinal"]
                            saved = next(
                                item
                                for item in after["assets"]
                                if item["status"] == "downloaded"
                                and item["pageUrl"].endswith(f"/C03/{number}")
                            )
                            original = settings.data_dir / paths[saved["id"]]
                            body = archive.read(entry["file"])
                            assert body == original.read_bytes()
                            digest = hashlib.sha256(body).hexdigest()
                            assert digest == saved["sha256"] == entry["sha256"] == sha256(original)
                            assert len(body) == saved["sizeBytes"] == entry["bytes"]
                            assert entry["file"].endswith(f"_C03_E{number:03d}_72p.mp4")
                            extracted = root / f"export-{number}.mp4"
                            extracted.write_bytes(body)
                            inspection = await asyncio.to_thread(inspect_and_decode, extracted)
                            assert 2.9 <= inspection["durationSeconds"] <= 3.3
                            attachment = await client.get(
                                f"/api/assets/{saved['id']}/file?download=true"
                            )
                            assert attachment.content == body
                            assert Path(entry["file"]).name in unquote(
                                attachment.headers["content-disposition"]
                            )
                            ranged = await client.get(
                                f"/api/assets/{saved['id']}/file", headers={"Range": "bytes=0-31"}
                            )
                            assert ranged.status_code == 206 and ranged.content == body[:32]
                            assert saved["_path"] == paths[saved["id"]]
                            outputs.append(
                                {
                                    "episodeKey": entry["episodeKey"],
                                    "archiveName": entry["file"],
                                    "sizeBytes": len(body),
                                    "sha256": digest,
                                    **inspection,
                                }
                            )
                        assert (
                            len([name for name in archive.namelist() if name.endswith(".mp4")]) == 5
                        )
                    assert len({item["sha256"] for item in outputs}) == 5
                    assert not list(settings.data_dir.rglob("original.part"))
                    assert not forbidden_browser_requests and 6 not in visits
                    assert requests and all(event["status"] == 200 for event in requests)
                    assert {
                        int(MEDIA_PATH.fullmatch(event["path"])[1]) for event in requests
                    } == set(range(1, 6))
                    return {
                        "scan": {
                            "pagesVisited": scan["pagesVisited"],
                            "pageOrdinals": visits,
                            "enumeratedCount": 6,
                            "resolvedCount": 5,
                            "blockedCount": 1,
                            "lockedPageVisited": False,
                            "lockedMediaRequested": False,
                        },
                        "downloads": {
                            "queued": 5,
                            "completed": 5,
                            "fullDecodedExports": 5,
                            "httpRequests": len(requests),
                            "bodyBytes": sum(r["bytes"] for r in requests),
                        },
                        "restart": {
                            "newStoreAndTaskManager": True,
                            "catalogPreserved": True,
                            "duplicateDownloadsSkipped": 5,
                            "originalPathsPreserved": True,
                        },
                        "archive": {
                            "savedCount": 5,
                            "allSaved": False,
                            "lockedEntryRetained": True,
                            "bytesAndHashesMatch": True,
                            "rangeRequestsPassed": 5,
                        },
                        "outputs": outputs,
                    }
        finally:
            await chromium.close()


async def run(output):
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    report = {
        "startedAt": datetime.now(UTC).isoformat(),
        "limitations": [
            "Synthetic local three-second episodes; no live site, account, entitlement or long-video claim.",
            "Chromium page requests are fulfilled; downloader HTTP uses real loopback bytes via fixture-only URL rewriting.",
            "Restart rebuilds Store/TaskManager after completed downloads in one interpreter; no crash/reboot claim.",
            "Uses the actual Scanner and Downloader; optional yt-dlp page extractor is not exercised.",
        ],
    }
    try:
        with tempfile.TemporaryDirectory(prefix=".series-", dir=output) as temporary:
            root = Path(temporary)
            sources = await asyncio.to_thread(make_sources, root / "sources")
            with local_origin(sources) as (port, requests):
                report.update(await exercise(root, sources, port, requests))
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error), traceback=traceback.format_exc())
    report["temporaryMediaRemoved"] = not any(output.glob(".series-*"))
    report["elapsedSeconds"] = round(time.monotonic() - started, 3)
    destination = output / "report.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(destination),
                "elapsedSeconds": report["elapsedSeconds"],
            }
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "test-output/stage1-series-acceptance"
    )
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(run(arguments.output.resolve())))
