"""Durable transfers exercised against a fixture-owned HTTP server."""

import asyncio
import hashlib
import io
import json
import random
import socket
import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from PIL import Image

from backend.downloader import DownloadCancelled, Downloader, DownloadPaused


class Policy:
    proxy_url = None

    async def validate(self, url):
        if not url.startswith("http://127.0.0.1:"):
            raise ValueError("fixture origin only")
        return url


class Browser:
    async def cookies_for(self, *_args):
        return []


def control():
    return SimpleNamespace(paused=False, cancelled=False)


async def ignore(_event):
    pass


@pytest.fixture
def origin():
    stream = io.BytesIO()
    pixels = random.Random(42).randbytes(512 * 512 * 3)
    Image.frombytes("RGB", (512, 512), pixels).save(stream, "PNG")
    state = SimpleNamespace(
        body=stream.getvalue(),
        etag='"version-1"',
        mode="range",
        requests=[],
        statuses=[],
        retry_after="0",
        disconnects=0,
        routes={},
        modified=None,
        stalled=False,
        stall_started=threading.Event(),
        release=threading.Event(),
        redirect_count=0,
        ignore_conditional=False,
        omit_length=False,
        conditional_encoding=None,
        conditional_length_delta=0,
        stall_conditional=False,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            request = {"path": self.path, "headers": dict(self.headers), "status": 0, "bytes": 0}
            state.requests.append(request)
            if self.path.startswith("/redirect/"):
                number = int(self.path.rsplit("/", 1)[-1])
                if number < state.redirect_count:
                    request["status"] = 302
                    self.send_response(302)
                    self.send_header("Location", f"/redirect/{number + 1}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            status = state.statuses.pop(0) if state.statuses else 200
            if status != 200:
                request["status"] = status
                self.send_response(status)
                self.send_header("Retry-After", state.retry_after)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body, mime, etag = state.routes.get(self.path, (state.body, "image/png", state.etag))
            start = 0
            range_header = self.headers.get("Range")
            if self.headers.get("If-None-Match") == etag and etag and not state.ignore_conditional:
                request["status"] = 304
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            if range_header and state.mode not in {"ignore", "changed"}:
                start = int(range_header.removeprefix("bytes=").rstrip("-"))
                status = 416 if state.mode == "416" or start >= len(body) else 206
            request["status"] = status
            self.send_response(status)
            self.send_header("Content-Type", mime)
            if etag:
                self.send_header("ETag", etag)
            if state.modified:
                self.send_header("Last-Modified", state.modified)
            if self.headers.get("If-None-Match") and state.conditional_encoding:
                self.send_header("Content-Encoding", state.conditional_encoding)
            if status == 416:
                self.send_header("Content-Range", f"bytes */{len(body)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if status == 206:
                offset = start + 1 if state.mode == "malformed" else start
                self.send_header("Content-Range", f"bytes {offset}-{len(body) - 1}/{len(body)}")
            body = body[start:] if status == 206 else body
            if not state.omit_length:
                delta = state.conditional_length_delta if self.headers.get("If-None-Match") else 0
                self.send_header("Content-Length", str(len(body) + delta))
            self.end_headers()
            if state.stalled or (state.stall_conditional and self.headers.get("If-None-Match")):
                state.stall_started.set()
                state.release.wait(5)
            if state.disconnects:
                state.disconnects -= 1
                body = body[:65536]
            try:
                self.wfile.write(body)
                self.wfile.flush()
                request["bytes"] = len(body)
                if len(body) == 65536:
                    self.connection.shutdown(socket.SHUT_RDWR)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    yield state
    state.release.set()
    server.shutdown()
    server.server_close()
    thread.join()


def asset(origin, **extra):
    return {
        "id": "resume_asset",
        "siteId": "fixture",
        "kind": "image",
        "engine": "http",
        "sourceUrl": origin.url + "/image?private=signed-secret",
        **extra,
    }


async def pause_http(tmp_path, origin):
    operation = control()

    async def pause(event):
        if event["downloadedBytes"] >= 65536:
            operation.paused = True

    with pytest.raises(DownloadPaused):
        await Downloader(tmp_path, Browser(), Policy()).download(asset(origin), operation, pause)
    return tmp_path / "assets/resume_asset/original.part"


@pytest.mark.asyncio
async def test_new_downloader_resumes_verified_prefix_with_exact_range(tmp_path, origin):
    partial = await pause_http(tmp_path, origin)
    saved = partial.stat().st_size
    checkpoints = list(partial.parent.glob("*.json")) + list(partial.parent.glob(".*.json"))
    assert checkpoints, "pause must durably persist a checkpoint"
    assert all(path.stat().st_mode & 0o077 == 0 for path in checkpoints + [partial])
    assert all("signed-secret" not in path.read_text() for path in checkpoints)
    events = []

    async def collect(event):
        events.append(event)

    result = await Downloader(tmp_path, Browser(), Policy()).download(
        asset(origin), control(), collect
    )
    headers = origin.requests[-1]["headers"]
    assert headers.get("Range") == f"bytes={saved}-"
    assert headers.get("If-Range") == origin.etag
    assert (tmp_path / result["path"]).read_bytes() == origin.body
    assert result["sha256"] == hashlib.sha256(origin.body).hexdigest()
    assert any(event.get("resumedBytes") == saved for event in events)
    assert "signed-secret" not in json.dumps(events)
    assert not list(partial.parent.glob("*.part"))
    assert not list(partial.parent.glob(".*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ignore", "changed", "malformed", "416"])
async def test_unsafe_range_response_restarts_without_mixing_versions(tmp_path, origin, mode):
    await pause_http(tmp_path, origin)
    origin.mode = mode
    if mode == "changed":
        image = io.BytesIO()
        Image.new("RGB", (20, 20), "red").save(image, "PNG")
        origin.body, origin.etag = image.getvalue(), '"version-2"'
    result = await Downloader(tmp_path, Browser(), Policy()).download(
        asset(origin), control(), ignore
    )
    assert (tmp_path / result["path"]).read_bytes() == origin.body
    assert any("Range" in req["headers"] for req in origin.requests[1:])
    if mode in {"malformed", "416"}:
        assert "Range" not in origin.requests[-1]["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("validator", [None, 'W/"weak-version"'])
async def test_absent_or_weak_validator_never_appends(tmp_path, origin, validator):
    origin.etag = validator
    await pause_http(tmp_path, origin)
    result = await Downloader(tmp_path, Browser(), Policy()).download(
        asset(origin), control(), ignore
    )
    assert "Range" not in origin.requests[-1]["headers"]
    assert (tmp_path / result["path"]).read_bytes() == origin.body


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", ["bytes", "checkpoint"])
async def test_corrupt_prefix_or_checkpoint_restarts_safely(tmp_path, origin, corrupt):
    partial = await pause_http(tmp_path, origin)
    if corrupt == "bytes":
        with partial.open("r+b") as target:
            target.write(b"corrupt")
    else:
        checkpoints = list(partial.parent.glob(".*.json"))
        assert checkpoints, "resume metadata must be persisted"
        checkpoints[0].write_text("{invalid JSON")
    result = await Downloader(tmp_path, Browser(), Policy()).download(
        asset(origin), control(), ignore
    )
    assert "Range" not in origin.requests[-1]["headers"]
    assert (tmp_path / result["path"]).read_bytes() == origin.body


@pytest.mark.asyncio
async def test_transient_status_and_disconnect_retry_from_checkpoint(tmp_path, origin):
    origin.statuses = [503, 429]
    origin.disconnects = 1
    events = []

    async def collect(event):
        events.append(event)

    result = await Downloader(tmp_path, Browser(), Policy()).download(
        asset(origin), control(), collect
    )
    assert (tmp_path / result["path"]).read_bytes() == origin.body
    assert len(origin.requests) == 4
    assert origin.requests[-1]["headers"].get("Range") == "bytes=65536-"
    assert any(event.get("phase") == "retrying" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 403])
async def test_authentication_errors_are_never_retried(tmp_path, origin, status):
    from backend.downloader import DownloadAccessRequired

    origin.statuses = [status] * 10
    with pytest.raises(DownloadAccessRequired, match=str(status)) as error:
        await Downloader(tmp_path, Browser(), Policy()).download(asset(origin), control(), ignore)
    assert error.value.status_code == status
    assert len(origin.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 403])
async def test_access_error_retains_verified_prefix_and_original(tmp_path, origin, status):
    from backend.downloader import DownloadAccessRequired

    partial = await pause_http(tmp_path, origin)
    saved = partial.read_bytes()
    original = partial.parent / "original.png"
    original.write_bytes(b"previous verified original")
    origin.statuses = [status]
    with pytest.raises(DownloadAccessRequired):
        await Downloader(tmp_path, Browser(), Policy()).download(asset(origin), control(), ignore)
    assert partial.read_bytes() == saved
    assert (partial.parent / ".download.json").exists()
    assert original.read_bytes() == b"previous verified original"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["paused", "cancelled"])
async def test_control_during_retry_after_is_prompt_and_preserves_only_pause(
    tmp_path, origin, state
):
    partial = await pause_http(tmp_path, origin)
    origin.statuses, origin.retry_after = [503] * 10, "3600"
    operation = control()

    async def stop(event):
        if event.get("phase") == "retrying":
            assert event["retryInSeconds"] <= 5
            setattr(operation, state, True)

    with pytest.raises(DownloadPaused if state == "paused" else DownloadCancelled):
        await asyncio.wait_for(
            Downloader(tmp_path, Browser(), Policy()).download(asset(origin), operation, stop), 2
        )
    assert partial.exists() == (state == "paused")


@pytest.mark.asyncio
async def test_retry_exhaustion_retains_checkpoint_and_existing_original(tmp_path, origin):
    partial = await pause_http(tmp_path, origin)
    original = partial.parent / "original.png"
    original.write_bytes(b"previous verified original")
    origin.statuses = [503] * 10
    with pytest.raises(ValueError, match="重试"):
        await Downloader(tmp_path, Browser(), Policy()).download(asset(origin), control(), ignore)
    assert len(origin.requests) == 5  # Initial paused request plus four bounded attempts.
    assert partial.exists()
    assert original.read_bytes() == b"previous verified original"


@pytest.mark.asyncio
async def test_process_shutdown_retains_verified_prefix(tmp_path, origin):
    operation = control()
    reached = asyncio.Event()

    async def wait_after_chunk(event):
        if event["downloadedBytes"] >= 65536:
            reached.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(
        Downloader(tmp_path, Browser(), Policy()).download(
            asset(origin), operation, wait_after_chunk
        )
    )
    await asyncio.wait_for(reached.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    partial = tmp_path / "assets/resume_asset/original.part"
    assert partial.exists(), "process shutdown must preserve resumable bytes"
    result = await Downloader(tmp_path, Browser(), Policy()).download(
        asset(origin), control(), ignore
    )
    assert origin.requests[-1]["headers"].get("Range") == "bytes=65536-"
    assert (tmp_path / result["path"]).read_bytes() == origin.body


def hls_fixture(tmp_path, origin, *, encrypted=False, separate_audio=False):
    import shutil
    import subprocess

    binary = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    fixture = tmp_path / "hls-source"
    fixture.mkdir()
    extra = []
    if encrypted:
        (fixture / "key.bin").write_bytes(bytes(range(16)))
        (fixture / "key-info").write_text(f"key.bin\n{fixture / 'key.bin'}\n")
        extra = ["-hls_key_info_file", str(fixture / "key-info")]
    subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x48:r=10:d=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "10",
            "-hls_time",
            "1",
            "-hls_list_size",
            "0",
            "-hls_playlist_type",
            "vod",
            *extra,
            str(fixture / "video.m3u8"),
        ],
        check=True,
    )
    if separate_audio:
        subprocess.run(
            [
                binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=3",
                "-c:a",
                "aac",
                "-hls_time",
                "1",
                "-hls_list_size",
                "0",
                "-hls_playlist_type",
                "vod",
                str(fixture / "audio.m3u8"),
            ],
            check=True,
        )
        (fixture / "index.m3u8").write_text(
            '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Main",DEFAULT=YES,'
            'URI="audio.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=100000,AUDIO="audio"\nvideo.m3u8\n'
        )
    for path in fixture.iterdir():
        body = path.read_bytes()
        mime = (
            "application/vnd.apple.mpegurl"
            if path.suffix == ".m3u8"
            else "application/octet-stream"
        )
        origin.routes["/" + path.name] = (body, mime, '"' + hashlib.sha256(body).hexdigest() + '"')
    return asset(
        origin,
        sourceUrl=origin.url + ("/index.m3u8" if separate_audio else "/video.m3u8"),
        kind="video",
        engine="yt-dlp",
    )


