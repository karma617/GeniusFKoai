from __future__ import annotations

from core import config_store as config_store_module
from core.proxy_pool import (
    DEFAULT_FALLBACK_PROXY_URL,
    DEFAULT_PROXY_UPSTREAM_URL,
    PROXY_STRATEGY_DEFAULT_ONLY,
    PROXY_STRATEGY_DIRECT,
    PROXY_STRATEGY_POOL_ONLY,
    PROXY_STRATEGY_POOL_THEN_DEFAULT,
    get_proxy_runtime_config,
    resolve_runtime_proxy,
)
from infrastructure import config_repository as config_repository_module


class _FakeConfigStore:
    def __init__(self, data: dict[str, str] | None = None):
        self.data = dict(data or {})

    def get(self, key: str, default: str = "") -> str:
        return self.data.get(key, default)

    def get_all(self) -> dict[str, str]:
        return dict(self.data)


class _FakeDefinitions:
    def list_by_type(self, provider_type: str, enabled_only: bool = False):
        return []


def _patch_config_store(monkeypatch, data: dict[str, str] | None = None) -> None:
    fake = _FakeConfigStore(data)
    monkeypatch.setattr(config_store_module, "config_store", fake)
    monkeypatch.setattr(config_repository_module, "config_store", fake)


def test_proxy_runtime_uses_default_local_proxy_when_pool_empty(monkeypatch):
    _patch_config_store(monkeypatch, {})

    assert get_proxy_runtime_config() == {
        "strategy": PROXY_STRATEGY_POOL_THEN_DEFAULT,
        "fallback_url": DEFAULT_FALLBACK_PROXY_URL,
        "upstream_url": DEFAULT_PROXY_UPSTREAM_URL,
    }
    assert resolve_runtime_proxy(proxy_getter=lambda: None) == DEFAULT_FALLBACK_PROXY_URL


def test_proxy_runtime_strategy_variants(monkeypatch):
    _patch_config_store(
        monkeypatch,
        {
            "proxy_strategy": PROXY_STRATEGY_POOL_ONLY,
            "proxy_fallback_url": "127.0.0.1:7897",
        },
    )
    assert resolve_runtime_proxy(proxy_getter=lambda: None) is None

    _patch_config_store(
        monkeypatch,
        {
            "proxy_strategy": PROXY_STRATEGY_DEFAULT_ONLY,
            "proxy_fallback_url": "127.0.0.1:7897",
        },
    )
    assert resolve_runtime_proxy(proxy_getter=lambda: "http://pool.local:9000") == "http://127.0.0.1:7897"

    _patch_config_store(monkeypatch, {"proxy_strategy": PROXY_STRATEGY_DIRECT})
    assert resolve_runtime_proxy(proxy_getter=lambda: "http://pool.local:9000") is None


def test_proxy_runtime_explicit_proxy_wins(monkeypatch):
    _patch_config_store(monkeypatch, {"proxy_strategy": PROXY_STRATEGY_DIRECT})

    assert resolve_runtime_proxy(explicit_proxy="127.0.0.1:8888") == "http://127.0.0.1:8888"


def test_config_repository_returns_proxy_defaults(monkeypatch):
    _patch_config_store(monkeypatch, {})

    repo = config_repository_module.ConfigRepository(definitions=_FakeDefinitions())

    assert repo.get_flat()["proxy_strategy"] == PROXY_STRATEGY_POOL_THEN_DEFAULT
    assert repo.get_flat()["proxy_fallback_url"] == DEFAULT_FALLBACK_PROXY_URL
    assert repo.get_flat()["proxy_upstream_url"] == DEFAULT_PROXY_UPSTREAM_URL


def test_proxy_runtime_returns_upstream_proxy_config(monkeypatch):
    _patch_config_store(
        monkeypatch,
        {
            "proxy_strategy": PROXY_STRATEGY_POOL_THEN_DEFAULT,
            "proxy_fallback_url": "127.0.0.1:7897",
            "proxy_upstream_url": "socks5h://127.0.0.1:7897",
        },
    )

    assert get_proxy_runtime_config() == {
        "strategy": PROXY_STRATEGY_POOL_THEN_DEFAULT,
        "fallback_url": "http://127.0.0.1:7897",
        "upstream_url": "socks5h://127.0.0.1:7897",
    }
