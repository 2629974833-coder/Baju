import copy
import importlib
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend.archive import download_filename
from backend.site_adapters import build_catalog
from tests_backend.test_app import app_at, until


def library(state):
    try:
        module = importlib.import_module("backend.series")
    except ImportError:
        pytest.fail("A persistent series projection is not implemented")
    return module.series_library(state)


def catalog(numbers=(1, 2, 3), *, origin="https://example.com", key="garden"):
    result = build_catalog(
        origin + "/series/" + key,
        [{"url": f"{origin}/{key}/episode/{n}", "ordinal": n} for n in numbers],
        expected_count=3,
    )
    result.update(
        seriesKey=key, scope="series", title="花园观察", catalogUrl=origin + "/series/" + key
    )
    return result


def scan(identifier, cat, **fields):
    return {
        "id": identifier,
        "siteId": "example",
        "url": cat["catalogUrl"],
        "status": "completed",
        "catalog": cat,
        "createdAt": "2026-11-03T00:00:00Z",
        **fields,
    }


def seed(app):
    cat = catalog()
    cat["entries"][2]["access"] = "needs-user"

    def mutate(state):
        state["sites"].append({"id": "example", "name": "本地课程", "url": "https://example.com"})
        state["scans"] = [scan("first", cat)]
        state["assets"] = [
            {
                "id": "ep1",
                "title": "Episode 1",
                "siteId": "example",
                "kind": "video",
                "engine": "http",
                "status": "discovered",
                "sourceUrl": "https://example.com/media/episode-1.mp4",
                "pageUrl": cat["entries"][0]["pageUrl"],
            }
        ]

    app.state.store.mutate(mutate)


def test_library_merges_partial_scans_and_clears_only_new_observed_access():
    old = catalog((1, 2))
    old["entries"][0]["access"] = "needs-user"
    new = catalog((1, 3))
    new["entries"][0]["access"] = "observed"
    state = {
        "scans": [
            scan("new", new, _pageAccess={new["entries"][0]["pageUrl"]: "observed"}),
            scan("old", old),
        ],
        "assets": [],
    }
    rows = library(state)
    assert len(rows) == 1
    assert rows[0]["scanId"] == "new"
    assert rows[0]["sourceScanIds"] == ["old", "new"]
    assert rows[0]["title"] == "花园观察"
    assert [e["ordinal"] for e in rows[0]["catalog"]["entries"]] == [1, 2, 3]
    assert rows[0]["catalog"]["entries"][0]["access"] == "observed"
    assert rows[0]["id"] == library({"scans": [scan("old", old)], "assets": []})[0]["id"]
    assert old["entries"][0]["access"] == "needs-user"


def test_library_keeps_origins_sites_and_series_separate():
    scans = [
        scan("a", catalog()),
        scan("b", catalog(origin="https://example.com:444")),
        scan("c", catalog(key="ocean")),
        scan("d", catalog(), siteId="another"),
    ]
    invalid = catalog()
    invalid["entries"][0]["pageUrl"] = "https://elsewhere.example/watch/1"
    scans.append(scan("invalid", invalid))
    scans.append({"id": "empty", "mode": "series"})
    rows = library({"scans": scans, "assets": []})
    assert [row["sourceScanIds"] for row in rows] == [["a"], ["b"], ["c"], ["d"]]


def test_library_rejects_cross_origin_entries_and_ignores_empty_catalogs():
    bad = catalog()
    bad["entries"][0]["pageUrl"] = "https://elsewhere.example/episode/1"
    assert (
        library({"scans": [scan("bad", bad), {"id": "empty", "mode": "series"}], "assets": []})
        == []
    )


def test_state_exposes_aggregated_series_without_private_fields(tmp_path):
    app = app_at(tmp_path)
    seed(app)
    with TestClient(app) as client:
        result = client.get("/api/state").json()
        assert len(result.get("series", [])) == 1
        row = result["series"][0]
        assert row["catalog"]["resolvedCount"] == 1
        assert row["catalog"]["blockedCount"] == 1
        assert "_headers" not in str(row)


