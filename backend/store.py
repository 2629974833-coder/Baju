import copy
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from backend.classification import classify_asset
from backend.series import annotate_series_scans, series_library
from backend.site_adapters import update_catalog


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix):
    return f"{prefix}_{uuid4().hex}"


def record(state, message, kind="action"):
    state["activity"].insert(
        0, {"id": new_id("evt"), "type": kind, "message": message, "createdAt": timestamp()}
    )
    del state["activity"][100:]


def public_fields(value):
    """Keep worker-only state and remote artwork addresses out of panel responses."""
    if isinstance(value, dict):
        return {key: public_fields(item) for key, item in value.items()
                if not key.startswith("_") and key != "coverUrls"}
    if isinstance(value, list):
        return [public_fields(item) for item in value]
    return value


def initial_state():
    return {
        "mode": "real",
        "version": 1,
        "assets": [],
        "jobs": [],
        "processingJobs": [],
        "scans": [],
        "rules": [],
        "sites": [
            {
                "id": domain.split(".")[0],
                "name": name,
                "domain": domain,
                "url": f"https://{domain}",
                "initials": initials,
                "auth": "disconnected",
                "access": "尚未保存会话",
                "updated": "尚未连接",
                "count": 0,
                "sessionSaved": False,
                "verified": False,
            }
            for domain, name, initials in [
                ("lustflix.tv", "Lustflix", "LF"),
                ("candy.ai", "Candy AI", "CA"),
                ("chunlou.tv", "春楼 CHUNLOU", "CL"),
                ("moolight.tv", "Moolight", "ML"),
            ]
        ],
        "settings": {"concurrency": 2, "autoProcess": False, "preserveOriginal": True},
        "activity": [],
    }


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS workspace (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO workspace VALUES (1, ?)",
                (json.dumps(initial_state(), ensure_ascii=False),),
            )
            state = json.loads(connection.execute("SELECT data FROM workspace WHERE id=1").fetchone()[0])
            if "processingJobs" not in state or "rules" not in state:
                state.setdefault("processingJobs", [])
                state.setdefault("rules", [])
                connection.execute("UPDATE workspace SET data=? WHERE id=1", (json.dumps(state, ensure_ascii=False),))
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def read(self):
        with self._lock, self._connect() as connection:
            return json.loads(
                connection.execute("SELECT data FROM workspace WHERE id=1").fetchone()[0]
            )

    def mutate(self, callback):
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = json.loads(
                connection.execute("SELECT data FROM workspace WHERE id=1").fetchone()[0]
            )
            result = callback(state)
            connection.execute(
                "UPDATE workspace SET data=? WHERE id=1", (json.dumps(state, ensure_ascii=False),)
            )
            return copy.deepcopy(result)

    def public_state(self):
        state = self.read()
        state["series"] = series_library(state)
        annotate_series_scans(state)
        for asset in state["assets"]:
            if asset.get("kind") == "video":
                asset["classification"] = classify_asset(asset)
        for scan in state["scans"]:
            # Site catalogs are retrieved on their own results page, not on every panel poll.
            site_catalog = scan.pop("siteCatalog", None)
            if site_catalog:
                scan["showsFound"] = len(site_catalog.get("shows", []))
                shows = site_catalog.get("shows", [])
                scan["siteSummary"] = {
                    "shows": len(shows),
                    "episodes": sum(len((show.get("catalog") or {}).get("entries", [])) for show in shows),
                    "directories": sum(bool((show.get("catalog") or {}).get("entries")) for show in shows),
                }
            if scan.get("catalog"):
                scan["catalog"] = update_catalog(
                    scan["catalog"], [asset for asset in state["assets"]
                                      if asset.get("siteId") == scan.get("siteId")]
                )
        for asset in state["assets"]:
            if asset.get("_chunlouEpisode") or asset.get("_lustflixGrant") or asset.get("shortMedia"):
                asset.pop("sourceUrl", None)  # Keep signed playback addresses in the worker.
        return public_fields(state)

    def recover_interrupted(self):
        def recover(state):
            for job in state["jobs"]:
                if job["status"] == "running":
                    job.update(
                        status="paused", error="服务已重启，请继续任务；将校验并恢复可用进度"
                    )
            for scan in state["scans"]:
                pending = [r for r in scan.get("unlockResults", {}).values() if r["status"] == "pending"]
                if pending:
                    message = "服务中断，上一笔金币购买结果待核对；重新扫描会核对站点权益"
                    for receipt in pending:
                        receipt.update(status="uncertain", spent=None, message=message)
                    summary = scan["unlockSummary"]
                    summary.update(unknown=summary.get("unknown", 0) + len(pending), stopped=True, lastMessage=message)
                if scan["status"] in ("running", "queued"):
                    scan.update(status="failed", error="服务重启中断了扫描，请重新创建扫描")

        self.mutate(recover)
