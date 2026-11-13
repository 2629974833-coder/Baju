"""Coin purchases explicitly requested in a series check. Persist intent before any write to a site."""

import asyncio
import hashlib
import json
from urllib.parse import urlsplit
from uuid import uuid4

from backend.store import timestamp

COIN_SITES = {"lustflix": "lustflix.tv", "chunlou": "chunlou.tv"}
_UNCERTAIN = {"pending", "uncertain"}
_LOCAL_CONFIRM = {"purchased", "verified-owned"}


def supports_coin_unlock(site):
    domain = COIN_SITES.get(site.get("id"))
    parsed = urlsplit(site.get("url") or "")
    return bool(domain and parsed.scheme == "https" and parsed.netloc in {domain, "www." + domain})


def purchase_adapter(site, browser, policy):
    if not supports_coin_unlock(site):
        raise ValueError("此来源尚未接入按集金币解锁")
    if site["id"] == "lustflix":
        from backend.lustflix_purchase import LustflixPurchases

        return LustflixPurchases(site, browser, policy)
    from backend.chunlou_purchase import ChunlouPurchases

    return ChunlouPurchases(site, browser, policy)


def empty_summary(limit):
    return dict(
        unlocked=0,
        spent=0,
        unknown=0,
        skipped=0,
        limit=limit,
        reasons={},
        lastMessage="",
        stopped=False,
    )


def _coins(value):
    return type(value) is int and value >= 0


