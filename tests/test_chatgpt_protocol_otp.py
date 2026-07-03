from __future__ import annotations

import json

from platforms.chatgpt.constants import OPENAI_PAGE_TYPES
from platforms.chatgpt import register as register_module
from platforms.chatgpt.register import RegistrationEngine, SentinelPayload, SignupFormResult
from platforms.chatgpt.oauth import OAuthStart


class _JsonResponse:
    status_code = 200
    text = '{"page":{"type":"email_otp_verification"}}'

    def json(self):
        return {"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}


class _FakeSession:
    def __init__(self):
        self.posts = []

    def post(self, url, headers=None, data=None, **kwargs):
        self.posts.append((url, headers or {}, data))
        return _JsonResponse()


class _SendOtpResponse:
    status_code = 200
    text = '{"ok":true}'
    headers = {"content-type": "application/json"}

    def json(self):
        return {"ok": True}


class _SendOtpRedirectResponse:
    status_code = 302
    text = ""
    headers = {"location": "https://auth.openai.com/email-verification"}


class _SendOtpHtmlResponse:
    status_code = 200
    text = "<!DOCTYPE html><html><title>Check your inbox - OpenAI</title></html>"
    headers = {"content-type": "text/html"}


class _EmailVerificationPageResponse:
    status_code = 200
    text = "<html>Email verification</html>"


class _InvalidStateResponse:
    status_code = 409
    text = '{"error":{"code":"invalid_state","message":"Your sign-in session is no longer valid. Please start over to continue."}}'

    def json(self):
        return {
            "error": {
                "code": "invalid_state",
                "message": "Your sign-in session is no longer valid. Please start over to continue.",
            }
        }


class _DeactivatedResponse:
    status_code = 403
    text = (
        '{"error":{"code":"account_deactivated","message":"You do not have an account '
        'because it has been deleted or deactivated."}}'
    )

    def json(self):
        return {
            "error": {
                "code": "account_deactivated",
                "message": "You do not have an account because it has been deleted or deactivated.",
            }
        }


class _OtpSuccessResponse:
    status_code = 200
    text = '{"page":{"type":"about_you"}}'

    def json(self):
        return {"page": {"type": "about_you"}, "continue_url": "https://auth.openai.com/continue"}


class _SentinelResponse:
    status_code = 200
    text = '{"token":"legacy-c","proofofwork":{"required":false}}'

    def json(self):
        return {"token": "legacy-c", "proofofwork": {"required": False}}


class _AuthorizeResponse:
    status_code = 200
    text = "<html>authorize</html>"
    url = "https://auth.openai.com/email-verification"


class _Cookie:
    def __init__(self, name: str, value: str, domain: str, path: str = "/"):
        self.name = name
        self.value = value
        self.domain = domain
        self.path = path


class _CookieJar:
    def __init__(self):
        self.items = [
            _Cookie("oai-client-auth-session", "old", ".auth.openai.com"),
            _Cookie("login", "old-login", "auth.openai.com"),
            _Cookie("oai-did", "old-did", ".auth.openai.com"),
        ]
        self.cleared = []

    def __iter__(self):
        return iter(list(self.items))

    def get(self, name, *args, **kwargs):
        for cookie in self.items:
            if cookie.name == name:
                return cookie.value
        return ""

    def set(self, name, value, domain=None, path="/"):
        self.items.append(_Cookie(name, value, domain or "", path or "/"))

    def clear(self, domain=None, path=None, name=None):
        self.cleared.append((domain, path, name))
        self.items = [
            cookie
            for cookie in self.items
            if not (cookie.domain == domain and cookie.path == path and cookie.name == name)
        ]


def _bare_engine() -> RegistrationEngine:
    engine = object.__new__(RegistrationEngine)
    engine.email = "new@example.com"
    engine.password = "Secret123!"
    engine.email_info = {"service_id": "mailbox-1"}
    engine.session = _FakeSession()
    engine.logs = []
    engine.callback_logger = None
    engine.task_uuid = None
    engine.proxy_url = None
    engine._otp_sent_at = None
    engine._is_existing_account = False
    engine._device_id = None
    engine._sentinel_token = None
    engine._signup_sentinel = None
    engine._password_sentinel = None
    engine._create_account_continue_url = None
    engine._email_otp_continue_url = ""
    engine._email_otp_page_loaded = False
    engine._otp_continue_url = None
    engine._otp_page_type = None
    engine._email_otp_exhausted = False
    engine.protocol_fingerprint = register_module.ProtocolFingerprint.create()
    return engine


def test_signup_email_otp_page_is_not_treated_as_existing_account():
    engine = _bare_engine()

    result = engine._submit_signup_form("device-id", None)

    assert result.success is True
    assert result.page_type == "email_otp_verification"
    assert result.is_existing_account is False
    assert engine._is_existing_account is False


def test_signup_form_recovers_from_invalid_state_by_reauthorizing():
    engine = _bare_engine()
    engine.oauth_start = OAuthStart(
        auth_url="https://auth.openai.com/api/accounts/authorize?state=old",
        state="old",
        code_verifier="",
        redirect_uri="",
    )

    class InvalidThenSuccessSession:
        def __init__(self):
            self.cookies = _CookieJar()
            self.posts = []
            self.gets = []

        def post(self, url, headers=None, data=None, **kwargs):
            self.posts.append((url, headers or {}, data))
            return _InvalidStateResponse() if len(self.posts) == 1 else _JsonResponse()

        def get(self, url, headers=None, **kwargs):
            self.gets.append((url, headers or {}, kwargs))
            self.cookies.set("oai-client-auth-session", "new", domain=".auth.openai.com")
            return _AuthorizeResponse()

    session = InvalidThenSuccessSession()
    engine.session = session
    engine._check_sentinel = lambda did, flow="authorize_continue": SentinelPayload(
        p="p2",
        t="",
        c="c2",
        flow=flow,
    )

    result = engine._submit_signup_form(
        "device-id",
        SentinelPayload(p="p1", t="", c="c1", flow="authorize_continue"),
    )

    assert result.success is True
    assert result.page_type == "email_otp_verification"
    assert len(session.posts) == 2
    assert len(session.gets) == 1
    assert session.gets[0][0] == engine.oauth_start.auth_url
    assert session.cookies.cleared
    assert any("invalid_state" in message for message in engine.logs)


def test_check_sentinel_prefers_quickjs_token(monkeypatch):
    from platforms.chatgpt.authflow_experimental import sentinel_quickjs

    engine = _bare_engine()

    class FakeHTTPClient:
        default_headers = {"User-Agent": "ua-test"}
        session = object()

        def post(self, *_args, **_kwargs):
            raise AssertionError("legacy sentinel should not be called")

    monkeypatch.setattr(
        sentinel_quickjs,
        "get_sentinel_token_via_quickjs",
        lambda *_args, **_kwargs: json.dumps(
            {"p": "quick-p", "t": "quick-t", "c": "quick-c", "id": "device-id", "flow": "authorize_continue"}
        ),
    )
    engine.http_client = FakeHTTPClient()

    payload = engine._check_sentinel("device-id", flow="authorize_continue")

    assert payload == SentinelPayload(p="quick-p", t="quick-t", c="quick-c", flow="authorize_continue")
    assert any("QuickJS Sentinel 已启用" in message for message in engine.logs)


def test_check_sentinel_falls_back_to_legacy_when_quickjs_missing(monkeypatch):
    from platforms.chatgpt.authflow_experimental import sentinel_quickjs

    engine = _bare_engine()
    calls = {"legacy": 0}

    class FakeHTTPClient:
        default_headers = {"User-Agent": "ua-test"}
        session = object()

        def post(self, *_args, **_kwargs):
            calls["legacy"] += 1
            return _SentinelResponse()

    monkeypatch.setattr(sentinel_quickjs, "get_sentinel_token_via_quickjs", lambda *_args, **_kwargs: None)
    engine.http_client = FakeHTTPClient()

    payload = engine._check_sentinel("device-id", flow="username_password_create")

    assert payload is not None
    assert payload.t == ""
    assert payload.c == "legacy-c"
    assert payload.flow == "username_password_create"
    assert calls["legacy"] == 1


def test_platform_sentinel_header_prefers_quickjs_token(monkeypatch):
    from platforms.chatgpt.authflow_experimental import sentinel_quickjs

    engine = _bare_engine()

    class FakeClient:
        default_headers = {"User-Agent": "ua-test"}
        session = object()

        def post(self, *_args, **_kwargs):
            raise AssertionError("legacy sentinel should not be called")

    monkeypatch.setattr(
        sentinel_quickjs,
        "get_sentinel_token_via_quickjs",
        lambda *_args, **_kwargs: json.dumps(
            {"p": "quick-p", "t": "quick-t", "c": "quick-c", "id": "device-id", "flow": "oauth_create_account"}
        ),
    )

    header = engine._build_sentinel_header_for_client(FakeClient(), "device-id", "oauth_create_account")

    assert json.loads(header) == {
        "p": "quick-p",
        "t": "quick-t",
        "c": "quick-c",
        "id": "device-id",
        "flow": "oauth_create_account",
    }


def test_protocol_email_otp_signup_sends_otp_without_password_step():
    engine = _bare_engine()
    calls = {"password": 0, "send": 0}

    def create_email():
        engine.email = "new@example.com"
        engine.email_info = {"service_id": "mailbox-1"}
        return True

    def register_password():
        calls["password"] += 1
        return False, None

    def send_otp():
        calls["send"] += 1
        return True

    engine._check_ip_location = lambda: (True, "JP")
    engine._create_email = create_email
    engine._init_session = lambda: True
    engine._start_oauth = lambda: True
    engine._get_device_id = lambda: "device-id"
    engine._check_sentinel = lambda did: None
    engine._submit_signup_form = lambda did, sen: SignupFormResult(
        success=True,
        page_type=OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"],
        response_data={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}},
    )
    engine._register_password = register_password
    engine._send_verification_code = send_otp
    engine._get_verification_code = lambda: None

    result = engine.run()

    assert result.success is False
    assert result.error_message == "获取验证码失败"
    assert calls == {"password": 0, "send": 1}


