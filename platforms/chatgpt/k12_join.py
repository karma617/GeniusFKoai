'''ChatGPT 强入 K12 空间流程。

注册成功后跳过接码/支付链路，直接用 /api/auth/session 拿到的 session JSON::

1. 向配置的 workspace ID 逐个发送加入申请（POST invites/request）；
2. 按 HTML 转换脚本（convertSession.sub2apiAccount）把 session JSON 转成
   sub2api 账号格式；
3. 上传到已配置的 sub2api 云端（复用 Settings 页的 sub2api 配置）。
'''

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from curl_cffi import requests as cffi_requests

from platforms.chatgpt.constants import CHATGPT_APP
from platforms.chatgpt.sub2api_upload import (
    DEFAULT_SUB2API_CONCURRENCY,
    DEFAULT_SUB2API_GROUP_NAME,
    Sub2ApiRequestError,
    _account_priority,
    _decode_jwt_payload,
    _epoch_seconds,
    _get_config_value,
    _normalize_proxy_id,
    _normalize_string,
    _normalize_timestamp,
    _request_json,
    _seconds_until,
    _strip_empty,
    get_groups_by_names,
    login_sub2api,
    resolve_sub2api_proxy,
)


# ===================== session -> sub2api 账号格式（复刻 HTML convertSession） =====================

K12_SUB2API_PLAN_TYPE = 'k12'
K12_SUB2API_CHATGPT_ACCOUNT_ID = 'a65ebb2e-dd7c-4fdb-9a5d-6ccaf6ad00a3'
K12_SUB2API_UPLOAD_RETRIES = 8
K12_SUB2API_UPLOAD_RETRY_DELAY_SECONDS = 2


def _safe_json_stem(value: Any) -> str:
    stem = re.sub(r"[^A-Za-z0-9._+-]+", "_", str(value or "").strip())
    stem = stem.strip("._-")
    return stem or "account"


def _write_local_json(target_dir: str, email: Any, payload: Any) -> str:
    directory = Path("data") / target_dir
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{_safe_json_stem(email)}_{timestamp}_{uuid.uuid4().hex[:8]}.json"
    path = directory / filename
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _write_named_local_json(target_dir: str, filename: str, payload: Any) -> str:
    directory = Path("data") / target_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _exported_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def build_sub2api_export_payload(accounts: list[dict]) -> dict:
    return {
        'exported_at': _exported_at(),
        'proxies': [],
        'accounts': [account for account in accounts if isinstance(account, dict)],
    }

