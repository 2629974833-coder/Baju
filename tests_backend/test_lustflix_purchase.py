"""Lustflix coin unlock protocol: all payment requests in these tests use MockTransport."""

import json

import httpx
import pytest

from backend.lustflix_purchase import LustflixPurchases

BASE = "https://lustflix.tv"
SLUG = "series-00000000-0000-0000-0000-000000000149"
SITE = {"id": "lf", "url": BASE}
KEY = "mock-episode-4-request-key"


def page(n=4, series=SLUG):
    return f"{BASE}/#/watch/{series}/{n}"


class Browser:
    async def cookies_for(self, identifier, url):
        assert identifier == "lf" and url.startswith(BASE + "/api/")
        return [{"name": "session", "value": "test-account"}]

    async def validate(self, url):
        assert url.startswith(BASE + "/")
        return url


class OfficialAPI:
    def __init__(self):
        self.uid = "account-a"
        self.balance = 11
        self.tickets = 0
        self.price = 4
        self.owned = {2}
        self.granted = set()
        self.transactions = []
        self.posts = []
        self.paths = []
        self.member_only = False
        self.available = True
        self.post_result = "normal"
        self.extra_debit = 0
        self.secured = False
        self.flip_on_content = False

    def wallet(self):
        return {
            "user": {"id": self.uid, "guest": False, "locked": self.secured},
            "wallet": {
                "balance": self.balance,
                "freeUnlocks": self.tickets,
                "unlocked": [f"{SLUG}:{n}" for n in self.owned],
                "transactions": self.transactions.copy(),
            },
        }

    def handle(self, request):
        path = request.url.path
        self.paths.append(path)
        assert request.url.host == "lustflix.tv"
        if path == "/api/tv/catalog":
            assert "cookie" not in request.headers
            return httpx.Response(200, json={"series": [{"code": "C149", "slug": SLUG}]})
        assert request.headers.get("cookie") == "session=test-account"
        if path == "/api/me":
            return httpx.Response(200, json=self.wallet())
        if path == "/api/content":
            assert request.method == "GET" and request.url.params["slug"] == SLUG
            if self.flip_on_content:
                self.uid = "other-account"
            rows = [
                {
                    "episode": n,
                    "available": self.available,
                    "viewerPriceCoins": 0 if n == 1 else self.price,
                }
                for n in (1, 2, 3, 4, 5)
            ]
            return httpx.Response(
                200,
                json={
                    "series": {"slug": SLUG, "memberOnly": self.member_only},
                    "episodes": rows,
                    "media": [
                        {"episode": n, "src": f"/api/media/{SLUG}/{n}/master.m3u8"}
                        for n in {1, *self.owned, *self.granted}
                    ],
                    "ownedEpisodes": sorted(self.owned),
                },
            )
        assert request.method == "POST" and path == "/api/payments/unlock"
        data = json.loads(request.content)
        self.posts.append(data)
        assert data == {
            "expectedUserId": "account-a",
            "slug": SLUG,
            "episode": 4,
            "requestKey": KEY,
        }
        if self.post_result == "unknown":
            raise httpx.ReadTimeout("uncertain")
        if self.post_result == "no_funds":
            return httpx.Response(409, json={"code": "insufficient_coins"})
        self.balance -= self.price + self.extra_debit
        self.transactions.append(
            {
                "id": "paid-transaction",
                "kind": "unlock",
                "amount": -self.price,
                "episode_key": f"{SLUG}:4",
                "unlockCreditsSpent": 0,
            }
        )
        self.owned.add(4)
        if self.post_result == "concurrent_purchase_and_rejection":
            return httpx.Response(409, json={"code": "insufficient_coins"})
        if self.post_result == "timeout_with_reference":
            self.transactions[-1]["requestKey"] = data["requestKey"]
            raise httpx.ReadTimeout("uncertain")
        if self.post_result == "ack_but_foreign_reference":
            self.transactions[-1]["requestKey"] = "another-client-request"
        if self.post_result == "timeout_after_charge":
            raise httpx.ReadTimeout("uncertain")
        if self.post_result == "change_account":
            self.uid = "other-account"
        return httpx.Response(200, json=self.wallet())


@pytest.fixture
def fixture(monkeypatch):
    api = OfficialAPI()
    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(api.handle), **kw)
    )
    return api, LustflixPurchases(SITE, Browser(), Browser())


async def unlock(adapter, **changes):
    options = {"expected_account": "account-a", "max_price": 4, "request_key": KEY}
    options.update(changes)
    return await adapter.unlock(page(), **options)


async def test_quote_uses_exact_selected_episodes_and_canonical_identity(fixture):
    api, adapter = fixture
    q = await adapter.quote([page(1, "C149"), page(2, "C149"), page(4, "C149")])
    assert list(q["episodes"]) == [page(1, "C149"), page(2, "C149"), page(4, "C149")]
    assert [e["status"] for e in q["episodes"].values()] == ["free", "owned", "locked"]
    assert q["accountId"] == "account-a" and q["balance"] == 11
    e = q["episodes"][page(4, "C149")]
    assert e["price"] == 4 and e["targetId"] == f"{SLUG}:4"
    q = await adapter.quote([page(4)])
    assert q["episodes"][page(4)]["targetId"] == e["targetId"]
    assert not api.posts


