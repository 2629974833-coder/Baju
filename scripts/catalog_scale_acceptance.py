"""Loopback-only Chromium acceptance for large catalogs and resumable scanning.

Run with .venv/bin/python scripts/catalog_scale_acceptance.py. Only a JSON report
survives; browser state, SQLite and fixture files use a disposable directory.
Media candidates are synthetic and no download jobs are created.
"""

import argparse
import asyncio
import contextlib
import json
import sys
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app import create_app  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.network import NetworkPolicy  # noqa: E402

EPISODES = 240
PAGE_SIZE = 20


class LoopbackPolicy(NetworkPolicy):
    async def validate(self, url):
        if urlsplit(url).hostname != "127.0.0.1":
            raise ValueError("Scale acceptance only permits its loopback fixture")
        return await super().validate(url)


def catalog_html():
    # Each replacement page repeats its preceding boundary episode. Only the
    # real button reveals later links, so a static DOM read cannot pass.
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<title>Field Notes Scale Fixture</title><h1>Field Notes</h1>
<img width="1" height="1" alt="" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">
<section aria-label="Episodes" data-series-url="/series/field-notes"
 data-series-title="Field Notes Scale Fixture" data-episode-count="{EPISODES}">
 <div id="episodes"></div><button id="more" type="button">Next page</button>
