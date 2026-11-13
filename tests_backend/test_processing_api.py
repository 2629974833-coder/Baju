import io
import json
import zipfile
from copy import deepcopy

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.site_adapters import build_catalog
from tests_backend.test_app import Browser, Downloader, Policy, Scanner, until
from tests_backend.test_processing_queue import Engine, workspace
from tests_backend.test_series_api import saved_series


def processing_app(tmp_path, **settings):
    workspace(tmp_path)
    return create_app(
        Settings(data_dir=tmp_path, **settings),
        browser=Browser(),
        scanner=Scanner(),
        downloader=Downloader(tmp_path),
        policy=Policy(),
        processor=Engine(tmp_path),
    )


def test_library_form_export_streams_attachment_and_validates_input(tmp_path):
    app = processing_app(tmp_path)
    payload, _ = saved_series(app, tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/library/archive-file", data={
            "payload": json.dumps({"ids": ["a1"], "variant": "original"}),
        })
        assert response.status_code == 200
        assert response.headers["content-disposition"].startswith("attachment;")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            videos = [name for name in archive.namelist() if name.endswith(".mp4")]
            assert len(videos) == 1 and archive.read(videos[0]) == payload
        assert client.post("/api/library/archive-file", json={"ids": ["a1"]}).status_code == 415
        assert client.post("/api/library/archive-file", data={"payload": "invalid"}).status_code == 422
        assert client.post("/api/library/archive-file", data={"payload": "x" * (4 * 1024**2)}).status_code == 413
        assert client.post("/api/library/archive-file", data={
            "payload": json.dumps({"ids": ["unknown"], "variant": "original"}),
        }).status_code == 400


def test_processing_api_versions_range_archive_and_no_path_exposure(tmp_path):
    with TestClient(processing_app(tmp_path)) as client:
        assert client.get("/api/capabilities").json()["processing"] is True
        response = client.post(
            "/api/processes", json={"ids": ["sample"], "options": {"mode": "add", "text": "SAMPLE"}}
        )
        assert response.status_code == 200 and response.json()["added"] == 1
        state = until(client, lambda s: s["processingJobs"][0]["status"] == "completed")
        version = state["assets"][0]["versions"][-1]
        assert "_path" not in version
        saved = client.get(version["fileUrl"])
        assert saved.status_code == 200 and saved.headers["content-type"] == "image/png"
        chunk = client.get(version["fileUrl"], headers={"Range": "bytes=0-15"})
        assert chunk.status_code == 206 and chunk.content == saved.content[:16]
        assert (
            client.get(version["fileUrl"] + "?download=true")
            .headers["content-disposition"]
            .startswith("attachment")
        )
        response = client.post(
            "/api/library/archive", json={"ids": ["sample"], "variant": "latest"}
        )
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            images = [name for name in archive.namelist() if name.endswith(".png")]
            assert len(images) == 1 and "/图片/" in images[0]
            assert archive.read(images[0]) == saved.content
            manifest = json.loads(archive.read("Framekeep/manifest.json"))
            assert manifest["items"][0]["versionId"] == version["id"]
        assert client.get("/api/assets/sample/versions/unknown/file").status_code == 404
        assert (
            client.post(
                "/api/processes", json={"ids": ["sample"], "options": {"mode": "remove"}}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/processes",
                json={"ids": ["sample"], "options": {"mode": "add", "extra": True}},
            ).status_code
            == 400
        )


def test_runtime_and_backup_endpoints_use_actual_workspace_state(tmp_path):
    with TestClient(processing_app(tmp_path)) as client:
        status = client.get("/api/operations").json()
        assert "disk" in status and "workers" in status and "dependencies" in status
        assert status["queues"]["processing"]["queued"] == 0
        response = client.post("/api/maintenance/backups", json={})
        assert response.status_code == 200, response.text
        backup = response.json()
        assert backup["verified"] is True and backup["fileCount"] >= 2
        assert not backup["path"].startswith("/")
        listed = client.get("/api/maintenance/backups").json()["backups"]
        assert len(listed) == 1 and listed[0]["id"] == backup["id"]
        verified = client.post(f"/api/maintenance/backups/{backup['id']}/verify", json={})
        assert verified.status_code == 200 and verified.json()["verified"] is True
        assert (
            client.post(
                "/api/maintenance/backups/not-an-existing-backup/verify", json={}
            ).status_code
            == 404
        )
        assert client.get("/api/state").json()["assets"][0]["id"] == "sample"


def test_backup_refuses_active_or_queued_work_and_low_storage_is_explicit(tmp_path):
    app = processing_app(tmp_path)
    app.state.store.mutate(
        lambda state: state["scans"].append({"id": "scan_fixture", "status": "queued"})
    )
    with TestClient(app) as client:
        # Startup recovers interrupted scan records, so mark a currently running scan explicitly.
        app.state.store.mutate(lambda state: state["scans"][0].update(status="running"))
        assert client.post("/api/maintenance/backups", json={}).status_code == 409
        app.state.storage.min_free_bytes = 10**30
        assert client.get("/api/ready").status_code == 503
        response = client.post(
            "/api/processes", json={"ids": ["sample"], "options": {"text": "SAMPLE"}}
        )
        assert response.status_code == 507