def test_history_media_does_not_clear_a_gate_or_disagree_with_download_api(tmp_path):
    app = app_at(tmp_path)
    seed(app)

    def observed_elsewhere(state):
        prior = state["scans"][0]
        prior["catalog"]["entries"][0]["access"] = "needs-user"
        newer = catalog()
        newer["entries"][0]["access"] = "observed"  # Derived earlier from an old asset.
        state["scans"].insert(
            0, scan("new", newer, _pageAccess={newer["entries"][1]["pageUrl"]: "observed"})
        )

    app.state.store.mutate(observed_elsewhere)
    with TestClient(app) as client:
        row = client.get("/api/state").json()["series"][0]
        assert row["catalog"]["entries"][0]["mediaStatus"] == "needs-user"
        key = row["catalog"]["entries"][0]["episodeKey"]
        response = client.post(
            f"/api/series/{row['id']}/downloads", json={"episodeKeys": [key]}
        ).json()
        assert (
            response["added"] == 0 and response["unavailableDetails"][0]["reason"] == "needs-user"
        )
        assert "_pageAccess" not in str(client.get("/api/state").json())


def test_single_url_starts_series_scan_and_persists_mode(tmp_path):
    app = app_at(tmp_path)
    options = []
    original = app.state.tasks.scanner.scan

    async def capture(site, values, *args):
        options.append(copy.deepcopy(values))
        return await original(site, values, *args)

    app.state.tasks.scanner.scan = capture
    with TestClient(app) as client:
        response = client.post(
            "/api/series", json={"url": "https://example.com/series/garden", "limit": 500}
        )
        assert response.status_code == 200
        state = until(client, lambda s: s["scans"][0]["status"] == "completed")
        assert state["scans"][0]["mode"] == "series"
        assert options[0]["mode"] == "series" and options[0]["limit"] == 500
        assert options[0]["kinds"] == ["video"]


def test_continue_series_scans_only_pending_observed_links(tmp_path):
    app = app_at(tmp_path)
    seed(app)
    captured = []
    original = app.state.tasks.scanner.scan

    async def capture(site, options, *args):
        captured.append(copy.deepcopy(options))
        return await original(site, options, *args)

    app.state.tasks.scanner.scan = capture
    with TestClient(app) as client:
        rows = client.get("/api/state").json().get("series", [])
        assert rows, "series library is missing"
        identifier = rows[0]["id"]
        response = client.post(f"/api/series/{identifier}/scan", json={})
        assert response.status_code == 200
        until(client, lambda s: s["scans"][0]["status"] == "completed")
        assert captured[0]["seedUrls"] == ["https://example.com/garden/episode/2"]
        assert captured[0]["catalog"]["expectedCount"] == 3
        assert captured[0]["followCatalog"] is False
        assert response.json()["skippedEpisodeKeys"] == [catalog()["entries"][2]["episodeKey"]]


def test_series_batch_download_options_and_episode_validation(tmp_path):
    app = app_at(tmp_path)
    seed(app)
    calls = []

    def enqueue(state, ids, options):
        calls.append((ids, options))
        return {"added": len(ids), "blocked": 0, "duplicates": 0}

    app.state.tasks.enqueue_in_state = enqueue
    with TestClient(app) as client:
        rows = client.get("/api/state").json().get("series", [])
        assert rows, "series library is missing"
        identifier = rows[0]["id"]
        response = client.post(
            f"/api/series/{identifier}/downloads", json={"options": {"maxHeight": 720}}
        )
        assert response.status_code == 200 and response.json()["added"] == 1
        assert calls == [(["ep1"], {"maxHeight": 720, "audioLanguage": "auto"})]
        assert (
            client.post(
                f"/api/series/{identifier}/scan", json={"episodeKeys": ["invented"]}
            ).status_code
            == 400
        )
        assert (
            client.post(f"/api/series/{identifier}/downloads", json={"episodeKeys": []}).status_code
            == 422
        )
        assert client.post("/api/series/missing/downloads", json={}).status_code == 404


def test_series_projection_survives_database_reopen(tmp_path):
    app = app_at(tmp_path)
    seed(app)
    with TestClient(app) as client:
        first = client.get("/api/state").json().get("series", [])
        assert first
    with TestClient(app_at(tmp_path)) as client:
        assert client.get("/api/state").json()["series"] == first


