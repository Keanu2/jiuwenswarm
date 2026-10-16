"""Local-only regressions for inference proxy policy, framing, and streaming."""

import asyncio
import contextlib
import socket
from unittest.mock import AsyncMock, Mock

import h11
import httpx
import pytest
from fastapi import FastAPI

from jiuwenbox.models.policy import InferencePrivacyProxyPolicy, ProxyRouteEntry
from jiuwenbox.proxy.inference_privacy_proxy import (
    InferencePrivacyProxy, InferencePrivacyProxyConfig, ProxyRoute,
)
from jiuwenbox.proxy.inference_privacy_proxy_manager import InferencePrivacyProxyManager
from jiuwenbox.proxy.target_security import ProxyPolicyError, TargetPolicy, check_address, origin
from jiuwenbox.server.routes import proxy as routes


@pytest.fixture(autouse=True)
def no_external_credentials(monkeypatch):
    monkeypatch.delenv("JIUWENBOX_PROXY_TOKEN", raising=False)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com", "http://user:pass@example.com",
    "http://example.com:0", "http://example.com:65536", "http://example.com#fragment",
    "http://example.com?key=secret", "http://example.com\\@localhost", "http://",
    "http://host%2eexample", "http://example.com\r\nHost: localhost",
])
def test_reject_ambiguous_endpoint(url):
    with pytest.raises(ValueError):
        ProxyRouteEntry(path_prefix="model", target_endpoint=url)


@pytest.mark.parametrize("address", [
    "169.254.169.254", "100.100.100.200", "168.63.129.16", "0.0.0.0", "::",
    "224.0.0.1", "fe80::1", "::ffff:169.254.169.254", "64:ff9b::a9fe:a9fe",
    "2002:a9fe:a9fe::1", "fd00:ec2::254",
])
def test_metadata_and_special_addresses_always_denied(address):
    with pytest.raises(ProxyPolicyError):
        check_address(address)


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "10.0.0.1", "192.168.1.1", "8.8.8.8"])
def test_model_address_classes_supported(address):
    check_address(address)


def test_exact_origin_normalization_and_tls_policy():
    assert origin("https://API.Example.com.:443/v1") == origin("https://api.example.com")
    config = InferencePrivacyProxyConfig(
        routes=[ProxyRoute("model", "https://api.example.com/v1")],
        allowed_origins=["http://corp.example:9000"],
    )
    policy = config.freeze_policy()
    policy.validate(ProxyRoute("corp", "http://corp.example:9000/v1"))
    for target in ("https://evil.example", "http://api.example.com", "https://api.example.com:444"):
        with pytest.raises(ProxyPolicyError):
            policy.validate(ProxyRoute("bad", target))
    with pytest.raises(ProxyPolicyError):
        policy.validate(ProxyRoute("bad", "https://api.example.com", skip_cert_verify=True))
    with pytest.raises(ValueError):
        InferencePrivacyProxyPolicy(allowed_origins=["https://api.example.com/v1"])


@pytest.mark.asyncio
@pytest.mark.parametrize("answers", [
    ["8.8.8.8", "169.254.169.254"], ["127.0.0.1"], ["2130706433"],
])
async def test_dns_answers_checked_before_any_connect(monkeypatch, answers):
    route = ProxyRoute("model", "http://public.example")
    policy = TargetPolicy.from_config(InferencePrivacyProxyConfig(routes=[route]))
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 80)) for a in answers
    ]))
    connect = AsyncMock()
    monkeypatch.setattr(loop, "sock_connect", connect)
    with pytest.raises((ProxyPolicyError, ValueError)):
        await policy.connect(route, None)
    connect.assert_not_called()


