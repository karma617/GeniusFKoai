"""outlookEmail 对外 API 邮箱 provider。"""
from __future__ import annotations

import html
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode, urlparse
from typing import Any

import requests

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link
from core.tls import mark_session_insecure, suppress_insecure_request_warning


DEFAULT_CODE_PATTERN = r"(?<!#)(?<!\d)(\d{6})(?!\d)"
OUTLOOK_EMAIL_PLUS_NOT_FOUND_CODES = {"HTTP_ERROR", "NOT_FOUND"}
OUTLOOK_EMAIL_PLUS_FOLDERS = {"inbox", "junkemail", "deleteditems"}
OUTLOOK_EMAIL_LOCAL_RESERVATION_TTL_SECONDS = 30 * 60
OUTLOOK_EMAIL_RETRY_STATUS_CODES = {502, 503, 504}
OUTLOOK_EMAIL_RETRY_ATTEMPTS = 3
OUTLOOK_EMAIL_RETRY_DELAY_SECONDS = 0.6
OUTLOOK_EMAIL_SELECTION_SCAN_LIMIT = 10000
OUTLOOK_EMAIL_ASYNC_PROBE_POLL_SECONDS = 1

_OUTLOOK_EMAIL_RESERVATION_LOCK = threading.Lock()
_OUTLOOK_EMAIL_RESERVED_ACCOUNTS: dict[str, float] = {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _truthy(value: Any) -> bool:
    return _text(value).lower() in {"1", "true", "yes", "on", "y"}


def _split_names(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(item or "") for item in value]
    else:
        raw_items = re.split(r"[,，\n\r]+", str(value or ""))
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        name = item.strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def _outlook_email_error_message(payload: Any, status_code: int) -> str:
    if not isinstance(payload, dict):
        return f"HTTP {status_code}"
    raw_error = payload.get("error")
    if isinstance(raw_error, dict):
        message = _text(raw_error.get("message") or raw_error.get("message_en") or raw_error.get("code"))
        trace_id = _text(raw_error.get("trace_id"))
        if trace_id:
            return f"{message or f'HTTP {status_code}'} (HTTP {status_code}, trace_id={trace_id})"
        return message or f"HTTP {status_code}"
    message = _text(payload.get("error") or payload.get("message"))
    trace_id = _text(payload.get("trace_id"))
    if trace_id:
        return f"{message or f'HTTP {status_code}'} (HTTP {status_code}, trace_id={trace_id})"
    return message or f"HTTP {status_code}"


def _join_nonempty(parts: list[str]) -> str:
    return "，".join(part for part in parts if part)


def _normalize_base_url(value: str) -> str:
    raw = _text(value)
    if not raw:
        raise RuntimeError("outlookEmail 未配置服务地址")
    if "://" not in raw:
        raw = f"https://{raw.lstrip('/')}"
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"outlookEmail 服务地址无效: {value!r}")
    return raw.rstrip("/")


def _is_local_api_url(value: str) -> bool:
    parsed = urlparse(value)
    host = (parsed.hostname or "").strip().lower()
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".localhost")


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, minimum), maximum)


