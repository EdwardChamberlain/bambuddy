"""Shared URL-safety primitives for the SSRF guards in this package.

Bambuddy has exactly two outbound-URL policies, and which one applies is a
property of the *service*, not of the caller:

- **LAN-service** (``assert_safe_lan_service_url`` below) — the service
  legitimately lives on the same host or home LAN, so loopback and RFC-1918
  must be permitted; blocking them would break the normal topology. Used for
  Spoolman, self-hosted notification servers (ntfy, Bark, Gotify, custom
  webhooks), Home Assistant, the Obico ML endpoint and the slicer sidecars.
- **Public-internet** (``_oidc_helpers.assert_safe_public_https_url``) — the
  resource can only sensibly live on the public internet, so a private
  address is an SSRF probe rather than a configuration. Used for OIDC issuer
  and icon URLs.

Each caller supplies the protocol schemes it actually supports. All callers
reject destinations that are dangerous regardless of topology:
numeric-encoded IPs, cloud-metadata endpoints, multicast and unspecified
addresses, and IPv4-mapped IPv6 encodings of any of the above.

The LAN-service policy lives here because it now has several callers; the
public-internet policy stays in ``_oidc_helpers`` next to its only consumer.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx

# Cloud-provider metadata endpoints — the classic SSRF credential-exfil
# targets. Both guards reject these unconditionally.
CLOUD_METADATA_IPS = frozenset(
    {
        # AWS / GCP / Azure / Oracle / DigitalOcean IMDS
        ipaddress.ip_address("169.254.169.254"),
        # Alibaba Cloud metadata
        ipaddress.ip_address("100.100.100.200"),
        # AWS IMDS IPv6
        ipaddress.ip_address("fd00:ec2::254"),
    }
)

# The DNS-name form of the same targets. Neither guard resolves hostnames (see
# the TOCTOU note on each), so an IP blocklist alone cannot catch these — but a
# literal-string match needs no resolution and costs nothing. These names only
# resolve inside the respective cloud, so there is no legitimate reason for any
# Bambuddy integration to point at one.
CLOUD_METADATA_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",  # GCP
        "metadata.goog",  # GCP short form
    }
)


# libc and browsers parse numeric-encoded IP forms (decimal ``2130706433``
# for 127.0.0.1, hex ``0x7f000001``) but Python's ``ipaddress.ip_address``
# raises ValueError on these, so they slip past the IP-class checks if
# not caught first. Used by both guards to reject up-front.
NUMERIC_IP_RE = re.compile(r"^(0x[0-9a-f]+|[0-9]+)$", re.I)


def unwrap_ipv4_mapped(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Return the underlying IPv4 for an IPv4-mapped IPv6 address, else return *addr*.

    ``::ffff:127.0.0.1`` and similar mapped forms must be unwrapped before
    the per-class checks (``is_private``, ``is_loopback``, …) — otherwise
    an attacker can encode a blocked IPv4 address as an IPv6 literal to
    bypass the guard.
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _assert_safe_lan_address(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    label: str,
) -> None:
    """Reject an address that is dangerous regardless of LAN topology."""
    effective = unwrap_ipv4_mapped(addr)

    if effective in CLOUD_METADATA_IPS:
        raise ValueError(f"{label} must not point to a cloud metadata endpoint")
    if effective.is_multicast or effective.is_unspecified:
        raise ValueError(f"{label} must not point to a multicast or unspecified address")


def _resolve_lan_addresses(hostname: str, port: int, *, label: str) -> tuple[str, ...]:
    """Resolve *hostname* and return addresses safe for a LAN-service request.

    Private and link-local addresses remain valid here because integrations such
    as Home Assistant and Spoolman commonly run on the same host or LAN. Only
    destinations that are invalid under every topology are rejected.
    """
    normalized_hostname = hostname.rstrip(".")
    if not normalized_hostname:
        raise ValueError(f"{label} must include a hostname")
    if NUMERIC_IP_RE.match(normalized_hostname):
        raise ValueError(f"{label} must not use numeric-encoded IP addresses; use standard dotted-decimal notation")

    try:
        addr = ipaddress.ip_address(normalized_hostname)
    except ValueError:
        try:
            address_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except (OSError, ValueError) as exc:
            raise ValueError(f"{label} hostname could not be resolved") from exc
        if not address_infos:
            raise ValueError(f"{label} hostname could not be resolved")

        addresses: list[str] = []
        for address_info in address_infos:
            sockaddr = address_info[4]
            address_text = sockaddr[0] if sockaddr else ""
            try:
                resolved = ipaddress.ip_address(address_text.split("%", 1)[0])
            except ValueError:
                raise ValueError(f"{label} hostname resolved to an invalid address") from None
            _assert_safe_lan_address(resolved, label=label)
            address = str(unwrap_ipv4_mapped(resolved))
            if address not in addresses:
                addresses.append(address)
        return tuple(addresses)

    _assert_safe_lan_address(addr, label=label)
    return (str(unwrap_ipv4_mapped(addr)),)


def resolve_safe_lan_addresses(hostname: str, port: int, *, label: str) -> tuple[str, ...]:
    """Resolve *hostname* once and return addresses allowed by the LAN policy.

    Callers that do not use httpx's transport need the same connect-time
    protection as :func:`lan_service_transport`.  In particular, the returned
    addresses must be used for the socket connection; resolving here and then
    resolving again in a different client would reintroduce a DNS-rebinding
    window.
    """
    return _resolve_lan_addresses(hostname, port, label=label)


def assert_safe_lan_service_url(
    url: str,
    *,
    label: str,
    resolve_hostname: bool = False,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
) -> None:
    """Raise ValueError if *url* is unsafe for a service that may live on the LAN.

    ``label`` names the setting in the error message ("Spoolman URL", "ntfy
    server URL", …) so the user sees which field they need to correct.

    Loopback (127.0.0.1) and RFC-1918 private ranges are deliberately
    **permitted** — Bambuddy is self-hosted and running Spoolman, ntfy,
    Bark, Home Assistant, an Obico ML endpoint or a slicer sidecar on the
    same host or home LAN is THE normal topology, not an attack. A blanket
    private-address block would break those integrations for most installs.

    What is rejected is dangerous under any topology:

    - Schemes outside *allowed_schemes*. HTTP integrations use the default
      ``http``/``https`` pair; scheme-aware LAN clients such as external
      cameras can opt into their supported protocol schemes while retaining
      the same destination policy.
    - Numeric-encoded IPv4 (decimal ``2130706433``, hex ``0x7f000001``) —
      libc and browsers resolve these, but Python's ``ipaddress`` raises
      ValueError on them, so they would slip past the checks below.
    - Cloud-provider metadata endpoints — the high-value SSRF target, and
      never a legitimate destination for any of these services.
    - Multicast and unspecified addresses — pointless as a destination and
      indicative of misuse.
    - IPv4-mapped IPv6 encodings of any of the above.

    Symbolic hostnames are accepted without DNS resolution by default so schema
    validation remains network-free and local-only names can be configured while
    offline. Callers that need an early DNS check can pass
    ``resolve_hostname=True``. Actual HTTP requests must additionally use
    ``lan_service_transport()`` below, which validates and pins the address at
    connection time so a DNS answer cannot change between validation and use.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in allowed_schemes:
        if allowed_schemes == ("http", "https"):
            raise ValueError(f"{label} must use http or https")
        schemes = ", ".join(allowed_schemes)
        raise ValueError(f"{label} must use one of: {schemes}")

    hostname = (parsed.hostname or "").lower()
    normalized_hostname = hostname.rstrip(".")

    # "http:///path" parses to an empty hostname. Never a valid destination,
    # and without this it falls through the ip_address() ValueError branch
    # below and is accepted as if it were a symbolic hostname.
    if not normalized_hostname:
        raise ValueError(f"{label} must include a hostname")

    if normalized_hostname in CLOUD_METADATA_HOSTNAMES:
        raise ValueError(f"{label} must not point to a cloud metadata endpoint")

    if NUMERIC_IP_RE.match(normalized_hostname):
        raise ValueError(f"{label} must not use numeric-encoded IP addresses; use standard dotted-decimal notation")

    try:
        addr = ipaddress.ip_address(normalized_hostname)
    except ValueError:
        if resolve_hostname:
            default_port = {"https": 443, "rtsps": 322, "rtsp": 554}.get(scheme, 80)
            _resolve_lan_addresses(
                hostname,
                parsed.port or default_port,
                label=label,
            )
        return  # symbolic hostname — checked authoritatively by the transport

    _assert_safe_lan_address(addr, label=label)


class _LanServiceAddressBackend:
    """httpcore backend that validates and pins each LAN-service connection."""

    def __init__(self, delegate=None) -> None:
        if delegate is None:
            from httpcore._backends.auto import AutoBackend

            delegate = AutoBackend()
        self._delegate = delegate

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if isinstance(host, bytes):
            host = host.decode("ascii")
        addresses = resolve_safe_lan_addresses(host, port, label="LAN service URL")
        return await self._delegate.connect_tcp(
            addresses[0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return await self._delegate.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    async def sleep(self, seconds):
        await self._delegate.sleep(seconds)


class _LanServiceTransport(httpx.AsyncHTTPTransport):
    """HTTP transport that preserves the original Host/SNI while pinning DNS."""

    def __init__(self) -> None:
        # A proxy would perform its own DNS lookup and bypass this policy.
        super().__init__(trust_env=False)
        self._pool._network_backend = _LanServiceAddressBackend()


def lan_service_transport() -> httpx.AsyncHTTPTransport:
    """Return the SSRF-safe transport for operator-configured LAN services."""
    return _LanServiceTransport()
