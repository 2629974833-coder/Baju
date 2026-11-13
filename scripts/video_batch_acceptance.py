"""Bounded HTTP/HLS/DASH video queue acceptance using only synthetic local media.

Run: uv run python scripts/video_batch_acceptance.py --rounds 1
Each round has 24 tasks at concurrency four, transient and permanent failures,
and a graceful queue restart with validated DASH checkpoint reuse. Media and
SQLite files are temporary; only the small JSON evidence report is retained.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.metadata
import json
import re
import sys
import tempfile
import time
import traceback
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.downloader import Downloader  # noqa: E402
from backend.store import Store  # noqa: E402
from backend.tasks import TaskManager  # noqa: E402
from scripts.download_acceptance import (  # noqa: E402
    FixturePolicy,
    inspect_and_decode,
    media_command,
    sha256,
)

TASK_COUNT = 24
CONCURRENCY = 4
TERMINAL = {"completed", "failed", "needs-user"}


class BatchFailure(AssertionError):
    def __init__(self, evidence):
        self.evidence = evidence
        super().__init__("Unexpected video queue terminal states; see failedRound evidence")


def make_sources(folder):
    folder.mkdir()
    source = folder / "source.mp4"
    media_command(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=128x72:rate=10:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "10",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(source),
        ]
    )
    hls, dash = folder / "hls", folder / "dash"
    hls.mkdir()
    dash.mkdir()
    media_command(
        [
            "-i",
            str(source),
            "-map",
            "0",
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_list_size",
            "0",
            "-hls_segment_filename",
            str(hls / "segment_%03d.ts"),
            str(hls / "index.m3u8"),
        ]
    )
    media_command(
        [
            "-i",
            str(source),
            "-map",
            "0",
            "-c",
            "copy",
            "-seg_duration",
            "1",
            "-use_template",
            "1",
            "-use_timeline",
            "1",
            "-adaptation_sets",
            "id=0,streams=v id=1,streams=a",
            "-f",
            "dash",
            str(dash / "index.mpd"),
        ]
    )
    return {
        "http": {"source.mp4": source.read_bytes()},
        "hls": {path.name: path.read_bytes() for path in hls.iterdir()},
        "dash": {path.name: path.read_bytes() for path in dash.iterdir()},
    }


def protocol_for(index):
    if index == 0:
        return "dash"
    if index in {1, 4}:
        return "http"
    if index == 2:
        return "hls"
    if index == 3:
        return "dash"
    return ("http", "hls", "dash")[index % 3]


def entry_for(protocol):
    return {"http": "source.mp4", "hls": "index.m3u8", "dash": "index.mpd"}[protocol]


class LocalOrigin:
    """Exact-origin HTTP with real conditional responses and deterministic failures."""

    def __init__(self, sources):
        self.sources = sources
        self.requests = []
        self.attempts = Counter()
        self.handlers = set()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=16384)
        self.origin = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *_args):
        self.server.close()
        await self.server.wait_closed()
        if self.handlers:
            _, pending = await asyncio.wait(self.handlers, timeout=3)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = request.decode("ascii").split("\r\n")
            method, route, _ = lines[0].split(" ")
            headers = {
                name.lower(): value
                for name, value in (line.split(": ", 1) for line in lines[1:] if ": " in line)
            }
            match = re.fullmatch(r"/job/(\d+)/([A-Za-z0-9_.-]+)", route)
            if method != "GET" or not match or not 0 <= int(match[1]) < TASK_COUNT:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            index, filename = int(match[1]), match[2]
            protocol = protocol_for(index)
            body = self.sources[protocol].get(filename)
            self.attempts[route] += 1
            status = 200 if body is not None else 404
            if index == 3:
                status = 403
            elif index in {1, 2} and filename == entry_for(protocol) and self.attempts[route] == 1:
                status = {1: 429, 2: 503}[index]
            elif index == 4:
                body = b"not a video; controlled permanent corruption"
            body = body or b""
            etag = '"' + hashlib.sha256(body).hexdigest() + '"'
            full_size, offset = len(body), 0
            if status == 200 and headers.get("if-none-match") == etag:
                status, body = 304, b""
            elif status == 200 and headers.get("if-range") == etag and headers.get("range"):
                requested = re.fullmatch(r"bytes=(\d+)-", headers["range"])
                offset = int(requested[1]) if requested else full_size
                if offset >= full_size:
                    status, body = 416, b""
                else:
                    status, body = 206, body[offset:]
            elif status != 200:
                body = b""
            mime = {
                ".mp4": "video/mp4",
                ".m3u8": "application/vnd.apple.mpegurl",
                ".mpd": "application/dash+xml",
            }.get(Path(filename).suffix, "application/octet-stream")
            response = [
                f"HTTP/1.1 {status} Fixture",
                f"Content-Length: {len(body)}",
                f"Content-Type: {mime}",
                f"ETag: {etag}",
                "Accept-Ranges: bytes",
                "Connection: close",
            ]
            if status in {429, 503}:
                response.append("Retry-After: 0")
            if status == 206:
                response.append(f"Content-Range: bytes {offset}-{full_size - 1}/{full_size}")
            elif status == 416:
                response.append(f"Content-Range: bytes */{full_size}")
            event = {
                "jobIndex": index,
                "protocol": protocol,
                "status": status,
                "resource": filename,
                "bytesWritten": 0,
            }
            self.requests.append(event)
            writer.write(("\r\n".join(response) + "\r\n\r\n").encode())
            await writer.drain()
            for start in range(0, len(body), 16384):
                chunk = body[start : start + 16384]
                writer.write(chunk)
                event["bytesWritten"] += len(chunk)
                await writer.drain()
                await asyncio.sleep(0.001)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self.handlers.discard(task)


class Browser:
    async def open_session(self, _site):
        pass

    async def cookies_for(self, _site_id, _url):
        return []


async def check_origin_range_contract(sources):
    """A completed prefix may legitimately resume at EOF; fixture HTTP must be correct."""
    async with LocalOrigin(sources) as origin:
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            url = origin.origin + "/job/6/source.mp4"
            original = await client.get(url)
            assert original.status_code == 200
            for offset in (len(original.content), len(original.content) + 1):
                response = await client.get(
                    url, headers={"Range": f"bytes={offset}-", "If-Range": original.headers["etag"]}
                )
                assert response.status_code == 416
                assert response.headers["content-range"] == f"bytes */{len(original.content)}"
                assert response.content == b""
            response = await client.get(
                url, headers={"Range": "bytes=1-", "If-Range": original.headers["etag"]}
            )
            assert response.status_code == 206
            assert response.content == original.content[1:]
    return {"eofRange": 416, "pastEofRange": 416, "validRange": 206}


class GateResolver:
    """A controlled gate deliberately stays closed; no external account is used."""

    calls = 0

    async def resolve_asset(self, _site, _asset, _cancelled):
        self.calls += 1
        return {"status": "needs-user", "reasonCode": "access-required"}


async def until(predicate, timeout=120):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def run_round(folder, sources, number):
    started = time.monotonic()
    paused = asyncio.Event()
    meter = {"active": 0, "peak": 0, "pauseRequested": False}
    store = Store(folder / "workspace.sqlite3")
    browser, resolver = Browser(), GateResolver()
    async with LocalOrigin(sources) as origin:
        policy = FixturePolicy(origin.origin)

        class ObservedDownloader(Downloader):
            async def download(self, asset, control, progress):
                meter["active"] += 1
                meter["peak"] = max(meter["peak"], meter["active"])

                async def observe(values):
                    await progress(values)
                    if (
                        asset["id"] == "video_00"
                        and values.get("completedSegments", 0) >= 2
                        and not meter["pauseRequested"]
                    ):
                        meter["pauseRequested"] = True
                        job = next(
                            job for job in store.read()["jobs"] if job["assetId"] == asset["id"]
                        )
                        manager.job_action(job["id"], "pause")
                        paused.set()

                try:
                    return await super().download(asset, control, observe)
                finally:
                    meter["active"] -= 1

        def seed(state):
            state["settings"]["concurrency"] = CONCURRENCY
            state["sites"] = [
                {"id": "fixture", "name": "Local video acceptance", "url": origin.origin}
            ]
            state["assets"] = [
                {
                    "id": f"video_{index:02}",
                    "siteId": "fixture",
                    "kind": "video",
                    "title": f"Controlled video {index}",
                    "status": "discovered",
                    "tags": [],
                    "sourceUrl": f"{origin.origin}/job/{index}/{entry_for(protocol_for(index))}",
                    "pageUrl": f"{origin.origin}/fixture",
                    "engine": "http" if protocol_for(index) == "http" else "yt-dlp",
                }
                for index in range(TASK_COUNT)
            ]

        store.mutate(seed)
        manager = TaskManager(store, browser, resolver, ObservedDownloader(folder, browser, policy))
        await manager.start()
        try:
            assert (
                manager.enqueue([asset["id"] for asset in store.read()["assets"]])["added"]
                == TASK_COUNT
            )
            await asyncio.wait_for(paused.wait(), 30)
            await until(
                lambda: (
                    not any(
                        job["assetId"] == "video_00" and job["id"] in manager.active
                        for job in store.read()["jobs"]
                    )
                )
            )
            # Complete actual transient retries before the deliberate queue
            # restart can reset a fresh transfer's per-attempt counters.
            await until(
                lambda: all(
                    job["status"] == "completed"
                    for job in store.read()["jobs"]
                    if job["assetId"] in {"video_01", "video_02"}
                )
            )
        finally:
            await manager.close()
        paused_before_restart = [
            job["id"] for job in store.read()["jobs"] if job["status"] == "paused"
        ]
        assert paused_before_restart
        checkpoint_root = folder / "assets/video_00/.hls/objects"
        assert any(checkpoint_root.glob("*.json")), "DASH pause did not save checkpoints"
        before_restart = len(origin.requests)
        store = Store(folder / "workspace.sqlite3")
        manager = TaskManager(store, browser, resolver, ObservedDownloader(folder, browser, policy))
        await manager.start()
        try:
            for job_id in paused_before_restart:
                manager.job_action(job_id, "resume")
            await until(lambda: all(job["status"] in TERMINAL for job in store.read()["jobs"]))
        finally:
            await manager.close()
        state = store.read()
        jobs = {job["assetId"]: job for job in state["jobs"]}
        counts = Counter(job["status"] for job in jobs.values())
        if counts != {"completed": 22, "failed": 1, "needs-user": 1}:
            raise BatchFailure(
                {
                    "round": number,
                    "jobStates": dict(counts),
                    "jobs": [
                        {
                            key: job.get(key)
                            for key in (
                                "assetId",
                                "status",
                                "phase",
                                "error",
                                "failureCode",
                                "refreshCount",
                                "resumedBytes",
                                "retryCount",
                                "completedSegments",
                                "totalSegments",
                            )
                        }
                        for job in jobs.values()
                    ],
                    "pausedBeforeRestart": paused_before_restart,
                    "requests": origin.requests,
                }
            )
        assert jobs["video_03"]["failureCode"] == "access-required"
        assert jobs["video_04"]["status"] == "failed"
        assert resolver.calls == 1
        assert meter["peak"] == CONCURRENCY, meter
        assert jobs["video_00"].get("resumedBytes", 0) > 0
        reuse = [
            event
            for event in origin.requests[before_restart:]
            if event["jobIndex"] == 0 and event["status"] == 304
        ]
        assert len(reuse) >= 2 and all(event["bytesWritten"] == 0 for event in reuse)
        assert any(event["status"] == 429 for event in origin.requests)
        assert any(event["status"] == 503 for event in origin.requests)
        assert jobs["video_01"].get("retryCount", 0) >= 1
        assert jobs["video_02"].get("retryCount", 0) >= 1
        verified = []
        for asset in state["assets"]:
            if asset["status"] != "downloaded":
                assert not (folder / "assets" / asset["id"] / "original.mp4").exists()
                continue
            target = folder / asset["_path"]
            assert target.stat().st_size == asset["sizeBytes"]
            digest = sha256(target)
            assert digest == asset["sha256"]
            index = int(asset["id"].split("_")[1])
            if protocol_for(index) == "http":
                assert digest == hashlib.sha256(sources["http"]["source.mp4"]).hexdigest()
            inspection = await asyncio.to_thread(inspect_and_decode, target)
            assert 2.9 <= inspection["durationSeconds"] <= 3.3
            verified.append(
                {
                    "assetId": asset["id"],
                    "protocol": protocol_for(index),
                    "sizeBytes": asset["sizeBytes"],
                    "sha256": digest,
                    **inspection,
                }
            )
        assert len(verified) == 22
        assert not list((folder / "assets").rglob("original.part"))
        return {
            "round": number,
            "status": "passed",
            "elapsedSeconds": round(time.monotonic() - started, 3),
            "taskCount": TASK_COUNT,
            "concurrencyLimit": CONCURRENCY,
            "peakActiveTransfers": meter["peak"],
            "jobStates": dict(counts),
            "fullyDecodedOutputs": len(verified),
            "outputs": verified,
            "gracefulRestart": {
                "pausedJobs": len(paused_before_restart),
                "newStoreAndDownloader": True,
                "resumedBytes": jobs["video_00"]["resumedBytes"],
                "zeroByte304Responses": len(reuse),
            },
            "httpStatuses": dict(Counter(str(event["status"]) for event in origin.requests)),
            "requestCount": len(origin.requests),
            "policyValidations": policy.validations,
            "fixtureBodyBytesWritten": sum(event["bytesWritten"] for event in origin.requests),
        }


async def run(arguments):
    started = time.monotonic()
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "startedAt": datetime.now(UTC).isoformat(),
        "requestedRounds": arguments.rounds,
        "ytDlpVersion": importlib.metadata.version("yt-dlp"),
        "rounds": [],
        "limitations": [
            "Synthetic local three-second media; no real site, account, paid content or long-duration claim.",
            "Queue restart is graceful in one Python interpreter with a new Store/Downloader/TaskManager; not process death.",
            "The 403 gate resolver is controlled and stays closed; this script does not test real browser login.",
            "Each round deletes source-independent queue files and outputs before the next round.",
        ],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="video-batch-", dir=output) as temporary:
            bulk = Path(temporary)
            sources = await asyncio.to_thread(make_sources, bulk / "sources")
            report["fixtureProtocolChecks"] = await check_origin_range_contract(sources)
            report["sourceFixtureBytes"] = sum(
                len(body) for files in sources.values() for body in files.values()
            )
            for number in range(1, arguments.rounds + 1):
                print(
                    f"Controlled video batch {number}/{arguments.rounds}: 24 jobs, concurrency 4",
                    flush=True,
                )
                with tempfile.TemporaryDirectory(prefix=f"round-{number}-", dir=bulk) as round_dir:
                    result = await run_round(Path(round_dir), sources, number)
                result["bulkFilesCleaned"] = True
                report["rounds"].append(result)
                print(
                    f"Round {number}: passed in {result['elapsedSeconds']}s; 22 outputs fully decoded",
                    flush=True,
                )
        report["status"] = "passed"
    except Exception as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        report.update(
            status="failed",
            errorType=type(error).__name__,
            error=str(error)[:1200],
            failureLocation=f"{Path(frame.filename).name}:{frame.lineno} ({frame.name})",
        )
        if isinstance(error, BatchFailure):
            report["failedRound"] = error.evidence
    report.update(
        completedAt=datetime.now(UTC).isoformat(),
        elapsedSeconds=round(time.monotonic() - started, 3),
        bulkFilesCleaned=True,
    )
    report["totalTasks"] = len(report["rounds"]) * TASK_COUNT
    report["totalFullyDecodedOutputs"] = sum(
        item["fullyDecodedOutputs"] for item in report["rounds"]
    )
    destination = output / "video-batch.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "elapsedSeconds": report["elapsedSeconds"],
                "report": str(destination),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=1, help="Bounded soak rounds, 1 to 1000")
    parser.add_argument("--output", type=Path, default=ROOT / "test-output/video-batch-acceptance")
    arguments = parser.parse_args()
    if not 1 <= arguments.rounds <= 1000:
        parser.error("rounds must be between 1 and 1000")
    raise SystemExit(asyncio.run(run(arguments)))


if __name__ == "__main__":
    main()
