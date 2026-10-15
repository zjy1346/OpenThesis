"""Official HTTPS policy enforced again at the actual socket connection.

Host/SNI stay the disclosure hostname. DNS answers are never substituted into
the URL, and connecting does not perform a second, unchecked resolution.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import ipaddress
import socket
import ssl
from typing import Mapping, Sequence
import urllib.parse
import urllib.request


OFFICIAL_HOSTS = frozenset({
    "www.cninfo.com.cn", "static.cninfo.com.cn",
    "www1.hkexnews.hk", "www.hkexnews.hk",
})
OFFICIAL_SUFFIXES = (".cninfo.com.cn", ".hkexnews.hk")
BENCHMARK_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("2001:2::/48"))


class OfficialNetworkError(RuntimeError):
    def __init__(self, message: str, *, code: str = "SOURCE_DNS_POLICY_BLOCKED"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ResolvedOfficialTarget:
    hostname: str
    port: int
    addresses: tuple[tuple, ...]


class OfficialNetworkPolicy:
    def __init__(self, *, host_profiles: Mapping[str, Sequence[str]] | None = None,
                 connect_proxy: str = ""):
        # Profiles are explicit, exact built-in hosts, never suffix permissions.
        self.host_profiles = {}
        for host, networks in (host_profiles or {}).items():
            if host not in OFFICIAL_HOSTS or isinstance(networks, str):
                raise ValueError("OFFICIAL_NETWORK_PROFILE_INVALID")
            parsed = tuple(ipaddress.ip_network(item, strict=True) for item in networks)
            for network in parsed:
                forbidden = ("127.0.0.0/8", "169.254.0.0/16", "0.0.0.0/8", "::1/128", "fe80::/10")
                if any(network.version == block.version and network.overlaps(block)
                       for item in forbidden if (block := ipaddress.ip_network(item))):
                    raise ValueError("OFFICIAL_NETWORK_PROFILE_INVALID")
            self.host_profiles[host] = parsed
        self.connect_proxy = connect_proxy
        if connect_proxy:
            parsed = urllib.parse.urlsplit(connect_proxy)
            if (parsed.scheme != "http" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.path not in {"", "/"}
                    or parsed.query or parsed.fragment or not parsed.port):
                raise ValueError("OFFICIAL_CONNECT_PROXY_INVALID")

    @staticmethod
    def validate_url(url: str) -> tuple[str, int]:
        try:
            parsed = urllib.parse.urlsplit(url)
            host, port = (parsed.hostname or "").lower(), parsed.port if parsed.port is not None else 443
        except ValueError as error:
            raise OfficialNetworkError("unsupported disclosure source URL") from error
        allowed = host in OFFICIAL_HOSTS or any(
            host.endswith(suffix) and host != suffix[1:] for suffix in OFFICIAL_SUFFIXES)
        if (parsed.scheme != "https" or not allowed or port != 443
                or parsed.username is not None or parsed.password is not None
                or "\\" in parsed.netloc or any(character.isspace() for character in parsed.netloc)):
            raise OfficialNetworkError("unsupported disclosure source URL")
        return host, port

    def resolve(self, url: str) -> ResolvedOfficialTarget:
        host, port = self.validate_url(url)
        try:
            answers = tuple(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError as error:
            raise OfficialNetworkError("official DNS is unavailable", code="SOURCE_NETWORK_UNAVAILABLE") from error
        if not answers:
            raise OfficialNetworkError("official DNS returned no targets", code="SOURCE_NETWORK_UNAVAILABLE")
        for family, kind, protocol, _, address in answers:
            try:
                resolved = ipaddress.ip_address(address[0])
            except ValueError as error:
                raise OfficialNetworkError("invalid official DNS target") from error
            if (family not in {socket.AF_INET, socket.AF_INET6} or kind != socket.SOCK_STREAM
                    or address[1] != port or resolved.is_loopback or resolved.is_link_local
                    or resolved.is_unspecified or resolved.is_multicast
                    or getattr(resolved, "ipv4_mapped", None) is not None):
                raise OfficialNetworkError("unsafe official DNS target")
            exceptions = ((*BENCHMARK_NETWORKS, *self.host_profiles.get(host, ()))
                          if host in OFFICIAL_HOSTS else ())
            if not resolved.is_global and not any(
                    resolved.version == network.version and resolved in network for network in exceptions):
                raise OfficialNetworkError("unsafe official DNS target")
        return ResolvedOfficialTarget(host, port, answers)

    def connect(self, host: str, port: int, timeout, source_address=None):
        target = self.resolve(f"https://{host}:{port}/")
        last_error = None
        for family, kind, protocol, _, address in target.addresses:
            stream = None
            try:
                stream = socket.socket(family, kind, protocol)
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    stream.settimeout(timeout)
                if source_address:
                    stream.bind(source_address)
                stream.connect(address)
                return stream
            except OSError as error:
                last_error = error
                if stream is not None:
                    stream.close()
        raise OfficialNetworkError("official connection is unavailable", code="SOURCE_NETWORK_UNAVAILABLE") from last_error

    def https_handler(self):
        policy = self

        class Connection(http.client.HTTPSConnection):
            def __init__(self, host, **kwargs):
                super().__init__(host, **kwargs)
                if self._context.verify_mode != ssl.CERT_REQUIRED or not self._context.check_hostname:
                    raise OfficialNetworkError("official TLS verification is required")
                # An explicitly configured CONNECT proxy is the only private
                # transport path. Destination TLS/hostname verification stays.
                if policy.connect_proxy:
                    proxy = urllib.parse.urlsplit(policy.connect_proxy)
                    original_connect = self._create_connection

                    def connect_proxy(address, timeout, source_address=None):
                        if address != (proxy.hostname, proxy.port):
                            raise OfficialNetworkError("unapproved proxy target")
                        return original_connect(address, timeout, source_address)
                    self._create_connection = connect_proxy
                else:
                    self._create_connection = lambda address, timeout, source_address=None: policy.connect(
                        address[0], address[1], timeout, source_address)

        class Handler(urllib.request.HTTPSHandler):
            def https_open(self, request):
                policy.validate_url(request.full_url)
                return self.do_open(Connection, request, context=self._context)

        return Handler(context=ssl.create_default_context())

    def proxy_handler(self):
        # Do not silently inherit arbitrary environment proxies. Explicit
        # HTTP CONNECT configuration preserves certificate checks end-to-end.
        return urllib.request.ProxyHandler({"https": self.connect_proxy} if self.connect_proxy else {})