async def pause_hls(data, item):
    operation = control()

    async def pause(event):
        if event.get("completedSegments", 0) >= 1:
            operation.paused = True

    with pytest.raises(DownloadPaused):
        await Downloader(data, Browser(), Policy()).download(item, operation, pause)


@pytest.mark.asyncio
@pytest.mark.parametrize("encrypted,separate_audio", [(False, False), (True, False), (False, True)])
async def test_hls_resume_revalidates_complete_segments_without_payload(
    tmp_path,
    origin,
    encrypted,
    separate_audio,
):
    item = hls_fixture(tmp_path, origin, encrypted=encrypted, separate_audio=separate_audio)
    data = tmp_path / "data"
    await pause_hls(data, item)
    first = next(req for req in origin.requests if req["path"].endswith(".ts"))
    before = len(origin.requests)
    events = []

    async def collect(event):
        events.append(event)

    result = await Downloader(data, Browser(), Policy()).download(item, control(), collect)
    resumed = [req for req in origin.requests[before:] if req["path"] == first["path"]]
    assert len(resumed) == 1
    assert resumed[0]["status"] == 304 and resumed[0]["bytes"] == 0
    assert resumed[0]["headers"].get("If-None-Match")
    assert any(event.get("resumedBytes", 0) >= first["bytes"] for event in events)
    assert result["duration"] >= 2.8
    assert (data / result["path"]).exists()
    assert not (data / "assets/resume_asset/.hls").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["manifest", "cache", "resource", "tail", "complete"])
