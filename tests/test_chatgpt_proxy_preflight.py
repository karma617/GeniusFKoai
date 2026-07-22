from __future__ import annotations

import pytest

from application import tasks


class _Logger:
    def __init__(self):
        self.messages = []

    def log(self, message, level="info"):
        self.messages.append((level, message))


def test_chatgpt_proxy_preflight_switches_to_next_reachable_proxy(monkeypatch):
    candidates = iter(["http://bad.proxy:3128", "http://good.proxy:3128"])
    failures = []

    def fake_resolve(platform_name, *, explicit_proxy, proxy_getter):
        return next(candidates)

    def fake_preflight(proxy, *, timeout=12):
        return (proxy == "http://good.proxy:3128", "ok" if proxy.endswith("good.proxy:3128") else "connect reset")

    monkeypatch.setattr(tasks, "_resolve_registration_proxy_for_platform", fake_resolve)
    monkeypatch.setattr(tasks, "_chatgpt_proxy_preflight", fake_preflight)
    monkeypatch.setattr("core.proxy_pool.proxy_pool.report_fail", lambda proxy: failures.append(proxy))
    logger = _Logger()

    resolved = tasks._resolve_chatgpt_reachable_proxy(
        platform_name="chatgpt",
        explicit_proxy=None,
        proxy_getter=lambda: None,
        logger=logger,
    )

    assert resolved == "http://good.proxy:3128"
    assert failures == ["http://bad.proxy:3128"]
    assert any(level == "warning" and "ChatGPT" in message for level, message in logger.messages)


def test_chatgpt_proxy_preflight_does_not_replace_explicit_proxy(monkeypatch):
    def fake_resolve(platform_name, *, explicit_proxy, proxy_getter):
        return explicit_proxy

    monkeypatch.setattr(tasks, "_resolve_registration_proxy_for_platform", fake_resolve)
    monkeypatch.setattr(tasks, "_chatgpt_proxy_preflight", lambda proxy, *, timeout=12: (False, "connect reset"))
    logger = _Logger()

    resolved = tasks._resolve_chatgpt_reachable_proxy(
        platform_name="chatgpt",
        explicit_proxy="http://bad.proxy:3128",
        proxy_getter=lambda: "http://other.proxy:3128",
        logger=logger,
    )

    assert resolved == "http://bad.proxy:3128"
    assert any(level == "warning" and "ChatGPT" in message for level, message in logger.messages)


