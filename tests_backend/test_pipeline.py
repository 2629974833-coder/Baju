import time

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from tests_backend.fixture_server import media_fixture


def wait_for(client, predicate):
    for _ in range(180):
        state = client.get("/api/state").json()
        if predicate(state):
            return state
        time.sleep(0.1)
    raise AssertionError(state)


def test_real_authorization_scan_download_and_restart(tmp_path):
    with media_fixture() as fixture:
        settings = Settings(data_dir=tmp_path, allow_private_hosts={"127.0.0.1"})
        with TestClient(create_app(settings)) as client:
            site = client.post(
                "/api/sites", json={"name": "Controlled fixture", "url": fixture.url}
            ).json()
            session = f"/api/sites/{site['id']}/session"
            scan = {"siteId": site["id"], "url": fixture.url + "/gallery", "limit": 1, "depth": 0}
            assert client.post("/api/scans", json=scan).status_code == 200
            state = wait_for(
                client, lambda state: state["scans"][0]["status"] not in {"queued", "running"}
            )
            assert state["scans"][0]["status"] == "needs-user"
            assert not state["assets"]
            assert client.post(session).json()["open"]
            assert client.get(session + "/screenshot").content.startswith(b"\xff\xd8")
            for action in [
                {"type": "text", "text": "fixture-user"},
                {"type": "key", "key": "Tab"},
                {"type": "text", "text": "fixture-password"},
                {"type": "key", "key": "Enter"},
            ]:
                assert client.post(session + "/input", json=action).status_code == 200
            assert client.post(session + "/save").json()["saved"]
            assert client.post("/api/scans", json=scan).status_code == 200
            state = wait_for(client, lambda state: state["scans"][0]["status"] == "completed")
            assert {asset["kind"] for asset in state["assets"]} == {"image", "video"}
            assert (
                client.post(
                    "/api/downloads", json={"ids": [a["id"] for a in state["assets"]]}
                ).json()["added"]
                == 2
            )
            state = wait_for(
                client, lambda state: all(j["status"] == "completed" for j in state["jobs"])
            )
            photo = next(a for a in state["assets"] if a["kind"] == "image")
            movie = next(a for a in state["assets"] if a["kind"] == "video")
            assert photo["sha256"] == fixture.sha256
            assert client.get(photo["fileUrl"]).content == fixture.photo
            assert client.get(movie["fileUrl"]).content == fixture.video
            assert client.get(photo["thumbnail"]).headers["content-type"] == "image/jpeg"
            assert client.get(movie["fileUrl"], headers={"Range": "bytes=0-7"}).status_code == 206
            assert all(
                authorized
                for path, authorized in fixture.requests
                if path in {"/photo.png", "/movie.mp4"}
            )
        with TestClient(create_app(settings)) as client:
            state = client.get("/api/state").json()
            assert len(state["assets"]) == 2 and all(
                a["status"] == "downloaded" for a in state["assets"]
            )
            assert client.post(session).json()["saved"]
            assert client.post("/api/scans", json=scan).status_code == 200
            state = wait_for(client, lambda state: state["scans"][0]["status"] == "completed")
            assert len(state["assets"]) == 2


def test_saved_session_restores_uncached_private_thumbnail(tmp_path):
    with media_fixture() as fixture:
        settings = Settings(data_dir=tmp_path, allow_private_hosts={"127.0.0.1"})
        with TestClient(create_app(settings)) as client:
            site = client.post(
                "/api/sites", json={"name": "Thumbnail fixture", "url": fixture.url}
            ).json()
            session = f"/api/sites/{site['id']}/session"
            assert client.post(session).status_code == 200
            for action in [
                {"type": "text", "text": "fixture-user"},
                {"type": "key", "key": "Tab"},
                {"type": "text", "text": "fixture-password"},
                {"type": "key", "key": "Enter"},
            ]:
                assert client.post(session + "/input", json=action).status_code == 200
            assert client.post(session + "/save").status_code == 200
            client.post(
                "/api/scans",
                json={
                    "siteId": site["id"],
                    "url": fixture.url + "/gallery",
                    "limit": 1,
                    "depth": 0,
                    "kinds": ["image"],
                },
            )
            state = wait_for(client, lambda state: state["scans"][0]["status"] == "completed")
            thumbnail = state["assets"][0]["thumbnail"]
        with TestClient(create_app(settings)) as client:
            response = client.get(thumbnail)
            assert response.headers["content-type"] == "image/jpeg"
            assert response.content.startswith(b"\xff\xd8")