@pytest.mark.parametrize("reaction", ["normal", "timeout_with_reference"])
async def test_unlock_and_reconcile_exact_new_wallet_receipt_then_deduplicate(fixture, reaction):
    api, adapter = fixture
    api.post_result = reaction
    api.extra_debit = 2  # unrelated simultaneous consumption cannot alter our recorded coin cost
    result = await unlock(adapter)
    assert result["status"] == "purchased" and result["spent"] == 4
    assert (await unlock(adapter))["status"] == "owned"
    assert len(api.posts) == 1


@pytest.mark.parametrize(
    "change,status",
    [
        ({"tickets": 1}, "unavailable"),
        ({"price": 5}, "price-changed"),
        ({"balance": 3}, "insufficient"),
        ({"member_only": True}, "unavailable"),
        ({"available": False}, "unavailable"),
        ({"price": 0}, "free"),
        ({"uid": "other-account"}, "account-changed"),
    ],
)
async def test_unpurchasable_or_changed_target_sends_no_debit(fixture, change, status):
    api, adapter = fixture
    for k, v in change.items():
        setattr(api, k, v)
    result = await unlock(adapter)
    assert result["status"] == status and result["spent"] == 0
    assert not api.posts


@pytest.mark.parametrize(
    "reaction,status,spent",
    [
        ("no_funds", "insufficient", 0),
        ("unknown", "uncertain", None),
        ("timeout_after_charge", "uncertain", None),
        ("ack_but_foreign_reference", "uncertain", None),
        ("change_account", "uncertain", None),
    ],
)
async def test_post_errors_are_not_retried_or_billed_by_guess(fixture, reaction, status, spent):
    api, adapter = fixture
    api.post_result = reaction
    result = await unlock(adapter)
    assert result["status"] == status and result["spent"] == spent
    assert result["stop"] == (status == "uncertain")
    assert len(api.posts) == 1


async def test_cancel_or_off_domain_cannot_post(fixture):
    api, adapter = fixture
    assert (await unlock(adapter, cancelled=lambda: True))["spent"] == 0
    with pytest.raises(ValueError):
        await adapter.quote(["https://other.example/#/watch/C149/4"])
    assert not api.posts


async def test_protected_account_with_wallet_can_quote_and_insufficient_only_skips_one(fixture):
    api, adapter = fixture
    api.secured = True
    api.balance = 2
    assert (await adapter.quote([page()]))["accountId"] == "account-a"
    result = await unlock(adapter)
    assert result["status"] == "insufficient" and result["stop"] is False and not api.posts


async def test_account_switch_during_price_read_invalidates_quote(fixture):
    api, adapter = fixture
    api.flip_on_content = True
    with pytest.raises(ValueError, match="账号"):
        await adapter.quote([page()])
    assert not api.posts


async def test_cancellation_during_payment_header_lookup_never_sends(fixture):
    api, adapter = fixture
    cancelled = False

    class SwitchingBrowser(Browser):
        async def cookies_for(self, identifier, url):
            nonlocal cancelled
            if url.endswith("/api/payments/unlock"):
                cancelled = True
            return await super().cookies_for(identifier, url)

    adapter.browser = SwitchingBrowser()
    result = await unlock(adapter, cancelled=lambda: cancelled)
    assert result["status"] == "cancelled" and not api.posts


async def test_paid_receipt_without_current_entitlement_skips_only_the_matching_episode(fixture):
    api, adapter = fixture
    api.transactions = [
        {"id": "other-show", "kind": "unlock", "episode_key": "unrelated:5", "amount": -4},
        {"id": "other-kind", "kind": "reward", "episode_key": f"{SLUG}:5", "amount": -4},
        {
            "id": "historical",
            "kind": "unlock",
            "episode_key": f"{SLUG}:4",
            "amount": -4,
            "unlockCreditsSpent": 0,
        },
    ]
    quote = await adapter.quote([page(4), page(5)])
    assert quote["episodes"][page(5)]["status"] == "locked"
    line = quote["episodes"][page(4)]
    assert line["status"] == "unavailable" and "核对" in line["message"]
    result = await unlock(adapter)
    assert result["status"] == "unavailable" and result["spent"] == 0
    assert not result["stop"] and not api.posts


async def test_rejected_purchase_with_concurrent_external_debit_is_never_billed(fixture):
    api, adapter = fixture
    api.post_result = "concurrent_purchase_and_rejection"
    result = await unlock(adapter)
    assert result["status"] == "owned" and result["spent"] == 0 and not result["stop"]
    assert api.owned == {2, 4} and len(api.posts) == 1


async def test_only_verified_paid_episode_has_owned_receipt(fixture):
    api, adapter = fixture
    api.granted = {3, 4}
    api.transactions = [
        {"id": "", "kind": "unlock", "episode_key": f"{SLUG}:3", "amount": -4},
        {"id": "spent-other-show", "kind": "unlock", "episode_key": "other:3", "amount": -4},
        {
            "id": "receipt-four",
            "kind": "unlock",
            "episode_key": f"{SLUG}:4",
            "amount": -4,
            "unlockCreditsSpent": 0,
        },
    ]
    episodes = (await adapter.quote([page(n) for n in range(1, 6)]))["episodes"]
    assert [e["status"] for e in episodes.values()] == ["free", "owned", "owned", "owned", "locked"]
    assert [e.get("ownedReceipt", False) for e in episodes.values()] == [
        False,
        False,
        False,
        True,
        False,
    ]
