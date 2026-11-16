"""Public HTTP destinations and DNS-pinned transports for untrusted URLs."""
from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


class UnsafeURL(ValueError):
    """The supplied URL is not an unambiguous, publicly reachable HTTP URL."""


@dataclass(frozen=True)
class PublicDestination:
    url: httpx.URL
    addresses: tuple[str, ...]


def _public_address(raw: str) -> str:
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise UnsafeURL("URL hostname did not resolve to a valid IP address.") from exc
    if (
        not address.is_global or address.is_reserved or address.is_multicast
        or address.is_loopback or address.is_link_local or address.is_unspecified
        or getattr(address, "is_site_local", False)
        or getattr(address, "ipv4_mapped", None) is not None
    ):
        raise UnsafeURL("Only public Internet destinations are allowed.")
    return str(address)


def resolve_public_url(raw: str, *, allow_query: bool = False) -> PublicDestination:
    if (
        not isinstance(raw, str) or not raw or len(raw) > 8192
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw)
        or "\\" in raw or "#" in raw or (not allow_query and "?" in raw)
    ):
        raise UnsafeURL("An HTTP(S) URL without credentials or fragments is required.")
    try:
        parsed = urlsplit(raw)
        if (
            parsed.scheme.lower() not in ("http", "https") or not parsed.netloc
            or not parsed.hostname or "@" in parsed.netloc or "%" in parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            raise UnsafeURL("An HTTP(S) URL without credentials or fragments is required.")
        url = httpx.URL(raw)
        host = url.raw_host.decode("ascii")
        port = parsed.port or (443 if url.scheme == "https" else 80)
    except (ValueError, UnicodeError, httpx.InvalidURL) as exc:
        raise UnsafeURL("Invalid HTTP(S) destination.") from exc
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as exc:
            raise UnsafeURL("The destination hostname could not be resolved.") from exc
        addresses = tuple(dict.fromkeys(_public_address(record[4][0]) for record in records))
        if not addresses:
            raise UnsafeURL("The destination hostname could not be resolved.")
    else:
        addresses = (_public_address(str(literal)),)
    # Prefer IPv4 on servers without IPv6 routing; never connect to an unchecked name.
    addresses = tuple(sorted(addresses, key=lambda address: ipaddress.ip_address(address).version))
    return PublicDestination(url=url, addresses=addresses)


def validate_public_url(raw: str, *, allow_query: bool = False) -> str:
    return str(resolve_public_url(raw, allow_query=allow_query).url)


class PublicOnlyTransport(httpx.BaseTransport):
    """Pin public DNS answers, retaining the original Host and TLS identity."""

    def __init__(
        self, *, transport_factory: Callable[[], httpx.BaseTransport] | None = None,
    ) -> None:
        self._factory = transport_factory or (lambda: httpx.HTTPTransport(trust_env=False))
        self._transports: dict[tuple[str, bytes, int | None], httpx.BaseTransport] = {}
        self._lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        destination = resolve_public_url(str(request.url), allow_query=True)
        origin = (destination.url.scheme, destination.url.raw_host, destination.url.port)
        with self._lock:
            if origin not in self._transports:
                self._transports[origin] = self._factory()
            transport = self._transports[origin]
        headers = request.headers.copy()
        headers["Host"] = destination.url.netloc.decode("ascii")
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = destination.url.raw_host.decode("ascii")
        # Separate pools per original origin also prevent TLS reuse across names
        # that happen to resolve to the same public address.
        pinned = httpx.Request(
            request.method, destination.url.copy_with(host=destination.addresses[0]),
            headers=headers, stream=request.stream, extensions=extensions,
        )
        return transport.handle_request(pinned)

    def close(self) -> None:
        with self._lock:
            transports, self._transports = self._transports, {}
        for transport in transports.values():
            transport.close()