@contextlib.asynccontextmanager
async def serving(handler, *, token=None, api_key="upstream-secret", base="/v1", tls=None, ca=None,
                  hostname="127.0.0.1", allowed=False, insecure=False):
    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=tls)
    port = server.sockets[0].getsockname()[1]
    endpoint = f"{'https' if tls else 'http'}://{hostname}:{port}"
    config = InferencePrivacyProxyConfig(
        routes=[ProxyRoute("model", endpoint + base, api_key, insecure)],
        allowed_origins=[endpoint] if allowed else [],
        insecure_tls_origins=[endpoint] if insecure else [], ca_file=ca,
    )
    proxy = InferencePrivacyProxy(config)
    if token:
        proxy._token = token.encode()
    proxy.enable_route("/model")
    try:
        await proxy.start()
        listen = proxy.server.sockets[0].getsockname()[1]
        yield proxy, f"http://127.0.0.1:{listen}", endpoint
    finally:
        await proxy.stop()
        server.close()
        await server.wait_closed()


async def read_request(reader):
    conn = h11.Connection(h11.SERVER)
    headers, body = None, bytearray()
    while True:
        event = await InferencePrivacyProxy._event(conn, reader)
        if isinstance(event, h11.Request):
            headers = event
        elif isinstance(event, h11.Data):
            body.extend(event.data)
        elif isinstance(event, h11.EndOfMessage):
            return headers, bytes(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["Authorization", "X-Api-Key", "Proxy-Authorization"])
async def test_token_auth_injection_binary_and_query(credential):
    captured = []

    async def handler(reader, writer):
        captured.append(await read_request(reader))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n\x00\xffOK")
        await writer.drain()
        writer.close()

    async with serving(handler, token="proxy-secret") as (_, url, endpoint):
        value = "proxy-secret" if credential == "X-Api-Key" else "Bearer proxy-secret"
        headers = {credential: value}
        if credential == "Proxy-Authorization":
            headers["Authorization"] = "Bearer placeholder"
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(url + "/model/chat?sig=a%2Fb%2Bz", headers=headers,
                                         content=b"\xff\x00\xfe\r\n")
        assert response.status_code == 200
        assert response.content == b"\x00\xffOK"
    request, body = captured[0]
    assert body == b"\xff\x00\xfe\r\n"
    assert request.target == b"/v1/chat?sig=a%2Fb%2Bz"
    assert dict(request.headers)[b"host"] == endpoint.removeprefix("http://").encode()
    assert b"proxy-secret" not in repr(list(request.headers)).encode()
    assert b"upstream-secret" in repr(list(request.headers)).encode()
    assert b"proxy-authorization" not in dict(request.headers)


@pytest.mark.asyncio
async def test_keepalive_each_request_reauthenticated_and_rerouted():
    calls = []

    async def handler(reader, writer):
        calls.append(await read_request(reader))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        await writer.drain()
        writer.close()

    async with serving(handler, token="secret") as (proxy, url, endpoint):
        proxy.config.routes.append(ProxyRoute("other", endpoint + "/v2", "second-key"))
        proxy.enable_route("/other")
        async with httpx.AsyncClient(trust_env=False) as client:
            first = await client.get(url + "/model/chat", headers={"Authorization": "Bearer secret"})
            second = await client.get(url + "/other/messages", headers={"X-Api-Key": "secret"})
            assert first.status_code == second.status_code == 200
            assert first.extensions["network_stream"] is second.extensions["network_stream"]
            denied = await client.get(url + "/model/chat")
            assert denied.status_code == 401
    assert len(calls) == 2
    assert calls[1][0].target == b"/v2/messages"
    assert dict(calls[1][0].headers)[b"x-api-key"] == b"second-key"


@pytest.mark.asyncio
async def test_sse_delivers_before_upstream_finishes_and_chunked_upload():
    release = asyncio.Event()
    captured = []

    async def handler(reader, writer):
        captured.append(await read_request(reader))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n9\r\ndata: a\n\n\r\n")
        await writer.drain()
        await release.wait()
        writer.write(b"9\r\ndata: b\n\n\r\n0\r\n\r\n")
        await writer.drain()
        writer.close()

    async def chunks():
        yield b"one\x00"
        await asyncio.sleep(0)
        yield b"\xfftwo"

    async with serving(handler) as (_, url, _):
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            async with client.stream("POST", url + "/model/chat", content=chunks()) as response:
                assert response.status_code == 200
                iterator = response.aiter_bytes()
                try:
                    assert await asyncio.wait_for(anext(iterator), 2) == b"data: a\n\n"
                finally:
                    release.set()
                assert b"".join([chunk async for chunk in iterator]) == b"data: b\n\n"
    assert captured[0][1] == b"one\x00\xfftwo"


