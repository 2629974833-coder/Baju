from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests_backend.test_app import app_at, until

RULE = {"name": " 每日检查 ", "siteId": "lustflix", "url": "https://lustflix.tv/updates"}


def test_rules_crud_persist_schedule_and_manual_scan(tmp_path):
    with TestClient(app_at(tmp_path)) as client:
        assert client.get("/api/capabilities").json()["automation"] is True
        assert client.get("/api/ready").json()["workers"]["automation"]["healthy"]
        created = client.post("/api/rules", json=RULE)
        assert created.status_code == 200
        rule = created.json()
        assert rule["name"] == "每日检查" and rule["autoDownload"] is False and rule["enabled"] is True
        assert datetime.fromisoformat(rule["nextRunAt"]) > datetime.now(UTC) + timedelta(minutes=14)
        url = "/api/rules/" + rule["id"]
        assert client.post(url + "/run").is_success
        state = until(client, lambda s: s["rules"][0]["status"] == "completed")
        assert len(state["assets"]) == 1 and not state["jobs"]
        assert state["rules"][0]["lastNewCount"] == 1
        assert client.patch(url, json={"enabled": False}).json()["nextRunAt"] is None
        assert client.post(url + "/run").status_code == 409
    with TestClient(app_at(tmp_path)) as client:
        assert client.get("/api/state").json()["rules"][0]["enabled"] is False
        changed = client.patch(url, json={"enabled": True, "intervalMinutes": 15})
        assert changed.is_success and changed.json()["nextRunAt"] and changed.json()["status"] == "idle"
        assert client.delete(url).is_success
        state = client.get("/api/state").json()
        assert not state["rules"] and len(state["assets"]) == len(state["scans"]) == 1
        assert client.post(url + "/run").status_code == 404


@pytest.mark.parametrize("changes,code", [
    ({"siteId": "absent"}, 404), ({"url": "https://example.com/"}, 400),
    ({"url": "https://lustflix.tv:8080/"}, 400), ({"url": "file:///tmp/a"}, 400),
    ({"name": "  "}, 422), ({"kinds": []}, 422), ({"intervalMinutes": 14}, 422),
    ({"intervalMinutes": 8 * 24 * 60}, 422), ({"autoDownload": "yes"}, 422),
    ({"status": "running"}, 422),
])
def test_rule_rejects_invalid_or_out_of_scope_target(tmp_path, changes, code):
    with TestClient(app_at(tmp_path)) as client:
        assert client.post("/api/rules", json=RULE | changes).status_code == code
        assert not client.get("/api/state").json()["rules"]


def test_rule_changes_are_atomic_and_private_bookkeeping_is_hidden(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        rule = client.post("/api/rules", json=RULE).json()
        url = "/api/rules/" + rule["id"]
        app.state.store.mutate(lambda s: s["rules"][0].update(_internal=["private"]))
        for body in ({}, {"siteId": None}, {"name": "x", "kinds": []}):
            assert client.patch(url, json=body).status_code == 422
        assert client.patch(url, json={"siteId": "chunlou"}).status_code == 400
        expected = {"siteId": rule["siteId"], "url": rule["url"]}
        new_url = "https://lustflix.tv/new"
        assert client.patch(url, json={"url": new_url, "expectedTarget": expected}).is_success
        assert client.patch(url, json={"url": RULE["url"], "expectedTarget": expected}).status_code == 409
        assert client.patch(url, json={"expectedTarget": expected}).status_code == 422
        updated = client.patch(url, json={"name": "改名"}).json()
        assert updated["name"] == "改名" and "_internal" not in updated and updated["url"] == new_url
        app.state.store.mutate(lambda s: s["rules"][0].update(status="running"))
        assert client.patch(url, json={"enabled": False}).status_code == 409
        assert client.delete(url).status_code == 409


def test_reopen_old_workspace_without_rules_list(tmp_path):
    app = app_at(tmp_path)
    app.state.store.mutate(lambda state: state.pop("rules"))
    with TestClient(app_at(tmp_path)) as client:
        assert client.get("/api/state").json()["rules"] == []
        assert client.post("/api/rules", json=RULE).is_success