def test_chatgpt_proxy_preflight_uses_runtime_pre_proxy(monkeypatch):
    captured = {}

    class _Response:
        status_code = 200

    class _Client:
        def __init__(self, proxy_url=None, config=None):
            captured["proxy_url"] = proxy_url
            captured["config"] = config
            config.proxy_upstream_url = "http://127.0.0.1:7897"
            self.config = config

        def get(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _Response()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr("platforms.chatgpt.http_client.OpenAIHTTPClient", _Client)

    ok, detail = tasks._chatgpt_proxy_preflight("socks5://user:pass@gate.kookeey.info:1000")

    assert ok is True
    assert detail == "HTTP 200 (本地中转)"
    assert captured["url"] == "https://chatgpt.com/cdn-cgi/trace"
    assert captured["proxy_url"] == "socks5://user:pass@gate.kookeey.info:1000"
    assert captured["config"].max_retries == 2
    assert captured["closed"] is True


def test_chatgpt_proxy_preflight_refreshes_local_node_on_transient_tls_error(monkeypatch):
    failures = []
    preflight_calls = []
    refreshes = []

    monkeypatch.setattr(
        tasks,
        "_resolve_registration_proxy_for_platform",
        lambda platform_name, **kwargs: "socks5://user:pass@us.1024proxy.io:3000",
    )
    results = iter([
        (
            False,
            "SSLError: Failed to perform, curl: (35) TLS connect error: "
            "OPENSSL_internal:invalid library (0)",
        ),
        (True, "HTTP 200"),
    ])

    def fake_preflight(proxy, *, timeout=12):
        preflight_calls.append(proxy)
        return next(results)

    def fake_refresh(**kwargs):
        refreshes.append(kwargs)
        return True

    monkeypatch.setattr(tasks, "_chatgpt_proxy_preflight", fake_preflight)
    monkeypatch.setattr(tasks, "_refresh_chatgpt_local_proxy_node", fake_refresh)
    monkeypatch.setattr("core.proxy_pool.proxy_pool.report_fail", lambda proxy: failures.append(proxy))
    logger = _Logger()

    resolved = tasks._resolve_chatgpt_reachable_proxy(
        platform_name="chatgpt",
        explicit_proxy=None,
        proxy_getter=lambda: None,
        logger=logger,
    )

    assert resolved == "socks5://user:pass@us.1024proxy.io:3000"
    assert preflight_calls == [
        "socks5://user:pass@us.1024proxy.io:3000",
        "socks5://user:pass@us.1024proxy.io:3000",
    ]
    assert len(refreshes) == 1
    assert failures == []
    assert any("已切换本地 Clash 节点后重试" in message for _level, message in logger.messages)


def test_chatgpt_protocol_preflight_switches_proxy_after_transient_tls_retry(monkeypatch):
    candidates = iter([
        "http://bad.proxy:3128",
        "http://good.proxy:3128",
    ])
    failures = []
    preflight_calls = []
    refreshes = []

    def fake_resolve(platform_name, *, explicit_proxy, proxy_getter):
        return next(candidates)

    results = iter([
        (
            False,
            "HTTPClientError: Failed to perform, curl: (56) Proxy CONNECT aborted",
        ),
        (
            False,
            "HTTPClientError: Failed to perform, curl: (56) Proxy CONNECT aborted",
        ),
        (True, "HTTP 200"),
    ])

    def fake_preflight(proxy, *, timeout=12):
        preflight_calls.append(proxy)
        return next(results)

    def fake_refresh(**kwargs):
        refreshes.append(kwargs)
        return True

    monkeypatch.setattr(tasks, "_resolve_registration_proxy_for_platform", fake_resolve)
    monkeypatch.setattr(tasks, "_chatgpt_proxy_preflight", fake_preflight)
    monkeypatch.setattr(tasks, "_refresh_chatgpt_local_proxy_node", fake_refresh)
    monkeypatch.setattr("core.proxy_pool.proxy_pool.report_fail", lambda proxy: failures.append(proxy))
    logger = _Logger()

    resolved = tasks._resolve_chatgpt_reachable_proxy(
        platform_name="chatgpt",
        explicit_proxy=None,
        proxy_getter=lambda: None,
        logger=logger,
        continue_on_transient_failure=False,
    )

    assert resolved == "http://good.proxy:3128"
    assert preflight_calls == [
        "http://bad.proxy:3128",
        "http://bad.proxy:3128",
        "http://good.proxy:3128",
    ]
    assert len(refreshes) == 1
    assert failures == ["http://bad.proxy:3128"]
    assert any("协议注册代理预检遇到传输/TLS异常" in message for _level, message in logger.messages)


def test_chatgpt_proxy_preflight_keeps_proxy_when_local_refresh_fails(monkeypatch):
    monkeypatch.setattr(
        tasks,
        "_resolve_registration_proxy_for_platform",
        lambda platform_name, **kwargs: "socks5://user:pass@us.1024proxy.io:3000",
    )
    monkeypatch.setattr(
        tasks,
        "_chatgpt_proxy_preflight",
        lambda proxy, *, timeout=12: (
            False,
            "SSLError: Failed to perform, curl: (35) TLS connect error: "
            "OPENSSL_internal:invalid library (0)",
        ),
    )
    monkeypatch.setattr(tasks, "_refresh_chatgpt_local_proxy_node", lambda **kwargs: False)
    logger = _Logger()

    resolved = tasks._resolve_chatgpt_reachable_proxy(
        platform_name="chatgpt",
        explicit_proxy=None,
        proxy_getter=lambda: None,
        logger=logger,
    )

    assert resolved == "socks5://user:pass@us.1024proxy.io:3000"
    assert any("继续使用浏览器真实流程" in message for _level, message in logger.messages)


def test_proxy_preflight_skips_non_chatgpt_platform(monkeypatch):
    checked = False

    def fake_preflight(proxy, *, timeout=12):
        nonlocal checked
        checked = True
        return True, "ok"

    monkeypatch.setattr(
        tasks,
        "_resolve_registration_proxy_for_platform",
        lambda platform_name, **kwargs: "http://proxy.local:3128",
    )
    monkeypatch.setattr(tasks, "_chatgpt_proxy_preflight", fake_preflight)

    resolved = tasks._resolve_chatgpt_reachable_proxy(
        platform_name="windsurf",
        explicit_proxy=None,
        proxy_getter=lambda: None,
        logger=_Logger(),
    )

    assert resolved == "http://proxy.local:3128"
    assert checked is False
