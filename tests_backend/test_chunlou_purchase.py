"""Site protocol tests using only a simulated issuer, wallet and unlock receipt."""

import json
from types import SimpleNamespace

import httpx
import pytest

from backend import chunlou
from backend.chunlou_purchase import ChunlouPurchases

BASE = f"https://{chunlou.AUTH_PROJECT}.supabase.co/rest/v1/"
USER = "22222222-1111-4111-8111-111111111111"
OTHER = "33333333-1111-4111-8111-111111111111"
EP = "11111111-1111-4111-8111-111111111111"
DRAMA = "44444444-1111-4111-8111-111111111111"
PAGE = f"https://chunlou.tv/feed?ep={EP}"
TOKEN = "fixture-only"


class Policy:
    async def validate(self, url):
        assert url.startswith(f"https://{chunlou.AUTH_PROJECT}.supabase.co/")
        return url


@pytest.fixture
def setup(monkeypatch):
    state = SimpleNamespace(
        uid=USER,
        balance=17,
        paid=False,
        price=12,
        ratio=0.8,
        owned=False,
        seq=4,
        failure=None,
        receipt_read_failure=False,
        vip=None,
        extra=0,
        creator=None,
        vip_only=False,
        flash=None,
        posted=[],
        billed=None,
        existing_debit=False,
        foreign=False,
    )

    def respond(request):
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert request.headers["apikey"] == "public-test"
        name = request.url.path.split("/")[-1]
        if name == "user":
            return httpx.Response(200, json={"id": state.uid, "is_anonymous": False})
        if name == "profiles":
            assert request.url.params["user_id"] == "eq." + USER
            return httpx.Response(
                200,
                json=[
                    {
                        "user_id": USER,
                        "coins": state.balance,
                        "vip_until": state.vip,
                        "vip_free_extra": state.extra,
                    }
                ],
            )
        if name == "my_price_ratio":
            return httpx.Response(200, json={"ratio": state.ratio, "pbucket": 20})
        if name == "app_settings":
            return httpx.Response(
                200, json=[{"key": "flash_sale", "value": state.flash}] if state.flash else []
            )
        if name == "episodes":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": EP,
                        "drama_id": DRAMA,
                        "kind": "main",
                        "status": "published",
                        "seq": state.seq,
                        "coin_price": state.price,
                        "drama": {
                            "id": DRAMA,
                            "status": "published",
                            "free_episodes": 1,
                            "owner_id": state.creator,
                            "house_owned": True,
                            "vip_only": state.vip_only,
                        },
                    }
                ],
            )
        if name == "coin_logs":
            assert request.method == "GET"
            assert request.url.params["user_id"] == "eq." + USER
            assert request.url.params["reason"] == "eq.unlock"
            assert request.url.params["ref_id"] == "eq." + EP
            rows = []
            if state.existing_debit:
                rows.append({"id": "old", "reason": "unlock", "ref_id": EP, "delta": -9})
            if state.paid and state.failure != "no-ledger":
                charged = (
                    state.billed
                    if state.billed is not None
                    else state.posted[-1]["p_expected_price"]
                )
                rows.append({"id": "new", "reason": "unlock", "ref_id": EP, "delta": -charged})
            return httpx.Response(200, json=rows)
        if name == "unlocks":
            assert request.url.params["user_id"] == "eq." + USER
            if state.receipt_read_failure and state.posted:
                return httpx.Response(503)
            return httpx.Response(
                200, json=[{"episode_id": EP}] if state.owned or state.paid else []
            )
        assert name == "unlock_episode" and request.method == "POST"
        body = json.loads(request.content)
        state.posted.append(body)
        if state.failure == "timeout":
            state.paid = True
            raise httpx.ReadTimeout("ambiguous")
        if state.failure == "server":
            return httpx.Response(500, json={})
        if state.failure == "race":
            state.owned = True
            return httpx.Response(400, json={"message": "ALREADY_UNLOCKED"})
        if state.failure == "price":
            return httpx.Response(400, json={"message": "PRICE_CHANGED"})
        state.paid = True
        return httpx.Response(200, json={})

    def client(_policy):
        return httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False)

    async def config(_client, _policy):
        return (
            BASE.replace(chunlou.AUTH_PROJECT, "foreign.example") if state.foreign else BASE,
            {"apikey": "public-test"},
        )

    async def account(_self):
        return USER, TOKEN

    monkeypatch.setattr(chunlou, "_client", client)
    monkeypatch.setattr(chunlou, "_directory_origin", config)
    monkeypatch.setattr(ChunlouPurchases, "_account", account)
    return ChunlouPurchases({"id": "chunlou", "url": "https://chunlou.tv"}, None, Policy()), state