def _strip_markup(text: str) -> str:
    cleaned = html.unescape(str(text or ""))
    cleaned = re.sub(r"<style[^>]*>.*?</style>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<script[^>]*>.*?</script>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _collect_text_parts(value: Any) -> list[str]:
    if isinstance(value, dict):
        parts: list[str] = []
        for item in value.values():
            parts.extend(_collect_text_parts(item))
        return parts
    if isinstance(value, (list, tuple, set)):
        parts: list[str] = []
        for item in value:
            parts.extend(_collect_text_parts(item))
        return parts
    text = _text(value)
    return [text] if text else []


class OutlookEmailEndpointNotFound(RuntimeError):
    """outlookEmail 旧版端点不存在，用于触发 outlookEmailPlus 兼容回退。"""


class OutlookEmailTemporaryUnavailable(RuntimeError):
    """outlookEmail 上游临时不可用；验证码轮询应继续等待。"""


class OutlookEmailMailbox(BaseMailbox):
    """通过 assast/outlookEmail 对外 API 读取 Outlook/Hotmail 邮件。"""

    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        admin_password: str = "",
        fixed_email: str = "",
        group_id: str = "",
        account_limit: str | int = "",
        account_offset: str | int = "",
        account_sort_by: str = "",
        account_sort_order: str = "",
        account_tag_ids: str = "",
        account_include_untagged: str | bool = "",
        email_folder: str = "",
        email_top: str | int = "",
        email_subject_contains: str = "",
        email_from_contains: str = "",
        email_keyword: str = "",
        poll_interval: str | int = "",
        skip_tag_names: str | list[str] = "",
        register_success_tag_names: str | list[str] = "",
        plus_success_tag_names: str | list[str] = "",
        invalid_email_tag_names: str | list[str] = "",
        proxy: str | None = None,
        log_fn=None,
    ):
        self.api = _normalize_base_url(api_url)
        self.api_key = _text(api_key)
        self.admin_password = _text(admin_password)
        self.fixed_email = _text(fixed_email)
        self.group_id = _text(group_id)
        self.account_limit = _bounded_int(account_limit, default=100, minimum=1, maximum=10000)
        self.account_offset = _bounded_int(account_offset, default=0, minimum=0, maximum=1000000)
        self.account_sort_by = _text(account_sort_by)
        self.account_sort_order = _text(account_sort_order).lower()
        self.account_tag_ids = _text(account_tag_ids)
        self.account_include_untagged = _truthy(account_include_untagged)
        self.email_folder = _text(email_folder).lower() or "all"
        self.email_top = _bounded_int(email_top, default=10, minimum=1, maximum=50)
        self.email_subject_contains = _text(email_subject_contains)
        self.email_from_contains = _text(email_from_contains)
        self.email_keyword = _text(email_keyword)
        self.poll_interval = _bounded_int(poll_interval, default=4, minimum=1, maximum=30)
        self.skip_tag_names = _split_names(skip_tag_names)
        self.register_success_tag_names = _split_names(register_success_tag_names)
        self.plus_success_tag_names = _split_names(plus_success_tag_names)
        self.invalid_email_tag_names = _split_names(invalid_email_tag_names) or ["无效邮箱"]
        proxy_url = _text(proxy)
        self.proxy = (
            {"http": proxy_url, "https": proxy_url}
            if proxy_url and not _is_local_api_url(self.api)
            else None
        )
        self._session: requests.Session | None = None
        self._admin_session: requests.Session | None = None
        self._session_local = threading.local()
        self._admin_session_local = threading.local()
        self._csrf_token: str = ""
        self._api_variant = ""
        self._log_fn = log_fn

        self._assert_ready()

    @classmethod
    def from_config(cls, config: dict) -> "OutlookEmailMailbox":
        return cls(
            api_url=config.get("outlook_email_api_url", ""),
            api_key=config.get("outlook_email_api_key", ""),
            admin_password=config.get("outlook_email_admin_password", ""),
            fixed_email=config.get("outlook_email_fixed_email", ""),
            group_id=config.get("outlook_email_group_id", ""),
            account_limit=config.get("outlook_email_account_limit", ""),
            account_offset=config.get("outlook_email_account_offset", ""),
            account_sort_by=config.get("outlook_email_account_sort_by", ""),
            account_sort_order=config.get("outlook_email_account_sort_order", ""),
            account_tag_ids=config.get("outlook_email_account_tag_ids", ""),
            account_include_untagged=config.get("outlook_email_account_include_untagged", ""),
            email_folder=config.get("outlook_email_folder", ""),
            email_top=config.get("outlook_email_top", ""),
            email_subject_contains=config.get("outlook_email_subject_contains", ""),
            email_from_contains=config.get("outlook_email_from_contains", ""),
            email_keyword=config.get("outlook_email_keyword", ""),
            poll_interval=config.get("outlook_email_poll_interval", ""),
            skip_tag_names=config.get("outlook_email_skip_tag_names", ""),
            register_success_tag_names=config.get("outlook_email_register_success_tag_names", ""),
            plus_success_tag_names=config.get("outlook_email_plus_success_tag_names", ""),
            invalid_email_tag_names=config.get("outlook_email_invalid_email_tag_names", ""),
            proxy=config.get("proxy") or config.get("mailbox_proxy"),
        )

    def _assert_ready(self) -> None:
        if not self.api_key:
            raise RuntimeError("outlookEmail 未配置 API Key")

    def _log(self, message: str) -> None:
        if not callable(self._log_fn):
            return
        try:
            self._log_fn(message)
        except Exception:
            return

    def _get_session(self) -> requests.Session:
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = requests.Session()
            session.trust_env = False
            session.proxies = self.proxy or {}
            mark_session_insecure(session)
            session.headers.update(
                {
                    "X-API-Key": self.api_key,
                    "user-agent": "aBaiAutoplus/outlookEmail-mailbox",
                    "accept": "application/json",
                }
            )
            self._session_local.session = session
            self._session = session
        return session

    def _reset_thread_session_if_current(self, session: requests.Session) -> bool:
        if getattr(self._session_local, "session", None) is not session:
            return False
        try:
            session.close()
        except Exception:
            pass
        self._session_local.session = None
        if self._session is session:
            self._session = None
        return True

    def _request_with_retries(
        self,
        session: requests.Session,
        method: str,
        path: str,
        *,
        retry_status_codes: set[int] | None = None,
        **kwargs: Any,
    ):
        retry_status_codes = OUTLOOK_EMAIL_RETRY_STATUS_CODES if retry_status_codes is None else retry_status_codes
        method_upper = method.upper()
        url = f"{self.api}{path}"
        active_session = session
        for attempt in range(OUTLOOK_EMAIL_RETRY_ATTEMPTS):
            try:
                with suppress_insecure_request_warning():
                    request_fn = getattr(active_session, method.lower())
                    response = request_fn(url, timeout=15, **kwargs)
            except requests.RequestException as exc:
                if attempt < OUTLOOK_EMAIL_RETRY_ATTEMPTS - 1:
                    if self._reset_thread_session_if_current(active_session):
                        active_session = self._get_session()
                    time.sleep(OUTLOOK_EMAIL_RETRY_DELAY_SECONDS * (attempt + 1))
                    continue
                raise RuntimeError(
                    f"outlookEmail {method_upper} {path} \u8bf7\u6c42\u5f02\u5e38: {exc}"
                ) from exc
            if response.status_code in retry_status_codes and attempt < OUTLOOK_EMAIL_RETRY_ATTEMPTS - 1:
                time.sleep(OUTLOOK_EMAIL_RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            return response
        raise RuntimeError(f"outlookEmail {method_upper} {path} \u8bf7\u6c42\u5931\u8d25")

    @staticmethod
    def _response_json(response, label: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(f"{label} \u54cd\u5e94\u4e0d\u662f JSON: HTTP {response.status_code}") from exc
        return payload if isinstance(payload, dict) else {"items": payload}

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self._get_session()
        clean_params = {
            key: value
            for key, value in (params or {}).items()
            if value not in (None, "")
        }
        retry_status_codes = set(OUTLOOK_EMAIL_RETRY_STATUS_CODES)
        if path == "/api/external/accounts" and self.admin_password:
            retry_status_codes = set()
        response = self._request_with_retries(
            session,
            "GET",
            path,
            retry_status_codes=retry_status_codes,
            params=clean_params,
        )
        label = f"outlookEmail GET {path}"
        payload = self._response_json(response, label)

        if response.status_code == 401:
            raise RuntimeError(f"{label} API Key \u8ba4\u8bc1\u5931\u8d25")
        if response.status_code >= 400:
            if response.status_code == 404 and self._is_endpoint_not_found(payload):
                raise OutlookEmailEndpointNotFound(f"outlookEmail GET {path} \u7aef\u70b9\u4e0d\u5b58\u5728")
            message = _outlook_email_error_message(payload, response.status_code)
            raise RuntimeError(f"{label} \u8bf7\u6c42\u5931\u8d25: {message}")
        if isinstance(payload, dict) and payload.get("success") is False:
            message = _outlook_email_error_message(payload, response.status_code)
            raise RuntimeError(f"{label} \u8bf7\u6c42\u5931\u8d25: {message}")
        return payload if isinstance(payload, dict) else {"items": payload}

    @staticmethod
    def _is_endpoint_not_found(payload: dict[str, Any]) -> bool:
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        source = error if error else payload
        code = _text(source.get("code")).upper()
        message = _text(source.get("message") or source.get("message_en") or source.get("error"))
        data = source.get("data") if isinstance(source.get("data"), dict) else {}
        status = data.get("status") if isinstance(data, dict) else None
        message_lc = message.lower()
        return (
            code in OUTLOOK_EMAIL_PLUS_NOT_FOUND_CODES
            or status == 404
            or ("resource" in message_lc and "not" in message_lc)
            or "\u8d44\u6e90\u4e0d\u5b58\u5728" in message
        )

    @staticmethod
    def _is_external_accounts_unavailable_error(exc: Exception) -> bool:
        text = str(exc or "").lower()
        if "api key" in text or "\u8ba4\u8bc1\u5931\u8d25" in text:
            return False
        return (
            "http 404" in text
            or "http 502" in text
            or "feature_disabled" in text
            or "feature disabled" in text
            or "connection reset" in text
            or "timed out" in text
            or "timeout" in text
            or ("resource" in text and "not" in text)
            or "\u8d44\u6e90\u4e0d\u5b58\u5728" in text
            or "bad gateway" in text
        )

    @staticmethod
    def _is_temporary_unavailable_error(exc: Exception) -> bool:
        text = str(exc or "").lower()
        if "api key" in text or "\u8ba4\u8bc1\u5931\u8d25" in text:
            return False
        return (
            "http 502" in text
            or "http 503" in text
            or "http 504" in text
            or "bad gateway" in text
            or "cloudflare" in text
            or "temporarily unavailable" in text
            or "graph/imap" in text
            or "\u5747\u8bfb\u53d6\u5931\u8d25" in text
            or "connection reset" in text
            or "timed out" in text
            or "timeout" in text
        )

    @staticmethod
    def _data_payload(payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        email_payload = payload.get("email")
        if isinstance(email_payload, dict):
            return email_payload
        return payload

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        session = self._get_session()
        response = self._request_with_retries(session, "POST", path, json=body)
        label = f"outlookEmail POST {path}"
        payload = self._response_json(response, label)

        if response.status_code == 401:
            raise RuntimeError(f"{label} API Key \u8ba4\u8bc1\u5931\u8d25")
        if response.status_code >= 400 or (isinstance(payload, dict) and payload.get("success") is False):
            message = _outlook_email_error_message(payload, response.status_code)
            raise RuntimeError(f"{label} \u8bf7\u6c42\u5931\u8d25: {message}")
        return payload if isinstance(payload, dict) else {"items": payload}

    def _get_admin_session(self) -> requests.Session:
        admin_session = getattr(self._admin_session_local, "session", None)
        if admin_session is not None:
            return admin_session
        if not self.admin_password:
            raise RuntimeError("outlookEmail 未配置管理员密码，无法执行管理操作")

        session = requests.Session()
        session.trust_env = False
        session.proxies = self.proxy or {}
        mark_session_insecure(session)
        session.headers.update(
            {
                "user-agent": "aBaiAutoplus/outlookEmail-mailbox",
                "accept": "application/json",
            }
        )
        login_response = self._request_with_retries(session, "POST", "/login", json={"password": self.admin_password})
        login_payload = self._response_json(login_response, "outlookEmail POST /login")
        if login_response.status_code >= 400 or login_payload.get("success") is False:
            message = _outlook_email_error_message(login_payload, login_response.status_code)
            raise RuntimeError(f"outlookEmail POST /login \u7ba1\u7406\u7aef\u767b\u5f55\u5931\u8d25: {message}")

        csrf_response = self._request_with_retries(session, "GET", "/api/csrf-token")
        csrf_payload = self._response_json(csrf_response, "outlookEmail GET /api/csrf-token")
        if csrf_response.status_code >= 400 or csrf_payload.get("success") is False:
            message = _outlook_email_error_message(csrf_payload, csrf_response.status_code)
            raise RuntimeError(f"outlookEmail GET /api/csrf-token \u7ba1\u7406\u7aef\u8bf7\u6c42\u5931\u8d25: {message}")
        self._csrf_token = _text(csrf_payload.get("csrf_token"))
        if self._csrf_token:
            session.headers.update({"X-CSRFToken": self._csrf_token})
        self._admin_session_local.session = session
        self._admin_session = session
        return session

    def _admin_get_json(self, path: str) -> dict[str, Any]:
        session = self._get_admin_session()
        response = self._request_with_retries(session, "GET", path)
        payload = self._response_json(response, f"outlookEmail GET {path}")
        if response.status_code >= 400 or payload.get("success") is False:
            message = _outlook_email_error_message(payload, response.status_code)
            raise RuntimeError(f"outlookEmail GET {path} \u7ba1\u7406\u7aef\u8bf7\u6c42\u5931\u8d25: {message}")
        return payload

    def _admin_post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        session = self._get_admin_session()
        response = self._request_with_retries(session, "POST", path, json=body)
        payload = self._response_json(response, f"outlookEmail POST {path}")
        if response.status_code >= 400 or payload.get("success") is False:
            message = _outlook_email_error_message(payload, response.status_code)
            raise RuntimeError(f"outlookEmail POST {path} \u7ba1\u7406\u7aef\u8bf7\u6c42\u5931\u8d25: {message}")
        return payload

    def _admin_delete_json(self, path: str) -> dict[str, Any]:
        session = self._get_admin_session()
        response = self._request_with_retries(session, "DELETE", path)
        payload = self._response_json(response, f"outlookEmail DELETE {path}")
        if response.status_code >= 400 or payload.get("success") is False:
            message = _outlook_email_error_message(payload, response.status_code)
            raise RuntimeError(f"outlookEmail DELETE {path} \u7ba1\u7406\u7aef\u5220\u9664\u5931\u8d25: {message}")
        return payload

    def _account_query_params(self, *, offset: int | None = None, limit: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": self.account_limit if limit is None else limit,
            "offset": self.account_offset if offset is None else offset,
        }
        if self.group_id:
            params["group_id"] = self.group_id
        if self.account_sort_by:
            params["sort_by"] = self.account_sort_by
        if self.account_sort_order in {"asc", "desc"}:
            params["sort_order"] = self.account_sort_order
        if self.account_tag_ids:
            params["tag_ids"] = self.account_tag_ids
            params["include_untagged"] = "true" if self.account_include_untagged else "false"
        return params

    def _admin_account_query_params(self, *, page: int | None = None, page_size: int | None = None) -> str:
        effective_page_size = max(1, min(page_size or self.account_limit, 100))
        params = {
            "page": page or max(1, (self.account_offset // effective_page_size) + 1),
            "page_size": effective_page_size,
        }
        if self.group_id:
            params["group_id"] = self.group_id
        if self.account_sort_by in {"email", "refresh_time"}:
            params["sort_by"] = self.account_sort_by
        if self.account_sort_order in {"asc", "desc"}:
            params["sort_order"] = self.account_sort_order
        if self.account_tag_ids:
            params["tag_ids"] = self.account_tag_ids
        return urlencode(params)

    def _claim_context(self) -> tuple[str, str]:
        caller_id = "GeniusFKoai"
        task_id = f"mailbox-{uuid.uuid4().hex[:16]}"
        return caller_id, task_id

    def _claim_pool_account(self) -> dict[str, Any]:
        caller_id, task_id = self._claim_context()
        payload = self._post_json(
            "/api/external/pool/claim-random",
            {
                "caller_id": caller_id,
                "task_id": task_id,
            },
        )
        data = self._data_payload(payload)
        if not data.get("email"):
            raise RuntimeError("outlookEmailPlus 领取邮箱后未返回 email")
        data["_pool_caller_id"] = caller_id
        data["_pool_task_id"] = task_id
        self._api_variant = "plus"
        return data

    def _release_pool_claim(self, item: dict[str, Any], *, reason: str = "provider test") -> None:
        account_id = item.get("account_id") or item.get("id")
        claim_token = _text(item.get("claim_token"))
        caller_id = _text(item.get("_pool_caller_id"))
        task_id = _text(item.get("_pool_task_id"))
        if not account_id or not claim_token or not caller_id or not task_id:
            return
        try:
            self._post_json(
                "/api/external/pool/claim-release",
                {
                    "account_id": account_id,
                    "claim_token": claim_token,
                    "caller_id": caller_id,
                    "task_id": task_id,
                    "reason": reason,
                },
            )
        except Exception:
            # 测试连接时尽力释放，不遮蔽原始可用邮箱结果。
            return

    def _complete_pool_claim(self, account: MailboxAccount, *, result: str, detail: str = "") -> None:
        metadata = (account.extra.get("provider_account") or {}).get("metadata") or {}
        account_id = metadata.get("account_id") or metadata.get("id") or account.account_id
        claim_token = _text(metadata.get("claim_token"))
        caller_id = _text(metadata.get("pool_caller_id"))
        task_id = _text(metadata.get("pool_task_id"))
        if not account_id or not claim_token or not caller_id or not task_id:
            return
        self._post_json(
            "/api/external/pool/claim-complete",
            {
                "account_id": account_id,
                "claim_token": claim_token,
                "caller_id": caller_id,
                "task_id": task_id,
                "result": result,
                "detail": detail,
            },
        )

    def _message_folders(self) -> list[str]:
        """返回需要轮询的邮件目录；all 表示同时扫收件箱与垃圾邮件。"""
        folder = (self.email_folder or "inbox").strip().lower()
        if folder == "all":
            return ["inbox", "junkemail"]
        if folder in OUTLOOK_EMAIL_PLUS_FOLDERS:
            return [folder]
        return ["inbox"]

    def _email_query_params(
        self,
        account: MailboxAccount,
        runtime_keyword: str = "",
        *,
        folder: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "email": account.email,
            "folder": (folder or self.email_folder),
            "top": self.email_top,
        }
        if self.email_subject_contains:
            params["subject_contains"] = self.email_subject_contains
        if self.email_from_contains:
            params["from_contains"] = self.email_from_contains
        api_keyword = self.email_keyword or _text(runtime_keyword)
        if api_keyword:
            params["keyword"] = api_keyword
        return params

    def _list_accounts(self, *, offset: int | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        payload = self._get_json("/api/external/accounts", self._account_query_params(offset=offset, limit=limit))
        self._api_variant = "legacy"
        items = payload.get("accounts")
        if not isinstance(items, list):
            items = payload.get("items") if isinstance(payload.get("items"), list) else []
        return [item for item in items if isinstance(item, dict)]

    def _list_admin_accounts(self, *, page: int | None = None, page_size: int | None = None) -> list[dict[str, Any]]:
        # outlookEmailPlus 未提供 /api/external/accounts；有管理员密码时走 Web 管理端分页接口。
        query = self._admin_account_query_params(page=page, page_size=page_size)
        payload = self._admin_get_json(f"/api/accounts?{query}")
        items = payload.get("accounts")
        if not isinstance(items, list):
            items = []
        self._api_variant = "plus_admin"
        return [item for item in items if isinstance(item, dict)]

    def _iter_external_account_pages_for_selection(self):
        page_size = max(1, self.account_limit)
        offset = self.account_offset
        scanned = 0
        while scanned < OUTLOOK_EMAIL_SELECTION_SCAN_LIMIT:
            accounts = self._list_accounts(offset=offset, limit=page_size)
            yield accounts
            count = len(accounts)
            if count < page_size:
                break
            offset += page_size
            scanned += count

    def _iter_admin_account_pages_for_selection(self):
        page_size = max(1, min(self.account_limit, 100))
        page = max(1, (self.account_offset // page_size) + 1)
        scanned = 0
        while scanned < OUTLOOK_EMAIL_SELECTION_SCAN_LIMIT:
            accounts = self._list_admin_accounts(page=page, page_size=page_size)
            yield accounts
            count = len(accounts)
            if count < page_size:
                break
            page += 1
            scanned += count

    def _iter_account_pages_for_selection(self):
        try:
            yield from self._iter_external_account_pages_for_selection()
        except OutlookEmailEndpointNotFound:
            if self.admin_password:
                yield from self._iter_admin_account_pages_for_selection()
                return
            raise
        except RuntimeError as exc:
            if self.admin_password and self._is_external_accounts_unavailable_error(exc):
                yield from self._iter_admin_account_pages_for_selection()
                return
            raise

    def _list_accounts_for_selection(self) -> list[dict[str, Any]]:
        try:
            return self._list_accounts()
        except OutlookEmailEndpointNotFound:
            if self.admin_password:
                return self._list_admin_accounts()
            raise
        except RuntimeError as exc:
            if self.admin_password and self._is_external_accounts_unavailable_error(exc):
                return self._list_admin_accounts()
            raise

    @staticmethod
    def _account_email(item: dict[str, Any]) -> str:
        return _text(item.get("email") or item.get("address") or item.get("mail"))

    @staticmethod
    def _tag_names(item: dict[str, Any]) -> set[str]:
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        result = set()
        for tag in tags:
            if isinstance(tag, dict):
                name = _text(tag.get("name"))
            else:
                name = _text(tag)
            if name:
                result.add(name.lower())
        return result

    def _has_skip_tag(self, item: dict[str, Any]) -> bool:
        blocked_tag_names = [*self.skip_tag_names, *self.invalid_email_tag_names]
        if not blocked_tag_names:
            return False
        account_tags = self._tag_names(item)
        return any(name.lower() in account_tags for name in blocked_tag_names)

    def _is_usable_account(self, item: dict[str, Any]) -> bool:
        if not OutlookEmailMailbox._account_email(item):
            return False
        if self._has_skip_tag(item):
            return False
        status = _text(item.get("status")).lower()
        refresh_status = _text(item.get("last_refresh_status")).lower()
        disabled_statuses = {"disabled", "deleted", "inactive", "failed", "error", "invalid"}
        if status in disabled_statuses or refresh_status in disabled_statuses:
            return False
        return True

    def _reservation_key_for_item(self, item: dict[str, Any]) -> str:
        """生成本机邮箱占用键，避免并发任务重复领取同一邮箱。"""
        email = self._account_email(item).lower()
        if not email:
            return ""
        resource_id = _text(item.get("id") or item.get("account_id") or email)
        group_id = _text(item.get("group_id") or self.group_id)
        return "|".join([self.api, group_id, resource_id, email])

    def _reservation_key_for_account(self, account: MailboxAccount) -> str:
        metadata = {}
        try:
            provider_account = (account.extra or {}).get("provider_account") or {}
            metadata = provider_account.get("metadata") or {}
        except Exception:
            metadata = {}
        reserved_key = _text(metadata.get("local_reservation_key"))
        if reserved_key:
            return reserved_key
        email = _text(getattr(account, "email", "")).lower()
        if not email:
            return ""
        resource_id = _text(getattr(account, "account_id", "") or metadata.get("id") or metadata.get("account_id") or email)
        group_id = _text(metadata.get("group_id") or self.group_id)
        return "|".join([self.api, group_id, resource_id, email])

    @staticmethod
    def _prune_expired_reservations(now: float) -> None:
        expired = [
            key
            for key, reserved_at in _OUTLOOK_EMAIL_RESERVED_ACCOUNTS.items()
            if now - reserved_at > OUTLOOK_EMAIL_LOCAL_RESERVATION_TTL_SECONDS
        ]
        for key in expired:
            _OUTLOOK_EMAIL_RESERVED_ACCOUNTS.pop(key, None)

    def _reserve_local_account(self, item: dict[str, Any]) -> bool:
        key = self._reservation_key_for_item(item)
        if not key:
            return True
        now = time.time()
        with _OUTLOOK_EMAIL_RESERVATION_LOCK:
            self._prune_expired_reservations(now)
            if key in _OUTLOOK_EMAIL_RESERVED_ACCOUNTS:
                return False
            _OUTLOOK_EMAIL_RESERVED_ACCOUNTS[key] = now
        item["_local_reservation_key"] = key
        item["_local_reserved_at"] = now
        return True

    def _release_local_account_reservation(self, account_or_item: MailboxAccount | dict[str, Any]) -> None:
        """释放本机邮箱占用；终态后由标签/删除结果决定后续是否可取。"""
        if isinstance(account_or_item, dict):
            key = _text(account_or_item.get("_local_reservation_key")) or self._reservation_key_for_item(account_or_item)
        else:
            key = self._reservation_key_for_account(account_or_item)
        if not key:
            return
        with _OUTLOOK_EMAIL_RESERVATION_LOCK:
            _OUTLOOK_EMAIL_RESERVED_ACCOUNTS.pop(key, None)

    @staticmethod
    def _is_account_readability_error(exc: Exception) -> bool:
        text = str(exc or "").lower()
        if not text:
            return False
        global_error_markers = (
            "api key",
            "认证失败",
            "unauthorized",
            "http 401",
            "http 403",
            "http 502",
            "http 503",
            "http 504",
            "bad gateway",
            "cloudflare",
            "temporarily unavailable",
            "connection reset",
            "timed out",
            "timeout",
        )
        if any(marker in text for marker in global_error_markers):
            return False
        account_error_markers = (
            "account_not_found",
            "account_auth_expired",
            "account_credential_decrypt_failed",
            "account import failed",
            "账号不存在",
            "账号授权已失效",
            "授权已失效",
            "重新授权",
            "凭据解密失败",
            "oauth 导入",
            "oauth import",
            "basic auth",
            "graph/imap",
            "均读取失败",
            "短期冷却",
            "跳过重复上游取件",
        )
        return any(marker in text for marker in account_error_markers)

    def _select_account(self, rejected_keys: set[str] | None = None) -> dict[str, Any]:
        rejected_keys = set(rejected_keys or set())
        saw_candidate = False
        saw_unrejected = False
        saw_reserved_blocked = False
        try:
            account_pages = self._iter_account_pages_for_selection()
            for accounts in account_pages:
                usable = [item for item in accounts if self._is_usable_account(item)]
                if not usable:
                    usable = [item for item in accounts if self._account_email(item) and not self._has_skip_tag(item)]
                if usable:
                    saw_candidate = True
                for item in usable:
                    if self._reservation_key_for_item(item) in rejected_keys:
                        continue
                    saw_unrejected = True
                    if self._reserve_local_account(item):
                        return item
                    saw_reserved_blocked = True
        except OutlookEmailEndpointNotFound:
            return self._claim_pool_account()
        if not saw_candidate:
            detail = _join_nonempty(
                [
                    f"group_id={self.group_id}" if self.group_id else "",
                    f"tag_ids={self.account_tag_ids}" if self.account_tag_ids else "",
                    f"skip_tags={','.join(self.skip_tag_names)}" if self.skip_tag_names else "",
                ]
            )
            suffix = f"（筛选条件：{detail}）" if detail else ""
            raise RuntimeError(f"outlookEmail 账号列表中没有可用邮箱{suffix}")
        if rejected_keys and not saw_unrejected:
            raise RuntimeError("outlookEmail 账号列表中没有通过读信预检的可用邮箱")
        if rejected_keys and not saw_reserved_blocked:
            raise RuntimeError("outlookEmail 账号列表中没有通过读信预检的可用邮箱")
        raise RuntimeError("outlookEmail 当前可用邮箱都已被本机其他任务占用，请稍后重试或降低并发")

    def _build_account(self, *, email: str, account_id: str = "", source: str, raw: dict[str, Any] | None = None) -> MailboxAccount:
        metadata = {
            "email": email,
            "api_url": self.api,
            "source": source,
        }
        raw = raw or {}
        for key in (
            "id",
            "account_id",
            "group_id",
            "group_name",
            "status",
            "account_type",
            "provider",
            "last_refresh_status",
            "claim_token",
            "claimed_at",
            "lease_expires_at",
            "_local_reservation_key",
            "_local_reserved_at",
        ):
            value = raw.get(key)
            if value not in (None, ""):
                metadata["local_reservation_key" if key == "_local_reservation_key" else key] = value
        if raw.get("_pool_caller_id"):
            metadata["pool_caller_id"] = raw["_pool_caller_id"]
        if raw.get("_pool_task_id"):
            metadata["pool_task_id"] = raw["_pool_task_id"]

        resource_id = account_id or _text(raw.get("id")) or email
        return MailboxAccount(
            email=email,
            account_id=resource_id,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "outlook_email",
                    "login_identifier": email,
                    "display_name": email,
                    "credentials": {},
                    "metadata": metadata,
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "outlook_email",
                    "resource_type": "mailbox",
                    "resource_identifier": resource_id,
                    "handle": email,
                    "display_name": email,
                    "metadata": metadata,
                },
            },
        )

    def get_email(self) -> MailboxAccount:
        if self.fixed_email:
            self._assert_fixed_email_not_skipped()
            self._log(f"outlookEmail using fixed mailbox: {self.fixed_email}")
            return self._build_account(email=self.fixed_email, account_id=self.fixed_email, source="fixed")

        self._log("outlookEmail selecting mailbox candidate...")
        item = self._select_account()
        email = self._account_email(item)
        account_id = _text(item.get("id") or item.get("account_id"))
        source = "outlook_email_plus_pool" if item.get("claim_token") else "account_list"
        account = self._build_account(email=email, account_id=account_id, source=source, raw=item)
        self._log(f"outlookEmail candidate selected: {email} source={source}; readability precheck skipped")
        return account

    def peek_email(self) -> str:
        if self.fixed_email:
            self._assert_fixed_email_not_skipped()
            return self.fixed_email

        try:
            item = self._select_account()
        except OutlookEmailEndpointNotFound:
            item = self._claim_pool_account()
        email = self._account_email(item)
        self._release_local_account_reservation(item)
        if item.get("claim_token"):
            self._release_pool_claim(item)
        if not email:
            raise RuntimeError("outlookEmail 未返回可用邮箱")
        return email

    def _assert_fixed_email_not_skipped(self) -> None:
        if not self.skip_tag_names:
            return
        target = self.fixed_email.lower()
        try:
            accounts = self._list_accounts_for_selection()
        except OutlookEmailEndpointNotFound:
            return
        for item in accounts:
            if self._account_email(item).lower() == target and self._has_skip_tag(item):
                raise RuntimeError(f"outlookEmail 固定邮箱带有跳过标签，已跳过: {self.fixed_email}")

    @staticmethod
    def _message_id(mail: dict[str, Any]) -> str:
        explicit = _text(mail.get("id") or mail.get("message_id") or mail.get("internet_message_id"))
        if explicit:
            return explicit
        return "|".join(
            _text(mail.get(key))
            for key in ("folder", "date", "created_at", "from", "from_address", "subject", "body_preview", "content_preview")
            if _text(mail.get(key))
        )

    @staticmethod
    def _message_text(mail: dict[str, Any]) -> str:
        fields = (
            "subject",
            "body_preview",
            "content_preview",
            "preview",
            "summary",
            "text",
            "content",
            "body",
            "body_text",
            "html_content",
            "raw_content",
            "html",
            "from",
            "from_address",
        )
        parts: list[str] = []
        for field in fields:
            parts.extend(_collect_text_parts(mail.get(field)))
        return _strip_markup(" ".join(parts))

    @classmethod
    def _collect_email_values(cls, value: Any) -> set[str]:
        emails: set[str] = set()
        if isinstance(value, dict):
            for item in value.values():
                emails.update(cls._collect_email_values(item))
            return emails
        if isinstance(value, (list, tuple, set)):
            for item in value:
                emails.update(cls._collect_email_values(item))
            return emails
        text = _text(value)
        if not text:
            return emails
        for match in re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text):
            emails.add(match.lower())
        return emails

    @classmethod
    def _message_recipient_emails(cls, mail: dict[str, Any]) -> set[str]:
        recipients: set[str] = set()
        for key in (
            "to",
            "to_address",
            "to_addresses",
            "to_recipients",
            "toRecipients",
            "recipients",
            "recipient",
            "delivered_to",
            "envelope_to",
            "original_to",
            "x_original_to",
        ):
            if key in mail:
                recipients.update(cls._collect_email_values(mail.get(key)))
        return recipients

    @staticmethod
    def _expected_alias_recipient(account: MailboxAccount) -> str:
        extra = dict(getattr(account, "extra", {}) or {})
        alias = extra.get("email_alias") if isinstance(extra.get("email_alias"), dict) else {}
        if alias:
            value = _text(alias.get("alias_email"))
            if value:
                return value.lower()
        resource = extra.get("provider_resource") if isinstance(extra.get("provider_resource"), dict) else {}
        metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
        alias = metadata.get("email_alias") if isinstance(metadata.get("email_alias"), dict) else {}
        value = _text(metadata.get("alias_email") or alias.get("alias_email"))
        return value.lower()

    @classmethod
    def _matches_expected_recipient(cls, account: MailboxAccount, mail: dict[str, Any]) -> bool:
        expected = cls._expected_alias_recipient(account)
        if not expected:
            return True
        recipients = cls._message_recipient_emails(mail)
        if not recipients:
            return True
        return expected in recipients

    @staticmethod
    def _message_epoch(mail: dict[str, Any]) -> float | None:
        raw_timestamp = mail.get("timestamp")
        try:
            if raw_timestamp not in (None, ""):
                value = float(raw_timestamp)
                if value > 0:
                    return value
        except Exception:
            pass

        for key in ("receivedDateTime", "received_at", "created_at", "date"):
            raw = _text(mail.get(key))
            if not raw:
                continue
            try:
                normalized = raw.replace("Z", "+00:00")
                parsed = datetime.fromisoformat(normalized)
            except Exception:
                try:
                    parsed = parsedate_to_datetime(raw)
                except Exception:
                    parsed = None
            if parsed is None:
                for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S"):
                    try:
                        parsed = datetime.strptime(raw, fmt)
                        break
                    except Exception:
                        parsed = None
            if parsed is None:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        return None

    @classmethod
    def _is_after_otp_sent(cls, mail: dict[str, Any], otp_sent_at: float | None) -> bool:
        """若 baseline 误含新邮件，则按发送时间放行，避免错过刚到垃圾箱的 OTP。"""
        if not otp_sent_at:
            return False
        epoch = cls._message_epoch(mail)
        return epoch is not None and epoch >= float(otp_sent_at) - 30

    @staticmethod
    def _emails_from_payload(payload_data: dict[str, Any]) -> list[dict[str, Any]]:
        items = payload_data.get("emails")
        if not isinstance(items, list):
            items = payload_data.get("items") if isinstance(payload_data.get("items"), list) else []
        return [item for item in items if isinstance(item, dict)]

    def _list_external_emails(self, account: MailboxAccount, runtime_keyword: str = "") -> list[dict[str, Any]]:
        """旧版 external/emails 也按目录拆查，避免 folder=all 漏掉 junkemail。"""
        items: list[dict[str, Any]] = []
        for folder in self._message_folders():
            payload = self._get_json(
                "/api/external/emails",
                self._email_query_params(account, runtime_keyword, folder=folder),
            )
            folder_items = self._emails_from_payload(self._data_payload(payload))
            for item in folder_items:
                item.setdefault("folder", folder)
            items.extend(folder_items)
        return items

    def _list_emails(self, account: MailboxAccount, runtime_keyword: str = "") -> list[dict[str, Any]]:
        if self._api_variant.startswith("plus"):
            payload_data = {"emails": self._list_plus_messages(account)}
        else:
            try:
                return self._list_external_emails(account, runtime_keyword)
            except OutlookEmailEndpointNotFound:
                payload_data = {"emails": self._list_plus_messages(account)}
            except RuntimeError as exc:
                if self._is_temporary_unavailable_error(exc):
                    payload_data = {"emails": self._list_plus_messages(account)}
                else:
                    raise
        return self._emails_from_payload(payload_data)

    def _message_scope_params(self, account: MailboxAccount, *, folder: str) -> dict[str, Any]:
        metadata = (account.extra.get("provider_account") or {}).get("metadata") or {}
        claim_token = _text(metadata.get("claim_token"))
        params: dict[str, Any] = {"folder": folder}
        if claim_token:
            params["claim_token"] = claim_token
        else:
            params["email"] = account.email
        return params

    def _load_message_detail(self, account: MailboxAccount, mail: dict[str, Any]) -> dict[str, Any] | None:
        """摘要不含验证码时读取详情正文；失败不打断轮询。"""
        message_id = self._message_id(mail)
        if not message_id:
            return None
        mail_folder = _text(mail.get("folder")).lower()
        folders = [mail_folder] if mail_folder in OUTLOOK_EMAIL_PLUS_FOLDERS else self._message_folders()
        encoded_id = quote(message_id, safe="")
        for folder in folders:
            try:
                payload = self._get_json(
                    f"/api/external/messages/{encoded_id}",
                    self._message_scope_params(account, folder=folder),
                )
                detail = self._data_payload(payload)
                if isinstance(detail, dict) and detail:
                    detail.setdefault("folder", folder)
                    return detail
            except Exception:
                continue
        return None

    def _async_probe_supported_for_account(self, account: MailboxAccount, otp_sent_at: float | None) -> bool:
        metadata = (account.extra.get("provider_account") or {}).get("metadata") or {}
        return bool(_text(metadata.get("claim_token")) and otp_sent_at)

    @staticmethod
    def _is_async_probe_unavailable_error(exc: Exception) -> bool:
        text = str(exc or "").lower()
        if "api key" in text or "\u8ba4\u8bc1\u5931\u8d25" in text:
            return False
        return "404" in text or "not found" in text or "\u7aef\u70b9\u4e0d\u5b58\u5728" in text

    def _code_from_message(
        self,
        account: MailboxAccount,
        mail: dict[str, Any],
        *,
        keyword: str,
        pattern: re.Pattern,
        baseline_ids: set,
        otp_sent_at: float | None,
    ) -> str | None:
        mid = self._message_id(mail)
        if not mid:
            return None
        if mid in baseline_ids and not self._is_after_otp_sent(mail, otp_sent_at):
            return None
        if not self._matches_keyword(mail, keyword):
            return None
        if not self._matches_expected_recipient(account, mail):
            return None
        text = re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", " ", self._message_text(mail))
        match = pattern.search(text)
        if match:
            return match.group(1) if match.groups() else match.group(0)
        detail = self._load_message_detail(account, mail)
        if not detail:
            return None
        combined = {**mail, **detail}
        if not self._matches_expected_recipient(account, combined):
            return None
        detail_text = re.sub(
            r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
            " ",
            self._message_text(combined),
        )
        match = pattern.search(detail_text)
        if match:
            return match.group(1) if match.groups() else match.group(0)
        return None

    def _wait_for_code_via_async_probe(
        self,
        account: MailboxAccount,
        *,
        keyword: str,
        timeout: int,
        pattern: re.Pattern,
        baseline_ids: set,
        otp_sent_at: float | None,
    ) -> str | None:
        if not self._async_probe_supported_for_account(account, otp_sent_at):
            return None
        folder = self._message_folders()[0]
        params = {
            **self._message_scope_params(account, folder=folder),
            "mode": "async",
            "timeout_seconds": max(int(timeout or 1), 1),
            "poll_interval": max(min(self.poll_interval, int(timeout or 1)), 1),
            "baseline_timestamp": max(int(float(otp_sent_at or 0)) - 30, 0),
        }
        if self.email_subject_contains:
            params["subject_contains"] = self.email_subject_contains
        if self.email_from_contains:
            params["from_contains"] = self.email_from_contains
        try:
            payload = self._get_json("/api/external/wait-message", params)
        except OutlookEmailEndpointNotFound:
            return None
        except Exception as exc:
            if self._is_async_probe_unavailable_error(exc):
                return None
            raise OutlookEmailTemporaryUnavailable(str(exc)) from exc
        data = self._data_payload(payload)
        probe_id = _text(data.get("probe_id"))
        if not probe_id:
            return None

        encoded_probe_id = quote(probe_id, safe="")
        deadline = time.time() + max(int(timeout or 1), 1)
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                status_payload = self._get_json(f"/api/external/probe/{encoded_probe_id}")
                status_data = self._data_payload(status_payload)
            except Exception as exc:
                last_error = exc
                break
            status = _text(status_data.get("status")).lower()
            if status == "matched":
                message = status_data.get("message")
                if isinstance(message, dict):
                    code = self._code_from_message(
                        account,
                        message,
                        keyword=keyword,
                        pattern=pattern,
                        baseline_ids=baseline_ids,
                        otp_sent_at=otp_sent_at,
                    )
                    if code:
                        return code
                return None
            if status in {"timeout", "error", "cancelled"}:
                last_error = RuntimeError(status_data.get("error_message") or status)
                break
            time.sleep(min(OUTLOOK_EMAIL_ASYNC_PROBE_POLL_SECONDS, max(deadline - time.time(), 0)))
        if last_error is not None:
            raise OutlookEmailTemporaryUnavailable(str(last_error)) from last_error
        return None

    def _list_plus_messages(self, account: MailboxAccount) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        last_error: Exception | None = None
        for folder in self._message_folders():
            params: dict[str, Any] = {
                **self._message_scope_params(account, folder=folder),
                "top": self.email_top,
                "skip": 0,
            }
            if self.email_subject_contains:
                params["subject_contains"] = self.email_subject_contains
            if self.email_from_contains:
                params["from_contains"] = self.email_from_contains
            try:
                payload = self._get_json("/api/external/messages", params)
            except Exception as exc:
                last_error = exc
                if self._is_temporary_unavailable_error(exc):
                    continue
                raise
            data = self._data_payload(payload)
            emails = data.get("emails") if isinstance(data.get("emails"), list) else []
            for item in emails:
                if isinstance(item, dict):
                    item.setdefault("folder", folder)
                    items.append(item)
        self._api_variant = "plus"
        if last_error is not None and not items:
            raise OutlookEmailTemporaryUnavailable(str(last_error)) from last_error
        return items

    def _precheck_account_readable(self, account: MailboxAccount) -> None:
        folder = self._message_folders()[0]
        params = {
            **self._message_scope_params(account, folder=folder),
            "top": 1,
            "skip": 0,
        }
        try:
            self._get_json("/api/external/messages", params)
        except OutlookEmailEndpointNotFound:
            self._get_json(
                "/api/external/emails",
                {
                    "email": account.email,
                    "folder": folder,
                    "top": 1,
                },
            )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {self._message_id(mail) for mail in self._list_emails(account) if self._message_id(mail)}
        except OutlookEmailTemporaryUnavailable:
            return set()

    def _matches_keyword(self, mail: dict[str, Any], runtime_keyword: str = "") -> bool:
        text = self._message_text(mail).lower()
        for keyword in (self.email_keyword, _text(runtime_keyword)):
            if keyword and keyword.lower() not in text:
                return False
        return True

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        otp_sent_at: float | None = None,
    ) -> str:
        baseline_ids = set(before_ids or [])
        pattern = re.compile(code_pattern or DEFAULT_CODE_PATTERN)
        started = time.time()
        last_error: Exception | None = None

        try:
            async_code = self._wait_for_code_via_async_probe(
                account,
                keyword=keyword,
                timeout=timeout,
                pattern=pattern,
                baseline_ids=baseline_ids,
                otp_sent_at=otp_sent_at,
            )
            if async_code:
                return async_code
        except OutlookEmailTemporaryUnavailable as exc:
            last_error = exc

        while time.time() - started < timeout:
            try:
                for mail in self._list_emails(account, runtime_keyword=keyword):
                    code = self._code_from_message(
                        account,
                        mail,
                        keyword=keyword,
                        pattern=pattern,
                        baseline_ids=baseline_ids,
                        otp_sent_at=otp_sent_at,
                    )
                    if code:
                        return code
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            time.sleep(self.poll_interval)

        message = f"等待验证码超时 ({timeout}s)"
        if last_error:
            message += f"，最后一次错误: {last_error}"
        raise TimeoutError(message)

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
    ) -> str:
        seen = set(before_ids or [])
        started = time.time()
        last_error: Exception | None = None

        while time.time() - started < timeout:
            try:
                for mail in self._list_emails(account, runtime_keyword=keyword):
                    mid = self._message_id(mail)
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    link = _extract_verification_link(self._message_text(mail), keyword)
                    if link:
                        return link
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            time.sleep(self.poll_interval)

        message = f"等待验证链接超时 ({timeout}s)"
        if last_error:
            message += f"，最后一次错误: {last_error}"
        raise TimeoutError(message)

    def _list_tags(self) -> list[dict[str, Any]]:
        payload = self._admin_get_json("/api/tags")
        tags = payload.get("tags")
        return [item for item in tags if isinstance(item, dict)] if isinstance(tags, list) else []

    def _get_or_create_tag_id(self, name: str) -> int:
        normalized = name.strip().lower()
        for tag in self._list_tags():
            if _text(tag.get("name")).lower() == normalized:
                return int(tag.get("id") or 0)
        payload = self._admin_post_json("/api/tags", {"name": name, "color": "#1a1a1a"})
        tag = payload.get("tag") if isinstance(payload.get("tag"), dict) else {}
        tag_id = int(tag.get("id") or 0)
        if tag_id <= 0:
            raise RuntimeError(f"outlookEmail 创建标签后未返回有效 ID: {name}")
        return tag_id

    def _resolve_account_id(self, *, email: str, account_id: str = "") -> int:
        try:
            numeric_id = int(str(account_id or "").strip())
        except (TypeError, ValueError):
            numeric_id = 0
        if numeric_id > 0:
            return numeric_id

        target = email.strip().lower()
        for item in self._list_accounts_for_selection():
            if self._account_email(item).lower() == target:
                try:
                    return int(item.get("id") or 0)
                except (TypeError, ValueError):
                    return 0
        return 0

    def add_tags_to_account(self, *, email: str, account_id: str = "", tag_names: list[str] | None = None) -> list[str]:
        names = _split_names(tag_names or [])
        if not names:
            return []
        resolved_account_id = self._resolve_account_id(email=email, account_id=account_id)
        if resolved_account_id <= 0:
            raise RuntimeError(f"outlookEmail 未找到可打标签的账号 ID: {email}")

        applied: list[str] = []
        for name in names:
            tag_id = self._get_or_create_tag_id(name)
            if tag_id <= 0:
                continue
            self._admin_post_json(
                "/api/accounts/tags",
                {"account_ids": [resolved_account_id], "tag_id": tag_id, "action": "add"},
            )
            applied.append(name)
        return applied

    def delete_account(self, account: MailboxAccount, reason: str = "") -> bool:
        """通过 outlookEmailPlus 管理端删除邮箱，防止异常邮箱再次被取用。"""
        try:
            resolved_account_id = self._resolve_account_id(email=account.email, account_id=account.account_id)
            if resolved_account_id <= 0:
                raise RuntimeError(f"outlookEmail 未找到可删除的账号 ID: {account.email}")

            # 仅在明确判定该邮箱不可用于 OpenAI 创建账号时执行真实删除。
            self._admin_delete_json(f"/api/accounts/{resolved_account_id}")
            return True
        finally:
            self._release_local_account_reservation(account)

    def mark_registration_success(self, account: MailboxAccount) -> list[str]:
        try:
            applied = self.add_tags_to_account(
                email=account.email,
                account_id=account.account_id,
                tag_names=self.register_success_tag_names,
            )
            self._complete_pool_claim(account, result="success", detail="registration_success")
            return applied
        finally:
            self._release_local_account_reservation(account)

    def mark_plus_success(self, account: MailboxAccount) -> list[str]:
        try:
            applied = self.add_tags_to_account(
                email=account.email,
                account_id=account.account_id,
                tag_names=self.plus_success_tag_names,
            )
            self._complete_pool_claim(account, result="success", detail="plus_success")
            return applied
        finally:
            self._release_local_account_reservation(account)

    def mark_invalid_email(self, account: MailboxAccount, reason: str = "") -> list[str]:
        """把收不到 OpenAI 验证码的邮箱打为无效，避免后续重复领取。"""
        try:
            applied = self.add_tags_to_account(
                email=account.email,
                account_id=account.account_id,
                tag_names=self.invalid_email_tag_names,
            )
            detail = reason or "invalid_email_no_otp"
            self._complete_pool_claim(account, result="failed", detail=detail)
            return applied
        finally:
            self._release_local_account_reservation(account)
