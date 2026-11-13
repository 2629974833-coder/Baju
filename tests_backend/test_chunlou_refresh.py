"""A read-only scan can use the saved account after its access token expires."""

import asyncio
import copy
import json
import time

import httpx
import pytest

from backend import chunlou
from backend.browser import BrowserService
from tests_backend.test_chunlou_account_import import ISSUER, KEY, LEGACY_KEY, PROJECT, payload
from tests_backend.test_downloader import Policy

SITE = {"id": "chunlou", "url": "https://chunlou.tv/"}
TOKEN_URL = ISSUER + "/token?grant_type=refresh_token"


def snapshot(*, valid_for=-120, key=KEY):
    expires = int(time.time()) + valid_for
    session = payload(claims={"exp": expires}, fields={"expires_at": expires})["session"]
    return {
        "cookies": [],
        "origins": [
            {
                "origin": "https://chunlou.tv",
                "localStorage": [
                    {"name": key, "value": session},
                    {"name": "age-verified-at", "value": "1700000000000"},
                ],
                "indexedDB": [],
            },
            {
                "origin": "https://elsewhere.example",
                "localStorage": [{"name": KEY, "value": "other-app"}],
            },
        ],
    }


def mock_identity(monkeypatch, *, override=None, status=200, redirect=False, foreign=False):
    calls = []
    real = httpx.AsyncClient

    async def config(_client, _policy):
        origin = "elsewhere.example" if foreign else PROJECT + ".supabase.co"
        return f"https://{origin}/rest/v1/", {"apikey": "sb_publishable_fixture"}

    async def send(request):
        calls.append(str(request.url))
        assert request.method == "POST" and calls[-1] == TOKEN_URL
        assert json.loads(request.content) == {"refresh_token": "fixture-secret-refresh"}
        assert request.headers["apikey"] == "sb_publishable_fixture"
        if redirect:
            return httpx.Response(307, headers={"Location": "https://elsewhere.example"})
        await asyncio.sleep(0.02)
        return httpx.Response(
            status,
            content=override or payload(fields={"refresh_token": "rotated-fixture"})["session"],
        )

    monkeypatch.setattr(chunlou, "_directory_origin", config)
    monkeypatch.setattr(
        chunlou,
        "_client",
        lambda _: real(transport=httpx.MockTransport(send), follow_redirects=False),
    )
    return calls


@pytest.mark.parametrize("valid_for", [-120, 60])
async def test_parallel_scans_rotate_once_and_preserve_encrypted_state(
    tmp_path, monkeypatch, valid_for
):
    service = BrowserService(tmp_path, Policy())
    calls = mock_identity(monkeypatch)
    try:
        saved = snapshot(key=LEGACY_KEY, valid_for=valid_for)
        await service.import_local_session(SITE, saved)
        contexts = await asyncio.gather(*(service.create_scan_context(SITE) for _ in range(2)))
        for context in contexts:
            await context.close()
        assert calls == [TOKEN_URL]
        state = service._saved_state("chunlou")
        expected = copy.deepcopy(saved)
        new_item = state["origins"][0]["localStorage"][0]
        assert json.loads(new_item["value"])["refresh_token"] == "rotated-fixture"
        expected["origins"][0]["localStorage"][0] = new_item
        assert state == expected
        assert b"rotated-fixture" not in service._state_path("chunlou").read_bytes()
    finally:
        await service.close()


@pytest.mark.parametrize(
    ("issue", "message"),
    [
        ("revoked", "重新关联"),
        ("wrong-user", "重新关联"),
        ("network", "稍后重试"),
        ("redirect", "稍后重试"),
        ("foreign", "配置"),
    ],
)
async def test_failed_refresh_preserves_original_state(tmp_path, monkeypatch, issue, message):
    kwargs = {"foreign": issue == "foreign", "redirect": issue == "redirect"}
    if issue == "revoked":
        kwargs["status"] = 400
    elif issue == "network":
        kwargs["status"] = 503
    elif issue == "wrong-user":
        kwargs["override"] = payload(claims={"sub": "other-user"})["session"]
    calls = mock_identity(monkeypatch, **kwargs)
    service = BrowserService(tmp_path, Policy())
    try:
        await service.import_local_session(SITE, snapshot())
        previous = service._state_path("chunlou").read_bytes()
        with pytest.raises(ValueError, match=message):
            ctx = await service.create_scan_context(SITE)
            await ctx.close()
        assert service._state_path("chunlou").read_bytes() == previous
        assert calls == ([] if issue == "foreign" else [TOKEN_URL])
    finally:
        await service.close()


async def test_valid_and_foreign_sessions_skip_refresh(tmp_path, monkeypatch):
    service = BrowserService(tmp_path, Policy())
    calls = mock_identity(monkeypatch)
    try:
        for site, state in [
            (SITE, snapshot(valid_for=3600)),
            ({"id": "another", "url": "https://example.org"}, snapshot()),
        ]:
            await service.import_local_session(site, state)
            ctx = await service.create_scan_context(site)
            await ctx.close()
            assert service._saved_state(site["id"]) == state
        assert not calls
    finally:
        await service.close()


async def test_cancelling_scan_finishes_accepted_rotation_before_unlocking(tmp_path, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    from backend import session_refresh

    mock_identity(monkeypatch)
    exchange = session_refresh._renew

    async def delayed(policy, saved):
        result = await exchange(policy, saved)
        started.set()
        await release.wait()
        return result

    monkeypatch.setattr(session_refresh, "_renew", delayed)
    service = BrowserService(tmp_path, Policy())
    try:
        await service.import_local_session(SITE, snapshot())
        scan = asyncio.create_task(service.create_scan_context(SITE))
        await asyncio.wait_for(started.wait(), timeout=5)
        scan.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await scan
        account = service._saved_state("chunlou")["origins"][0]["localStorage"][0]["value"]
        assert json.loads(account)["refresh_token"] == "rotated-fixture"
    finally:
        release.set()
        await service.close()
