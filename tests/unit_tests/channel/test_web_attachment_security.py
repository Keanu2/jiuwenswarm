# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Attachment URL policy and bounded downloads; no external network access."""

from __future__ import annotations

import asyncio
import gzip
import socket
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from jiuwenswarm.common.security import attachment_download as downloads
from jiuwenswarm.common.security.attachment_download import (
    AttachmentDownloadPolicy,
    AttachmentError,
    AttachmentResolver,
    download_attachment,
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    for name in (
        downloads.ENV_PRIVATE_ORIGINS,
        "WEB_ATTACHMENT_MAX_BYTES",
        "WEB_ATTACHMENT_TIMEOUT_SECONDS",
        "WEB_ATTACHMENT_MAX_FILES",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://[::1]/x",
        "http://10.2.3.4/x",
        "http://172.16.2.3/x",
        "http://192.168.1.2/x",
        "http://[fc00::1]/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://[fe80::1]/x",
        "http://100.100.100.200/",
        "http://168.63.129.16/",
        "http://[fd00:ec2::254]/",
        "http://[::ffff:169.254.169.254]/",
        "http://[::ffff:127.0.0.1]/",
        "http://0.0.0.0/",
        "http://[::]/",
        "http://224.0.0.1/",
        "http://[ff02::1]/",
        "http://[64:ff9b::a9fe:a9fe]/",
        "http://[2002:a9fe:a9fe::1]/",
        "http://[2001::1]/",
        "http://192.0.2.1/",
        "http://255.255.255.255/",
        "http://127.1/",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://192.0.0.192/",
        "http://192.88.99.1/",
        "http://[fec0::1]/",
        "http://[fd20:ce::254]/",
    ],
)
def test_default_policy_blocks_nonpublic_and_metadata_addresses(url):
    with pytest.raises(AttachmentError):
        AttachmentDownloadPolicy().check_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a",
        "data:text/plain,a",
        "",
        None,
        "http://",
        "http://user:password@example.com/a",
        "http://example.com:0/a",
        "http://example.com:65536/a",
        "http://example.com\\@127.0.0.1/a",
        "http://[fe80::1%25eth0]/a",
        "http://example.com/a\r\nInjected:value",
    ],
)
def test_only_plain_http_urls_are_downloadable(url):
    with pytest.raises(AttachmentError):
        AttachmentDownloadPolicy().check_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/report%20one.xlsx?signature=secret%2Ftoken&expires=1",
        "http://8.8.8.8:8080/a",
        "https://[2606:4700:4700::1111]/a",
    ],
)
def test_public_http_https_and_custom_ports_remain_supported(url):
    target, private = AttachmentDownloadPolicy().check_url(url)
    assert target.scheme in {"http", "https"}
    assert not private


@pytest.mark.parametrize(
    "setting",
    [
        "*",
        "http://*.corp",
        "http://files.corp/",
        "http://files.corp/path",
        "http://files.corp/path/..",
        "http://files.corp?x=y",
        "http://files.corp#x",
        "http://u:p@files.corp",
        "http://files.corp,",
        "null",
    ],
)
def test_private_allowlist_requires_exact_origins(monkeypatch, setting):
    monkeypatch.setenv(downloads.ENV_PRIVATE_ORIGINS, setting)
    with pytest.raises(ValueError, match=downloads.ENV_PRIVATE_ORIGINS):
        AttachmentDownloadPolicy.from_env()


def test_private_allowlist_matches_scheme_host_and_port(monkeypatch):
    monkeypatch.setenv(
        downloads.ENV_PRIVATE_ORIGINS, " https://files.corp:8443,http://10.2.3.4:8080 "
    )
    policy = AttachmentDownloadPolicy.from_env()
    assert policy.check_url("https://files.corp:8443/download?sig=abc")[1]
    assert policy.check_url("http://10.2.3.4:8080/a")[1]
    for url in (
        "http://files.corp:8443/a",
        "https://files.corp/a",
        "https://files.corp.evil:8443/a",
    ):
        assert not policy.check_url(url)[1]
    with pytest.raises(AttachmentError):
        policy.check_url("http://10.2.3.4:8081/a")


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/a",
        "http://100.100.100.200/a",
        "http://168.63.129.16/a",
        "http://[fd00:ec2::254]/a",
        "http://[fe80::1]/a",
        "http://224.0.0.1/a",
    ],
)
def test_metadata_and_special_addresses_cannot_be_allowlisted(url):
    from yarl import URL

    origin = URL(url).origin()
    policy = AttachmentDownloadPolicy(
        private_origins=frozenset({(origin.scheme, origin.raw_host, origin.port)})
    )
    with pytest.raises(AttachmentError):
        policy.check_url(url)


