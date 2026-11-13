"""Neutral local acceptance for CPU processing, version archives and restore.

Automatic mode exercises the real application API with disposable source media.
--serve exposes the same two fixtures on loopback for manual browser acceptance.
No saved accounts, external media, or the primary workspace are read or modified.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import httpx
import uvicorn
from cryptography.fernet import Fernet
from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app import create_app  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.store import timestamp  # noqa: E402
from scripts.download_acceptance import media_command, sha256  # noqa: E402

REGION = {"x": 70, "y": 75, "width": 25, "height": 20}
SITE = {
    "id": "neutral-local",
    "name": "本地验收样例",
    "domain": "fixture.invalid",
    "url": "https://fixture.invalid",
    "initials": "验",
    "auth": "disconnected",
    "sessionSaved": True,
    "verified": False,
    "access": "仅本地合成会话",
}


def seed_fixtures(app, data_dir):
    """Directly seed generated originals; this is explicitly not a download proof."""
    assets = []
    for identifier, kind, title in (
        ("fixture_image", "image", "花园色彩图 · 本地样例"),
        ("fixture_video", "video", "双音轨测试片 · 本地样例"),
    ):
        folder = data_dir / "assets" / identifier
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("original.png" if kind == "image" else "original.mp4")
        if kind == "image":
            image = Image.new("RGB", (640, 360))
            draw = ImageDraw.Draw(image)
            for y in range(360):
                draw.line((0, y, 639, y), fill=(30 + y // 3, 100 + y // 4, 150))
            # The declared repair region surrounds this deliberately added mark.
            draw.rectangle((466, 284, 590, 325), fill="white")
            draw.text((480, 298), "FIXTURE MARK", fill="black")
            image.save(path)
        else:
            media_command(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=640x360:rate=24:duration=6",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=6",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=660:duration=6",
                    "-map",
                    "0:v",
                    "-map",
                    "1:a",
                    "-map",
                    "2:a",
                    "-vf",
                    "drawbox=x=466:y=284:w=124:h=41:color=white:t=fill",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-metadata:s:a:0",
                    "language=eng",
                    "-metadata:s:a:1",
                    "language=zho",
                    "-movflags",
                    "+faststart",
                    str(path),
                ]
            )
        file_url = f"/api/assets/{identifier}/file"
        thumbnail = file_url
        if kind == "video":
            thumbnail_path = folder / "fixture-thumbnail.jpg"
            media_command(["-i", str(path), "-frames:v", "1", str(thumbnail_path)])
            thumbnail = f"/api/assets/{identifier}/thumbnail"
        assets.append(
            {
                "id": identifier,
                "siteId": SITE["id"],
                "kind": kind,
                "title": title,
                "status": "downloaded",
                "_path": path.relative_to(data_dir).as_posix(),
                "sourceUrl": f"https://fixture.invalid/{identifier}",
                "pageUrl": "https://fixture.invalid/local-acceptance",
                "engine": "local-fixture",
                "mime": "image/png" if kind == "image" else "video/mp4",
                "width": 640,
                "height": 360,
                "sizeBytes": path.stat().st_size,
                "sizeMB": round(path.stat().st_size / 1024**2, 2),
                "sha256": sha256(path),
                "fileUrl": file_url,
                "thumbnail": thumbnail,
                "duration": "0:06" if kind == "video" else "—",
                "tags": ["本地验收", "中性样例"],
                "favorite": False,
                "category": "测试样例",
                "date": timestamp()[:10],
                "downloadedAt": timestamp(),
                "versions": [
                    {
                        "id": "original",
                        "label": "原始文件",
                        "createdAt": timestamp(),
                        "path": file_url,
                    }
                ],
            }
        )
    session_dir = data_dir / "sessions"
    session_dir.mkdir(mode=0o700, exist_ok=True)
    key = Fernet.generate_key()
    (session_dir / ".key").write_bytes(key)
    session_path = session_dir / (hashlib.sha256(SITE["id"].encode()).hexdigest() + ".enc")
    session_path.write_bytes(
        Fernet(key).encrypt(json.dumps({"cookies": [], "origins": []}).encode())
    )
    for path in session_dir.iterdir():
        path.chmod(0o600)

    def seed(state):
        state["sites"] = [dict(SITE)]
        state["assets"] = assets
        state["jobs"] = []
        state["processingJobs"] = []
        state["scans"] = []

    app.state.store.mutate(seed)
    return {asset["id"]: asset["sha256"] for asset in assets}


async def json_request(client, method, path, **kwargs):
    response = await client.request(method, path, **kwargs)
    assert response.is_success, (response.status_code, response.text[:400])
    return response.json()


async def wait_state(client, predicate, *, timeout=120):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        state = await json_request(client, "GET", "/api/state")
        if predicate(state):
            return state
        failed = [row for row in state.get("processingJobs", []) if row["status"] == "failed"]
        assert not failed, [
            {key: row.get(key) for key in ("status", "failureCode", "error")} for row in failed
        ]
        await asyncio.sleep(0.015)
    raise AssertionError("Timed out waiting for the local processing fixture")


def versions(state):
    return [
        (asset, version)
        for asset in state["assets"]
        for version in asset.get("versions", [])
        if version.get("kind") == "processed" and version.get("status") == "completed"
    ]


def validate_video(path):
    info = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=30,
        ).stdout
    )
    audio = [item for item in info["streams"] if item["codec_type"] == "audio"]
    assert len(audio) == 2
    assert {item.get("tags", {}).get("language") for item in audio} == {"eng", "zho"}
    assert all(abs(float(item["duration"]) - 6) < 0.2 for item in audio)
    assert abs(float(info["format"]["duration"]) - 6) < 0.2
    media_command(
        [
            "-xerror",
            "-err_detect",
            "explode",
            "-i",
            str(path),
            "-map",
            "0:v",
            "-map",
            "0:a",
            "-f",
            "null",
            "-",
        ]
    )
    return {
        "duration": round(float(info["format"]["duration"]), 3),
        "audioTracks": 2,
        "fullDecode": True,
    }


def validate_image(source, path, mode):
    with Image.open(source) as before, Image.open(path) as after:
        before, after = before.convert("RGB"), after.convert("RGB")
        assert before.size == after.size == (640, 360)
        changes = ImageChops.difference(before, after).getbbox()
        assert changes
        if mode == "remove":
            assert changes[0] >= 448 and changes[1] >= 270
            assert changes[2] <= 608 and changes[3] <= 342
            assert after.getpixel((500, 310)) != before.getpixel((500, 310))
        if mode in {"add", "both"}:
            assert ImageChops.difference(
                before.crop((0, 0, 640, 120)), after.crop((0, 0, 640, 120))
            ).getbbox()
    return {"changedPixels": True, "regionBounded": mode == "remove"}


async def recovery_job(client, app, label):
    before = await json_request(client, "GET", "/api/state")
    old_ids = {row["id"] for row in before["processingJobs"]}
    await json_request(
        client,
        "POST",
        "/api/processes",
        json={
            "ids": ["fixture_video"],
            "options": {"text": label, "fontSize": 7, "position": "top-left"},
        },
    )
    state = await wait_state(
        client,
        lambda s: any(
            row["id"] not in old_ids and row["status"] == "running" for row in s["processingJobs"]
        ),
    )
    job = next(row for row in state["processingJobs"] if row["id"] not in old_ids)
    assert job["id"] in app.state.processing.active
    return job["id"]


async def verify(root, data_dir, app, original_hashes):
    started = time.monotonic()
    options = [
        {
            "mode": mode,
            "text": "验收 FIELD NOTES",
            "position": "top-left",
            "fontSize": 8,
            "opacity": 90,
            "region": REGION,
        }
        for mode in ("add", "remove", "both")
    ]
    media_results = []
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", trust_env=False
        ) as client:
            ready = await json_request(client, "GET", "/api/ready")
            assert ready["ready"]
            for option in options:
                submitted = await json_request(
                    client,
                    "POST",
                    "/api/processes",
                    json={"ids": list(original_hashes), "options": option},
                )
                assert submitted["added"] == 2
            state = await wait_state(client, lambda s: len(versions(s)) == 6)
            for option in options:
                duplicate = await json_request(
                    client,
                    "POST",
                    "/api/processes",
                    json={"ids": list(original_hashes), "options": option},
                )
                assert duplicate["added"] == 0 and duplicate["duplicates"] == 2
            for asset, version in versions(state):
                record = next(
                    item for item in app.state.store.read()["assets"] if item["id"] == asset["id"]
                )
                raw = next(item for item in record["versions"] if item["id"] == version["id"])
                source, path = data_dir / record["_path"], data_dir / raw["_path"]
                assert sha256(source) == original_hashes[asset["id"]] == raw["sourceSha256"]
                assert sha256(path) == raw["sha256"]
                response = await client.get(version["fileUrl"], headers={"Range": "bytes=0-31"})
                assert response.status_code == 206 and response.content == path.read_bytes()[:32]
                check = (
                    await asyncio.to_thread(validate_video, path)
                    if asset["kind"] == "video"
                    else validate_image(source, path, version["options"]["mode"])
                )
                media_results.append(
                    {"kind": asset["kind"], "mode": version["options"]["mode"], **check}
                )
            archive = await client.post(
                "/api/library/archive", json={"ids": list(original_hashes), "variant": "latest"}
            )
            assert archive.status_code == 200
            with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
                names = bundle.namelist()
                assert len(names) == 3
                manifest = json.loads(bundle.read("Framekeep/manifest.json"))
                for item in manifest["items"]:
                    assert hashlib.sha256(bundle.read(item["file"])).hexdigest() == item["sha256"]
                assert any("/图片/" in name for name in names) and any(
                    "/视频/" in name for name in names
                )
            pause_id = await recovery_job(client, app, "PAUSE RECOVERY")
            await json_request(
                client, "POST", f"/api/processes/{pause_id}/action", json={"action": "pause"}
            )
            await wait_state(
                client,
                lambda s: (
                    any(
                        row["id"] == pause_id and row["status"] == "paused"
                        for row in s["processingJobs"]
                    )
                    and pause_id not in app.state.processing.active
                ),
            )
            await json_request(
                client, "POST", f"/api/processes/{pause_id}/action", json={"action": "resume"}
            )
            await wait_state(
                client,
                lambda s: any(
                    row["id"] == pause_id and row["status"] == "completed"
                    for row in s["processingJobs"]
                ),
            )
            restart_id = await recovery_job(client, app, "RESTART RECOVERY")
    assert (
        next(row for row in app.state.store.read()["processingJobs"] if row["id"] == restart_id)[
            "status"
        ]
        == "paused"
    )
    restarted = create_app(Settings(data_dir=data_dir))
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted),
            base_url="http://testserver",
            trust_env=False,
        ) as client:
            await json_request(
                client, "POST", f"/api/processes/{restart_id}/action", json={"action": "resume"}
            )
            state = await wait_state(client, lambda s: len(versions(s)) == 8)
            backup = await json_request(client, "POST", "/api/maintenance/backups", json={})
            assert backup["verified"]
            checked = await json_request(
                client, "POST", f"/api/maintenance/backups/{backup['id']}/verify", json={}
            )
            assert checked["verified"]
            assert (await json_request(client, "GET", "/api/operations"))["ready"]
    backup_dir = data_dir / "backups" / backup["id"]
    restored_dir = root / "restored"
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "backend.backup",
            "restore",
            "--backup",
            str(backup_dir),
            "--data-dir",
            str(restored_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["restored"]
    restored = create_app(Settings(data_dir=restored_dir))
    async with restored.router.lifespan_context(restored):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restored),
            base_url="http://testserver",
            trust_env=False,
        ) as client:
            state = await json_request(client, "GET", "/api/state")
            assert len(versions(state)) == 8
            for asset in state["assets"]:
                original = await client.get(asset["fileUrl"])
                assert hashlib.sha256(original.content).hexdigest() == original_hashes[asset["id"]]
            for _asset, version in versions(state):
                response = await client.get(version["fileUrl"])
                assert (
                    response.status_code == 200
                    and hashlib.sha256(response.content).hexdigest() == version["sha256"]
                )
            session = await json_request(client, "GET", f"/api/sites/{SITE['id']}/session")
            assert session["saved"]
    return {
        "passed": True,
        "elapsedSeconds": round(time.monotonic() - started, 2),
        "originals": 2,
        "coreVersions": 6,
        "versionsIncludingRecovery": 8,
        "duplicateSubmissionsSkipped": 6,
        "media": media_results,
        "versionRange206": True,
        "categorizedZip": True,
        "pauseResumeFromOriginal": True,
        "restartResumeFromOriginal": True,
        "verifiedBackupAndCliRestore": True,
        "restoredOriginalsAndVersionsHashEqual": True,
        "syntheticEncryptedSessionRestored": True,
        "scope": "Local generated fixtures; CPU resume restarts from original; no server deployment or real-site media acceptance.",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=4198)
    parser.add_argument("--report", default="test-output/stage23-processing-acceptance.json")
    args = parser.parse_args()

    def terminate(_number, _frame):
        raise KeyboardInterrupt()

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        with tempfile.TemporaryDirectory(prefix="framekeep-processing-acceptance-") as folder:
            root = Path(folder).resolve()
            data_dir = root / "workspace"
            app = create_app(Settings(data_dir=data_dir))
            originals = seed_fixtures(app, data_dir)
            if args.serve:
                print(
                    json.dumps(
                        {
                            "panel": f"http://127.0.0.1:{args.port}/#process",
                            "fixtures": 2,
                            "temporaryWorkspace": str(data_dir),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
            else:
                report = asyncio.run(verify(root, data_dir, app, originals))
                report["temporaryWorkspaceRemoved"] = True
        if not args.serve:
            output = ROOT / args.report
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False))
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