def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _openai_auth_section(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    auth = payload.get('https://api.openai.com/auth')
    return auth if isinstance(auth, dict) else {}


def _openai_profile_section(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    profile = payload.get('https://api.openai.com/profile')
    return profile if isinstance(profile, dict) else {}


def _b64url_json(obj: dict) -> str:
    import json
    raw = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    import base64
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _email_key(email: str) -> str | None:
    if not isinstance(email, str):
        return None
    normalized = re.sub(r'[^a-z0-9]+', '_', email.strip().lower())
    normalized = re.sub(r'^_+|_+$', '', normalized)
    return normalized or None


def _synthetic_id_token(email, account_id, plan_type, user_id, expires_at) -> str | None:
    if not account_id:
        return None
    now = int(datetime.now(timezone.utc).timestamp())
    auth: dict[str, Any] = {'chatgpt_account_id': account_id}
    if plan_type:
        auth['chatgpt_plan_type'] = plan_type
    if user_id:
        auth['chatgpt_user_id'] = user_id
        auth['user_id'] = user_id
    expires = _epoch_seconds(expires_at) or (now + 90 * 24 * 3600)
    payload: dict[str, Any] = {
        'iat': now,
        'exp': expires,
        'https://api.openai.com/auth': auth,
    }
    if email:
        payload['email'] = email
    return (
        f'{_b64url_json({"alg": "none", "typ": "JWT", "cpa_synthetic": True})}.'
        f'{_b64url_json(payload)}.'
    )


def _k12_account_name(email: str, source_name: str, workspace_id: str = '') -> str:
    base = _first_non_empty(email, source_name, 'ChatGPT Account')
    ws_prefix = str(workspace_id or '').strip()[:8]
    if ws_prefix:
        return f'k12-{base}-{ws_prefix}'
    return base


def convert_session_to_sub2api_account(session_json: Any, *, source_name: str = 'k12', workspace_id: str = '') -> dict:
    '''把 /api/auth/session 返回的 JSON 转成 sub2api 账号格式（对齐 HTML 的 sub2apiAccount）。'''
    record = session_json if isinstance(session_json, dict) else {}
    access_token = _first_non_empty(record.get('accessToken'), record.get('access_token'))
    if not access_token:
        raise ValueError('session JSON 缺少 accessToken')
    session_token = _first_non_empty(record.get('sessionToken'), record.get('session_token'))
    refresh_token = _first_non_empty(record.get('refreshToken'), record.get('refresh_token'))
    input_id_token = _first_non_empty(record.get('idToken'), record.get('id_token'))

    payload = _decode_jwt_payload(access_token) or {}
    id_payload = _decode_jwt_payload(input_id_token) if input_id_token else {}
    auth = _openai_auth_section(payload)
    id_auth = _openai_auth_section(id_payload)
    profile = _openai_profile_section(payload)

    exp_value = payload.get('exp') if isinstance(payload, dict) else None
    expires_at = _first_non_empty(
        _normalize_timestamp(exp_value) if isinstance(exp_value, (int, float)) else None,
        _normalize_timestamp(record.get('expires')),
        _normalize_timestamp(record.get('expiresAt')),
        _normalize_timestamp(record.get('expired')),
        _normalize_timestamp(record.get('expires_at')),
    ) or ''

    user_obj = record.get('user') if isinstance(record.get('user'), dict) else {}
    account_obj = record.get('account') if isinstance(record.get('account'), dict) else {}
    email = _first_non_empty(
        user_obj.get('email'),
        record.get('email'),
        profile.get('email'),
        id_payload.get('email') if isinstance(id_payload, dict) else None,
        payload.get('email') if isinstance(payload, dict) else None,
    )
    account_id = _first_non_empty(
        auth.get('chatgpt_account_id'),
        account_obj.get('id'),
        record.get('account_id'),
        record.get('chatgptAccountId'),
        record.get('chatgpt_account_id'),
        id_auth.get('chatgpt_account_id'),
    )
    user_id = _first_non_empty(
        auth.get('chatgpt_user_id') or auth.get('user_id'),
        user_obj.get('id'),
        record.get('user_id'),
        record.get('chatgptUserId'),
        record.get('chatgpt_user_id'),
        id_auth.get('chatgpt_user_id') or id_auth.get('user_id'),
    )
    plan_type = K12_SUB2API_PLAN_TYPE

    exported_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    expires_in = _seconds_until(expires_at) if expires_at else None
    name = _k12_account_name(email, source_name, workspace_id)
    synthetic = _synthetic_id_token(email, account_id, plan_type, user_id, expires_at) if not input_id_token else None
    id_token = _first_non_empty(input_id_token, synthetic)

    sub2api_account = _strip_empty({
        'name': _first_non_empty(name, email, source_name, 'ChatGPT Account'),
        'platform': 'openai',
        'type': 'oauth',
        'concurrency': DEFAULT_SUB2API_CONCURRENCY,
        'priority': 1,
        'credentials': {
            'access_token': access_token,
            'chatgpt_account_id': K12_SUB2API_CHATGPT_ACCOUNT_ID,
            'chatgpt_user_id': user_id,
            'email': email,
            'expires_at': expires_at,
            'expires_in': expires_in,
            'plan_type': plan_type,
            'id_token': id_token,
            'refresh_token': refresh_token or '',
            'session_token': session_token or '',
        },
        'extra': {
            'email': email,
            'email_key': _email_key(email),
            'name': name,
            'workspace_id': str(workspace_id or '').strip(),
            'auth_provider': _first_non_empty(record.get('authProvider'), record.get('auth_provider')),
            'source': 'chatgpt_web_session',
            'last_refresh': exported_at,
        },
    })

    return {
        'access_token': access_token,
        'refresh_token': refresh_token or '',
        'session_token': session_token or '',
        'id_token': id_token,
        'account_id': account_id,
        'user_id': user_id,
        'email': email,
        'expires_at': expires_at,
        'expires_epoch': _epoch_seconds(expires_at),
        'name': name,
        'sub2api_account': sub2api_account,
    }


def save_session_to_local_upload_jsons(session_json: Any, *, workspace_id: str = '') -> tuple[str, str]:
    info = convert_session_to_sub2api_account(session_json, workspace_id=workspace_id)
    email = info.get('email') or 'k12'
    sub2api_path = _write_local_json(
        'sub2api',
        email,
        build_sub2api_export_payload([info['sub2api_account']]),
    )
    cpa_payload = {
        'access_token': info.get('access_token') or '',
        'account_id': info.get('account_id') or '',
        'email': info.get('email') or '',
        'expired': info.get('expires_at') or '',
        'id_token': info.get('id_token') or '',
        'last_refresh': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00'),
        'refresh_token': info.get('refresh_token') or '',
        'type': 'codex',
    }
    cpa_path = _write_local_json('cpa', email, cpa_payload)
    return cpa_path, sub2api_path


def merge_sub2api_export_files(paths: list[str]) -> tuple[str, dict]:
    accounts: list[dict] = []
    for raw_path in paths:
        raw_text = str(raw_path or '').strip()
        if not raw_text:
            continue
        path = Path(raw_text)
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict) and isinstance(data.get('accounts'), list):
            accounts.extend(item for item in data['accounts'] if isinstance(item, dict))
        elif isinstance(data, dict) and data.get('credentials'):
            accounts.append(data)

    payload = build_sub2api_export_payload(accounts)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = _write_named_local_json(
        'sub2api',
        f'k12-sub2api-batch-{timestamp}-{uuid.uuid4().hex[:8]}.json',
        payload,
    )
    return path, payload


# ===================== account_id 兜底提取（参考 FrciblyK12/cpa_upload.py） =====================

def _enrich_account_id_from_backend_api(access_token, *, proxy=None, log=print, timeout=15):
    # Use access_token to call /backend-api/me as fallback for account_id.
    # Returns (account_id, access_token).
    if not access_token:
        return "", ""
    try:
        resp = cffi_requests.get(
            f"{CHATGPT_APP}/backend-api/me",
            headers={
                "authorization": f"Bearer {access_token}",
                "accept": "application/json",
            },
            proxies=_proxies_from_url(proxy),
            verify=False,
            timeout=timeout,
            impersonate="chrome110",
        )
        status = int(getattr(resp, "status_code", 0) or 0)
        log(f"  [K12] /backend-api/me HTTP {status}")
        if status == 200:
            me = resp.json()
            if isinstance(me, dict):
                accounts = me.get("accounts", {})
                if isinstance(accounts, dict):
                    for acct in accounts.values():
                        aid = (acct.get("account", {}) or {}).get("account_id", "")
                        if aid:
                            log(f"  [K12] /backend-api/me -> account_id={aid}")
                            return str(aid), access_token
                aid = me.get("id", "")
                if aid:
                    log(f"  [K12] /backend-api/me -> id={aid}")
                    return str(aid), access_token
    except Exception as exc:
        log(f"  [K12] /backend-api/me failed: {exc}")
    return "", access_token


def _enrich_account_id_from_session_refresh(session_token, *, log=print, timeout=15):
    # Use session_token to refresh and get new access_token, extract account_id.
    # Returns (account_id, new_access_token, expired_iso).
    if not session_token:
        return "", "", ""
    try:
        s = cffi_requests.Session(impersonate="chrome120")
        s.cookies.set("__Secure-next-auth.session-token", session_token, domain=".chatgpt.com", path="/")
        resp = s.get(
            f"{CHATGPT_APP}/api/auth/session",
            headers={"accept": "application/json"},
            timeout=timeout,
        )
        status = int(getattr(resp, "status_code", 0) or 0)
        log(f"  [K12] session refresh HTTP {status}")
        if status == 200:
            data = resp.json()
            new_at = str(data.get("accessToken", "") or "")
            if new_at:
                p = _decode_jwt_payload(new_at)
                auth = _openai_auth_section(p)
                aid = str(auth.get("chatgpt_account_id", "") or "")
                expired = ""
                exp = p.get("exp")
                if isinstance(exp, int) and exp > 0:
                    expired = _normalize_timestamp(exp)
                if aid:
                    log(f"  [K12] session refresh -> account_id={aid}")
                    return aid, new_at, expired
    except Exception as exc:
        log(f"  [K12] session refresh failed: {exc}")
    return "", "", ""


# ===================== 上传到 sub2api 云端 =====================

def _is_retryable_sub2api_upload_error(exc: Exception) -> bool:
    if not isinstance(exc, Sub2ApiRequestError):
        return False
    status_code = int(getattr(exc, "status_code", 0) or 0)
    return status_code == 0 or status_code in {408, 409, 425, 429} or status_code >= 500


def upload_session_to_sub2api(
    session_json: Any,
    *,
    workspace_id: str = '',
    api_url: str | None = None,
    email: str | None = None,
    password: str | None = None,
    group_name: str | None = None,
    account_priority: Any = None,
    default_proxy_name: str | None = None,
    timeout: int = 30,
    log: Any = print,
    proxy: str | None = None,
    max_upload_retries: int = K12_SUB2API_UPLOAD_RETRIES,
) -> tuple[bool, str]:
    '''把 session JSON 转成 sub2api 账号后直建到 sub2api 云端。'''
    api_url = api_url or _get_config_value('sub2api_url')
    email = email or _get_config_value('sub2api_email')
    password = password if password not in (None, '') else _get_config_value('sub2api_password')
    group_name = group_name or _get_config_value('sub2api_group_name') or DEFAULT_SUB2API_GROUP_NAME
    default_proxy_name = default_proxy_name if default_proxy_name is not None else _get_config_value('sub2api_default_proxy_name')

    try:
        info = convert_session_to_sub2api_account(session_json, workspace_id=workspace_id)

        # account_id fallback: try /backend-api/me then session_token refresh
        if not info.get('account_id'):
            log('  [K12] account_id empty, trying /backend-api/me fallback...')
            aid, at = _enrich_account_id_from_backend_api(
                info['access_token'], proxy=None, log=log,
            )
            if aid:
                info['account_id'] = aid
                info['sub2api_account']['credentials']['chatgpt_account_id'] = K12_SUB2API_CHATGPT_ACCOUNT_ID
            else:
                log('  [K12] /backend-api/me no account_id, trying session_token refresh...')
                aid2, new_at, expired2 = _enrich_account_id_from_session_refresh(
                    info.get('session_token', ''), log=log,
                )
                if aid2:
                    info['account_id'] = aid2
                    info['access_token'] = new_at
                    info['sub2api_account']['credentials']['chatgpt_account_id'] = K12_SUB2API_CHATGPT_ACCOUNT_ID
                    info['sub2api_account']['credentials']['access_token'] = new_at
                    if expired2:
                        info['sub2api_account']['credentials']['expires_at'] = expired2
                        info['expires_epoch'] = _epoch_seconds(expired2)
                else:
                    log('  [K12] WARNING: account_id ultimately empty, sub2api upload may fail')

        priority = _account_priority(account_priority)
        total_attempts = max(1, int(max_upload_retries) + 1)
        last_error = ''
        for attempt in range(1, total_attempts + 1):
            try:
                origin, token = login_sub2api(api_url, email or '', password or '', timeout=timeout, retries=0)
                groups = get_groups_by_names(origin, token, group_name, timeout=timeout, retries=0)
                group_ids = [_normalize_proxy_id(g.get('id')) for g in groups]
                group_ids = [i for i in group_ids if i]
                if not group_ids:
                    return False, 'SUB2API 返回的目标分组 ID 无效'

                proxy_id = None
                if _normalize_string(default_proxy_name):
                    sub2api_proxy = resolve_sub2api_proxy(origin, token, default_proxy_name or '', timeout=timeout, retries=0)
                    proxy_id = _normalize_proxy_id((sub2api_proxy or {}).get('id'))

                payload = dict(info['sub2api_account'])
                payload.update({
                    'priority': priority,
                    'group_ids': group_ids,
                    'auto_pause_on_expired': True,
                })
                if info['expires_epoch']:
                    payload['expires_at'] = info['expires_epoch']
                if proxy_id:
                    payload['proxy_id'] = proxy_id

                created = _request_json(
                    origin,
                    '/api/v1/admin/accounts',
                    method='POST',
                    token=token,
                    body=payload,
                    timeout=timeout,
                    retries=0,
                )
                return True, f'SUB2API 已创建账号 #{(created or {}).get("id", "unknown")}'
            except Exception as exc:
                last_error = str(exc)
                if attempt >= total_attempts or not _is_retryable_sub2api_upload_error(exc):
                    return False, last_error
                log(f'  [K12] SUB2API 上传失败，将重试 {attempt}/{max_upload_retries}: {last_error}')
                time.sleep(K12_SUB2API_UPLOAD_RETRY_DELAY_SECONDS)
        return False, last_error or 'SUB2API 上传失败'
    except Exception as exc:
        return False, str(exc)


def upload_sub2api_export_accounts(
    accounts: list[dict],
    *,
    api_url: str | None = None,
    email: str | None = None,
    password: str | None = None,
    group_name: str | None = None,
    account_priority: Any = None,
    default_proxy_name: str | None = None,
    timeout: int = 30,
    log: Any = print,
    max_upload_retries: int = K12_SUB2API_UPLOAD_RETRIES,
) -> tuple[bool, str]:
    '''把已导出的 sub2api accounts 统一上传；登录、分组、代理只解析一次。'''
    valid_accounts = [account for account in accounts if isinstance(account, dict)]
    if not valid_accounts:
        return False, '没有可上传的 SUB2API 账号'

    api_url = api_url or _get_config_value('sub2api_url')
    email = email or _get_config_value('sub2api_email')
    password = password if password not in (None, '') else _get_config_value('sub2api_password')
    group_name = group_name or _get_config_value('sub2api_group_name') or DEFAULT_SUB2API_GROUP_NAME
    default_proxy_name = default_proxy_name if default_proxy_name is not None else _get_config_value('sub2api_default_proxy_name')

    try:
        origin, token = login_sub2api(api_url, email or '', password or '', timeout=timeout, retries=0)
        groups = get_groups_by_names(origin, token, group_name, timeout=timeout, retries=0)
        group_ids = [_normalize_proxy_id(g.get('id')) for g in groups]
        group_ids = [i for i in group_ids if i]
        if not group_ids:
            return False, 'SUB2API 返回的目标分组 ID 无效'

        proxy_id = None
        if _normalize_string(default_proxy_name):
            sub2api_proxy = resolve_sub2api_proxy(origin, token, default_proxy_name or '', timeout=timeout, retries=0)
            proxy_id = _normalize_proxy_id((sub2api_proxy or {}).get('id'))

        priority = _account_priority(account_priority)
        success = 0
        failures: list[str] = []
        total_attempts = max(1, int(max_upload_retries) + 1)
        for account in valid_accounts:
            last_error = ''
            for attempt in range(1, total_attempts + 1):
                try:
                    payload = dict(account)
                    payload.update({
                        'priority': priority,
                        'group_ids': group_ids,
                        'auto_pause_on_expired': True,
                    })
                    credentials = payload.get('credentials') if isinstance(payload.get('credentials'), dict) else {}
                    expires_epoch = _epoch_seconds(payload.get('expires_at')) or _epoch_seconds(credentials.get('expires_at'))
                    if expires_epoch:
                        payload['expires_at'] = expires_epoch
                    if proxy_id:
                        payload['proxy_id'] = proxy_id

                    _request_json(
                        origin,
                        '/api/v1/admin/accounts',
                        method='POST',
                        token=token,
                        body=payload,
                        timeout=timeout,
                        retries=0,
                    )
                    success += 1
                    break
                except Exception as exc:
                    last_error = str(exc)
                    if attempt >= total_attempts or not _is_retryable_sub2api_upload_error(exc):
                        failures.append(last_error)
                        break
                    log(f'  [K12] SUB2API 批量上传失败，将重试 {attempt}/{max_upload_retries}: {last_error}')
                    time.sleep(K12_SUB2API_UPLOAD_RETRY_DELAY_SECONDS)

        if failures:
            return False, f'SUB2API 批量上传完成：成功 {success}/{len(valid_accounts)}，失败 {len(failures)}，首个错误: {failures[0]}'
        return True, f'SUB2API 批量上传完成：成功 {success}/{len(valid_accounts)}'
    except Exception as exc:
        return False, str(exc)


# ===================== 向 workspace 发加入申请（复刻油猴脚本 invite/request） =====================

def _proxies_from_url(proxy: str | None) -> dict | None:
    if not proxy:
        return None
    return {'http': proxy, 'https': proxy}


def ensure_chatgpt_session_cookie(cookies: str | None, session_token: str | None = "") -> str:
    """确保 Cookie header 中带有 ChatGPT NextAuth session token。"""
    header = str(cookies or "").strip()
    token = str(session_token or "").strip()
    if not token or "__Secure-next-auth.session-token=" in header:
        return header
    addition = f"__Secure-next-auth.session-token={token}"
    return f"{header}; {addition}" if header else addition


def compact_chatgpt_session_cookies(cookies: str | None, session_token: str | None = "") -> str:
    """只保留 /api/auth/session exchange 必需的小 Cookie，避免历史 Cookie 触发 431。"""
    header = ensure_chatgpt_session_cookie(cookies, session_token)
    allowed = {"__Secure-next-auth.session-token", "next-auth.session-token", "oai-did"}
    selected: dict[str, str] = {}
    for item in str(header or "").split(";"):
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name in allowed and value:
            selected[name] = value
    if "__Secure-next-auth.session-token" not in selected and "next-auth.session-token" not in selected:
        return header
    return "; ".join(f"{name}={value}" for name, value in selected.items())


def parse_workspace_ids(raw: str) -> list[str]:
    return [w.strip() for w in re.split(r'[\n,]+', raw or '') if w.strip()]


def _join_response_ok(status: int, text: str) -> bool:
    if not (200 <= status < 300):
        return False
    try:
        data = json.loads(text or "{}")
    except Exception:
        return True
    if isinstance(data, dict) and "success" in data:
        return data.get("success") is True
    return True


def _is_unusable_workspace_response(status: int, text: str) -> bool:
    if int(status or 0) not in {404, 500}:
        return False
    try:
        data = json.loads((text or "").strip() or "{}")
    except Exception:
        return False
    detail = str(data.get("detail") or "").strip() if isinstance(data, dict) else ""
    return (int(status or 0), detail) in {
        (404, "Not Found"),
        (500, "Internal Server Error"),
    }


def _find_key_recursive(value: Any, key: str) -> str:
    if isinstance(value, dict):
        direct = value.get(key)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        for item in value.values():
            found = _find_key_recursive(item, key)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_key_recursive(item, key)
            if found:
                return found
    return ""


def _session_chatgpt_account_id(session_json: Any) -> str:
    if not isinstance(session_json, dict):
        return ""
    direct = _find_key_recursive(session_json, "chatgpt_account_id")
    if direct:
        return direct
    access_token = _first_non_empty(session_json.get("accessToken"), session_json.get("access_token"))
    payload = _decode_jwt_payload(access_token) if access_token else {}
    auth = _openai_auth_section(payload)
    return _first_non_empty(auth.get("chatgpt_account_id"), payload.get("chatgpt_account_id")) or ""


def validate_workspace_exchange_session(session_json: Any, workspace_id: str) -> tuple[bool, str]:
    """确认 exchange 后的 session 已拿到可上传的 ChatGPT account 标识。"""
    if not isinstance(session_json, dict):
        return False, "exchange 响应不是 JSON object"
    if not (session_json.get("accessToken") or session_json.get("access_token")):
        return False, "exchange session 缺少 accessToken"
    account_id = _session_chatgpt_account_id(session_json)
    if not account_id:
        return False, "exchange session 缺少 chatgpt_account_id，无法确认可上传账号"
    return True, f"chatgpt_account_id={account_id}"


def send_workspace_join_requests(
    access_token: str,
    cookies: str,
    workspace_ids: str,
    *,
    proxy: str | None = None,
    log: Any = print,
    max_retries: int = 3,
    retry_backoff_ms: int = 5000,
    timeout: int = 30,
) -> list[dict]:
    '''用子号 AT 向每个 workspace 发 POST invites/request。'''
    ids = parse_workspace_ids(workspace_ids)
    if not access_token:
        log('  [K12] 无可用 accessToken，跳过 workspace 加入申请')
        return []
    if not ids:
        log('  [K12] 未配置 workspace ID，跳过加入申请')
        return []
    proxies = _proxies_from_url(proxy)
    device_id = str(uuid.uuid4())
    results: list[dict] = []
    for ws_id in ids:
        ok = False
        message = ''
        for attempt in range(max_retries + 1):
            url = f'{CHATGPT_APP}/backend-api/accounts/{ws_id}/invites/request'
            headers = {
                'accept': '*/*',
                'authorization': f'Bearer {access_token}',
                'content-type': 'application/json',
                'oai-device-id': device_id,
                'oai-language': 'en-US',
            }
            if cookies:
                headers['cookie'] = cookies
            try:
                resp = cffi_requests.request(
                    'POST',
                    url,
                    headers=headers,
                    data='',
                    proxies=proxies,
                    verify=False,
                    timeout=timeout,
                    impersonate='chrome110',
                )
                status = int(getattr(resp, 'status_code', 0) or 0)
                text = getattr(resp, 'text', '') or ''
                if _join_response_ok(status, text):
                    ok = True
                    message = f'HTTP {status}: {text[:200]}'
                    log(f'  [K12] join {ws_id[:8]} 请求已接受: {message}')
                    break
                message = f'HTTP {status}: {text[:200]}'
                if status in (401, 403):
                    log(f'  [K12] join {ws_id[:8]} 鉴权失败: {message}')
                    break
                if _is_unusable_workspace_response(status, text):
                    log(f'  [K12] join {ws_id[:8]} 空间不可用: {message}')
                    break
            except Exception as exc:
                message = f'网络错误: {exc}'
            log(f'  [K12] join {ws_id[:8]} 第 {attempt + 1} 次失败: {message}')
            if attempt < max_retries:
                time.sleep(retry_backoff_ms / 1000 * (attempt + 1))
        results.append({'workspace_id': ws_id, 'ok': ok, 'message': message})
    return results


# ===================== 切换到 K12 workspace 并拿"新 session" =====================

_EXCHANGE_HEADERS_BASE: dict[str, str] = {
    "accept": "*/*",
    "accept-encoding": "gzip, deflate, br, zstd",
    "accept-language": "zh-CN,zh;q=0.9",
    "priority": "u=1, i",
    "referer": f"{CHATGPT_APP}/",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    ),
    "x-openai-target-path": "/api/auth/session",
    "x-openai-target-route": "/api/auth/session",
}


