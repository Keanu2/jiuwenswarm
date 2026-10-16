# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bounded HTTP attachment downloads with connection-time SSRF validation."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver
from yarl import URL

ENV_PRIVATE_ORIGINS = "WEB_ATTACHMENT_PRIVATE_ORIGINS"
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_METADATA_IPS = {
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("168.63.129.16"),
    ipaddress.ip_address("fd00:ec2::254"),
    ipaddress.ip_address("fd20:ce::254"),
}
_SPECIAL_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        # Explicit ranges keep behavior safe on all supported Python versions;
        # is_global classifications changed for several special ranges in 3.13.
        "0.0.0.0/8",
        "192.0.0.0/24",
        "192.88.99.0/24",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "2002::/16",
        "2001::/23",
        "fec0::/10",
    )
)


class AttachmentError(ValueError):
    """Safe client-facing failure, without credentials or signed URL parameters."""

    def __init__(self, message: str, code: str = "ATTACHMENT_REJECTED"):
        super().__init__(message)
        self.code = code


def _http_url(value: str) -> URL:
    if not isinstance(value, str) or not value or len(value) > 16384:
        raise AttachmentError("Attachment URL must be an HTTP(S) URL")
    if any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise AttachmentError("Attachment URL contains invalid characters")
    try:
        url = URL(value)
        if url.scheme not in {"http", "https"} or not url.raw_host or url.port is None:
            raise ValueError("Invalid HTTP URL")
        if url.user is not None or url.password is not None or "%" in url.raw_host:
            raise ValueError("Credentials and scoped addresses are not supported")
        if not 1 <= url.port <= 65535:
            raise ValueError("Invalid port")
    except (ValueError, UnicodeError) as exc:
        raise AttachmentError(
            "Attachment URL must be HTTP(S), without embedded credentials"
        ) from exc
    # Normalize the hostname for IDNA/TLS, but retain existing escapes in
    # signed paths/queries. Requoting %2F or %2B can invalidate CDN signatures.
    encoded = URL(value, encoded=True)
    path = encoded.raw_path if encoded.raw_path.isascii() else url.raw_path
    query = (
        encoded.raw_query_string
        if encoded.raw_query_string.isascii()
        else url.raw_query_string
    )
    return URL.build(
        scheme=url.scheme,
        authority=url.raw_authority,
        path=path,
        query_string=query,
        encoded=True,
    )


def _origin(url: URL) -> tuple[str, str, int]:
    return url.scheme, url.raw_host, url.port