async def test_hls_manifest_resource_or_cache_change_prevents_stale_reuse(tmp_path, origin, change):
    item = hls_fixture(tmp_path, origin)
    data = tmp_path / "data"
    await pause_hls(data, item)
    first = next(req for req in origin.requests if req["path"].endswith(".ts"))
    before = len(origin.requests)
    if change == "manifest":
        body, mime, _etag = origin.routes["/video.m3u8"]
        origin.routes["/video.m3u8"] = (body + b"#changed\n", mime, '"manifest-v2"')
    elif change == "cache":
        objects = list((data / "assets/resume_asset/.hls").rglob("*.bin"))
        assert objects, "complete segments must have durable cached bytes"
        objects[0].write_bytes(b"corrupt")
    elif change in {"tail", "complete"}:
        cached = next((data / "assets/resume_asset/.hls/objects").glob("*.bin"))
        if change == "tail":
            with cached.open("ab") as target:
                target.write(b"unverified tail")
        else:
            checkpoint = cached.with_suffix(".json")
            metadata = json.loads(checkpoint.read_text())
            retained = cached.read_bytes()[:100]
            cached.write_bytes(retained)
            metadata.update(offset=len(retained), sha256=hashlib.sha256(retained).hexdigest())
            checkpoint.write_text(json.dumps(metadata))
    else:
        body, mime, _etag = origin.routes[first["path"]]
        origin.routes[first["path"]] = (body, mime, '"changed-resource"')
    result = await Downloader(data, Browser(), Policy()).download(item, control(), ignore)
    resumed = [req for req in origin.requests[before:] if req["path"] == first["path"]]
    assert resumed[0]["status"] == 200 and resumed[0]["bytes"] > 0
    if change in {"manifest", "cache", "tail", "complete"}:
        assert "If-None-Match" not in resumed[0]["headers"]
    assert result["duration"] >= 2.8


