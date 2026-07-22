from __future__ import annotations

from curl_cffi import CurlOpt

from core import config_store as config_store_module
from core import http_client as http_client_module
from core import proxy_pool as proxy_pool_module
from core.http_client import HTTPClient, RequestConfig, build_cffi_proxy_request_kwargs
from platforms.chatgpt import payment as chatgpt_payment
from platforms.chatgpt import switch as chatgpt_switch
from platforms.chatgpt.http_client import OpenAIHTTPClient


class _FakeResponse:
    status_code = 200


class _FakeSession:
    trust_env = True

    def __init__(self):
        self.kwargs = None

    def request(self, method, url, **kwargs):
        self.kwargs = kwargs
        return _FakeResponse()


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


def test_http_client_request_does_not_pass_curl_options_per_request():
    session = _FakeSession()
    client = HTTPClient(
        proxy_url="socks5://user:pass@gate.kookeey.info:1000",
        config=RequestConfig(proxy_upstream_url="http://127.0.0.1:7897"),
        session=session,
    )

    response = client.get("https://chatgpt.com/backend-api/me")

    assert response.status_code == 200
    assert session.kwargs["proxies"] == {
        "http": "socks5h://user:pass@gate.kookeey.info:1000",
        "https": "socks5h://user:pass@gate.kookeey.info:1000",
    }
    assert "curl_options" not in session.kwargs


def test_openai_http_client_falls_back_to_target_proxy_when_pre_proxy_transport_fails(monkeypatch):
    calls: list[str] = []

    class _FailingSession:
        trust_env = False

        def request(self, method, url, **kwargs):
            calls.append("upstream")
            raise ConnectionError("Proxy CONNECT aborted")

        def close(self):
            return None

    class _DirectSession:
        trust_env = True

        def request(self, method, url, **kwargs):
            calls.append("direct")
            return _FakeResponse()

        def close(self):
            return None

    direct_session = _DirectSession()
    monkeypatch.setattr(http_client_module, "_PROXY_DIRECT_ROUTE_UNTIL", {})
    monkeypatch.setattr("core.http_client.Session", lambda **_kwargs: direct_session)
    client = OpenAIHTTPClient(
        proxy_url="http://user:pass@us.1024proxy.io:3000",
        config=RequestConfig(
            max_retries=2,
            proxy_upstream_url="http://127.0.0.1:7897",
        ),
    )
    client._session = _FailingSession()

    response = client.get("https://chatgpt.com/")

    assert response.status_code == 200
    assert calls == ["upstream", "direct"]
    assert client.session is direct_session
    assert client.session.trust_env is False
    assert client.config.proxy_upstream_url == ""

    next_client = OpenAIHTTPClient(
        proxy_url="http://other:pass@us.1024proxy.io:3000",
        config=RequestConfig(proxy_upstream_url="http://127.0.0.1:7897"),
    )
    assert next_client.config.proxy_upstream_url == ""
    assert next_client.config.proxy_route_upstream_url == "http://127.0.0.1:7897"


def test_openai_http_client_restores_pre_proxy_when_cached_direct_route_fails(monkeypatch):
    calls: list[str] = []
    created: list[dict] = []

    class _FailingDirectSession:
        trust_env = False

        def request(self, method, url, **kwargs):
            calls.append("direct")
            raise ConnectionError("direct route failed")

        def close(self):
            return None

    class _UpstreamSession:
        trust_env = True

        def request(self, method, url, **kwargs):
            calls.append("upstream")
            return _FakeResponse()

        def close(self):
            return None

    monkeypatch.setattr(http_client_module, "_PROXY_DIRECT_ROUTE_UNTIL", {})
    http_client_module.remember_proxy_route(
        "http://user:pass@us.1024proxy.io:3000",
        use_upstream=False,
    )

    upstream_session = _UpstreamSession()

    def _session_factory(**kwargs):
        created.append(kwargs)
        return upstream_session

    monkeypatch.setattr("core.http_client.Session", _session_factory)
    client = OpenAIHTTPClient(
        proxy_url="http://user:pass@us.1024proxy.io:3000",
        config=RequestConfig(
            max_retries=2,
            proxy_upstream_url="http://127.0.0.1:7897",
        ),
    )
    assert client.config.proxy_upstream_url == ""
    client._session = _FailingDirectSession()

    response = client.get("https://chatgpt.com/")

    assert response.status_code == 200
    assert calls == ["direct", "upstream"]
    assert client.config.proxy_upstream_url == "http://127.0.0.1:7897"
    assert created[0]["curl_options"][CurlOpt.PRE_PROXY] == "socks5h://127.0.0.1:7897"


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