def test_delete_completed_job_keeps_both_downloadable_versions(tmp_path):
    with TestClient(processing_app(tmp_path)) as client:
        client.post("/api/processes", json={"ids": ["sample"], "options": {"text": "ARCHIVE"}}).raise_for_status()
        state = until(client, lambda s: s["processingJobs"][0]["status"] == "completed")
        job, asset = state["processingJobs"][0], state["assets"][0]
        file_urls = ["/api/assets/sample/file", asset["versions"][-1]["fileUrl"]]
        before = [client.get(url).raise_for_status().content for url in file_urls]
        url = f"/api/processes/{job['id']}/action"
        response = client.post(url, json={"action": "delete"})
        assert response.status_code == 200, response.text
        assert response.json()["deleted"]
        assert client.post(url, json={"action": "delete"}).json()["deleted"] is False
        assert client.get("/api/state").json()["processingJobs"] == []
        assert [client.get(url).raise_for_status().content for url in file_urls] == before


def test_library_exports_original_and_processed_under_the_custom_series_title(tmp_path):
    app = processing_app(tmp_path)
    payload, path = saved_series(app, tmp_path)
    processed = path.with_name("cut.mp4")
    processed.write_bytes(b"processed version")
    version = {
        "id": "cut",
        "kind": "processed",
        "status": "completed",
        "_path": str(processed.relative_to(tmp_path)),
    }
    app.state.store.mutate(
        lambda state: next(a for a in state["assets"] if a["id"] == "a1").update(versions=[version])
    )
    with TestClient(app) as client:
        series = client.get("/api/state").json()["series"][0]
        client.put(
            "/api/series/" + series["id"] + "/title", json={"title": "山海记"}
        ).raise_for_status()
        for variant, content in (("original", payload), ("latest", b"processed version")):
            response = client.post("/api/library/archive", json={"ids": ["a1"], "variant": variant})
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                assert {name.split("/")[0] for name in archive.namelist()} == {"山海记"}
                manifest = json.loads(archive.read("山海记/manifest.json"))
                file = manifest["items"][0]["file"]
                assert file.count("/") == 1 and archive.read(file) == content


def test_library_mixed_export_keeps_distinct_show_folders_and_reserved_root_names(tmp_path):
    app = processing_app(tmp_path)
    payload, _ = saved_series(app, tmp_path)

    def seed(state):
        state["scans"][0]["catalog"]["title"] = "Framekeep"
        template = next(a for a in state["assets"] if a["id"] == "a1")
        for code, title in [("C05", "FRAMEKEEP"), ("C06", "manifest.json")]:
            page = f"https://lustflix.tv/#/watch/{code}/1"
            catalog = build_catalog(page, [{"url": page}], expected_count=1)
            catalog["title"] = title
            state["scans"].append(
                {
                    "id": code,
                    "siteId": "lustflix",
                    "url": page,
                    "status": "completed",
                    "catalog": catalog,
                }
            )
            state["assets"].append({**template, "id": code, "pageUrl": page})

    app.state.store.mutate(seed)
    with TestClient(app) as client:
        response = client.post(
            "/api/library/archive",
            json={
                "ids": ["sample", "a1", "C05", "C06"],
                "variant": "original",
            },
        ).raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            files = {i["assetId"]: i["file"] for i in manifest["items"]}
            assert files["sample"].startswith("Framekeep/")
            assert len({f.split("/")[0].casefold() for f in files.values()}) == 4
            for identifier in ["a1", "C05", "C06"]:
                path = files[identifier]
                assert path.count("/") == 1 and path.split("/")[0].casefold() != "manifest.json"
                assert archive.read(path) == payload


def test_library_archive_only_promotes_an_unambiguous_episode_match(tmp_path):
    app = processing_app(tmp_path)
    saved_series(app, tmp_path)

    def add_overlapping_catalog(state):
        other = deepcopy(state["scans"][0])
        other["id"] = "second"
        other["catalog"].update(seriesKey="another", title="另一部剧")
        state["scans"].append(other)

    app.state.store.mutate(add_overlapping_catalog)
    with TestClient(app) as client:

        def file_path(folder):
            response = client.post(
                "/api/library/archive", json={"ids": ["a1"], "variant": "original"}
            )
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                manifest = json.loads(archive.read(folder + "/manifest.json"))
                return manifest["items"][0]["file"]

        assert file_path("Framekeep").startswith("Framekeep/Lustflix/视频/")
        app.state.store.mutate(
            lambda state: state["scans"][1]["catalog"]["entries"][0].update(access="needs-user")
        )
        assert file_path("C04").startswith("C04/")
