import json

from fastapi.testclient import TestClient

from backend.site_adapters import build_catalog
from tests_backend.test_app import app_at, until


class Wallet:
    def __init__(self):
        self.calls = []
        self.owned = {2}
        self.remaining = 12
        self.uncertain = False

    async def quote(self, urls):
        self.pages = list(urls)
        result = {}
        for url in urls:
            n = int(url.rsplit("/", 1)[1])
            result[url] = {
                "status": "owned" if n in self.owned else "free" if n == 1 else "locked",
                "targetId": f"garden:{n}",
                "price": 4 if n < 5 else 3,
            }
        return {"accountId": "private-account", "balance": self.remaining, "episodes": result}

    async def unlock(self, url, *, expected_account, max_price, request_key, cancelled):
        n = int(url.rsplit("/", 1)[1])
        assert expected_account == "private-account" and not cancelled()
        price = 4 if n < 5 else 3
        assert max_price >= price
        self.calls.append((n, request_key))
        if self.uncertain:
            return {"status": "uncertain", "message": "站点响应超时，扣款结果待核对", "spent": None}
        self.owned.add(n)
        self.remaining -= price
        return {"status": "purchased", "message": "确认已解锁", "spent": price}


def scenario(tmp_path):
    wallet = Wallet()
    app = app_at(tmp_path)
    base = "https://lustflix.tv/#/watch/C02/"
    cat = build_catalog(base + "1", [{"url": base + str(i)} for i in range(1, 6)], expected_count=5)
    app.state.store.mutate(
        lambda s: s["scans"].append(
            {
                "id": "directory",
                "siteId": "lustflix",
                "url": base + "1",
                "status": "completed",
                "mode": "series",
                "catalog": cat,
            }
        )
    )
    app.state.tasks.purchase_factory = lambda site: wallet

    async def probe(site, options, on_asset, on_progress, cancelled):
        url = options["url"]
        n = int(url.rsplit("/", 1)[1])
        status = "ready" if n == 1 or n in wallet.owned else "needs-user"
        return {
            "status": "completed",
            "pagesVisited": 1,
            "_episodeOutcomes": {url: {"status": status, "message": status}},
        }

    app.state.tasks.scanner.scan = probe
    return app, wallet


def submit(client, options=None, keys=None):
    row = client.get("/api/state").json()["series"][0]
    keys = keys or [e["episodeKey"] for e in row["catalog"]["entries"]]
    body = {"episodeKeys": keys, "scanAndCheck": True}
    if options is not None:
        body["autoUnlock"] = options
    res = client.post(f"/api/series/{row['id']}/scan", json=body)
    assert res.status_code == 200, res.text
    scan_id = res.json()["id"]
    return until(
        client, lambda s: s["scans"][0]["id"] == scan_id and s["scans"][0]["status"] == "completed"
    )["scans"][0]


def test_optin_pays_only_for_locked_selection_within_batch_budget(tmp_path):
    app, wallet = scenario(tmp_path)
    with TestClient(app) as client:
        submit(client)
        assert not wallet.calls
        row = submit(client, {"maxCoins": 7})
        assert [n for n, _ in wallet.calls] == [3, 5]
        assert row["unlockSummary"]["spent"] == 7 and row["unlockSummary"]["unlocked"] == 2
        assert row["unlockSummary"]["skipped"] == 1
        assert "private-account" not in json.dumps(row)
        assert wallet.calls[0][1] not in json.dumps(client.get("/api/state").json())
        submit(client, {}, ["C02:2", "C02:3"])
        assert len(wallet.calls) == 2


