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


class OutlookEmailEndpointNotFound(RuntimeError):
    """outlookEmail 旧版端点不存在，用于触发 outlookEmailPlus 兼容回退。"""


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
        self._csrf_token: str = ""
        self._api_variant = ""

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

    def _get_session(self) -> requests.Session:
        if self._session is None:
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
            self._session = session
        return self._session

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self._get_session()
        clean_params = {
            key: value
            for key, value in (params or {}).items()
            if value not in (None, "")
        }
        url = f"{self.api}{path}"
        with suppress_insecure_request_warning():
            response = session.get(url, params=clean_params, timeout=15)

        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(f"outlookEmail 响应不是 JSON: HTTP {response.status_code}") from exc

        if response.status_code in {401, 403}:
            raise RuntimeError("outlookEmail API Key 认证失败")
        if response.status_code >= 400:
            if response.status_code == 404 and self._is_endpoint_not_found(payload):
                raise OutlookEmailEndpointNotFound(f"outlookEmail 端点不存在: {path}")
            message = payload.get("error") or payload.get("message") or f"HTTP {response.status_code}"
            raise RuntimeError(f"outlookEmail 请求失败: {message}")
        if isinstance(payload, dict) and payload.get("success") is False:
            message = payload.get("error") or payload.get("message") or "success=false"
            raise RuntimeError(f"outlookEmail 请求失败: {message}")
        return payload if isinstance(payload, dict) else {"items": payload}

    @staticmethod
    def _is_endpoint_not_found(payload: dict[str, Any]) -> bool:
        code = _text(payload.get("code")).upper()
        message = _text(payload.get("message") or payload.get("error"))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        status = data.get("status") if isinstance(data, dict) else None
        return code in OUTLOOK_EMAIL_PLUS_NOT_FOUND_CODES or status == 404 or "资源不存在" in message

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
        with suppress_insecure_request_warning():
            response = session.post(f"{self.api}{path}", json=body, timeout=15)

        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(f"outlookEmail 响应不是 JSON: HTTP {response.status_code}") from exc

        if response.status_code in {401, 403}:
            raise RuntimeError("outlookEmail API Key 认证失败")
        if response.status_code >= 400 or (isinstance(payload, dict) and payload.get("success") is False):
            message = payload.get("error") or payload.get("message") or f"HTTP {response.status_code}"
            raise RuntimeError(f"outlookEmail 请求失败: {message}")
        return payload if isinstance(payload, dict) else {"items": payload}

    def _get_admin_session(self) -> requests.Session:
        if self._admin_session is not None:
            return self._admin_session
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
        with suppress_insecure_request_warning():
            login_response = session.post(
                f"{self.api}/login",
                json={"password": self.admin_password},
                timeout=15,
            )
        login_payload = self._response_json(login_response, "outlookEmail 登录")
        if login_response.status_code >= 400 or login_payload.get("success") is False:
            message = login_payload.get("error") or login_payload.get("message") or f"HTTP {login_response.status_code}"
            raise RuntimeError(f"outlookEmail 管理端登录失败: {message}")

        with suppress_insecure_request_warning():
            csrf_response = session.get(f"{self.api}/api/csrf-token", timeout=15)
        csrf_payload = self._response_json(csrf_response, "outlookEmail CSRF")
        self._csrf_token = _text(csrf_payload.get("csrf_token"))
        if self._csrf_token:
            session.headers.update({"X-CSRFToken": self._csrf_token})
        self._admin_session = session
        return session

    @staticmethod
    def _response_json(response, label: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(f"{label} 响应不是 JSON: HTTP {response.status_code}") from exc
        return payload if isinstance(payload, dict) else {"items": payload}

    def _admin_get_json(self, path: str) -> dict[str, Any]:
        session = self._get_admin_session()
        with suppress_insecure_request_warning():
            response = session.get(f"{self.api}{path}", timeout=15)
        payload = self._response_json(response, f"outlookEmail GET {path}")
        if response.status_code >= 400 or payload.get("success") is False:
            message = payload.get("error") or payload.get("message") or f"HTTP {response.status_code}"
            raise RuntimeError(f"outlookEmail 管理端请求失败: {message}")
        return payload

    def _admin_post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        session = self._get_admin_session()
        with suppress_insecure_request_warning():
            response = session.post(f"{self.api}{path}", json=body, timeout=15)
        payload = self._response_json(response, f"outlookEmail POST {path}")
        if response.status_code >= 400 or payload.get("success") is False:
            message = payload.get("error") or payload.get("message") or f"HTTP {response.status_code}"
            raise RuntimeError(f"outlookEmail 管理端请求失败: {message}")
        return payload

    def _admin_delete_json(self, path: str) -> dict[str, Any]:
        session = self._get_admin_session()
        with suppress_insecure_request_warning():
            response = session.delete(f"{self.api}{path}", timeout=15)
        payload = self._response_json(response, f"outlookEmail DELETE {path}")
        if response.status_code >= 400 or payload.get("success") is False:
            message = payload.get("error") or payload.get("message") or f"HTTP {response.status_code}"
            raise RuntimeError(f"outlookEmail 管理端删除失败: {message}")
        return payload

    def _account_query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": self.account_limit,
            "offset": self.account_offset,
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

    def _admin_account_query_params(self) -> str:
        params = {
            "page": max(1, (self.account_offset // max(1, min(self.account_limit, 100))) + 1),
            "page_size": max(1, min(self.account_limit, 100)),
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

    def _list_accounts(self) -> list[dict[str, Any]]:
        payload = self._get_json("/api/external/accounts", self._account_query_params())
        self._api_variant = "legacy"
        items = payload.get("accounts")
        if not isinstance(items, list):
            items = payload.get("items") if isinstance(payload.get("items"), list) else []
        return [item for item in items if isinstance(item, dict)]

    def _list_admin_accounts(self) -> list[dict[str, Any]]:
        # outlookEmailPlus 未提供 /api/external/accounts；有管理员密码时走 Web 管理端分页接口。
        query = self._admin_account_query_params()
        payload = self._admin_get_json(f"/api/accounts?{query}")
        items = payload.get("accounts")
        if not isinstance(items, list):
            items = []
        self._api_variant = "plus_admin"
        return [item for item in items if isinstance(item, dict)]

    def _list_accounts_for_selection(self) -> list[dict[str, Any]]:
        try:
            return self._list_accounts()
        except OutlookEmailEndpointNotFound:
            if self.admin_password:
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

    def _select_account(self) -> dict[str, Any]:
        try:
            accounts = self._list_accounts_for_selection()
        except OutlookEmailEndpointNotFound:
            return self._claim_pool_account()
        usable = [item for item in accounts if self._is_usable_account(item)]
        if not usable:
            fallback = [item for item in accounts if self._account_email(item) and not self._has_skip_tag(item)]
            usable = fallback
        if not usable:
            detail = _join_nonempty(
                [
                    f"group_id={self.group_id}" if self.group_id else "",
                    f"tag_ids={self.account_tag_ids}" if self.account_tag_ids else "",
                    f"skip_tags={','.join(self.skip_tag_names)}" if self.skip_tag_names else "",
                ]
            )
            suffix = f"（筛选条件：{detail}）" if detail else ""
            raise RuntimeError(f"outlookEmail 账号列表中没有可用邮箱{suffix}")
        for item in usable:
            if self._reserve_local_account(item):
                return item
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
            return self._build_account(email=self.fixed_email, account_id=self.fixed_email, source="fixed")

        item = self._select_account()
        email = self._account_email(item)
        account_id = _text(item.get("id") or item.get("account_id"))
        source = "outlook_email_plus_pool" if item.get("claim_token") else "account_list"
        return self._build_account(email=email, account_id=account_id, source=source, raw=item)

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
        return _strip_markup(" ".join(_text(mail.get(field)) for field in fields))

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

    def _list_plus_messages(self, account: MailboxAccount) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
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
            payload = self._get_json("/api/external/messages", params)
            data = self._data_payload(payload)
            emails = data.get("emails") if isinstance(data.get("emails"), list) else []
            for item in emails:
                if isinstance(item, dict):
                    item.setdefault("folder", folder)
                    items.append(item)
        self._api_variant = "plus"
        return items

    def get_current_ids(self, account: MailboxAccount) -> set:
        return {self._message_id(mail) for mail in self._list_emails(account) if self._message_id(mail)}

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
        seen = set(before_ids or [])
        pattern = re.compile(code_pattern or DEFAULT_CODE_PATTERN)
        started = time.time()
        last_error: Exception | None = None

        while time.time() - started < timeout:
            try:
                for mail in self._list_emails(account, runtime_keyword=keyword):
                    mid = self._message_id(mail)
                    if not mid:
                        continue
                    if mid in seen and not self._is_after_otp_sent(mail, otp_sent_at):
                        continue
                    seen.add(mid)
                    if not self._matches_keyword(mail, keyword):
                        continue
                    text = re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", " ", self._message_text(mail))
                    match = pattern.search(text)
                    if match:
                        return match.group(1) if match.groups() else match.group(0)
                    detail = self._load_message_detail(account, mail)
                    if detail:
                        detail_text = re.sub(
                            r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                            " ",
                            self._message_text({**mail, **detail}),
                        )
                        match = pattern.search(detail_text)
                        if match:
                            return match.group(1) if match.groups() else match.group(0)
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
