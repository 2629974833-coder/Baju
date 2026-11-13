import asyncio
import base64
import importlib
import socket
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from backend.network import NetworkPolicy


def proxy_class():
    assert (Path(__file__).parents[1] / "backend/egress.py").exists(), "EgressProxy is missing"
    return importlib.import_module("backend.egress").EgressProxy


def auth_header(proxy):
    parts = urlsplit(proxy.url)
    value = base64.b64encode(f"{parts.username}:{parts.password}".encode()).decode()
    return f"Proxy-Authorization: Basic {value}\r\n"


async def proxy_connection(proxy, connector=asyncio.open_connection):
    parts = urlsplit(proxy.url)
    return await connector(parts.hostname, parts.port)


@pytest.mark.asyncio
async def test_proxy_requires_auth_and_rejects_private_destinations():
    proxy = proxy_class()(NetworkPolicy())
    await proxy.start()
    try:
        reader, writer = await proxy_connection(proxy)
        writer.write(b"GET http://127.0.0.1/private HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        assert b"407" in await reader.readline()
        writer.close()
        await writer.wait_closed()
        reader, writer = await proxy_connection(proxy)
        writer.write(
            ("GET http://127.0.0.1/private HTTP/1.1\r\n" + auth_header(proxy) + "\r\n").encode()
        )
        await writer.drain()
        assert b"403" in await reader.readline()
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()


def tls_contexts(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture.example")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    key_path, cert_path = tmp_path / "key.pem", tmp_path / "cert.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.check_hostname = False
    client.verify_mode = ssl.CERT_NONE
    return server, client


@pytest.mark.asyncio
@pytest.mark.parametrize("tunnel", [False, True])
async def test_proxy_pins_validated_ip_for_http_and_tls_connect(tmp_path, monkeypatch, tunnel):
    requests, dials, resolutions = [], [], []
    server_tls, client_tls = tls_contexts(tmp_path)

    async def origin(reader, writer):
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\nfixture"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(origin, "127.0.0.1", 0, ssl=server_tls if tunnel else None)
    origin_port = server.sockets[0].getsockname()[1]
    real_connect = asyncio.open_connection
    loop = asyncio.get_running_loop()
    real_resolve = loop.getaddrinfo

    async def resolve(host, port, *args, **kwargs):
        if host in {"fixture.example", b"fixture.example"}:
            resolutions.append(host)
            address = "93.184.216.34" if len(resolutions) == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]
        return await real_resolve(host, port, *args, **kwargs)

    async def dial(host, port, *args, **kwargs):
        dials.append(host)
        assert host == "93.184.216.34", "The proxy must dial the validated IP, never the hostname"
        return await real_connect("127.0.0.1", origin_port)

    proxy = proxy_class()(NetworkPolicy())
    monkeypatch.setattr(loop, "getaddrinfo", resolve)
    monkeypatch.setattr("backend.egress.asyncio.open_connection", dial)
    await proxy.start()
    try:
        reader, writer = await proxy_connection(proxy, real_connect)
        if tunnel:
            writer.write(
                (
                    f"CONNECT fixture.example:{origin_port} HTTP/1.1\r\n"
                    + auth_header(proxy)
                    + "\r\n"
                ).encode()
            )
            await writer.drain()
            assert b"200" in await reader.readuntil(b"\r\n\r\n")
            await writer.start_tls(client_tls, server_hostname="fixture.example")
            writer.write(
                b"GET /protected?x=1 HTTP/1.1\r\nHost: fixture.example\r\nConnection: close\r\n\r\n"
            )
        else:
            writer.write(
                (
                    f"GET http://fixture.example:{origin_port}/protected?x=1 HTTP/1.1\r\nHost: fixture.example\r\n"
                    + auth_header(proxy)
                    + "\r\n"
                ).encode()
            )
        await writer.drain()
        response = await reader.read()
        assert response.endswith(b"fixture")
        assert len(resolutions) == 1 and dials == ["93.184.216.34"]
        assert requests[0].startswith(b"GET /protected?x=1 HTTP/1.1")
        assert b"Proxy-Authorization" not in requests[0]
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_proxy_bounds_headers_and_closes_active_tunnels():
    async def origin(reader, writer):
        await reader.read()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(origin, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    proxy = proxy_class()(NetworkPolicy({"127.0.0.1"}))
    await proxy.start()
    reader, writer = await proxy_connection(proxy)
    writer.write((f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n" + auth_header(proxy) + "\r\n").encode())
    await writer.drain()
    assert b"200" in await reader.readuntil(b"\r\n\r\n")
    await proxy.close()
    assert await asyncio.wait_for(reader.read(), 1) == b""
    writer.close()
    await writer.wait_closed()
    await proxy.start()
    try:
        reader, writer = await proxy_connection(proxy)
        writer.write(
            b"GET http://fixture.example/ HTTP/1.1\r\nX-Fill: " + b"a" * 40000 + b"\r\n\r\n"
        )
        await writer.drain()
        assert b"431" in await reader.readline()
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_fake_dns_requires_explicit_trust_and_never_allows_literal(monkeypatch):
    assert hasattr(NetworkPolicy, "resolve"), "NetworkPolicy.resolve is missing"
    loop = asyncio.get_running_loop()

    async def resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("198.18.0.9", port))]

    monkeypatch.setattr(loop, "getaddrinfo", resolve)
    with pytest.raises(ValueError):
        await NetworkPolicy().resolve("https://fixture.example/media")
    policy = NetworkPolicy(trust_fake_dns=True)
    url, addresses = await policy.resolve("https://fixture.example/media")
    assert url == "https://fixture.example/media" and addresses == ["198.18.0.9"]
    with pytest.raises(ValueError):
        await policy.resolve("https://198.18.0.9/media")


@pytest.mark.asyncio
async def test_proxy_preserves_http_post_body_and_websocket_upgrade():
    requests = []

    async def origin(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        requests.append(headers)
        if b"upgrade: websocket" in headers.lower():
            writer.write(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
            )
            await writer.drain()
            writer.write(await reader.readexactly(5))
        else:
            assert await reader.readexactly(5) == b"hello"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(origin, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    proxy = proxy_class()(NetworkPolicy({"127.0.0.1"}))
    await proxy.start()
    try:
        reader, writer = await proxy_connection(proxy)
        writer.write(
            (
                f"POST http://127.0.0.1:{port}/submit HTTP/1.1\r\nContent-Length: 5\r\n"
                + auth_header(proxy)
                + "\r\nhello"
            ).encode()
        )
        await writer.drain()
        assert (await reader.read()).endswith(b"OK")
        writer.close()
        await writer.wait_closed()
        reader, writer = await proxy_connection(proxy)
        writer.write(
            (
                f"GET ws://127.0.0.1:{port}/socket HTTP/1.1\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
                + auth_header(proxy)
                + "\r\n"
            ).encode()
        )
        await writer.drain()
        assert b"101" in await reader.readuntil(b"\r\n\r\n")
        writer.write(b"hello")
        await writer.drain()
        assert await reader.readexactly(5) == b"hello"
        assert all(b"proxy-authorization" not in request.lower() for request in requests)
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_tunnel_idle_timeout_tracks_both_directions(monkeypatch):
    proxy_type = proxy_class()
    monkeypatch.setattr("backend.egress._IDLE_TIMEOUT", 0.05, raising=False)

    async def origin(reader, writer):
        for _ in range(6):
            writer.write(b"data")
            await writer.drain()
            await asyncio.sleep(0.03)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(origin, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    proxy = proxy_type(NetworkPolicy({"127.0.0.1"}))
    await proxy.start()
    try:
        reader, writer = await proxy_connection(proxy)
        writer.write(
            (f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n" + auth_header(proxy) + "\r\n").encode()
        )
        await writer.drain()
        assert b"200" in await reader.readuntil(b"\r\n\r\n")
        assert await reader.read() == b"data" * 6
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_address,expected", [("1.1.1.1", 200), ("169.254.169.254", 403)])
async def test_changed_route_refreshes_pinned_address_before_forwarding(monkeypatch, new_address, expected):
    loop = asyncio.get_running_loop()
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    addresses = iter(("8.8.4.4", new_address))
    original = asyncio.open_connection
    dials = []

    async def dns(host, port, **kwargs):
        assert host == "moving.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (next(addresses), port))]

    async def dial(address, port, **kwargs):
        if "family" in kwargs:
            dials.append(address)
            if address == "8.8.4.4":
                raise ConnectionRefusedError()
            assert address == "1.1.1.1"
            return await original("127.0.0.1", port)
        return await original(address, port, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", dns)
    monkeypatch.setattr(asyncio, "open_connection", dial)
    proxy = proxy_class()(NetworkPolicy())
    await proxy.start()
    try:
        reader, writer = await proxy_connection(proxy, original)
        writer.write((f"CONNECT moving.example:{port} HTTP/1.1\r\n" + auth_header(proxy) + "\r\n").encode())
        await writer.drain()
        assert (await reader.readline()).split()[1] == str(expected).encode()
        assert dials == (["8.8.4.4", "1.1.1.1"] if expected == 200 else ["8.8.4.4"])
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("succeeds", [True, False])
async def test_temporary_dns_failure_is_not_permission_denial(monkeypatch, succeeds):
    loop = asyncio.get_running_loop()
    calls = 0

    async def dns(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1 or not succeeds:
            raise socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.1.1.1", 443))]

    monkeypatch.setattr(loop, "getaddrinfo", dns)
    policy = NetworkPolicy()
    if succeeds:
        assert await policy.validate("https://moving.example/") == "https://moving.example/"
    else:
        proxy = proxy_class()(policy)
        await proxy.start()
        try:
            reader, writer = await proxy_connection(proxy)
            writer.write(("CONNECT moving.example:443 HTTP/1.1\r\n" + auth_header(proxy) + "\r\n").encode())
            await writer.drain()
            assert (await reader.readline()).split()[1] == b"502"
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.close()
    assert calls == 2


@pytest.mark.asyncio
async def test_forbidden_dns_answer_cannot_mask_next_valid_route(monkeypatch):
    ips = iter(("169.254.169.254", "1.1.1.1"))

    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (next(ips), 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    policy = NetworkPolicy()
    with pytest.raises(ValueError, match="禁止访问"):
        await policy.resolve("https://moving.example")
    _, ready = await policy.resolve("https://moving.example")
    assert ready == ["1.1.1.1"]