def test_uncertain_purchase_never_automatically_charged_twice_and_scan_continues(tmp_path):
    app, wallet = scenario(tmp_path)
    wallet.uncertain = True
    with TestClient(app) as client:
        first = submit(client, {})
        assert first["checkedCount"] == 5 and first["unlockSummary"]["unknown"] == 1
        assert len(wallet.calls) == 1
        first_id = first["id"]
    reopened = app_at(tmp_path)
    reopened.state.tasks.purchase_factory = lambda site: wallet
    reopened.state.tasks.scanner.scan = app.state.tasks.scanner.scan
    with TestClient(reopened) as client:
        second = submit(client, {}, ["C02:3"])
        assert second["checkedCount"] == 1 and len(wallet.calls) == 1
        assert client.get("/api/state").json()["scans"][1]["id"] == first_id


def test_playable_episode_is_not_bought_even_if_wallet_initially_quotes_a_price(tmp_path):
    app, wallet = scenario(tmp_path)

    async def playable(site, options, *args):
        page = options["url"]
        return {
            "status": "completed",
            "_episodeOutcomes": {page: {"status": "ready", "message": "正片已取得"}},
        }

    app.state.tasks.scanner.scan = playable
    with TestClient(app) as client:
        result = submit(client, {}, ["C02:3"])
        assert wallet.calls == [] and result["unlockSummary"]["spent"] == 0


def test_coin_request_is_limited_to_observed_keys_and_explicit_combined_action(tmp_path):
    app, wallet = scenario(tmp_path)
    with TestClient(app) as client:
        row = client.get("/api/state").json()["series"][0]
        path = f"/api/series/{row['id']}/scan"
        assert (
            client.post(path, json={"episodeKeys": ["C02:3"], "autoUnlock": {}}).status_code == 400
        )
        assert (
            client.post(
                path, json={"episodeKeys": ["C02:999"], "scanAndCheck": True, "autoUnlock": {}}
            ).status_code
            == 400
        )
        for cap in (True, -1, 0, "100", 9007199254740992):
            assert (
                client.post(
                    path,
                    json={
                        "episodeKeys": ["C02:3"],
                        "scanAndCheck": True,
                        "autoUnlock": {"maxCoins": cap},
                    },
                ).status_code
                == 422
            )
        assert not wallet.calls


def test_interrupted_order_needs_permanent_receipt_to_reconcile(tmp_path):
    from backend.coin_unlock import CoinUnlockBatch, empty_summary

    app, wallet = scenario(tmp_path)
    store = app.state.store
    site = next(item for item in store.read()["sites"] if item["id"] == "lustflix")
    entry = store.read()["scans"][0]["catalog"]["entries"][2]
    key = entry["episodeKey"]
    store.mutate(
        lambda state: state["scans"][0].update(status="running", unlockSummary=empty_summary(10))
    )
    buyer = CoinUnlockBatch(store, "directory", site, wallet, 10)
    buyer.account = "private-account"
    order = buyer._order_key("garden:3")
    assert buyer._reserve(order, entry, 4)[0]
    store.recover_interrupted()
    interrupted = store.public_state()["scans"][0]
    assert interrupted["unlockResults"][key]["status"] == "uncertain"
    assert interrupted["unlockSummary"]["unknown"] == 1
    assert interrupted["unlockSummary"]["spent"] == 0

    # A subscription/playback grant does not prove that the old purchase completed.
    wallet.owned.add(3)
    with TestClient(app) as client:
        submit(client, {}, [key])
        assert store.read()["_coinUnlockOrders"][order]["status"] in {"pending", "uncertain"}
        wallet.owned.remove(3)
        retry = submit(client, {}, [key])
        assert retry["unlockSummary"]["unknown"] == 1 and not wallet.calls
        wallet.owned.add(3)
        original_quote = wallet.quote

        async def purchased_quote(urls):
            result = await original_quote(urls)
            for episode in result["episodes"].values():
                episode["ownedReceipt"] = True
            return result

        wallet.quote = purchased_quote
        confirmed = submit(client, {}, [key])
        assert store.read()["_coinUnlockOrders"][order]["status"] == "verified-owned"
        assert confirmed["unlockSummary"]["spent"] == 0 and not wallet.calls