@pytest.mark.parametrize(
    "name",
    [
        "WEB_ATTACHMENT_MAX_BYTES",
        "WEB_ATTACHMENT_TIMEOUT_SECONDS",
        "WEB_ATTACHMENT_MAX_FILES",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1", "oops"])
def test_invalid_resource_limits_fail_configuration(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        AttachmentDownloadPolicy.from_env()


def record(host):
    return {
        "hostname": "files.example",
        "host": host,
        "port": 443,
        "family": socket.AF_INET6 if ":" in host else socket.AF_INET,
        "proto": 0,
        "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
    }


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["::ffff:127.0.0.1"],
        ["8.8.8.8", "10.0.0.1"],
        ["8.8.8.8", "fc00::1"],
        [],
    ],
)
@pytest.mark.asyncio
async def test_connector_resolver_rejects_private_and_mixed_dns(monkeypatch, addresses):
    resolver = AttachmentResolver(AttachmentDownloadPolicy(), allow_private=False)
    monkeypatch.setattr(
        resolver.resolver,
        "resolve",
        AsyncMock(return_value=[record(a) for a in addresses]),
    )
    try:
        with pytest.raises(AttachmentError):
            await resolver.resolve("files.example", 443)
    finally:
        await resolver.close()


@pytest.mark.asyncio
async def test_checked_numeric_dns_answers_are_used_without_a_second_resolution(
    monkeypatch,
):
    resolver = AttachmentResolver(AttachmentDownloadPolicy(), allow_private=False)
    answers = [record("8.8.8.8"), record("2606:4700:4700::1111")]
    lookup = AsyncMock(return_value=answers)
    monkeypatch.setattr(resolver.resolver, "resolve", lookup)
    try:
        assert await resolver.resolve("files.example", 443) is answers
        lookup.assert_awaited_once_with("files.example", 443, socket.AF_UNSPEC)
    finally:
        await resolver.close()


class FakeResponse:
    def __init__(
        self, *, status=200, location=None, chunks=(b"report",), length=None, delay=0
    ):
        self.status = status
        self.headers = {"Location": location} if location is not None else {}
        self.content_length = length
        self.chunks = chunks
        self.delay = delay
        self.content = self
        self.read_chunks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            self.read_chunks += 1
            yield chunk


@pytest.fixture
def fake_http(monkeypatch):
    responses, calls = [], []

    class FakeSession:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            self.connector = kwargs["connector"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def get(self, url, *, allow_redirects):
            assert allow_redirects is False
            calls.append(str(url))
            return responses.pop(0)

    monkeypatch.setattr(downloads.aiohttp, "ClientSession", FakeSession)
    return responses, calls


@pytest.mark.asyncio
async def test_public_cdn_redirect_and_signed_query_remain_usable(fake_http):
    responses, calls = fake_http
    responses.extend(
        [
            FakeResponse(
                status=302, location="https://cdn.example/report.xlsx?sig=a%2Fb&x=1"
            ),
            FakeResponse(status=307, location="/next.xlsx?sig=c%2Fd"),
            FakeResponse(chunks=(b"one", b"two")),
        ]
    )
    result = await download_attachment(
        "https://files.example/start?sig=original", AttachmentDownloadPolicy()
    )
    assert result == b"onetwo"
    assert calls == [
        "https://files.example/start?sig=original",
        "https://cdn.example/report.xlsx?sig=a%2Fb&x=1",
        "https://cdn.example/next.xlsx?sig=c%2Fd",
    ]


@pytest.mark.parametrize(
    "location",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1/admin",
        "file:///etc/passwd",
    ],
)
@pytest.mark.asyncio
async def test_each_redirect_is_revalidated_before_another_request(fake_http, location):
    responses, calls = fake_http
    responses.append(FakeResponse(status=302, location=location))
    with pytest.raises(AttachmentError):
        await download_attachment(
            "https://files.example/start", AttachmentDownloadPolicy()
        )
    assert calls == ["https://files.example/start"]


@pytest.mark.asyncio
async def test_redirect_count_is_bounded(fake_http):
    responses, calls = fake_http
    responses.extend(FakeResponse(status=302, location="/again") for _ in range(6))
    with pytest.raises(AttachmentError, match="redirect limit"):
        await download_attachment(
            "https://files.example/start", AttachmentDownloadPolicy()
        )
    assert len(calls) == 6


@pytest.mark.parametrize(
    "length,chunks",
    [(11, (b"unused",)), (None, (b"12345", b"678901")), (4, (b"decoded" * 3,))],
)
@pytest.mark.asyncio
async def test_declared_chunked_and_decoded_body_sizes_are_bounded(
    fake_http, length, chunks
):
    responses, _ = fake_http
    response = FakeResponse(length=length, chunks=chunks)
    responses.append(response)
    with pytest.raises(AttachmentError) as error:
        await download_attachment(
            "https://files.example/a", AttachmentDownloadPolicy(max_bytes=10)
        )
    assert error.value.code == "ATTACHMENT_TOO_LARGE"
    if length == 11:
        assert response.read_chunks == 0


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_does_not_expose_signed_url(fake_http):
    responses, _ = fake_http
    responses.append(FakeResponse(delay=0.1))
    with pytest.raises(AttachmentError) as error:
        await download_attachment(
            "https://files.example/a?secret=token",
            AttachmentDownloadPolicy(timeout_seconds=0.01),
        )
    assert error.value.code == "ATTACHMENT_DOWNLOAD_FAILED"
    assert "token" not in str(error.value)


@pytest.mark.asyncio
async def test_zero_byte_attachment_is_a_success(fake_http):
    responses, _ = fake_http
    responses.append(FakeResponse(chunks=()))
    assert (
        await download_attachment(
            "https://files.example/empty", AttachmentDownloadPolicy()
        )
        == b""
    )


@pytest.mark.asyncio
async def test_real_aiohttp_connector_pins_allowlisted_dns_and_rechecks_redirect(
    monkeypatch,
):
    received = []

    async def handler(request):
        received.append((request.host, request.raw_path))
        if request.path == "/start":
            raise web.HTTPFound("/report?sig=abc")
        if request.path == "/compressed":
            return web.Response(
                body=gzip.compress(b"a" * 10000), headers={"Content-Encoding": "gzip"}
            )
        return web.Response(body=b"report")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    answers = [{**record("127.0.0.1"), "port": port}]
    lookup = AsyncMock(return_value=answers)
    monkeypatch.setattr(downloads.ThreadedResolver, "resolve", lookup)
    policy = AttachmentDownloadPolicy(
        private_origins=frozenset({("http", "files.corp", port)})
    )
    try:
        url = f"http://files.corp:{port}"
        assert await download_attachment(url + "/start", policy) == b"report"
        assert received == [
            (f"files.corp:{port}", "/start"),
            (f"files.corp:{port}", "/report?sig=abc"),
        ]
        assert lookup.await_count == 2  # Exactly one DNS resolution per connection.
        received.clear()
        with pytest.raises(AttachmentError):
            await download_attachment(url + "/report", AttachmentDownloadPolicy())
        assert received == []
        with pytest.raises(AttachmentError) as error:
            await download_attachment(
                url + "/compressed", replace(policy, max_bytes=1000)
            )
        assert error.value.code == "ATTACHMENT_TOO_LARGE"
        received.clear()
        lookup.side_effect = [answers, [{**record("169.254.169.254"), "port": port}]]
        with pytest.raises(AttachmentError):
            await download_attachment(url + "/start", policy)
        assert received == [(f"files.corp:{port}", "/start")]
    finally:
        await runner.cleanup()
