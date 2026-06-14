from __future__ import annotations

import json
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
        return self._otp_callback()

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


def _extract_continue_url(resp) -> str:
    location = str(getattr(resp, "headers", {}).get("Location") or "").strip()
    if location:
        return location
    try:
        data = resp.json() or {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return ""
    return str(data.get("continue_url") or data.get("redirect_url") or "").strip()


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
    engine._set_oai_did_for_session(session, device_id)

    oauth_start = generate_oauth_url(
        redirect_uri=CODEX_REDIRECT_URI,
        scope=CODEX_SCOPE,
        client_id=CODEX_CLIENT_ID,
    )
    log_fn(f"  获取rt(协议): OAuth 授权链接已生成 state={oauth_start.state[:18]}...")

    auth_resp = session.get(oauth_start.auth_url, timeout=30, allow_redirects=True)
    log_fn(f"  获取rt(协议): authorize -> {getattr(auth_resp, 'status_code', 0)}")
    _log_response_debug(log_fn, "authorize", auth_resp)

    try:
        sentinel_header = engine._build_sentinel_header_for_client(client, device_id, "authorize_continue")
        log_fn("  获取rt(协议): Sentinel 已就绪")
    except Exception as exc:
        raise RuntimeError(f"获取rt协议模式 Sentinel 初始化失败: {exc}") from exc

    continue_body = {"username": {"kind": "email", "value": email}}
    continue_headers = _json_headers(engine, device_id=device_id, referer=f"{OPENAI_AUTH}/log-in")
    continue_headers["openai-sentinel-token"] = sentinel_header
    log_fn(
        "  获取rt(协议): authorize/continue request: "
        f"endpoint={OPENAI_API_ENDPOINTS['signup']} referer={continue_headers.get('referer') or ''} "
        f"body_keys={','.join(continue_body.keys())} sentinel=yes"
    )
    continue_resp = _post_json(
        session,
        OPENAI_API_ENDPOINTS["signup"],
        headers=continue_headers,
        body=continue_body,
        allow_redirects=False,
        timeout=30,
    )
    _log_response_debug(log_fn, "authorize/continue", continue_resp)
    if continue_resp.status_code == 409 and engine._is_invalid_state_response(continue_resp):
        log_fn("  获取rt(协议): authorize/continue invalid_state，重建 authorize + sentinel 后重试")
        auth_resp = session.get(oauth_start.auth_url, timeout=30, allow_redirects=True)
        _log_response_debug(log_fn, "authorize retry", auth_resp)
        sentinel_header = engine._build_sentinel_header_for_client(client, device_id, "authorize_continue")
        continue_headers = _json_headers(engine, device_id=device_id, referer=f"{OPENAI_AUTH}/log-in")
        continue_headers["openai-sentinel-token"] = sentinel_header
        continue_resp = _post_json(
            session,
            OPENAI_API_ENDPOINTS["signup"],
            headers=continue_headers,
            body=continue_body,
            allow_redirects=False,
            timeout=30,
        )
        _log_response_debug(log_fn, "authorize/continue retry", continue_resp)
    if continue_resp.status_code != 200:
        detail = _continue_error_message(continue_resp)
        raise RuntimeError(
            f"获取rt协议模式 authorize/continue 失败: HTTP {continue_resp.status_code}"
            f"{(': ' + detail) if detail else ''}"
        )
    continue_data = continue_resp.json() or {}
    page_type = str(((continue_data.get("page") or {}).get("type")) or "")
    log_fn(f"  获取rt(协议): authorize/continue -> page={page_type or '(unknown)'}")

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
                send_ok = engine._send_platform_login_otp(client)
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
            if _is_retryable_email_otp_status(last_status) and validate_attempt <= max_invalid_retries:
                continue
            raise RuntimeError(f"获取rt协议模式邮箱验证码校验失败: HTTP {last_status}")
        if otp_data is None:
            raise RuntimeError(f"获取rt协议模式邮箱验证码校验失败: HTTP {last_status}")
    else:
        otp_data = continue_data

    next_page = str(((otp_data.get("page") or {}).get("type")) or page_type or "")
    phone_callback_obj = phone_callback
    if next_page == "add_phone":
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

        last_send_error = ""
        for attempt in range(1, max(int(phone_change_limit or 1), 1) + 1):
            phone_number = phone_callback_obj()
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
            if send_resp.status_code == 200:
                try:
                    if hasattr(phone_callback_obj, "mark_send_succeeded"):
                        phone_callback_obj.mark_send_succeeded()
                except Exception:
                    pass
                code = phone_callback_obj()
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
                    try:
                        if hasattr(phone_callback_obj, "mark_code_failed"):
                            phone_callback_obj.mark_code_failed(f"HTTP {validate_resp.status_code}")
                    except Exception:
                        pass
                    raise RuntimeError(f"获取rt协议模式手机验证码校验失败: HTTP {validate_resp.status_code}")
                try:
                    if hasattr(phone_callback_obj, "report_success"):
                        phone_callback_obj.report_success()
                except Exception:
                    pass
                break

            try:
                send_data = send_resp.json() or {}
            except Exception:
                send_data = {"raw": getattr(send_resp, "text", "")}
            last_send_error = json.dumps(send_data, ensure_ascii=False)[:240]
            try:
                if hasattr(phone_callback_obj, "mark_send_failed"):
                    phone_callback_obj.mark_send_failed(last_send_error)
            except Exception:
                pass
            log_fn(f"  获取rt(协议): add_phone 被拒，准备换号 attempt={attempt} detail={last_send_error}")
        else:
            raise RuntimeError(f"获取rt协议模式手机号提交失败，已达换号上限: {last_send_error or 'unknown'}")

    final_authorize = session.get(oauth_start.auth_url, timeout=30, allow_redirects=True)
    callback_url = ""
    callback_location = _extract_continue_url(final_authorize)
    if callback_location:
        callback_url = callback_location
    maybe_url = str(getattr(final_authorize, "url", "") or "")
    if not callback_url and _extract_oauth_callback_params_from_url(maybe_url):
        callback_url = maybe_url
    if callback_url and callback_url.startswith("/"):
        from urllib.parse import urljoin
        callback_url = urljoin(OPENAI_AUTH, callback_url)
    if not callback_url:
        callback_url = engine._follow_platform_redirects_for_callback(session, oauth_start.auth_url)
    if not callback_url:
        auth_cookie = engine._complete_platform_oauth(client, device_id, oauth_start, "")
        if auth_cookie:
            return auth_cookie
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
