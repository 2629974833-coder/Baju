"""Neutral, loopback-only whole-series acceptance with the actual browser and downloader.

Run directly for assertions/report, or --serve for manual UI verification at port 4196.
All SQLite/session/media data live in a disposable temporary directory.
"""

import argparse
import asyncio
import contextlib
import io
import json
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app import create_app  # noqa: E402
from backend.config import Settings  # noqa: E402
from scripts.download_acceptance import inspect_and_decode, media_command, sha256  # noqa: E402


def sources(root, structured=False):
    result = {}
    media_count = 6 if structured else 5
    for number in range(1, media_count + 1):
        path = root / f"clip-{number}.mp4"
        media_command(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=160x90:rate=10:duration=3",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={330 + number * 80}:duration=3",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                "-shortest",
                str(path),
            ]
        )
        result[f"/media/clip-{number}.mp4"] = path.read_bytes()
    links = "".join(
        f'<a href="/garden/{n}" data-episode-number="{n}">第{n}集'
        + (" <span>Members only</span>" if n == 6 else "")
        + "</a> "
        for n in range(1, 7)
    )
    directory = (
        '<section aria-label="Episodes" data-series-url="/series/garden" '
        'data-series-title="花园观察 · 测试课程" data-episode-count="6">'
        "<h2>花园观察 · 测试课程</h2>" + links + "</section>"
    )
    if structured:
        data = {
            "@context": "https://schema.org",
            "@type": "TVSeries",
            "url": "/series/garden",
            "name": "花园观察 · 测试课程",
            "numberOfEpisodes": 6,
            "episode": [
                {
                    "@type": "TVEpisode",
                    "episodeNumber": n,
                    "url": f"/garden/{n}",
                    "name": f"第{n}集",
                }
                for n in range(1, 7)
            ],
        }
        directory = '<script type="application/ld+json">' + json.dumps(data) + "</script>"
    for number in range(0, 7):
        page = "/series/garden" if number == 0 else f"/garden/{number}"
        video = (
            f'<main><video controls preload="none" src="/media/clip-{number}.mp4"></video></main>'
            if 1 <= number <= media_count
            else ""
        )
        result[page] = (
            '<!doctype html><html lang="zh"><meta charset="utf-8">'
            "<title>花园观察 · 测试课程</title><h1>花园观察 · 测试课程</h1>"
            + directory
            + video
            + '<a href="/unrelated">Other series</a></html>'
        ).encode()
    return result


