from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from tests_backend.test_app import app_at
from tests_backend.test_series_api import saved_series


def test_dismiss_scan_catalog_preserves_other_scan_series_archive_and_history(tmp_path):
    app = app_at(tmp_path)
    contents, path = saved_series(app, tmp_path)
    app.state.store.mutate(lambda state: state["scans"].append(
        {**deepcopy(state["scans"][0]), "id": "duplicate"}
    ))
    endpoint = "/api/scans/s1/discovery-dismiss"
    with TestClient(app) as client:
        before = client.get("/api/state").json()
        assert len(before["series"]) == 1
        keys = {"scope": "episodes", "episodeKeys": ["C04:1", "C04:1"]}
        response = client.post(endpoint, json=keys)
        assert response.status_code == 200
        assert response.json() == {
            "dismissed": 1, "discoveryCatalogDismissed": False,
            "discoveryDismissedEpisodeKeys": ["C04:1"],
        }
        assert client.post(endpoint, json=keys).json()["dismissed"] == 0

    with TestClient(app_at(tmp_path)) as client:
        partial = client.get("/api/state").json()
        assert partial["scans"][0]["discoveryDismissedEpisodeKeys"] == ["C04:1"]
        assert not partial["scans"][0].get("discoveryCatalogDismissed")
        assert partial["scans"][1] == before["scans"][1]
        last = client.post(endpoint, json={"scope": "episodes", "episodeKeys": ["C04:2"]}).json()
        assert last["dismissed"] == 1 and last["discoveryCatalogDismissed"]
        assert client.post(endpoint, json={"scope": "catalog"}).json()["dismissed"] == 0
        other = client.post("/api/scans/duplicate/discovery-dismiss", json={"scope": "catalog"}).json()
        assert other == {
            "dismissed": 1, "discoveryCatalogDismissed": True, "discoveryDismissedEpisodeKeys": [],
        }
        assert client.post("/api/scans/duplicate/discovery-dismiss", json=keys).json()["dismissed"] == 0
        after = client.get("/api/state").json()
        assert after["series"] == before["series"]
        assert after["assets"] == before["assets"] and after["jobs"] == before["jobs"]
        assert all(scan["discoveryCatalogDismissed"] for scan in after["scans"])
        assert [s["catalog"] for s in after["scans"]] == [s["catalog"] for s in before["scans"]]
        assert client.get("/api/scans/s1/archive").status_code == 200
    assert path.read_bytes() == contents


@pytest.mark.parametrize(("identifier", "body", "code"), [
    ("s1", {}, 422),
    ("s1", {"scope": "episodes"}, 422),
    ("s1", {"scope": "episodes", "episodeKeys": []}, 422),
    ("s1", {"scope": "episodes", "episodeKeys": [""]}, 422),
    ("s1", {"scope": "catalog", "episodeKeys": None}, 422),
    ("s1", {"scope": "catalog", "episodeKeys": ["C04:1"]}, 422),
    ("s1", {"scope": "episodes", "episodeKeys": ["C04:1", "foreign"]}, 400),
    ("empty", {"scope": "catalog"}, 400),
    ("missing", {"scope": "catalog"}, 404),
])
def test_invalid_discovery_scope_is_atomic(tmp_path, identifier, body, code):
    app = app_at(tmp_path)
    saved_series(app, tmp_path)
    app.state.store.mutate(lambda state: state["scans"].append({"id": "empty", "status": "completed"}))
    with TestClient(app) as client:
        before = app.state.store.read()
        assert client.post(f"/api/scans/{identifier}/discovery-dismiss", json=body).status_code == code
        assert app.state.store.read() == before


GROUP_PATH = "/api/scans/discovery-dismiss-group"


