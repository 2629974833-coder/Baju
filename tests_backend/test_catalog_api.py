from fastapi.testclient import TestClient
from test_app import app_at

from backend.site_adapters import build_catalog


def setup_catalog(app):
    page = "https://lustflix.tv/#/watch/C04/1"
    catalog = build_catalog(
        page, [{"url": page}, {"url": page[:-1] + "2", "access": "needs-user"}], expected_count=2
    )
    catalog["entries"][0]["assetIds"] = ["a1", "v1"]

    def seed(state):
        state["scans"].append(
            {
                "id": "s1",
                "siteId": "lustflix",
                "url": page,
                "status": "completed",
                "catalog": catalog,
            }
        )
        for identifier, role in [("a1", "master"), ("v1", "stream_0")]:
            state["assets"].append(
                {
                    "id": identifier,
                    "siteId": "lustflix",
                    "pageUrl": page,
                    "kind": "video",
                    "engine": "yt-dlp",
                    "status": "discovered",
                    "sourceUrl": f"https://lustflix.tv/api/media/a/1/{role}.m3u8",
                }
            )

    app.state.store.mutate(seed)


def test_catalog_api_accepts_only_observed_episode_keys(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        setup_catalog(app)
        assert (
            client.post("/api/scans/s1/episodes", json={"episodeKeys": ["invented"]}).status_code
            == 400
        )
        response = client.post("/api/scans/s1/episodes", json={"episodeKeys": ["C04:1"]})
        assert response.status_code == 200
        assert response.json()["url"] == "https://lustflix.tv/#/watch/C04/1"
        assert (
            client.post(
                "/api/scans/s1/episodes",
                json={"episodeKeys": [], "urls": ["https://external.invalid"]},
            ).status_code
            == 422
        )


def test_catalog_download_selects_one_master_and_reports_unavailable(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        setup_catalog(app)
        response = client.post("/api/scans/s1/downloads", json={"episodeKeys": ["C04:1", "C04:2"]})
        assert response.status_code == 200
        result = response.json()
        assert result["added"] == 1 and result["unavailable"] == ["C04:2"]
        assert app.state.store.read()["jobs"][0]["assetId"] == "a1"


def test_stale_catalog_cannot_download_preview_evidence(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        setup_catalog(app)

        def change(state):
            for asset in state["assets"]:
                asset["_mediaLabel"] = "Official trailer"

        app.state.store.mutate(change)
        result = client.post("/api/scans/s1/downloads", json={"episodeKeys": ["C04:1"]}).json()
        assert result["added"] == 0 and result["unavailable"] == ["C04:1"]