def test_run_defaults_to_platform_reference_flow_when_http_client_exists(monkeypatch):
    engine = _bare_engine()
    engine.http_client = object()
    called = []

    def create_email():
        engine.email = "platform@example.com"
        return True

    def platform_reference(result):
        called.append(result.email)
        result.success = True
        result.email = engine.email
        result.access_token = "access-token"
        result.account_id = "acct-123"
        return result

    engine._check_ip_location = lambda: (True, "JP")
    engine._create_email = create_email
    engine._init_session = lambda: True
    engine._run_platform_reference_register = platform_reference
    monkeypatch.delenv("CHATGPT_REGISTER_FLOW", raising=False)

    result = engine.run()

    assert result.success is True
    assert result.email == "platform@example.com"
    assert called == ["platform@example.com"]


def test_send_verification_code_uses_password_referer_like_reference_flow():
    engine = _bare_engine()
    calls = []

    class SendSession:
        def get(self, url, headers=None, timeout=None, **kwargs):
            calls.append((url, headers or {}))
            return _SendOtpResponse()

    engine.session = SendSession()

    assert engine._send_verification_code() is True
    assert calls[-1][0].endswith("/api/accounts/email-otp/send")
    assert calls[-1][1]["referer"] == "https://auth.openai.com/create-account/password"