def _positive_setting(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
        if value <= 0:
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    return value


@dataclass(frozen=True)
class AttachmentDownloadPolicy:
    private_origins: frozenset[tuple[str, str, int]] = frozenset()
    max_bytes: int = 64 * 1024 * 1024
    timeout_seconds: int = 60
    max_files: int = 16
    max_redirects: int = 5

    @classmethod
    def from_env(cls) -> AttachmentDownloadPolicy:
        private_origins = set()
        raw = os.environ.get(ENV_PRIVATE_ORIGINS, "").strip()
        if raw:
            for item in raw.split(","):
                value = item.strip()
                try:
                    url = _http_url(value)
                    # Inspect the original authority, before URL path normalization.
                    authority = value.partition("://")[2]
                    if authority.endswith(":") or any(c in authority for c in "/*?#"):
                        raise AttachmentError("Not an exact origin")
                except AttachmentError as exc:
                    raise ValueError(
                        f"{ENV_PRIVATE_ORIGINS} requires comma-separated exact http(s)://host[:port] origins"
                    ) from exc
                private_origins.add(_origin(url))
        return cls(
            private_origins=frozenset(private_origins),
            max_bytes=_positive_setting("WEB_ATTACHMENT_MAX_BYTES", 64 * 1024 * 1024),
            timeout_seconds=_positive_setting("WEB_ATTACHMENT_TIMEOUT_SECONDS", 60),
            max_files=_positive_setting("WEB_ATTACHMENT_MAX_FILES", 16),
        )

    def check_ip(self, value: str, *, allow_private: bool) -> None:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise AttachmentError(
                "Attachment hostname did not resolve to an IP address"
            ) from exc
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if (
            address in _METADATA_IPS
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or (address.is_reserved and not address.is_loopback)
            or any(address in network for network in _SPECIAL_NETWORKS)
            or (not address.is_global and not allow_private)
        ):
            raise AttachmentError(
                "Attachment destination is not an allowed network address"
            )

    def check_url(self, value: str) -> tuple[URL, bool]:
        url = _http_url(value)
        allow_private = _origin(url) in self.private_origins
        try:
            address = ipaddress.ip_address(url.raw_host)
        except ValueError:
            # aiohttp skips its resolver for numeric dotted/decimal hosts,
            # including short, octal, and integer IPv4 representations.
            if aiohttp.helpers.is_ip_address(url.raw_host):
                raise AttachmentError(
                    "Attachment IP address must use canonical notation"
                ) from None
            # Other hostnames, including hex aliases, use the checked resolver.
        else:
            self.check_ip(str(address), allow_private=allow_private)
        return url, allow_private


class AttachmentResolver(AbstractResolver):
    """The connector receives only checked numeric addresses, with no second DNS lookup."""

    def __init__(self, policy: AttachmentDownloadPolicy, *, allow_private: bool):
        self.policy = policy
        self.allow_private = allow_private
        self.resolver = ThreadedResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
        records = await self.resolver.resolve(host, port, family)
        if not records:
            raise AttachmentError("Attachment hostname did not resolve")
        # Reject mixed public/private DNS answers instead of allowing a fallback
        # connection to an address that wasn't checked.
        for record in records:
            self.policy.check_ip(record["host"], allow_private=self.allow_private)
        return records

    async def close(self) -> None:
        await self.resolver.close()


async def download_attachment(url: str, policy: AttachmentDownloadPolicy) -> bytes:
    try:
        async with asyncio.timeout(policy.timeout_seconds):
            current = url
            for hop in range(policy.max_redirects + 1):
                target, allow_private = policy.check_url(current)
                resolver = AttachmentResolver(policy, allow_private=allow_private)
                try:
                    connector = aiohttp.TCPConnector(
                        resolver=resolver, use_dns_cache=False
                    )
                    timeout = aiohttp.ClientTimeout(
                        total=policy.timeout_seconds,
                        connect=min(15, policy.timeout_seconds),
                        sock_read=min(30, policy.timeout_seconds),
                    )
                    async with aiohttp.ClientSession(
                        connector=connector,
                        timeout=timeout,
                        trust_env=False,
                        cookie_jar=aiohttp.DummyCookieJar(),
                    ) as session:
                        async with session.get(
                            target, allow_redirects=False
                        ) as response:
                            if response.status in _REDIRECT_STATUSES:
                                location = response.headers.get("Location")
                                if not location:
                                    raise AttachmentError(
                                        "Attachment redirect has no Location",
                                        "ATTACHMENT_DOWNLOAD_FAILED",
                                    )
                                if hop == policy.max_redirects:
                                    raise AttachmentError(
                                        "Attachment redirect limit exceeded"
                                    )
                                # Preserve signed queries and relative CDN redirects.
                                current = str(target.join(URL(location, encoded=True)))
                                continue
                            if response.status != 200:
                                raise AttachmentError(
                                    f"Attachment server returned HTTP {response.status}",
                                    "ATTACHMENT_DOWNLOAD_FAILED",
                                )
                            if (
                                response.content_length is not None
                                and response.content_length > policy.max_bytes
                            ):
                                raise AttachmentError(
                                    "Attachment exceeds the download size limit",
                                    "ATTACHMENT_TOO_LARGE",
                                )
                            content = bytearray()
                            # Count decoded bytes too: Content-Length cannot bound
                            # chunked bodies or compressed response expansion.
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                if len(content) + len(chunk) > policy.max_bytes:
                                    raise AttachmentError(
                                        "Attachment exceeds the download size limit",
                                        "ATTACHMENT_TOO_LARGE",
                                    )
                                content.extend(chunk)
                            return bytes(content)
                finally:
                    await resolver.close()
    except AttachmentError:
        raise
    except (aiohttp.ClientError, OSError, ValueError, TimeoutError) as exc:
        # aiohttp exceptions can include a URL or a redirect's signed query.
        raise AttachmentError(
            "Attachment download failed or timed out", "ATTACHMENT_DOWNLOAD_FAILED"
        ) from exc
    raise AttachmentError("Attachment redirect limit exceeded")  # pragma: no cover
