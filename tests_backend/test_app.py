import base64
import time

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings


class Browser:
    async def start(self):
        pass

    async def close(self):
        pass

    async def open_session(self, site):
        return {"open": True, "saved": False, "verified": False}

    async def save_session(self, site_id):
        return {"open": True, "saved": True, "verified": False}

    async def disconnect(self, site_id):
        return {"open": False, "saved": False, "verified": False}


class Scanner:
    async def scan(self, site, options, on_asset, on_progress, cancelled):
        for _ in range(2):
            await on_asset(
                {
                    "sourceUrl": "https://example.com/photo.png",
                    "pageUrl": options["url"],
                    "kind": "image",
                    "title": "A photo",
                    "engine": "http",
                    "_headers": {"Referer": options["url"]},
                }
            )
        await on_progress(
            {"status": "completed", "pagesVisited": 1, "assetsFound": 1, "message": "done"}
        )


class Downloader:
    def __init__(self, root):
        self.root = root

    async def download(self, asset, control, on_progress):
        path = self.root / "assets" / asset["id"] / "original.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"verified-file-fixture")
        await on_progress({"progress": 100})
        return {
            "path": str(path.relative_to(self.root)),
            "size": path.stat().st_size,
            "sha256": "a" * 64,
            "mime": "image/png",
            "width": 80,
            "height": 60,
        }


class Policy:
    async def validate(self, url):
        if not url.startswith("https://"):
            raise ValueError("Only public HTTPS in fixture")
        return url


def app_at(tmp_path, **kwargs):
    return create_app(
        Settings(data_dir=tmp_path, **kwargs),
        browser=Browser(),
        scanner=Scanner(),
        downloader=Downloader(tmp_path),
        policy=Policy(),
    )


def until(client, predicate):
    for _ in range(100):
        state = client.get("/api/state").json()
        if predicate(state):
            return state
        time.sleep(0.02)
    raise AssertionError(state)


def test_scan_download_persist_and_file(tmp_path):
    with TestClient(app_at(tmp_path)) as client:
        assert client.get("/api/capabilities").json()["mode"] == "real"
        assert client.get("/").status_code == 200
        assert client.get("/api/no-such-route").status_code == 404
        assert (
            client.post(
                "/api/scans",
                json={
                    "siteId": "chunlou",
                    "url": "https://chunlou.tv",
                    "depth": 1,
                    "limit": 3,
                    "kinds": ["image"],
                },
            ).status_code
            == 200
        )
        state = until(client, lambda s: s["scans"][0]["status"] == "completed")
        assert len(state["assets"]) == 1
        asset = state["assets"][0]
        assert "_headers" not in asset
        assert asset["thumbnail"].startswith("/api/assets/")
        result = client.post("/api/downloads", json={"ids": [asset["id"], asset["id"]]}).json()
        assert result["added"] == 1
        state = until(client, lambda s: s["jobs"][0]["status"] == "completed")
        downloaded = state["assets"][0]
        assert downloaded["status"] == "downloaded"
        assert downloaded["sha256"] == "a" * 64
        assert client.get(downloaded["fileUrl"]).content == b"verified-file-fixture"
        assert client.post("/api/downloads", json={"ids": [asset["id"]]}).json()["duplicates"] == 1
        assert (
            client.post(
                "/api/jobs/" + state["jobs"][0]["id"] + "/action", json={"action": "retry"}
            ).status_code
            == 409
        )
    with TestClient(app_at(tmp_path)) as client:
        assert len(client.get("/api/state").json()["assets"]) == 1
        assert client.get(downloaded["fileUrl"]).content == b"verified-file-fixture"


def test_session_save_does_not_claim_entitlement(tmp_path):
    with TestClient(app_at(tmp_path)) as client:
        assert client.post("/api/sites/chunlou/session").status_code == 200
        assert client.post("/api/sites/chunlou/session/save").status_code == 200
        site = next(s for s in client.get("/api/state").json()["sites"] if s["id"] == "chunlou")
        assert site["sessionSaved"] is True and site["verified"] is False
        assert client.delete("/api/sites/chunlou/session").status_code == 200
        assert client.post("/api/sites/unknown/session").status_code == 404


def test_dismiss_from_discovery_is_persistent_and_preserves_files_and_jobs_on_rescan(tmp_path):
    app = app_at(tmp_path)
    scan = {"url": "https://example.com/album", "kinds": ["image"]}
    with TestClient(app) as client:
        assert client.post("/api/scans", json=scan).is_success
        state = until(client, lambda s: s["scans"][0]["status"] == "completed")
        item_id = state["assets"][0]["id"]
        assert client.post("/api/downloads", json={"ids": [item_id]}).json()["added"] == 1
        state = until(client, lambda s: s["jobs"][0]["status"] == "completed")
        original = state["assets"][0]
        app.state.store.mutate(
            lambda s: s["assets"].extend([
                {"id": "other-selected", "kind": "image"},
                {"id": "keep", "kind": "image"},
            ])
        )

        request = {"ids": [item_id, item_id, "unknown", "other-selected"]}
        assert client.post("/api/assets/dismiss", json=request).json() == {"dismissed": 2}
        assert client.post("/api/assets/dismiss", json=request).json() == {"dismissed": 0}
        assert client.post("/api/scans", json=scan).is_success
        rescanned = until(
            client, lambda s: len(s["scans"]) == 2 and s["scans"][0]["status"] == "completed"
        )
        assert rescanned["jobs"] == state["jobs"]

    with TestClient(app_at(tmp_path)) as client:
        items = {a["id"]: a for a in client.get("/api/state").json()["assets"]}
        assert items[item_id] == {**original, "dismissedFromDiscovery": True}
        assert items["other-selected"]["dismissedFromDiscovery"] is True
        assert "dismissedFromDiscovery" not in items["keep"]
        assert len(items) == 3
        assert client.get(original["fileUrl"]).content == b"verified-file-fixture"


def test_invalid_dismiss_batch_rejected_without_partial_changes(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        app.state.store.mutate(lambda s: s["assets"].append({"id": "keep", "kind": "image"}))
        for ids in ([], ["keep", ""], ["keep", 123], ["keep"] * 10001):
            assert client.post("/api/assets/dismiss", json={"ids": ids}).status_code == 422
        assert "dismissedFromDiscovery" not in app.state.store.read()["assets"][0]


def test_input_and_origin_validation(tmp_path):
    with TestClient(app_at(tmp_path)) as client:
        assert client.post("/api/scans", json={"url": "file:///etc/passwd"}).status_code == 400
        assert (
            client.post(
                "/api/scans", json={"url": "https://example.com", "limit": 10000}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/scans", json={"siteId": "chunlou", "url": "https://candy.ai"}
            ).status_code
            == 400
        )
        assert client.patch("/api/settings", json={"concurrency": 99}).status_code == 422
        assert client.patch("/api/settings", json={"autoProcess": True}).status_code == 422
        assert (
            client.post(
                "/api/sites",
                json={"name": "X", "url": "https://example.com"},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert client.get("/api/state", headers={"Host": "evil.example"}).status_code == 400
        assert client.get("/api/assets/unknown/file").status_code == 404


def test_admin_auth_covers_api_and_files(tmp_path):
    app = app_at(tmp_path, admin_token="test-secret-token-long-enough")
    with TestClient(app) as client:
        assert client.get("/").status_code == 401
        assert client.get("/api/state").status_code == 401
        auth = base64.b64encode(b"admin:test-secret-token-long-enough").decode()
        assert (
            client.get("/api/state", headers={"Authorization": f"Basic {auth}"}).status_code == 200
        )
