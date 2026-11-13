import hashlib
import io
import json
import zipfile
from urllib.parse import unquote

from fastapi.testclient import TestClient

from tests_backend.test_app import app_at, until


def test_single_link_rescan_keeps_only_its_own_results_and_original(tmp_path):
    app = app_at(tmp_path)
    payload = {"scope": "single", "url": "https://example.com/watch/1"}
    with TestClient(app) as client:
        scan = client.post("/api/intake", json=payload).raise_for_status().json()
        until(client, lambda s: s["scans"][0]["status"] == "completed")
        path = "/api/intake/" + scan["id"]
        result = client.get(path + "/results").raise_for_status().json()
        assert result["scope"] == "single" and len(result["groups"]) == 1
        ids = result["groups"][0]["assetIds"]
        assert len(ids) == 1
        assert client.post(path + "/archive", json={"ids": ids}).status_code == 409
        client.post("/api/downloads", json={"ids": ids}).raise_for_status()
        until(client, lambda s: s["jobs"][0]["status"] == "completed")
        second = client.post("/api/intake", json=payload).raise_for_status().json()
        until(client, lambda s: s["scans"][0]["status"] == "completed")
        repeat = client.get("/api/intake/" + second["id"] + "/results").json()
        assert repeat["groups"][0]["assetIds"] == ids
    with TestClient(app_at(tmp_path)) as client:
        assert client.get(path + "/results").json()["groups"][0]["assetIds"] == ids
        assert (
            client.post(path + "/archive", json={"ids": ids + ["not-an-observation"]}).status_code
            == 400
        )
        archive = client.post(path + "/archive", json={"ids": ids}).raise_for_status()
        with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
            (name,) = [n for n in z.namelist() if not n.endswith("/manifest.json")]
            assert "/图片/" in name
            assert z.read(name) == client.get("/api/assets/" + ids[0] + "/file").content


