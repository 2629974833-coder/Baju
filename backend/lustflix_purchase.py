"""Single-episode coin payments on the official Lustflix API. No implicit POST retries."""

import asyncio
import json
import re
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx

from backend.lustflix_directory import _public_get, _script_url, _series_reference, directory_route

_ORIGIN = "https://lustflix.tv"
_KEY = re.compile(r"[A-Za-z0-9_-]{16,80}")
_ALIAS = re.compile(r"C\d+", re.IGNORECASE)
_SLUG = re.compile(r"[a-zA-Z0-9_-]{1,100}")
_MAX_JSON = 2 * 1024**2


def _coins(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def _result(status, message, *, price=None, spent=0, stop=False):
    return {"status": status, "message": message, "price": price, "spent": spent, "stop": stop}


@dataclass
class _Account:
    user: str
    balance: int
    tickets: int
    owned: set
    transactions: list


def _account(payload):
    user = payload.get("user") or {}
    wallet = payload.get("wallet") or {}
    if (
        not isinstance(user, dict)
        or not isinstance(user.get("id"), str)
        or not user["id"]
        or user.get("guest")
    ):
        raise ValueError("请先在 Lustflix 登录并保存可用的账号会话")
    if not isinstance(wallet, dict) or not _coins(wallet.get("balance")):
        raise ValueError("站点未返回可核对的金币余额")
    rewards = payload.get("rewards") or {}
    tickets = wallet.get(
        "freeUnlocks", rewards.get("availableUnlocks", 0) if isinstance(rewards, dict) else 0
    )
    if not _coins(tickets):
        raise ValueError("站点未返回有效的单集券状态")
    owned = wallet.get("unlocked", [])
    transactions = wallet.get("transactions", [])
    if not isinstance(owned, list) or not isinstance(transactions, list):
        raise ValueError("站点钱包格式不完整")
    return _Account(
        user["id"],
        wallet["balance"],
        tickets,
        {k for k in owned if isinstance(k, str)},
        [t for t in transactions if isinstance(t, dict)],
    )


class _CancelledBeforeDispatch(ValueError):
    pass


class _StatusError(ValueError):
    def __init__(self, status, code):
        super().__init__("站点请求被拒绝" if status < 500 else "站点暂时不可用")
        self.status, self.code = status, code


def _declined(error, price):
    if not 400 <= error.status < 500:
        return None
    if error.code in {"insufficient_coins", "insufficient_balance"}:
        return _result("insufficient", "站点拒绝支付：金币余额不足", price=price)
    if error.code == "account_changed":
        return _result("account-changed", "站点拒绝支付：账号已切换", price=price, stop=True)
    if error.code == "membership_includes_episode":
        return _result("unavailable", "站点已拒绝购买，请核对会员播放权限", price=price)
    return None


def _attributable(debit, request_key, acknowledged):
    # A new transaction can come from another device. A lost response needs
    # the reference on the ledger; explicit foreign references always win.
    references = [debit[key] for key in ("requestKey", "request_key") if key in debit]
    if references:
        return all(reference == request_key for reference in references)
    return acknowledged


class LustflixPurchases:
    def __init__(self, site, browser, policy):
        official = urlsplit(site.get("url", ""))
        if official.scheme != "https" or official.netloc not in {"lustflix.tv", "www.lustflix.tv"}:
            raise ValueError("该来源未接入 Lustflix 金币解锁")
        self.site, self.browser, self.policy = site, browser, policy
        self._aliases = {}

    def _client(self):
        return httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=12,
            proxy=getattr(self.policy, "proxy_url", None),
        )

    async def _json(self, client, path, *, payment=None, on_dispatch=None):
        url = await self.policy.validate(_ORIGIN + path)
        if urlsplit(url).netloc != "lustflix.tv" or urlsplit(url).scheme != "https":
            raise ValueError("站点地址不匹配")
        headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
        cookies = await self.browser.cookies_for(self.site["id"], url)
        if cookies:
            headers["Cookie"] = "; ".join(f"{item['name']}={item['value']}" for item in cookies)
        client.cookies.clear()
        method = "GET"
        if payment is not None:
            if path != "/api/payments/unlock":
                raise ValueError("单集解锁地址无效")
            method = "POST"
            headers.update({"Origin": _ORIGIN, "Referer": _ORIGIN + "/"})
        if on_dispatch is not None:
            on_dispatch()
        async with client.stream(method, url, headers=headers, json=payment) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes(65536):
                if len(body) + len(chunk) > _MAX_JSON:
                    raise ValueError("站点回复超过读取上限")
                body.extend(chunk)
            try:
                value = json.loads(body)
            except (ValueError, RecursionError):
                value = None
            if response.status_code != 200:
                code = value.get("code") if isinstance(value, dict) else None
                raise _StatusError(response.status_code, code if isinstance(code, str) else "")
            if not isinstance(value, dict):
                raise ValueError("站点未返回有效的账号或解锁结果")
            return value

    async def _slug(self, client, key):
        if not _ALIAS.fullmatch(key):
            return key
        if key not in self._aliases:
            index = json.loads(
                await _public_get(client, self.policy, _ORIGIN + "/api/tv/catalog", None)
            )
            rows = index.get("series") if isinstance(index, dict) else None
            if not isinstance(rows, list):
                raise ValueError("无法核对选集对应的站点剧名编号")
            matches = [
                row.get("slug") for row in rows if isinstance(row, dict) and row.get("code") == key
            ]
            if not matches:
                html = await _public_get(client, self.policy, _ORIGIN + "/", None)
                script = await _public_get(client, self.policy, _script_url(_ORIGIN, html), None)
                ref = _series_reference(script, key)
                matches = [ref.get("slug")] if ref else []
            if (
                len(matches) != 1
                or not isinstance(matches[0], str)
                or not _SLUG.fullmatch(matches[0])
            ):
                raise ValueError("无法唯一核对选集所属剧集")
            self._aliases[key] = matches[0]
        return self._aliases[key]

    @staticmethod
    def _routes(urls):
        if not 1 <= len(urls) <= 1000:
            raise ValueError("请选择 1–1000 集")
        routes = {}
        for url in urls:
            route = directory_route(url)
            if (
                not route
                or not route[0].startswith("/watch/")
                or not route[2]
                or int(route[2]) > 1000
            ):
                raise ValueError("只允许为选中的 Lustflix 单集报价")
            routes[url] = (route[1], int(route[2]))
        if len({key for key, _ in routes.values()}) != 1:
            raise ValueError("一次只能为同一部剧的选集报价")
        return routes

    async def _read_quote(self, client, urls):
        routes = self._routes(urls)
        key = next(iter(routes.values()))[0]
        slug = await self._slug(client, key)
        before = _account(await self._json(client, "/api/me"))
        data = await self._json(client, "/api/content?" + urlencode({"slug": slug}))
        series = data.get("series")
        rows, media, owned = data.get("episodes"), data.get("media"), data.get("ownedEpisodes")
        if (
            not isinstance(series, dict)
            or series.get("slug") != slug
            or not all(isinstance(v, list) for v in (rows, media, owned))
        ):
            raise ValueError("无法核对站点当前剧集及购买权限")
        by_number = {}
        for row in rows:
            n = row.get("episode") if isinstance(row, dict) else None
            if type(n) is not int or not 1 <= n <= 1000 or n in by_number:
                raise ValueError("站点集号发生变化，请重新读取目录")
            by_number[n] = row
        grants = {
            m["episode"]
            for m in media
            if isinstance(m, dict)
            and type(m.get("episode")) is int
            and isinstance(m.get("src"), str)
            and m["src"]
        }
        owned = {n for n in owned if type(n) is int}
        account = _account(await self._json(client, "/api/me"))
        if before.user != account.user:
            raise ValueError("读取单集报价期间站点账号已切换，请重试")
        debited_targets, receipt_targets = set(), set()
        for tx in account.transactions:
            target = tx.get("episode_key")
            if (
                tx.get("kind") == "unlock"
                and isinstance(target, str)
                and type(tx.get("amount")) is int
                and tx["amount"] < 0
            ):
                debited_targets.add(target)
                if isinstance(tx.get("id"), str) and tx["id"]:
                    receipt_targets.add(target)
        episodes = {}
        for url, (_, number) in routes.items():
            row = by_number.get(number, {})
            target = f"{slug}:{number}"
            price = row.get("viewerPriceCoins", row.get("priceCoins"))
            if number in owned or target in account.owned:
                status, message = "owned", "站点已记录本集解锁"
            elif row.get("available") is not True:
                status, message = "unavailable", "本集尚未上架或状态待确认"
            elif _coins(price) and price == 0:
                status, message = "free", "本集免费"
            elif number in grants or (
                series.get("memberOnly") is True and data.get("memberAccess") is True
            ):
                status, message = "owned", "当前账号已获播放权限"
            elif target in debited_targets:
                status, message = (
                    "unavailable",
                    "站点已有本集扣币流水，当前没有播放权益，请在站点核对账单",
                )
            elif series.get("memberOnly") is True:
                status, message = "unavailable", "本集要求会员权限"
            elif not _coins(price) or price > 100000:
                status, message = "unavailable", "无法核对本集金币价格"
            elif account.tickets:
                status, message = "unavailable", "账号有可用单集券，站点的单集接口无法指定只扣金币"
            else:
                status, message = "locked", "可使用金币解锁"
            episodes[url] = {
                "status": status,
                "price": price if _coins(price) else None,
                "message": message,
                "targetId": target,
                "ownedReceipt": status == "owned" and target in receipt_targets,
            }
        return account, slug, episodes

    async def quote(self, urls):
        try:
            async with asyncio.timeout(45), self._client() as client:
                account, slug, episodes = await self._read_quote(client, urls)
        except (httpx.HTTPError, TimeoutError):
            raise ValueError("无法读取 Lustflix 当前账号和金币报价，请稍后重试") from None
        return {
            "accountId": account.user,
            "balance": account.balance,
            "slug": slug,
            "episodes": episodes,
        }

    async def unlock(
        self, url, *, expected_account, max_price, request_key, cancelled=lambda: False
    ):
        if (
            not _coins(max_price)
            or not isinstance(request_key, str)
            or not _KEY.fullmatch(request_key)
        ):
            raise ValueError("单集支付额度或请求标识无效")
        if cancelled():
            return _result("cancelled", "已停止自动解锁", stop=True)
        sent = False
        declined = None
        try:
            async with asyncio.timeout(50), self._client() as client:
                before, slug, episodes = await self._read_quote(client, [url])
                ep = episodes[url]
                price = ep["price"]
                if before.user != expected_account:
                    return _result("account-changed", "账号已切换，请重新核对金币报价", stop=True)
                if ep["status"] != "locked":
                    return _result(ep["status"], ep["message"], price=price)
                if price > max_price:
                    return _result("price-changed", "当前单集价格超过原报价或剩余预算", price=price)
                if price > before.balance:
                    return _result("insufficient", "金币余额不足", price=price)
                if cancelled():
                    return _result("cancelled", "已停止自动解锁", stop=True)
                payload = {
                    "slug": slug,
                    "episode": int(ep["targetId"].rsplit(":", 1)[1]),
                    "expectedUserId": expected_account,
                    "requestKey": request_key,
                }
                acknowledged = False

                def dispatch():
                    nonlocal sent
                    if cancelled():
                        raise _CancelledBeforeDispatch()
                    sent = True

                try:
                    receipt = await self._json(
                        client, "/api/payments/unlock", payment=payload, on_dispatch=dispatch
                    )
                    acknowledged = _account(receipt).user == expected_account
                    if not acknowledged:
                        return _uncertain(price)
                except _CancelledBeforeDispatch:
                    return _result("cancelled", "已停止自动解锁", price=price, stop=True)
                except _StatusError as error:
                    declined = _declined(error, price)
                    if declined and declined["stop"]:
                        return declined
                except (httpx.HTTPError, ValueError):
                    if not sent:
                        raise
                    # The request may have charged. Only read to reconcile it.
                after, _, confirmed = await self._read_quote(client, [url])
                if after.user != expected_account:
                    if declined:
                        return _result(
                            "account-changed",
                            "付款已被拒绝，复核时账号已切换",
                            price=price,
                            stop=True,
                        )
                    return _uncertain(price)
                if declined:
                    if confirmed[url]["status"] == "owned":
                        return _result(
                            "owned", "本次付款已被拒绝，当前账号已获本集播放权限", price=price
                        )
                    return declined
                target = ep["targetId"]
                seen = {t["id"] for t in before.transactions if isinstance(t.get("id"), str)}
                debits = [
                    t
                    for t in after.transactions
                    if isinstance(t.get("id"), str)
                    and t["id"]
                    and t["id"] not in seen
                    and t.get("kind") == "unlock"
                    and t.get("episode_key") == target
                ]
                verified = confirmed[url]["status"] == "owned" and target in after.owned
                if verified and len(debits) == 1:
                    amount = debits[0].get("amount")
                    if (
                        type(amount) is int
                        and amount < 0
                        and debits[0].get("unlockCreditsSpent") == 0
                        and _attributable(debits[0], request_key, acknowledged)
                    ):
                        spent = -amount
                        msg = f"已解锁本集，站点流水扣除 {spent} 金币"
                        if spent > max_price:
                            msg += "，实际扣款超过复核价格，已停止后续支付"
                        return _result(
                            "purchased", msg, price=price, spent=spent, stop=spent > max_price
                        )
                return _uncertain(price)
        except (httpx.HTTPError, ValueError, TimeoutError):
            if declined:
                return declined
            if sent:
                return _uncertain(price)
            return _result("failed", "未能核对账号、价格或余额，未提交支付", stop=True)


def _uncertain(price):
    return _result(
        "uncertain", "单集支付结果待核对，已停止自动解锁", price=price, spent=None, stop=True
    )
