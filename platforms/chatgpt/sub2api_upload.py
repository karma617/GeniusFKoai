"""SUB2API 上传与会话导入工具。

本模块复刻浏览器扩展里“导出至 SUB2API”的管理 API 直连思路：
先登录 SUB2API 管理端，再解析 openai 分组与可选代理，最后把当前 ChatGPT
账号按 codex-session 格式导入远端。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Tuple
from urllib.parse import urlparse

from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)

DEFAULT_SUB2API_GROUP_NAME = "codex"
DEFAULT_SUB2API_ACCOUNT_PRIORITY = 1
DEFAULT_SUB2API_CONCURRENCY = 10
DEFAULT_SUB2API_RATE_MULTIPLIER = 1
DEFAULT_SUB2API_REQUEST_RETRIES = 8
DEFAULT_SUB2API_RETRY_DELAY_SECONDS = 2


class Sub2ApiRequestError(RuntimeError):
    """携带 HTTP 状态码的 SUB2API 请求异常，便于按状态码做降级处理。"""

    def __init__(self, message: str, *, status_code: int = 0, path: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.path = path


def _normalize_string(value: Any = "") -> str:
    return str(value or "").strip()


def _get_config_value(key: str) -> str:
    try:
        from core.config_store import config_store

        return config_store.get(key, "")
    except Exception:
        return ""


def _decode_jwt_payload(token: str) -> dict:
    """不验签解析 JWT payload，仅用于提取账号 ID 与过期时间。"""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def _extract_credential(account: Any, key: str) -> str:
    """从账号对象或 credentials 容器里读取指定凭据。"""
    value = getattr(account, key, None)
    if value:
        return str(value)
    extra = getattr(account, "extra", None)
    if isinstance(extra, dict) and extra.get(key):
        return str(extra[key])
    creds = getattr(account, "credentials", None) or []
    if isinstance(creds, dict):
        return str(creds.get(key) or "")
    if isinstance(creds, list):
        for item in creds:
            if not isinstance(item, dict):
                continue
            if item.get("key") == key and item.get("value") not in (None, ""):
                return str(item["value"])
            if item.get(key) not in (None, ""):
                return str(item[key])
    return ""


def normalize_sub2api_origin(raw_url: str) -> str:
    """将 SUB2API 后台任意路径归一为 origin。"""
    text = _normalize_string(raw_url)
    if not text:
        return ""
    with_protocol = text if re.match(r"^https?://", text, flags=re.I) else f"http://{text}"
    parsed = urlparse(with_protocol)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("SUB2API URL 格式无效，请填写后台地址或域名。")
    return f"{parsed.scheme}://{parsed.netloc}"


def _error_message(payload: Any, status_code: int, path: str) -> str:
    if isinstance(payload, dict):
        for key in ("message", "detail", "error", "reason"):
            message = _normalize_string(payload.get(key))
            if message:
                return message
    return f"SUB2API 请求失败（HTTP {status_code}）：{path}"


def _is_retryable_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or status_code >= 500


def _request_json(
    origin: str,
    path: str,
    *,
    method: str = "GET",
    token: str = "",
    body: dict | None = None,
    timeout: int = 30,
    retries: int = DEFAULT_SUB2API_REQUEST_RETRIES,
    retry_delay: float = DEFAULT_SUB2API_RETRY_DELAY_SECONDS,
) -> Any:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    max_attempts = max(1, int(retries or 0) + 1)
    last_error: Sub2ApiRequestError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = cffi_requests.request(
                method,
                f"{origin}{path}",
                headers=headers,
                data=data,
                proxies=None,
                verify=False,
                timeout=timeout,
                impersonate="chrome110",
            )
        except Exception as exc:
            last_error = Sub2ApiRequestError(f"SUB2API 请求异常：{exc}", path=path)
            if attempt < max_attempts:
                logger.warning("[SUB2API] 请求异常，将重试 %s/%s: %s", attempt, retries, exc)
                if retry_delay > 0:
                    time.sleep(retry_delay)
                continue
            raise last_error from exc

        status_code = int(getattr(response, "status_code", 0) or 0)
        text = getattr(response, "text", "") or ""
        try:
            payload = json.loads(text) if text else None
        except Exception:
            payload = None

        if isinstance(payload, dict) and "code" in payload:
            try:
                code = int(payload.get("code"))
            except Exception:
                code = -1
            if code == 0:
                return payload.get("data")
            error = Sub2ApiRequestError(
                _error_message(payload, status_code, path),
                status_code=status_code,
                path=path,
            )
            if _is_retryable_status(status_code) and attempt < max_attempts:
                logger.warning("[SUB2API] 请求失败，将重试 %s/%s: %s", attempt, retries, error)
                if retry_delay > 0:
                    time.sleep(retry_delay)
                last_error = error
                continue
            raise error

        if status_code < 200 or status_code >= 300:
            error = Sub2ApiRequestError(
                _error_message(payload, status_code, path),
                status_code=status_code,
                path=path,
            )
            if _is_retryable_status(status_code) and attempt < max_attempts:
                logger.warning("[SUB2API] 请求失败，将重试 %s/%s: %s", attempt, retries, error)
                if retry_delay > 0:
                    time.sleep(retry_delay)
                last_error = error
                continue
            raise error
        return payload
    if last_error:
        raise last_error
    raise Sub2ApiRequestError(f"SUB2API 请求失败：{path}", path=path)


def login_sub2api(
    api_url: str,
    email: str,
    password: str,
    *,
    timeout: int = 30,
    retries: int = DEFAULT_SUB2API_REQUEST_RETRIES,
) -> tuple[str, str]:
    """登录 SUB2API 管理端，返回 origin 与 access token。"""
    origin = normalize_sub2api_origin(api_url)
    if not origin:
        raise ValueError("SUB2API URL 未配置")
    if not _normalize_string(email):
        raise ValueError("SUB2API 登录邮箱未配置")
    if not str(password or ""):
        raise ValueError("SUB2API 登录密码未配置")

    payload = _request_json(
        origin,
        "/api/v1/auth/login",
        method="POST",
        body={"email": _normalize_string(email), "password": str(password or "")},
        timeout=timeout,
        retries=retries,
    )
    token = _normalize_string((payload or {}).get("access_token") or (payload or {}).get("accessToken"))
    if not token:
        raise ValueError("SUB2API 登录返回缺少 access_token")
    return origin, token


def normalize_sub2api_group_names(value: Any) -> list[str]:
    """解析分组名称，支持逗号、中文逗号、分号与换行分隔。"""
    source = value if isinstance(value, (list, tuple)) else re.split(r"[\r\n,，;；]+", str(value or ""))
    seen: set[str] = set()
    names: list[str] = []
    for item in source:
        name = _normalize_string(item)
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names or [DEFAULT_SUB2API_GROUP_NAME]


def get_groups_by_names(
    origin: str,
    token: str,
    group_names: Any,
    *,
    timeout: int = 30,
    retries: int = DEFAULT_SUB2API_REQUEST_RETRIES,
) -> list[dict]:
    target_names = normalize_sub2api_group_names(group_names)
    groups = _request_json(origin, "/api/v1/admin/groups/all", token=token, timeout=timeout, retries=retries)
    matched: list[dict] = []
    missing: list[str] = []
    all_groups = groups if isinstance(groups, list) else []
    for name in target_names:
        normalized = name.lower()
        group = next(
            (
                item for item in all_groups
                if _normalize_string(item.get("name")).lower() == normalized
                and (_normalize_string(item.get("platform")) in {"", "openai"})
            ),
            None,
        )
        if group:
            matched.append(group)
        else:
            missing.append(name)
    if missing:
        raise ValueError(f"SUB2API 中未找到以下 openai 分组：{'、'.join(missing)}。")
    return matched


def _normalize_proxy_id(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except Exception:
        return None
    return number if number > 0 else None


def _build_proxy_display_name(proxy: dict) -> str:
    proxy_id = _normalize_proxy_id(proxy.get("id"))
    name = _normalize_string(proxy.get("name")) or "(未命名代理)"
    protocol = _normalize_string(proxy.get("protocol"))
    host = _normalize_string(proxy.get("host"))
    port = _normalize_string(proxy.get("port"))
    address = f"{protocol}://{host}:{port}" if protocol and host and port else ""
    return " ".join(part for part in (name, f"#{proxy_id}" if proxy_id else "", address) if part)


def _is_active_proxy(proxy: dict) -> bool:
    status = _normalize_string(proxy.get("status")).lower()
    return not status or status == "active"


def resolve_sub2api_proxy(
    origin: str,
    token: str,
    preference: str,
    *,
    timeout: int = 30,
    retries: int = DEFAULT_SUB2API_REQUEST_RETRIES,
) -> dict | None:
    """按 ID、精确名称、模糊文本依次匹配 SUB2API 代理。"""
    normalized = _normalize_string(preference)
    if not normalized:
        return None
    proxies = _request_json(
        origin,
        "/api/v1/admin/proxies/all?with_count=true",
        token=token,
        timeout=timeout,
        retries=retries,
    )
    active = [
        item for item in (proxies if isinstance(proxies, list) else [])
        if isinstance(item, dict) and _is_active_proxy(item) and _normalize_proxy_id(item.get("id"))
    ]
    preferred_id = _normalize_proxy_id(normalized)
    if preferred_id:
        proxy = next((item for item in active if _normalize_proxy_id(item.get("id")) == preferred_id), None)
        if proxy:
            return proxy
        available = "；".join(_build_proxy_display_name(item) for item in active[:8]) or "无可用代理"
        raise ValueError(f"SUB2API 默认代理 ID “{normalized}”不存在或未启用。可用代理：{available}")

    lowered = normalized.lower()
    exact = [item for item in active if _normalize_string(item.get("name")).lower() == lowered]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        available = "；".join(_build_proxy_display_name(item) for item in exact[:8])
        raise ValueError(f"SUB2API 默认代理“{normalized}”匹配到多个代理，请改填代理 ID。候选：{available}")

    fuzzy = [
        item for item in active
        if lowered in " ".join(
            _normalize_string(part).lower()
            for part in (item.get("id"), item.get("name"), item.get("protocol"), item.get("host"), item.get("port"))
        )
    ]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        available = "；".join(_build_proxy_display_name(item) for item in fuzzy[:8])
        raise ValueError(f"SUB2API 默认代理“{normalized}”匹配到多个代理，请改填代理 ID。候选：{available}")

    available = "；".join(_build_proxy_display_name(item) for item in active[:8]) or "无可用代理"
    raise ValueError(f"SUB2API 默认代理“{normalized}”不存在或未启用。可用代理：{available}")


def _normalize_timestamp(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        try:
            if text.isdigit():
                number = int(text)
                dt = datetime.fromtimestamp(number / 1000 if number > 1e11 else number, tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch_seconds(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
            return int(number / 1000 if number > 1e11 else number)
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except Exception:
        return None


def _seconds_until(expires_at: str) -> int | None:
    seconds = _epoch_seconds(expires_at)
    if not seconds:
        return None
    return max(0, int(seconds - datetime.now(timezone.utc).timestamp()))


def _strip_empty(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_empty(item)
            for key, item in value.items()
            if item not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [_strip_empty(item) for item in value]
    return value


def _account_extra(account: Any) -> dict:
    extra = getattr(account, "extra", None)
    return extra if isinstance(extra, dict) else {}


def _account_priority(value: Any = None) -> int:
    raw = _normalize_string(value if value not in (None, "") else _get_config_value("sub2api_account_priority"))
    if not raw:
        return DEFAULT_SUB2API_ACCOUNT_PRIORITY
    try:
        priority = int(raw)
    except Exception as exc:
        raise ValueError("SUB2API 账号优先级必须是大于等于 1 的整数。") from exc
    if priority < 1:
        raise ValueError("SUB2API 账号优先级必须是大于等于 1 的整数。")
    return priority


def _account_tokens(account: Any) -> dict[str, str]:
    access_token = _extract_credential(account, "access_token") or _extract_credential(account, "accessToken")
    refresh_token = _extract_credential(account, "refresh_token") or _extract_credential(account, "refreshToken")
    id_token = _extract_credential(account, "id_token") or _extract_credential(account, "idToken")
    session_token = _extract_credential(account, "session_token") or _extract_credential(account, "sessionToken")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "session_token": session_token,
    }


def _account_plan_type(account: Any) -> str:
    value = (
        _normalize_string(getattr(account, "plan_type", ""))
        or _extract_credential(account, "plan_type")
        or _extract_credential(account, "planType")
    )
    if value:
        return value.lower()
    extra = getattr(account, "extra", None)
    usage = extra.get("usage") if isinstance(extra, dict) and isinstance(extra.get("usage"), dict) else {}
    return _normalize_string(usage.get("plan_type") or usage.get("planType")).lower()


def _is_k12_account(account: Any) -> bool:
    return _account_plan_type(account) == "k12"


def _build_codex_session_content(account: Any, tokens: dict[str, str]) -> str:
    extra = _account_extra(account)
    session = getattr(account, "session", None) or extra.get("session")
    access_token = tokens["access_token"]
    if not access_token:
        raise ValueError("账号缺少 access_token，无法导入 SUB2API。")
    if not tokens["refresh_token"] and not _is_k12_account(account):
        raise ValueError("账号尚未获取 rt，不能导入 SUB2API。")
    if isinstance(session, dict) and session:
        session_payload = dict(session)
        # 普通账号必须先获取 rt；K12 session 允许无 refreshToken 上传。
        session_payload.setdefault("accessToken", access_token)
        if tokens["refresh_token"]:
            session_payload.setdefault("refreshToken", tokens["refresh_token"])
        if tokens["session_token"]:
            session_payload.setdefault("sessionToken", tokens["session_token"])
        if tokens["id_token"]:
            session_payload.setdefault("idToken", tokens["id_token"])
        return json.dumps(session_payload, ensure_ascii=False, separators=(",", ":"))
    return access_token


def _account_name(account: Any, access_token: str) -> str:
    email = _normalize_string(getattr(account, "email", ""))
    if email:
        return email
    claims = _decode_jwt_payload(access_token)
    return _normalize_string(claims.get("email")) or "ChatGPT Account"


def _account_expires(account: Any, access_token: str) -> tuple[str, int | None]:
    extra = _account_extra(account)
    expires_at = _normalize_timestamp(
        getattr(account, "expires_at", "")
        or getattr(account, "expired", "")
        or extra.get("expires_at")
        or extra.get("expired")
    )
    claims = _decode_jwt_payload(access_token)
    exp = claims.get("exp")
    if not expires_at and isinstance(exp, int) and exp > 0:
        expires_at = _normalize_timestamp(exp)
    return expires_at, _epoch_seconds(expires_at)


def _build_import_payload(
    account: Any,
    *,
    group_ids: list[int],
    proxy_id: int | None,
    priority: int,
) -> dict:
    tokens = _account_tokens(account)
    access_token = tokens["access_token"]
    expires_at, expires_epoch = _account_expires(account, access_token)
    payload = {
        "content": _build_codex_session_content(account, tokens),
        "group_ids": group_ids,
        "name": _account_name(account, access_token),
        "priority": priority,
        "auto_pause_on_expired": True,
        "update_existing": True,
    }
    if expires_epoch:
        payload["expires_at"] = expires_epoch
    if proxy_id:
        payload["proxy_id"] = proxy_id
    return payload


def _build_direct_account_payload(
    account: Any,
    *,
    group_ids: list[int],
    proxy_id: int | None,
    priority: int,
) -> dict:
    tokens = _account_tokens(account)
    access_token = tokens["access_token"]
    if not access_token:
        raise ValueError("账号缺少 access_token，无法导入 SUB2API。")
    if not tokens["refresh_token"] and not _is_k12_account(account):
        raise ValueError("账号尚未获取 rt，不能导入 SUB2API。")
    claims = _decode_jwt_payload(access_token)
    auth_info = claims.get("https://api.openai.com/auth", {}) if isinstance(claims, dict) else {}
    expires_at, expires_epoch = _account_expires(account, access_token)
    account_id = (
        _normalize_string(auth_info.get("chatgpt_account_id") if isinstance(auth_info, dict) else "")
        or _extract_credential(account, "chatgpt_account_id")
        or _extract_credential(account, "account_id")
        or _normalize_string(getattr(account, "account_id", ""))
    )
    user_id = (
        _normalize_string(auth_info.get("chatgpt_user_id") if isinstance(auth_info, dict) else "")
        or _normalize_string(auth_info.get("user_id") if isinstance(auth_info, dict) else "")
        or _normalize_string(getattr(account, "user_id", ""))
    )
    workspace_id = (
        _normalize_string(getattr(account, "workspace_id", ""))
        or _extract_credential(account, "workspace_id")
        or _normalize_string(auth_info.get("organization_id") if isinstance(auth_info, dict) else "")
    )
    email = _normalize_string(getattr(account, "email", "")) or _normalize_string(claims.get("email") if isinstance(claims, dict) else "")
    payload = {
        "name": email or "ChatGPT Account",
        "platform": "openai",
        "type": "oauth",
        "expires_at": expires_epoch,
        "auto_pause_on_expired": True,
        "concurrency": DEFAULT_SUB2API_CONCURRENCY,
        "priority": priority,
        "rate_multiplier": DEFAULT_SUB2API_RATE_MULTIPLIER,
        "group_ids": group_ids,
        "credentials": _strip_empty({
            "access_token": access_token,
            "refresh_token": tokens["refresh_token"],
            "id_token": tokens["id_token"],
            "session_token": tokens["session_token"],
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": user_id,
            "organization_id": workspace_id,
            "email": email,
            "expires_at": expires_at,
            "expires_in": _seconds_until(expires_at),
            "plan_type": _account_plan_type(account),
            "client_id": _extract_credential(account, "client_id") or _extract_credential(account, "clientId"),
        }),
        "extra": _strip_empty({
            "email": email,
            "name": email or "ChatGPT Account",
            "source": "geniusfkoai",
            "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }),
    }
    if proxy_id:
        payload["proxy_id"] = proxy_id
    return _strip_empty(payload)


def _normalize_import_result(result: Any) -> dict:
    if not isinstance(result, dict):
        return {"total": 0, "created": 0, "updated": 0, "skipped": 0, "failed": 0}
    return {
        "total": max(0, int(result.get("total") or 0)),
        "created": max(0, int(result.get("created") or 0)),
        "updated": max(0, int(result.get("updated") or 0)),
        "skipped": max(0, int(result.get("skipped") or 0)),
        "failed": max(0, int(result.get("failed") or 0)),
        "errors": result.get("errors") if isinstance(result.get("errors"), list) else [],
        "warnings": result.get("warnings") if isinstance(result.get("warnings"), list) else [],
    }


def _import_summary(result: dict) -> str:
    return (
        "SUB2API 会话导入完成："
        f"新建 {result['created']}，更新 {result['updated']}，"
        f"跳过 {result['skipped']}，失败 {result['failed']}"
    )


def _import_error_detail(result: dict) -> str:
    for bucket in ("errors", "warnings"):
        for item in result.get(bucket) or []:
            if isinstance(item, dict) and item.get("message"):
                return str(item["message"])
            if isinstance(item, str) and item:
                return item
    return _import_summary(result)


def upload_to_sub2api(
    account: Any,
    *,
    api_url: str | None = None,
    email: str | None = None,
    password: str | None = None,
    group_name: str | None = None,
    account_priority: Any = None,
    default_proxy_name: str | None = None,
    timeout: int = 30,
) -> Tuple[bool, str]:
    """上传单个 ChatGPT 账号到 SUB2API。"""
    api_url = api_url or _get_config_value("sub2api_url")
    email = email or _get_config_value("sub2api_email")
    password = password if password not in (None, "") else _get_config_value("sub2api_password")
    group_name = group_name or _get_config_value("sub2api_group_name") or DEFAULT_SUB2API_GROUP_NAME
    default_proxy_name = default_proxy_name if default_proxy_name is not None else _get_config_value("sub2api_default_proxy_name")

    try:
        priority = _account_priority(account_priority)
        origin, token = login_sub2api(api_url, email or "", password or "", timeout=timeout)
        groups = get_groups_by_names(origin, token, group_name, timeout=timeout)
        group_ids = [
            int(group.get("id"))
            for group in groups
            if _normalize_proxy_id(group.get("id"))
        ]
        if not group_ids:
            return False, "SUB2API 返回的目标分组 ID 无效"

        proxy_id = None
        if _normalize_string(default_proxy_name):
            proxy = resolve_sub2api_proxy(origin, token, default_proxy_name or "", timeout=timeout)
            proxy_id = _normalize_proxy_id((proxy or {}).get("id"))

        import_payload = _build_import_payload(
            account,
            group_ids=group_ids,
            proxy_id=proxy_id,
            priority=priority,
        )
        try:
            result = _normalize_import_result(_request_json(
                origin,
                "/api/v1/admin/accounts/import/codex-session",
                method="POST",
                token=token,
                body=import_payload,
                timeout=timeout,
            ))
            if result["failed"] > 0 or (result["created"] + result["updated"] <= 0):
                return False, _import_error_detail(result)
            return True, _import_summary(result)
        except Sub2ApiRequestError as exc:
            if exc.status_code not in {404, 405}:
                raise
            logger.info("[SUB2API] codex-session 导入接口不可用，降级为账号直建: %s", exc)

        direct_payload = _build_direct_account_payload(
            account,
            group_ids=group_ids,
            proxy_id=proxy_id,
            priority=priority,
        )
        created = _request_json(
            origin,
            "/api/v1/admin/accounts",
            method="POST",
            token=token,
            body=direct_payload,
            timeout=timeout,
        )
        return True, f"SUB2API 已创建账号 #{(created or {}).get('id', 'unknown')}"
    except Exception as exc:
        logger.warning("[SUB2API] 上传失败: %s", exc)
        return False, str(exc)