@pytest.mark.asyncio
async def test_split_headers_expect_continue_and_no_upstream_redirect_follow():
    async def handler(reader, writer):
        request, body = await read_request(reader)
        assert body == b"abc"
        writer.write(b"HTTP/1.1 302 Found\r\nLocation: http://169.254.169.254/\r\n"
                     b"Content-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    async with serving(handler) as (_, url, _):
        port = int(url.rsplit(":", 1)[1])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"POST /model/chat HTTP/1.1\r\nHo")
        await writer.drain()
        writer.write(b"st: localhost\r\nContent-Length: 3\r\nExpect: 100-continue\r\n\r\n")
        await writer.drain()
        assert b"100" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        writer.write(b"abc")
        await writer.drain()
        assert b"302" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_nonloopback_listener_requires_separate_token(monkeypatch):
    proxy = InferencePrivacyProxy(InferencePrivacyProxyConfig(listen_host="0.0.0.0"))
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", "management-only")
    with pytest.raises(ProxyPolicyError):
        await proxy.start()
    assert proxy.server is None


@pytest.mark.asyncio
async def test_api_cannot_expand_policy_and_bad_update_keeps_running(monkeypatch):
    manager = InferencePrivacyProxyManager()
    config = InferencePrivacyProxyConfig(
        listen_port=18322, routes=[ProxyRoute("model", "https://api.example.com")],
        allowed_origins=["http://corp.example:9000"],
    )
    await manager.create_proxy("model", config)
    instance = manager.get_global_instance()
    running = Mock(is_running=True, stop=AsyncMock())
    instance.proxy = running
    from jiuwenbox.proxy.inference_privacy_proxy_manager import ProxyState
    instance.state = ProxyState.RUNNING
    monkeypatch.setattr(routes, "_mgr", lambda: manager)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for method, path in [("POST", "/proxies"), ("PUT", "/proxies/model")]:
            result = await client.request(method, "/api/v1" + path, json={
                "path_prefix": "evil", "target_endpoint": "http://127.0.0.1:22",
                "allowed_origins": ["http://127.0.0.1:22"],
            })
            assert result.status_code == 403
        allowed = await client.post("/api/v1/proxies", json={
            "path_prefix": "corp", "target_endpoint": "http://corp.example:9000/v1",
        })
        assert allowed.status_code == 201
        tls_bypass = await client.put("/api/v1/proxies/model", json={
            "path_prefix": "model", "target_endpoint": "https://api.example.com",
            "skip_cert_verify": True,
        })
        assert tls_bypass.status_code == 403
    running.stop.assert_not_called()
    assert instance.config.routes[0].target_endpoint == "https://api.example.com"
    await manager.delete_proxy("corp")
    instance.state = ProxyState.STOPPED
    await manager.delete_proxy("model")
    with pytest.raises(ProxyPolicyError):
        await manager.create_proxy("evil", InferencePrivacyProxyConfig(
            listen_port=18322, routes=[ProxyRoute("evil", "https://evil.example")]))


