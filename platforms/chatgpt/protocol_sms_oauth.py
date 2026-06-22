from __future__ import annotations

import json
import time
import uuid
from typing import Callable
from urllib.parse import urlencode, urljoin

from .constants import CHATGPT_APP, OPENAI_API_ENDPOINTS, OPENAI_AUTH
from .http_client import OpenAIHTTPClient
from .protocol_get_rt import _extract_continue_url_from_payload, _extract_page_type, _response_json
from .register import (
    PLATFORM_REFERENCE_USER_AGENT,
    RegistrationEngine,
    RegistrationResult,
    _cookies_to_header,
    _decode_jwt_payload_no_verify,
    _extract_chatgpt_account_id,
    generate_random_user_info,
)


class _StaticEmailService:
    def __init__(self, email: str):
        self.service_type = type("ST", (), {"value": "sms_oauth_protocol"})()
        self._email = email

    def create_email(self, config=None):
        return {"email": self._email, "service_id": self._email, "token": self._email}

    def get_verification_code(self, *args, **kwargs):
        return ""

    def update_status(self, success, error=None):
        return None

    @property
    def status(self):
        return None


def _post_json(session, url: str, *, headers: dict, body: dict, allow_redirects: bool = True, timeout: int = 30):
    return session.post(
        url,
        headers=headers,
        data=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        allow_redirects=allow_redirects,
        timeout=timeout,
    )


def _safe_response_text(resp, *, limit: int = 500) -> str:
    try:
        data = resp.json()
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        text = str(getattr(resp, "text", "") or "")
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    return text[:limit]


def _cookie_value(session, name: str) -> str:
    cookies = getattr(session, "cookies", None)
    if cookies is None:
        return ""
    getter = getattr(cookies, "get", None)
    if callable(getter):
        for kwargs in (
            {},
            {"domain": "chatgpt.com"},
            {"domain": ".chatgpt.com"},
            {"domain": "auth.openai.com"},
            {"domain": ".auth.openai.com"},
        ):
            try:
                value = getter(name, **kwargs)
            except TypeError:
                try:
                    value = getter(name)
                except Exception:
                    value = ""
            except Exception:
                value = ""
            if value:
                return str(value)
    try:
        for cookie in cookies:
            if str(getattr(cookie, "name", "") or "") == name:
                return str(getattr(cookie, "value", "") or "")
    except Exception:
        pass
    return ""


def _csrf_token_from_session(session) -> str:
    raw = _cookie_value(session, "__Host-next-auth.csrf-token")
    if "%7C" in raw:
        return raw.split("%7C", 1)[0]
    if "|" in raw:
        return raw.split("|", 1)[0]
    return raw


def _start_chatgpt_nextauth_oauth(session, engine: RegistrationEngine, *, email: str, device_id: str) -> str:
    engine._set_oai_did_for_session(session, device_id)
    session.get(
        f"{CHATGPT_APP}/",
        headers=engine._platform_nav_headers(referer=f"{CHATGPT_APP}/"),
        allow_redirects=True,
        timeout=30,
    )
    csrf_resp = session.get(
        f"{CHATGPT_APP}/api/auth/csrf",
        headers={
            "accept": "application/json",
            "referer": f"{CHATGPT_APP}/",
        },
        allow_redirects=True,
        timeout=30,
    )
    csrf_token = ""
    try:
        csrf_token = str((csrf_resp.json() or {}).get("csrfToken") or "").strip()
    except Exception:
        csrf_token = ""
    csrf_token = csrf_token or _csrf_token_from_session(session)
    if not csrf_token:
        raise RuntimeError("sms_oauth protocol NextAuth csrf token missing")

    query = urlencode(
        {
            "prompt": "login",
            "ext-oai-did": device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "screen_hint": "login_or_signup",
            "login_hint": email or "",
        }
    )
    body = urlencode(
        {
            "callbackUrl": f"{CHATGPT_APP}/",
            "csrfToken": csrf_token,
            "json": "true",
        }
    )
    signin_resp = session.post(
        f"{CHATGPT_APP}/api/auth/signin/openai?{query}",
        headers={
            "accept": "application/json",
            "content-type": "application/x-www-form-urlencoded",
            "origin": CHATGPT_APP,
            "referer": f"{CHATGPT_APP}/",
        },
        data=body,
        allow_redirects=True,
        timeout=30,
    )
    if int(getattr(signin_resp, "status_code", 0) or 0) != 200:
        raise RuntimeError(f"sms_oauth protocol signin/openai failed: {_safe_response_text(signin_resp)}")
    try:
        signin_data = signin_resp.json() or {}
    except Exception:
        signin_data = {}
    auth_url = str(signin_data.get("url") or "").strip()
    if not auth_url:
        raise RuntimeError("sms_oauth protocol signin/openai did not return authorize URL")
    return auth_url


