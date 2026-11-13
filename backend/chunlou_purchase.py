"""Account-bound, single-episode coin purchases using Chunlou's own wallet contract."""

import asyncio
import json
import time
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import httpx

from backend import chunlou
from backend.session_refresh import _account as scan_account

_REST = f"https://{chunlou.AUTH_PROJECT}.supabase.co/rest/v1/"
_EP_SELECT = (
    "id,drama_id,seq,kind,status,coin_price,"
    "drama:dramas!inner(id,status,free_episodes,vip_only,owner_id,house_owned)"
)
_LOGIN = "请在来源中保存已登录的春楼账号后重试"
_UNCERTAIN = "春楼购买结果尚未核实，已停止本批购买"
_ERRORS = {
    "NOT_ENOUGH_COINS": ("insufficient", "春楼金币余额不足"),
    "PRICE_CHANGED": ("price-changed", "春楼单集价格已变化，请重新检查"),
    "EPISODE_IS_FREE": ("free", "春楼已确认本集免费"),
    "VIP_ONLY": ("unavailable", "本集需要春楼 VIP"),
    "MIN_LEVEL_REQUIRED": ("unavailable", "当前账号等级未达到本集要求"),
    "EPISODE_NOT_FOUND": ("unavailable", "春楼未找到此集"),
    "BRANCH_ENTRY_MANUAL_ONLY": ("unavailable", "本集需要在春楼手动选择分支"),
    "NEW_ACCOUNT_UNLOCK_PAUSED": ("unavailable", "站点已暂停该账号的金币解锁"),
    "BANNED": ("unavailable", "该账号当前不能进行金币解锁"),
    "NOT_LOGGED_IN": ("failed", _LOGIN),
    "NOT_AUTHENTICATED": ("failed", _LOGIN),
}


def _future(value):
    if value is None:
        return False
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.tzinfo is not None and stamp.timestamp() > time.time()
    except (AttributeError, ValueError, OverflowError):
        raise ValueError("春楼有效期信息不完整，无法确认本集价格") from None


def _ratio(value):
    try:
        ratio = Decimal(str(value))
        if not ratio.is_finite():
            raise InvalidOperation()
    except (ValueError, InvalidOperation):
        raise ValueError("春楼未返回有效的账号价格系数") from None
    if ratio <= 0:
        return Decimal(1)
    return min(Decimal(2), max(Decimal("0.1"), ratio))