def test_runtime_proxy_config_uses_local_fallback_as_upstream(monkeypatch):
    values = {
        "proxy_strategy": "pool_then_default",
        "proxy_fallback_url": "http://127.0.0.1:7897",
        "proxy_upstream_url": "",
    }

    monkeypatch.setattr(
        config_store_module.config_store,
        "get",
        lambda key, default="": values.get(key, default),
    )

    config = proxy_pool_module.get_proxy_runtime_config()

    assert config["fallback_url"] == "http://127.0.0.1:7897"
    assert config["upstream_url"] == "http://127.0.0.1:7897"


def test_cffi_proxy_request_kwargs_uses_pre_proxy():
    kwargs = build_cffi_proxy_request_kwargs(
        "socks5://user:pass@gate.kookeey.info:1000",
        proxy_upstream_url="http://127.0.0.1:7897",
    )

    assert kwargs["proxies"] == {
        "http": "socks5h://user:pass@gate.kookeey.info:1000",
        "https": "socks5h://user:pass@gate.kookeey.info:1000",
    }
    assert kwargs["curl_options"][CurlOpt.PRE_PROXY] == "socks5h://127.0.0.1:7897"


def test_cffi_proxy_request_kwargs_does_not_pre_proxy_local_target():
    kwargs = build_cffi_proxy_request_kwargs(
        "http://127.0.0.1:7897",
        proxy_upstream_url="http://127.0.0.1:7897",
    )

    assert kwargs["proxies"] == {
        "http": "http://127.0.0.1:7897",
        "https": "http://127.0.0.1:7897",
    }
    assert "curl_options" not in kwargs


def test_chatgpt_switch_cffi_requests_use_runtime_proxy_upstream(monkeypatch):
    monkeypatch.setattr(
        "core.proxy_pool.get_proxy_runtime_config",
        lambda: {
            "strategy": "pool_then_default",
            "fallback_url": "http://127.0.0.1:7897",
            "upstream_url": "http://127.0.0.1:7897",
        },
    )

    kwargs = chatgpt_switch._build_proxy_request_kwargs("socks5://user:pass@gate.kookeey.info:1000")

    assert kwargs["proxies"]["https"] == "socks5h://user:pass@gate.kookeey.info:1000"
    assert kwargs["curl_options"][CurlOpt.PRE_PROXY] == "socks5h://127.0.0.1:7897"


def test_chatgpt_payment_cffi_requests_use_runtime_proxy_upstream(monkeypatch):
    monkeypatch.setattr(
        "core.proxy_pool.get_proxy_runtime_config",
        lambda: {
            "strategy": "pool_then_default",
            "fallback_url": "http://127.0.0.1:7897",
            "upstream_url": "http://127.0.0.1:7897",
        },
    )

    kwargs = chatgpt_payment._build_proxy_request_kwargs("socks5://user:pass@gate.kookeey.info:1000")

    assert kwargs["proxies"]["https"] == "socks5h://user:pass@gate.kookeey.info:1000"
    assert kwargs["curl_options"][CurlOpt.PRE_PROXY] == "socks5h://127.0.0.1:7897"
