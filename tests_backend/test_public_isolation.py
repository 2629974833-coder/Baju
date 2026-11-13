"""Cross-workspace endpoint and streaming ownership checks for public deployment."""

import asyncio

from fastapi.testclient import TestClient

from tests_backend.test_app import until
from tests_backend.test_public import setup, visit

ORIGIN = {"Origin": "https://testserver"}


def saved_fixture(client):
    scan = client.post("/api/scans", headers=ORIGIN,
                       json={"siteId": "lustflix", "url": "https://lustflix.tv/"}).json()
    state = until(client, lambda current: bool(current["assets"]))
    asset = state["assets"][0]
    assert client.post("/api/downloads", headers=ORIGIN, json={"ids": [asset["id"]]}).status_code == 200
    state = until(client, lambda current: current["assets"][0]["status"] == "downloaded")
    return scan, state["assets"][0], state["jobs"][0]


def test_foreign_ids_do_not_grant_file_catalog_job_or_session_access(tmp_path):
    app, _, _, _ = setup(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        _, token_a = visit(client, app)
        scan, asset, job = saved_fixture(client)
        site = client.post("/api/sites", headers=ORIGIN,
                           json={"name": "Owner private source", "url": "https://owner.example/"}).json()
        owner = app.state.manager.active[app.state.manager.identify(token_a)]
        owner.app.state.store.mutate(lambda state: next(item for item in state["sites"]
            if item["id"] == site["id"]).update(sessionSaved=True))

        visit(client, app)
        for path in (
            f"/api/assets/{asset['id']}/file",
            f"/api/assets/{asset['id']}/versions/original/file",
            f"/api/assets/{asset['id']}/thumbnail",
            f"/api/scans/{scan['id']}/archive",
            f"/api/intake/{scan['id']}/results",
        ):
            assert client.get(path).status_code == 404, path
        assert client.patch(f"/api/assets/{asset['id']}", headers=ORIGIN,
                            json={"favorite": True}).status_code == 404
        assert client.post(f"/api/jobs/{job['id']}/action", headers=ORIGIN,
                           json={"action": "cancel"}).status_code == 404
        assert client.post(f"/api/scans/{scan['id']}/episodes", headers=ORIGIN,
                           json={"episodeKeys": ["1"]}).status_code == 404
        assert client.post("/api/library/archive", headers=ORIGIN,
                           json={"ids": [asset["id"]]}).status_code == 400
        assert client.post("/api/downloads", headers=ORIGIN,
                           json={"ids": [asset["id"]]}).json() == {"added": 0, "blocked": 1, "duplicates": 0}
        assert client.post(f"/api/sites/{site['id']}/session", headers=ORIGIN).status_code in {403, 404}
        other = client.get("/api/state").json()
        assert all(item["id"] != site["id"] and not item["sessionSaved"] for item in other["sites"])

        restored, _ = visit(client, app, token_a)
        assert not restored.json()["assets"][0]["favorite"]
        assert restored.json()["jobs"][0]["status"] == "completed"
        assert client.get(f"/api/assets/{asset['id']}/file").content == b"verified-file-fixture"


def test_inflight_file_stream_keeps_workspace_until_response_finishes(tmp_path):
    app, _, clock, _ = setup(tmp_path, retention_seconds=100, idle_unload_seconds=10)
    with TestClient(app, base_url="https://testserver") as client:
        _, token = visit(client, app)
        _, asset, _ = saved_fixture(client)
        manager = app.state.manager
        tenant = manager.active[manager.identify(token)]

        async def stream_with_cleanup():
            streaming, finish = asyncio.Event(), asyncio.Event()
            scope = {"type": "http", "http_version": "1.1", "method": "GET", "scheme": "https",
                     "path": f"/api/assets/{asset['id']}/file", "root_path": "", "query_string": b"",
                     "headers": [(b"host", b"testserver"),
                                 (b"cookie", f"{manager.cookie_name}={token}".encode())],
                     "client": ("127.0.0.1", 40000), "server": ("testserver", 443)}

            async def receive():
                await asyncio.Event().wait()

            async def send(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    streaming.set()
                    await finish.wait()

            response = asyncio.create_task(app(scope, receive, send))
            try:
                await asyncio.wait_for(streaming.wait(), 2)
                assert tenant.requests == 1
                clock[0] += 101
                await manager.cleanup()
                assert tenant.path.exists() and tenant.identifier in manager.active
            finally:
                finish.set()
                await asyncio.wait_for(response, 2)
            assert tenant.requests == 0

        client.portal.call(stream_with_cleanup)
