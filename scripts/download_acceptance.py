"""Reproducible local long-media/large-transfer acceptance, without account data.

Run from the repository: uv run python scripts/download_acceptance.py
Bulk fixtures and downloads are always removed; only sanitized JSON is retained.
This exercises the actual Downloader with an exact-origin local fixture policy.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.metadata
import importlib.util
import json
import platform
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MIB = 1024**2
GIB = 1024**3


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(MIB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def peak_rss(who=resource.RUSAGE_SELF):
    value = resource.getrusage(who).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def binary(name):
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"Required local media tool missing: {name}")
    return found


def media_command(arguments, timeout=300):
    result = subprocess.run(
        [binary("ffmpeg"), "-nostdin", "-hide_banner", "-v", "error", *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        # Fixture paths are local and generated; never include any application data.
        raise RuntimeError(f"Fixture FFmpeg failed: {result.stderr[-1200:]}")


def inspect_and_decode(path):
    result = subprocess.run(
        [
            binary("ffprobe"),
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    info = json.loads(result.stdout)
    tracks = [
        {
            key: stream[key]
            for key in ("codec_type", "codec_name", "width", "height", "sample_rate")
            if key in stream
        }
        for stream in info["streams"]
        if stream.get("codec_type") in {"video", "audio"}
    ]
    assert {track["codec_type"] for track in tracks} == {"audio", "video"}
    started = time.monotonic()
    media_command(
        [
            "-xerror",
            "-err_detect",
            "explode",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ]
    )
    return {
        "durationSeconds": float(info["format"]["duration"]),
        "tracks": tracks,
        "fullDecode": "passed",
        "decodeElapsedSeconds": round(time.monotonic() - started, 3),
    }


class DiskSampler:
    """Measure actual fixture/download bytes, independently of transfer buffers."""

    def __init__(self, root):
        self.root = root
        self.peak = 0
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self):
        while not self.stopped.wait(0.05):
            total = 0
            for path in self.root.rglob("*"):
                with contextlib.suppress(FileNotFoundError):
                    if path.is_file():
                        total += path.stat().st_size
            self.peak = max(self.peak, total)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.stopped.set()
        self.thread.join()


class Browser:
    async def cookies_for(self, _site_id, _url):
        return []


class FixturePolicy:
    def __init__(self, origin):
        self.origin = origin
        self.validations = 0

    async def validate(self, url):
        parsed = urlsplit(url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
            raise ValueError("Acceptance requests must stay on the exact local fixture origin")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Acceptance fixture URLs cannot contain credentials or parameters")
        self.validations += 1
        return url


class FixtureServer:
    """Bounded streaming HTTP server; request logs contain no credentials or full URLs."""

    def __init__(self, chunk_delay=0.001):
        self.routes = {}
        self.requests = []
        self.handlers = set()
        self.chunk_delay = chunk_delay

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.origin = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *_args):
        self.server.close()
        await self.server.wait_closed()
        await self.settle()

    async def settle(self):
        if self.handlers:
            _, pending = await asyncio.wait(self.handlers, timeout=3)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def add(self, route, path, mime):
        self.routes[route] = (path, mime, f'"{sha256(path)}"')

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = request.decode("ascii").split("\r\n")
            method, route, _ = lines[0].split(" ")
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            headers = {name.lower(): value for name, value in headers.items()}
            if method != "GET" or route not in self.routes:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            path, mime, etag = self.routes[route]
            size, start, status = path.stat().st_size, 0, 200
            range_header = headers.get("range")
            if range_header and headers.get("if-range") == etag:
                assert range_header.startswith("bytes=") and range_header.endswith("-")
                start = int(range_header[6:-1])
                assert 0 <= start < size
                status = 206
            event = {
                "resource": route,
                "range": range_header,
                "ifRange": headers.get("if-range"),
                "status": status,
                "startOffset": start,
                "declaredBytes": size - start,
                "payloadBytesWritten": 0,
                "completed": False,
            }
            self.requests.append(event)
            response = [
                f"HTTP/1.1 {status} OK",
                f"Content-Type: {mime}",
                f"Content-Length: {size - start}",
                f"ETag: {etag}",
                "Accept-Ranges: bytes",
                "Connection: close",
            ]
            if status == 206:
                response.append(f"Content-Range: bytes {start}-{size - 1}/{size}")
            writer.write(("\r\n".join(response) + "\r\n\r\n").encode("ascii"))
            await writer.drain()
            with path.open("rb") as source:
                source.seek(start)
                while chunk := source.read(64 * 1024):
                    writer.write(chunk)
                    event["payloadBytesWritten"] += len(chunk)
                    await writer.drain()
                    # Keep pause observable; source bytes are never materialized at once.
                    await asyncio.sleep(self.chunk_delay)
            event["completed"] = True
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self.handlers.discard(task)


def asset(server, identifier, route, engine="http"):
    return {
        "id": identifier,
        "siteId": "controlled_fixture",
        "kind": "video",
        "engine": engine,
        "sourceUrl": server.origin + route,
        "pageUrl": server.origin + "/fixture",
        "title": identifier,
    }


def control():
    return SimpleNamespace(paused=False, cancelled=False)


def summarize_requests(requests):
    return {
        "requests": requests,
        "networkPayloadBytesWritten": sum(event["payloadBytesWritten"] for event in requests),
        "networkMeasurement": (
            "HTTP body bytes submitted to the fixture socket, excluding headers; "
            "an interrupted request may include in-flight bytes beyond the persisted prefix."
        ),
    }


def load_subject(path):
    """Freeze the selected downloader revision, including for child processes."""
    path = path.resolve()
    source_bytes = path.read_bytes()
    spec = importlib.util.spec_from_file_location("backend._acceptance_downloader", path)
    subject = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = subject
    exec(compile(source_bytes, str(path), "exec"), subject.__dict__)
    subject._acceptance_source_bytes = source_bytes
    return subject


async def process_worker(config_path):
    """Private child entry point; only fixture configuration is accepted here."""
    config = json.loads(config_path.read_text())
    subject = load_subject(Path(config["source"]))
    data_dir = Path(config["dataDir"])
    media = config["asset"]
    observed = {"resumedBytesReported": 0, "firstResumePartialBytes": None}

    async def progress(event):
        observed["resumedBytesReported"] = max(
            observed["resumedBytesReported"], event.get("resumedBytes", 0)
        )
        if event.get("phase") == "resuming" and observed["firstResumePartialBytes"] is None:
            partial = data_dir / "assets" / media["id"] / "original.part"
            observed["firstResumePartialBytes"] = partial.stat().st_size

    result = await subject.Downloader(
        data_dir, Browser(), FixturePolicy(config["origin"])
    ).download(media, control(), progress)
    print(json.dumps({"result": result, **observed, "peakRssBytes": peak_rss()}), flush=True)


async def process_death(root, evidence, subject):
    """SIGKILL leaves real unsaved bytes; another interpreter validates and resumes."""
    source = root / "process.bmp"
    with Image.new("RGB", (2048, 2048), (23, 101, 189)) as picture:
        picture.save(source)
    source_size, source_hash = source.stat().st_size, sha256(source)
    assert 8 * MIB <= source_size <= 16 * MIB
    frozen_source = root / "downloader-subject.py"
    frozen_source.write_bytes(subject._acceptance_source_bytes)
    data_dir = root / "worker-data"
    partial = data_dir / "assets" / "process" / "original.part"
    checkpoint = partial.with_name(".download.json")
    started = time.monotonic()
    processes = []
    async with FixtureServer(chunk_delay=0.004) as server:
        server.add("/process.bmp", source, "image/bmp")
        config_path = root / "worker.json"
        config_path.write_text(
            json.dumps(
                {
                    "source": str(frozen_source),
                    "dataDir": str(data_dir),
                    "origin": server.origin,
                    "asset": {**asset(server, "process", "/process.bmp"), "kind": "image"},
                }
            )
        )

        async def spawn():
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker-config",
                str(config_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            processes.append(child)
            return child

        try:
            first = await spawn()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if first.returncode is not None:
                    raise AssertionError("Worker exited before publishing a resumable checkpoint")
                if checkpoint.is_file() and partial.is_file():
                    saved = json.loads(checkpoint.read_text())
                    if (
                        saved.get("offset", 0) >= 4 * MIB
                        and partial.stat().st_size >= saved["offset"] + 128 * 1024
                    ):
                        first.kill()
                        break
                await asyncio.sleep(0.002)
            else:
                raise AssertionError("No committed prefix plus uncommitted tail within 15 seconds")
            await asyncio.wait_for(first.communicate(), 5)
            await server.settle()
            saved = json.loads(checkpoint.read_text())
            offset = saved["offset"]
            partial_bytes = partial.stat().st_size
            tail_bytes = partial_bytes - offset
            assert first.returncode == -signal.SIGKILL
            assert offset >= 4 * MIB and 0 < tail_bytes < 4 * MIB
            with partial.open("rb") as prefix:
                assert hashlib.sha256(prefix.read(offset)).hexdigest() == saved["sha256"]
            request_split = len(server.requests)
            second = await spawn()
            stdout, stderr = await asyncio.wait_for(second.communicate(), 30)
            if second.returncode:
                raise AssertionError(
                    f"Fresh worker failed (exit {second.returncode}): {stderr[-500:]!r}"
                )
            worker = json.loads(stdout)
            await server.settle()
            result = worker["result"]
            destination = data_dir / result["path"]
            resumed_requests = server.requests[request_split:]
            output_hash = sha256(destination)
            with Image.open(destination) as picture:
                picture.load()
                assert picture.size == (2048, 2048)
            evidence.update(
                sourceBytes=source_size,
                sourceSha256=source_hash,
                outputBytes=destination.stat().st_size,
                outputSha256=output_hash,
                processDeath="SIGKILL",
                killedWorkerExitCode=first.returncode,
                newProcessVerified=first.pid != second.pid,
                freshWorkerExitCode=second.returncode,
                savedPrefixBytes=offset,
                savedPrefixSha256=saved["sha256"],
                partialBytesAfterKill=partial_bytes,
                uncommittedTailBytes=tail_bytes,
                tailOrigin="Actual streamed bytes beyond the fsynced checkpoint; no injected data",
                firstResumePartialBytes=worker["firstResumePartialBytes"],
                uncommittedTailTruncated=worker["firstResumePartialBytes"] == offset,
                resumedBytesReported=worker["resumedBytesReported"],
                resumedNetworkPayloadBytesWritten=sum(
                    event["payloadBytesWritten"] for event in resumed_requests
                ),
                prefixNotRetransferred=bool(resumed_requests)
                and all(
                    event["status"] == 206 and event["startOffset"] >= offset
                    for event in resumed_requests
                ),
                exactBytesAndSha256=(
                    destination.stat().st_size == source_size
                    and output_hash == source_hash == result["sha256"]
                ),
                fullImageDecode="passed",
                freshWorkerPeakRssBytes=worker["peakRssBytes"],
                elapsedSeconds=round(time.monotonic() - started, 3),
                **summarize_requests(server.requests),
            )
            assert evidence["newProcessVerified"] and evidence["uncommittedTailTruncated"]
            assert evidence["exactBytesAndSha256"] and evidence["prefixNotRetransferred"]
            assert resumed_requests[0]["range"] == f"bytes={offset}-"
            assert resumed_requests[0]["ifRange"] == f'"{source_hash}"'
            assert evidence["resumedNetworkPayloadBytesWritten"] == source_size - offset
            assert worker["resumedBytesReported"] == offset
        finally:
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await process.communicate()


async def large_transfer(root, evidence, subject):
    source = root / "large.avi"
    generated = time.monotonic()
    await asyncio.to_thread(
        media_command,
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=512x512:r=10:d=72",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=8000:duration=72",
            "-c:v",
            "rawvideo",
            "-threads:v",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "pcm_s16le",
            "-shortest",
            str(source),
        ],
    )
    source_size = source.stat().st_size
    source_hash = sha256(source)
    assert source_size >= 256 * MIB
    evidence.update(
        sourceBytes=source_size,
        sourceSha256=source_hash,
        generationElapsedSeconds=round(time.monotonic() - generated, 3),
        generation="72 seconds, 512x512 10 fps rawvideo plus 8 kHz mono PCM in AVI; no padding",
    )
    rss_before = peak_rss()
    started = time.monotonic()
    data_dir = root / "data"
    async with FixtureServer() as server:
        server.add("/large.avi", source, "video/x-msvideo")
        policy = FixturePolicy(server.origin)
        operation = control()
        paused_events = []

        async def pause_after_prefix(event):
            if event.get("downloadedBytes", 0) >= 32 * MIB:
                paused_events.append(dict(event))
                operation.paused = True

        try:
            await subject.Downloader(data_dir, Browser(), policy).download(
                asset(server, "large", "/large.avi"), operation, pause_after_prefix
            )
        except subject.DownloadPaused:
            evidence["pauseObserved"] = True
        else:
            raise AssertionError("The real downloader did not pause")
        await server.settle()
        partial = data_dir / "assets" / "large" / "original.part"
        saved_size = partial.stat().st_size
        assert 0 < saved_size < source_size
        evidence.update(
            savedPrefixBytes=saved_size,
            savedPrefixSha256=sha256(partial),
            pauseProgress=paused_events[-1],
            pauseNetworkPayloadBytesWritten=sum(
                event["payloadBytesWritten"] for event in server.requests
            ),
        )
        request_split = len(server.requests)
        resumed_bytes = 0

        async def observe_resume(event):
            nonlocal resumed_bytes
            resumed_bytes = max(resumed_bytes, event.get("resumedBytes", 0))

        resumed = time.monotonic()
        result = await subject.Downloader(data_dir, Browser(), policy).download(
            asset(server, "large", "/large.avi"), control(), observe_resume
        )
        await server.settle()
        destination = data_dir / result["path"]
        resumed_requests = server.requests[request_split:]
        output_hash = sha256(destination)
        evidence.update(
            outputBytes=destination.stat().st_size,
            outputSha256=output_hash,
            exactBytesAndSha256=(
                destination.stat().st_size == source_size
                and output_hash == source_hash == result["sha256"]
            ),
            resumedBytesReported=resumed_bytes,
            resumedNetworkPayloadBytesWritten=sum(
                event["payloadBytesWritten"] for event in resumed_requests
            ),
            resumeElapsedSeconds=round(time.monotonic() - resumed, 3),
            prefixNotRetransferred=bool(resumed_requests)
            and all(
                event["status"] == 206 and event["startOffset"] >= saved_size
                for event in resumed_requests
            ),
            policyValidations=policy.validations,
            **summarize_requests(server.requests),
        )
        evidence.update(await asyncio.to_thread(inspect_and_decode, destination))
        evidence.update(
            elapsedSeconds=round(time.monotonic() - started, 3),
            downloaderAndFixturePeakRssBytes=peak_rss(),
            downloaderAndFixtureRssGrowthBytes=max(0, peak_rss() - rss_before),
        )
        assert evidence["exactBytesAndSha256"], "Final bytes/hash differ from generated media"
        assert evidence["prefixNotRetransferred"], "A new Downloader retransferred the saved prefix"
        assert resumed_requests[0]["range"] == f"bytes={saved_size}-"
        assert resumed_requests[0]["ifRange"] == f'"{source_hash}"'
        assert evidence["resumedNetworkPayloadBytesWritten"] == source_size - saved_size
        assert resumed_bytes == saved_size, "Resume progress must report reused prefix bytes"
        assert peak_rss() < 256 * MIB, "Downloader/fixture process exceeded the 256 MiB RSS budget"
        assert 71 <= evidence["durationSeconds"] <= 73


async def long_media(root, evidence, subject):
    source = root / "long.mp4"
    generated = time.monotonic()
    await asyncio.to_thread(
        media_command,
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=160x90:r=1:d=3600",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:sample_rate=8000:duration=3600",
            "-c:v",
            "libx264",
            "-threads:v",
            "1",
            "-preset",
            "ultrafast",
            "-crf",
            "40",
            "-g",
            "60",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "12k",
            "-movflags",
            "+faststart",
            "-shortest",
            str(source),
        ],
    )
    hls = root / "hls"
    hls.mkdir()
    await asyncio.to_thread(
        media_command,
        [
            "-i",
            str(source),
            "-map",
            "0",
            "-c",
            "copy",
            "-hls_time",
            "300",
            "-hls_list_size",
            "0",
            "-hls_playlist_type",
            "vod",
            str(hls / "index.m3u8"),
        ],
    )
    evidence.update(
        sourceBytes=source.stat().st_size,
        sourceSha256=sha256(source),
        generationElapsedSeconds=round(time.monotonic() - generated, 3),
        generation="3600 seconds, 160x90 1 fps H.264 plus 8 kHz mono AAC; HTTP and VOD HLS",
        hlsSegmentCount=len(list(hls.glob("*.ts"))),
    )
    async with FixtureServer() as server:
        server.add("/long.mp4", source, "video/mp4")
        for path in hls.iterdir():
            server.add(
                "/hls/" + path.name,
                path,
                "application/vnd.apple.mpegurl" if path.suffix == ".m3u8" else "video/mp2t",
            )
        for engine, route in (("http", "/long.mp4"), ("yt-dlp", "/hls/index.m3u8")):
            started = time.monotonic()
            policy = FixturePolicy(server.origin)
            first_request = len(server.requests)
            data_dir = root / engine

            async def progress(_event):
                pass

            result = await subject.Downloader(data_dir, Browser(), policy).download(
                asset(server, "long", route, engine), control(), progress
            )
            await server.settle()
            destination = data_dir / result["path"]
            details = await asyncio.to_thread(inspect_and_decode, destination)
            details.update(
                outputBytes=destination.stat().st_size,
                outputSha256=sha256(destination),
                elapsedSeconds=round(time.monotonic() - started, 3),
                policyValidations=policy.validations,
                **summarize_requests(server.requests[first_request:]),
            )
            if engine == "http":
                details["exactBytesAndSha256"] = (
                    details["outputBytes"] == evidence["sourceBytes"]
                    and details["outputSha256"] == evidence["sourceSha256"] == result["sha256"]
                )
                assert details["exactBytesAndSha256"]
            evidence[engine] = details
            assert 3599 <= details["durationSeconds"] <= 3602
            shutil.rmtree(data_dir)


async def run(arguments):
    subject = load_subject(arguments.downloader_source)
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "schemaVersion": 1,
        "startedAt": datetime.now(UTC).isoformat(),
        "downloaderSourceSha256": hashlib.sha256(subject._acceptance_source_bytes).hexdigest(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "httpx": importlib.metadata.version("httpx"),
            "ytDlp": importlib.metadata.version("yt-dlp"),
            "ffmpeg": subprocess.run(
                [binary("ffmpeg"), "-version"], capture_output=True, text=True, check=True
            ).stdout.splitlines()[0],
        },
        "limits": {
            "minimumLargeFileBytes": 256 * MIB,
            "peakBulkFileBudgetBytes": GIB,
            "largeTransferPythonRssBudgetBytes": 256 * MIB,
        },
        "limitations": [
            "Synthetic local controlled fixtures do not certify any real website or membership.",
            "Browser cookies are empty; policy allows only the fixture's exact loopback origin.",
            "The 60-minute fixture has intentionally low resolution, frame rate and bitrate.",
            "The large fixture tests streaming byte volume with rawvideo/PCM, not a long codec encode.",
            "The large case uses a new Downloader in the same process; process-death uses SIGKILL "
            "and a fresh Python interpreter. Neither simulates power loss or storage-device failure.",
            "Main-process RSS includes its fixture server; subprocess downloader RSS and child "
            "media tools are reported separately.",
            "Process-wide ru_maxrss is a high-water mark, not a complete memory-growth profile.",
            "Bulk-file peak is sampled every 50 ms; stages allocate at most source plus one output "
            "for the large fixture, and small source/cache/output files for long media.",
            "No production services, account state, credentials, or external websites are accessed.",
        ],
        "cases": {},
    }
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="acceptance-bulk-", dir=output) as temporary:
        bulk = Path(temporary)
        with DiskSampler(bulk) as sampler:
            for name in arguments.cases:
                evidence = {"status": "running"}
                report["cases"][name] = evidence
                case_dir = bulk / name
                case_dir.mkdir()
                print(f"Starting controlled {name} acceptance", flush=True)
                try:
                    await {
                        "large": large_transfer,
                        "long": long_media,
                        "process-death": process_death,
                    }[name](case_dir, evidence, subject)
                    evidence["status"] = "passed"
                except Exception as error:
                    evidence.update(
                        status="failed", errorType=type(error).__name__, error=str(error)
                    )
                finally:
                    shutil.rmtree(case_dir)
                print(f"Controlled {name}: {evidence['status']}", flush=True)
            report["sampledPeakBulkFileBytes"] = sampler.peak
    report.update(
        bulkFilesCleaned=True,
        elapsedSeconds=round(time.monotonic() - started, 3),
        downloaderAndFixturePeakRssBytes=peak_rss(),
        maximumChildProcessRssBytes=peak_rss(resource.RUSAGE_CHILDREN),
        completedAt=datetime.now(UTC).isoformat(),
    )
    passed = all(case["status"] == "passed" for case in report["cases"].values())
    report["status"] = "passed" if passed and report["sampledPeakBulkFileBytes"] < GIB else "failed"
    destination = output / f"{arguments.report_name}.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "report": str(destination)}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=("long", "large", "process-death"),
        default=["long", "large", "process-death"],
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "test-output" / "download-reliability-20261103"
    )
    parser.add_argument("--report-name", default="controlled-media")
    parser.add_argument(
        "--downloader-source", type=Path, default=ROOT / "backend" / "downloader.py"
    )
    parser.add_argument("--worker-config", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.worker_config:
        asyncio.run(process_worker(arguments.worker_config))
        return
    if not arguments.report_name.replace("-", "").replace("_", "").isalnum():
        parser.error("report-name must be a simple filename stem")
    raise SystemExit(asyncio.run(run(arguments)))


if __name__ == "__main__":
    main()
