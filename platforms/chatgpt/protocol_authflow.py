"""Experimental ChatGPT protocol registration via imported AuthFlow.

This module intentionally stays separate from ``protocol_mailbox.py``.  It is
enabled only by an explicit config flag so the existing registration path keeps
its current behavior.
"""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from platforms.chatgpt.register import RegistrationResult, _extract_chatgpt_account_id


def _safe_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


class _LogHandler(logging.Handler):
    def __init__(self, log_fn: Callable[[str], None]):
        super().__init__(level=logging.INFO)
        self._log_fn = log_fn
        self.setFormatter(logging.Formatter("[AuthFlow] %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._log_fn(self.format(record))
        except Exception:
            pass


class _ProjectMailboxProvider:
    def __init__(
        self,
        *,
        mailbox,
        mailbox_account,
        log_fn: Callable[[str], None],
    ):
        self._mailbox = mailbox
        self._account = mailbox_account
        self._log = log_fn
        self._before_ids: set[str] = set()
        self.outlook_exhausted = False

    def create_mailbox(self) -> str:
        try:
            self._before_ids = set(self._mailbox.get_current_ids(self._account) or set())
        except Exception as exc:
            self._before_ids = set()
            self._log(f"[AuthFlowMailbox] 邮箱基线读取失败，继续等待新邮件: {exc}")
        email = str(getattr(self._account, "email", "") or "").strip()
        if not email:
            raise RuntimeError("当前 mailbox provider 未返回邮箱地址")
        self._log(f"[AuthFlowMailbox] 使用当前项目 mailbox: {email}")
        return email

    def wait_for_otp(self, email: str, timeout: int = 120, issued_after: float | None = None) -> str:
        if email and str(email).strip().lower() != str(getattr(self._account, "email", "") or "").strip().lower():
            raise RuntimeError(f"AuthFlow 请求邮箱 {email} 与当前 mailbox 不一致")

        if issued_after is not None:
            elapsed = time.time() - issued_after
            if elapsed < 5:
                time.sleep(5 - elapsed)

        wait_kwargs = {
            "keyword": "",
            "timeout": timeout,
            "code_pattern": r"(?<!#)(?<!\d)(\d{6})(?!\d)",
            "before_ids": self._before_ids or None,
        }
        try:
            if "otp_sent_at" in inspect.signature(self._mailbox.wait_for_code).parameters:
                wait_kwargs["otp_sent_at"] = issued_after
        except Exception:
            pass

        self._log(f"[AuthFlowMailbox] 等待验证码 timeout={timeout}s before_ids={len(self._before_ids)}")
        code = self._mailbox.wait_for_code(self._account, **wait_kwargs)
        self._log("[AuthFlowMailbox] 已获取验证码")
        try:
            self._before_ids = set(self._mailbox.get_current_ids(self._account) or set())
        except Exception:
            pass
        return str(code or "").strip()

    def mark_outlook_dead(self, reason: str = "") -> None:
        marker = getattr(self._mailbox, "mark_invalid_email", None)
        if callable(marker):
            try:
                marker(self._account, reason=reason)
                self.outlook_exhausted = True
            except Exception as exc:
                self._log(f"[AuthFlowMailbox] 标记邮箱无效失败: {exc}")


@dataclass
class ChatGPTAuthFlowProtocolWorker:
    mailbox: Any
    mailbox_account: Any
    proxy_url: str | None = None
    log_fn: Callable[[str], None] = print
    metadata_extra: dict[str, Any] = field(default_factory=dict)

    def run(self, *, email: str, password: str) -> RegistrationResult:
        if not self.mailbox or not self.mailbox_account:
            raise ValueError("实验 AuthFlow 依赖当前项目 mailbox provider，当前未获取到邮箱账号")
        if email and str(email).strip().lower() != str(getattr(self.mailbox_account, "email", "") or "").strip().lower():
            raise ValueError("实验 AuthFlow 只能使用当前 mailbox provider 分配的邮箱")

        from platforms.chatgpt.authflow_experimental.auth_flow import AuthFlow
        from platforms.chatgpt.authflow_experimental.config import Config

        provider = _ProjectMailboxProvider(
            mailbox=self.mailbox,
            mailbox_account=self.mailbox_account,
            log_fn=self.log_fn,
        )
        flow = AuthFlow(Config(proxy=self.proxy_url))
        flow.result.email = str(email or getattr(self.mailbox_account, "email", "") or "").strip()
        flow.result.password = str(password or "").strip()

        handler = _LogHandler(self.log_fn)
        loggers = [
            logging.getLogger("platforms.chatgpt.authflow_experimental.auth_flow"),
            logging.getLogger("platforms.chatgpt.authflow_experimental.sentinel"),
            logging.getLogger("platforms.chatgpt.authflow_experimental.sentinel_quickjs"),
        ]
        for logger in loggers:
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
        try:
            result = flow.run_register(provider)
        finally:
            for logger in loggers:
                logger.removeHandler(handler)

        data = result.to_dict() if hasattr(result, "to_dict") else {}
        cookies = str(data.get("cookie_header") or "")
        session_payload = {
            "accessToken": str(data.get("access_token") or ""),
            "sessionToken": str(data.get("session_token") or ""),
            "expires": "",
            "user": {},
        }
        metadata = {
            "authflow_experimental": True,
            "source": "gpt-outlook-register-authflow",
            "cookies": cookies,
            "session": session_payload,
            "profile": {},
            "expires_at": "",
            "device_id": str(data.get("device_id") or ""),
            "csrf_token": str(data.get("csrf_token") or ""),
            "registration_refresh_token": str(data.get("refresh_token") or ""),
            "refresh_token_source": "authflow_experimental" if data.get("refresh_token") else "",
        }
        metadata.update(_safe_dict(self.metadata_extra))

        return RegistrationResult(
            success=True,
            email=str(data.get("email") or email or ""),
            password=str(data.get("password") or password or ""),
            account_id=_extract_chatgpt_account_id(str(data.get("access_token") or "")),
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            id_token=str(data.get("id_token") or ""),
            session_token=str(data.get("session_token") or ""),
            metadata=metadata,
            source="authflow_experimental",
        )
