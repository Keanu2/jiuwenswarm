"""Deployment-owned inference target policy and DNS-pinned connections."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


class ProxyPolicyError(ValueError):
    """A request would exceed the administrator's proxy policy."""


def parse_endpoint(value: str) -> tuple[str, int, bool, str]:
    """Parse an HTTP base URL without credentials or ambiguous authority syntax."""
    if not value or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("Invalid inference endpoint URL")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "%" in parsed.netloc
        ):
            raise ValueError("Invalid inference endpoint URL")
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if not host or not 1 <= port <= 65535:
            raise ValueError("Invalid inference endpoint port")
        return host, port, parsed.scheme == "https", parsed.path.rstrip("/")
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Invalid inference endpoint URL") from exc


def origin(value: str, *, exact: bool = False) -> tuple[str, str, int]:
    host, port, tls, path = parse_endpoint(value)
    if exact and path:
        raise ValueError("Proxy allowlists require exact HTTP(S) origins without paths")
    return ("https" if tls else "http", host, port)


def check_address(value: str) -> None:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    # These destinations are never model endpoints, even on an approved origin.
    if (
        address.is_link_local or address.is_multicast or address.is_unspecified
        or (address.is_reserved and not address.is_loopback)
        or str(address) in {"100.100.100.200", "168.63.129.16", "fd00:ec2::254"}
        or (isinstance(address, ipaddress.IPv6Address) and (
            address.sixtofour is not None or address.teredo is not None
            or address in ipaddress.ip_network("64:ff9b::/96")
            or address in ipaddress.ip_network("64:ff9b:1::/48")
        ))
    ):
        raise ProxyPolicyError("Forbidden inference target address")


@dataclass(frozen=True)
class TargetPolicy:
    allowed: frozenset[tuple[str, str, int]]
    insecure_tls: frozenset[tuple[str, str, int]] = frozenset()
    private: frozenset[tuple[str, str, int]] = frozenset()

    @classmethod
    def from_config(cls, config):
        explicit = frozenset(origin(v, exact=True) for v in config.allowed_origins)
        local = set()
        for route in config.routes:
            target = origin(route.target_endpoint)
            try:
                address = ipaddress.ip_address(target[1])
            except ValueError:
                if target[1] == "localhost":
                    local.add(target)
            else:
                if not address.is_global:
                    local.add(target)
        return cls(
            frozenset(origin(r.target_endpoint) for r in config.routes)
            | explicit,
            frozenset(origin(v, exact=True) for v in config.insecure_tls_origins),
            explicit | frozenset(local),
        )

    def validate(self, route) -> None:
        target = origin(route.target_endpoint)
        if target not in self.allowed:
            raise ProxyPolicyError("Inference target origin is not approved in deployment policy")
        if route.skip_cert_verify and target not in self.insecure_tls:
            raise ProxyPolicyError("TLS verification bypass is not approved for this origin")
        host = target[1]
        if host == "metadata.google.internal" or host.endswith(".metadata.google.internal"):
            raise ProxyPolicyError("Forbidden inference target address")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return  # Hostnames are checked against every resolved address before connecting.
        check_address(str(address))

    async def connect(self, route, ssl_context):
        """Resolve once, reject mixed safe/unsafe answers, connect using numeric addresses."""
        self.validate(route)
        loop = asyncio.get_running_loop()
        answers = await asyncio.wait_for(loop.getaddrinfo(
            route.target_host, route.target_port, type=socket.SOCK_STREAM,
        ), timeout=10)
        if not answers:
            raise OSError("Inference target did not resolve")
        for _, _, _, _, sockaddr in answers:
            check_address(sockaddr[0])
            if not ipaddress.ip_address(sockaddr[0]).is_global and origin(route.target_endpoint) not in self.private:
                raise ProxyPolicyError("Private inference target requires an explicit allowed origin")
        last_error = None
        for family, socktype, proto, _, sockaddr in answers:
            sock = socket.socket(family, socktype, proto)
            sock.setblocking(False)
            try:
                await asyncio.wait_for(loop.sock_connect(sock, sockaddr), timeout=10)
                kwargs = {"ssl": ssl_context, "server_hostname": route.target_host} if ssl_context else {}
                return await asyncio.wait_for(asyncio.open_connection(sock=sock, **kwargs), timeout=10)
            except (OSError, asyncio.TimeoutError) as exc:
                sock.close()
                last_error = exc
            except BaseException:
                sock.close()
                raise
        raise OSError("Cannot connect to inference target") from last_error