def _price(base, pricing, sale, drama_id):
    # The player's effPrice first rounds the ratio to millionths, then floors coins.
    if not isinstance(pricing, dict) or "ratio" not in pricing:
        raise ValueError("春楼账号价格信息不完整")
    ratio = _ratio(pricing["ratio"])
    bucket = pricing.get("pbucket")
    if (
        isinstance(sale, dict)
        and sale.get("enabled") is True
        and sale.get("drama_id") == drama_id
        and _future(sale.get("ends_at"))
        and (bucket is None or (type(bucket) in (int, float) and bucket < 50))
    ):
        ratio = min(ratio, Decimal(1), _ratio(sale.get("ratio")))
    millionths = int((ratio * 1_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return max(1, base * millionths // 1_000_000)


def _line(identifier, episode, owned, profile, pricing, sale):
    def line(status, message, price=0):
        return {"targetId": identifier, "status": status, "price": price, "message": message}

    if not episode or episode.get("status") != "published" or episode.get("kind") != "main":
        return line("unavailable", "本集未发布或不属于主线选集")
    if identifier in owned:
        return {**line("owned", "春楼已确认本账号有此集的解锁记录"), "ownedReceipt": True}
    drama = episode.get("drama")
    if (
        not isinstance(drama, dict)
        or not drama.get("id")
        or drama["id"] != episode.get("drama_id")
        or drama.get("status") not in {"published", "archived"}
        or type(drama.get("vip_only")) is not bool
    ):
        return line("unavailable", "春楼剧集信息不完整")
    if drama.get("owner_id") == profile["user_id"]:
        return line("free", "本账号为该剧创作者")
    vip = _future(profile.get("vip_until"))
    if drama["vip_only"] and not vip:
        return line("unavailable", "本集需要春楼 VIP")
    seq, free, base = episode.get("seq"), drama.get("free_episodes"), episode.get("coin_price")
    if type(seq) is not int or seq < 1 or type(free) is not int or free < 0:
        return line("unavailable", "春楼未确认本集的免费范围")
    if type(base) is not int or base < 0:
        return line("unavailable", "春楼未确认本集的单价")
    if seq <= free or base == 0:
        return line("free", "本集在站点免费范围内")
    extra = 0 if profile.get("vip_free_extra") is None else profile["vip_free_extra"]
    if vip and (not drama.get("owner_id") or drama.get("house_owned") is True):
        if type(extra) is not int:
            return line("unavailable", "春楼未确认账号的 VIP 免费范围")
        if seq <= free + extra:
            return line("free", "当前 VIP 权益已覆盖本集")
    return line("locked", "可使用春楼金币解锁本集", _price(base, pricing, sale, drama["id"]))


def _result(status, message, *, spent=0, stop=False):
    return {"status": status, "message": message, "spent": spent, "stop": stop}


class ChunlouPurchases:
    def __init__(self, site, browser, policy):
        if not chunlou._canonical_url(site.get("url")):
            raise ValueError("当前来源不是春楼")
        self.site, self.browser, self.policy = site, browser, policy
        self._public_headers = None

    async def _account(self):
        context = await self.browser.create_scan_context(self.site)
        try:
            account = scan_account(await context.storage_state())
        finally:
            await context.close()
        if not account or account[1]["expires_at"] <= time.time():
            raise ValueError(_LOGIN)
        session = account[1]
        return session["user"]["id"], session["access_token"]

    async def _headers(self, client, user_id, token):
        if not isinstance(user_id, str) or not chunlou._UUID.fullmatch(user_id):
            raise ValueError(_LOGIN)
        if self._public_headers is None:
            origin, headers = await chunlou._directory_origin(client, self.policy)
            if origin != _REST:
                raise ValueError("春楼账号服务的地址发生变化")
            self._public_headers = headers
        headers = {**self._public_headers, "Authorization": "Bearer " + token}
        identity = await self._get(client, chunlou.AUTH_ISSUER + "/user", headers)
        if (
            not isinstance(identity, dict)
            or identity.get("id") != user_id
            or identity.get("is_anonymous")
        ):
            raise ValueError("无法确认当前春楼账号")
        return headers

    async def _get(self, client, url, headers, **params):
        data = await chunlou._get(client, self.policy, url, headers=headers, params=params)
        try:
            return json.loads(data)
        except (ValueError, RecursionError):
            raise ValueError("春楼返回的数据格式无效") from None

    async def _receipts(self, client, headers, user_id, ids):
        rows = await self._get(
            client,
            _REST + "unlocks",
            headers,
            select="episode_id",
            user_id="eq." + user_id,
            episode_id="in.(" + ",".join(ids) + ")",
        )
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or row.get("episode_id") not in ids for row in rows
        ):
            raise ValueError("春楼返回的解锁记录与选集不符")
        return {row["episode_id"] for row in rows}

    async def _debits(self, client, headers, user_id, identifier):
        # The official wallet uses reason=unlock and ref_id=episode_id to associate
        # each coin_logs delta with an episode. Other wallet debits are irrelevant.
        rows = await self._get(
            client,
            _REST + "coin_logs",
            headers,
            select="id,delta,reason,ref_id",
            user_id="eq." + user_id,
            ref_id="eq." + identifier,
            reason="eq.unlock",
            delta="lt.0",
            limit="2",
        )
        if not isinstance(rows, list) or any(
            not isinstance(row, dict)
            or not row.get("id")
            or row.get("ref_id") != identifier
            or row.get("reason") != "unlock"
            or type(row.get("delta")) is not int
            for row in rows
        ):
            raise ValueError("春楼未返回可核实的单集扣款明细")
        return [-row["delta"] for row in rows if row["delta"] < 0]

    async def _read_quote(self, client, headers, user_id, selected):
        profiles, pricing, offers = await asyncio.gather(
            self._get(
                client,
                _REST + "profiles",
                headers,
                select="user_id,coins,vip_until,vip_free_extra",
                user_id="eq." + user_id,
            ),
            self._get(client, _REST + "rpc/my_price_ratio", headers),
            self._get(
                client, _REST + "app_settings", headers, select="key,value", key="eq.flash_sale"
            ),
        )
        if (
            not isinstance(profiles, list)
            or len(profiles) != 1
            or not isinstance(profiles[0], dict)
        ):
            raise ValueError("春楼未返回当前账号的钱包")
        profile = profiles[0]
        if (
            profile.get("user_id") != user_id
            or type(profile.get("coins")) is not int
            or profile["coins"] < 0
        ):
            raise ValueError("春楼未确认当前账号的金币余额")
        if (
            not isinstance(offers, list)
            or len(offers) > 1
            or any(not isinstance(row, dict) or row.get("key") != "flash_sale" for row in offers)
        ):
            raise ValueError("春楼促销价格信息不完整")
        sale = offers[0].get("value") if offers else None
        rows, owned = {}, set()
        ids = list(dict.fromkeys(selected.values()))
        for start in range(0, len(ids), 64):
            chunk = ids[start : start + 64]
            entries, receipts = await asyncio.gather(
                self._get(
                    client,
                    _REST + "episodes",
                    headers,
                    select=_EP_SELECT,
                    status="eq.published",
                    id="in.(" + ",".join(chunk) + ")",
                ),
                self._receipts(client, headers, user_id, chunk),
            )
            if not isinstance(entries, list):
                raise ValueError("春楼单集目录格式无效")
            for entry in entries:
                identifier = entry.get("id") if isinstance(entry, dict) else None
                if identifier not in chunk or identifier in rows:
                    raise ValueError("春楼返回的单集与选择不一致")
                rows[identifier] = entry
            owned.update(receipts)
        return {
            "accountId": user_id,
            "balance": profile["coins"],
            "episodes": {
                url: _line(identifier, rows.get(identifier), owned, profile, pricing, sale)
                for url, identifier in selected.items()
            },
        }

    @staticmethod
    def _selected(urls):
        result = {url: chunlou.episode_route(url) for url in urls}
        if not result or len(result) > 1000 or not all(result.values()):
            raise ValueError("金币解锁仅支持已列入春楼目录的单集")
        return result

    async def quote(self, urls):
        selected = self._selected(urls)
        try:
            async with asyncio.timeout(60), chunlou._client(self.policy) as client:
                user_id, token = await self._account()
                headers = await self._headers(client, user_id, token)
                return await self._read_quote(client, headers, user_id, selected)
        except (httpx.HTTPError, TimeoutError, OSError):
            raise ValueError("暂时无法读取春楼钱包或单集价格") from None

    async def unlock(self, url, *, expected_account, max_price, request_key, cancelled):
        # This RPC guards both entitlement and expected price. It has no order-key
        # parameter; the queue journals request_key before invoking us and owns retries.
        selected = self._selected([url])
        identifier = selected[url]
        sent = False
        try:
            async with asyncio.timeout(60), chunlou._client(self.policy) as client:
                user_id, token = await self._account()
                if user_id != expected_account:
                    return _result("failed", "春楼账号发生变化，已停止购买", stop=True)
                headers = await self._headers(client, user_id, token)
                quote = await self._read_quote(client, headers, user_id, selected)
                line = quote["episodes"][url]
                if line["status"] != "locked":
                    return _result(line["status"], line["message"])
                price = line["price"]
                if type(max_price) is not int or price > max_price:
                    return _result("price-changed", "春楼单集价格超过本次允许金额")
                if price > quote["balance"]:
                    return _result("insufficient", "春楼金币余额不足")
                if await self._debits(client, headers, user_id, identifier):
                    if identifier in await self._receipts(client, headers, user_id, [identifier]):
                        return _result("owned", "春楼已确认该集此前已解锁")
                    return _result(
                        "unavailable",
                        "春楼已有本集扣款记录，但尚无对应解锁权益，请先核对站点账单",
                        stop=True,
                    )
                endpoint = _REST + "rpc/unlock_episode"
                await self.policy.validate(endpoint)
                if cancelled():
                    return _result("failed", "已取消购买", stop=True)
                client.cookies.clear()
                sent = True
                try:
                    async with client.stream(
                        "POST",
                        endpoint,
                        headers=headers,
                        json={"p_episode_id": identifier, "p_expected_price": price},
                    ) as response:
                        status = response.status_code
                        raw = bytearray()
                        async for block in response.aiter_bytes(16384):
                            if len(raw) + len(block) > 65536:
                                break
                            raw.extend(block)
                except (httpx.HTTPError, TimeoutError, OSError):
                    status, raw = 0, b""
                if 400 <= status < 500:
                    try:
                        problem = json.loads(raw)
                        message = problem.get("message", "") if isinstance(problem, dict) else ""
                    except (ValueError, RecursionError):
                        message = ""
                    for code, (state, label) in _ERRORS.items():
                        if isinstance(message, str) and code in message:
                            return _result(
                                state,
                                label,
                                stop=state == "failed"
                                or code in {"BANNED", "NEW_ACCOUNT_UNLOCK_PAUSED"},
                            )
                    if isinstance(message, str) and "ALREADY_UNLOCKED" in message:
                        if identifier in await self._receipts(
                            client, headers, user_id, [identifier]
                        ):
                            return _result("owned", "春楼已确认该集此前已解锁")
                receipts = await self._receipts(client, headers, user_id, [identifier])
                if 200 <= status < 300 and identifier in receipts:
                    debits = await self._debits(client, headers, user_id, identifier)
                    if len(debits) == 1:
                        charged = debits[0]
                        return _result(
                            "purchased",
                            f"已用 {charged} 金币解锁本集",
                            spent=charged,
                            stop=charged > price,
                        )
                message = (
                    "本账号已有该集收据，但本次实际扣款未确认"
                    if identifier in receipts
                    else _UNCERTAIN
                )
                return _result("uncertain", message, spent=None, stop=True)
        except (httpx.HTTPError, TimeoutError, OSError, ValueError):
            if sent:
                return _result("uncertain", _UNCERTAIN, spent=None, stop=True)
            return _result("failed", "春楼账号、余额或单集价格暂时无法核实", stop=True)
