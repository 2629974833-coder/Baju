"""Real Chromium -> installed yt-dlp -> guarded download, using local fixtures."""

import hashlib
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from backend.browser import BrowserService
from backend.downloader import Downloader
from backend.extractor import ExtractorService
from backend.network import NetworkPolicy
from backend.scanner import Scanner


async def test_json_ld_only_video_enters_normal_scan_and_verified_download(tmp_path):
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    fixture = tmp_path / "fixture.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=600",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(fixture),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    media = fixture.read_bytes()
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, dict(self.headers)))
            cookie = None
            status, mime = 200, "text/html"
            if self.path == "/login":
                cookie = "fixture_session=authorized; Path=/; HttpOnly; SameSite=Lax"
                body = b"<title>Local authorized fixture</title>"
            elif self.path == "/episode":
                metadata = json.dumps(
                    {
                        "@context": "https://schema.org",
                        "@type": "VideoObject",
                        "name": "Metadata-only episode",
                        "duration": "PT1S",
                        "contentUrl": origin + "/media.mp4?signature=fixture-only",
                    }
                )
                body = (
                    "<title>Episode</title><p>Video metadata fixture</p>"
                    '<script type="application/ld+json">' + metadata + "</script>"
                ).encode()
            elif self.path == "/media.mp4?signature=fixture-only":
                if (
                    "fixture_session=authorized" not in self.headers.get("Cookie", "")
                    or self.headers.get("Referer") != origin + "/episode"
                ):
                    status, body = 403, b"browser session required"
                else:
                    mime, body = "video/mp4", media
            else:
                status, body = 404, b"not found"
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    policy = NetworkPolicy(allow_private_hosts={"127.0.0.1"})
    browser = BrowserService(tmp_path / "data", policy)
    site = {
        "id": "jsonld_fixture",
        "name": "Fixture",
        "domain": "127.0.0.1",
        "url": origin + "/login",
    }
    assets, events = [], []

    async def found(asset):
        assets.append(asset)

    async def progress(event):
        events.append(event)

    options = {"url": origin + "/episode", "depth": 0, "limit": 1, "kinds": ["video"]}
    try:
        await browser.open_session(site)
        await Scanner(browser, policy).scan(site, options, found, progress, lambda: False)
        assert assets == []  # Neither a DOM media element nor an observed media request exists.
        assert not any(path.startswith("/media.mp4") for path, _ in hits)

        result = await Scanner(browser, policy, ExtractorService(browser, policy)).scan(
            site, options, found, progress, lambda: False
        )
        assert result["status"] == "completed"
        assert len(assets) == 1
        asset = {**assets[0], "id": "metadata_video", "siteId": site["id"]}
        assert asset["title"] == "Metadata-only episode"
        assert asset["pageUrl"] == origin + "/episode"
        assert asset["engine"] == "http"
        assert not any(path.startswith("/media.mp4") for path, _ in hits)
        downloaded = await Downloader(tmp_path / "data", browser, policy).download(
            asset, SimpleNamespace(cancelled=False, paused=False), progress
        )
        output = tmp_path / "data" / downloaded["path"]
        assert output.read_bytes() == media
        assert downloaded["sha256"] == hashlib.sha256(media).hexdigest()
        assert downloaded["width"] == 320 and downloaded["height"] == 180
        assert 0.9 <= downloaded["duration"] <= 1.1
        subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-i", str(output), "-f", "null", "-"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        assert sum(path.startswith("/media.mp4") for path, _ in hits) == 1
    finally:
        await browser.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
