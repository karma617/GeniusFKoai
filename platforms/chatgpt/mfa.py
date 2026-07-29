from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from typing import Any, Callable

from platforms.chatgpt.payment_protocol import build_protocol_session


CHATGPT_APP = "https://chatgpt.com"
MFA_ENROLL_URL = f"{CHATGPT_APP}/backend-api/accounts/mfa/enroll"
MFA_ACTIVATE_URL = f"{CHATGPT_APP}/backend-api/accounts/mfa/user/activate_enrollment"
MFA_INFO_URL = f"{CHATGPT_APP}/backend-api/accounts/mfa_info"

DEFAULT_MFA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
DEFAULT_CLIENT_VERSION = "prod-01e2e28be691381cd82f4d9cc32c32f65c723ad8"
DEFAULT_CLIENT_BUILD_NUMBER = "8690212"


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
    value = _text(cookies)
    if value:
        return value
    token = _text(session_token)
    if token:
        return f"__Secure-next-auth.session-token={token}"
    return ""


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
) -> dict[str, str]:
    headers = {
        "accept": "application/json",
        "accept-language": "zh-CN,zh;q=0.9",
        "cache-control": "no-cache",
        "oai-client-build-number": DEFAULT_CLIENT_BUILD_NUMBER,
        "oai-client-version": DEFAULT_CLIENT_VERSION,
        "oai-device-id": _text(device_id) or _extract_cookie_value(cookies, "oai-did") or str(uuid.uuid4()),
        "oai-language": "zh-CN",
        "oai-session-id": _text(session_id) or str(uuid.uuid4()),
        "pragma": "no-cache",
        "referer": f"{CHATGPT_APP}/",
        "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": DEFAULT_MFA_USER_AGENT,
        "x-openai-target-path": path,
        "x-openai-target-route": path,
    }
    if method.upper() == "POST":
        headers.update(
            {
                "content-type": "application/json",
                "origin": CHATGPT_APP,
            }
        )
    token = _text(access_token)
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers


def enable_totp_mfa(
    *,
    cookies: str = "",
    session_token: str = "",
    access_token: str = "",
    proxy: str | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    cookie_header = _cookie_header(cookies, session_token)
    if not (cookie_header or _text(access_token)):
        raise RuntimeError("缺少 ChatGPT cookies/session_token/access_token，跳过 2FA 设置")

    log = log_fn or (lambda _message: None)
    session = build_protocol_session(
        proxy=proxy,
        cookies_str=cookie_header,
        impersonate="chrome136",
    )
    session.headers.update(
        {
            "User-Agent": DEFAULT_MFA_USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )

    oai_session_id = str(uuid.uuid4())
    device_id = _extract_cookie_value(cookie_header, "oai-did")

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
        ),
        timeout=30,
    )
    info_status = int(getattr(info_resp, "status_code", 0) or 0)
    info_data = _response_json(info_resp)
    enabled = bool(isinstance(info_data, dict) and (info_data.get("mfa_enabled") or info_data.get("mfa_enabled_v2")))
    if info_status != 200 or not enabled:
        raise RuntimeError(f"2FA 状态确认失败 HTTP {info_status}: {_text(getattr(info_resp, 'text', ''))[:200]}")

    return {
        "ok": True,
        "totp_secret": secret,
        "mfa_session_id": activation_session_id,
        "mfa_factor_id": factor_id or _text(info_data.get("native_default_factor_id")),
        "mfa_info": info_data,
        "current_code": generate_totp_code(secret),
    }
