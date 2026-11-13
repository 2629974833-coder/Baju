from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from tests_backend.test_app import app_at
from tests_backend.test_series_api import saved_series

PATH = "/api/series/scan-notices/dismiss"


def seed(app, tmp_path):
    saved_series(app, tmp_path)
    old = {
        "id": "old",
        "mode": "series",
        "siteId": "example",
        "url": "https://example.com/one",
        "status": "needs-user",
    }

    def add(state):
        state["scans"].extend(
            [
                old,
                {**old, "id": "new", "status": "failed"},
                {**old, "id": "empty", "status": "completed"},
                {**old, "id": "active", "status": "running"},
                {**old, "id": "page", "mode": "page"},
            ]
        )

    return add


def test_dismissing_only_clears_selected_notices_across_restart(tmp_path):
    app = app_at(tmp_path)
    add = seed(app, tmp_path)
    with TestClient(app) as client:
        app.state.store.mutate(add)
        before = deepcopy(app.state.store.read())
        response = client.post(PATH, json={"scanIds": ["old", "old", "empty"]})
        assert response.status_code == 200 and response.json() == {"dismissed": 2}
        assert client.post(PATH, json={"scanIds": ["old", "empty"]}).json() == {"dismissed": 0}
        after = app.state.store.read()
        assert [s["id"] for s in after["scans"] if s.pop("seriesNoticeDismissed", False)] == [
            "old",
            "empty",
        ]
        assert before == after
    with TestClient(app_at(tmp_path)) as client:
        j = client.get("/api/state").json()
        assert [s["id"] for s in j["scans"] if s.get("seriesNoticeDismissed")] == ["old", "empty"]


@pytest.mark.parametrize(
    "bad,status", [("active", 409), ("missing", 404), ("page", 400), ("s1", 400)]
)
def test_invalid_batch_cannot_clear_a_valid_notice(tmp_path, bad, status):
    app = app_at(tmp_path)
    add = seed(app, tmp_path)
    with TestClient(app) as client:
        app.state.store.mutate(add)
        assert client.post(PATH, json={"scanIds": ["old", bad]}).status_code == status
        assert not any(s.get("seriesNoticeDismissed") for s in app.state.store.read()["scans"])


def test_timeout_and_protected_media_accept_bulk_clear(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        statuses = ["timed-out", "unsupported-drm"]
        app.state.store.mutate(
            lambda state: state["scans"].extend(
                {"id": status, "mode": "series", "status": status} for status in statuses
            )
        )
        assert client.post(PATH, json={"scanIds": statuses}).json() == {"dismissed": 2}