</section>
<aside><h2>Recommendations</h2><a href="/watch/999">Episode 999</a></aside>
<a href="/home">Home</a>
<script>
let windowNumber = 1;
function render() {{
  const first = Math.max(1, (windowNumber - 1) * {PAGE_SIZE});
  const last = Math.min({EPISODES}, windowNumber * {PAGE_SIZE});
  document.getElementById('episodes').innerHTML = Array.from(
    {{length: last - first + 1}}, (_, offset) => {{
      const number = first + offset;
      return `<a href="/watch/${{number}}" data-episode-number="${{number}}">Episode ${{number}}</a> `;
    }}).join('');
  document.getElementById('more').disabled = last === {EPISODES};
}}
document.getElementById('more').onclick = () => {{
  windowNumber += 1;
  fetch('/catalog-window/' + windowNumber);
  render();
}};
render();
</script></html>""".encode()


@contextlib.contextmanager
def fixture_origin():
    requests = []
    paused_request, release_request = threading.Event(), threading.Event()
    block_next_five = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            requests.append(path)
            if path == "/watch/5" and block_next_five.is_set():
                block_next_five.clear()
                paused_request.set()
                if not release_request.wait(timeout=15):
                    self.send_error(504, "Fixture cancellation barrier timed out")
                    return
            mime, status = "text/html; charset=utf-8", 200
            if path == "/series/field-notes":
                body = catalog_html()
            elif path.startswith("/watch/") and path.removeprefix("/watch/").isdigit():
                number = int(path.removeprefix("/watch/"))
                body = (
                    f"<!doctype html><title>Field Notes {number}</title>"
                    f'<main><video controls preload="none" src="/media/{number}.mp4">'
                    "</video></main>"
                ).encode()
            elif path.startswith("/catalog-window/"):
                body, mime = b"page revealed", "text/plain"
            elif path.startswith("/media/"):
                body, mime = b"synthetic candidate; no download acceptance", "video/mp4"
            else:
                body, status = b"not found", 404
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}/series/field-notes",
            requests,
            block_next_five,
            paused_request,
            release_request,
        )
    finally:
        release_request.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


async def poll(client, predicate, *, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await client.get("/api/state")
        response.raise_for_status()
        state = response.json()
        if predicate(state):
            return state
        await asyncio.sleep(0.05)
    raise AssertionError({"message": "Acceptance timed out", "scans": state["scans"]})


async def post(client, path, payload=None):
    response = await client.post(path, json=payload or {})
    assert response.status_code == 200, (path, response.status_code, response.text)
    return response.json()


def catalog_identity(state, *, identifier=None, resolved):
    assert len(state["series"]) == 1
    row = state["series"][0]
    if identifier:
        assert row["id"] == identifier
    catalog = row["catalog"]
    entries = catalog["entries"]
    assert catalog["expectedCount"] == EPISODES
    assert catalog["enumeratedCount"] == EPISODES
    assert [entry["ordinal"] for entry in entries] == list(range(1, EPISODES + 1))
    assert len({entry["episodeKey"] for entry in entries}) == EPISODES
    assert len({entry["pageUrl"] for entry in entries}) == EPISODES
    assert catalog["observedGapOrdinals"] == []
    assert catalog["resolvedCount"] == resolved, catalog["resolvedCount"]
    assert catalog["status"] != "complete-observed"
    assert len(state["assets"]) == resolved and state["jobs"] == []
    return row["id"], [(entry["episodeKey"], entry["pageUrl"]) for entry in entries]


async def verify(data_dir, fixture):
    started = time.monotonic()
    url, requests, block_next_five, paused_request, release_request = fixture

    def application():
        return create_app(
            Settings(data_dir=data_dir, admin_token=""),
            policy=LoopbackPolicy(allow_private_hosts={"127.0.0.1"}),
        )

    async def finished(client, app, scan_id, *, status="completed"):
        state = await poll(
            client,
            lambda state: (
                scan_id not in app.state.tasks.scan_tasks
                and next(scan for scan in state["scans"] if scan["id"] == scan_id)["status"]
                not in {"queued", "running"}
            ),
        )
        scan = next(scan for scan in state["scans"] if scan["id"] == scan_id)
        assert scan["status"] == status, scan
        return state

    rounds = []
    app = application()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client:
            initial = await post(client, "/api/series", {"url": url, "limit": 2})
            state = await finished(client, app, initial["id"])
            identifier, identity = catalog_identity(state, resolved=1)
            assert state["scans"][0]["pagesVisited"] == 2
            rounds.append({"limit": 2, "resolved": 1, "status": "completed"})
            assert [path for path in requests if path.startswith("/catalog-window/")] == [
                f"/catalog-window/{number}" for number in range(2, EPISODES // PAGE_SIZE + 1)
            ]
            prefix = f"/api/series/{identifier}/scan"
            continuation = await post(client, prefix, {"limit": 2})
            state = await finished(client, app, continuation["id"])
            assert catalog_identity(state, identifier=identifier, resolved=3)[1] == identity
            rounds.append({"limit": 2, "resolved": 3, "status": "completed"})

            block_next_five.set()
            interrupted = await post(client, prefix, {"limit": 3})
            await poll(client, lambda _state: paused_request.is_set())
            try:
                await post(client, f"/api/scans/{interrupted['id']}/cancel")
            finally:
                release_request.set()
            state = await finished(client, app, interrupted["id"], status="cancelled")
            assert catalog_identity(state, identifier=identifier, resolved=4)[1] == identity
            assert "/watch/6" not in requests
            rounds.append({"limit": 3, "resolved": 4, "status": "cancelled"})

    restarted = application()
    assert restarted.state.store is not app.state.store
    persisted = restarted.state.store.public_state()
    assert catalog_identity(persisted, identifier=identifier, resolved=4)[1] == identity
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(restarted), base_url="http://testserver"
        ) as client:
            resumed = await post(client, prefix, {"limit": 2})
            state = await finished(client, restarted, resumed["id"])
            assert catalog_identity(state, identifier=identifier, resolved=6)[1] == identity
            rounds.append({"limit": 2, "resolved": 6, "status": "completed-after-reopen"})

    page_hits = Counter(path for path in requests if path.startswith("/watch/"))
    assert page_hits == Counter({f"/watch/{n}": 2 if n == 5 else 1 for n in range(1, 7)})
    assert "/home" not in requests and "/watch/999" not in requests
    return {
        "passed": True,
        "elapsedSeconds": round(time.monotonic() - started, 2),
        "enumerated": EPISODES,
        "expectedCount": EPISODES,
        "paginationClicks": EPISODES // PAGE_SIZE - 1,
        "orderedAndDeduplicated": True,
        "recommendationsExcluded": True,
        "catalogStableAcrossRounds": True,
        "cancelledCatalogPreserved": True,
        "resumedAfterStoreReopen": True,
        "resolved": 6,
        "downloadJobs": 0,
        "rounds": rounds,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="test-output/catalog-scale-acceptance.json")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="framekeep-catalog-scale-") as directory:
        with fixture_origin() as fixture:
            report = asyncio.run(verify(Path(directory) / "workspace", fixture))
    report["temporaryWorkspaceRemoved"] = not Path(directory).exists()
    assert report["temporaryWorkspaceRemoved"]
    path = ROOT / args.report
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