@pytest.mark.asyncio
async def test_hot_update_preserves_all_enabled_routes_and_cancels_old_connections():
    calls = []

    async def handler(reader, writer):
        calls.append(await read_request(reader))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        await writer.drain()
        writer.close()

    async with serving(handler) as (_, _, endpoint):
        # Reserve an ephemeral port, then let the manager own its listener.
        reserve = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = reserve.sockets[0].getsockname()[1]
        reserve.close()
        await reserve.wait_closed()
        manager = InferencePrivacyProxyManager()
        await manager.create_proxy("one", InferencePrivacyProxyConfig(
            listen_port=port, routes=[ProxyRoute("one", endpoint), ProxyRoute("two", endpoint)]))
        await manager.start_proxy("one")
        await manager.start_proxy("two")
        old = manager.get_global_instance().proxy
        idle_reader, idle_writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(0)
        try:
            await asyncio.wait_for(manager.update_proxy("one", InferencePrivacyProxyConfig(
                routes=[ProxyRoute("one", endpoint + "/updated")])), 3)
            assert not old.is_running
            assert await asyncio.wait_for(idle_reader.read(), 2) == b""
            async with httpx.AsyncClient(trust_env=False) as client:
                for path in ("one", "two"):
                    response = await client.get(f"http://127.0.0.1:{port}/{path}/chat")
                    assert response.status_code == 200
            assert calls[0][0].target == b"/updated/chat"
            assert calls[1][0].target == b"/chat"
        finally:
            idle_writer.close()
            await idle_writer.wait_closed()
            await manager.stop_proxy("one")
            await manager.stop_proxy("two")


