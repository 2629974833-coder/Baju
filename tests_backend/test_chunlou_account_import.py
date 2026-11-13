import base64
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import chunlou
from tests_backend.test_app import app_at

BASE = "http://127.0.0.1"
ROUTE = "/api/sites/chunlou/local-auth"
PROJECT = "grzjuztzhrexhvmetnvd"
ISSUER = f"https://{PROJECT}.supabase.co/auth/v1"
KEY = f"sb-{PROJECT}-all-auth-token"
LEGACY_KEY = f"sb-{PROJECT}-auth-token"
ORIGIN = {"Origin": BASE}


def payload(*, key=KEY, claims=None, fields=None):
    expires = int(time.time()) + 3600

    def encode(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    identity = {
        "iss": ISSUER,
        "sub": "fixture-user",
        "exp": expires,
        "role": "authenticated",
        "is_anonymous": False,
        **(claims or {}),
    }
    access = encode({"alg": "ES256"}) + "." + encode(identity) + ".signed"
    session = {
        "access_token": access,
        "refresh_token": "fixture-secret-refresh",
        "expires_at": expires,
        "token_type": "bearer",
        "provider_token": "unrelated-oauth-secret",
        "user": {"id": identity["sub"], "email": "private@example.test"},
        **(fields or {}),
    }
    return {"storageKey": key, "session": json.dumps(session)}


def api(app, host="127.0.0.1"):
    return TestClient(app, base_url=BASE, client=(host, 1245))


def test_import_is_bound_to_saved_chunlou_source_and_same_origin_panel(tmp_path):
    app = app_at(tmp_path)
    with api(app, "192.0.2.7") as c:
        assert c.get(ROUTE).json().get("canImportAccount") is False
        assert c.post(ROUTE + "/import", headers=ORIGIN, json=payload()).status_code == 403
    with api(app_at(tmp_path)) as c:
        app = c.app
        assert c.get(ROUTE).json()["canImportAccount"] is True
        assert c.get(ROUTE.replace("chunlou", "lustflix")).json()["canImportAccount"] is False
        for h in (
            {},
            {"Origin": "https://chunlou.tv"},
            {"Origin": "null"},
            {**ORIGIN, "X-Forwarded-For": "192.0.2.7"},
            {**ORIGIN, "Sec-Fetch-Site": "cross-site"},
        ):
            assert c.post(ROUTE + "/import", headers=h, json=payload()).status_code == 403
        assert (
            c.post(
                ROUTE.replace("chunlou", "lustflix") + "/import", headers=ORIGIN, json=payload()
            ).status_code
            == 400
        )
        app.state.store.mutate(
            lambda s: next(x for x in s["sites"] if x["id"] == "chunlou").update(
                url="http://chunlou.tv/"
            )
        )
        assert c.get(ROUTE).json()["canImportAccount"] is False
        assert c.post(ROUTE + "/import", headers=ORIGIN, json=payload()).status_code == 400


def test_import_validates_without_echoing_submitted_values(tmp_path):
    with api(app_at(tmp_path)) as c:
        for bad in (
            payload(key="sb-other-project-auth-token"),
            payload(key="sb-impersonate-auth"),
            payload(key="sb-other-project-all-auth-token"),
            payload(key=[]),
            payload(claims={"iss": f"https://{PROJECT}-all.supabase.co/auth/v1"}),
            payload(claims={"iss": "https://unrelated.supabase.co/auth/v1"}),
            payload(claims={"exp": int(time.time()) - 1}),
            payload(fields={"expires_at": int(time.time()) - 1}),
            payload(claims={"exp": float("inf")}),
            payload(fields={"refresh_token": ""}),
            payload(claims={"is_anonymous": True}),
            payload(fields={"user": {"id": "wrong-account"}}),
            payload(fields={"user": {"id": "fixture-user", "is_anonymous": True}}),
            payload(fields={"access_token": ".broken."}),
            {"unknown": "fixture-secret-refresh"},
            {"storageKey": KEY, "session": {"refresh_token": "fixture-secret-refresh"}},
            {"storageKey": KEY, "session": "[" * 1024},
        ):
            r = c.post(ROUTE + "/import", headers=ORIGIN, json=bad)
            assert r.status_code == 400
            assert "fixture-secret" not in r.text and "access_token" not in r.text
        for body, status in (
            (b"{{ private", 400),
            (b"[" * 1024 + b"]" * 1024, 400),
            (b"x" * 65537, 413),
        ):
            r = c.post(
                ROUTE + "/import",
                headers={**ORIGIN, "Content-Type": "application/json"},
                content=body,
            )
            assert r.status_code == status
        assert (
            c.post(ROUTE + "/import", headers=ORIGIN, data={"session": "private"}).status_code
            == 415
        )
        assert not any(s["sessionSaved"] for s in c.get("/api/state").json()["sites"])


@pytest.mark.parametrize("source_key", [KEY, LEGACY_KEY])
def test_import_minimizes_account_fields_and_preserves_failure_state(tmp_path, monkeypatch, source_key):
    app = app_at(tmp_path)
    official_calls = mock_identity(monkeypatch)
    saved = []

    async def save(site, state, *, keep_chunlou_age, check_idle):
        assert site["id"] == "chunlou" and keep_chunlou_age
        check_idle()
        saved.append(state)
        return {"saved": True, "open": False, "verified": False}

    monkeypatch.setattr(app.state.browser, "import_local_session", save, raising=False)
    with api(app) as c:
        r = c.post(ROUTE + "/import", headers=ORIGIN, json=payload(key=source_key))
        assert r.status_code == 200 and r.json() == {
            "saved": True,
            "open": False,
            "verified": False,
            "accountVerified": True,
        }
        assert official_calls == [
            "https://chunlou.tv/",
            "https://chunlou.tv/assets/index-fixture.js",
            "https://chunlou.tv/assets/config-fixture.js",
            ISSUER + "/user",
        ]
        (state,) = saved
        assert state["cookies"] == []
        (origin,) = state["origins"]
        assert origin["origin"] == "https://chunlou.tv"
        (item,) = origin["localStorage"]
        assert item["name"] == KEY
        parsed = json.loads(item["value"])
        assert parsed["user"]["id"] == "fixture-user" and "email" not in parsed["user"]
        assert (
            parsed["refresh_token"] == "fixture-secret-refresh" and "provider_token" not in parsed
        )
        workspace = c.get("/api/state")
        assert parsed["refresh_token"] not in workspace.text
        source = next(s for s in workspace.json()["sites"] if s["id"] == "chunlou")
        assert source["sessionSaved"] and not source["verified"]
        app.state.store.mutate(
            lambda s: s["scans"].append(
                {
                    "id": "reading",
                    "status": "running",
                    "siteId": "chunlou",
                    "url": "https://chunlou.tv",
                }
            )
        )
        assert c.post(ROUTE + "/import", headers=ORIGIN, json=payload()).status_code == 409
        assert len(saved) == 1


def test_open_native_auth_does_not_get_overwritten_by_handoff(tmp_path, monkeypatch):
    from types import SimpleNamespace

    app = app_at(tmp_path)
    calls = mock_identity(monkeypatch)
    with api(app) as c:
        app.state.local_auth._window = SimpleNamespace(
            site_id="chunlou", guard=SimpleNamespace(returncode=None)
        )
        try:
            r = c.post(ROUTE + "/import", headers=ORIGIN, json=payload())
            assert r.status_code == 409 and "专用授权窗口" in r.text
            assert c.get(ROUTE).json()["active"] and not calls
        finally:
            app.state.local_auth._window = None


def mock_identity(monkeypatch, *, code=200, user=None, configured_project=PROJECT, offline=False):
    calls = []

    def handle(request):
        assert request.method == "GET" and not request.content and not request.url.query
        calls.append(str(request.url))
        if request.url.host == "chunlou.tv":
            assert "authorization" not in request.headers and "cookie" not in request.headers
            body = {
                "/": '<script type="module" src="/assets/index-fixture.js"></script>',
                "/assets/index-fixture.js": 'import("./config-fixture.js")',
                "/assets/config-fixture.js": f'const cfg={{supabaseUrl:"https://{configured_project}.supabase.co",supabaseAnonKey:"sb_publishable_fixture123456"}}',
            }[request.url.path]
            return httpx.Response(200, text=body)
        assert str(request.url) == ISSUER + "/user" and "cookie" not in request.headers
        assert request.headers["apikey"] == "sb_publishable_fixture123456"
        assert request.headers["authorization"].startswith("Bearer ")
        assert "fixture-secret-refresh" not in str(request.headers)
        if offline:
            raise httpx.ConnectError("fixture")
        return httpx.Response(
            code,
            json=user or {"id": "fixture-user", "is_anonymous": False},
            headers={"location": "https://elsewhere.example/"} if 300 <= code < 400 else {},
        )

    monkeypatch.setattr(
        chunlou,
        "_client",
        lambda _policy: httpx.AsyncClient(
            transport=httpx.MockTransport(handle), follow_redirects=False
        ),
        raising=False,
    )
    return calls


@pytest.mark.parametrize(
    "case", ["other-project", "rejected", "wrong-user", "anonymous", "redirect", "offline"]
)
def test_failed_official_identity_check_never_persists(case, tmp_path, monkeypatch):
    calls = mock_identity(
        monkeypatch,
        configured_project="untrustedproject" if case == "other-project" else PROJECT,
        code={"rejected": 401, "redirect": 302}.get(case, 200),
        offline=case == "offline",
        user={
            "id": "another-user" if case == "wrong-user" else "fixture-user",
            "is_anonymous": case == "anonymous",
        },
    )
    with api(app_at(tmp_path)) as c:
        r = c.post(ROUTE + "/import", headers=ORIGIN, json=payload())
        assert r.status_code == 400 and "fixture-secret" not in r.text
        assert not any(s["sessionSaved"] for s in c.get("/api/state").json()["sites"])
    assert len(calls) == (3 if case == "other-project" else 4)


@pytest.mark.asyncio
async def test_account_survives_restart_and_only_saved_age_record_is_merged(tmp_path, monkeypatch):
    from backend.browser import BrowserService
    from backend.local_auth import LocalAuthService
    from backend.network import NetworkPolicy

    mock_identity(monkeypatch)
    from tests_backend.test_app import Policy

    site = {"id": "fixture", "url": "https://chunlou.tv/"}
    policy = NetworkPolicy()
    browser = BrowserService(tmp_path, policy)
    service = LocalAuthService(Policy())
    prior = {
        "cookies": [],
        "origins": [
            {
                "origin": "https://chunlou.tv",
                "localStorage": [
                    {"name": "age-verified-at", "value": "1700000000000"},
                    {"name": "sb-old-auth-token", "value": "stale"},
                ],
            }
        ],
    }
    try:
        await browser.import_local_session(site, prior)
        path = browser._state_path(site["id"])
        previous = path.read_bytes()

        def busy():
            raise ValueError("fixture busy")

        async def failed(site, state):
            return await browser.import_local_session(
                site, state, keep_chunlou_age=True, check_idle=busy
            )

        with pytest.raises(ValueError, match="fixture busy"):
            await service.import_account(site, payload(), failed)
        assert path.read_bytes() == previous

        async def install(site, state):
            return await browser.import_local_session(site, state, keep_chunlou_age=True)

        result = await service.import_account(site, payload(), install)
        assert result["accountVerified"] and result["saved"]
        assert b"fixture-secret-refresh" not in path.read_bytes()
    finally:
        await browser.close()
    restarted = BrowserService(tmp_path, policy)
    try:
        context = await restarted.create_scan_context(site)
        try:
            page = await context.new_page()
            await page.route(
                "https://chunlou.tv/fixture",
                lambda route: route.fulfill(
                    content_type="text/html", body="<title>Local fixture</title>"
                ),
            )
            await page.goto("https://chunlou.tv/fixture")
            local = await page.evaluate("() => ({...localStorage})")
            assert set(local) == {KEY, "age-verified-at"}
            assert local["age-verified-at"] == "1700000000000"
            assert json.loads(local[KEY])["user"]["id"] == "fixture-user"
            assert await context.cookies() == []
        finally:
            await context.close()
    finally:
        await restarted.close()
        await service.close()


async def test_scanner_restores_existing_legacy_state_only_for_its_original_chunlou_account(tmp_path):
    from backend.browser import BrowserService
    from tests_backend.test_app import Policy

    service = BrowserService(tmp_path, Policy())
    site = {"id": "saved-before-upgrade", "url": "https://www.chunlou.tv/"}
    try:
        for current in ([], [{"name": KEY, "value": "current"}]):
            state = {"cookies": [], "origins": [
                {"origin": origin, "localStorage": [{"name": LEGACY_KEY, "value": "legacy"}, *current]}
                for origin in ("https://chunlou.tv", "https://example.org")
            ]}
            await service.import_local_session(site, state)
            path = service._state_path(site["id"])
            previous = path.read_bytes()
            context = await service.create_scan_context(site)
            try:
                restored = {
                    origin["origin"]: {item["name"]: item["value"] for item in origin["localStorage"]}
                    for origin in (await context.storage_state())["origins"]
                }
                assert restored["https://chunlou.tv"].get(KEY) == ("current" if current else "legacy")
                assert restored["https://example.org"].get(KEY) == ("current" if current else None)
                assert path.read_bytes() == previous
            finally:
                await context.close()
    finally:
        await service.close()


def test_browser_storage_failure_hides_session_details(tmp_path, monkeypatch):
    app = app_at(tmp_path)
    mock_identity(monkeypatch)

    async def reject(_site, state, **_options):
        raise RuntimeError(repr(state))

    monkeypatch.setattr(app.state.browser, "import_local_session", reject, raising=False)
    with api(app) as c:
        response = c.post(ROUTE + "/import", headers=ORIGIN, json=payload())
        assert response.status_code == 502 and "fixture-secret-refresh" not in response.text
        assert not any(site["sessionSaved"] for site in c.get("/api/state").json()["sites"])
