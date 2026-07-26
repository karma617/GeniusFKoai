"""SMSBower 邮箱接码（Google/Gmail）— 通过 getActivation/getCode 自动取号收码。"""
from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urlencode, urlparse

import requests

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link, _normalize_api_base_url

logger = logging.getLogger(__name__)

DEFAULT_SMSBOWER_MAIL_API_URL = "https://smsbower.page"
DEFAULT_SMSBOWER_MAIL_SERVICE = "dr"  # OpenAI (ChatGPT)
DEFAULT_SMSBOWER_MAIL_DOMAIN = "gmail.com"


class SmsBowerMailMailbox(BaseMailbox):
    """SMSBower 第三方谷歌邮箱：注册时 getActivation 取邮箱，wait_for_code 轮询 getCode。"""

    def __init__(
        self,
        api_url: str = "",
        api_key: str = "",
        service: str = "",
        domain: str = "",
        alias: str | int | bool = "0",
        max_price: str | float | None = None,
        ref: str = "",
        poll_interval: str | float | int = 3,
        proxy: str | None = None,
    ):
        self.api = _normalize_api_base_url(
            api_url,
            default=DEFAULT_SMSBOWER_MAIL_API_URL,
            label="SMSBower Mail API URL",
        )
        self.api_key = str(api_key or "").strip()
        self.service = str(service or DEFAULT_SMSBOWER_MAIL_SERVICE).strip() or DEFAULT_SMSBOWER_MAIL_SERVICE
        self.domain = str(domain or DEFAULT_SMSBOWER_MAIL_DOMAIN).strip() or DEFAULT_SMSBOWER_MAIL_DOMAIN
        self.alias = self._normalize_alias(alias)
        self.max_price = self._optional_float(max_price)
        self.ref = str(ref or "").strip()
        try:
            self.poll_interval = max(1.0, float(poll_interval or 3))
        except Exception:
            self.poll_interval = 3.0
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._session = requests.Session()
        if self.proxy:
            self._session.proxies.update(self.proxy)

    @staticmethod
    def _normalize_alias(value: str | int | bool) -> str:
        text = str(value if value is not None else "0").strip().lower()
        if text in {"1", "true", "yes", "on", "y"}:
            return "1"
        return "0"

    @staticmethod
    def _optional_float(value: str | float | None) -> float | None:
        text = str(value if value is not None else "").strip()
        if not text:
            return None
        try:
            number = float(text)
        except Exception:
            return None
        if number < 0:
            return None
        return number

    def _assert_ready(self) -> None:
        if not self.api_key:
            raise RuntimeError("SMSBower 邮箱未配置 API Key")
        if not self.service:
            raise RuntimeError("SMSBower 邮箱未配置 service")
        if not self.domain:
            raise RuntimeError("SMSBower 邮箱未配置 domain")

    def _request_json(self, path: str, params: dict[str, Any], *, timeout: float = 30.0) -> dict:
        query = {k: v for k, v in params.items() if v is not None and str(v) != ""}
        query["api_key"] = self.api_key
        url = f"{self.api.rstrip('/')}/{path.lstrip('/')}"
        response = self._session.get(url, params=query, timeout=timeout)
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(
                f"SMSBower 响应非 JSON: HTTP {response.status_code} body={response.text[:300]}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"SMSBower 响应结构异常: {payload!r}")
        return payload

    @staticmethod
    def _error_message(payload: dict) -> str:
        return str(
            payload.get("error")
            or payload.get("message")
            or payload.get("msg")
            or payload
        ).strip()

    def _set_status(self, mail_id: str | int, status: int) -> bool:
        mail_id = str(mail_id or "").strip()
        if not mail_id:
            return False
        try:
            payload = self._request_json(
                "/api/mail/setStatus",
                {"id": mail_id, "status": int(status)},
                timeout=20,
            )
            ok = str(payload.get("status") or "").strip() in {"1", "success", "ok", "true"}
            if not ok and payload.get("status") is True:
                ok = True
            if not ok and int(payload.get("status") or 0) == 1:
                ok = True
            if not ok:
                logger.warning("SMSBower setStatus 失败 id=%s status=%s payload=%s", mail_id, status, payload)
            return bool(ok)
        except Exception as exc:
            logger.warning("SMSBower setStatus 异常 id=%s status=%s err=%s", mail_id, status, exc)
            return False

    def peek_email(self) -> str:
        """测试配置：查价格库存，不真正锁定邮箱。"""
        self._assert_ready()
        payload = self._request_json(
            "/api/mail/getPriceRests",
            {"service": self.service, "domain": self.domain},
            timeout=20,
        )
        if int(payload.get("status") or 0) != 1 and str(payload.get("status") or "") not in {"1", "success"}:
            raise RuntimeError(f"SMSBower 查价失败: {self._error_message(payload)}")
        data = payload.get("data") or {}
        service_data = data.get(self.service) if isinstance(data, dict) else None
        domain_data = service_data.get(self.domain) if isinstance(service_data, dict) else None
        if not isinstance(domain_data, dict):
            raise RuntimeError(f"SMSBower 无可用库存: service={self.service} domain={self.domain}")
        price = domain_data.get("price")
        count = domain_data.get("count")
        return f"{self.service}@{self.domain} (price={price}, count={count})"

    def get_email(self) -> MailboxAccount:
        self._assert_ready()
        params: dict[str, Any] = {
            "service": self.service,
            "domain": self.domain,
            "alias": self.alias,
        }
        if self.ref:
            params["ref"] = self.ref
        if self.max_price is not None:
            params["maxPrice"] = self.max_price

        payload = self._request_json("/api/mail/getActivation", params, timeout=45)
        status = payload.get("status")
        if int(status or 0) != 1 and str(status or "") not in {"1", "success"}:
            raise RuntimeError(f"SMSBower 取邮箱失败: {self._error_message(payload)}")

        email = str(payload.get("mail") or payload.get("email") or "").strip()
        mail_id = str(payload.get("mailId") or payload.get("mail_id") or payload.get("id") or "").strip()
        if not email or not mail_id:
            raise RuntimeError(f"SMSBower 取邮箱响应缺少 mail/mailId: {payload}")

        return MailboxAccount(
            email=email,
            account_id=mail_id,
            extra={
                "mailbox_provider_key": "smsbower_mail_api",
                "smsbower_mail_id": mail_id,
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "smsbower_mail",
                    "login_identifier": email,
                    "display_name": email,
                    "credentials": {
                        "api_key": self.api_key,
                    },
                    "metadata": {
                        "api_url": self.api,
                        "service": self.service,
                        "domain": self.domain,
                        "alias": self.alias,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "smsbower_mail",
                    "resource_type": "mailbox",
                    "resource_identifier": mail_id,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "mail_id": mail_id,
                        "service": self.service,
                        "domain": self.domain,
                        "api_url": self.api,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        # SMSBower 只暴露验证码轮询接口，不提供邮件 ID 列表。
        return set()

    def _mail_id(self, account: MailboxAccount) -> str:
        extra = account.extra or {}
        mail_id = str(
            account.account_id
            or extra.get("smsbower_mail_id")
            or ((extra.get("provider_resource") or {}).get("resource_identifier") or "")
            or ((extra.get("provider_resource") or {}).get("metadata") or {}).get("mail_id")
            or ""
        ).strip()
        if not mail_id:
            raise RuntimeError("SMSBower mailbox 缺少 mailId")
        return mail_id

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
    ) -> str:
        mail_id = self._mail_id(account)
        pattern = re.compile(code_pattern) if code_pattern else None
        start = time.time()
        last_error = ""
        while time.time() - start < timeout:
            try:
                payload = self._request_json("/api/mail/getCode", {"mailId": mail_id}, timeout=20)
                status = payload.get("status")
                if int(status or 0) == 1 or str(status or "") in {"1", "success"}:
                    code = str(payload.get("code") or "").strip()
                    if not code:
                        last_error = "empty code"
                    else:
                        # 有些响应直接给验证码；有些给整段文本。
                        if pattern:
                            match = pattern.search(code)
                            if match:
                                code = match.group(1) if match.groups() else match.group(0)
                        else:
                            match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", code)
                            if match:
                                code = match.group(1)
                        if code:
                            self._set_status(mail_id, 3)  # success / charge
                            return code
                else:
                    last_error = self._error_message(payload)
                    # 明确已取消时直接失败
                    if "canceled" in last_error.lower() or "cancelled" in last_error.lower():
                        raise RuntimeError(f"SMSBower 激活已取消: {last_error}")
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = str(exc)
            time.sleep(self.poll_interval)
        raise TimeoutError(f"等待 SMSBower 验证码超时 ({timeout}s) last={last_error}")

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
    ) -> str:
        # getCode 通常只返回 code；若返回文本则尝试抽链接。
        mail_id = self._mail_id(account)
        start = time.time()
        last_error = ""
        while time.time() - start < timeout:
            try:
                payload = self._request_json("/api/mail/getCode", {"mailId": mail_id}, timeout=20)
                status = payload.get("status")
                if int(status or 0) == 1 or str(status or "") in {"1", "success"}:
                    text = " ".join(
                        str(payload.get(key) or "")
                        for key in ("code", "text", "html", "message", "body")
                    )
                    link = _extract_verification_link(text, keyword)
                    if link:
                        self._set_status(mail_id, 3)
                        return link
                    last_error = "code received but no link"
                else:
                    last_error = self._error_message(payload)
            except Exception as exc:
                last_error = str(exc)
            time.sleep(self.poll_interval)
        raise TimeoutError(f"等待 SMSBower 验证链接超时 ({timeout}s) last={last_error}")

    def delete_account(self, account: MailboxAccount, reason: str = "") -> bool:
        try:
            mail_id = self._mail_id(account)
        except Exception:
            return False
        return self._set_status(mail_id, 2)

    def mark_invalid_email(self, account: MailboxAccount, reason: str = "") -> list[str]:
        self.delete_account(account, reason=reason)
        return ["smsbower_cancelled"]
