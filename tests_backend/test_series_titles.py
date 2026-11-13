import io
import zipfile
from copy import deepcopy
from urllib.parse import unquote

from fastapi.testclient import TestClient

from tests_backend.test_app import app_at, until
from tests_backend.test_series_api import saved_series


def test_custom_name_survives_rescan_and_restart_and_can_be_cleared(tmp_path):
    app = app_at(tmp_path)
    payload, _ = saved_series(app, tmp_path)
    with TestClient(app) as client:
        state = client.get("/api/state").json()
        path = "/api/series/" + state["series"][0]["id"]
        assert client.put(path + "/title", json={"title": "  山海记  "}).status_code == 200
        assert client.put(path + "/title", json={"title": "剧名\n换行"}).status_code == 422
        assert client.put("/api/series/invented/title", json={"title": "名字"}).status_code == 404

        def rescan(state):
            fresh = deepcopy(state["scans"][0])
            fresh["id"] = "new"
            fresh["catalog"]["title"] = "来源新剧名"
            state["scans"].insert(0, fresh)

        app.state.store.mutate(rescan)
    with TestClient(app_at(tmp_path)) as client:
        state = client.get("/api/state").json()
        row = state["series"][0]
        assert (row["customTitle"], row["title"], row["sourceTitle"]) == ("山海记", "山海记", "来源新剧名")
        assert state["scans"][0]["customSeriesTitle"] == "山海记"
        assert state["scans"][0]["catalog"]["title"] == "来源新剧名"
        for archive_path in [path + "/archive", "/api/scans/s1/archive"]:
            response = client.get(archive_path)
            assert unquote(response.headers["content-disposition"]).endswith("''山海记.zip")
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                name = next(n for n in archive.namelist() if n.endswith(".mp4"))
                assert archive.namelist() == ["山海记/manifest.json", name]
                assert name.split("/") == ["山海记", "Lustflix_山海记_E001_720p.mp4"]
                assert archive.read(name) == payload
        assert client.put(path + "/title", json={"title": ""}).status_code == 200
        restored = client.get("/api/state").json()["series"][0]
        assert restored["title"] == "来源新剧名" and restored["customTitle"] == ""


def test_first_scan_binds_suggested_title_to_identified_series(tmp_path):
    app = app_at(tmp_path)

    async def observe(site, options, asset_found, progress, cancelled):
        from tests_backend.test_series_library import catalog
        cat = catalog(origin="https://example.com", key="known")
        await progress({"status": "completed", "catalog": cat})

    app.state.tasks.scanner.scan = observe
    with TestClient(app) as client:
        body = {"url": "https://example.com/show/first", "customTitle": "自选名称"}
        response = client.post("/api/series", json=body)
        assert response.status_code == 200
        state = until(client, lambda s: s.get("series") and s["scans"][0].get("finishedAt"))
        assert state["series"][0]["title"] == "自选名称"
        sid = state["series"][0]["id"]
        assert client.put(f"/api/series/{sid}/title", json={"title": "改过的名称"}).status_code == 200
        assert client.post("/api/series", json=body).status_code == 200
        state = until(client, lambda s: s["scans"][0].get("finishedAt"))
        assert state["series"][0]["title"] == "改过的名称"
