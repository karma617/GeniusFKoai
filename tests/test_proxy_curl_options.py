from curl_cffi import CurlOpt

from core.http_client import _proxy_curl_options

UPSTREAM = "http://127.0.0.1:7897"
UPSTREAM_NORMALIZED = "socks5h://127.0.0.1:7897"


def test_socks_main_proxy_never_gets_pre_proxy():
    # 实测 curl_cffi 在 SOCKS 主代理叠加 PRE_PROXY 时会整体忽略主代理，
    # 流量直接从本机出口出去，导致地区代理出口校验读到错误国家。
    assert _proxy_curl_options(UPSTREAM, "socks5://user:pass@gw.example.test:3000") == {}
    assert _proxy_curl_options(UPSTREAM, "socks5h://user:pass@gw.example.test:3000") == {}


def test_http_main_proxy_keeps_pre_proxy():
    options = _proxy_curl_options(UPSTREAM, "http://user:pass@gw.example.test:8080")
    assert options == {CurlOpt.PRE_PROXY: UPSTREAM_NORMALIZED}


def test_missing_main_proxy_keeps_pre_proxy():
    options = _proxy_curl_options(UPSTREAM, None)
    assert options == {CurlOpt.PRE_PROXY: UPSTREAM_NORMALIZED}


def test_empty_upstream_returns_no_options():
    assert _proxy_curl_options("", "socks5://user:pass@gw.example.test:3000") == {}
    assert _proxy_curl_options("", "http://gw.example.test:8080") == {}