def test_series_batch_repeat_is_deduplicated_and_archive_matches_saved_entries(tmp_path):
    app = app_at(tmp_path)
    seed(app)
    with TestClient(app) as client:
        identifier = client.get("/api/state").json()["series"][0]["id"]
        path = f"/api/series/{identifier}"
        assert client.get(path + "/archive").status_code == 409
        result = client.post(path + "/downloads", json={}).json()
        assert result["added"] == 1 and len(result["unavailable"]) == 2
        state = until(client, lambda s: s["jobs"][0]["status"] == "completed")
        assert state["series"][0]["catalog"]["downloadedCount"] == 1
        assert client.post(path + "/downloads", json={}).json()["duplicates"] == 1
        response = client.get(path + "/archive")
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert len(archive.namelist()) == 2
            assert any("E001" in name for name in archive.namelist())


def test_series_defaults_to_catalog_reread_for_unobserved_tail(tmp_path):
    app = app_at(tmp_path)
    seed(app)

    def partial(state):
        state["scans"][0]["catalog"] = catalog((1,))

    app.state.store.mutate(partial)
    captured = []
    original = app.state.tasks.scanner.scan

    async def capture(site, options, *args):
        captured.append(copy.deepcopy(options))
        return await original(site, options, *args)

    app.state.tasks.scanner.scan = capture
    with TestClient(app) as client:
        identifier = client.get("/api/state").json()["series"][0]["id"]
        response = client.post(f"/api/series/{identifier}/scan", json={})
        assert response.status_code == 200
        until(client, lambda s: s["scans"][0]["status"] == "completed")
        assert captured[0]["seedUrls"] == ["https://example.com/series/garden"]
        assert captured[0]["followCatalog"] is True


def test_series_export_uses_readable_title_and_keeps_safe_episode_suffix():
    name = download_filename(
        {"kind": "video", "_path": "assets/a/original.mp4"},
        {"name": "课程"},
        {"ordinal": 2},
        {"seriesKey": "generic-opaque", "title": "../花园/观察"},
    )
    assert name == "课程_花园_观察_E002.mp4"


def test_dismiss_catalogs_persists_by_identity_and_keeps_observations_and_saved_files(tmp_path):
    app = app_at(tmp_path)
    seed(app)
    with TestClient(app) as client:
        row = client.get("/api/state").json()["series"][0]
        path = f"/api/series/{row['id']}"
        assert client.post(path + "/downloads", json={}).json()["added"] == 1
        until(client, lambda state: state["jobs"][0]["status"] == "completed")
        file_bytes = client.get("/api/assets/ep1/file").content
        initial = app.state.store.read()
        payload = {"ids": [row["id"], row["id"], "missing"]}
        assert client.post("/api/series/dismiss", json=payload).json() == {"dismissed": 1}
        assert client.post("/api/series/dismiss", json=payload).json() == {"dismissed": 0}
        app.state.store.mutate(lambda s: s["scans"].insert(0, scan("new", catalog())))
        app.state.store.mutate(
            lambda s: s["scans"].append(
                {
                    "id": "old-empty",
                    "siteId": "example",
                    "url": row["url"],
                    "status": "failed",
                    "mode": "series",
                }
            )
        )
        state = client.get("/api/state").json()
        assert state["series"] == [] and state["dismissedSeriesCount"] == 1
        assert all(snapshot["seriesDismissed"] for snapshot in state["scans"])
        assert state["scans"][0]["catalog"]["enumeratedCount"] == 3
        assert app.state.store.read()["assets"] == initial["assets"]
        assert app.state.store.read()["jobs"] == initial["jobs"]
        assert client.get(path + "/archive").status_code == 404
        assert client.get("/api/assets/ep1/file").content == file_bytes
    with TestClient(app_at(tmp_path)) as client:
        assert client.get("/api/state").json()["series"] == []


