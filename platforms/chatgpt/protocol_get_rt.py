from __future__ import annotations

import json
import time
import urllib.parse
import uuid
from typing import Any, Callable

from .browser_get_rt import build_get_rt_phone_callback
from .constants import CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE, OPENAI_API_ENDPOINTS, OPENAI_AUTH
from .http_client import OpenAIHTTPClient
from .oauth import generate_oauth_url, submit_callback_url
from .register import RegistrationEngine, _extract_oauth_callback_params_from_url


class _GetRtMailboxEmailService:
    def __init__(self, otp_callback: Callable[[], str], *, log_fn: Callable[[str], None], email: str):
        self._otp_callback = otp_callback
        self._log = log_fn
        self._email = email
        self.service_type = type("ST", (), {"value": "get_rt_mailbox"})()

    def create_email(self, config=None):
        return {"email": self._email, "service_id": self._email, "token": self._email}

    def get_verification_code(self, email=None, email_id=None, timeout=120, pattern=None, otp_sent_at=None):
        return self._otp_callback(
            timeout=timeout,
            pattern=pattern,
            otp_sent_at=otp_sent_at,
        )

    def update_status(self, success, error=None):
        return None

    @property
    def status(self):
        return None


def _json_headers(engine: RegistrationEngine, *, device_id: str, referer: str) -> dict[str, str]:
    return engine._platform_json_headers(device_id=device_id, referer=referer)


def _post_json(session, url: str, *, headers: dict[str, str], body: dict[str, Any], allow_redirects: bool = True, timeout: int = 30):
    return session.post(
        url,
        headers=headers,
        data=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        allow_redirects=allow_redirects,
        timeout=timeout,
    )


def _post_empty_json(session, url: str, *, headers: dict[str, str], allow_redirects: bool = True, timeout: int = 30):
    return session.post(
        url,
        headers=headers,
        data="",
        allow_redirects=allow_redirects,
        timeout=timeout,
    )


def _extract_continue_url(resp) -> str:
    headers = getattr(resp, "headers", {}) or {}
    location = str(headers.get("Location") or headers.get("location") or "").strip()
    if location:
        return location
    return _extract_continue_url_from_payload(_response_json(resp))


