"""Isolated operations soak, crash lease recovery and complete backup/restore.

Only generated PNGs and an encrypted synthetic empty browser session are used.
The duration is configurable; a short local pass is not a server longevity claim.
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
from cryptography.fernet import Fernet
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app import create_app  # noqa: E402
from backend.backup import verify_backup  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.store import Store  # noqa: E402


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def seed(root):
    store = Store(root / "workspace.sqlite3")
    original = root / "assets/neutral/original.png"
    processed = root / "assets/neutral/versions/v1/processed.png"
    processed.parent.mkdir(parents=True)
    Image.new("RGB", (320, 180), "seagreen").save(original)
    Image.new("RGB", (320, 180), "steelblue").save(processed)
    (original.parent / "original.part").write_bytes(b"synthetic partial checkpoint")
    (original.parent / ".download.json").write_text('{"offset":28}')
    sessions = root / "sessions"
    sessions.mkdir()
    key = Fernet.generate_key()
    (sessions / ".key").write_bytes(key)
    session_name = hashlib.sha256(b"neutral").hexdigest() + ".enc"
    (sessions / session_name).write_bytes(Fernet(key).encrypt(b'{"cookies":[],"origins":[]}'))

    def save(state):
        state["sites"] = [
            {
                "id": "neutral",
                "name": "Neutral fixture",
                "url": "https://neutral.example",
                "domain": "neutral.example",
                "sessionSaved": True,
            }
        ]
        state["assets"] = [
            {
                "id": "neutral",
                "siteId": "neutral",
                "title": "Synthetic sample",
                "kind": "image",
                "status": "downloaded",
                "_path": "assets/neutral/original.png",
                "sha256": digest(original),
                "mime": "image/png",
                "width": 320,
                "height": 180,
                "versions": [
                    {
                        "id": "v1",
                        "status": "completed",
                        "mime": "image/png",
                        "_path": "assets/neutral/versions/v1/processed.png",
                        "sha256": digest(processed),
                    }
                ],
            }
        ]
        state["jobs"] = [
            {
                "id": "download-interrupted",
                "assetId": "neutral",
                "type": "download",
                "status": "running",
                "progress": 42,
            }
        ]
        state["processingJobs"] = [
            {
                "id": "process-interrupted",
                "assetId": "neutral",
                "type": "process",
                "status": "running",
                "progress": 37,
                "versionId": None,
            }
        ]
        state["scans"] = [{"id": "scan-interrupted", "siteId": "neutral", "status": "running"}]

    store.mutate(save)
    return store.read(), {
        path.relative_to(root).as_posix(): digest(path) for path in (original, processed)
    }


def cli(*arguments, success=True):
    result = subprocess.run(
        [sys.executable, "-m", "backend.backup", *map(str, arguments)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if success:
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    assert result.returncode != 0 and "正在被其他" in result.stderr


async def exercise(root, seconds):
    app = create_app(Settings(data_dir=root))
    samples = 0
    started = time.monotonic()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", trust_env=False
        ) as client:
            state = app.state.store.read()
            assert state["jobs"][0]["status"] == "paused" and state["jobs"][0]["progress"] == 42
            assert state["processingJobs"][0]["status"] == "paused"
            assert state["scans"][0]["status"] == "failed"
            soak_started = time.monotonic()
            while time.monotonic() - soak_started < seconds:
                response = await client.get("/api/ready")
                assert response.status_code == 200, response.json()
                assert all(item["healthy"] for item in response.json()["workers"].values())
                samples += 1
                await asyncio.sleep(0.5)
            soak_seconds = time.monotonic() - soak_started
            response = await client.post("/api/maintenance/backups", json={})
            assert response.status_code == 200, response.json()
            online = response.json()
            assert online["verified"] is True
            listed = (await client.get("/api/maintenance/backups")).json()["backups"]
            assert listed[0]["verified"] is None
            verified = await client.post(f"/api/maintenance/backups/{online['id']}/verify", json={})
            assert verified.status_code == 200 and verified.json()["verified"] is True
            original_usage = app.state.storage._disk_usage
            app.state.storage._disk_usage = lambda _: SimpleNamespace(total=1000, used=1000, free=0)
            low = await client.get("/api/ready")
            assert low.status_code == 503 and not low.json()["disk"]["healthy"]
            app.state.storage._disk_usage = original_usage
            assert (await client.get("/api/ready")).status_code == 200
            app.state.tasks._scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await app.state.tasks._scheduler
            stopped = await client.get("/api/ready")
            assert (
                stopped.status_code == 503 and not stopped.json()["workers"]["download"]["healthy"]
            )
    restarted = create_app(Settings(data_dir=root))
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted),
            base_url="http://testserver",
            trust_env=False,
        ) as client:
            assert (await client.get("/api/ready")).status_code == 200
    return {
        "healthSamples": samples,
        "soakSeconds": round(soak_seconds, 2),
        "elapsedSeconds": round(time.monotonic() - started, 2),
        "onlineBackupVerified": True,
        "storageFaultDetected": True,
        "workerFaultDetected": True,
        "healthyAfterRestart": True,
        "interruptedDownloadAndProcessingPaused": True,
        "interruptedScanFailed": True,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--report", default="test-output/operations-acceptance.json")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 86400:
        parser.error("--seconds must be between 1 and 86400")
    with tempfile.TemporaryDirectory(prefix="framekeep-operations-") as temporary:
        parent = Path(temporary)
        source, snapshot, restored = parent / "source", parent / "snapshot", parent / "restored"
        original_state, hashes = seed(source)
        program = (
            "from backend.operations import WorkspaceLock; import sys,time; "
            "lock=WorkspaceLock(sys.argv[1]);lock.acquire();print('held',flush=True);time.sleep(60)"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", program, str(source)], cwd=ROOT, stdout=subprocess.PIPE
        )
        try:
            assert child.stdout.readline().strip() == b"held"
            cli("create", "--data-dir", source, "--output", snapshot, success=False)
        finally:
            child.kill()
            child.wait(timeout=5)
        created = cli("create", "--data-dir", source, "--output", snapshot)
        assert cli("verify", "--backup", snapshot)["verified"] is True
        assert cli("restore", "--backup", snapshot, "--data-dir", restored)["restored"] is True
        assert Store(restored / "workspace.sqlite3").read() == original_state
        for relative, value in hashes.items():
            assert digest(restored / relative) == value
        assert (source / "sessions/.key").read_bytes() == (restored / "sessions/.key").read_bytes()
        result = asyncio.run(exercise(restored, args.seconds))
        assert verify_backup(snapshot)["verified"] is True
        for relative, value in hashes.items():
            assert digest(restored / relative) == value and digest(source / relative) == value
        report = {
            "passed": True,
            "crashedLeaseReleased": True,
            "cliRoundTripVerified": True,
            "snapshotFiles": created["fileCount"],
            "snapshotBytes": created["totalBytes"],
            "originalAndVersionHashesUnchanged": True,
            **result,
        }
    report["temporaryWorkspaceRemoved"] = not parent.exists()
    path = ROOT / args.report
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
