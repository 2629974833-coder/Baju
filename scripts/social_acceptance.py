"""Real public social-link acceptance through the isolated application API.

No account/session import, production workspace, or external downloader service.
Reports distinguish complete playable download from watermark evidence. Source
URLs/signatures/cookies are never included in output. Retains six original frames
for human review, not an automatic "watermark-free" verdict.

Local example:
  uv run python scripts/social_acceptance.py --platform tiktok --trust-fake-dns
Container example after copying this script to /tmp (normal DNS):
  PYTHONPATH=/app python /tmp/social_acceptance.py --platform youtube --output /tmp/social-proof
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import backend  # noqa: E402
from backend.app import create_app  # noqa: E402
from backend.config import Settings  # noqa: E402

ROOT = Path(backend.__file__).resolve().parents[1]
CODE_HASHES = {name: hashlib.sha256((ROOT / "backend" / name).read_bytes()).hexdigest()[:16]
               for name in ("short_media.py", "extractor.py", "scanner.py", "downloader.py", "youtube_media.py", "instagram_media.py", "instagram_requests.py", "app.py", "tasks.py", "intake.py")
               if (ROOT / "backend" / name).is_file()}
SAMPLES = {
    "douyin": "https://www.douyin.com/video/6961737553342991651",
    "youtube": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
    "tiktok": "https://www.tiktok.com/@scout2015/video/6718335390845095173",
    "instagram": "https://www.instagram.com/p/BQ0eAlwhDrw/",
}
TERMINAL = {"completed", "failed", "needs-user", "cancelled", "timed-out", "unsupported-drm", "unsupported-media"}


def safe_reason(value):
    value = str(value or "")
    if re.search(r"cookie|authorization|bearer|eyJ[A-Za-z0-9_-]{10}", value, re.I):
        return "Sensitive diagnostic omitted"
    value = re.sub(r"https?://[^\s<>]+", "[URL omitted]", value)
    value = re.sub(r"(?i)(token|signature|secret|password)\s*[:=]\s*\S+", r"\1=[omitted]", value)
    return value[:400]


def response_json(response):
    if response.status_code >= 400:
        try:
            reason = response.json().get("detail")
        except ValueError:
            reason = "Non-JSON server response"
        raise RuntimeError(f"HTTP {response.status_code}: {safe_reason(reason)}")
    return response.json()


def wait_for(client, select, settled, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = response_json(client.get("/api/state"))
        result = select(state)
        if result and settled(result):
            return state, result
        time.sleep(0.25)
    raise TimeoutError("Acceptance deadline exceeded")


def media_tool(name, arguments, timeout=90):
    result = subprocess.run([name, "-v", "error", *arguments], capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{name} exit {result.returncode}: {safe_reason(result.stderr.decode(errors='replace'))}")
    return result.stdout


def inspect_media(path, output, frame_count):
    probe = json.loads(media_tool("ffprobe", ["-protocol_whitelist", "file,pipe", "-show_format", "-show_streams", "-of", "json", str(path)]))
    duration = float(probe.get("format", {}).get("duration") or 0)
    tracks = [{key: stream[key] for key in ("codec_type", "codec_name", "width", "height", "sample_rate", "channels", "duration") if key in stream}
              for stream in probe["streams"] if stream.get("codec_type") in {"video", "audio"}]
    assert duration > 0 and any(track["codec_type"] == "video" for track in tracks), "Downloaded file has no timed video"
    media_tool("ffmpeg", ["-nostdin", "-xerror", "-err_detect", "explode", "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"], timeout=180)
    frames = []
    for index in range(frame_count):
        seconds = round(duration * (index + 0.5) / frame_count, 3)
        destination = output / f"frame-{index + 1:02d}-{seconds:07.3f}s.png"
        media_tool("ffmpeg", ["-nostdin", "-y", "-ss", str(seconds), "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", "0:v:0", "-frames:v", "1", str(destination)])
        assert destination.is_file(), "Frame extraction produced no image"
        frames.append({"seconds": seconds, "file": destination.name})
    return {"durationSeconds": duration, "tracks": tracks, "fullDecode": "passed", "frames": frames}


def run_one(platform, url, args):
    output = args.output / platform
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "platform": platform, "publicPage": url, "startedAt": datetime.now(UTC).isoformat(),
        "accountImported": False, "trustFakeDns": args.trust_fake_dns,
        "autoDownload": args.auto_download,
        "downloaded": False, "playable": False,
        "watermarkVisualReview": "pending; playback/source-clean metadata is not a visual verdict",
        "ytDlpVersion": importlib.metadata.version("yt-dlp"),
        "codeHashes": CODE_HASHES,
    }
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix=f"framekeep-social-{platform}-") as temporary:
            root = Path(temporary)
            settings = Settings(data_dir=root, admin_token="", allowed_hosts=["testserver"],
                                trust_fake_dns=args.trust_fake_dns, max_download_bytes=64 * 1024**2)
            app = create_app(settings)
            with TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
                scan = response_json(client.post("/api/intake", json={
                    "url": url, "scope": "single", "kinds": ["video"], "autoDownload": args.auto_download,
                }))
                state, result = wait_for(client,
                    lambda current: next((item for item in current["scans"] if item["id"] == scan["id"]), None),
                    lambda item: item["status"] in TERMINAL, args.timeout)
                candidates = [asset for asset in state["assets"] if asset["kind"] == "video"]
                report.update(scanStatus=result["status"], scanMessage=safe_reason(result.get("message")),
                              scanError=safe_reason(result.get("error")), videoCandidates=len(candidates))
                if result["status"] != "completed" or not candidates:
                    report["failureStage"] = "parse"
                    return report
                if args.candidate_index >= len(candidates):
                    raise ValueError("Selected candidate index is outside the parsed video list")
                selected = candidates[args.candidate_index]
                internal = next(asset for asset in app.state.store.read()["assets"] if asset["id"] == selected["id"])
                report.update(watermark=(selected.get("shortMedia") or {}).get("watermark", "not-provided"),
                              formatId=internal.get("_formatId"), engine=selected.get("engine"),
                              declaredAudio=internal.get("_instagramAudioExpected"),
                              declaredDuration=selected.get("duration"), selectedCandidate=args.candidate_index,
                              candidateScope="One parsed video, not proof that a carousel or every platform video is supported")
                if args.auto_download:
                    assert result.get("downloadAfterScan") is True
                    queued = result.get("downloadSummary") or {}
                    report["manualDownloadRequests"] = 0
                else:
                    queued = response_json(client.post("/api/downloads", json={"ids": [selected["id"]]}))
                    report["manualDownloadRequests"] = 1
                report["enqueueSummary"] = {key: queued.get(key) for key in ("added", "blocked", "duplicates", "error") if key in queued}
                if queued.get("added", 0) < 1 or queued.get("error"):
                    report.update(failureStage="enqueue", enqueueSummary={key: queued.get(key) for key in ("added", "blocked", "duplicates")})
                    return report
                state, job = wait_for(client,
                    lambda current: next((item for item in current["jobs"] if item["assetId"] == selected["id"]), None),
                    lambda item: item["status"] in TERMINAL | {"paused"}, args.timeout)
                report.update(downloadStatus=job["status"], downloadError=safe_reason(job.get("error")))
                if job["status"] != "completed":
                    report["failureStage"] = "download"
                    return report
                internal = next(asset for asset in app.state.store.read()["assets"] if asset["id"] == selected["id"])
                path = root / internal["_path"]
                content = path.read_bytes()
                report.update(downloaded=True, bytes=len(content), sha256=hashlib.sha256(content).hexdigest(), extension=path.suffix)
                saved = next(asset for asset in state["assets"] if asset["id"] == selected["id"])
                exported = client.get(saved["fileUrl"], params={"download": "true"})
                assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
                assert hashlib.sha256(exported.content).hexdigest() == report["sha256"]
                ranged = client.get(saved["fileUrl"], headers={"Range": "bytes=0-31"})
                assert ranged.status_code == 206 and ranged.content == content[:32]
                report.update(fileExport="byte-identical", rangeStatus=206)
                report.update(inspect_media(path, output, args.frames))
                report["playable"] = True
                if args.keep_media:
                    shutil.copyfile(path, output / ("source" + path.suffix))
    except Exception as error:
        report.update(failureStage=report.get("failureStage", "runtime"), errorType=type(error).__name__, reason=safe_reason(error))
    finally:
        report["elapsedSeconds"] = round(time.monotonic() - started, 2)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=[*SAMPLES, "all"], default="all")
    parser.add_argument("--url", help="Alternative public page URL; requires a single --platform")
    parser.add_argument("--output", type=Path, default=ROOT / "test-output/social-20261113")
    parser.add_argument("--trust-fake-dns", action="store_true", help="Only for the local proxy's fake DNS network")
    parser.add_argument("--keep-media", action="store_true")
    parser.add_argument("--auto-download", action="store_true", help="Require intake to enqueue automatically without POST /api/downloads")
    parser.add_argument("--candidate-index", type=int, choices=range(20), default=0)
    parser.add_argument("--frames", type=int, choices=range(5, 9), default=6)
    parser.add_argument("--timeout", type=int, default=150)
    args = parser.parse_args()
    if args.url and args.platform == "all":
        parser.error("--url requires one --platform")
    for binary in ("ffmpeg", "ffprobe"):
        if not shutil.which(binary):
            parser.error(f"Required media tool missing: {binary}")
    platforms = list(SAMPLES) if args.platform == "all" else [args.platform]
    results = []
    for platform in platforms:
        report = run_one(platform, args.url or SAMPLES[platform], args)
        results.append(report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    # A failed parse/download/decode must remain a failing acceptance exit code.
    return 0 if all(result["playable"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
