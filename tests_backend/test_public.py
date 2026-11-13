"""Anonymous workspaces must remain separate through files, restarts and cleanup."""

import importlib
import importlib.util
import json
import os
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.operations import StoragePressure
from tests_backend.test_app import Browser, Downloader, Policy, Scanner, until


def public_module():
    assert importlib.util.find_spec("backend.public") is not None, (
        "Public workspace isolation is missing"
    )
    return importlib.import_module("backend.public")


def setup(tmp_path, **options):
    module = public_module()
    from backend.app import create_app

    clock = [100000.0]
    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    (static / "index.html").write_text("public panel")
    settings = Settings(data_dir=tmp_path / "public", static_dir=static, min_free_bytes=0)

    def factory(tenant, *, resource_limits=None):
        return create_app(
            tenant,
            browser=Browser(),
            scanner=Scanner(),
            downloader=Downloader(tenant.data_dir),
            policy=Policy(),
            resource_limits=resource_limits,
        )

    config = module.PublicOptions(**options)
    app = module.create_public_app(
        settings, options=config, app_factory=factory, clock=lambda: clock[0]
    )
    return app, settings, clock, factory


def visit(client, app, token=None):
    client.cookies.clear()
    if token:
        client.cookies.set(app.state.manager.cookie_name, token)
    response = client.get("/api/state", headers={"Sec-Fetch-Site": "same-origin"})
    cookie = response.cookies.get(app.state.manager.cookie_name)
    return response, cookie