def _chatgpt_session_from_nextauth_callback(session, engine: RegistrationEngine, continue_url: str) -> dict:
    callback_url = str(continue_url or "").strip()
    if not callback_url:
        raise RuntimeError("sms_oauth protocol create_account did not return continue_url")
    if callback_url.startswith("/"):
        callback_url = urljoin(OPENAI_AUTH, callback_url)
    if "chatgpt.com/api/auth/callback/openai" not in callback_url.lower():
        raise RuntimeError(f"sms_oauth protocol expected ChatGPT NextAuth callback, got: {callback_url[:180]}")

    cb_resp = session.get(
        callback_url,
        headers=engine._platform_nav_headers(referer=f"{OPENAI_AUTH}/about-you"),
        allow_redirects=True,
        timeout=30,
    )
    status = int(getattr(cb_resp, "status_code", 0) or 0)
    if status >= 400:
        raise RuntimeError(f"sms_oauth protocol ChatGPT callback failed: HTTP {status} {_safe_response_text(cb_resp)}")

    session_resp = session.get(
        f"{CHATGPT_APP}/api/auth/session",
        headers={
            "accept": "application/json",
            "referer": f"{CHATGPT_APP}/",
        },
        allow_redirects=True,
        timeout=30,
    )
    status = int(getattr(session_resp, "status_code", 0) or 0)
    if status != 200:
        raise RuntimeError(f"sms_oauth protocol session API failed: HTTP {status} {_safe_response_text(session_resp)}")
    try:
        session_data = session_resp.json() or {}
    except Exception as exc:
        raise RuntimeError(f"sms_oauth protocol session API JSON parse failed: {exc}") from exc
    if not isinstance(session_data, dict):
        raise RuntimeError("sms_oauth protocol session API did not return object")

    access_token = str(session_data.get("accessToken") or session_data.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("sms_oauth protocol session API did not return accessToken")
    session_token = _cookie_value(session, "__Secure-next-auth.session-token")
    account_cookie = _cookie_value(session, "_account")
    account_id = _extract_chatgpt_account_id(access_token) or account_cookie
    return {
        "access_token": access_token,
        "refresh_token": str(session_data.get("refreshToken") or session_data.get("refresh_token") or "").strip(),
        "id_token": str(session_data.get("idToken") or session_data.get("id_token") or "").strip(),
        "session_token": session_token,
        "account_id": account_id,
        "workspace_id": str(session_data.get("workspaceId") or session_data.get("workspace_id") or "").strip(),
        "profile": session_data.get("user") if isinstance(session_data.get("user"), dict) else {},
        "expires_at": str(session_data.get("expires") or "").strip(),
        "cookies": _cookies_to_header(getattr(session, "cookies", None)),
        "session": session_data,
    }


def _is_phone_send_retryable(text: str) -> bool:
    lower = str(text or "").lower()
    return any(
        marker in lower
        for marker in (
            "virtual phone number",
            "voip",
            "invalid phone",
            "couldn't send a text message",
            "switched to whatsapp",
            "suspicious behavior",
            "fraud",
        )
    )


def _is_existing_phone_account_response(resp, text: str = "") -> bool:
    status = int(getattr(resp, "status_code", 0) or 0)
    value = f"{str(text or _safe_response_text(resp)).lower()} {str(getattr(resp, 'url', '') or '').lower()}"
    if status in (301, 302, 303, 307, 308):
        try:
            location = str((getattr(resp, "headers", {}) or {}).get("location") or "")
        except Exception:
            location = ""
        value = f"{value} {location.lower()}"
    return any(
        marker in value
        for marker in (
            "login_password",
            "log-in/password",
            "invalid_username_or_password",
            "incorrect phone number or password",
            "existing account",
        )
    )


def _is_create_account_password_response(resp) -> bool:
    url = str(getattr(resp, "url", "") or "").lower()
    if "create-account/password" in url:
        return True
    try:
        data = resp.json() or {}
    except Exception:
        data = {}
    return _extract_page_type(data) == "create_account_password"


class ChatGPTProtocolSmsOAuthWorker:
    def __init__(
        self,
        *,
        phone_callback: Callable[[], str] | None,
        proxy_url: str | None = None,
        log_fn: Callable[[str], None] = print,
        phone_change_limit: int = 10,
    ):
        if phone_callback is None:
            raise ValueError("sms_oauth protocol requires phone_callback")
        self.phone_callback = phone_callback
        self.proxy_url = proxy_url
        self.log_fn = log_fn
        self.phone_change_limit = max(int(phone_change_limit or 1), 1)

    def _log(self, message: str) -> None:
        try:
            self.log_fn(message)
        except Exception:
            pass

    def _next_phone(self) -> str:
        phone = str(self.phone_callback() or "").strip()
        if not phone:
            raise RuntimeError("sms_oauth protocol did not receive phone number")
        return phone

    def _next_sms_code(self) -> str:
        code = str(self.phone_callback() or "").strip()
        if not code:
            raise RuntimeError("sms_oauth protocol did not receive phone OTP")
        return code

    def _start_authorize_session(self, client, engine: RegistrationEngine, *, device_id: str):
        # \u590d\u7528\u90ae\u7bb1\u534f\u8bae\u5df2\u9a8c\u8bc1\u7684 platform authorize \u94fe\u8def\uff08\u80fd\u7ed5\u8fc7 403\uff09\uff0c
        # \u4e0d\u8d70\u4f1a\u88ab\u98ce\u63a7\u7684 NextAuth signin/openai \u8def\u5f84\u3002
        engine._set_oai_did_for_session(client.session, device_id)
        oauth_start = engine._platform_reference_authorize(client, device_id)
        self._log("sms_oauth(protocol): platform authorize \u5df2\u5efa\u7acb\u4f1a\u8bdd")
        return oauth_start

    def run(self, *, email: str, password: str) -> RegistrationResult:
        result = RegistrationResult(success=False, email=email, password=password)
        client = OpenAIHTTPClient(proxy_url=self.proxy_url)
        client.default_headers["User-Agent"] = PLATFORM_REFERENCE_USER_AGENT
        session = client.session
        engine = RegistrationEngine(
            email_service=_StaticEmailService(email),
            proxy_url=self.proxy_url,
            callback_logger=self.log_fn,
        )
        engine.http_client = client
        engine.session = session
        engine.email = email
        engine.password = password

        device_id = str(uuid.uuid4())
        try:
            last_error = ""
            phone_validate_data = {}
            # \u4f1a\u8bdd\u53ea\u9700\u5efa\u7acb\u4e00\u6b21\uff08\u6210\u529f HAR \u91cc authorize \u53ea\u5bfc\u822a\u4e00\u6b21\uff0c
            # \u540e\u7eed\u6362\u53f7\u53ea\u662f\u91cd\u590d POST authorize/continue\uff09\u3002
            self._log("sms_oauth(protocol): initializing ChatGPT platform authorize session")
            oauth_start = self._start_authorize_session(client, engine, device_id=device_id)
            for attempt in range(1, self.phone_change_limit + 1):
                phone = self._next_phone()
                self._log(f"sms_oauth(protocol): phone attempt {attempt}/{self.phone_change_limit} phone={phone}")
                continue_headers = engine._platform_json_headers(device_id=device_id, referer=f"{OPENAI_AUTH}/log-in-or-create-account?usernameKind=phone_number")
                continue_headers["openai-sentinel-token"] = engine._build_sentinel_header_for_client(
                    client,
                    device_id,
                    "authorize_continue",
                )
                continue_resp = _post_json(
                    session,
                    OPENAI_API_ENDPOINTS["signup"],
                    headers=continue_headers,
                    body={"username": {"value": phone, "kind": "phone_number"}, "screen_hint": "login_or_signup"},
                    allow_redirects=False,
                    timeout=30,
                )
                continue_text = _safe_response_text(continue_resp)
                self._log(f"sms_oauth(protocol): authorize/continue -> {getattr(continue_resp, 'status_code', 0)}")
                if _is_existing_phone_account_response(continue_resp, continue_text):
                    last_error = continue_text or "phone resolved to existing account"
                    if hasattr(self.phone_callback, "mark_send_failed"):
                        self.phone_callback.mark_send_failed(last_error)
                    continue
                if continue_resp.status_code != 200:
                    last_error = continue_text
                    if hasattr(self.phone_callback, "mark_send_failed"):
                        self.phone_callback.mark_send_failed(last_error)
                    continue

                register_headers = engine._platform_json_headers(device_id=device_id, referer=f"{OPENAI_AUTH}/create-account/password")
                register_headers["openai-sentinel-token"] = engine._build_sentinel_header_for_client(
                    client,
                    device_id,
                    "username_password_create",
                )
                register_resp = _post_json(
                    session,
                    OPENAI_API_ENDPOINTS["register"],
                    headers=register_headers,
                    body={"username": phone, "password": password},
                    allow_redirects=False,
                    timeout=30,
                )
                register_text = _safe_response_text(register_resp)
                self._log(f"sms_oauth(protocol): user/register -> {getattr(register_resp, 'status_code', 0)}")
                if _is_existing_phone_account_response(register_resp, register_text):
                    last_error = register_text or "phone resolved to existing account"
                    if hasattr(self.phone_callback, "mark_send_failed"):
                        self.phone_callback.mark_send_failed(last_error)
                    continue
                if register_resp.status_code != 200:
                    last_error = register_text
                    if hasattr(self.phone_callback, "mark_send_failed"):
                        self.phone_callback.mark_send_failed(last_error)
                    continue

                send_resp = session.get(
                    f"{OPENAI_AUTH}/api/accounts/phone-otp/send",
                    headers=engine._platform_nav_headers(referer=f"{OPENAI_AUTH}/create-account/password"),
                    allow_redirects=False,
                    timeout=30,
                )
                send_text = _safe_response_text(send_resp)
                send_location = ""
                try:
                    send_location = str((getattr(send_resp, "headers", {}) or {}).get("location") or "")
                except Exception:
                    send_location = ""
                location_suffix = f" location={send_location[:180]}" if send_location else ""
                self._log(f"sms_oauth(protocol): phone-otp/send -> {getattr(send_resp, 'status_code', 0)}{location_suffix}")
                if int(getattr(send_resp, "status_code", 0) or 0) not in (200, 302):
                    last_error = send_text
                    if hasattr(self.phone_callback, "mark_send_failed"):
                        self.phone_callback.mark_send_failed(last_error)
                    continue
                if send_text and _is_phone_send_retryable(send_text):
                    last_error = send_text
                    if hasattr(self.phone_callback, "mark_send_failed"):
                        self.phone_callback.mark_send_failed(last_error)
                    continue
                if hasattr(self.phone_callback, "mark_send_succeeded"):
                    self.phone_callback.mark_send_succeeded()

                # 成功 HAR entry 478: phone-otp/send 302 -> contact-verification
                # 浏览器自动跟随 redirect 到 contact-verification 页面，协议模式需手动 GET 该页面以匹配流程。
                contact_verification_url = send_location or f"{OPENAI_AUTH}/contact-verification"
                try:
                    session.get(
                        contact_verification_url,
                        headers=engine._platform_nav_headers(referer=f"{OPENAI_AUTH}/create-account/password"),
                        allow_redirects=True,
                        timeout=30,
                    )
                    self._log("sms_oauth(protocol): contact-verification 页面已加载")
                except Exception as cv_exc:
                    self._log(f"sms_oauth(protocol): contact-verification 加载失败(非致命): {cv_exc}")

                code = self._next_sms_code()
                validate_referer = f"{OPENAI_AUTH}/contact-verification"
                validate_headers = engine._platform_json_headers(device_id=device_id, referer=validate_referer)
                validate_resp = _post_json(
                    session,
                    f"{OPENAI_AUTH}/api/accounts/phone-otp/validate",
                    headers=validate_headers,
                    body={"code": code},
                    allow_redirects=True,
                    timeout=30,
                )
                self._log(f"sms_oauth(protocol): phone-otp/validate -> {getattr(validate_resp, 'status_code', 0)}")
                if validate_resp.status_code != 200:
                    last_error = _safe_response_text(validate_resp)
                    if hasattr(self.phone_callback, "mark_code_failed"):
                        self.phone_callback.mark_code_failed(last_error)
                    if hasattr(self.phone_callback, "mark_send_failed"):
                        self.phone_callback.mark_send_failed(last_error)
                    continue
                phone_validate_data = _response_json(validate_resp)
                if hasattr(self.phone_callback, "report_success"):
                    self.phone_callback.report_success()
                break
            else:
                raise RuntimeError(f"sms_oauth protocol phone attempts exhausted: {last_error or 'unknown'}")

            page_type = _extract_page_type(phone_validate_data)
            if page_type and page_type != "about_you":
                self._log(f"sms_oauth(protocol): phone validate next page={page_type}")

            user_info = generate_random_user_info()
            create_headers = engine._platform_json_headers(device_id=device_id, referer=f"{OPENAI_AUTH}/about-you")
            create_headers["openai-sentinel-token"] = engine._build_sentinel_header_for_client(
                client,
                device_id,
                "oauth_create_account",
            )
            create_resp = _post_json(
                session,
                OPENAI_API_ENDPOINTS["create_account"],
                headers=create_headers,
                body=user_info,
                allow_redirects=False,
                timeout=30,
            )
            self._log(f"sms_oauth(protocol): create_account -> {getattr(create_resp, 'status_code', 0)}")
            if create_resp.status_code not in (200, 302):
                raise RuntimeError(f"sms_oauth protocol create_account failed: {_safe_response_text(create_resp)}")

            create_data = _response_json(create_resp)
            continue_url = _extract_continue_url_from_payload(create_data)
            if not continue_url:
                continue_url = engine._create_account_continue_url or ""
            session_info = _chatgpt_session_from_nextauth_callback(session, engine, continue_url)
            self._log(
                "sms_oauth(protocol): ChatGPT session acquired "
                f"accessToken=yes session_token={'yes' if session_info.get('session_token') else 'no'}"
            )

            platform_token_info = None
            try:
                # \u590d\u7528\u9876\u90e8 authorize \u5efa\u7acb\u4f1a\u8bdd\u65f6\u7684 oauth_start\uff08state/PKCE \u5fc5\u987b\u4e00\u81f4\uff09\u3002
                token_continue_url = engine._create_account_continue_url or f"{OPENAI_AUTH}/sign-in-with-chatgpt/codex/consent"
                platform_token_info = engine._complete_platform_oauth(
                    client,
                    device_id,
                    oauth_start,
                    token_continue_url,
                )
                if platform_token_info and platform_token_info.get("refresh_token"):
                    self._log("sms_oauth(protocol): Platform OAuth refresh_token acquired")
            except Exception as exc:
                self._log(f"sms_oauth(protocol): Platform OAuth refresh_token unavailable: {exc}")

            access_token = str((platform_token_info or {}).get("access_token") or session_info.get("access_token") or "")
            refresh_token = str((platform_token_info or {}).get("refresh_token") or "")
            id_token = str((platform_token_info or {}).get("id_token") or session_info.get("id_token") or "")
            payload = _decode_jwt_payload_no_verify(id_token) or _decode_jwt_payload_no_verify(access_token)
            account_id = (
                str((platform_token_info or {}).get("account_id") or "").strip()
                or str(session_info.get("account_id") or "").strip()
                or str(payload.get("sub") or "").strip()
            )

            result.success = True
            result.account_id = account_id
            result.access_token = access_token
            result.refresh_token = refresh_token
            result.id_token = id_token
            result.session_token = str(session_info.get("session_token") or "")
            result.workspace_id = str(session_info.get("workspace_id") or "")
            result.source = "sms_oauth_protocol"
            result.metadata = {
                "auth_source": "sms_oauth_protocol",
                "registration_refresh_token": refresh_token,
                "registration_refresh_token_usable": False,
                "refresh_token_source": "phone_first_oauth" if refresh_token else "",
                "cookies": session_info.get("cookies", ""),
                "profile": session_info.get("profile", {}),
                "expires_at": session_info.get("expires_at", ""),
                "session": session_info.get("session", {}),
                "oauth_error": "" if refresh_token else "sms_oauth protocol registered account but did not acquire refresh_token",
                "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            return result
        finally:
            try:
                session.close()
            except Exception:
                pass
