from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import uuid
from typing import Any, Callable

from platforms.chatgpt.payment_protocol import build_protocol_session


CHATGPT_APP = "https://chatgpt.com"
MFA_ENROLL_URL = f"{CHATGPT_APP}/backend-api/accounts/mfa/enroll"
MFA_ACTIVATE_URL = f"{CHATGPT_APP}/backend-api/accounts/mfa/user/activate_enrollment"
MFA_INFO_URL = f"{CHATGPT_APP}/backend-api/accounts/mfa_info"

DEFAULT_MFA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) "
    "Gecko/20100101 Firefox/135.0"
)
DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.5"
DEFAULT_SEC_CH_UA = '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"'
DEFAULT_CLIENT_VERSION = "prod-fc98dd36cc7acf295fb888b3b2c9e7c00ad14591"
DEFAULT_CLIENT_BUILD_NUMBER = "8823441"
SECURITY_PREFLIGHT_PATHS = (
    "/backend-api/accounts/mfa_info",
    "/backend-api/accounts/security_settings/info",
    "/backend-api/accounts/change_password/eligibility",
    "/backend-api/accounts/add_password/eligibility",
    "/backend-api/accounts/sessions",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _extract_cookie_value(cookies: str, name: str) -> str:
    target = str(name or "").strip()
    for part in str(cookies or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip() == target:
            return value.strip()
    return ""


def _cookie_header(cookies: str = "", session_token: str = "") -> str:
    """仅恢复 MFA API 所需的 ChatGPT 登录态 Cookie。"""
    parsed: dict[str, str] = {}
    for part in str(cookies or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name and value:
            parsed[name] = value
    token = _text(session_token)
    if token:
        parsed["__Secure-next-auth.session-token"] = token
    allowed_names = (
        "__Secure-next-auth.session-token",
        "__Secure-oai-is",
        "_account",
        "oai-did",
    )
    return "; ".join(
        f"{name}={parsed[name]}"
        for name in allowed_names
        if _text(parsed.get(name))
    )


def _primary_language(accept_language: str) -> str:
    for item in str(accept_language or "").split(","):
        lang = item.split(";", 1)[0].strip()
        if lang:
            return lang
    return "en-US"


def _client_observation() -> str:
    return "v1.r.p." + secrets.token_urlsafe(12)[:16]


def _response_json(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def generate_totp_code(secret: str, *, at_time: int | None = None, digits: int = 6, period: int = 30) -> str:
    normalized = _text(secret).replace(" ", "").upper()
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    key = base64.b32decode(normalized + padding)
    counter = int((time.time() if at_time is None else at_time) // period).to_bytes(8, "big")
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = (
        ((digest[offset] & 0x7F) << 24)
        | ((digest[offset + 1] & 0xFF) << 16)
        | ((digest[offset + 2] & 0xFF) << 8)
        | (digest[offset + 3] & 0xFF)
    )
    return str(binary % (10 ** digits)).zfill(digits)


def _headers(
    *,
    path: str,
    method: str,
    cookies: str,
    access_token: str = "",
    device_id: str = "",
    session_id: str = "",
    user_agent: str = "",
    accept_language: str = "",
    client_version: str = "",
    client_build_number: str = "",
    content_type_for_post: bool = True,
    observation_id: str = "",
) -> dict[str, str]:
    effective_user_agent = _text(user_agent) or DEFAULT_MFA_USER_AGENT
    effective_accept_language = _text(accept_language) or DEFAULT_ACCEPT_LANGUAGE
    headers = {
        "accept": "*/*",
        "accept-language": effective_accept_language,
        "oai-client-build-number": _text(client_build_number) or DEFAULT_CLIENT_BUILD_NUMBER,
        "oai-client-version": _text(client_version) or DEFAULT_CLIENT_VERSION,
        "oai-device-id": _text(device_id) or _extract_cookie_value(cookies, "oai-did") or str(uuid.uuid4()),
        "oai-language": _primary_language(effective_accept_language),
        "oai-session-id": _text(session_id) or str(uuid.uuid4()),
        "referer": f"{CHATGPT_APP}/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": effective_user_agent,
        "x-oai-is-client-observation": _text(observation_id) or _client_observation(),
        "x-openai-target-path": path,
        "x-openai-target-route": path,
    }
    if "Chrome/" in effective_user_agent and "Firefox/" not in effective_user_agent:
        headers.update(
            {
                "sec-ch-ua": DEFAULT_SEC_CH_UA,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "priority": "u=1, i",
            }
        )
    if method.upper() == "POST":
        headers["origin"] = CHATGPT_APP
        if content_type_for_post:
            headers["content-type"] = "application/json"
    token = _text(access_token)
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers


def _security_get(
    session: Any,
    *,
    path: str,
    cookie_header: str,
    access_token: str,
    device_id: str,
    session_id: str,
    user_agent: str,
    accept_language: str,
    client_version: str,
    client_build_number: str,
    observation_id: str = "",
):
    return session.get(
        f"{CHATGPT_APP}{path}",
        headers=_headers(
            path=path,
            method="GET",
            cookies=cookie_header,
            access_token=access_token,
            device_id=device_id,
            session_id=session_id,
            user_agent=user_agent,
            accept_language=accept_language,
            client_version=client_version,
            client_build_number=client_build_number,
            observation_id=observation_id,
        ),
        timeout=30,
    )


def enable_totp_mfa(
    *,
    cookies: str = "",
    session_token: str = "",
    access_token: str = "",
    proxy: str | None = None,
    log_fn: Callable[[str], None] | None = None,
    user_agent: str = "",
    accept_language: str = "",
    client_version: str = "",
    client_build_number: str = "",
    device_id: str = "",
    oai_session_id: str = "",
    impersonate: str = "",
) -> dict[str, Any]:
    cookie_header = _cookie_header(cookies, session_token)
    if not (cookie_header or _text(access_token)):
        raise RuntimeError("缺少 ChatGPT cookies/session_token/access_token，跳过 2FA 设置")

    log = log_fn or (lambda _message: None)
    session = build_protocol_session(
        proxy=proxy,
        cookies_str=cookie_header,
        impersonate=impersonate or "firefox135",
    )
    effective_user_agent = _text(user_agent) or DEFAULT_MFA_USER_AGENT
    effective_accept_language = _text(accept_language) or DEFAULT_ACCEPT_LANGUAGE
    session.headers.update(
        {
            "User-Agent": effective_user_agent,
            "Accept-Language": effective_accept_language,
        }
    )

    oai_session_id = _text(oai_session_id) or str(uuid.uuid4())
    device_id = _text(device_id) or _extract_cookie_value(cookie_header, "oai-did")

    log("2FA: 预热账号安全设置接口")
    first_security_observation = _client_observation()
    settings_security_observation = _client_observation()
    for index, path in enumerate(SECURITY_PREFLIGHT_PATHS):
        response = _security_get(
            session,
            path=path,
            cookie_header=cookie_header,
            access_token=access_token,
            device_id=device_id,
            session_id=oai_session_id,
            user_agent=effective_user_agent,
            accept_language=effective_accept_language,
            client_version=client_version,
            client_build_number=client_build_number,
            observation_id=first_security_observation if index == 0 else settings_security_observation,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        log(f"2FA: 预热 {path} 状态: {status}")
        if status >= 400:
            raise RuntimeError(f"2FA preflight {path} HTTP {status}: {_text(getattr(response, 'text', ''))[:200]}")

    log("2FA: 创建 TOTP enrollment")
    enroll_resp = session.post(
        MFA_ENROLL_URL,
        headers=_headers(
            path="/backend-api/accounts/mfa/enroll",
            method="POST",
            cookies=cookie_header,
            access_token=access_token,
            device_id=device_id,
            session_id=oai_session_id,
            user_agent=effective_user_agent,
            accept_language=effective_accept_language,
            client_version=client_version,
            client_build_number=client_build_number,
        ),
        json={"factor_type": "totp"},
        timeout=30,
    )
    enroll_status = int(getattr(enroll_resp, "status_code", 0) or 0)
    enroll_data = _response_json(enroll_resp)
    if enroll_status != 200 or not isinstance(enroll_data, dict):
        raise RuntimeError(f"2FA enrollment HTTP {enroll_status}: {_text(getattr(enroll_resp, 'text', ''))[:200]}")

    secret = _text(enroll_data.get("secret"))
    activation_session_id = _text(enroll_data.get("session_id"))
    factor = enroll_data.get("factor") if isinstance(enroll_data.get("factor"), dict) else {}
    factor_id = _text(factor.get("id"))
    if not secret or not activation_session_id:
        raise RuntimeError("2FA enrollment 响应缺少 secret/session_id")

    mid_info_resp = _security_get(
        session,
        path="/backend-api/accounts/mfa_info",
        cookie_header=cookie_header,
        access_token=access_token,
        device_id=device_id,
        session_id=oai_session_id,
        user_agent=effective_user_agent,
        accept_language=effective_accept_language,
        client_version=client_version,
        client_build_number=client_build_number,
    )
    log(f"2FA: enrollment 后 mfa_info 状态: {int(getattr(mid_info_resp, 'status_code', 0) or 0)}")

    code = generate_totp_code(secret)
    log("2FA: 生成 TOTP 验证码并激活")
    activate_resp = session.post(
        MFA_ACTIVATE_URL,
        headers=_headers(
            path="/backend-api/accounts/mfa/user/activate_enrollment",
            method="POST",
            cookies=cookie_header,
            access_token=access_token,
            device_id=device_id,
            session_id=oai_session_id,
            user_agent=effective_user_agent,
            accept_language=effective_accept_language,
            client_version=client_version,
            client_build_number=client_build_number,
        ),
        json={"code": code, "factor_type": "totp", "session_id": activation_session_id},
        timeout=30,
    )
    activate_status = int(getattr(activate_resp, "status_code", 0) or 0)
    activate_data = _response_json(activate_resp)
    if activate_status != 200 or not isinstance(activate_data, dict) or not activate_data.get("success"):
        raise RuntimeError(f"2FA activate HTTP {activate_status}: {_text(getattr(activate_resp, 'text', ''))[:200]}")

    info_resp = session.get(
        MFA_INFO_URL,
        headers=_headers(
            path="/backend-api/accounts/mfa_info",
            method="GET",
            cookies=cookie_header,
            access_token=access_token,
            device_id=device_id,
            session_id=oai_session_id,
            user_agent=effective_user_agent,
            accept_language=effective_accept_language,
            client_version=client_version,
            client_build_number=client_build_number,
        ),
        timeout=30,
    )
    info_status = int(getattr(info_resp, "status_code", 0) or 0)
    info_data = _response_json(info_resp)
    enabled = bool(isinstance(info_data, dict) and (info_data.get("mfa_enabled") or info_data.get("mfa_enabled_v2")))
    if info_status != 200 or not enabled:
        raise RuntimeError(f"2FA 状态确认失败 HTTP {info_status}: {_text(getattr(info_resp, 'text', ''))[:200]}")

    try:
        heartbeat_resp = session.post(
            f"{CHATGPT_APP}/backend-api/sentinel/heartbeat",
            headers=_headers(
                path="/backend-api/sentinel/heartbeat",
                method="POST",
                cookies=cookie_header,
                access_token=access_token,
                device_id=device_id,
                session_id=oai_session_id,
                user_agent=effective_user_agent,
                accept_language=effective_accept_language,
                client_version=client_version,
                client_build_number=client_build_number,
                content_type_for_post=False,
            ),
            timeout=30,
        )
        log(f"2FA: Sentinel heartbeat 状态: {int(getattr(heartbeat_resp, 'status_code', 0) or 0)}")
    except Exception as exc:
        log(f"2FA: Sentinel heartbeat 失败，继续返回绑定结果: {exc}")

    return {
        "ok": True,
        "totp_secret": secret,
        "mfa_session_id": activation_session_id,
        "mfa_factor_id": factor_id or _text(info_data.get("native_default_factor_id")),
        "mfa_info": info_data,
        "current_code": generate_totp_code(secret),
    }
