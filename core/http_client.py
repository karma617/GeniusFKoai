"""通用 HTTP 客户端 - 基于 curl_cffi，支持代理、重试、会话管理"""
"""
HTTP 客户端封装
基于 curl_cffi 的 HTTP 请求封装，支持代理和错误处理
"""

import time
import json
import threading
from typing import Optional, Dict, Any, Union, Tuple
from dataclasses import dataclass
import logging
from urllib.parse import urlsplit

from curl_cffi import requests as cffi_requests
from curl_cffi import CurlOpt
from curl_cffi.requests import Session, Response





logger = logging.getLogger(__name__)

_PROXY_DIRECT_ROUTE_TTL_SECONDS = 600.0
_PROXY_DIRECT_ROUTE_LOCK = threading.Lock()
_PROXY_DIRECT_ROUTE_UNTIL: dict[str, float] = {}


def _proxy_route_key(proxy_url: str | None) -> str:
    proxy = _normalize_runtime_proxy_url(proxy_url)
    if not proxy:
        return ""
    parsed = urlsplit(proxy)
    host = str(parsed.hostname or "").lower()
    port = int(parsed.port or 0)
    return f"{host}:{port}" if host and port else proxy.lower()


def preferred_proxy_upstream(proxy_url: str | None, upstream_url: str | None) -> str:
    upstream = str(upstream_url or "").strip()
    key = _proxy_route_key(proxy_url)
    if not upstream or not key:
        return upstream
    now = time.monotonic()
    with _PROXY_DIRECT_ROUTE_LOCK:
        until = float(_PROXY_DIRECT_ROUTE_UNTIL.get(key) or 0)
        if until > now:
            return ""
        _PROXY_DIRECT_ROUTE_UNTIL.pop(key, None)
    return upstream


def remember_proxy_route(proxy_url: str | None, *, use_upstream: bool) -> None:
    key = _proxy_route_key(proxy_url)
    if not key:
        return
    with _PROXY_DIRECT_ROUTE_LOCK:
        if use_upstream:
            _PROXY_DIRECT_ROUTE_UNTIL.pop(key, None)
        else:
            _PROXY_DIRECT_ROUTE_UNTIL[key] = time.monotonic() + _PROXY_DIRECT_ROUTE_TTL_SECONDS


@dataclass
class RequestConfig:
    """HTTP 请求配置"""
    timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 1.0
    impersonate: str = "chrome136"
    verify_ssl: bool = True
    follow_redirects: bool = True
    proxy_upstream_url: str = ""
    proxy_upstream_fallback_direct: bool = False
    proxy_route_upstream_url: str = ""


class HTTPClientError(Exception):
    """HTTP 客户端异常"""
    pass


def _proxy_curl_options(upstream_url: str | None) -> dict:
    upstream = _normalize_pre_proxy_url(upstream_url)
    if not upstream:
        return {}
    return {CurlOpt.PRE_PROXY: upstream}


def _normalize_pre_proxy_url(value: str | None) -> str:
    proxy = _normalize_runtime_proxy_url(value)
    lower = proxy.lower()
    if (
        lower.startswith("http://127.0.0.1")
        or lower.startswith("http://localhost")
        or lower.startswith("http://[::1]")
        or lower.startswith("http://0.0.0.0")
    ):
        return "socks5h://" + proxy.split("://", 1)[1]
    return proxy


def _is_local_runtime_proxy_url(value: str | None) -> bool:
    proxy = _normalize_runtime_proxy_url(value).lower()
    return any(marker in proxy for marker in ("127.0.0.1", "localhost", "[::1]", "0.0.0.0"))


def build_cffi_proxy_request_kwargs(
    proxy_url: str | None,
    *,
    proxy_upstream_url: str | None = "",
) -> dict[str, Any]:
    proxy = _normalize_runtime_proxy_url(proxy_url)
    if not proxy:
        return {}
    kwargs: dict[str, Any] = {"proxies": {"http": proxy, "https": proxy}}
    if not _is_local_runtime_proxy_url(proxy):
        curl_options = _proxy_curl_options(proxy_upstream_url)
        if curl_options:
            kwargs["curl_options"] = curl_options
    return kwargs


def _normalize_runtime_proxy_url(value: str | None) -> str:
    proxy = str(value or "").strip()
    if proxy.startswith("socks5://"):
        return "socks5h://" + proxy[len("socks5://"):]
    return proxy


