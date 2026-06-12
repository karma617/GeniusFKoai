"""代理池 - 从数据库读取代理，支持轮询、按区域选取和全局回退策略。"""
from typing import Optional
from sqlmodel import Session, select
from .db import ProxyModel, engine
import time, threading, random
from datetime import datetime, timezone


DEFAULT_FALLBACK_PROXY_URL = "http://127.0.0.1:7897"
PROXY_STRATEGY_POOL_THEN_DEFAULT = "pool_then_default"
PROXY_STRATEGY_POOL_ONLY = "pool_only"
PROXY_STRATEGY_DEFAULT_ONLY = "default_only"
PROXY_STRATEGY_DIRECT = "direct"
PROXY_STRATEGIES = {
    PROXY_STRATEGY_POOL_THEN_DEFAULT,
    PROXY_STRATEGY_POOL_ONLY,
    PROXY_STRATEGY_DEFAULT_ONLY,
    PROXY_STRATEGY_DIRECT,
}


def normalize_proxy_url(value: str | None) -> str | None:
    """规范化用户输入的代理地址；无协议时默认补 http://。"""
    proxy = str(value or "").strip()
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = f"http://{proxy}"
    return proxy


def get_proxy_runtime_config() -> dict[str, str]:
    """读取全局代理策略配置。

    strategy:
      - pool_then_default: 先取代理池，池空则用 fallback_url
      - pool_only: 只取代理池
      - default_only: 只用 fallback_url
      - direct: 不使用代理
    """
    from core.config_store import config_store

    strategy = str(
        config_store.get("proxy_strategy", PROXY_STRATEGY_POOL_THEN_DEFAULT) or ""
    ).strip()
    if strategy not in PROXY_STRATEGIES:
        strategy = PROXY_STRATEGY_POOL_THEN_DEFAULT
    fallback_url = normalize_proxy_url(
        config_store.get("proxy_fallback_url", DEFAULT_FALLBACK_PROXY_URL)
    ) or ""
    return {"strategy": strategy, "fallback_url": fallback_url}


def resolve_runtime_proxy(
    *,
    explicit_proxy: str | None = None,
    proxy_getter=None,
    region: str = "",
) -> str | None:
    """按全局策略解析实际使用的代理。

    显式传入的代理优先级最高；否则按用户在代理资源页选择的方式取代理。
    """
    explicit = normalize_proxy_url(explicit_proxy)
    if explicit:
        return explicit

    config = get_proxy_runtime_config()
    strategy = config["strategy"]
    fallback_url = config["fallback_url"]

    if strategy == PROXY_STRATEGY_DIRECT:
        return None
    if strategy == PROXY_STRATEGY_DEFAULT_ONLY:
        return fallback_url or None

    getter = proxy_getter
    if getter is None:
        getter = lambda: proxy_pool.get_next(region=region)
    pooled = normalize_proxy_url(getter())
    if pooled:
        return pooled
    if strategy == PROXY_STRATEGY_POOL_THEN_DEFAULT:
        return fallback_url or None
    return None


class ProxyPool:
    def __init__(self):
        self._index = 0
        self._lock = threading.Lock()

    def get_next(self, region: str = "") -> Optional[str]:
        """获取下一个可用代理。

        优先级:
          1. 动态代理 provider（如果已配置且启用）
          2. 静态代理池里 region 匹配的代理
          3. 静态代理池里**任意**可用代理（软回退——region 不匹配总比无代理强）
        """
        # 1. 尝试动态代理
        try:
            from core.proxy_providers import get_dynamic_proxy
            dynamic = get_dynamic_proxy()
            if dynamic:
                return dynamic
        except Exception:
            pass

        # 2/3. 静态代理池：先按 region 严格匹配，没有再回退到任意代理
        with Session(engine) as s:
            all_active = s.exec(
                select(ProxyModel).where(ProxyModel.is_active == True)
            ).all()
            if not all_active:
                return None
            preferred = (
                [p for p in all_active if (p.region or "") == region]
                if region
                else list(all_active)
            )
            pool = preferred if preferred else list(all_active)
            pool.sort(
                key=lambda p: p.success_count / max(p.success_count + p.fail_count, 1),
                reverse=True,
            )
            with self._lock:
                idx = self._index % len(pool)
                self._index += 1
            return pool[idx].url

    def report_success(self, url: str) -> None:
        with Session(engine) as s:
            p = s.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
            if p:
                p.success_count += 1
                p.last_checked = datetime.now(timezone.utc)
                s.add(p)
                s.commit()

    def report_fail(self, url: str) -> None:
        with Session(engine) as s:
            p = s.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
            if p:
                p.fail_count += 1
                p.last_checked = datetime.now(timezone.utc)
                # 连续失败超过10次自动禁用
                if p.fail_count > 0 and p.success_count == 0 and p.fail_count >= 5:
                    p.is_active = False
                s.add(p)
                s.commit()

    def check_all(self) -> dict:
        """检测所有代理可用性"""
        import requests
        with Session(engine) as s:
            proxies = s.exec(select(ProxyModel)).all()
        results = {"ok": 0, "fail": 0}
        for p in proxies:
            try:
                r = requests.get("https://httpbin.org/ip",
                                 proxies={"http": p.url, "https": p.url},
                                 timeout=8)
                if r.status_code == 200:
                    self.report_success(p.url)
                    results["ok"] += 1
                    continue
            except Exception:
                pass
            self.report_fail(p.url)
            results["fail"] += 1
        return results


proxy_pool = ProxyPool()
