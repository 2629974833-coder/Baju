"""Renew saved Chunlou accounts at the official issuer before creating a read-only scan."""

import asyncio
import base64
import json
import math
import re
import time

import httpx

from backend import chunlou


class ChunlouRelinkRequired(ValueError):
    """The saved site session needs user action before any episode can be checked."""


def registered_session(value, *, allow_expired=False):
    """Validate the exported session shape before checking account identity or content access."""
    try:
        session = json.loads(value)
        token = session["access_token"]
        if not isinstance(token, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token
        ):
            return None
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        user, refresh = session["user"], session["refresh_token"]
        expires = min(float(claims["exp"]), float(session["expires_at"]))
        if not (
            isinstance(user["id"], str)
            and user["id"]
            and claims["sub"] == user["id"]
            and isinstance(refresh, str)
            and refresh.strip()
            and all(math.isfinite(float(v)) for v in (claims["exp"], session["expires_at"]))
            and (allow_expired or expires > time.time())
            and not claims.get("is_anonymous")
            and not user.get("is_anonymous")
            and claims.get("iss") == chunlou.AUTH_ISSUER
            and claims.get("role") == "authenticated"
        ):
            return None
        return {
            "access_token": token,
            "refresh_token": refresh,
            "token_type": "bearer",
            "expires_at": int(expires),
            "expires_in": max(1, int(expires - time.time())),
            "user": {"id": user["id"], "is_anonymous": False},
        }
    except (
        AttributeError,
        KeyError,
        IndexError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
    ):
        return None


async def finish_before_cancel(operation):
    """A cancelled request must leave the browser and its guardian in a known state."""
    task = asyncio.create_task(operation)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    result = task.result()
    if interrupted:
        raise asyncio.CancelledError
    return result


# The frontend Supabase client starts its own refresh shortly before expiry.
# Prepare enough lifetime for a scan before enforcing its read-only network rules.
_REFRESH_AHEAD = 120
_RELINK = "春楼账号授权已失效，请刷新已登录的春楼页面并在拾帧重新关联"
_TEMPORARY = "暂时无法续期春楼账号，请检查网络后稍后重试"


def _account(state):
    if not state:
        return None
    for origin in state.get("origins", []):
        if origin.get("origin") != "https://chunlou.tv":
            continue
        entries = origin.get("localStorage", [])
        for key in (chunlou.AUTH_STORAGE_KEY, chunlou.LEGACY_AUTH_STORAGE_KEY):
            for item in entries:
                if item.get("name") == key:
                    account = registered_session(item.get("value"), allow_expired=True)
                    return (item, account) if account else None
    return None


async def _renew(policy, saved):
    try:
        async with asyncio.timeout(25), chunlou._client(policy) as client:
            try:
                origin, headers = await chunlou._directory_origin(client, policy)
            except (ValueError, UnicodeError):
                raise ValueError("无法核对春楼身份服务配置，请稍后重试") from None
            if origin != f"https://{chunlou.AUTH_PROJECT}.supabase.co/rest/v1/":
                raise ValueError("春楼身份服务配置已更改，需更新拾帧后重试")
            url = chunlou.AUTH_ISSUER + "/token?grant_type=refresh_token"
            await policy.validate(url)
            client.cookies.clear()
            async with client.stream(
                "POST", url, headers=headers, json={"refresh_token": saved["refresh_token"]}
            ) as res:
                if res.status_code in {400, 401, 403, 422}:
                    raise ChunlouRelinkRequired(_RELINK)
                if res.status_code != 200:
                    raise ValueError(_TEMPORARY)
                body = bytearray()
                async for chunk in res.aiter_bytes(16384):
                    if len(body) + len(chunk) > 65536:
                        raise ValueError(_TEMPORARY)
                    body.extend(chunk)
        renewed = registered_session(body)
        if not renewed or renewed["user"]["id"] != saved["user"]["id"]:
            raise ChunlouRelinkRequired("春楼未能确认同一账号，请在已登录的春楼页面重新关联")
        return json.dumps(renewed, separators=(",", ":"))
    except (httpx.HTTPError, TimeoutError, OSError):
        raise ValueError(_TEMPORARY) from None


async def prepare_scan_state(policy, site_url, working, read_saved, persist):
    """The caller holds its per-site lock while a refresh grant rotates and is written."""
    if chunlou._canonical_url(site_url) is None:
        return working
    current = _account(working)
    if not current or current[1]["expires_at"] > time.time() + _REFRESH_AHEAD:
        return working
    archived = read_saved()
    saved = _account(archived)
    if not saved or saved[1]["user"]["id"] != current[1]["user"]["id"]:
        # A different account in a live authorization window must be saved explicitly.
        raise ChunlouRelinkRequired("请在春楼授权窗口完成登录并保存当前账号，然后重新扫描")
    if saved[1]["expires_at"] <= time.time() + _REFRESH_AHEAD:

        async def rotate_and_persist():
            value = await _renew(policy, saved[1])
            saved[0]["value"] = value
            persist(archived)

        await finish_before_cancel(rotate_and_persist())
    current[0]["value"] = saved[0]["value"]
    return working
