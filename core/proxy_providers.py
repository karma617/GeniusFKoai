"""动态代理 IP 提供者 — 具体实现已迁移到 providers/proxy/

支持两种模式:
  1. 静态代理: 从数据库读取固定代理列表（现有逻辑）
  2. 动态代理: 从第三方 API 实时获取代理 IP

动态代理 provider 通过 provider_settings 配置；如果未配置则自动回退到静态代理池。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class BaseProxyProvider(ABC):
    """动态代理提供者基类。"""

    @abstractmethod
    def get_proxy(self) -> Optional[str]:
        """获取一个代理 URL，格式: http://host:port 或 http://user:pass@host:port。
        返回 None 表示无可用代理。"""
        ...


# ---------------------------------------------------------------------------
# Lazy re-exports for backward compatibility
# (concrete classes now live under providers/proxy/)
# ---------------------------------------------------------------------------
_LAZY_IMPORTS = {
    "ApiExtractProvider": "providers.proxy.api_extract",
    "RotatingProxyProvider": "providers.proxy.rotating_gateway",
    "ClashProxyProvider": "providers.proxy.clash",
}


def __getattr__(name: str):
    module_path = _LAZY_IMPORTS.get(name)
    if module_path is not None:
        import importlib
        mod = importlib.import_module(module_path)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_proxy_provider(provider_key: str, config: dict) -> BaseProxyProvider:
    """根据 provider_key 和配置创建代理提供者。"""
    if provider_key == "api_extract":
        api_url = config.get("proxy_api_url", "")
        if not api_url:
            raise RuntimeError("动态代理未配置 API URL")
        provider_cls = __getattr__("ApiExtractProvider")
        return provider_cls(
            api_url=api_url,
            protocol=config.get("proxy_protocol", "http"),
            username=config.get("proxy_username", ""),
            password=config.get("proxy_password", ""),
        )

    if provider_key == "rotating_gateway":
        gateway = config.get("proxy_gateway_url", "")
        if not gateway:
            raise RuntimeError("旋转代理未配置网关地址")
        provider_cls = __getattr__("RotatingProxyProvider")
        return provider_cls(gateway_url=gateway)

    if provider_key == "clash":
        provider_cls = __getattr__("ClashProxyProvider")
        return provider_cls.from_config(config)

    raise RuntimeError(f"未知的代理 provider: {provider_key}")


def get_dynamic_proxy(extra: dict | None = None) -> Optional[str]:
    """尝试从配置的动态代理 provider 获取代理。

    如果未配置动态代理，返回 None（回退到静态代理池）。
    """
    try:
        from infrastructure.provider_settings_repository import ProviderSettingsRepository
        repo = ProviderSettingsRepository()
        settings = repo.list_enabled("proxy")
        for setting in settings:
            if not setting.enabled:
                continue
            config = setting.get_config()
            auth = setting.get_auth()
            merged = {**config, **auth, **(extra or {})}
            try:
                provider = create_proxy_provider(setting.provider_key, merged)
                proxy = provider.get_proxy()
                if proxy:
                    return proxy
            except Exception as exc:
                logger.debug(f"[ProxyProvider] {setting.provider_key} 获取失败: {exc}")
                continue
    except Exception:
        pass
    return None


def prepare_dynamic_proxy_for_task(concurrency: int, extra: dict | None = None) -> list[str]:
    """让已启用的动态代理 provider 做任务级准备。"""
    try:
        from infrastructure.provider_settings_repository import ProviderSettingsRepository
        repo = ProviderSettingsRepository()
        settings = repo.list_enabled("proxy")
        for setting in settings:
            config = setting.get_config()
            auth = setting.get_auth()
            merged = {**config, **auth, **(extra or {})}
            try:
                provider = create_proxy_provider(setting.provider_key, merged)
                prepare = getattr(provider, "prepare_for_concurrency", None)
                if callable(prepare):
                    try:
                        prepared = list(prepare(concurrency, refresh=True) or [])
                    except TypeError:
                        prepared = list(prepare(concurrency) or [])
                    if prepared:
                        return prepared
            except Exception as exc:
                logger.debug(f"[ProxyProvider] {setting.provider_key} 任务级准备失败: {exc}")
                raise
    except Exception:
        raise
    return []


def refresh_dynamic_proxy(proxy: str, extra: dict | None = None) -> str:
    """刷新已准备的动态代理入口，返回刷新后的代理地址。"""
    proxy_value = str(proxy or "").strip()
    if not proxy_value:
        return ""
    try:
        from infrastructure.provider_settings_repository import ProviderSettingsRepository
        repo = ProviderSettingsRepository()
        settings = repo.list_enabled("proxy")
        for setting in settings:
            config = setting.get_config()
            auth = setting.get_auth()
            merged = {**config, **auth, **(extra or {})}
            try:
                provider = create_proxy_provider(setting.provider_key, merged)
                refresh = getattr(provider, "refresh_prepared_proxy", None)
                if callable(refresh):
                    refreshed = str(refresh(proxy_value) or "").strip()
                    if refreshed:
                        return refreshed
            except Exception as exc:
                logger.debug(f"[ProxyProvider] {setting.provider_key} 刷新代理失败: {exc}")
                raise
    except Exception:
        raise
    return proxy_value