@pytest.mark.asyncio
async def test_repeated_disconnects_report_unique_retained_bytes(tmp_path, origin):
    origin.disconnects = 3
    events = []

    async def collect(event):
        events.append(event)

    result = await Downloader(tmp_path, Browser(), Policy()).download(
        asset(origin), control(), collect
    )
    assert result["sha256"] == hashlib.sha256(origin.body).hexdigest()
    assert events[-1]["resumedBytes"] == 3 * 65536
    assert events[-1]["retryCount"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("old_enough", [True, False])
async def test_last_modified_needs_strong_date_guard(tmp_path, origin, old_enough):
    origin.etag = None
    origin.modified = format_datetime(
        datetime.now(timezone.utc) - timedelta(seconds=120 if old_enough else 5), usegmt=True
    )
    partial = await pause_http(tmp_path, origin)
    saved = partial.stat().st_size
    result = await Downloader(tmp_path, Browser(), Policy()).download(
        asset(origin), control(), ignore
    )
    headers = origin.requests[-1]["headers"]
    if old_enough:
        assert headers.get("Range") == f"bytes={saved}-"
        assert headers.get("If-Range") == origin.modified
    else:
        assert "Range" not in headers
    assert result["sha256"] == hashlib.sha256(origin.body).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["paused", "cancelled"])