def test_send_verification_code_visits_email_verification_page_before_send():
    engine = _bare_engine()
    engine._email_otp_continue_url = "https://auth.openai.com/email-verification"
    calls = []

    class SendSession:
        def get(self, url, headers=None, timeout=None, **kwargs):
            calls.append((url, headers or {}))
            if len(calls) == 1:
                return _EmailVerificationPageResponse()
            return _SendOtpResponse()

    engine.session = SendSession()

    assert engine._send_verification_code() is True
    assert calls[0][0] == "https://auth.openai.com/email-verification"
    assert calls[1][0].endswith("/api/accounts/email-otp/send")
    assert engine._email_otp_page_loaded is True


def test_send_verification_code_keeps_original_redirect_response():
    engine = _bare_engine()
    engine._email_otp_page_loaded = True
    calls = []

    class SendSession:
        def get(self, url, headers=None, timeout=None, **kwargs):
            calls.append((url, headers or {}, kwargs))
            return _SendOtpRedirectResponse()

        def post(self, url, headers=None, **kwargs):  # pragma: no cover - 不应触发兜底
            raise AssertionError("redirect success should not use POST fallback")

    engine.session = SendSession()

    assert engine._send_verification_code() is True
    assert calls[0][0].endswith("/api/accounts/email-otp/send")
    assert calls[0][2]["allow_redirects"] is False
    assert engine._otp_sent_at is not None