def test_removed_series_keeps_only_valid_scans_groupable_in_discovery(tmp_path):
    app = app_at(tmp_path)
    saved_series(app, tmp_path)

    def seed(state):
        first = state["scans"][0]
        repeat = {**deepcopy(first), "id": "repeat"}
        invalid = {**deepcopy(first), "id": "invalid"}
        invalid["catalog"]["entries"][0]["pageUrl"] = "https://elsewhere.invalid/1"
        empty = {**deepcopy(first), "id": "empty"}
        empty["catalog"]["entries"] = []
        state["scans"].extend([repeat, invalid, empty])

    app.state.store.mutate(seed)
    with TestClient(app) as client:
        identifier = client.get("/api/state").json()["series"][0]["id"]
        assert client.post("/api/series/dismiss", json={"ids": [identifier]}).status_code == 200
        snapshot = client.get("/api/state").json()
        assert not snapshot["series"]
        by_scan = {s["id"]: s.get("discoverySeriesId") for s in snapshot["scans"]}
        assert by_scan == {"s1": identifier, "repeat": identifier, "invalid": None, "empty": None}
        assert client.post(GROUP_PATH, json={"scanIds": ["s1", "repeat"]}).json() == {"dismissed": 2}


def test_group_dismiss_freezes_confirmed_scans_and_survives_restart(tmp_path):
    app = app_at(tmp_path)
    contents, file = saved_series(app, tmp_path)
    def append_scans(state, ids):
        for identifier in ids:
            state["scans"].append({**deepcopy(state["scans"][0]), "id": identifier})

    with TestClient(app) as client:
        app.state.store.mutate(lambda state: append_scans(state, ["repeat", "unselected"]))
        body = {"scanIds": ["s1", "repeat", "repeat"]}
        # A further observation arrives after the group was displayed for confirmation.
        app.state.store.mutate(lambda state: append_scans(state, ["new"]))
        before = client.get("/api/state").json()
        response = client.post(GROUP_PATH, json=body)
        assert response.status_code == 200 and response.json() == {"dismissed": 2}
    with TestClient(app_at(tmp_path)) as client:
        assert client.post(GROUP_PATH, json=body).json() == {"dismissed": 0}
        after = client.get("/api/state").json()
        assert [s["id"] for s in after["scans"] if s.get("discoveryCatalogDismissed")] == ["s1", "repeat"]
        assert after["series"] == before["series"]
        assert after["assets"] == before["assets"] and after["jobs"] == before["jobs"]
        assert [s["catalog"] for s in after["scans"]] == [s["catalog"] for s in before["scans"]]
        assert file.read_bytes() == contents


@pytest.mark.parametrize(("target","code"), [
    ("missing", 404), ("same-title-other-site", 400), ("invalid-origin", 400), ("empty-directory", 400),
])
def test_group_dismiss_rejects_unrelated_scans_without_partial_effects(tmp_path, target, code):
    app = app_at(tmp_path)
    saved_series(app, tmp_path)
    def seed(state):
        first = state["scans"][0]
        first["catalog"]["title"] = "同名剧集"
        for name in ("same-title-other-site", "invalid-origin", "empty-directory"):
            other = {**deepcopy(first), "id": name}
            if name == "same-title-other-site":
                other["siteId"] = "other"
            elif name == "invalid-origin":
                other["catalog"]["entries"][0]["pageUrl"] = "https://unrelated.invalid/ep/1"
            else:
                other["catalog"]["entries"] = []
            state["scans"].append(other)
    app.state.store.mutate(seed)
    with TestClient(app) as client:
        before = app.state.store.read()
        assert client.post(GROUP_PATH, json={"scanIds": ["s1", target]}).status_code == code
        assert app.state.store.read() == before


@pytest.mark.parametrize("body", [{}, {"scanIds": []}, {"scanIds": [""]}, {"scanIds": ["x"] * 10001}])
def test_group_dismiss_requires_bounded_explicit_scan_ids(tmp_path, body):
    with TestClient(app_at(tmp_path)) as client:
        assert client.post(GROUP_PATH, json=body).status_code == 422


@pytest.mark.parametrize("status", ["queued", "running"])
def test_group_dismiss_rejects_an_active_member_atomically(tmp_path, status):
    app = app_at(tmp_path)
    saved_series(app, tmp_path)
    with TestClient(app) as client:
        app.state.store.mutate(lambda state: state["scans"].append(
            {**deepcopy(state["scans"][0]), "id": "active", "status": status}
        ))
        before = app.state.store.read()
        response = client.post(GROUP_PATH, json={"scanIds": ["s1", "active"]})
        assert response.status_code == 409
        assert app.state.store.read() == before