def exchange_workspace_session(
    cookies: str,
    workspace_id: str,
    *,
    access_token: str = "",
    proxy: str | None = None,
    log: Any = print,
    max_retries: int = 3,
    retry_backoff_ms: int = 3000,
    timeout: int = 30,
) -> dict | None:
    """通过 exchange_workspace_token=true 一次性完成切换工作区并拿到新 session JSON。

    HAR 抓包显示浏览器只请求了一次 GET /api/auth/session?exchange_workspace_token=true...，
    响应即为切换后的 session；无需再单独调无参 /api/auth/session。
    """
    if not workspace_id:
        return None
    device_id = str(uuid.uuid4())
    proxies = _proxies_from_url(proxy)
    url = (
        f"{CHATGPT_APP}/api/auth/session"
        f"?exchange_workspace_token=true"
        f"&workspace_id={workspace_id}"
        f"&reason=setCurrentAccount"
    )
    headers = dict(_EXCHANGE_HEADERS_BASE)
    headers["oai-device-id"] = device_id
    compact_cookies = compact_chatgpt_session_cookies(cookies)
    if compact_cookies:
        headers["cookie"] = compact_cookies
        if cookies and len(compact_cookies) < len(str(cookies)):
            log(f"  [K12] exchange cookie header 已压缩: {len(str(cookies))} -> {len(compact_cookies)}")

    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            resp = cffi_requests.request(
                "GET",
                url,
                headers=headers,
                proxies=proxies,
                verify=False,
                timeout=timeout,
                impersonate="chrome110",
            )
            status = int(getattr(resp, "status_code", 0) or 0)
            text = getattr(resp, "text", "") or ""
            if 200 <= status < 300 and text:
                log(f"  [K12] exchange {workspace_id[:8]} 响应 body (前500字符): {text[:500]}")
                try:
                    data = json.loads(text)
                except Exception as exc:
                    last_error = f"exchange 响应 JSON 解析失败: {exc}"
                    log(f"  [K12] exchange {workspace_id[:8]} 第 {attempt + 1} 次: {last_error}")
                else:
                    if isinstance(data, dict) and (data.get("accessToken") or data.get("access_token")):
                        valid, reason = validate_workspace_exchange_session(data, workspace_id)
                        if valid:
                            log(f"  [K12] exchange {workspace_id[:8]} HTTP {status} 成功，已获取 ChatGPT account 标识 ({reason})")
                            return data
                        last_error = reason
                        log(f"  [K12] exchange {workspace_id[:8]} 第 {attempt + 1} 次校验失败: {last_error}")
                        break
                    last_error = f"exchange 响应缺少 accessToken: keys={list(data.keys()) if isinstance(data, dict) else type(data).__name__}"
                    if isinstance(data, dict) and list(data.keys()) == ["WARNING_BANNER"]:
                        last_error += "；疑似缺少有效 chatgpt.com NextAuth session/cookie"
                    log(f"  [K12] exchange {workspace_id[:8]} 第 {attempt + 1} 次: {last_error}")
            else:
                last_error = f"HTTP {status}: {text[:500]}"
                log(f"  [K12] exchange {workspace_id[:8]} 第 {attempt + 1} 次失败: {last_error}")
                if status in (401, 403):
                    break
                if _is_unusable_workspace_response(status, text):
                    log(f"  [K12] exchange {workspace_id[:8]} 空间不可用，跳过当前 workspace")
                    break
        except Exception as exc:
            last_error = f"网络错误: {exc}"
            log(f"  [K12] exchange {workspace_id[:8]} 第 {attempt + 1} 次失败: {last_error}")
        if attempt < max_retries:
            time.sleep(retry_backoff_ms / 1000 * (attempt + 1))
    log(f"  [K12] exchange {workspace_id[:8]} 重试 {max_retries + 1} 次仍失败: {last_error}")
    return None

