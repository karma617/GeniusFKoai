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

    with pytest.raises(RuntimeError, match="ChatGPT"):
        tasks._resolve_chatgpt_reachable_proxy(
            platform_name="chatgpt",
            explicit_proxy="http://bad.proxy:3128",
            proxy_getter=lambda: "http://other.proxy:3128",
            logger=logger,
        )


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