def test_whole_site_multi_membership_colliding_titles_and_selection(tmp_path):
    app = app_at(tmp_path)
    groups = []
    refs = {}
    origin = "https://example.com"
    data = b"shared art"
    path = tmp_path / "assets" / "art" / "original.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    for n in [1, 2]:
        url = f"{origin}/show/{n}"
        page = url + "/episode/1"
        groups.append(
            {
                "title": " 同/名 ",
                "url": url,
                "category": "剧情",
                "catalog": {
                    "entries": [
                        {"episodeKey": "1", "ordinal": 1, "title": "开篇", "pageUrl": page},
                    ]
                },
            }
        )
        refs[url] = {page: ["art"]}

    def seed(state):
        state["scans"].insert(
            0,
            {
                "id": "mine",
                "siteId": "source",
                "url": origin,
                "intakeScope": "site",
                "siteCatalog": {"shows": groups, "limited": True},
                "status": "completed",
                "_intakeRefs": refs,
            },
        )
        state["assets"].append(
            {
                "id": "art",
                "siteId": "source",
                "title": "宣传图",
                "kind": "image",
                "_path": "assets/art/original.png",
                "status": "downloaded",
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )

    with TestClient(app) as client:
        app.state.store.mutate(seed)
        url = "/api/intake/mine"
        r = client.get(url + "/results").raise_for_status().json()
        assert r["limited"] and len(r["groups"]) == 2
        for group in r["groups"]:
            assert group["episodes"][0]["assetIds"] == ["art"] and group["assetIds"] == []

        def paths(selection, manifest_path):
            response = client.post(
                url + "/archive", json={"ids": ["art"], **selection}
            ).raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                listed = [item["file"] for item in json.loads(z.read(manifest_path))["items"]]
                assert set(z.namelist()) == {manifest_path, *listed}
                assert all(z.read(name) == data for name in listed)
                return listed

        both = paths({}, "manifest.json")
        assert both == [
            "同_名/E001 开篇/图片/宣传图.png",
            "同_名 (2)/E001 开篇/图片/宣传图.png",
        ]
        assert paths(
            {"groupKey": r["groups"][1]["key"], "episodeKey": "1"}, "同_名/manifest.json"
        ) == [both[0]]
        app.state.store.mutate(
            lambda s: s["scans"][0]["siteCatalog"]["shows"][0].update(title="MANIFEST.JSON")
        )
        assert paths({}, "manifest.json")[0].startswith("MANIFEST.JSON (2)/")
        app.state.store.mutate(lambda s: s["scans"][0]["_intakeRefs"].pop(r["groups"][0]["key"]))
        assert paths({}, "同_名/manifest.json") == [both[0]]
        assert (
            client.post(url + "/archive", json={"ids": ["art"], "groupKey": "missing"}).status_code
            == 400
        )


def test_local_exports_when_exact_page_requires_auth_and_form_selection_is_validated(tmp_path):
    app = app_at(tmp_path)
    url = "https://example.com/watch/4"
    payload = b"an earlier verified original"
    path = tmp_path / "assets" / "here" / "original.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)

    def seed(state):
        state["scans"].append(
            {
                "id": "stopped",
                "siteId": "site",
                "intakeScope": "single",
                "url": url,
                "status": "needs-user",
                "message": "请处理登录",
            }
        )
        state["assets"].extend(
            {
                "id": identifier,
                "siteId": "site",
                "pageUrl": page,
                "kind": "video",
                "_path": "assets/here/original.mp4",
                "status": "downloaded",
            }
            for identifier, page in [("here", url), ("other", url + "0")]
        )

    with TestClient(app) as client:
        app.state.store.mutate(seed)
        route = "/api/intake/stopped"
        result = client.get(route + "/results").json()
        assert result["status"] == "needs-user" and result["groups"][0]["localOnlyIds"] == ["here"]
        assert result["groups"][0]["assetIds"] == ["here"]
        data = {"payload": '{"ids":["here"]}'}
        response = client.post(route + "/archive-file", data=data)
        assert response.is_success and response.headers["x-frame-options"] == "SAMEORIGIN"
        assert "当前单集或作品.zip" in unquote(response.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            assert set(z.namelist()) == {
                "当前单集或作品/manifest.json",
                "当前单集或作品/剧集素材/视频/视频.mp4",
            }
        assert client.get("/").headers["referrer-policy"] == "same-origin"
        assert (
            client.post(
                route + "/archive-file", data=data, headers={"origin": "https://elsewhere.test"}
            ).status_code
            == 403
        )
        assert (
            client.post(route + "/archive-file", data={"payload": '{"ids":["other"]}'}).status_code
            == 400
        )
        assert client.post(route + "/archive-file", data={"payload": "broken"}).status_code == 422


def test_continue_after_restart_preserves_observations_and_repeated_submit_reuses_child(tmp_path):
    scan_url = "https://example.com/series/one"
    app = app_at(tmp_path)

    def seed(state):
        state["sites"].append({"id": "local", "url": scan_url, "name": "test"})
        state["scans"].append(
            {
                "id": "first",
                "siteId": "local",
                "intakeScope": "series",
                "url": scan_url,
                "intakeKinds": ["image"],
                "status": "completed",
                "canContinue": True,
                "limit": 1,
                "_intakeRefs": {"": {scan_url: ["previous"]}},
                "_intakeCheckpoint": {"sample": True},
            }
        )

    app.state.store.mutate(seed)
    app = app_at(tmp_path)
    scanner = app.state.tasks.scanner
    capture = []
    original = scanner.scan

    async def check(site, options, on_asset, progress, cancelled):
        capture.append(options)
        await original(site, options, on_asset, progress, cancelled)
        await progress({"canContinue": False})

    scanner.scan = check
    with TestClient(app) as client:
        path = "/api/intake/first"
        child = client.post(path + "/continue", json={"limit": 3}).raise_for_status().json()["id"]
        assert client.post(path + "/continue", json={}).json()["id"] == child
        until(client, lambda s: s["scans"][0]["status"] == "completed")
        results = client.get("/api/intake/" + child + "/results").json()
        assert results["previousScanId"] == "first" and results["canContinue"] is False
        assert "previous" in results["groups"][0]["assetIds"]
        assert len(capture) == 1 and capture[0]["_resumeIntake"]["_intakeCheckpoint"] == {
            "sample": True
        }
        old = client.get(path + "/results").json()
        assert not old["canContinue"] and old["nextScanId"] == child
        assert client.post("/api/intake/" + child + "/continue", json={}).status_code == 409