def send_workspace_join_and_pick_first_success(
    access_token: str,
    cookies: str,
    workspace_ids: str,
    *,
    proxy: str | None = None,
    log: Any = print,
    max_retries: int = 3,
    retry_backoff_ms: int = 5000,
    timeout: int = 30,
) -> tuple[str, list[dict]]:
    """按顺序 join，命中第一个 HTTP 2xx 即返回该 workspace_id 与全部结果。

    返回 (chosen_workspace_id, results)。chosen 为空字符串表示全部失败。
    """
    ids = parse_workspace_ids(workspace_ids)
    if not access_token or not ids:
        return "", []
    proxies = _proxies_from_url(proxy)
    device_id = str(uuid.uuid4())
    results: list[dict] = []
    chosen = ""
    for ws_id in ids:
        ok = False
        message = ""
        for attempt in range(max_retries + 1):
            url = f"{CHATGPT_APP}/backend-api/accounts/{ws_id}/invites/request"
            headers = {
                "accept": "*/*",
                "authorization": f"Bearer {access_token}",
                "content-type": "application/json",
                "oai-device-id": device_id,
                "oai-language": "en-US",
            }
            if cookies:
                headers["cookie"] = cookies
            try:
                resp = cffi_requests.request(
                    "POST",
                    url,
                    headers=headers,
                    data="",
                    proxies=proxies,
                    verify=False,
                    timeout=timeout,
                    impersonate="chrome110",
                )
                status = int(getattr(resp, "status_code", 0) or 0)
                text = getattr(resp, "text", "") or ""
                if _join_response_ok(status, text):
                    ok = True
                    message = f"HTTP {status}: {text[:200]}"
                    log(f"  [K12] join {ws_id[:8]} 请求已接受: {message}")
                    break
                message = f"HTTP {status}: {text[:200]}"
                if status in (401, 403):
                    log(f"  [K12] join {ws_id[:8]} 鉴权失败: {message}")
                    break
                if _is_unusable_workspace_response(status, text):
                    log(f"  [K12] join {ws_id[:8]} 空间不可用: {message}")
                    break
            except Exception as exc:
                message = f"网络错误: {exc}"
            log(f"  [K12] join {ws_id[:8]} 第 {attempt + 1} 次失败: {message}")
            if attempt < max_retries:
                time.sleep(retry_backoff_ms / 1000 * (attempt + 1))
        results.append({"workspace_id": ws_id, "ok": ok, "message": message})
        if ok and not chosen:
            chosen = ws_id
            # 命中第一个即停，剩余 workspace 跳过
            log(f"  [K12] 命中首个成功 workspace {ws_id[:8]}，跳过后续 {max(len(ids) - len(results), 0)} 个")
            break
    return chosen, results