def test_dismiss_episode_changes_series_actions_and_retains_source_coverage(tmp_path):
    app = app_at(tmp_path)
    seed(app)
    with TestClient(app) as client:
        row = client.get("/api/state").json()["series"][0]
        path = f"/api/series/{row['id']}"
        assert client.post(path + "/downloads", json={}).json()["added"] == 1
        until(client, lambda s: s["jobs"][0]["status"] == "completed")
        keys = [entry["episodeKey"] for entry in row["catalog"]["entries"]]
        payload = {"ids": [row["id"]], "episodeKeys": [keys[1], keys[1]]}
        assert client.post("/api/series/dismiss", json=payload).json() == {"dismissed": 1}
        assert client.post("/api/series/dismiss", json=payload).json() == {"dismissed": 0}
        app.state.store.mutate(lambda s: s["scans"].insert(0, scan("again", catalog())))
        state = client.get("/api/state").json()
        row = state["series"][0]
        visible = row["catalog"]
        assert [entry["episodeKey"] for entry in visible["entries"]] == [keys[0], keys[2]]
        assert visible["dismissedEpisodeCount"] == 1 and visible["enumeratedCount"] == 2
        assert visible["expectedCount"] == 3 and visible["missingCount"] == 0
        assert state["scans"][0]["catalog"]["enumeratedCount"] == 3
        assert client.post(path + "/scan", json={"episodeKeys": [keys[1]]}).status_code == 400
        assert client.post(path + "/scan", json={}).status_code == 409
        assert client.post(path + "/downloads", json={}).json()["unavailable"] == [keys[2]]
        response = client.get(path + "/archive")
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(
                archive.read(next(n for n in archive.namelist() if n.endswith(".json")))
            )
        assert [e["episodeKey"] for e in manifest["entries"]] == [keys[0], keys[2]]
        assert manifest["missingEpisodeKeys"] == [] and manifest["dismissedEpisodeCount"] == 1
        assert client.post(
            "/api/series/dismiss", json={**payload, "ids": [row["id"], "x"]}
        ).status_code in (400, 422)
        assert (
            client.post("/api/series/dismiss", json={**payload, "episodeKeys": []}).status_code
            == 422
        )
        assert client.post(
            "/api/series/dismiss", json={**payload, "episodeKeys": None}
        ).status_code in (400, 422)
        assert len(client.get("/api/state").json()["series"]) == 1


def test_selected_scan_persists_exact_playback_result_after_directory_reread_and_restart(tmp_path):
    app = app_at(tmp_path)
    seed(app)
    directory = catalog()
    target = directory["entries"][0]
    message = "此集使用站点专用密钥"
    calls = []

    async def protected(_site, options, _found, progress, _cancelled):
        calls.append(options["seedUrls"])
        await progress({
            "status": "completed", "pagesVisited": 1, "assetsFound": 0,
            "catalog": copy.deepcopy(directory),
            "_episodeOutcomes": {target["pageUrl"]: {"status": "unsupported-media", "message": message}},
        })

    app.state.tasks.scanner.scan = protected
    with TestClient(app) as client:
        row = client.get("/api/state").json()["series"][0]
        path = f"/api/series/{row['id']}"
        scan_input = {"episodeKeys": [target["episodeKey"]]}
        assert client.post(path + "/scan", json=scan_input).status_code == 200
        state = until(client, lambda s: s["scans"][0]["status"] == "completed")
        assert calls == [[target["pageUrl"]]]
        assert state["scans"][0]["catalog"]["entries"][0]["mediaStatus"] == "unsupported-media"
        assert client.post(path + "/downloads", json=scan_input).json()["unavailableDetails"] == [
            {"episodeKey": target["episodeKey"], "reason": "unsupported-media"}
        ]
        app.state.store.mutate(lambda s: s["scans"].insert(0, scan("directory-only", directory)))
    with TestClient(app_at(tmp_path)) as client:
        cat = client.get("/api/state").json()["series"][0]["catalog"]
        first, second, third = cat["entries"]
        assert first["scanMessage"] == message and first["mediaStatus"] == "unsupported-media"
        assert not first["downloadAssetId"] and cat["unsupportedCount"] == 1
        assert second["mediaStatus"] == "unresolved" and third["mediaStatus"] == "needs-user"