class CoinUnlockBatch:
    def __init__(self, store, scan_id, site, adapter, limit):
        self.store, self.scan_id, self.site = store, scan_id, site
        self.adapter, self.limit = adapter, limit
        self.summary = empty_summary(limit)
        self.quotes = {}
        self.account = None
        self.stop_message = ""
        self.unresolved_orders = set()

    def _scan(self, state):
        return next(row for row in state["scans"] if row["id"] == self.scan_id)

    def _save_note(
        self, entry, status, message, *, spent=0, order=None, stop=False, reconciled=False
    ):
        """Commit the receipt and its public batch totals in the same transaction."""
        s = self.summary
        if stop:
            self.stop_message = message
            s["stopped"] = True
        s["lastMessage"] = message
        if status == "purchased":
            s["unlocked"] += 1
        elif status == "uncertain":
            s["unknown"] += 1
        elif status not in {"owned", "free"}:
            s["skipped"] += 1
            s["reasons"][message] = s["reasons"].get(message, 0) + 1
        s["spent"] += spent if _coins(spent) else 0
        details = {"status": status, "message": message, "spent": spent}
        if reconciled:
            details["reconciled"] = True

        def persist(state):
            scan = self._scan(state)
            scan["unlockSummary"] = s
            scan.setdefault("unlockResults", {})[entry["episodeKey"]] = details
            if order is not None:
                state["_coinUnlockOrders"][order].update(
                    status="verified-owned" if reconciled else status,
                    spent=None if reconciled else spent,
                    updatedAt=timestamp(),
                )

        self.store.mutate(persist)
        return details

    async def prepare(self, urls):
        try:
            quote = await self.adapter.quote(urls)
            self.account = quote["accountId"]
            if (
                not isinstance(self.account, str)
                or not self.account
                or not _coins(quote["balance"])
            ):
                raise ValueError("无法确认当前账号或金币余额")
            self.unresolved_orders = {
                key
                for key, value in self.store.read().get("_coinUnlockOrders", {}).items()
                if value.get("status") in _UNCERTAIN
            }
            if not isinstance(quote["episodes"], dict):
                raise ValueError("无法读取单集价格")
            self.quotes = quote["episodes"]
        except Exception as exc:
            self.stop_message = (
                str(exc) if isinstance(exc, ValueError) else "无法读取站点账号和钱包，请检查授权"
            )
            self.stop_message = self.stop_message[:220] or "无法确认账号和余额"
            self.summary.update(stopped=True, lastMessage=self.stop_message)
            self.store.mutate(lambda state: self._scan(state).update(unlockSummary=self.summary))

    def _order_key(self, target):
        # An episode may have several share URLs. The adapter supplies its canonical purchase ID.
        identity = json.dumps([self.site["id"], self.account, target], separators=(",", ":"))
        return hashlib.sha256(identity.encode()).hexdigest()

    def _reserve(self, key, entry, price):
        def reserve(state):
            orders = state.setdefault("_coinUnlockOrders", {})
            existing = orders.get(key, {})
            if existing.get("status") in _UNCERTAIN | _LOCAL_CONFIRM:
                return None, existing["status"]
            request_key = str(uuid4())
            orders[key] = {
                "status": "pending",
                "requestKey": request_key,
                "scanId": self.scan_id,
                "episodeKey": entry["episodeKey"],
                "quotedPrice": price,
                "updatedAt": timestamp(),
            }
            self._scan(state).setdefault("unlockResults", {})[entry["episodeKey"]] = {
                "status": "pending",
                "spent": None,
                "message": "已登记订单，等待站点确认",
            }
            return request_key, None

        return self.store.mutate(reserve)

    async def check(self, entry, cancelled, *, playable):
        url = entry["pageUrl"]
        item = self.quotes.get(url) or {}
        status = item.get("status")
        target = item.get("targetId")
        order = (
            self._order_key(target) if isinstance(target, str) and target and self.account else None
        )
        if (
            status == "owned"
            and item.get("ownedReceipt") is True
            and order in self.unresolved_orders
        ):
            self.unresolved_orders.discard(order)
            return self._save_note(
                entry,
                "owned",
                "站点已确认本集归当前账号所有；前次实际扣款请查看账单",
                order=order,
                reconciled=True,
            )
        if playable or status in {"free", "owned"}:
            return None
        if self.stop_message:
            return self._save_note(entry, "skipped", self.stop_message)
        if status != "locked" or not order or not _coins(item.get("price")) or not item["price"]:
            return self._save_note(
                entry, "skipped", item.get("message") or "没有可确认的单集金币报价"
            )
        price = item["price"]
        if self.limit is not None and price > self.limit - self.summary["spent"]:
            return self._save_note(entry, "skipped", "本集价格超出本次剩余金币预算")
        if cancelled():
            raise asyncio.CancelledError()
        key, previous = self._reserve(order, entry, price)
        if previous in _UNCERTAIN:
            return self._save_note(
                entry, "uncertain", "本集前次付款尚未核实，已停止本批扣币", spent=None, stop=True
            )
        if key is None:
            return self._save_note(entry, "skipped", "本集已有付款记录，待核对站点账单与权限")
        unclear = "付款结果待核对，本次已停止继续扣币"
        try:
            async with asyncio.timeout(100):
                result = await self.adapter.unlock(
                    url,
                    expected_account=self.account,
                    max_price=price,
                    request_key=key,
                    cancelled=cancelled,
                )
        except asyncio.CancelledError:
            self._save_note(entry, "uncertain", unclear, spent=None, order=order, stop=True)
            raise
        except Exception:
            result = {"status": "uncertain", "message": unclear, "spent": None}
        if not isinstance(result, dict):
            result = {}
        status = result.get("status")
        spent = result.get("spent")
        message = result.get("message") or "已核对本集金币解锁结果"
        if status == "purchased" and _coins(spent):
            if spent > price:
                message = f"已解锁，实际扣除 {spent} 金币，高于提交前报价；本批停止继续扣币"
            return self._save_note(
                entry,
                status,
                message,
                spent=spent,
                order=order,
                stop=spent > price or bool(result.get("stop")),
            )
        if (
            status
            in {
                "owned",
                "free",
                "unavailable",
                "insufficient",
                "price-changed",
                "failed",
                "cancelled",
                "account-changed",
                "unsupported",
                "skipped",
            }
            and _coins(spent)
            and spent == 0
        ):
            return self._save_note(
                entry, status, message, order=order, stop=bool(result.get("stop"))
            )
        return self._save_note(
            entry,
            "uncertain",
            message if status == "uncertain" else unclear,
            spent=spent if _coins(spent) else None,
            order=order,
            stop=True,
        )