def _response_json(resp) -> dict[str, Any]:
    try:
        data = resp.json() or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_continue_url_from_payload(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    return str(data.get("continue_url") or data.get("redirect_url") or "").strip()


def _extract_page_type(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    page = data.get("page") or {}
    return str((page if isinstance(page, dict) else {}).get("type") or "").strip()


def _is_mfa_challenge_payload(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    return _extract_page_type(data) == "mfa_challenge" or "/mfa-challenge" in _extract_continue_url_from_payload(data)


def _short_url(url: str, *, limit: int = 220) -> str:
    value = str(url or "").strip()
    return value if len(value) <= limit else value[:limit] + "..."


def _normalize_auth_url(url: str, *, base_url: str = OPENAI_AUTH) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    return urllib.parse.urljoin(base_url, value)


def _is_codex_consent_url(url: str) -> bool:
    path = urllib.parse.urlsplit(str(url or "")).path.rstrip("/")
    return path == "/sign-in-with-chatgpt/codex/consent"


def _codex_consent_data_url(consent_url: str) -> str:
    parsed = urllib.parse.urlsplit(consent_url)
    return urllib.parse.urlunsplit(
        (
            parsed.scheme or "https",
            parsed.netloc or urllib.parse.urlsplit(OPENAI_AUTH).netloc,
            "/sign-in-with-chatgpt/codex/consent.data",
            "_routes=SIGN_IN_WITH_CHATGPT_CODEX_CONSENT",
            "",
        )
    )


def _extract_workspace_id_from_payload(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    auth_session = data.get("oai-client-auth-session") or data.get("auth_session") or data
    if not isinstance(auth_session, dict):
        return ""
    workspaces = auth_session.get("workspaces") or []
    if not isinstance(workspaces, list) or not workspaces:
        return ""
    first = workspaces[0] if isinstance(workspaces[0], dict) else {}
    return str(first.get("id") or "").strip()


def _extract_org_selection_body(data: Any) -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    orgs = list((((data.get("data") or {}).get("orgs")) or []))
    if not orgs or not isinstance(orgs[0], dict) or not orgs[0].get("id"):
        return {}
    body = {"org_id": str(orgs[0].get("id") or "").strip()}
    projects = list(orgs[0].get("projects") or [])
    if projects and isinstance(projects[0], dict) and projects[0].get("id"):
        body["project_id"] = str(projects[0].get("id") or "").strip()
    return body


def _resolve_callback_from_continue_url(
    *,
    session,
    engine: RegistrationEngine,
    device_id: str,
    continue_url: str,
    auth_payload: dict[str, Any],
    referer: str,
    log_fn: Callable[[str], None],
) -> str:
    current_url = _normalize_auth_url(continue_url)
    if not current_url:
        return ""
    if _extract_oauth_callback_params_from_url(current_url):
        return current_url

    page_type = _extract_page_type(auth_payload)
    log_fn(f"  get_rt(protocol): OAuth continue_url={_short_url(current_url)} page={page_type or '-'}")

    if _is_codex_consent_url(current_url):
        consent_data_url = _codex_consent_data_url(current_url)
        try:
            consent_resp = session.get(
                consent_data_url,
                headers={"accept": "*/*", "referer": referer or current_url},
                allow_redirects=True,
                timeout=30,
            )
            log_fn(f"  get_rt(protocol): consent.data -> {getattr(consent_resp, 'status_code', 0)}")
            _log_response_debug(log_fn, "consent.data", consent_resp)
        except Exception as exc:
            log_fn(f"  get_rt(protocol): consent.data failed: {exc}")

        workspace_id = _extract_workspace_id_from_payload(auth_payload)
        if not workspace_id:
            try:
                workspace_id = _extract_workspace_id_from_payload(engine._decode_client_auth_session_cookie(session))
            except Exception:
                workspace_id = ""
        if workspace_id:
            log_fn(f"  get_rt(protocol): workspace/select request workspace_id={workspace_id[:8]}...")
            ws_resp = _post_json(
                session,
                OPENAI_API_ENDPOINTS["select_workspace"],
                headers=_json_headers(engine, device_id=device_id, referer=current_url),
                body={"workspace_id": workspace_id},
                allow_redirects=False,
                timeout=30,
            )
            log_fn(f"  get_rt(protocol): workspace/select -> {getattr(ws_resp, 'status_code', 0)}")
            _log_response_debug(log_fn, "workspace/select", ws_resp)
            ws_data = _response_json(ws_resp)
            next_url = _normalize_auth_url(_extract_continue_url(ws_resp), base_url=current_url)
            if next_url:
                if _extract_oauth_callback_params_from_url(next_url):
                    return next_url
                callback_url = engine._follow_platform_redirects_for_callback(session, next_url)
                if callback_url:
                    return callback_url

            org_body = _extract_org_selection_body(ws_data)
            if org_body:
                org_resp = _post_json(
                    session,
                    f"{OPENAI_AUTH}/api/accounts/organization/select",
                    headers=_json_headers(engine, device_id=device_id, referer=current_url),
                    body=org_body,
                    allow_redirects=False,
                    timeout=30,
                )
                log_fn(f"  get_rt(protocol): organization/select -> {getattr(org_resp, 'status_code', 0)}")
                _log_response_debug(log_fn, "organization/select", org_resp)
                org_next_url = _normalize_auth_url(_extract_continue_url(org_resp), base_url=current_url)
                if org_next_url:
                    if _extract_oauth_callback_params_from_url(org_next_url):
                        return org_next_url
                    callback_url = engine._follow_platform_redirects_for_callback(session, org_next_url)
                    if callback_url:
                        return callback_url
        else:
            log_fn("  get_rt(protocol): workspace/select skipped: no workspace_id")

    callback_url = engine._follow_platform_redirects_for_callback(session, current_url)
    if callback_url:
        return callback_url
    return ""


def _safe_json_or_text(resp, *, limit: int = 600) -> str:
    try:
        data = resp.json()
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        text = str(getattr(resp, "text", "") or "")
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _log_response_debug(log_fn, label: str, resp) -> None:
    headers = getattr(resp, "headers", {}) or {}
    status = int(getattr(resp, "status_code", 0) or 0)
    url = str(getattr(resp, "url", "") or "")
    location = str(headers.get("Location") or headers.get("location") or "").strip()
    content_type = str(headers.get("content-type") or headers.get("Content-Type") or "").strip()
    bits = [
        f"status={status}",
        f"url={url[:220] or '-'}",
        f"content_type={content_type or '-'}",
    ]
    if location:
        bits.append(f"location={location[:220]}")
    log_fn(f"  获取rt(协议): {label} debug: " + " ".join(bits))
    if status >= 400:
        log_fn(f"  获取rt(协议): {label} body: {_safe_json_or_text(resp)}")


def _continue_error_message(resp) -> str:
    try:
        data = resp.json()
    except Exception:
        data = {}
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "").strip()
            message = str(error.get("message") or error.get("type") or "").strip()
            if code or message:
                return f"{code}: {message}".strip(": ")
        for key in ("code", "message", "detail", "error_description"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    return _safe_json_or_text(resp, limit=240)


def _refresh_otp_callback_baseline(otp_callback, log_fn: Callable[[str], None]) -> None:
    refresh = getattr(otp_callback, "refresh_before_ids", None)
    if not callable(refresh):
        return
    try:
        before_ids = refresh()
        try:
            count = len(before_ids or [])
        except Exception:
            count = 0
        log_fn(f"  get_rt(protocol): mailbox baseline refreshed before OTP resend: before_ids={count}")
    except Exception as exc:
        log_fn(f"  get_rt(protocol): mailbox baseline refresh failed before OTP resend: {exc}")


def _is_retryable_email_otp_status(status: int) -> bool:
    return int(status or 0) in {400, 401, 409}


def _is_login_cooldown_error_text(text: str) -> bool:
    value = str(text or "").lower()
    if not value:
        return False
    return any(
        marker in value
        for marker in (
            "too many tries",
            "please wait a few minutes",
            "too many attempts",
            "too many requests",
            "try again later",
        )
    )


def _login_cooldown_error(stage: str, detail: str = "") -> RuntimeError:
    suffix = f": {detail}" if detail else ""
    return RuntimeError(f"GET_RT_EMAIL_LOGIN_COOLDOWN: {stage}{suffix}")


def _wait_before_authorize_retry(log_fn: Callable[[str], None], delay_seconds: int = 5) -> None:
    delay = max(1, int(delay_seconds or 5))
    log_fn(f"  获取rt(协议): authorize 重试退避 {delay}s，等待 Cloudflare/登录状态稳定")
    time.sleep(delay)


def _is_login_restart_required_text(text: str) -> bool:
    value = str(text or "").lower()
    if not value:
        return False
    return any(
        marker in value
        for marker in (
            "invalid_state",
            "invalid_auth_step",
            "invalid authorization step",
            "sign-in session is no longer valid",
            "session is no longer valid",
            "please start over",
            "start over to continue",
        )
    )


def _login_restart_required_error(stage: str, detail: str = "") -> RuntimeError:
    suffix = f": {detail}" if detail else ""
    return RuntimeError(f"GET_RT_LOGIN_RESTART_REQUIRED: {stage}{suffix}")


_GET_RT_LOGIN_MAX_ATTEMPTS = 3
_GET_RT_LOGIN_RETRY_DELAYS = (5, 10)

_CLOUDFLARE_CHALLENGE_MARKERS = (
    "just a moment",
    "challenges.cloudflare.com",
    "cf-challenge",
    "cf_chl_opt",
    "__cf_chl",
    "attention required",
    "cf-browser-verification",
)


def _response_body_lower(resp, *, limit: int = 4000) -> str:
    return str(getattr(resp, "text", "") or "")[:limit].lower()


def _is_cloudflare_challenge_response(resp) -> bool:
    """识别 Cloudflare managed challenge / block HTML 页面。"""
    body = _response_body_lower(resp)
    if any(marker in body for marker in _CLOUDFLARE_CHALLENGE_MARKERS):
        return True
    status = int(getattr(resp, "status_code", 0) or 0)
    if status < 400:
        return False
    headers = getattr(resp, "headers", {}) or {}
    server = str(headers.get("server") or headers.get("Server") or "").strip().lower()
    content_type = str(
        headers.get("content-type") or headers.get("Content-Type") or ""
    ).strip().lower()
    return server == "cloudflare" and "text/html" in content_type


def _is_authorize_page_ready(resp) -> bool:
    """authorize 响应是否已进入有效 2xx/登录页面（排除 Cloudflare challenge）。"""
    status = int(getattr(resp, "status_code", 0) or 0)
    if status < 200 or status >= 300:
        return False
    return not _is_cloudflare_challenge_response(resp)


def _bootstrap_authorize_until_ready(
    *,
    session,
    auth_url: str,
    log_fn: Callable[[str], None],
    stage: str,
    max_attempts: int = _GET_RT_LOGIN_MAX_ATTEMPTS,
    retry_delays: tuple[int, ...] = _GET_RT_LOGIN_RETRY_DELAYS,
):
    """在同一 engine/session/proxy 内重新 bootstrap authorize，直到进入有效 2xx/登录页面。

    未就绪期间严禁提交 authorize/continue；耗尽后抛 GET_RT_LOGIN_RESTART_REQUIRED，
    由外层完整重启（新建 engine/session/proxy）。
    """
    response = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            delay = retry_delays[min(attempt - 2, len(retry_delays) - 1)] if retry_delays else 5
            _wait_before_authorize_retry(log_fn, delay)
        response = session.get(auth_url, timeout=30, allow_redirects=True)
        status = int(getattr(response, "status_code", 0) or 0)
        ready = _is_authorize_page_ready(response)
        log_fn(
            f"  获取rt(协议): authorize attempt={attempt}/{max_attempts} "
            f"-> status={status} ready={ready} "
            f"cf_challenge={_is_cloudflare_challenge_response(response)} stage={stage}"
        )
        _log_response_debug(log_fn, f"authorize attempt {attempt}", response)
        if ready:
            return response
        log_fn(
            "  获取rt(协议): authorize 未进入有效登录页面，禁止提交 authorize/continue: "
            f"stage={stage} attempt={attempt}/{max_attempts}"
        )
    raise _login_restart_required_error(
        stage,
        f"authorize 连续 {max_attempts} 次未进入有效 2xx/登录页面: "
        f"HTTP {int(getattr(response, 'status_code', 0) or 0)}",
    )


def _build_continue_sentinel(
    *,
    engine: RegistrationEngine,
    client,
    device_id: str,
    log_fn: Callable[[str], None],
) -> str:
    try:
        sentinel_header = engine._build_sentinel_header_for_client(client, device_id, "authorize_continue")
    except Exception as exc:
        raise RuntimeError(f"获取rt协议模式 Sentinel 初始化失败: {exc}") from exc
    log_fn("  获取rt(协议): Sentinel 已就绪")
    return sentinel_header


def _post_authorize_continue(
    *,
    session,
    engine: RegistrationEngine,
    device_id: str,
    email: str,
    sentinel_header: str,
    log_fn: Callable[[str], None],
    label: str = "authorize/continue",
):
    continue_body = {"username": {"kind": "email", "value": email}}
    continue_headers = _json_headers(engine, device_id=device_id, referer=f"{OPENAI_AUTH}/log-in")
    continue_headers["openai-sentinel-token"] = sentinel_header
    log_fn(
        f"  获取rt(协议): authorize/continue request ({label}): "
        f"endpoint={OPENAI_API_ENDPOINTS['signup']} referer={continue_headers.get('referer') or ''} "
        f"body_keys={','.join(continue_body.keys())} sentinel=yes"
    )
    resp = _post_json(
        session,
        OPENAI_API_ENDPOINTS["signup"],
        headers=continue_headers,
        body=continue_body,
        allow_redirects=False,
        timeout=30,
    )
    _log_response_debug(log_fn, label, resp)
    return resp


def _send_platform_login_otp_checked(
    *,
    client: OpenAIHTTPClient,
    engine: RegistrationEngine,
    log_fn: Callable[[str], None],
) -> bool:
    if not hasattr(engine, "_platform_nav_headers"):
        return bool(engine._send_platform_login_otp(client))
    resp = client.session.get(
        OPENAI_API_ENDPOINTS["send_otp"],
        headers=engine._platform_nav_headers(referer=f"{OPENAI_AUTH}/email-verification"),
        allow_redirects=True,
        timeout=15,
    )
    status = int(getattr(resp, "status_code", 0) or 0)
    log_fn(f"  get_rt(protocol): email-otp/send -> {status}")
    _log_response_debug(log_fn, "email-otp/send", resp)
    if status in (200, 302):
        try:
            engine._otp_sent_at = time.time()
        except Exception:
            pass
        return True
    detail = _continue_error_message(resp)
    if _is_login_cooldown_error_text(detail):
        raise _login_cooldown_error("email-otp/send", detail)
    return False


def _is_retryable_phone_send_failure_text(text: str) -> bool:
    value = str(text or "").lower()
    if not value:
        return False
    return any(
        marker in value
        for marker in (
            "we couldn't send a text message",
            "we could not send a text message",
            "couldn't send a text",
            "could not send a text",
            "can't send a text",
            "cannot send a text",
            "unable to send a text",
            "switched to whatsapp",
            "continue to send a verification code on whatsapp",
        )
    )


def _is_phone_verification_rate_limit_text(text: str) -> bool:
    value = str(text or "").lower()
    if not value:
        return False
    if "too many phone verification requests" in value:
        return True
    if "made too many phone verification requests" in value:
        return True
    return "rate_limit_exceeded" in value and (
        "phone verification" in value
        or "phone_number" in value
        or "phone number" in value
        or "add_phone" in value
        or "add-phone" in value
    )


def _phone_verification_rate_limit_error(stage: str, detail: str = "") -> RuntimeError:
    suffix = f": {detail}" if detail else ""
    return RuntimeError(f"GET_RT_PHONE_VERIFICATION_RATE_LIMIT: {stage}{suffix}")


def _proxy_pool_required_error(stage: str, detail: str = "") -> RuntimeError:
    suffix = f": {detail}" if detail else ""
    return RuntimeError(f"GET_RT_PROXY_POOL_REQUIRED: 请使用代理池IP: {stage}{suffix}")


def _send_passwordless_login_otp(
    *,
    session,
    engine: RegistrationEngine,
    device_id: str,
    log_fn: Callable[[str], None],
) -> dict[str, Any]:
    headers = _json_headers(engine, device_id=device_id, referer=f"{OPENAI_AUTH}/log-in/password")
    resp = _post_empty_json(
        session,
        f"{OPENAI_AUTH}/api/accounts/passwordless/send-otp",
        headers=headers,
        allow_redirects=False,
        timeout=30,
    )
    log_fn(f"  \u83b7\u53d6rt(\u534f\u8bae): passwordless/send-otp -> {getattr(resp, 'status_code', 0)}")
    _log_response_debug(log_fn, "passwordless/send-otp", resp)
    if resp.status_code != 200:
        detail = _continue_error_message(resp)
        if _is_login_cooldown_error_text(detail):
            raise _login_cooldown_error("passwordless/send-otp", detail)
        raise RuntimeError(
            f"\u83b7\u53d6rt\u534f\u8bae\u6a21\u5f0f passwordless/send-otp \u5931\u8d25: HTTP {resp.status_code}"
            f"{(': ' + detail) if detail else ''}"
        )
    data = _response_json(resp)
    log_fn(
        "  \u83b7\u53d6rt(\u534f\u8bae): passwordless/send-otp -> "
        f"page={_extract_page_type(data) or '(unknown)'}"
    )
    return data


def _complete_mfa_challenge_if_needed(
    *,
    engine: RegistrationEngine,
    payload: dict[str, Any],
    log_fn: Callable[[str], None],
) -> dict[str, Any]:
    if not _is_mfa_challenge_payload(payload):
        return payload
    if not str(getattr(engine, "totp_secret", "") or "").strip():
        raise RuntimeError("获取rt协议模式进入 2FA challenge，但本地未保存 totp_secret")
    log_fn("  获取rt(协议): 进入 2FA challenge，使用本地 TOTP 完成验证")
    next_payload = engine._latest_chatgpt_complete_mfa_challenge(payload)
    return next_payload if isinstance(next_payload, dict) else {}


def _activate_add_phone_step(
    *,
    session,
    engine: RegistrationEngine,
    continue_url: str,
    referer: str,
    log_fn: Callable[[str], None],
) -> str:
    """Navigate to the post-MFA add-phone page before submitting a number."""
    add_phone_url = _normalize_auth_url(continue_url, base_url=OPENAI_AUTH)
    if not add_phone_url:
        add_phone_url = f"{OPENAI_AUTH}/add-phone"
    response = session.get(
        add_phone_url,
        headers=engine._platform_nav_headers(referer=referer),
        allow_redirects=True,
        timeout=30,
    )
    status = int(getattr(response, "status_code", 0) or 0)
    final_url = str(getattr(response, "url", "") or add_phone_url)
    log_fn(f"  获取rt(协议): add_phone 页面激活 -> {status} url={_short_url(final_url)}")
    _log_response_debug(log_fn, "add_phone activate", response)
    detail = _continue_error_message(response)
    if status >= 400 or _is_login_restart_required_text(detail):
        raise _login_restart_required_error("add-phone/activate", detail or f"HTTP {status}")
    return final_url


def run_protocol_get_rt(
    *,
    email: str,
    password: str,
    proxy: str | None,
    otp_callback,
    log_fn: Callable[[str], None],
    sms_provider: str = "",
    smspool_api_key: str = "",
    smspool_max_price: str = "0.13",
    smspool_country: str = "",
    smspool_service: str = "",
    smspool_base_url: str = "",
    smspool_compat_base_url: str = "",
    smspool_pricing_option: str = "",
    smspool_poll_interval: str = "",
    smsapi_phone: str = "",
    smsapi_url: str = "",
    phone_callback=None,
    phone_change_limit: int = 10,
    phone_code_timeout: int = 60,
    totp_secret: str = "",
    proxy_from_pool: bool = False,
) -> dict[str, Any]:
    email_service = _GetRtMailboxEmailService(otp_callback, log_fn=log_fn, email=email)
    engine = RegistrationEngine(
        email_service=email_service,
        proxy_url=proxy,
        callback_logger=log_fn,
    )
    engine.email = email
    engine.password = password

    client = OpenAIHTTPClient(proxy_url=proxy)
    session = client.session
    device_id = str(uuid.uuid4())
    engine.http_client = client
    engine.session = session
    engine._device_id = device_id
    engine.totp_secret = str(totp_secret or "").strip()
    prefer_password_totp_login = bool(engine.password and engine.totp_secret)
    engine._set_oai_did_for_session(session, device_id)

    oauth_start = generate_oauth_url(
        redirect_uri=CODEX_REDIRECT_URI,
        scope=CODEX_SCOPE,
        client_id=CODEX_CLIENT_ID,
    )
    log_fn(f"  获取rt(协议): OAuth 授权链接已生成 state={oauth_start.state[:18]}...")

    _bootstrap_authorize_until_ready(
        session=session,
        auth_url=oauth_start.auth_url,
        log_fn=log_fn,
        stage="authorize/bootstrap",
    )

    sentinel_header = _build_continue_sentinel(
        engine=engine, client=client, device_id=device_id, log_fn=log_fn
    )
    continue_resp = _post_authorize_continue(
        session=session,
        engine=engine,
        device_id=device_id,
        email=email,
        sentinel_header=sentinel_header,
        log_fn=log_fn,
        label="authorize/continue attempt 1",
    )

    continue_attempts_used = 1
    while (
        int(getattr(continue_resp, "status_code", 0) or 0) == 409
        and engine._is_invalid_state_response(continue_resp)
        and continue_attempts_used < _GET_RT_LOGIN_MAX_ATTEMPTS
    ):
        delay = _GET_RT_LOGIN_RETRY_DELAYS[
            min(continue_attempts_used - 1, len(_GET_RT_LOGIN_RETRY_DELAYS) - 1)
        ]
        log_fn(
            "  获取rt(协议): authorize/continue invalid_state，将重建 authorize 上下文与 Sentinel 后重试: "
            f"attempt={continue_attempts_used}/{_GET_RT_LOGIN_MAX_ATTEMPTS} delay={delay}s"
        )
        _wait_before_authorize_retry(log_fn, delay)
        _bootstrap_authorize_until_ready(
            session=session,
            auth_url=oauth_start.auth_url,
            log_fn=log_fn,
            stage="authorize/continue-recovery",
        )
        sentinel_header = _build_continue_sentinel(
            engine=engine, client=client, device_id=device_id, log_fn=log_fn
        )
        continue_attempts_used += 1
        continue_resp = _post_authorize_continue(
            session=session,
            engine=engine,
            device_id=device_id,
            email=email,
            sentinel_header=sentinel_header,
            log_fn=log_fn,
            label=f"authorize/continue attempt {continue_attempts_used}",
        )

    if int(getattr(continue_resp, "status_code", 0) or 0) == 409 and engine._is_invalid_state_response(continue_resp):
        log_fn(
            "  获取rt(协议): authorize/continue invalid_state 恢复耗尽，需完整重启登录: "
            f"attempts={continue_attempts_used}/{_GET_RT_LOGIN_MAX_ATTEMPTS}"
        )
        raise _login_restart_required_error(
            "authorize/continue",
            f"invalid_state 重试 {continue_attempts_used}/{_GET_RT_LOGIN_MAX_ATTEMPTS} 次后仍失败: "
            f"{_continue_error_message(continue_resp)}",
        )
    if continue_resp.status_code != 200:
        detail = _continue_error_message(continue_resp)
        if _is_login_cooldown_error_text(detail):
            raise _login_cooldown_error("authorize/continue", detail)
        raise RuntimeError(
            f"获取rt协议模式 authorize/continue 失败: HTTP {continue_resp.status_code}"
            f"{(': ' + detail) if detail else ''}"
        )
    continue_data = continue_resp.json() or {}
    page_type = str(((continue_data.get("page") or {}).get("type")) or "")
    log_fn(f"  获取rt(协议): authorize/continue -> page={page_type or '(unknown)'}")

    if page_type == "login_password":
        if prefer_password_totp_login:
            log_fn("  获取rt(协议): 检测到已保存密码和 2FA，直接使用密码 + TOTP，不触发邮箱验证码")
            continue_data = engine._latest_chatgpt_verify_login_password()
            page_type = _extract_page_type(continue_data)
        else:
            passwordless_data = _send_passwordless_login_otp(
                session=session,
                engine=engine,
                device_id=device_id,
                log_fn=log_fn,
            )
            if passwordless_data:
                continue_data = passwordless_data
                page_type = _extract_page_type(continue_data)

    if page_type in {"email_otp_send", "email_otp_verification"}:
        if prefer_password_totp_login:
            log_fn(f"  获取rt(协议): 密码+2FA账号返回邮箱验证码步骤，改用邮箱 OTP 后继续 TOTP: {page_type}")
        if not callable(otp_callback):
            raise RuntimeError(f"获取rt协议模式进入邮箱验证码步骤但邮箱 OTP 服务不可用: {page_type}")

    if page_type == "email_otp_send":
        log_fn("  \u83b7\u53d6rt(\u534f\u8bae): email_otp_send \u9875\u9762\uff0c\u663e\u5f0f\u89e6\u53d1\u90ae\u7bb1\u9a8c\u8bc1\u7801")
        if not _send_platform_login_otp_checked(client=client, engine=engine, log_fn=log_fn):
            raise RuntimeError("\u83b7\u53d6rt\u534f\u8bae\u6a21\u5f0f\u53d1\u9001\u90ae\u7bb1\u9a8c\u8bc1\u7801\u5931\u8d25")
        page_type = "email_otp_verification"

    if page_type == "email_otp_verification":
        otp_data = None
        max_invalid_retries = 3
        last_status = 0
        for validate_attempt in range(1, max_invalid_retries + 2):
            if validate_attempt > 1:
                log_fn(
                    "  get_rt(protocol): previous email OTP was rejected, "
                    f"resending a fresh code ({validate_attempt - 1}/{max_invalid_retries})"
                )
                _refresh_otp_callback_baseline(otp_callback, log_fn)
                send_ok = _send_platform_login_otp_checked(client=client, engine=engine, log_fn=log_fn)
                if not send_ok:
                    raise RuntimeError("获取rt协议模式发送邮箱验证码失败")

            code = engine._wait_platform_login_code(client)
            if not code:
                raise RuntimeError("获取rt协议模式等待邮箱验证码失败")
            otp_resp = engine._validate_platform_login_otp(client, device_id, code)
            last_status = int(getattr(otp_resp, "status_code", 0) or 0)
            log_fn(
                f"  获取rt(协议): email OTP validate -> {last_status} "
                f"attempt={validate_attempt}/{max_invalid_retries + 1}"
            )
            _log_response_debug(log_fn, f"email OTP validate attempt {validate_attempt}", otp_resp)
            if last_status == 200:
                otp_data = otp_resp.json() or {}
                break
            otp_detail = _continue_error_message(otp_resp)
            if _is_login_cooldown_error_text(otp_detail):
                raise _login_cooldown_error("email-otp/validate", otp_detail)
            if _is_retryable_email_otp_status(last_status) and validate_attempt <= max_invalid_retries:
                continue
            raise RuntimeError(f"获取rt协议模式邮箱验证码校验失败: HTTP {last_status}: {otp_detail}")
        if otp_data is None:
            raise RuntimeError(f"获取rt协议模式邮箱验证码校验失败: HTTP {last_status}")
    else:
        otp_data = continue_data

    oauth_payload = otp_data if isinstance(otp_data, dict) else {}
    oauth_payload = _complete_mfa_challenge_if_needed(engine=engine, payload=oauth_payload, log_fn=log_fn)
    oauth_continue_url = _extract_continue_url_from_payload(oauth_payload) or _extract_continue_url_from_payload(continue_data)
    oauth_referer = f"{OPENAI_AUTH}/email-verification" if page_type == "email_otp_verification" else f"{OPENAI_AUTH}/log-in"

    next_page = _extract_page_type(oauth_payload) or str(((otp_data.get("page") or {}).get("type")) or page_type or "")
    phone_callback_obj = phone_callback
    if next_page == "add_phone":
        _activate_add_phone_step(
            session=session,
            engine=engine,
            continue_url=oauth_continue_url,
            referer=oauth_referer,
            log_fn=log_fn,
        )
        if phone_callback_obj is None and sms_provider:
            phone_callback_obj, phone_error = build_get_rt_phone_callback(
                sms_provider=sms_provider,
                smspool_api_key=smspool_api_key,
                smspool_max_price=smspool_max_price,
                smspool_country=smspool_country,
                smspool_service=smspool_service,
                smspool_base_url=smspool_base_url,
                smspool_compat_base_url=smspool_compat_base_url,
                smspool_pricing_option=smspool_pricing_option,
                smspool_poll_interval=smspool_poll_interval,
                smsapi_phone=smsapi_phone,
                smsapi_url=smsapi_url,
                phone_change_limit=phone_change_limit,
                log_fn=log_fn,
            )
            if not phone_callback_obj:
                raise RuntimeError(f"获取rt协议模式手机回调创建失败: {phone_error}")
        if phone_callback_obj is None:
            raise RuntimeError("获取rt协议模式进入 add_phone，但未配置手机验证码回调")

        try:
            phone_code_timeout = max(1, int(phone_code_timeout or 60))
        except (TypeError, ValueError):
            phone_code_timeout = 60
        if hasattr(phone_callback_obj, "set_code_timeout"):
            phone_callback_obj.set_code_timeout(phone_code_timeout)
        log_fn(f"  获取rt(协议): 手机验证码等待上限 {phone_code_timeout}s，超时释放号码并切换下一个")

        last_send_error = ""
        for attempt in range(1, max(int(phone_change_limit or 1), 1) + 1):
            try:
                phone_number = phone_callback_obj()
            except Exception as exc:
                last_send_error = str(exc)[:240]
                try:
                    if hasattr(phone_callback_obj, "mark_send_failed"):
                        phone_callback_obj.mark_send_failed(last_send_error)
                except Exception:
                    pass
                log_fn(
                    "  \u83b7\u53d6rt(\u534f\u8bae): add_phone \u83b7\u53d6\u624b\u673a\u53f7\u5931\u8d25\uff0c"
                    f"\u4fdd\u6301\u5f53\u524d session \u7ee7\u7eed\u6362\u53f7 attempt={attempt} detail={last_send_error}"
                )
                if attempt >= 3 and attempt % 3 == 0:
                    log_fn(
                        f"  \u83b7\u53d6rt(\u534f\u8bae): add_phone \u8fde\u7eed {attempt} \u6b21\u83b7\u53d6\u624b\u673a\u53f7\u5931\u8d25\uff0c"
                        "\u4ecd\u5728\u5f53\u524d\u5df2\u767b\u5f55 session \u5185\u6362\u53f7"
                    )
                continue
            log_fn(f"  获取rt(协议): add_phone 第 {attempt} 次提交手机号 {phone_number}")
            add_phone_headers = _json_headers(engine, device_id=device_id, referer=f"{OPENAI_AUTH}/add-phone")
            send_resp = _post_json(
                session,
                f"{OPENAI_AUTH}/api/accounts/add-phone/send",
                headers=add_phone_headers,
                body={"phone_number": phone_number, "channel": "sms"},
                allow_redirects=True,
                timeout=30,
            )
            send_data = _response_json(send_resp)
            send_detail_text = ""
            if send_data:
                try:
                    send_detail_text = json.dumps(send_data, ensure_ascii=False, separators=(",", ":"))[:480]
                except Exception:
                    send_detail_text = str(send_data)[:480]
            else:
                send_detail_text = str(getattr(send_resp, "text", "") or "")[:480]

            if _is_login_restart_required_text(send_detail_text):
                last_send_error = send_detail_text[:240]
                try:
                    if hasattr(phone_callback_obj, "mark_send_failed"):
                        phone_callback_obj.mark_send_failed(last_send_error)
                except Exception:
                    pass
                log_fn(
                    "  \u83b7\u53d6rt(\u534f\u8bae): add_phone session \u5df2\u5931\u6548\uff0c"
                    f"\u5c06\u4ece\u5934\u91cd\u65b0\u767b\u5f55 detail={last_send_error}"
                )
                raise _login_restart_required_error("add-phone/send", last_send_error)

            if _is_phone_verification_rate_limit_text(send_detail_text):
                last_send_error = send_detail_text[:240]
                try:
                    if hasattr(phone_callback_obj, "mark_send_failed"):
                        phone_callback_obj.mark_send_failed(last_send_error)
                except Exception:
                    pass
                if not proxy_from_pool:
                    log_fn(
                        "  获取rt(协议): add_phone 触发手机验证频率限制，当前未使用代理池IP，终止任务: 请使用代理池IP "
                        f"attempt={attempt} detail={last_send_error}"
                    )
                    raise _proxy_pool_required_error("add-phone/send", last_send_error)
                log_fn(
                    "  获取rt(协议): add_phone 触发手机验证频率限制，已释放当前手机号，"
                    "本次授权退出后更换代理IP并重新租用手机号 "
                    f"attempt={attempt} detail={last_send_error}"
                )
                raise _phone_verification_rate_limit_error("add-phone/send", last_send_error)

            if _is_retryable_phone_send_failure_text(send_detail_text):
                last_send_error = send_detail_text[:240]
                try:
                    if hasattr(phone_callback_obj, "mark_send_failed"):
                        phone_callback_obj.mark_send_failed(last_send_error)
                except Exception:
                    pass
                log_fn(
                    "  \u83b7\u53d6rt(\u534f\u8bae): add_phone \u5f53\u524d\u624b\u673a\u53f7\u4e0d\u53ef\u7528\uff0c"
                    f"\u51c6\u5907\u6362\u53f7 attempt={attempt} detail={last_send_error}"
                )
                if attempt >= 3 and attempt % 3 == 0:
                    log_fn(
                        f"  \u83b7\u53d6rt(\u534f\u8bae): add_phone \u8fde\u7eed {attempt} \u6b21\u9047\u5230\u4e0d\u53ef\u7528\u624b\u673a\u53f7\uff0c"
                        "\u5efa\u8bae\u66f4\u6362 country/service \u6216\u51b7\u5374\u540e\u91cd\u8bd5"
                    )
                continue

            if send_resp.status_code == 200:
                try:
                    if hasattr(phone_callback_obj, "mark_send_succeeded"):
                        phone_callback_obj.mark_send_succeeded()
                except Exception:
                    pass
                try:
                    code = phone_callback_obj()
                except Exception as exc:
                    last_send_error = str(exc)[:240]
                    try:
                        if hasattr(phone_callback_obj, "mark_code_failed"):
                            phone_callback_obj.mark_code_failed(last_send_error)
                    except Exception:
                        pass
                    try:
                        if hasattr(phone_callback_obj, "mark_send_failed"):
                            phone_callback_obj.mark_send_failed(last_send_error)
                    except Exception:
                        pass
                    log_fn(
                        f"  获取rt(协议): phone OTP 获取失败，已释放当前手机号；"
                        f"当前授权已进入验证码步骤，将从头重新登录后换号 attempt={attempt} detail={last_send_error}"
                    )
                    raise _login_restart_required_error("phone-otp/wait", last_send_error)
                if not code:
                    last_send_error = "empty phone otp"
                    try:
                        if hasattr(phone_callback_obj, "mark_code_failed"):
                            phone_callback_obj.mark_code_failed(last_send_error)
                    except Exception:
                        pass
                    try:
                        if hasattr(phone_callback_obj, "mark_send_failed"):
                            phone_callback_obj.mark_send_failed(last_send_error)
                    except Exception:
                        pass
                    log_fn(
                        "  获取rt(协议): phone OTP 为空，已释放当前手机号；"
                        f"当前授权已进入验证码步骤，将从头重新登录后换号 attempt={attempt}"
                    )
                    raise _login_restart_required_error("phone-otp/wait", last_send_error)
                validate_headers = _json_headers(engine, device_id=device_id, referer=f"{OPENAI_AUTH}/phone-verification")
                validate_resp = _post_json(
                    session,
                    f"{OPENAI_AUTH}/api/accounts/phone-otp/validate",
                    headers=validate_headers,
                    body={"code": code},
                    allow_redirects=True,
                    timeout=30,
                )
                log_fn(f"  获取rt(协议): phone OTP validate -> {validate_resp.status_code}")
                if validate_resp.status_code != 200:
                    _log_response_debug(log_fn, "phone OTP validate", validate_resp)
                    try:
                        if hasattr(phone_callback_obj, "mark_code_failed"):
                            phone_callback_obj.mark_code_failed(f"HTTP {validate_resp.status_code}")
                    except Exception:
                        pass
                    validate_detail = _continue_error_message(validate_resp)
                    last_send_error = f"HTTP {validate_resp.status_code}: {validate_detail}"[:240]
                    if _is_login_restart_required_text(validate_detail):
                        try:
                            if hasattr(phone_callback_obj, "mark_send_failed"):
                                phone_callback_obj.mark_send_failed(last_send_error)
                        except Exception:
                            pass
                        log_fn(
                            "  \u83b7\u53d6rt(\u534f\u8bae): phone OTP validate session \u5df2\u5931\u6548\uff0c"
                            f"\u5c06\u4ece\u5934\u91cd\u65b0\u767b\u5f55 detail={last_send_error}"
                        )
                        raise _login_restart_required_error("phone-otp/validate", last_send_error)
                    if int(getattr(validate_resp, "status_code", 0) or 0) == 429 or _is_phone_verification_rate_limit_text(validate_detail):
                        try:
                            if hasattr(phone_callback_obj, "mark_send_failed"):
                                phone_callback_obj.mark_send_failed(last_send_error)
                        except Exception:
                            pass
                        if not proxy_from_pool:
                            raise _proxy_pool_required_error("phone-otp/validate", last_send_error)
                        raise _phone_verification_rate_limit_error("phone-otp/validate", last_send_error)
                    try:
                        if hasattr(phone_callback_obj, "mark_send_failed"):
                            phone_callback_obj.mark_send_failed(last_send_error)
                    except Exception:
                        pass
                    log_fn(
                        "  获取rt(协议): phone OTP 校验失败，已释放当前手机号；"
                        f"当前授权无法返回 add_phone，将从头重新登录后换号 attempt={attempt} detail={last_send_error}"
                    )
                    raise _login_restart_required_error("phone-otp/validate", last_send_error)
                phone_validate_data = _response_json(validate_resp)
                phone_continue_url = _extract_continue_url_from_payload(phone_validate_data)
                phone_page = _extract_page_type(phone_validate_data)
                if phone_validate_data:
                    oauth_payload = phone_validate_data
                if phone_continue_url:
                    oauth_continue_url = phone_continue_url
                oauth_referer = f"{OPENAI_AUTH}/phone-verification"
                log_fn(
                    "  get_rt(protocol): phone OTP validate next="
                    f"{_short_url(phone_continue_url or '-')} page={phone_page or '-'}"
                )
                try:
                    if hasattr(phone_callback_obj, "report_success"):
                        phone_callback_obj.report_success()
                except Exception:
                    pass
                break

            last_send_error = send_detail_text[:240]
            try:
                if hasattr(phone_callback_obj, "mark_send_failed"):
                    phone_callback_obj.mark_send_failed(last_send_error)
            except Exception:
                pass
            log_fn(f"  获取rt(协议): add_phone 被拒，准备换号 attempt={attempt} detail={last_send_error}")
            if attempt >= 3 and attempt % 3 == 0:
                log_fn(
                    f"  获取rt(协议): add_phone 反欺诈连续 {attempt} 次被拒，"
                    "建议更换 country/service 或冷却几小时后重试"
                )
        else:
            raise RuntimeError(f"获取rt协议模式手机号提交失败，已达换号上限: {last_send_error or 'unknown'}")

    callback_url = ""
    if oauth_continue_url:
        callback_url = _resolve_callback_from_continue_url(
            session=session,
            engine=engine,
            device_id=device_id,
            continue_url=oauth_continue_url,
            auth_payload=oauth_payload,
            referer=oauth_referer,
            log_fn=log_fn,
        )

    if not callback_url:
        final_authorize = session.get(oauth_start.auth_url, timeout=30, allow_redirects=True)
        callback_location = _extract_continue_url(final_authorize)
        if callback_location:
            callback_url = _normalize_auth_url(callback_location, base_url=oauth_start.auth_url)
        maybe_url = str(getattr(final_authorize, "url", "") or "")
        if not callback_url and _extract_oauth_callback_params_from_url(maybe_url):
            callback_url = maybe_url
    if not callback_url:
        callback_url = engine._follow_platform_redirects_for_callback(session, oauth_start.auth_url)
    if not callback_url:
        # dump 关键上下文，避免“未获取到 OAuth callback”走到这里时无从排查。
        try:
            ctx_continue = str(oauth_continue_url or "")[:240]
            payload_keys = sorted(list(oauth_payload.keys())) if isinstance(oauth_payload, dict) else []
            final_url = ""
            try:
                final_url = str(getattr(final_authorize, "url", "") or "")[:240]
            except Exception:
                final_url = ""
            log_fn(
                "  获取rt(协议): callback 缺失上下文 "
                f"continue={ctx_continue or '-'} payload_keys={payload_keys} "
                f"final_authorize_url={final_url or '-'}"
            )
        except Exception:
            pass
        raise RuntimeError("获取rt协议模式未获取到 OAuth callback")

    token_json = submit_callback_url(
        callback_url=callback_url,
        expected_state=oauth_start.state,
        code_verifier=oauth_start.code_verifier,
        redirect_uri=CODEX_REDIRECT_URI,
        client_id=CODEX_CLIENT_ID,
        proxy_url=proxy,
    )
    token_info = json.loads(token_json)
    if not token_info.get("access_token"):
        raise RuntimeError("获取rt协议模式 token 交换失败: access_token 为空")
    return token_info