@contextlib.contextmanager
def fixture_origin(files, port=0, structured=False):
    requests = []
    access_ready = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            body = files.get(path, b"")
            status = 200 if path in files else 404
            if (
                structured
                and path in {"/garden/6", "/media/clip-6.mp4"}
                and not access_ready.is_set()
            ):
                status, body = 403, b"Members only"
            self.send_response(status)
            self.send_header(
                "Content-Type", "video/mp4" if path.endswith(".mp4") else "text/html; charset=utf-8"
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            requests.append(path)

        def do_POST(self):
            if structured and self.path == "/fixture/allow":
                access_ready.set()
                self.send_response(204)
            else:
                self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/series/garden", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


async def verify(app, url, requests, data_dir, structured=False):
    started = time.monotonic()
    expected_saved = 6 if structured else 5

    async def poll(client, predicate):
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            state = (await client.get("/api/state")).json()
            if predicate(state):
                return state
            await asyncio.sleep(0.15)
        raise AssertionError({"scans": state["scans"], "jobs": state["jobs"]})

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client:
            response = await client.post("/api/series", json={"url": url})
            assert response.status_code == 200, response.text
            state = await poll(
                client, lambda s: s["scans"][0]["status"] not in {"queued", "running"}
            )
            assert len(state["series"]) == 1, state["scans"]
            row = state["series"][0]
            catalog = row["catalog"]
            assert catalog["enumeratedCount"] == 6, catalog
            assert catalog["resolvedCount"] == 5, catalog
            assert catalog["blockedCount"] == 1, catalog
            assert "/unrelated" not in requests
            if structured:
                assert catalog["discoverySources"] == ["json-ld"]
            else:
                assert "/garden/6" not in requests
            prefix = f"/api/series/{row['id']}"
            if structured:
                still_blocked = await client.post(prefix + "/scan", json={"recheckAccess": True})
                assert still_blocked.status_code == 200
                state = await poll(
                    client, lambda s: s["scans"][0]["status"] not in {"queued", "running"}
                )
                assert state["series"][0]["catalog"]["blockedCount"] == 1
                async with httpx.AsyncClient(trust_env=False) as fixture:
                    allowed = await fixture.post(url.rsplit("/series/", 1)[0] + "/fixture/allow")
                    assert allowed.status_code == 204
                before_requests = len(requests)
                checked = await client.post(prefix + "/scan", json={"recheckAccess": True})
                assert (
                    checked.status_code == 200 and len(checked.json()["recheckedEpisodeKeys"]) == 1
                )
                state = await poll(
                    client, lambda s: s["scans"][0]["status"] not in {"queued", "running"}
                )
                catalog = state["series"][0]["catalog"]
                assert catalog["blockedCount"] == 0 and catalog["resolvedCount"] == 6, catalog
                assert not any(
                    path.startswith("/garden/") and path != "/garden/6"
                    for path in requests[before_requests:]
                )
            batch = (await client.post(prefix + "/downloads", json={})).json()
            assert batch["added"] == expected_saved, batch
            state = await poll(
                client,
                lambda s: (
                    len(s["jobs"]) == expected_saved
                    and all(j["status"] not in {"running", "queued"} for j in s["jobs"])
                ),
            )
            assert all(job["status"] == "completed" for job in state["jobs"]), state["jobs"]
            saved = []
            for asset in app.state.store.read()["assets"]:
                if asset.get("status") != "downloaded":
                    continue
                path = data_dir / asset["_path"]
                assert sha256(path) == asset["sha256"]
                inspection = await asyncio.to_thread(inspect_and_decode, path)
                ranged = await client.get(asset["fileUrl"], headers={"Range": "bytes=0-127"})
                assert ranged.status_code == 206 and len(ranged.content) == 128
                saved.append(
                    {"id": asset["id"], "sha256": asset["sha256"], "inspection": inspection}
                )
            repeated = (await client.post(prefix + "/downloads", json={})).json()
            assert repeated["added"] == 0 and repeated["duplicates"] == expected_saved, repeated
            response = await client.get(prefix + "/archive")
            assert response.status_code == 200
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                assert len(archive.namelist()) == expected_saved + 1
                archive_names = archive.namelist()
            identifier = row["id"]
    restarted = create_app(Settings(data_dir=data_dir, allow_private_hosts={"127.0.0.1"}))
    persisted = restarted.state.store.public_state()["series"]
    assert len(persisted) == 1 and persisted[0]["id"] == identifier
    assert persisted[0]["catalog"]["downloadedCount"] == expected_saved
    return {
        "passed": True,
        "elapsedSeconds": round(time.monotonic() - started, 2),
        "enumerated": 6,
        "saved": len(saved),
        "blocked": 0 if structured else 1,
        "structuredAndAccessRecheck": structured,
        "repeatDuplicates": expected_saved,
        "persistedAfterReopen": True,
        "archiveEntries": archive_names,
        "files": saved,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--structured", action="store_true")
    parser.add_argument("--port", type=int, default=4196)
    parser.add_argument("--fixture-port", type=int, default=0)
    parser.add_argument("--report", default="test-output/series-library-acceptance.json")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="framekeep-neutral-series-") as folder:
        root = Path(folder)
        with fixture_origin(sources(root, args.structured), args.fixture_port, args.structured) as (
            url,
            requests,
        ):
            settings = Settings(data_dir=root / "workspace", allow_private_hosts={"127.0.0.1"})
            app = create_app(settings)
            if args.serve:
                print(
                    json.dumps(
                        {"panel": f"http://127.0.0.1:{args.port}/#series", "fixtureUrl": url}
                    ),
                    flush=True,
                )
                uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
            else:
                report = asyncio.run(verify(app, url, requests, settings.data_dir, args.structured))
                path = ROOT / args.report
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(
                    json.dumps(
                        {key: value for key, value in report.items() if key != "files"},
                        ensure_ascii=False,
                    )
                )


if __name__ == "__main__":
    main()