def buy(adapter, cap=12, account=USER):
    return adapter.unlock(
        PAGE,
        expected_account=account,
        max_price=cap,
        request_key="stable-test-order-id",
        cancelled=lambda: False,
    )


async def test_discount_quote_then_purchase_and_repeat_without_charging_again(setup):
    adapter, s = setup
    q = await adapter.quote([PAGE])
    row = q["episodes"][PAGE]
    assert (q["accountId"], q["balance"], row["status"], row["price"], row["targetId"]) == (
        USER,
        17,
        "locked",
        9,
        EP,
    )
    assert s.posted == []
    assert not row.get("ownedReceipt")
    result = await buy(adapter, 9)
    assert (result["status"], result["spent"]) == ("purchased", 9)
    assert (await adapter.quote([PAGE]))["episodes"][PAGE]["ownedReceipt"] is True
    assert (await buy(adapter))["status"] == "owned"
    assert s.posted == [{"p_episode_id": EP, "p_expected_price": 9}]


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("owned", True, "owned"),
        ("balance", 8, "insufficient"),
        ("creator", USER, "free"),
        ("seq", 1, "free"),
        ("price", 18, "price-changed"),
        ("vip_only", True, "unavailable"),
    ],
)
async def test_no_post_if_not_necessary_or_not_allowed(setup, field, value, expected):
    api, s = setup
    setattr(s, field, value)
    assert (await buy(api))["status"] == expected
    assert not s.posted


async def test_vip_and_flash_sale_follow_official_price_rules(setup):
    api, s = setup
    s.vip = "2999-01-01T00:00:00Z"
    s.extra = 3
    vip_line = (await api.quote([PAGE]))["episodes"][PAGE]
    assert vip_line["status"] == "free" and not vip_line.get("ownedReceipt")
    s.seq = 5
    s.flash = {"enabled": True, "drama_id": DRAMA, "ratio": 0.5, "ends_at": s.vip}
    assert (await buy(api, cap=6))["spent"] == 6
    assert s.posted == [{"p_episode_id": EP, "p_expected_price": 6}]


@pytest.mark.parametrize(
    "failure,status",
    [
        ("price", "price-changed"),
        ("race", "owned"),
        ("server", "uncertain"),
        ("timeout", "uncertain"),
    ],
)
async def test_no_automatic_retry_after_submission(setup, failure, status):
    api, s = setup
    s.failure = failure
    result = await buy(api)
    assert result["status"] == status
    assert result["spent"] == (None if status == "uncertain" else 0)
    assert len(s.posted) == 1


async def test_no_receipt_yet_or_recheck_failed_is_uncertain(setup):
    api, s = setup
    s.receipt_read_failure = True
    result = await buy(api)
    assert result["status"] == "uncertain" and result["stop"] is True and result["spent"] is None


async def test_pin_account_origin_and_target(setup):
    api, s = setup
    assert (await buy(api, account=OTHER))["stop"]
    assert not s.posted
    with pytest.raises(ValueError):
        await api.quote([PAGE.replace("chunlou.tv", "other.example")])
    s.foreign = True
    with pytest.raises(ValueError):
        await api.quote([PAGE])
    assert not s.posted


async def test_receipt_without_site_debit_does_not_claim_quote_as_spent(setup):
    api, s = setup
    s.failure = "no-ledger"
    result = await buy(api)
    assert s.paid and result["status"] == "uncertain" and result["spent"] is None
    assert result["stop"] and len(s.posted) == 1


async def test_reports_actual_posted_debit_instead_of_estimated_price(setup):
    api, s = setup
    s.billed = 7
    result = await buy(api)
    assert s.posted[0]["p_expected_price"] == 9
    assert result["status"] == "purchased" and result["spent"] == 7


async def test_prior_debit_with_missing_entitlement_never_charges_again(setup):
    api, s = setup
    s.existing_debit = True
    result = await buy(api)
    assert result["stop"] and not s.posted


async def test_cancelled_while_quoted_does_not_submit_purchase(setup):
    api, s = setup
    result = await api.unlock(
        PAGE, expected_account=USER, max_price=10, request_key="test-cancel", cancelled=lambda: True
    )
    assert result["spent"] == 0 and not s.posted
