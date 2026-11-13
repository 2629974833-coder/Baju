import pytest

from backend.local_auth import LocalAuthService, site_state
from backend.network import NetworkPolicy

SITE = {"id": "source", "url": "https://chunlou.tv/"}


def test_state_import_is_limited_to_exact_site():
    raw = {
        "cookies": [
            {"name": name, "domain": domain}
            for name, domain in [
                ("accepted", ".chunlou.tv"),
                ("other", "unrelated.example"),
                ("neighbor", "cdn.chunlou.tv"),
                ("parent", ".tv"),
            ]
        ],
        "origins": [
            {
                "origin": "https://chunlou.tv",
                "localStorage": [{"name": "key", "value": "value"}],
                "indexedDB": [{"name": "auth"}],
            },
            {"origin": "https://other.example", "localStorage": []},
            {"origin": "http://chunlou.tv", "localStorage": []},
        ],
    }
    state = site_state(raw, SITE["url"])
    assert [cookie["name"] for cookie in state["cookies"]] == ["accepted"]
    assert state["origins"] == [raw["origins"][0]]
    assert raw["origins"][0]["indexedDB"] == state["origins"][0]["indexedDB"]


async def test_local_window_reports_unavailable_and_never_opens_unrelated_site(tmp_path):
    service = LocalAuthService(NetworkPolicy(), executable=tmp_path / "missing")
    assert not service.info(SITE["id"])["available"]
    with pytest.raises(ValueError):
        await service.open(SITE)
    with pytest.raises(ValueError):
        await service.save(SITE, None)
    await service.close()