class HTTPClient:
    """
    HTTP 客户端封装
    支持代理、重试、错误处理和会话管理
    """

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        config: Optional[RequestConfig] = None,
        session: Optional[Session] = None
    ):
        """
        初始化 HTTP 客户端

        Args:
            proxy_url: 代理 URL，如 "http://127.0.0.1:7890"
            config: 请求配置
            session: 可重用的会话对象
        """
        self.proxy_url = proxy_url
        self.config = config or RequestConfig()
        self._session = session

    @property
    def proxies(self) -> Optional[Dict[str, str]]:
        """获取代理配置"""
        if not self.proxy_url:
            return None
        proxy_url = _normalize_runtime_proxy_url(self.proxy_url)
        return {
            "http": proxy_url,
            "https": proxy_url,
        }

    @property
    def session(self) -> Session:
        """获取会话对象（单例）"""
        if self._session is None:
            self._session = self._create_session(self.config.proxy_upstream_url)
        return self._session

    def _create_session(self, upstream_url: str | None) -> Session:
        session = Session(
            proxies=self.proxies or {"http": "", "https": ""},
            impersonate=self.config.impersonate,
            verify=self.config.verify_ssl,
            timeout=self.config.timeout,
            curl_options=_proxy_curl_options(upstream_url),
        )
        # Avoid implicit Windows/system HTTP(S)_PROXY routing before the selected proxy.
        session.trust_env = False
        return session

    def request(
        self,
        method: str,
        url: str,
        **kwargs
    ) -> Response:
        """
        发送 HTTP 请求

        Args:
            method: HTTP 方法 (GET, POST, PUT, DELETE, etc.)
            url: 请求 URL
            **kwargs: 其他请求参数

        Returns:
            Response 对象

        Raises:
            HTTPClientError: 请求失败
        """
        # 设置默认参数
        kwargs.setdefault("timeout", self.config.timeout)
        kwargs.setdefault("allow_redirects", self.config.follow_redirects)

        # 添加代理配置
        if self.proxies and "proxies" not in kwargs:
            kwargs["proxies"] = self.proxies
        elif "proxies" not in kwargs:
            kwargs["proxies"] = {"http": "", "https": ""}
        last_exception = None
        direct_fallback_attempted = False
        for attempt in range(self.config.max_retries):
            try:
                response = self.session.request(method, url, **kwargs)
                if self.config.proxy_upstream_fallback_direct and self.proxy_url:
                    remember_proxy_route(
                        self.proxy_url,
                        use_upstream=bool(str(self.config.proxy_upstream_url or "").strip()),
                    )

                # 检查响应状态码
                if response.status_code >= 400:
                    logger.warning(
                        f"HTTP {response.status_code} for {method} {url}"
                        f" (attempt {attempt + 1}/{self.config.max_retries})"
                    )

                    # 如果是服务器错误，重试
                    if response.status_code >= 500 and attempt < self.config.max_retries - 1:
                        time.sleep(self.config.retry_delay * (attempt + 1))
                        continue

                return response

            except (cffi_requests.RequestsError, ConnectionError, TimeoutError) as e:
                last_exception = e
                logger.warning(
                    f"请求失败: {method} {url} (attempt {attempt + 1}/{self.config.max_retries}): {e}"
                )

                if (
                    not direct_fallback_attempted
                    and self.config.proxy_upstream_fallback_direct
                    and str(self.config.proxy_route_upstream_url or self.config.proxy_upstream_url or "").strip()
                    and self.proxy_url
                    and not _is_local_runtime_proxy_url(self.proxy_url)
                ):
                    direct_fallback_attempted = True
                    direct_session = None
                    current_upstream = str(self.config.proxy_upstream_url or "").strip()
                    route_upstream = str(
                        self.config.proxy_route_upstream_url or current_upstream
                    ).strip()
                    alternate_upstream = "" if current_upstream else route_upstream
                    try:
                        direct_session = self._create_session(alternate_upstream)
                        response = direct_session.request(method, url, **kwargs)
                        try:
                            self.session.close()
                        except Exception:
                            pass
                        self._session = direct_session
                        self.config.proxy_upstream_url = alternate_upstream
                        remember_proxy_route(
                            self.proxy_url,
                            use_upstream=bool(alternate_upstream),
                        )
                        route_label = "本地中转" if alternate_upstream else "目标代理直连"
                        logger.warning(f"代理链路失败，已自动切换为{route_label}: {method} {url}")
                        if response.status_code >= 500 and attempt < self.config.max_retries - 1:
                            time.sleep(self.config.retry_delay * (attempt + 1))
                            continue
                        return response
                    except (cffi_requests.RequestsError, ConnectionError, TimeoutError) as direct_error:
                        last_exception = direct_error
                        if direct_session is not None:
                            try:
                                direct_session.close()
                            except Exception:
                                pass

                if attempt < self.config.max_retries - 1:
                    time.sleep(self.config.retry_delay * (attempt + 1))
                else:
                    break

        raise HTTPClientError(
            f"请求失败，最大重试次数已达: {method} {url} - {last_exception}"
        )

    def get(self, url: str, **kwargs) -> Response:
        """发送 GET 请求"""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, data: Any = None, json: Any = None, **kwargs) -> Response:
        """发送 POST 请求"""
        return self.request("POST", url, data=data, json=json, **kwargs)

    def put(self, url: str, data: Any = None, json: Any = None, **kwargs) -> Response:
        """发送 PUT 请求"""
        return self.request("PUT", url, data=data, json=json, **kwargs)

    def delete(self, url: str, **kwargs) -> Response:
        """发送 DELETE 请求"""
        return self.request("DELETE", url, **kwargs)

    def head(self, url: str, **kwargs) -> Response:
        """发送 HEAD 请求"""
        return self.request("HEAD", url, **kwargs)

    def options(self, url: str, **kwargs) -> Response:
        """发送 OPTIONS 请求"""
        return self.request("OPTIONS", url, **kwargs)

    def patch(self, url: str, data: Any = None, json: Any = None, **kwargs) -> Response:
        """发送 PATCH 请求"""
        return self.request("PATCH", url, data=data, json=json, **kwargs)

    def download_file(self, url: str, filepath: str, chunk_size: int = 8192) -> None:
        """
        下载文件

        Args:
            url: 文件 URL
            filepath: 保存路径
            chunk_size: 块大小

        Raises:
            HTTPClientError: 下载失败
        """
        try:
            response = self.get(url, stream=True)
            response.raise_for_status()

            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)

        except Exception as e:
            raise HTTPClientError(f"下载文件失败: {url} - {e}")

    def check_proxy(self, test_url: str = "https://httpbin.org/ip") -> bool:
        """
        检查代理是否可用

        Args:
            test_url: 测试 URL

        Returns:
            bool: 代理是否可用
        """
        if not self.proxy_url:
            return False

        try:
            response = self.get(test_url, timeout=10)
            return response.status_code == 200
        except Exception:
            return False

    def close(self):
        """关闭会话"""
        if self._session:
            self._session.close()
            self._session = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
