from __future__ import annotations

from curl_cffi import CurlOpt

from core.http_client import HTTPClient, RequestConfig


def test_http_client_uses_proxy_upstream_as_libcurl_pre_proxy():
    client = HTTPClient(
        proxy_url="http://user:pass@gate.kookeey.info:1000",
        config=RequestConfig(proxy_upstream_url="socks5h://127.0.0.1:7897"),
    )

    assert client.proxies == {
        "http": "http://user:pass@gate.kookeey.info:1000",
        "https": "http://user:pass@gate.kookeey.info:1000",
    }
    assert client.session.proxies == client.proxies
    assert client.session.curl_options[CurlOpt.PRE_PROXY] == "socks5h://127.0.0.1:7897"
    assert client.session.trust_env is False


def test_http_client_direct_mode_clears_system_proxy():
    client = HTTPClient()

    assert client.proxies is None
    assert client.session.proxies == {"http": "", "https": ""}
    assert CurlOpt.PRE_PROXY not in client.session.curl_options
    assert client.session.trust_env is False


def test_http_client_normalizes_socks5_to_socks5h_at_runtime():
    client = HTTPClient(proxy_url="socks5://user:pass@gate.kookeey.info:1000")

    assert client.proxies == {
        "http": "socks5h://user:pass@gate.kookeey.info:1000",
        "https": "socks5h://user:pass@gate.kookeey.info:1000",
    }