async def test_control_interrupts_stalled_network_without_waiting_for_timeout(
    tmp_path, origin, state
):
    partial = await pause_http(tmp_path, origin)
    saved = partial.stat().st_size
    origin.stalled = True
    operation = control()
    task = asyncio.create_task(
        Downloader(tmp_path, Browser(), Policy()).download(asset(origin), operation, ignore)
    )
    for _ in range(100):
        if origin.stall_started.is_set():
            break
        await asyncio.sleep(0.01)
    assert origin.stall_started.is_set()
    setattr(operation, state, True)
    with pytest.raises(DownloadPaused if state == "paused" else DownloadCancelled):
        await asyncio.wait_for(task, 1)
    assert partial.exists() == (state == "paused")
    if state == "paused":
        assert partial.stat().st_size == saved


@pytest.mark.asyncio
async def test_redirect_budget_does_not_expand_with_resume_support(tmp_path, origin):
    origin.redirect_count = 11
    with pytest.raises(ValueError, match="重定向"):
        await Downloader(tmp_path, Browser(), Policy()).download(
            asset(origin, sourceUrl=origin.url + "/redirect/0"),
            control(),
            ignore,
        )
    assert len(origin.requests) == 11


@pytest.mark.asyncio
async def test_uncommitted_tail_is_discarded_after_checkpoint_prefix_verification(tmp_path, origin):
    partial = await pause_http(tmp_path, origin)
    saved = partial.stat().st_size
    with partial.open("ab") as target:
        target.write(b"uncommitted crash tail")
    result = await Downloader(tmp_path, Browser(), Policy()).download(
        asset(origin), control(), ignore
    )
    assert origin.requests[-1]["headers"].get("Range") == f"bytes={saved}-"
    assert result["sha256"] == hashlib.sha256(origin.body).hexdigest()


@pytest.mark.asyncio
async def test_cancel_cleans_hls_checkpoint_but_preserves_original(tmp_path, origin):
    item = hls_fixture(tmp_path, origin)
    data = tmp_path / "data"
    await pause_hls(data, item)
    folder = data / "assets/resume_asset"
    (folder / "original.mp4").write_bytes(b"previous verified original")
    operation = control()
    operation.cancelled = True
    with pytest.raises(DownloadCancelled):
        await Downloader(data, Browser(), Policy()).download(item, operation, ignore)
    assert not (folder / ".hls").exists()
    assert (folder / "original.mp4").read_bytes() == b"previous verified original"


