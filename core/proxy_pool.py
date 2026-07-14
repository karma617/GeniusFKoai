"""代理池 - 从数据库读取代理，支持轮询、按区域选取和全局回退策略。"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from sqlmodel import Session, select
from .db import ProxyModel, engine
import time, threading, random
from datetime import datetime, timezone
from urllib.parse import quote, unquote


DEFAULT_FALLBACK_PROXY_URL = "http://127.0.0.1:7897"
DEFAULT_PROXY_UPSTREAM_URL = ""
PROXY_CHECK_URL = "https://cloudflare.com/cdn-cgi/trace"
PROXY_CHECK_CONCURRENCY = 20
PROXY_IMPORT_SCHEMES = {"http", "https", "socks5"}
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


def normalize_proxy_scheme(value: str | None) -> str:
    scheme = str(value or "").strip().lower()
    return scheme if scheme in PROXY_IMPORT_SCHEMES else "http"


def _normalize_host_port_auth_proxy(value: str, *, default_scheme: str = "http") -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    scheme = normalize_proxy_scheme(default_scheme)
    rest = raw
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        scheme = (scheme or default_scheme).strip().lower()
    if "@" in rest:
        return f"{scheme}://{rest}"
    parts = rest.split(":")
    if len(parts) == 4 and parts[1].isdigit():
        host, port, username, password = parts
        if host and port:
            return (
                f"{scheme}://"
                f"{quote(unquote(username), safe='')}:{quote(unquote(password), safe='')}"
                f"@{host}:{port}"
            )
    return None


def normalize_proxy_url(value: str | None, *, default_scheme: str = "http") -> str | None:
    """规范化用户输入的代理地址；注册流程可直接交给 HTTPClient 使用。"""
    proxy = str(value or "").strip()
    if not proxy:
        return None
    scheme = normalize_proxy_scheme(default_scheme)
    converted = _normalize_host_port_auth_proxy(proxy, default_scheme=scheme)
    if converted:
        return converted
    if "://" not in proxy:
        proxy = f"{scheme}://{proxy}"
    return proxy


def _proxy_url_key(value: str | None) -> str:
    return (normalize_proxy_url(value) or str(value or "").strip()).lower()


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
    upstream_url = normalize_proxy_url(
        config_store.get("proxy_upstream_url", DEFAULT_PROXY_UPSTREAM_URL)
    ) or ""
    return {"strategy": strategy, "fallback_url": fallback_url, "upstream_url": upstream_url}


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

    def report_success(self, url: str, *, region: str = "") -> None:
        with Session(engine) as s:
            p = self._find_by_url(s, url)
            if p:
                p.success_count += 1
                p.last_checked = datetime.now(timezone.utc)
                if region:
                    p.region = region
                s.add(p)
                s.commit()

    def report_fail(self, url: str) -> None:
        with Session(engine) as s:
            p = self._find_by_url(s, url)
            if p:
                p.fail_count += 1
                p.last_checked = datetime.now(timezone.utc)
                # 连续失败超过10次自动禁用
                if p.fail_count > 0 and p.success_count == 0 and p.fail_count >= 5:
                    p.is_active = False
                s.add(p)
                s.commit()

    def _find_by_url(self, session: Session, url: str) -> ProxyModel | None:
        p = session.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
        if p:
            return p
        expected = _proxy_url_key(url)
        for item in session.exec(select(ProxyModel)).all():
            if _proxy_url_key(item.url) == expected:
                return item
        return None

    def _check_one(self, url: str, *, timeout: int) -> dict:
        from core.http_client import RequestConfig
        from platforms.chatgpt.http_client import OpenAIHTTPClient

        proxy_url = normalize_proxy_url(url) or url
        result = {"url": url, "checked_url": proxy_url, "ok": False, "status_code": None, "error": "", "region": ""}
        client = None
        try:
            client = OpenAIHTTPClient(
                proxy_url=proxy_url,
                config=RequestConfig(
                    timeout=timeout,
                    max_retries=1,
                    impersonate="chrome136",
                    proxy_upstream_url=get_proxy_runtime_config()["upstream_url"],
                ),
            )
            response = client.get(PROXY_CHECK_URL, timeout=timeout)
            result["status_code"] = response.status_code
            if response.status_code == 200:
                result["ok"] = True
                for line in str(response.text or "").splitlines():
                    if line.startswith("loc="):
                        result["region"] = line.split("=", 1)[1].strip()
                        break
            else:
                result["error"] = f"HTTP {response.status_code}"
        except Exception as exc:
            result["error"] = str(exc)[:200]
        finally:
            try:
                client.close()
            except Exception:
                pass
        return result

    def check_all(self, *, concurrency: int = PROXY_CHECK_CONCURRENCY, timeout: int = 12) -> dict:
        """并发检测所有代理是否能按注册流程 HTTPClient 方式出网。"""
        with Session(engine) as s:
            proxies = s.exec(select(ProxyModel)).all()
        results = {"ok": 0, "fail": 0, "total": len(proxies), "results": []}
        if not proxies:
            return results

        workers = max(1, min(int(concurrency or 1), len(proxies)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(self._check_one, p.url, timeout=timeout): p.url
                for p in proxies
            }
            for future in as_completed(future_map):
                try:
                    item = future.result()
                except Exception as exc:
                    item = {
                        "url": future_map[future],
                        "ok": False,
                        "status_code": None,
                        "error": str(exc)[:200],
                    }
                if item["ok"]:
                    self.report_success(item["url"], region=str(item.get("region") or "").strip())
                    results["ok"] += 1
                else:
                    self.report_fail(item["url"])
                    results["fail"] += 1
                results["results"].append(item)
        return results


proxy_pool = ProxyPool()
