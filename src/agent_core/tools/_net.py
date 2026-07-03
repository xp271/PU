"""Shared network helpers for WebSearch / WebFetch tools.

SSRF host validation, shared User-Agent header, and timeout constants.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 "
    "agent-core/0.1"
)
DEFAULT_TIMEOUT = 15.0

# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------

_PRIVATE_RANGES = [
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),  # link-local (incl. cloud metadata)
    ipaddress.IPv6Network("fc00::/7"),         # unique local
    ipaddress.IPv6Network("fe80::/10"),        # link-local
]

_LOOPBACK = ipaddress.IPv4Network("127.0.0.0/8")
_LOOPBACK_V6 = ipaddress.IPv6Network("::1/128")


def assert_public_host(url: str) -> None:
    """Validate *url* scheme and reject private / reserved / loopback hosts.

    Raises :exc:`ValueError` if the scheme is not ``http``/``https`` or the
    resolved IP falls inside a private, reserved, or loopback range.
    This is defence-in-depth — callers should also use their own URL-parsing
    for report formatting.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"仅支持 http/https scheme，当前 scheme: {parsed.scheme or '<空>'}"
        )
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError(f"URL 无有效主机: {url}")

    # Resolve hostname → IP addresses (defensive DNS rebinding check)
    try:
        addrs = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"DNS 解析失败: {hostname} ({exc})") from exc

    for family, _, _, _, sockaddr in addrs:
        ip_str = sockaddr[0]

        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        if ip.is_loopback:
            raise ValueError(f"SSRF 拦截：禁止访问环回地址 ({ip_str})")
        if ip.is_link_local:
            raise ValueError(f"SSRF 拦截：禁止访问链路本地地址 ({ip_str})")

        # Check private ranges even for IPv4-mapped IPv6 addresses
        check_ip = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        if isinstance(check_ip, ipaddress.IPv4Address):
            if check_ip.is_private:
                raise ValueError(f"SSRF 拦截：禁止访问内网地址 ({ip_str})")

        if isinstance(check_ip, ipaddress.IPv6Address):
            for net in _PRIVATE_RANGES:
                if isinstance(net, ipaddress.IPv6Network) and check_ip in net:
                    raise ValueError(f"SSRF 拦截：禁止访问内网地址 ({ip_str})")

        # Extra check for cloud metadata IP
        if ip_str == "169.254.169.254":
            raise ValueError(f"SSRF 拦截：禁止访问云元数据地址 ({ip_str})")
