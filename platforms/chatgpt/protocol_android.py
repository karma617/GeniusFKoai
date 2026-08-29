"""ChatGPT Android app protocol registration worker."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from platforms.chatgpt.constants import generate_random_user_info
from platforms.chatgpt.register import RegistrationResult, _extract_chatgpt_account_id


BASE_URL = "https://auth.openai.com"
AUTHORIZE_URL = f"{BASE_URL}/api/accounts/authorize"
FIRST_PARTY_AUTHORIZE_URL = f"{BASE_URL}/api/first_party_authorize/next"
TOKEN_URL = f"{BASE_URL}/oauth/token"
CLIENT_ID = "app_xwBKzt04752TTSfXnki17hmB"
REDIRECT_URI = "com.openai.chatgpt://auth.openai.com/android/com.openai.chatgpt/callback"
SCOPES = (
    "openid email profile offline_access model.request model.read "
    "organization.read organization.write"
)
APP_VERSION = "1.2026.237"
APP_BUILD = "2623711"
APP_HASH = "zFKflHMWTnT"
APP_USER_AGENT = "ChatGPT/1.2026.237 (Android 10; MIX 2S; build 2623711)"
WEBVIEW_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; MIX 2S) "
    "AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36"
)


def _pkce_pair() -> tuple[str, str]:
    alphabet = string.ascii_letters + string.digits + "-._~"
    verifier = "".join(secrets.choice(alphabet) for _ in range(64))
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _safe_json(response: requests.Response, stage: str) -> dict[str, Any]:
    raw_body = str(getattr(response, "text", "") or "").replace("\r", " ").replace("\n", " ").strip()
    try:
        data = response.json()
    except ValueError as exc:
        body = raw_body[:500] or "<empty>"
        raise RuntimeError(
            f"ANDROID协议 {stage} 返回非 JSON: status={response.status_code} body={body}"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"ANDROID协议 {stage} 返回格式错误: status={response.status_code}")
    if response.status_code < 200 or response.status_code >= 300:
        detail = json.dumps(data, ensure_ascii=False)[:500] if data else (raw_body[:500] or "<empty>")
        response_headers = getattr(response, "headers", {}) or {}
        trace_headers = {
            key: str(response_headers.get(key) or "")
            for key in ("x-request-id", "cf-ray", "content-type")
            if response_headers.get(key)
        }
        trace = f" headers={json.dumps(trace_headers, ensure_ascii=False)}" if trace_headers else ""
        raise RuntimeError(f"ANDROID协议 {stage} HTTP {response.status_code}: {detail}{trace}")
    return data


def _token_expiry(tokens: dict[str, Any]) -> tuple[str, int]:
    try:
        expires_in = int(tokens.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in <= 0:
        return "", 0
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    return expires_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"), expires_in


def refresh_android_oauth_tokens(
    refresh_token: str,
    *,
    proxy_url: str | None = None,
    timeout: int = 30,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """按 Android App 协议刷新 OAuth token（逆向自 ChatGPT Android 1.2026.237）。"""
    token = str(refresh_token or "").strip()
    if not token:
        raise ValueError("ANDROID协议刷新缺少 refresh_token")
    if session is None:
        session = requests.Session()
        session.trust_env = False
        if proxy_url:
            session.proxies.update({"http": proxy_url, "https": proxy_url})
    response = session.post(
        TOKEN_URL,
        json={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": token,
            "scope": SCOPES,
        },
        headers={
            "User-Agent": APP_USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    tokens = _safe_json(response, "刷新 OAuth token")
    access_token = str(tokens.get("access_token") or "")
    if not access_token:
        raise RuntimeError(
            f"ANDROID协议刷新 OAuth token 缺少 access_token: "
            f"{json.dumps(tokens, ensure_ascii=False)[:500]}"
        )
    result = dict(tokens)
    result["refresh_token"] = str(tokens.get("refresh_token") or "") or token
    computed_expires_at, expires_in = _token_expiry(tokens)
    result["expires_at"] = str(tokens.get("expires_at") or "") or computed_expires_at
    result["expires_in"] = expires_in
    return result


class ChatGPTAndroidProtocolWorker:
    """Run the Android app's first-party authorization registration flow."""

    def __init__(
        self,
        *,
        mailbox,
        mailbox_account,
        proxy_url: str | None = None,
        log_fn: Callable[[str], None] = print,
    ):
        if not mailbox or not mailbox_account:
            raise ValueError("ANDROID协议依赖当前项目 mailbox provider，当前未获取到邮箱账号")
        self.mailbox = mailbox
        self.mailbox_account = mailbox_account
        self.proxy_url = proxy_url
        self.log_fn = log_fn
        self.email = str(getattr(mailbox_account, "email", "") or "").strip()
        self.device_id = str(uuid.uuid4())
        self.state = secrets.token_hex(16)
        self.nonce = secrets.token_hex(16)
        self.auth_session_logging_id = str(uuid.uuid4())
        self.verifier, self.challenge = _pkce_pair()
        self.session = requests.Session()
        self.session.trust_env = False
        if proxy_url:
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})

    def _log(self, message: str) -> None:
        try:
            self.log_fn(message)
        except Exception:
            pass

    def _web_headers(self) -> dict[str, str]:
        return {
            "User-Agent": WEBVIEW_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US",
            "OAI-Device-Id": self.device_id,
            "OAI-Client-Type": "android",
            "OAI-Package-Name": "com.openai.chatgpt",
        }

    def _api_headers(self, *, include_target_path: bool = True) -> dict[str, str]:
        headers = {
            "User-Agent": APP_USER_AGENT,
            "OAI-Package-Name": "com.openai.chatgpt",
            "OAI-Client-Type": "android",
            "OAI-Device-Id": self.device_id,
            "Accept-Language": "en-US",
            "X-Device-Tier": "high",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if include_target_path:
            headers.update(
                {
                    "X-OpenAI-Target-Path": "/api/first_party_authorize/next",
                    "OAI-Android-Play-Integrity-Token-Failed": "service -2",
                }
            )
        return headers

    def _authorize_url(self) -> str:
        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
            "state": self.state,
            "nonce": self.nonce,
            "audience": "https://api.openai.com/v1",
            "issuer": "https://auth.openai.com",
            "ccaps": "default_otp_v2+login_methods",
            "android_device_id": self.device_id,
            "requester_metadata_app_version": APP_VERSION,
            "auth_session_logging_id": self.auth_session_logging_id,
            "login_hint": self.email,
            "screen_hint": "signup",
            "hydra_flow": "condense",
            "app_hash": APP_HASH,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def _post_first_party(self, page_type: str, data: dict[str, Any], stage: str) -> dict[str, Any]:
        response = self.session.post(
            FIRST_PARTY_AUTHORIZE_URL,
            json={"origin_page_type": page_type, "data": data},
            headers=self._api_headers(),
            timeout=30,
        )
        if response.status_code >= 400:
            diagnostic_data = dict(data)
            if "code" in diagnostic_data:
                diagnostic_data["code"] = "<redacted>"
            raw_body = str(getattr(response, "text", "") or "").replace("\r", " ").replace("\n", " ").strip()
            self._log(
                f"ANDROID协议 {stage} 请求诊断: page_type={page_type} "
                f"request={json.dumps(diagnostic_data, ensure_ascii=False)} "
                f"response_body={(raw_body[:500] or '<empty>')}"
            )
        payload = _safe_json(response, stage)
        self._log(
            f"ANDROID协议 {stage}: status={response.status_code} "
            f"type={payload.get('type', '')}"
        )
        return payload

    @staticmethod
    def _extract_auth_code(payload: dict[str, Any]) -> str:
        nested = payload.get("payload")
        if isinstance(nested, dict) and nested.get("code"):
            return str(nested["code"])
        continue_url = str(payload.get("continue_url") or "")
        if continue_url:
            return str(parse_qs(urlparse(continue_url).query).get("code", [""])[0] or "")
        return ""

    def _mark_existing_email(self) -> list[str]:
        marker = getattr(self.mailbox, "mark_registration_success", None)
        if not callable(marker):
            return []
        try:
            return list(marker(self.mailbox_account) or [])
        except Exception as exc:
            self._log(f"ANDROID协议 已注册邮箱打标失败（忽略）: {exc}")
            return []

    def run(self, *, email: str, password: str) -> RegistrationResult:
        requested_email = str(email or self.email).strip()
        if requested_email.lower() != self.email.lower():
            raise ValueError("ANDROID协议只能使用当前 mailbox provider 分配的邮箱")

        try:
            before_ids = set(self.mailbox.get_current_ids(self.mailbox_account) or set())
        except Exception as exc:
            before_ids = set()
            self._log(f"ANDROID协议 邮箱基线读取失败，继续等待新邮件: {exc}")

        self._log(f"ANDROID协议 authorize: email={requested_email}")
        authorize_response = self.session.get(
            self._authorize_url(),
            headers=self._web_headers(),
            allow_redirects=True,
            timeout=30,
        )
        self._log(
            f"ANDROID协议 authorize: status={authorize_response.status_code} "
            f"url={str(authorize_response.url or '')[:160]}"
        )
        if authorize_response.status_code < 200 or authorize_response.status_code >= 400:
            body = str(authorize_response.text or "").replace("\n", " ")[:400]
            raise RuntimeError(
                f"ANDROID协议 authorize HTTP {authorize_response.status_code}: {body}"
            )

        final_url = str(authorize_response.url or "").strip().lower()
        if "/log-in" in final_url:
            applied = self._mark_existing_email()
            tag_detail = f"，已打标: {', '.join(applied)}" if applied else ""
            raise RuntimeError(
                "ANDROID协议初始化进入登录页面，当前邮箱已被识别为已有账号；"
                f"已跳过注册触发请求{tag_detail}"
            )

        if "/email-verification" in final_url or "/email-otp" in final_url:
            self._log("ANDROID协议 authorize 已进入邮箱验证码页面，按新账号正常发码状态继续")
            trigger = {"type": "email_otp_verification"}
        else:
            trigger = self._post_first_party(
                "create_account_password",
                {"intent": "passwordless_signup_send_otp"},
                "发送邮箱验证码",
            )
        if trigger.get("type") != "email_otp_verification":
            raise RuntimeError(
                f"ANDROID协议发送验证码流程异常: type={trigger.get('type')} "
                f"body={json.dumps(trigger, ensure_ascii=False)[:500]}"
            )

        self._log(f"ANDROID协议等待邮箱验证码 timeout=30s before_ids={len(before_ids)}")
        code = self.mailbox.wait_for_code(
            self.mailbox_account,
            keyword="",
            timeout=30,
            before_ids=before_ids or None,
            code_pattern=r"(?<!#)(?<!\d)(\d{6})(?!\d)",
        )
        if not code:
            raise RuntimeError("ANDROID协议未获取到邮箱验证码")
        self._log("ANDROID协议已获取邮箱验证码")

        validated = self._post_first_party(
            "email_otp_verification",
            {"intent": "validate", "code": str(code).strip()},
            "验证邮箱验证码",
        )
        if validated.get("type") != "about_you":
            raise RuntimeError(
                f"ANDROID协议验证码验证流程异常: type={validated.get('type')} "
                f"body={json.dumps(validated, ensure_ascii=False)[:500]}"
            )

        profile_name = self._make_name()
        self._log(f"ANDROID协议提交账号资料: name={profile_name} birthday=1995-06-15")
        about_you = self._post_first_party(
            "about_you",
            {
                "name": profile_name,
                "birthday": "1995-06-15",
            },
            "提交账号资料",
        )
        if about_you.get("type") != "token_exchange":
            raise RuntimeError(
                f"ANDROID协议账号资料提交异常: type={about_you.get('type')} "
                f"body={json.dumps(about_you, ensure_ascii=False)[:500]}"
            )

        auth_code = self._extract_auth_code(about_you)
        if not auth_code:
            raise RuntimeError("ANDROID协议未获取到 OAuth authorization code")

        token_response = self.session.post(
            TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": auth_code,
                "code_verifier": self.verifier,
                "redirect_uri": REDIRECT_URI,
            },
            headers=self._api_headers(include_target_path=False),
            timeout=30,
        )
        tokens = _safe_json(token_response, "交换 OAuth token")
        access_token = str(tokens.get("access_token") or "")
        if not access_token:
            raise RuntimeError(
                f"ANDROID协议 OAuth token 缺少 access_token: "
                f"{json.dumps(tokens, ensure_ascii=False)[:500]}"
            )
        expires_at, expires_in = _token_expiry(tokens)
        self._log(
            f"ANDROID协议注册完成: access_token=yes "
            f"refresh_token={'yes' if tokens.get('refresh_token') else 'no'} "
            f"expires_in={expires_in}"
        )

        return RegistrationResult(
            success=True,
            email=requested_email,
            password=str(password or ""),
            account_id=_extract_chatgpt_account_id(access_token),
            access_token=access_token,
            refresh_token=str(tokens.get("refresh_token") or ""),
            id_token=str(tokens.get("id_token") or ""),
            session_token="",
            metadata={
                "protocol_variant": "android",
                "source": "chatgpt_android_app_protocol",
                "device_id": self.device_id,
                "chatgpt_user_agent": APP_USER_AGENT,
                "chatgpt_app_version": APP_VERSION,
                "chatgpt_app_build": APP_BUILD,
                "chatgpt_accept_language": "en-US",
                "redirect_uri": REDIRECT_URI,
                "cookies": "",
                "cookie_header": "",
                "expires_at": expires_at,
                "expires_in": expires_in,
            },
            source="chatgpt_android_app_protocol",
        )

    @staticmethod
    def _make_name() -> str:
        profile = generate_random_user_info()
        name = str(profile.get("name") or "").strip()
        return name or "Alex Morgan"
