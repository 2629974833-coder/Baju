"""Outbound HTTP destinations for a personal, single-admin collector."""

import asyncio
import ipaddress
import socket
import time
from urllib.parse import urlsplit, urlunsplit


class NetworkResolutionError(ValueError):
    """A name could not be resolved, rather than being forbidden by policy."""


def is_transient_proxy_error(exc):
    # httpcore reports the HTTP CONNECT reply as "<status> <reason>".
    return str(exc).partition(" ")[0] in {"502", "503", "504"}


async def _lookup(host, port):
    for attempt in range(2):
        try:
            records = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
                timeout=4,
            )
            addresses = list(dict.fromkeys(ipaddress.ip_address(r[4][0]) for r in records))
            if not addresses:
                raise NetworkResolutionError("无法解析目标站点地址")
            return addresses
        except (OSError, TimeoutError) as exc:
            transient = isinstance(exc, TimeoutError) or getattr(exc, "errno", None) == socket.EAI_AGAIN
            if not transient or attempt:
                raise NetworkResolutionError("无法解析目标站点地址") from exc
            await asyncio.sleep(0.15)


class NetworkPolicy:
    def __init__(self, allow_private_hosts=None, trust_fake_dns=False):
        self.allow_private_hosts = {h.lower() for h in (allow_private_hosts or set())}
        self.trust_fake_dns = bool(trust_fake_dns)
        self.proxy_url = None
        self._cache = {}

    def invalidate_dns(self, url: str) -> bool:
        host = urlsplit(url).hostname
        return bool(host and self._cache.pop(host.rstrip(".").lower(), None))

    async def validate(self, url: str) -> str:
        normalized, _ = await self.resolve(url)
        return normalized

    async def resolve(self, url: str) -> tuple[str, list[str]]:
        if not isinstance(url, str) or len(url) > 16384 or any(ord(c) < 32 for c in url):
            raise ValueError("无效的网址")
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ValueError("网址或端口格式不正确") from exc
        if (
            parsed.scheme not in ("http", "https")
            or not host
            or parsed.username
            or parsed.password
            or port == 0
        ):
            raise ValueError("仅支持不含账号密码的 HTTP / HTTPS 网址")
        host = host.rstrip(".").lower()
        allow_private = host in self.allow_private_hosts
        if not allow_private and (
            host == "localhost" or host.endswith((".localhost", ".local", ".internal"))
        ):
            raise ValueError("禁止访问本机或内部网络地址")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        cache_result = False
        if literal is not None:
            addresses = [literal]
        else:
            cached = self._cache.get(host)
            if cached and cached[0] > time.monotonic():
                addresses = cached[1]
            else:
                addresses = await _lookup(host, port or (443 if parsed.scheme == "https" else 80))
                cache_result = True
        fake_range = ipaddress.ip_network("198.18.0.0/15")
        if not addresses or any(
            not address.is_global
            and not allow_private
            and not (self.trust_fake_dns and literal is None and address in fake_range)
            for address in addresses
        ):
            raise ValueError("禁止访问私网、保留地址或云主机元数据服务")
        if cache_result:
            self._cache[host] = (time.monotonic() + 20, addresses)
        normalized = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, parsed.fragment)
        )
        return normalized, [str(address) for address in addresses]