@pytest.fixture
def tls_files(tmp_path):
    import datetime
    import ssl
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "model.internal")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("model.internal")]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context, str(cert_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["custom-ca", "untrusted", "wrong-host", "explicit-insecure"])
async def test_tls_ca_sni_hostname_and_dns_pinning(tls_files, monkeypatch, mode):
    context, ca = tls_files
    names, resolutions, calls = [], [], []
    context.set_servername_callback(lambda _, name, __: names.append(name))
    loop = asyncio.get_running_loop()
    original = loop.getaddrinfo

    async def resolve(host, port, *args, **kwargs):
        if host in ("model.internal", "wrong.internal"):
            resolutions.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return await original(host, port, *args, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", resolve)

    async def handler(reader, writer):
        calls.append(await read_request(reader))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        await writer.drain()
        writer.close()

    hostname = "wrong.internal" if mode == "wrong-host" else "model.internal"
    async with serving(handler, hostname=hostname, tls=context,
                       ca=ca if mode in ("custom-ca", "wrong-host") else None,
                       allowed=True, insecure=mode == "explicit-insecure") as (_, url, _):
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(url + "/model/chat")
        assert response.status_code == (200 if mode in ("custom-ca", "explicit-insecure") else 502)
    assert names == [hostname]
    assert resolutions == [hostname]  # No second hostname resolution during socket/TLS connect.
    assert len(calls) == (1 if response.status_code == 200 else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra, status", [
    (b"Authorization: Bearer bad\r\n", 401),
    (b"Authorization: Bearer secret\r\nAuthorization: Bearer secret\r\n", 401),
    (b"Authorization: Bearer secret\r\nUpgrade: websocket\r\n", 400),
    (b"Authorization: Bearer secret\r\nContent-Length: 0\r\nTransfer-Encoding: chunked\r\n", 400),
    (b"Authorization: Bearer secret\r\nConnection: Authorization\r\n", 400),
])
async def test_invalid_auth_and_framing_never_reach_upstream(extra, status):
    handler = AsyncMock()
    async with serving(handler, token="secret") as (_, url, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", int(url.rsplit(":", 1)[1]))
        writer.write(b"GET /model/chat HTTP/1.1\r\nHost: localhost\r\n" + extra + b"\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 3)
        assert f" {status} ".encode() in response
        writer.close()
        await writer.wait_closed()
    handler.assert_not_called()


@pytest.mark.asyncio
async def test_dedicated_proxy_auth_preserves_upstream_passthrough():
    captured = []

    async def handler(reader, writer):
        captured.append(await read_request(reader))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    async with serving(handler, token="secret", api_key="") as (_, url, _):
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(url + "/model/chat", headers={
                "Proxy-Authorization": "Bearer secret", "Authorization": "Bearer actual-api-key",
            })
            assert response.status_code == 200
    assert dict(captured[0][0].headers)[b"authorization"] == b"Bearer actual-api-key"
    assert b"proxy-authorization" not in dict(captured[0][0].headers)


def test_tls_exception_does_not_survive_route_replacement():
    import ssl

    loose = ProxyRoute("same", "https://debug.example", skip_cert_verify=True)
    strict = ProxyRoute("same", "https://production.example")
    proxy = InferencePrivacyProxy(InferencePrivacyProxyConfig(
        routes=[loose], allowed_origins=["https://production.example"],
        insecure_tls_origins=["https://debug.example"],
    ))
    assert proxy._get_ssl_context(loose).verify_mode == ssl.CERT_NONE
    assert proxy._get_ssl_context(strict).verify_mode == ssl.CERT_REQUIRED
    assert proxy._get_ssl_context(strict).check_hostname


@pytest.mark.asyncio
async def test_empty_deployment_routes_can_be_managed_without_expanding_trust():
    manager = InferencePrivacyProxyManager()
    await manager.load_from_policy(InferencePrivacyProxyPolicy(
        listen_host="127.0.0.1", listen_port=18322,
        allowed_origins=["http://127.0.0.1:11434"],
    ))
    await manager.create_proxy("local", InferencePrivacyProxyConfig(
        routes=[ProxyRoute("local", "http://127.0.0.1:11434/v1")]))
    assert (await manager.get_proxy("local"))["state"] == "stopped"
    with pytest.raises(ProxyPolicyError):
        await manager.create_proxy("other", InferencePrivacyProxyConfig(
            routes=[ProxyRoute("other", "http://127.0.0.1:11435")]))


@pytest.mark.asyncio
@pytest.mark.parametrize("form", [b"GET http://169.254.169.254/ HTTP/1.1", b"CONNECT host:443 HTTP/1.1"])
async def test_forward_proxy_and_tunnel_requests_are_rejected(form):
    handler = AsyncMock()
    async with serving(handler) as (_, url, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", int(url.rsplit(":", 1)[1]))
        writer.write(form + b"\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        assert b"400 Bad Request" in await asyncio.wait_for(reader.read(), 3)
        writer.close()
        await writer.wait_closed()
    handler.assert_not_called()


@pytest.mark.asyncio
async def test_early_upstream_rejection_does_not_wait_for_entire_upload():
    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 413 Content Too Large\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    async with serving(handler) as (_, url, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", int(url.rsplit(":", 1)[1]))
        writer.write(b"POST /model/chat HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1000000\r\n\r\na")
        await writer.drain()
        assert b"413 Content Too Large" in await asyncio.wait_for(reader.read(), 3)
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_ipv6_model_host_and_port_are_preserved():
    captured = []

    async def handler(reader, writer):
        captured.append(await read_request(reader))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    try:
        server = await asyncio.start_server(handler, "::1", 0)
    except OSError:
        pytest.skip("IPv6 loopback is unavailable")
    port = server.sockets[0].getsockname()[1]
    proxy = InferencePrivacyProxy(InferencePrivacyProxyConfig(
        routes=[ProxyRoute("model", f"http://[::1]:{port}")]))
    proxy.enable_route("/model")
    try:
        await proxy.start()
        front_port = proxy.server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(f"http://127.0.0.1:{front_port}/model/chat")
        assert response.status_code == 200
        assert dict(captured[0][0].headers)[b"host"] == f"[::1]:{port}".encode()
    finally:
        await proxy.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_interrupted_stream_closes_without_appending_second_response():
    async def handler(reader, writer):
        await read_request(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nabc")
        await writer.drain()
        writer.close()

    async with serving(handler) as (_, url, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", int(url.rsplit(":", 1)[1]))
        writer.write(b"GET /model/chat HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 3)
        assert response.count(b"HTTP/1.1") == 1
        assert b"200 OK" in response and response.endswith(b"abc")
        writer.close()
        await writer.wait_closed()