def _session(
    expires,
    *,
    anonymous=False,
    claim_id="registered",
    user_id="registered",
    refresh="fixture-refresh",
    key="sb-grzjuztzhrexhvmetnvd-all-auth-token",
    issuer="https://grzjuztzhrexhvmetnvd.supabase.co/auth/v1",
):
    import base64
    import json

    claims = {
        "sub": claim_id,
        "exp": expires,
        "is_anonymous": anonymous,
        "iss": issuer,
        "role": "authenticated",
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    session = {
        "user": {"id": user_id},
        "access_token": "header." + payload + ".signature",
        "expires_at": expires,
        "refresh_token": refresh,
    }
    return {
        "cookies": [{"name": "cf_clearance", "domain": "chunlou.tv"}],
        "origins": [
            {
                "origin": "https://chunlou.tv",
                "localStorage": [{"name": key, "value": json.dumps(session)}],
            }
        ],
    }


def test_www_exports_parent_domain_cookies_but_not_parent_host_only_cookies():
    raw = {
        "cookies": [
            {"domain": domain} for domain in ["chunlou.tv", ".chunlou.tv", "www.chunlou.tv"]
        ],
        "origins": [],
    }
    result = site_state(raw, "https://www.chunlou.tv/")
    assert [cookie["domain"] for cookie in result["cookies"]] == [".chunlou.tv", "www.chunlou.tv"]


async def test_save_rejects_pending_verification_expired_and_anonymous_sessions_allows_retry():
    import time
    from types import SimpleNamespace

    service = LocalAuthService(NetworkPolicy())
    callback_count = 0

    class Window:
        site_id = SITE["id"]
        origin = ("https", "chunlou.tv", 443)
        guard = SimpleNamespace(returncode=None)
        closed = False

        async def snapshot(self):
            return self.raw

        async def close(self):
            self.closed = True

    window = service._window = Window()

    async def install(_site, _state):
        nonlocal callback_count
        callback_count += 1

    try:
        future = time.time() + 1800
        for raw in [
            {"cookies": [{"domain": "chunlou.tv", "name": "cf_clearance"}], "origins": []},
            _session(time.time() - 5),
            _session(float("inf")),
            _session(future, anonymous=True),
            _session(future, user_id="other-user"),
            _session(future, refresh=""),
            _session(future, key="sb-other-all-auth-token"),
            _session(future, issuer="https://other.supabase.co/auth/v1"),
        ]:
            window.raw = raw
            with pytest.raises(ValueError, match="账号登录"):
                await service.save(SITE, install)
            assert not window.closed and service.info(SITE["id"])["active"]
            assert callback_count == 0
        window.raw = _session(future)

        async def storage_failure(site, state):
            raise OSError("injected write failure")

        with pytest.raises(OSError, match="injected"):
            await service.save(SITE, storage_failure)
        assert not window.closed and service.info(SITE["id"])["active"]
        assert await service.save(SITE, install) == {
            "open": False,
            "saved": True,
            "verified": False,
        }
        assert callback_count == 1 and window.closed and not service.info(SITE["id"])["active"]
    finally:
        await service.close()


async def test_cancelled_request_finishes_native_window_cleanup():
    import asyncio
    from types import SimpleNamespace

    service = LocalAuthService(NetworkPolicy())
    started, release = asyncio.Event(), asyncio.Event()

    async def close():
        started.set()
        await release.wait()

    service._window = SimpleNamespace(site_id="source", close=close)
    closing = asyncio.create_task(service.cancel("source"))
    await started.wait()
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert service._window is None


def test_system_browser_selected_by_site_origin_and_does_not_require_lsof(monkeypatch):
    import sys

    monkeypatch.setattr("backend.local_auth.shutil.which", lambda _name: None)
    service = LocalAuthService(NetworkPolicy(), executable=sys.executable)
    info = service.info("custom-id", "https://chunlou.tv/")
    assert info["mode"] == "system-browser" and info["available"]
    assert not service.info("chunlou", "https://other.example/")["available"]


def test_offline_profile_copy_contains_only_browser_storage(tmp_path):
    from backend.local_auth import _copy_storage

    profile = tmp_path / "user"
    default = profile / "Default"
    (default / "IndexedDB").mkdir(parents=True)
    (profile / "Local State").write_text("state")
    (default / "Cookies").write_text("cookies")
    (default / "IndexedDB" / "value").write_text("database")
    (default / "History").write_text("unused")
    (default / "Sessions").symlink_to(tmp_path)
    copy = tmp_path / "copy"
    _copy_storage(profile, copy)
    assert (copy / "Default" / "IndexedDB" / "value").read_text() == "database"
    assert (copy / "Default" / "Cookies").is_file()
    assert not (copy / "Default" / "History").exists()
    assert not (copy / "Default" / "Sessions").exists()


async def test_interrupted_offline_export_finishes_before_window_reopens(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from backend.local_auth import _SystemWindow

    service = LocalAuthService(NetworkPolicy())
    window = _SystemWindow("source", ("https", "chunlou.tv", 443))
    window.guard = SimpleNamespace(returncode=None)
    service._window = window
    started, release = asyncio.Event(), asyncio.Event()
    events = []

    async def read():
        started.set()
        await release.wait()
        events.append("snapshot finished")
        return {"cookies": [], "origins": []}

    async def resume():
        events.append("reopened")

    monkeypatch.setattr(window, "_snapshot", read)
    monkeypatch.setattr(window, "resume", resume)
    saving = asyncio.create_task(service.save(SITE, None))
    await started.wait()
    saving.cancel()
    await asyncio.sleep(0)
    assert not saving.done() and events == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await saving
    assert events == ["snapshot finished", "reopened"]


async def test_chunlou_www_login_imports_the_canonical_browser_origin(monkeypatch):
    import sys
    import time
    from types import SimpleNamespace

    from backend.local_auth import _SystemWindow

    class Policy:
        def __init__(self):
            self.targets = []

        async def resolve(self, _url):
            raise AssertionError

        async def validate(self, url):
            self.targets.append(url)
            return url

    policy = Policy()
    service = LocalAuthService(policy, executable=sys.executable)
    site = {"id": "alias", "url": "https://www.chunlou.tv:443/"}
    launched = []

    async def start(window, binary, target):
        launched.append((window.origin, target))
        window.guard = SimpleNamespace(returncode=None)

        async def wait():
            import asyncio

            await asyncio.Event().wait()

        window.guard.wait = wait

    async def snapshot(_window):
        state = _session(time.time() + 600)
        state["origins"].append({"origin": "https://unrelated.example", "localStorage": []})
        return state

    async def close(_window):
        pass

    async def install(source, state):
        assert source["id"] == site["id"]
        assert [o["origin"] for o in state["origins"]] == ["https://chunlou.tv"]
        assert state["cookies"][0]["domain"] == "chunlou.tv"

    monkeypatch.setattr(_SystemWindow, "start", start)
    monkeypatch.setattr(_SystemWindow, "snapshot", snapshot)
    monkeypatch.setattr(_SystemWindow, "close", close)
    try:
        for denied in (
            "https://chunlou.tv:444/auth",
            "http://chunlou.tv/auth",
            "https://id.chunlou.tv/auth",
        ):
            with pytest.raises(ValueError):
                await service.open(site, denied)
        await service.open(site, "https://www.chunlou.tv:443/auth?r=/feed")
        assert launched == [(("https", "chunlou.tv", 443), "https://chunlou.tv/auth?r=/feed")]
        assert policy.targets == ["https://chunlou.tv/auth?r=/feed"]
        assert (await service.save(site, install))["saved"]
    finally:
        await service.close()


def test_lata_uses_system_browser_and_keeps_chunlou_account_import_separate(monkeypatch):
    import sys

    monkeypatch.setattr('backend.local_auth.shutil.which', lambda _name: None)
    service = LocalAuthService(NetworkPolicy(), executable=sys.executable)
    for url in ('https://lata.casa/series', 'https://www.lata.casa/login'):
        info = service.info('custom-lata-id', url)
        assert info['available'] and info['mode'] == 'system-browser'
        assert not info['canImportAccount']
    assert service.info('chunlou', 'https://chunlou.tv')['canImportAccount']
    for url in ('https://lata.casa:8443', 'http://lata.casa', 'https://lata.casa.other.test'):
        assert service.info('lata', url)['mode'] == 'guarded-browser'


async def test_lata_save_requires_site_login_and_filters_google_credentials(monkeypatch):
    from types import SimpleNamespace

    service = LocalAuthService(NetworkPolicy())
    site = {'id': 'lata-source', 'url': 'https://lata.casa/series'}
    captured = []

    class Window:
        site_id = site['id']
        origin = ('https', 'lata.casa', 443)
        guard = SimpleNamespace(returncode=None)
        closed = False

        async def snapshot(self):
            return {'cookies': [{'domain': 'lata.casa', 'name': 'site-session', 'value': 'test'},
                                {'domain': '.google.com', 'name': 'google-session', 'value': 'private'}],
                    'origins': [{'origin':'https://lata.casa', 'localStorage':[]},
                                {'origin':'https://accounts.google.com', 'localStorage':[]}]}

        async def close(self):
            self.closed = True

    async def verify(_policy, state):
        assert [c['name'] for c in state['cookies']] == ['site-session']
        assert [o['origin'] for o in state['origins']] == ['https://lata.casa']
        if not verified:
            raise ValueError('Lata 尚未确认账号登录')

    async def install(_site, state):
        captured.append(state)

    monkeypatch.setattr('backend.local_auth._verify_lata_login', verify, raising=False)
    window = service._window = Window()
    verified = False
    with pytest.raises(ValueError, match='Lata'):
        await service.save(site, install)
    assert not captured and not window.closed
    verified = True
    assert (await service.save(site, install))['saved']
    assert len(captured) == 1 and window.closed


async def test_lata_login_verification_is_read_only_and_requires_explicit_login(monkeypatch):
    import httpx

    from backend.local_auth import _verify_lata_login

    requests = []
    replies = [{'signedIn': False, 'member': True}, {'signedIn': 'true'}, {'signedIn': True, 'member': False}]

    class Policy:
        proxy_url = None
        async def validate(self, url):
            assert url == 'https://lata.casa/community/feed'
            return url

    async def respond(request):
        requests.append(request)
        assert request.method == 'GET' and request.headers['cookie'] == 'site-session=test'
        return httpx.Response(200, json=replies.pop(0))

    actual_client = httpx.AsyncClient
    monkeypatch.setattr('backend.local_auth.httpx.AsyncClient', lambda **kw: actual_client(transport=httpx.MockTransport(respond), **kw))
    state = {'cookies':[{'name':'site-session','value':'test','domain':'lata.casa','path':'/'}]}
    for _ in range(2):
        with pytest.raises(ValueError, match='登录'):
            await _verify_lata_login(Policy(), state)
    await _verify_lata_login(Policy(), state)
    assert len(requests) == 3