def test_exact_single_playback_result_updates_only_matching_site_and_new_proof_clears_it():
    cat = catalog((1,))
    entry = cat["entries"][0]
    page = entry["pageUrl"]
    old = scan("old", cat)
    def attempt(status, **fields):
        return {"siteId": old["siteId"], "url": page, "_episodeOutcomes": {
            page: {"status": status, "message": status}}, **fields}

    failed = attempt("failed")
    state = {"scans": [attempt("ready", siteId="other"), failed, old], "assets": []}
    before = library(state)[0]["catalog"]
    assert before["entries"][0]["mediaStatus"] == "failed" and before["failedCount"] == 1
    state["scans"].insert(0, attempt("ready"))
    state["assets"].append({
        "id": "newFile", "siteId": old["siteId"], "kind": "video",
        "engine": "http", "sourceUrl": "https://example.com/video/episode-1.mp4",
        "title": "Episode 1", "status": "discovered", "pageUrl": page,
    })
    after = library(state)[0]["catalog"]["entries"][0]
    assert after["mediaStatus"] == "ready" and after["downloadAssetId"] == "newFile"


def test_known_protected_media_keeps_previously_downloaded_original_available():
    cat = catalog((1,))
    entry = cat["entries"][0]
    old = scan("old", cat, _episodeOutcomes={
        entry["pageUrl"]: {"status": "unsupported-media", "message": "受保护"},
    })
    asset = {"id": "original", "kind": "video", "engine": "http", "title": "Episode 1",
             "siteId": old["siteId"], "sourceUrl": "https://example.com/videos/episode-1.mp4",
             "status": "downloaded", "pageUrl": entry["pageUrl"]}
    updated = library({"scans": [old], "assets": [asset]})[0]["catalog"]["entries"][0]
    assert updated["downloaded"] and updated["downloadAssetId"] == "original"
    assert updated.get("scanStatus") == "unsupported-media"


def test_legacy_selection_scans_all_selected_pages_without_expanding_the_directory(tmp_path):
    app = app_at(tmp_path)
    seed(app)
    with TestClient(app) as client:
        state = client.get("/api/state").json()
        # The discovery page dispatches to the source scan, rather than to the library ID.
        entries = state["scans"][0]["catalog"]["entries"]
        calls = []
        app.state.tasks.start_scan = lambda _site, options: calls.append(options) or {"id": "selected"}
        response = client.post("/api/scans/first/episodes", json={
            "episodeKeys": [entry["episodeKey"] for entry in entries[:2]],
        })
        assert response.status_code == 200
        only, = calls
        assert only["mode"] == "series" and not only["followCatalog"]
        assert only["seedUrls"] == [entry["pageUrl"] for entry in entries[:2]]


def test_purchase_observed_after_player_gate_reopens_the_episode_for_scanning():
    previous = catalog((1,))
    entry = previous["entries"][0]
    entry.update(access="needs-user", accessEvidence="player", scanStatus="needs-user", scanMessage="登录后重试")
    page = entry["pageUrl"]
    current = catalog((1,))
    current["entries"][0].update(access="observed", accessEvidence="account-unlock")
    scans = [
        scan("fresh purchase", current),
        scan("refused", previous, _episodeOutcomes={page: {"status": "needs-user", "message": "登录后重试"}},
             _pageAccess={page: "needs-user"}),
    ]
    row = library({"scans": scans, "assets": []})[0]["catalog"]["entries"][0]
    assert row["mediaStatus"] == "unresolved" and row["accessEvidence"] == "account-unlock"
    assert not row.get("scanMessage")


def test_legacy_chunlou_price_and_player_denial_remain_distinct_after_a_new_directory_read():
    old = catalog((1, 2))
    old.update(adapterId="chunlou", discoverySources=["site-directory"])
    for entry in old["entries"]:
        entry["access"] = "needs-user"
    current = copy.deepcopy(old)
    for entry in current["entries"]:
        entry["accessEvidence"] = "public-directory"
    first, second = old["entries"]
    before = scan("legacy", old, _pageAccess={second["pageUrl"]: "needs-user"})
    after = scan("new", current, _episodeOutcomes={
        first["pageUrl"]: {"status": "failed", "message": "未取得清单"},
    })
    rows = library({"scans": [after, before], "assets": []})[0]["catalog"]["entries"]
    assert (rows[0].get("accessEvidence"), rows[0]["mediaStatus"]) == ("public-directory", "failed")
    assert (rows[1].get("accessEvidence"), rows[1]["mediaStatus"]) == ("player", "needs-user")