def test_send_verification_code_does_not_accept_html_page_as_api_success():
    engine = _bare_engine()
    engine._email_otp_page_loaded = True
    calls = []

    class SendSession:
        def get(self, url, headers=None, timeout=None, **kwargs):
            calls.append(("GET", url, kwargs))
            return _SendOtpHtmlResponse()

        def post(self, url, headers=None, **kwargs):
            calls.append(("POST", url, kwargs))
            return _SendOtpResponse()

    engine.session = SendSession()

    assert engine._send_verification_code() is True
    assert [call[0] for call in calls] == ["GET", "GET", "POST"]
    assert all(call[2]["allow_redirects"] is False for call in calls)
    assert any("非 JSON 200" in str(item) for item in engine.logs)


def test_get_verification_code_resends_after_each_60s_timeout(monkeypatch):
    engine = _bare_engine()
    engine._otp_sent_at = 1000.0
    send_calls = []
    waits = []

    class EmailService:
        def get_verification_code(self, **kwargs):
            waits.append(kwargs)
            return "654321" if len(waits) == 3 else ""

    engine.email_service = EmailService()
    engine._send_verification_code = lambda: send_calls.append("send") or True
    monkeypatch.setenv("CHATGPT_OTP_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("CHATGPT_EMAIL_OTP_MAX_ATTEMPTS", "3")

    assert engine._get_verification_code() == "654321"
    assert send_calls == ["send", "send"]
    assert [item["timeout"] for item in waits] == [60, 60, 60]
    assert any("重新发送验证码 (2/3)" in message for message in engine.logs)


def test_get_verification_code_marks_invalid_email_after_three_timeouts(monkeypatch):
    engine = _bare_engine()
    engine._otp_sent_at = 1000.0
    marks = []

    class EmailService:
        def get_verification_code(self, **kwargs):
            raise TimeoutError("no code")

        def mark_invalid_email(self, *, reason: str = ""):
            marks.append(reason)
            return ["无效邮箱"]

    engine.email_service = EmailService()
    engine._send_verification_code = lambda: True
    monkeypatch.setenv("CHATGPT_OTP_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("CHATGPT_EMAIL_OTP_MAX_ATTEMPTS", "3")

    assert engine._get_verification_code() is None
    assert marks == ["invalid_email_no_otp"]
    assert any("已给邮箱 new@example.com 打标: 无效邮箱" in message for message in engine.logs)


def test_get_verification_code_can_skip_invalid_mark_for_token_subflows(monkeypatch):
    engine = _bare_engine()
    marks = []

    class EmailService:
        def get_verification_code(self, **kwargs):
            return ""

        def mark_invalid_email(self, *, reason: str = ""):
            marks.append(reason)
            return ["无效邮箱"]

    engine.email_service = EmailService()
    engine._send_verification_code = lambda: True
    monkeypatch.setenv("CHATGPT_OTP_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("CHATGPT_EMAIL_OTP_MAX_ATTEMPTS", "3")

    assert engine._get_verification_code(mark_invalid_on_timeout=False) is None
    assert marks == []


def test_validate_verification_code_recovers_from_invalid_state(monkeypatch):
    engine = _bare_engine()
    events = {"start": 0, "send": 0, "refresh_seen": 0}

    class InvalidSession:
        def post(self, url, headers=None, data=None, **kwargs):
            return _InvalidStateResponse()

    class SuccessSession:
        def post(self, url, headers=None, data=None, **kwargs):
            return _OtpSuccessResponse()

    class FakeHttpClient:
        default_headers = {"User-Agent": "Mozilla/5.0 Chrome/136"}

        def __init__(self, proxy_url=None):
            self.session = SuccessSession()

    class EmailService:
        def refresh_before_ids(self):
            events["refresh_seen"] += 1
            return {"old-message-id"}

    engine.session = InvalidSession()
    engine.email_service = EmailService()
    engine._start_oauth = lambda: events.__setitem__("start", events["start"] + 1) or True
    engine._get_device_id = lambda: "device-id"
    engine._check_sentinel = lambda did: None
    engine._submit_signup_form = lambda did, sen: SignupFormResult(
        success=True,
        page_type=OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"],
    )
    engine._send_verification_code = lambda: events.__setitem__("send", events["send"] + 1) or True
    engine._get_verification_code = lambda: "222222"
    monkeypatch.setattr(register_module, "OpenAIHTTPClient", FakeHttpClient)

    assert engine._validate_verification_code("111111") is True
    assert events == {"start": 1, "send": 1, "refresh_seen": 1}
    assert engine._otp_page_type == "about_you"


def test_platform_login_validate_preserves_deactivated_response_without_retry():
    engine = _bare_engine()

    class Session:
        def __init__(self):
            self.posts = 0

        def post(self, url, headers=None, data=None, **kwargs):
            self.posts += 1
            if self.posts == 1:
                return _DeactivatedResponse()
            return _InvalidStateResponse()

    client = type("Client", (), {"session": Session(), "default_headers": {"User-Agent": "Mozilla/5.0 Chrome/136"}})()
    engine._build_sentinel_header_for_client = lambda *_args, **_kwargs: "sentinel"

    response = engine._validate_platform_login_otp(client, "device-id", "123456")

    assert response.status_code == 403
    assert "deleted or deactivated" in response.text
    assert client.session.posts == 1
    assert any("保留首次响应不重复提交 OTP" in message for message in engine.logs)


def test_create_user_account_deletes_mailbox_when_openai_marks_email_deactivated():
    engine = _bare_engine()
    deleted = {}

    class DumpResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {}

    class DeactivatedCreateAccountResponse:
        status_code = 403
        text = (
            '{"error":{"message":"You do not have an account because it has been '
            'deleted or deactivated."}}'
        )

        def json(self):
            return {
                "error": {
                    "message": "You do not have an account because it has been deleted or deactivated.",
                }
            }

    class CreateAccountSession:
        def get(self, url, headers=None, timeout=None):
            return DumpResponse()

        def post(self, url, headers=None, data=None, **kwargs):
            return DeactivatedCreateAccountResponse()

    class EmailService:
        service_type = type("ST", (), {"value": "mailbox"})()

        def delete_current_email(self, *, reason: str = ""):
            deleted["reason"] = reason
            return True

    engine.session = CreateAccountSession()
    engine.email_service = EmailService()

    assert engine._create_user_account() is False
    assert deleted["reason"] == "openai_account_deleted_or_deactivated"
    assert any("已通过邮箱接口删除不可用邮箱" in message for message in engine.logs)