def test_static_health_and_capabilities_do_not_create_workspace(tmp_path):
    app, settings, _, _ = setup(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        for path in ("/", "/api/health", "/api/ready", "/api/public-info", "/api/capabilities"):
            response = client.get(path)
            assert response.status_code in {200, 503}
            assert "set-cookie" not in response.headers
        info = client.get("/api/capabilities").json()
        assert info["publicMode"] and info["retentionHours"] == 24
        assert not info["processing"] and not info["automation"]
        assert not list((settings.data_dir / "workspaces").iterdir())


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path", ["/", "/index.html", "/app.js", "/module.mjs", "/styles.css", "/guide/"])
def test_public_panel_code_is_never_cached_even_on_conditional_requests(tmp_path, monkeypatch, method, path):
    app, settings, _, _ = setup(tmp_path)
    for name in ("app.js", "module.mjs", "styles.css", "guide/index.html"):
        file = settings.static_dir / name
        file.parent.mkdir(exist_ok=True)
        file.write_text("panel code")
    static = public_module().StaticFiles
    get_response = static.get_response

    async def cacheable_response(self, *args, **kwargs):
        response = await get_response(self, *args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=14400"
        return response

    monkeypatch.setattr(static, "get_response", cacheable_response)
    with TestClient(app, base_url="https://testserver") as client:
        response = client.request(method, path)
        assert response.status_code == 200
        conditional = client.request(method, path, headers={"If-None-Match": response.headers["etag"]})
        assert conditional.status_code == 304
        for result in (response, conditional):
            assert result.headers["cache-control"] == "no-store"
            assert "set-cookie" not in result.headers
        assert not app.state.manager.active
        assert not list((settings.data_dir / "workspaces").iterdir())


def test_public_static_binary_assets_keep_their_cache_policy_and_api_remains_private(tmp_path, monkeypatch):
    app, settings, _, _ = setup(tmp_path)
    for name in ("cover.png", "icon.svg", "font.woff2"):
        (settings.static_dir / name).write_bytes(b"binary asset")
    static = public_module().StaticFiles
    get_response = static.get_response

    async def cacheable_response(self, *args, **kwargs):
        response = await get_response(self, *args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=14400"
        return response

    monkeypatch.setattr(static, "get_response", cacheable_response)
    with TestClient(app, base_url="https://testserver") as client:
        for path in ("/cover.png", "/icon.svg", "/font.woff2"):
            response = client.get(path)
            conditional = client.head(path, headers={"If-None-Match": response.headers["etag"]})
            assert response.status_code == 200 and conditional.status_code == 304
            assert response.headers["cache-control"] == "public, max-age=14400"
            assert conditional.headers["cache-control"] == "public, max-age=14400"
            assert "set-cookie" not in response.headers
        assert client.get("/api/health").headers["cache-control"] == "no-store"
        assert client.post("/api/downloads", json={"ids": []}).status_code == 403
        assert not list((settings.data_dir / "workspaces").iterdir())


def test_distinct_visitors_cannot_read_each_others_files_or_choose_workspace(tmp_path):
    app, _, _, _ = setup(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        first, token_a = visit(client, app)
        assert first.status_code == 200
        assert first.json()["publicMode"] and first.json()["settings"]["concurrency"] == 1
        assert first.json()["coinUnlockSiteIds"] == []
        assert "HttpOnly" in first.headers["set-cookie"]
        assert "Secure" in first.headers["set-cookie"]
        assert "SameSite=lax" in first.headers["set-cookie"]
        scan = client.post(
            "/api/scans",
            headers={"Origin": "https://testserver"},
            json={"siteId": "lustflix", "url": "https://lustflix.tv/"},
        )
        assert scan.status_code == 200, scan.text
        state = until(client, lambda s: bool(s["assets"]))
        asset = state["assets"][0]
        assert (
            client.post(
                "/api/downloads",
                headers={"Origin": "https://testserver"},
                json={"ids": [asset["id"]]},
            ).status_code
            == 200
        )
        until(client, lambda s: s["assets"][0]["status"] == "downloaded")
        assert client.get(f"/api/assets/{asset['id']}/file").status_code == 200
        second, token_b = visit(client, app)
        assert token_a != token_b and second.json()["assets"] == []
        assert client.get(f"/api/assets/{asset['id']}/file").status_code == 404
        request = client.get("/api/state?workspace=" + token_a, headers={"X-Workspace-ID": token_a})
        assert request.json()["assets"] == []
        restored, _ = visit(client, app, token_a)
        assert restored.json()["assets"][0]["id"] == asset["id"]


def test_unsigned_or_tampered_cookie_cannot_adopt_an_existing_workspace(tmp_path):
    app, _, _, _ = setup(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        _, token = visit(client, app)
        invalid = token[:-1] + ("0" if token[-1] != "0" else "1")
        response, new = visit(client, app, invalid)
        assert response.status_code == 200 and new != token
        assert len(app.state.manager.active) == 2


def test_non_ascii_cookie_signature_is_rejected_without_server_error(tmp_path):
    app, _, _, _ = setup(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        _, token = visit(client, app)
        # Quoted octal escapes are decoded by the HTTP cookie parser to non-ASCII.
        malformed = token.split(".", 1)[0] + "." + r"\351" * 64
        client.cookies.clear()
        response = client.get(
            "/api/state",
            headers=[
                (
                    "cookie",
                    f'{app.state.manager.cookie_name}="{malformed}"',
                ),
            ],
        )
        assert response.status_code == 200
        assert response.cookies.get(app.state.manager.cookie_name) != token
        assert len(app.state.manager.active) == 2


def test_cookie_and_workspace_survive_process_restart(tmp_path):
    app, settings, clock, factory = setup(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        _, token = visit(client, app)
        tenant = next(iter(app.state.manager.active.values()))
        tenant.app.state.store.mutate(lambda state: state["activity"].append({"title": "kept"}))
        secret = settings.data_dir / ".public-secret"
        before = secret.read_bytes()
        assert os.stat(secret).st_mode & 0o777 == 0o600
    module = public_module()
    reopened = module.create_public_app(settings, app_factory=factory, clock=lambda: clock[0])
    with TestClient(reopened, base_url="https://testserver") as client:
        state, renewed = visit(client, reopened, token)
        assert state.json()["activity"][-1]["title"] == "kept"
        assert renewed == token and secret.read_bytes() == before


def test_cross_site_cannot_allocate_or_write_and_public_admin_routes_are_blocked(tmp_path):
    app, settings, _, _ = setup(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/state", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert (
            client.get("/api/state", headers={"Origin": "https://evil.example"}).status_code == 403
        )
        assert not list((settings.data_dir / "workspaces").iterdir())
        assert client.post("/api/downloads", json={"ids": []}).status_code == 403
        assert client.get("/api/state").status_code == 200  # Fetch may omit Origin.
        for path in (
            "/api/maintenance/backups",
            "/api/rules",
            "/api/processes",
            "/api/operations",
            "/api/settings",
            "/api/sites/lustflix/local-auth",
        ):
            assert (
                client.post(path, headers={"Origin": "https://testserver"}, json={}).status_code
                == 403
            )
        assert (
            client.post(
                "/api/series/any/scan",
                headers={"Origin": "https://testserver"},
                json={"autoUnlock": {"maxCoins": 10}},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/series/any/scan",
                headers={"Origin": "https://testserver"},
                json={"autoUnlock": {}},
            ).status_code
            == 403
        )
        for value in ([], None, 3):
            response = client.post(
                "/api/series/any/scan",
                headers={"Origin": "https://testserver"},
                content=json.dumps(value),
            )
            assert response.status_code == 422


def test_busy_workspace_is_not_evicted_and_capacity_returns_429(tmp_path):
    app, _, clock, _ = setup(tmp_path, max_active_workspaces=1, max_workspaces=2)
    with TestClient(app, base_url="https://testserver") as client:
        _, token = visit(client, app)
        tenant = next(iter(app.state.manager.active.values()))
        tenant.app.state.store.mutate(
            lambda state: state["scans"].append({"id": "busy", "status": "running"})
        )
        assert visit(client, app)[0].status_code == 429
        tenant.app.state.store.mutate(lambda state: state["scans"].clear())
        assert visit(client, app)[0].status_code == 200
        assert len(app.state.manager.active) == 1
        # The old browser can return after in-memory eviction; its disk state is retained.
        assert visit(client, app, token)[0].status_code == 200


def test_retention_cleanup_protects_busy_requests_and_jobs(tmp_path):
    app, settings, clock, _ = setup(tmp_path, retention_seconds=100, idle_unload_seconds=10)
    with TestClient(app, base_url="https://testserver") as client:
        visit(client, app)
        tenant = next(iter(app.state.manager.active.values()))
        tenant.requests = 1
        clock[0] += 101
        client.portal.call(app.state.manager.cleanup)
        assert tenant.path.exists()
        tenant.requests = 0
        tenant.app.state.store.mutate(
            lambda state: state["scans"].append({"id": "busy", "status": "running"})
        )
        client.portal.call(app.state.manager.cleanup)
        assert tenant.path.exists()
        tenant.app.state.store.mutate(lambda state: state["scans"].clear())
        client.portal.call(app.state.manager.cleanup)
        assert not tenant.path.exists() and not app.state.manager.active
        assert not list((settings.data_dir / "workspaces").iterdir())


def test_storage_guard_checks_incremental_quota_and_keeps_original_disk_guard(tmp_path):
    app, _, _, _ = setup(tmp_path, max_workspace_bytes=500000, max_total_bytes=1000000)
    with TestClient(app, base_url="https://testserver") as client:
        visit(client, app)
        tenant = next(iter(app.state.manager.active.values()))
        guard = tenant.app.state.storage
        assert tenant.app.state.tasks.storage_guard is guard
        assert tenant.app.state.tasks.downloader.storage_guard is guard
        (tenant.path / "quota-fixture").write_bytes(b"x" * 500000)
        try:
            guard.check()
        except StoragePressure:
            pass
        else:
            raise AssertionError("Growing media must hit the per-visitor quota")
        assert (
            client.post(
                "/api/downloads", headers={"Origin": "https://testserver"}, json={"ids": []}
            ).status_code
            == 503
        )
        assert client.get("/api/state").status_code == 200  # Still let the owner inspect results.
        assert (
            client.post(
                "/api/jobs/pause", headers={"Origin": "https://testserver"}, json={}
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/assets/dismiss",
                headers={"Origin": "https://testserver"},
                json={"ids": ["does-not-exist"]},
            ).status_code
            != 503
        )


def test_cleanup_ignores_stale_queue_records_but_respects_another_process_lock(tmp_path):
    from backend.operations import WorkspaceLock
    from backend.store import Store

    app, settings, clock, _ = setup(tmp_path, retention_seconds=100)
    with TestClient(app, base_url="https://testserver") as client:
        path = settings.data_dir / "workspaces" / ("a" * 32)
        store = Store(path / "workspace.sqlite3")
        store.mutate(lambda state: state["scans"].append({"id": "old", "status": "running"}))
        (path / ".public.json").write_text('{"lastSeen":0}')
        with WorkspaceLock(path):
            client.portal.call(app.state.manager.cleanup)
            assert path.exists()
        client.portal.call(app.state.manager.cleanup)
        assert not path.exists()


def test_public_root_has_a_single_owner(tmp_path):
    from backend.operations import WorkspaceBusy, WorkspaceLock

    app, settings, _, _ = setup(tmp_path)
    with TestClient(app, base_url="https://testserver"):
        try:
            WorkspaceLock(settings.data_dir).acquire()
        except WorkspaceBusy:
            pass
        else:
            raise AssertionError("Another process can clean an active public workspace")
    with WorkspaceLock(settings.data_dir):
        pass


def test_public_env_options_are_bounded_and_local_settings_unchanged(tmp_path, monkeypatch):
    module = public_module()
    monkeypatch.setenv("FRAMEKEEP_PUBLIC_MAX_ACTIVE_WORKSPACES", "2")
    monkeypatch.setenv("FRAMEKEEP_PUBLIC_RETENTION_HOURS", "12")
    options = module.PublicOptions.from_env()
    assert options.max_active_workspaces == 2 and options.retention_seconds == 12 * 3600
    assert options.secure_cookie is True
    try:
        replace(options, max_active_workspaces=0)
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid active workspace limit accepted")


def test_new_workspace_rate_limit_does_not_block_returning_owner(tmp_path):
    app, _, clock, _ = setup(
        tmp_path, max_active_workspaces=1, new_workspaces_per_ip=2, allocation_window_seconds=10
    )
    with TestClient(app, base_url="https://testserver") as client:
        _, first = visit(client, app)
        assert visit(client, app)[0].status_code == 200
        assert visit(client, app)[0].status_code == 429
        assert visit(client, app, first)[0].status_code == 200
        clock[0] += 11
        assert visit(client, app)[0].status_code == 200


def test_all_tenants_and_browser_requests_use_the_same_resource_limit(tmp_path):
    from contextlib import asynccontextmanager

    app, _, _, _ = setup(tmp_path)
    manager = app.state.manager
    original = manager.resource_limits
    calls = []

    class RecordingLimits:
        @asynccontextmanager
        async def slot(self, kind, *, check=None):
            calls.append(kind)
            async with original.slot(kind, check=check):
                yield

    manager.resource_limits = RecordingLimits()
    with TestClient(app, base_url="https://testserver") as client:
        visit(client, app)
        assert (
            client.post(
                "/api/sites/lustflix/session", headers={"Origin": "https://testserver"}, json={}
            ).status_code
            == 200
        )
        assert calls == ["scan"]
        assert client.get("/api/assets/unknown/thumbnail").status_code == 404
        assert calls == ["scan", "scan"]
        visit(client, app)
        for tenant in manager.active.values():
            assert tenant.app.state.tasks.resource_limits is manager.resource_limits


def test_workspace_storage_limit_and_directory_expiry_do_not_leak_files(tmp_path):
    app, settings, clock, _ = setup(
        tmp_path, max_active_workspaces=1, max_workspaces=1, retention_seconds=20
    )
    with TestClient(app, base_url="https://testserver") as client:
        _, old = visit(client, app)
        assert visit(client, app)[0].status_code == 429
        clock[0] += 21
        response, current = visit(client, app, old)
        assert response.status_code == 200 and current != old
        assert len(list((settings.data_dir / "workspaces").iterdir())) == 1


def test_generic_and_chunked_body_limits_apply_before_workspace_allocation(tmp_path, monkeypatch):
    app, settings, _, _ = setup(tmp_path)
    module = public_module()
    monkeypatch.setattr(module, "MAX_API_BODY_BYTES", 1024, raising=False)
    with TestClient(app, base_url="https://testserver") as client:
        for body in (b"x" * 1400, iter([b"x" * 700, b"x" * 700])):
            response = client.post(
                "/api/sites", headers={"Origin": "https://testserver"}, content=body
            )
            assert response.status_code == 413
        assert not list((settings.data_dir / "workspaces").iterdir())


def test_public_browser_gets_shared_manual_and_worker_session_bound(tmp_path):
    app, _, _, _ = setup(tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        visit(client, app)
        tenant = next(iter(app.state.manager.active.values()))
        assert tenant.app.state.browser.max_open_sessions == 2
