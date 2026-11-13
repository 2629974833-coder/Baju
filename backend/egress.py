"""Authenticated loopback proxy that connects only to already validated numeric IPs."""

import asyncio
import base64
import contextlib
import ipaddress
import re
import secrets
import socket
import time
from urllib.parse import urlsplit

from backend.network import NetworkResolutionError

_HEADER_LIMIT = 32 * 1024
_BODY_LIMIT = 64 * 1024 * 1024
_IDLE_TIMEOUT = 90
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class _ProxyError(Exception):
    def __init__(self, status):
        self.status = status


class EgressProxy:
    def __init__(self, policy):
        self.policy = policy
        self._username, self._password = secrets.token_hex(8), secrets.token_urlsafe(32)
        self._authorization = (
            "Basic " + base64.b64encode(f"{self._username}:{self._password}".encode()).decode()
        )
        self._server = None
        self._closing = False
        self._tasks = set()
        self._writers = set()

    @property
    def url(self):
        if self._server is None:
            raise RuntimeError("出口代理尚未启动")
        port = self._server.sockets[0].getsockname()[1]
        return f"http://{self._username}:{self._password}@127.0.0.1:{port}"

    async def start(self):
        if self._server is None:
            self._closing = False
            self._server = await asyncio.start_server(
                self._handle, "127.0.0.1", 0, limit=_HEADER_LIMIT
            )

    async def close(self):
        self._closing = True
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for writer in list(self._writers):
            writer.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            await server.wait_closed()

    async def _handle(self, reader, writer):
        if self._closing:
            writer.close()
            return
        task = asyncio.current_task()
        self._tasks.add(task)
        self._writers.add(writer)
        upstream = None
        committed = False
        try:
            method, target, headers = await self._read_request(reader)
            if not secrets.compare_digest(
                headers.get("proxy-authorization", "").encode("latin-1"),
                self._authorization.encode(),
            ):
                raise _ProxyError(407)
            if method == "CONNECT":
                parsed = urlsplit("//" + target)
                if (
                    not parsed.hostname
                    or not parsed.port
                    or parsed.path
                    or parsed.query
                    or parsed.fragment
                ):
                    raise _ProxyError(400)
                url = "https://" + target + "/"
            else:
                parsed = urlsplit(target)
                if parsed.scheme not in {"http", "ws"} or not parsed.hostname or parsed.fragment:
                    raise _ProxyError(400)
                url = target.replace("ws://", "http://", 1)
            url, addresses = await self._resolve(url)
            parsed = urlsplit(url)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            try:
                upstream_reader, upstream = await self._connect(addresses, port)
            except _ProxyError:
                # No request bytes have been forwarded. A proxy switch can invalidate
                # cached DNS; retry only when a newly validated address is available.
                invalidate = getattr(self.policy, "invalidate_dns", None)
                if not invalidate or not invalidate(url):
                    raise
                _, refreshed = await self._resolve(url)
                fresh = [address for address in refreshed if address not in addresses[:4]]
                if not fresh:
                    raise
                upstream_reader, upstream = await self._connect(fresh, port)
            self._writers.add(upstream)
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                committed = True
                await self._tunnel(reader, writer, upstream_reader, upstream)
            else:
                upgrade = await self._forward_request(method, parsed, headers, reader, upstream)
                committed = True
                if upgrade:
                    await self._tunnel(reader, writer, upstream_reader, upstream)
                else:
                    await self._pipe(upstream_reader, writer)
        except _ProxyError as error:
            if not committed:
                await self._error(writer, error.status)
        except (ValueError, UnicodeError):
            if not committed:
                await self._error(writer, 400)
        except TimeoutError:
            if not committed:
                await self._error(writer, 504)
        except (OSError, asyncio.IncompleteReadError):
            if not committed:
                await self._error(writer, 502)
        finally:
            for connection in (upstream, writer):
                if connection is not None:
                    self._writers.discard(connection)
                    connection.close()
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await asyncio.wait_for(connection.wait_closed(), timeout=2)
            self._tasks.discard(task)

    async def _resolve(self, url):
        try:
            return await self.policy.resolve(url)
        except NetworkResolutionError:
            raise _ProxyError(502) from None
        except ValueError:
            raise _ProxyError(403) from None

    @staticmethod
    async def _read_request(reader):
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        except asyncio.LimitOverrunError:
            raise _ProxyError(431) from None
        if len(raw) > _HEADER_LIMIT:
            raise _ProxyError(431)
        lines = raw.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if (
            len(parts) != 3
            or parts[2] not in {"HTTP/1.0", "HTTP/1.1"}
            or not _TOKEN.fullmatch(parts[0])
        ):
            raise _ProxyError(400)
        headers = {}
        for line in lines[1:-2]:
            name, separator, value = line.partition(":")
            if (
                not separator
                or not _TOKEN.fullmatch(name)
                or any(ord(c) < 32 and c != "\t" for c in value)
            ):
                raise _ProxyError(400)
            name = name.lower()
            if name in headers:
                raise _ProxyError(400)
            headers[name] = value.strip()
        return parts[0], parts[1], headers

    @staticmethod
    async def _connect(addresses, port):
        for address in addresses[:4]:
            # Numeric conversion also prevents a custom policy from causing a second DNS lookup.
            ip = ipaddress.ip_address(address)
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(
                        str(ip), port, family=socket.AF_INET6 if ip.version == 6 else socket.AF_INET
                    ),
                    timeout=10,
                )
            except (OSError, TimeoutError):
                continue
        raise _ProxyError(502)

    @staticmethod
    async def _forward_request(method, target, headers, reader, upstream):
        # HTTPS uses CONNECT and supports arbitrary HTTP framing inside its pinned TLS tunnel.
        # Plain HTTP intentionally accepts one bounded, Content-Length-delimited request only.
        if "transfer-encoding" in headers or "expect" in headers:
            raise _ProxyError(400)
        length = headers.get("content-length", "0")
        if not length.isascii() or not length.isdecimal() or len(length) > 10:
            raise _ProxyError(400)
        length = int(length)
        if length > _BODY_LIMIT:
            raise _ProxyError(413)
        upgrade = headers.get("upgrade", "").lower() == "websocket"
        if upgrade and (method != "GET" or length):
            raise _ProxyError(400)
        ignored = {
            "host",
            "connection",
            "proxy-connection",
            "proxy-authorization",
            "keep-alive",
            "te",
            "trailer",
            "upgrade",
        }
        ignored.update(value.strip().lower() for value in headers.get("connection", "").split(","))
        ignored.add("content-length")
        if upgrade:
            ignored.discard("upgrade")
        path = target.path or "/"
        if target.query:
            path += "?" + target.query
        connection = "Upgrade" if upgrade else "close"
        lines = [f"{method} {path} HTTP/1.1", f"Host: {target.netloc}", f"Connection: {connection}"]
        if "content-length" in headers:
            lines.append(f"Content-Length: {length}")
        lines.extend(f"{name}: {value}" for name, value in headers.items() if name not in ignored)
        upstream.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        await upstream.drain()
        remaining = length
        while remaining:
            block = await asyncio.wait_for(reader.readexactly(min(65536, remaining)), timeout=30)
            upstream.write(block)
            await asyncio.wait_for(upstream.drain(), timeout=30)
            remaining -= len(block)
        return upgrade

    @staticmethod
    async def _pipe(reader, writer, activity=None):
        while True:
            try:
                block = await asyncio.wait_for(reader.read(65536), timeout=_IDLE_TIMEOUT)
            except TimeoutError:
                if activity is not None and time.monotonic() - activity[0] < _IDLE_TIMEOUT:
                    continue
                raise
            if not block:
                return
            if activity is not None:
                activity[0] = time.monotonic()
            writer.write(block)
            await asyncio.wait_for(writer.drain(), timeout=30)

    async def _tunnel(self, reader, writer, upstream_reader, upstream):
        activity = [time.monotonic()]
        pipes = [
            asyncio.create_task(self._pipe(reader, upstream, activity)),
            asyncio.create_task(self._pipe(upstream_reader, writer, activity)),
        ]
        try:
            done, _ = await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in pipes:
                task.cancel()
            await asyncio.gather(*pipes, return_exceptions=True)

    @staticmethod
    async def _error(writer, status):
        names = {
            400: "Bad Request",
            403: "Forbidden",
            407: "Proxy Authentication Required",
            413: "Content Too Large",
            431: "Request Header Fields Too Large",
            502: "Bad Gateway",
            504: "Gateway Timeout",
        }
        challenge = (
            'Proxy-Authenticate: Basic realm="Framekeep egress"\r\n' if status == 407 else ""
        )
        response = f"HTTP/1.1 {status} {names[status]}\r\n{challenge}Content-Length: 0\r\nConnection: close\r\n\r\n"
        with contextlib.suppress(OSError):
            writer.write(response.encode())
            await asyncio.wait_for(writer.drain(), timeout=2)