@pytest.mark.asyncio
async def test_weak_etag_cannot_fall_back_to_last_modified(tmp_path, origin):
    origin.etag = 'W/"weak"'
    origin.modified = format_datetime(datetime.now(timezone.utc) - timedelta(days=2), usegmt=True)
    await pause_http(tmp_path, origin)
    result = await Downloader(tmp_path, Browser(), Policy()).download(
        asset(origin), control(), ignore
    )
    assert "If-Range" not in origin.requests[-1]["headers"]
    assert result["sha256"] == hashlib.sha256(origin.body).hexdigest()


@pytest.mark.asyncio
async def test_retry_and_verify_events_preserve_known_transfer_total(tmp_path, origin):
    origin.disconnects = 1
    events = []

    async def collect(event):
        events.append(event)

    await Downloader(tmp_path, Browser(), Policy()).download(asset(origin), control(), collect)
    retry = next(event for event in events if event.get("phase") == "retrying")
    assert retry["totalBytes"] == len(origin.body)
    assert retry["progress"] > 0
    verifying = next(event for event in events if event.get("phase") == "verifying")
    assert verifying["totalBytes"] == len(origin.body)
    assert verifying["progress"] == 95


@pytest.mark.asyncio
async def test_changed_child_playlist_invalidates_old_objects_before_pause(tmp_path, origin):
    item = hls_fixture(tmp_path, origin, separate_audio=True)
    data = tmp_path / "data"
    body, mime, _etag = origin.routes["/video.m3u8"]
    for revision in range(3):
        origin.routes["/video.m3u8"] = (
            body + f"#revision {revision}\n".encode(),
            mime,
            f'"revision-{revision}"',
        )
        await pause_hls(data, item)
        retained = list((data / "assets/resume_asset/.hls/objects").glob("*.bin"))
        assert len(retained) == 1, "changed child manifests must not accumulate obsolete fragments"


@pytest.mark.asyncio
async def test_child_redirect_target_changes_do_not_accumulate_old_fragments(tmp_path, origin):
    item = hls_fixture(tmp_path, origin, separate_audio=True)
    data = tmp_path / "data"
    master, mime, tag = origin.routes["/index.m3u8"]
    origin.routes["/index.m3u8"] = (master.replace(b"video.m3u8", b"/redirect/0"), mime, tag)
    playlist, mime, tag = origin.routes["/video.m3u8"]
    playlist = b"\n".join(
        b"/" + line if line and not line.startswith(b"#") else line
        for line in playlist.split(b"\n")
    )
    for number in range(1, 4):
        origin.routes[f"/redirect/{number}"] = (playlist, mime, tag)
    for count in range(1, 4):
        origin.redirect_count = count
        await pause_hls(data, item)
        assert len(list((data / "assets/resume_asset/.hls/objects").glob("*.bin"))) == 1


@pytest.mark.asyncio
async def test_disk_write_failure_is_sanitized_and_preserves_verified_original(
    tmp_path,
    origin,
    monkeypatch,
):
    import errno

    folder = tmp_path / "assets/resume_asset"
    folder.mkdir(parents=True)
    original = folder / "original.png"
    original.write_bytes(b"previous verified original")

    def disk_full(*_args):
        raise OSError(errno.ENOSPC, "private fixture path must not escape")

    monkeypatch.setattr(Downloader, "_write_checkpoint", disk_full)
    with pytest.raises(ValueError, match="磁盘") as error:
        await Downloader(tmp_path, Browser(), Policy()).download(asset(origin), control(), ignore)
    assert "private fixture" not in str(error.value)
    assert original.read_bytes() == b"previous verified original"
    assert not (folder / "original.part").exists()


