from unittest.mock import MagicMock, patch
import socket
import ssl

import pytest

from openthesis.official_network import OfficialNetworkError, OfficialNetworkPolicy


def answer(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
    return (family, socket.SOCK_STREAM, 6, "", sockaddr)


@pytest.mark.parametrize("address", ["198.18.0.7", "2001:2::4f", "1.1.1.1", "2606:4700:4700::1111"])
def test_exact_official_dual_stack_targets(address):
    with patch("socket.getaddrinfo", return_value=[answer(address)]):
        assert OfficialNetworkPolicy().resolve("https://static.cninfo.com.cn/file.pdf").hostname == "static.cninfo.com.cn"


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1"])
def test_one_private_answer_rejects_entire_set(address):
    with patch("socket.getaddrinfo", return_value=[answer("1.1.1.1"), answer(address)]):
        with pytest.raises(OfficialNetworkError) as error:
            OfficialNetworkPolicy().resolve("https://static.cninfo.com.cn/")
        assert error.value.code == "SOURCE_DNS_POLICY_BLOCKED"


@pytest.mark.parametrize("url", [
    "http://static.cninfo.com.cn/", "https://static.cninfo.com.cn:444/",
    "https://static.cninfo.com.cn:0/", "https://user@static.cninfo.com.cn/",
    "https://static.cninfo.com.cn.evil.test/", "https://[2001:2::1]/",
])
def test_url_policy_checks_scheme_identity_credentials_and_port(url):
    with pytest.raises(OfficialNetworkError):
        OfficialNetworkPolicy.validate_url(url)


def test_subdomain_has_no_benchmark_exception():
    with patch("socket.getaddrinfo", return_value=[answer("2001:2::4f")]):
        with pytest.raises(OfficialNetworkError):
            OfficialNetworkPolicy().resolve("https://cdn.static.cninfo.com.cn/")


def test_connect_pins_validated_sockaddr_without_second_dns_lookup():
    stream = MagicMock()
    with patch("socket.getaddrinfo", side_effect=[[answer("1.1.1.1")], [answer("127.0.0.1")]]) as dns, patch("socket.socket", return_value=stream):
        assert OfficialNetworkPolicy().connect("static.cninfo.com.cn", 443, 5) is stream
        stream.connect.assert_called_once_with(("1.1.1.1", 443))
        assert dns.call_count == 1


def test_profile_is_exact_host_and_cannot_authorize_loopback():
    with pytest.raises(ValueError):
        OfficialNetworkPolicy(host_profiles={"static.cninfo.com.cn": ["0.0.0.0/0"]})
    policy = OfficialNetworkPolicy(host_profiles={"static.cninfo.com.cn": ["10.8.0.0/24"]})
    with patch("socket.getaddrinfo", return_value=[answer("10.8.0.2")]):
        policy.resolve("https://static.cninfo.com.cn/")
        with pytest.raises(OfficialNetworkError):
            policy.resolve("https://www.cninfo.com.cn/")


def test_tls_defaults_and_proxy_are_explicit_not_environment_inherited():
    handler = OfficialNetworkPolicy().https_handler()
    assert handler._context.verify_mode == ssl.CERT_REQUIRED
    assert handler._context.check_hostname
    with patch.dict("os.environ", {"https_proxy": "http://127.0.0.1:9999"}):
        assert OfficialNetworkPolicy().proxy_handler().proxies == {}
    assert OfficialNetworkPolicy(connect_proxy="http://127.0.0.1:7890").proxy_handler().proxies == {"https": "http://127.0.0.1:7890"}


def test_socket_creation_failure_is_typed_and_does_not_escape_search_policy():
    with patch("socket.getaddrinfo", return_value=[answer("1.1.1.1")]), patch("socket.socket", side_effect=OSError("unavailable")):
        with pytest.raises(OfficialNetworkError) as error:
            OfficialNetworkPolicy().connect("static.cninfo.com.cn", 443, 5)
        assert error.value.code == "SOURCE_NETWORK_UNAVAILABLE"
