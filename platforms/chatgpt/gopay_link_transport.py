"""纯协议 GoPay 提链的 curl_cffi Transport 实现。

为 ``gopay_link_protocol.Transport`` 提供真实 HTTP 通道：

- OpenAI 后端路由（chatgpt.com/backend-api）附加 access_token、设备元数据、
  oai-session-id 与 Sentinel 头；
- Stripe 路由（api.stripe.com 等）只用 request 自带头 + 相应 Content-Type。

认证、代理、超时、非 2xx 报错都在这里处理；协议逻辑本身在
``gopay_link_protocol.py``。
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit


class CurlCffiTransport:
    """用 curl_cffi 会话实现 ``Transport.send(RequestSpec) -> dict``。

    每个实例对应"一个 ChatGPT 账号 + 一条固定代理"的一次提链会话，内部复用
    ``platforms.chatgpt.payment`` 的 checkout 会话 / Sentinel / 代理工具函数。
    """

    def __init__(
        self,
        *,
        access_token: str,
        cookies: str = "",
        device_id: str = "",
        client_version: str = "",
        build_number: str = "",
        chatgpt_account_id: str = "",
        proxy: Optional[str] = None,
        country: str = "ID",
        log: Callable[[str], None] = print,
    ) -> None:
        self.access_token = str(access_token or "")
        self.cookies = str(cookies or "")
        self.device_id = str(device_id or "")
        self.client_version = str(client_version or "")
        self.build_number = str(build_number or "")
        self.chatgpt_account_id = str(chatgpt_account_id or "")
        self.proxy = proxy
        self.country = str(country or "ID").strip().upper()
        self.log = log
        self._session_id = str(uuid.uuid4())
        self._session = None
        self._sentinel_headers: dict[str, str] = {}
        self._sentinel_ready = False

    def _ensure_session(self):
        if self._session is None:
            from platforms.chatgpt import payment as _payment

            self._session = _payment._create_checkout_session(self.proxy)
            if self.device_id:
                try:
                    self._session.cookies.set(
                        "oai-did", self.device_id, domain="chatgpt.com", path="/"
                    )
                except Exception:
                    pass
        return self._session

    def _ensure_sentinel(self) -> dict[str, str]:
        if self._sentinel_ready:
            return self._sentinel_headers
        from platforms.chatgpt import payment as _payment

        session = self._ensure_session()
        # 预热：与 generate_plus_link 一致，失败不阻塞（只记日志）。
        try:
            session.get(
                _payment.CHECKOUT_WARMUP_URL,
                headers={
                    "User-Agent": _payment.DEFAULT_CHECKOUT_UA,
                    "Accept-Language": _payment.DEFAULT_CHECKOUT_ACCEPT_LANGUAGE,
                    "Accept": "application/json,text/plain,*/*",
                },
                timeout=30,
            )
        except Exception as exc:
            self.log(f"GoPay 协议提链预热瞬时失败，继续生成 Sentinel: {type(exc).__name__}")
        self._sentinel_headers = _payment._build_checkout_sentinel_headers(
            session,
            device_id=self.device_id,
            country=self.country,
            client_version=self.client_version,
            log=self.log,
        )
        self._sentinel_ready = True
        return self._sentinel_headers

    def probe_checkout_exit(self, expected_country: str) -> dict[str, str]:
        """在业务请求前确认当前提链会话的真实出口国家/IP。"""
        from platforms.chatgpt import payment as _payment

        return _payment._probe_checkout_session_exit(
            self._ensure_session(),
            expected_country=str(expected_country or self.country).strip().upper(),
            proxy=self.proxy,
            log=self.log,
        )

    def _openai_headers(self) -> dict[str, str]:
        from platforms.chatgpt import payment as _payment

        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "*/*",
            "Accept-Language": _payment.DEFAULT_CHECKOUT_ACCEPT_LANGUAGE,
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "User-Agent": _payment.DEFAULT_CHECKOUT_UA,
            "oai-language": "en-US",
            "oai-session-id": self._session_id,
        }
        if self.device_id:
            headers["oai-device-id"] = self.device_id
        if self.client_version:
            headers["oai-client-version"] = self.client_version
        if self.build_number:
            headers["oai-client-build-number"] = self.build_number
        if self.chatgpt_account_id:
            headers["Chatgpt-Account-Id"] = self.chatgpt_account_id
        headers.update(self._ensure_sentinel())
        return headers

    def send(self, request: Any) -> dict[str, Any]:
        host = (urlsplit(str(request.url or "")).hostname or "").lower()
        is_openai = (
            host == "chatgpt.com"
            or host.endswith(".chatgpt.com")
            or host.endswith(".openai.com")
        )
        headers = dict(request.headers or {})
        if is_openai:
            merged = self._openai_headers()
            merged.update(headers)  # 让 request 的 Referer / x-openai-* 覆盖默认
            headers = merged

        session = self._ensure_session()
        if getattr(request, "json", None) is not None:
            headers.setdefault("Content-Type", "application/json")
            resp = session.post(request.url, headers=headers, json=request.json, timeout=60)
        elif getattr(request, "data", None) is not None:
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            resp = session.post(
                request.url, headers=headers, data=dict(request.data), timeout=60
            )
        else:
            params = dict(request.params or ())
            resp = session.get(request.url, headers=headers, params=params, timeout=60)

        status = int(getattr(resp, "status_code", 0) or 0)
        if not (200 <= status < 300):
            raise RuntimeError(f"[{request.stage}] HTTP {status}: {self._sanitize(resp)}")
        try:
            data = resp.json()
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _sanitize(resp: Any) -> str:
        try:
            text = str(getattr(resp, "text", "") or "")
        except Exception:
            text = ""
        text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer ******", text)
        return text[:300]

    def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