@pytest.mark.asyncio
async def test_cancelled_media_process_does_not_leave_running_child(tmp_path):
    import os
    import sys

    pid_file = tmp_path / "fixture-child.pid"
    task = asyncio.create_task(
        Downloader._process(
            [
                sys.executable,
                "-c",
                "import os,sys,time; from pathlib import Path; "
                "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)",
                str(pid_file),
            ]
        )
    )
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    process_id = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("omit_length", [False, True])
async def test_complete_hls_cache_reuses_matching_strong_etag_on_200(
    tmp_path,
    origin,
    omit_length,
):
    item = hls_fixture(tmp_path, origin)
    data = tmp_path / "data"
    await pause_hls(data, item)
    first = next(req for req in origin.requests if req["path"].endswith(".ts"))
    before = len(origin.requests)
    origin.ignore_conditional = True
    origin.omit_length = omit_length
    events = []
    operation = control()

    async def pause_again(event):
        events.append(event)
        if event.get("completedSegments", 0) >= 1:
            operation.paused = True

    with pytest.raises(DownloadPaused):
        await Downloader(data, Browser(), Policy()).download(item, operation, pause_again)
    resumed = [req for req in origin.requests[before:] if req["path"] == first["path"]]
    assert len(resumed) == 1 and resumed[0]["status"] == 200
    assert resumed[0]["headers"].get("If-None-Match")
    assert events[-1]["resumedBytes"] == first["bytes"]
    assert events[-1]["resumeSupported"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["etag", "weak", "html", "encoding", "length"])
async def test_hls_200_cache_reuse_cannot_bypass_response_validation(tmp_path, origin, invalid):
    item = hls_fixture(tmp_path, origin)
    data = tmp_path / "data"
    await pause_hls(data, item)
    first = next(req for req in origin.requests if req["path"].endswith(".ts"))
    body, mime, etag = origin.routes[first["path"]]
    origin.ignore_conditional = True
    if invalid == "etag":
        etag = '"changed-resource"'
    elif invalid == "weak":
        etag = "W/" + etag
    elif invalid == "html":
        mime = "text/html"
    elif invalid == "encoding":
        origin.conditional_encoding = "gzip"
    else:
        origin.conditional_length_delta = 1
    origin.routes[first["path"]] = (body, mime, etag)
    events = []
    operation = control()

    async def stop(event):
        events.append(event)
        if event.get("completedSegments", 0) >= 1:
            operation.paused = True

    expected = ValueError if invalid in {"html", "encoding", "length"} else DownloadPaused
    with pytest.raises(expected):
        await Downloader(data, Browser(), Policy()).download(item, operation, stop)
    assert not any(event.get("resumedBytes") for event in events)


@pytest.mark.asyncio
async def test_missing_content_length_is_not_reported_as_missing_validator(tmp_path, origin):
    origin.omit_length = True
    operation = control()
    events = []

    async def stop(event):
        events.append(event)
        if event["downloadedBytes"]:
            operation.paused = True

    with pytest.raises(DownloadPaused):
        await Downloader(tmp_path, Browser(), Policy()).download(asset(origin), operation, stop)
    assert "长度" in events[-1]["resumeMessage"]
    assert "未提供可靠续传验证器" not in events[-1]["resumeMessage"]


@pytest.mark.asyncio
async def test_hls_200_strong_validation_does_not_wait_for_response_body(tmp_path, origin):
    item = hls_fixture(tmp_path, origin)
    data = tmp_path / "data"
    await pause_hls(data, item)
    first = next(req for req in origin.requests if req["path"].endswith(".ts"))
    before = len(origin.requests)
    origin.ignore_conditional = True
    origin.omit_length = True
    origin.stall_conditional = True
    await asyncio.wait_for(pause_hls(data, item), 1)
    resumed = next(req for req in origin.requests[before:] if req["path"] == first["path"])
    assert origin.stall_started.is_set()
    assert resumed["status"] == 200 and resumed["bytes"] == 0


@pytest.mark.asyncio
async def test_temporary_proxy_tunnel_failure_resumes_saved_bytes(tmp_path, origin, monkeypatch):
    import httpx

    partial = await pause_http(tmp_path, origin)
    saved = partial.stat().st_size
    send = httpx.AsyncClient.send
    calls = 0

    async def interrupt_once(client, request, **kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ProxyError("502 Bad Gateway")
        return await send(client, request, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "send", interrupt_once)
    result = await Downloader(tmp_path, Browser(), Policy()).download(asset(origin), control(), ignore)
    assert calls == 2 and origin.requests[-1]["headers"]["Range"] == f"bytes={saved}-"
    assert (tmp_path / result["path"]).read_bytes() == origin.body
