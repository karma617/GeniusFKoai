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
    engine._latest_chatgpt_init_final_url = ""
    engine._email_otp_exhausted = False
    engine._email_otp_failure_reason = ""
    engine._last_about_you_error = ""
    engine._last_create_account_error_code = ""
    engine._last_create_account_transport_error = ""
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
    captured = {}

    class FakeHTTPClient:
        default_headers = {"User-Agent": "ua-test"}
        session = object()

        def post(self, *_args, **_kwargs):
            raise AssertionError("legacy sentinel should not be called")

    def quickjs_tokens(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "token": json.dumps(
                {"p": "quick-p", "t": "quick-t", "c": "quick-c", "id": "device-id", "flow": "authorize_continue"}
            ),
            "so_token": '{"so":"quick-so","c":"quick-c","id":"device-id","flow":"authorize_continue"}',
        }

    monkeypatch.setattr(
        sentinel_quickjs,
        "get_sentinel_tokens_via_quickjs",
        quickjs_tokens,
    )
    engine.http_client = FakeHTTPClient()

    payload = engine._check_sentinel("device-id", flow="authorize_continue")

    assert payload == SentinelPayload(
        p="quick-p",
        t="quick-t",
        c="quick-c",
        flow="authorize_continue",
        so_token='{"so":"quick-so","c":"quick-c","id":"device-id","flow":"authorize_continue"}',
    )
    assert captured["user_agent"] == "ua-test"
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

    monkeypatch.setattr(sentinel_quickjs, "get_sentinel_tokens_via_quickjs", lambda *_args, **_kwargs: None)
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
        "get_sentinel_tokens_via_quickjs",
        lambda *_args, **_kwargs: {
            "token": json.dumps(
                {"p": "quick-p", "t": "quick-t", "c": "quick-c", "id": "device-id", "flow": "oauth_create_account"}
            ),
            "so_token": "",
        },
    )

    header = engine._build_sentinel_header_for_client(FakeClient(), "device-id", "oauth_create_account")

    assert json.loads(header) == {
        "p": "quick-p",
        "t": "quick-t",
        "c": "quick-c",
        "id": "device-id",
        "flow": "oauth_create_account",
    }


def test_create_account_sends_quickjs_sentinel_so_token():
    engine = _bare_engine()
    captured = {}

    class DumpResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {}

    class CreateAccountResponse:
        status_code = 200
        text = '{"continue_url":"https://chatgpt.com/api/auth/callback/openai?code=ok"}'

        def json(self):
            return {"continue_url": "https://chatgpt.com/api/auth/callback/openai?code=ok"}

    class CreateAccountSession:
        def get(self, url, headers=None, timeout=None):
            return DumpResponse()

        def post(self, url, headers=None, data=None, **kwargs):
            captured["headers"] = headers or {}
            captured["data"] = data
            return CreateAccountResponse()

    engine.session = CreateAccountSession()
    engine._device_id = "device-id"
    engine._check_sentinel = lambda did, flow="authorize_continue": SentinelPayload(
        p="quick-p",
        t="quick-t",
        c="quick-c",
        flow=flow,
        so_token='{"so":"quick-so","c":"quick-c","id":"device-id","flow":"oauth_create_account"}',
    )

    assert engine._create_user_account() is True

    assert json.loads(captured["headers"]["openai-sentinel-token"]) == {
        "p": "quick-p",
        "t": "quick-t",
        "c": "quick-c",
        "id": "device-id",
        "flow": "oauth_create_account",
    }
    assert (
        captured["headers"]["openai-sentinel-so-token"]
        == '{"so":"quick-so","c":"quick-c","id":"device-id","flow":"oauth_create_account"}'
    )


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


def test_latest_chatgpt_register_flow_uses_login_hint_and_session(monkeypatch):
    engine = _bare_engine()
    calls = []

    class Response:
        def __init__(self, status_code=200, data=None, text="", headers=None, url=""):
            self.status_code = status_code
            self._data = data if data is not None else {}
            self.text = text or json.dumps(self._data)
            self.headers = headers or {}
            self.url = url

        def json(self):
            return self._data

    class EmailService:
        service_type = type("ST", (), {"value": "outlook_email_api"})()

        def create_email(self):
            return {"email": "new@example.com", "service_id": "mailbox-1"}

        def refresh_before_ids(self):
            return {"old-message"}

        def get_verification_code(self, **kwargs):
            assert kwargs["email"] == "new@example.com"
            assert kwargs["otp_sent_at"] is not None
            return "654321"

    class Session:
        def __init__(self):
            self.cookies = _CookieJar()

        def get(self, url, headers=None, allow_redirects=True, timeout=None, **kwargs):
            calls.append(("GET", url, headers or None))
            if url == "https://chatgpt.com/api/auth/csrf":
                return Response(data={"csrfToken": "csrf-token"})
            if url == "https://chatgpt.com/api/auth/session":
                return Response(
                    data={
                        "accessToken": "access-token",
                        "sessionToken": "session-token-json",
                        "account": {"id": "acct_123"},
                        "user": {"email": "new@example.com"},
                        "expires": "2026-07-14T00:00:00.000Z",
                    }
                )
            if url == "https://chatgpt.com/":
                return Response()
            if url == "https://auth.openai.com/oauth/start":
                return Response(status_code=302, headers={"Location": "https://auth.openai.com/email-verification"})
            if url == "https://auth.openai.com/email-verification":
                return Response(text="<html>Email verification</html>")
            if url == "https://auth.openai.com/about-you":
                return Response(text="<html>About you</html>", url="https://chatgpt.com/")
            if url == "https://auth.openai.com/api/accounts/client_auth_session_dump":
                raise AssertionError("latest chatgpt_register flow must not call client_auth_session_dump")
            if url.startswith("https://chatgpt.com/api/auth/callback/openai"):
                self.cookies.set("__Secure-next-auth.session-token", "session-token-cookie")
                self.cookies.set("_account", "acct_cookie")
                return Response()
            raise AssertionError(f"unexpected GET {url}")

        def post(self, url, headers=None, data=None, allow_redirects=True, timeout=None, **kwargs):
            calls.append(("POST", url, data))
            assert "/api/accounts/user/register" not in url
            assert "/oauth/token" not in url
            if url.startswith("https://chatgpt.com/api/auth/signin/openai?"):
                assert "login_hint=new%40example.com" in url
                return Response(data={"url": "https://auth.openai.com/oauth/start"})
            if url == "https://auth.openai.com/api/accounts/email-otp/validate":
                assert json.loads(data)["code"] == "654321"
                return Response(data={"continue_url": "https://auth.openai.com/about-you", "page": {"type": "about_you"}})
            if url == "https://auth.openai.com/api/accounts/create_account":
                payload = json.loads(data)
                assert payload["name"]
                assert payload["birthdate"]
                return Response(data={"continue_url": "https://chatgpt.com/api/auth/callback/openai?code=abc&state=xyz"})
            raise AssertionError(f"unexpected POST {url}")

    engine.email_service = EmailService()
    engine.session = Session()
    engine._init_latest_chatgpt_session = lambda: True
    engine._check_sentinel = lambda *args, **kwargs: None
    monkeypatch.setattr(register_module.time, "sleep", lambda _seconds: None)

    result = engine.run_chatgpt_register_latest()

    assert result.success is True
    assert result.email == "new@example.com"
    assert result.account_id == "acct_123"
    assert result.access_token == "access-token"
    assert result.refresh_token == ""
    assert result.session_token == "session-token-json"
    assert result.metadata["auth_source"] == "chatgpt_register_latest"
    assert any(
        item[0] == "POST" and item[1] == "https://auth.openai.com/api/accounts/create_account"
        for item in calls
    )
    assert not any("/api/accounts/user/register" in item[1] for item in calls)
    assert not any(item[1] == "https://auth.openai.com/api/accounts/client_auth_session_dump" for item in calls)


def test_latest_chatgpt_session_uses_firefox144(monkeypatch):
    engine = _bare_engine()
    created = {}

    class FakeHttpClient:
        def __init__(self, proxy_url=None, config=None):
            self.session = object()
            self.default_headers = {}
            created["proxy_url"] = proxy_url
            created["config"] = config
            created["session"] = self.session
            created["client"] = self

    engine.proxy_url = "http://127.0.0.1:7897"
    monkeypatch.setattr(register_module, "OpenAIHTTPClient", FakeHttpClient)

    assert engine._init_latest_chatgpt_session() is True
    assert created["proxy_url"] == "http://127.0.0.1:7897"
    assert created["config"].impersonate == "firefox144"
    assert created["config"].timeout == 60
    assert created["client"].default_headers["User-Agent"] == register_module.LATEST_CHATGPT_FIREFOX_USER_AGENT
    assert engine.session is created["session"]


def test_latest_chatgpt_register_continues_when_about_you_redirects_elsewhere(monkeypatch):
    engine = _bare_engine()
    calls = {"create": 0, "finish": 0}

    class EmailService:
        service_type = type("ST", (), {"value": "outlook_email_api"})()

        def create_email(self):
            return {"email": "new@example.com", "service_id": "mailbox-1"}

    engine.email_service = EmailService()
    engine._init_latest_chatgpt_session = lambda: True
    engine._refresh_mailbox_before_ids = lambda: None
    engine._latest_chatgpt_init_email_oauth = lambda: (True, "")
    engine._get_verification_code = lambda **_kwargs: "654321"
    engine._latest_chatgpt_validate_email_otp = lambda code: {"continue_url": "https://auth.openai.com/about-you"}

    def open_about_you(url):
        engine._log(
            "chatgpt_register about-you 最终页面不是 /about-you: https://chatgpt.com/，按源项目语义继续创建账号资料",
            "warning",
        )
        return True

    engine._latest_chatgpt_open_about_you = open_about_you

    def create_account():
        calls["create"] += 1
        return True

    def finish(result):
        calls["finish"] += 1
        result.success = True
        result.email = engine.email
        result.account_id = "acct_123"
        result.access_token = "access-token"
        return result

    engine._latest_chatgpt_create_account_with_retry = create_account
    engine._latest_chatgpt_fetch_session_result = finish
    monkeypatch.setattr(register_module.time, "sleep", lambda _seconds: None)

    result = engine.run_chatgpt_register_latest()

    assert result.success is True
    assert calls == {"create": 1, "finish": 1}
    assert any("按源项目语义继续创建账号资料" in message for message in engine.logs)




def test_latest_chatgpt_register_uses_otp_callback_without_create_account(monkeypatch):
    engine = _bare_engine()
    calls = {"about": 0, "create": 0, "finish": 0}

    class EmailService:
        service_type = type("ST", (), {"value": "outlook_email_api"})()

        def create_email(self):
            return {"email": "new@example.com", "service_id": "mailbox-1"}

    callback_url = "https://chatgpt.com/api/auth/callback/openai?code=code_1&state=state_1"
    engine.email_service = EmailService()
    engine._init_latest_chatgpt_session = lambda: True
    engine._refresh_mailbox_before_ids = lambda: None
    engine._latest_chatgpt_init_email_oauth = lambda: (True, "")
    engine._get_verification_code = lambda **_kwargs: "808611"
    engine._latest_chatgpt_validate_email_otp = lambda code: {
        "continue_url": callback_url,
        "page": {"type": "external_url", "payload": {"url": callback_url}},
    }

    def open_about_you(url):
        calls["about"] += 1
        raise AssertionError("OTP callback branch must not open about-you")

    def create_account():
        calls["create"] += 1
        raise AssertionError("OTP callback branch must not call create_account")

    def finish(result):
        calls["finish"] += 1
        assert engine._create_account_continue_url == callback_url
        assert engine._is_existing_account is True
        assert engine._latest_chatgpt_session_source == "latest_otp_external_callback"
        result.success = True
        result.email = engine.email
        result.source = "login"
        return result

    engine._latest_chatgpt_open_about_you = open_about_you
    engine._latest_chatgpt_create_account_with_retry = create_account
    engine._latest_chatgpt_fetch_session_result = finish
    monkeypatch.setattr(register_module.time, "sleep", lambda _seconds: None)

    result = engine.run_chatgpt_register_latest()

    assert result.success is True
    assert result.source == "login"
    assert calls == {"about": 0, "create": 0, "finish": 1}
    assert any("跳过 about-you/create_account" in message for message in engine.logs)


def test_latest_chatgpt_fetch_session_marks_otp_callback_as_existing_account():
    engine = _bare_engine()

    class EmailService:
        service_type = type("ST", (), {"value": "outlook_email_api"})()

    class Response:
        def __init__(self, data=None):
            self.status_code = 200
            self._data = data if data is not None else {}
            self.text = json.dumps(self._data)

        def json(self):
            return self._data

    class Session:
        def __init__(self):
            self.cookies = _CookieJar()

        def get(self, url, headers=None, allow_redirects=True, timeout=None, **kwargs):
            if url.startswith("https://chatgpt.com/api/auth/callback/openai"):
                self.cookies.set("__Secure-next-auth.session-token", "session-cookie")
                self.cookies.set("_account", "acct_cookie")
                return Response()
            if url == "https://chatgpt.com/api/auth/session":
                return Response(
                    {
                        "accessToken": "access-token",
                        "sessionToken": "session-token-json",
                        "account": {"id": "acct_123"},
                        "user": {"email": "new@example.com"},
                        "expires": "2026-07-14T00:00:00.000Z",
                    }
                )
            raise AssertionError(f"unexpected GET {url}")

    engine.email_service = EmailService()
    engine.session = Session()
    engine._is_existing_account = True
    engine._latest_chatgpt_session_source = "latest_otp_external_callback"
    engine._create_account_continue_url = "https://chatgpt.com/api/auth/callback/openai?code=code_1&state=state_1"

    result = engine._latest_chatgpt_fetch_session_result(register_module.RegistrationResult(success=False))

    assert result.success is True
    assert result.source == "login"
    assert result.account_id == "acct_123"
    assert result.metadata["is_existing_account"] is True
    assert result.metadata["chatgpt_session_source"] == "latest_otp_external_callback"


def test_latest_chatgpt_register_submits_password_before_waiting_for_otp(monkeypatch):
    engine = _bare_engine()
    calls = []

    class EmailService:
        service_type = type("ST", (), {"value": "icloud_hme"})()

        def create_email(self):
            return {"email": "new@example.com", "service_id": "mailbox-1"}

    engine.email_service = EmailService()
    engine._init_latest_chatgpt_session = lambda: True
    engine._refresh_mailbox_before_ids = lambda: None

    def init_oauth():
        calls.append("init")
        engine._latest_chatgpt_init_final_url = "https://auth.openai.com/create-account/password"
        return True, ""

    def register_password():
        calls.append("password")
        engine.password = "Generated123!"
        engine._email_otp_continue_url = "https://auth.openai.com/email-verification"
        engine._otp_sent_at = 1000.0
        return True, engine.password

    def get_verification_code(**_kwargs):
        calls.append("otp")
        assert calls[:2] == ["init", "password"]
        assert engine._otp_sent_at == 1000.0
        return "654321"

    def finish(result):
        result.success = True
        result.email = engine.email
        result.account_id = "acct_123"
        result.access_token = "access-token"
        return result

    engine._latest_chatgpt_init_email_oauth = init_oauth
    engine._register_password = register_password
    engine._get_verification_code = get_verification_code
    engine._latest_chatgpt_validate_email_otp = lambda code: {"continue_url": "https://auth.openai.com/about-you"}
    engine._latest_chatgpt_open_about_you = lambda url: True
    engine._latest_chatgpt_create_account_with_retry = lambda: True
    engine._latest_chatgpt_fetch_session_result = finish
    monkeypatch.setattr(register_module.time, "sleep", lambda _seconds: None)

    result = engine.run_chatgpt_register_latest()

    assert result.success is True
    assert result.password == "Generated123!"
    assert calls == ["init", "password", "otp"]


def test_latest_chatgpt_register_retries_init_transport_error(monkeypatch):
    engine = _bare_engine()
    calls = {"init": 0, "refresh": 0, "reset": 0}

    class EmailService:
        service_type = type("ST", (), {"value": "outlook_email_api"})()

        def create_email(self):
            return {"email": "new@example.com", "service_id": "mailbox-1"}

    engine.email_service = EmailService()
    engine._init_latest_chatgpt_session = lambda: True
    engine._refresh_mailbox_before_ids = lambda: calls.__setitem__("refresh", calls["refresh"] + 1)
    engine._reset_latest_chatgpt_session_for_retry = lambda: calls.__setitem__("reset", calls["reset"] + 1)
    engine._get_verification_code = lambda **_kwargs: "654321"
    engine._latest_chatgpt_validate_email_otp = lambda code: {"continue_url": "https://auth.openai.com/about-you"}
    engine._latest_chatgpt_open_about_you = lambda url: True
    engine._latest_chatgpt_create_account_with_retry = lambda: True

    def init_oauth():
        calls["init"] += 1
        if calls["init"] == 1:
            return (
                False,
                "Failed to perform, curl: (35) TLS connect error: "
                "error:00000000:invalid library (0):OPENSSL_internal:invalid library (0).",
            )
        return True, ""

    def finish(result):
        result.success = True
        result.email = engine.email
        result.account_id = "acct_123"
        result.access_token = "access-token"
        return result

    engine._latest_chatgpt_init_email_oauth = init_oauth
    engine._latest_chatgpt_fetch_session_result = finish
    monkeypatch.setattr(register_module.time, "sleep", lambda _seconds: None)

    result = engine.run_chatgpt_register_latest()

    assert result.success is True
    assert calls["init"] == 2
    assert calls["reset"] == 1
    assert calls["refresh"] == 2


def test_latest_chatgpt_create_account_retries_transport_error(monkeypatch):
    engine = _bare_engine()
    calls = {"create": 0}

    def create_account():
        calls["create"] += 1
        if calls["create"] == 1:
            engine._last_create_account_transport_error = "curl: (55) BAD_DECRYPT"
            return False
        engine._create_account_continue_url = "https://chatgpt.com/api/auth/callback/openai?code=abc"
        return True

    engine._latest_chatgpt_create_user_account = create_account
    monkeypatch.setattr(register_module.time, "sleep", lambda _seconds: None)

    assert engine._latest_chatgpt_create_account_with_retry() is True
    assert calls["create"] == 2


def test_latest_chatgpt_init_signin_403_records_response_hint():
    engine = _bare_engine()

    class Response:
        def __init__(self, status_code=200, data=None, text="", headers=None):
            self.status_code = status_code
            self._data = data if data is not None else {}
            self.text = text or json.dumps(self._data)
            self.headers = headers or {}

        def json(self):
            return self._data

    class Session:
        def __init__(self):
            self.cookies = _CookieJar()

        def get(self, url, **_kwargs):
            if url == "https://chatgpt.com/api/auth/csrf":
                return Response(data={"csrfToken": "csrf-token"})
            return Response()

        def post(self, url, **_kwargs):
            assert url.startswith("https://chatgpt.com/api/auth/signin/openai?")
            return Response(status_code=403, text="Forbidden by edge policy")

    engine.session = Session()

    ok, error = engine._latest_chatgpt_init_email_oauth()

    assert ok is False
    assert error == "signin_no_authorize_url_http_403:body=Forbidden by edge policy"
    assert any("signin/openai 未返回 authorize URL" in item for item in engine.logs)


def test_latest_chatgpt_register_retries_init_signin_403(monkeypatch):
    engine = _bare_engine()
    calls = {"init": 0, "refresh": 0, "reset": 0}

    class EmailService:
        service_type = type("ST", (), {"value": "outlook_email_api"})()

        def create_email(self):
            return {"email": "new@example.com", "service_id": "mailbox-1"}

    engine.email_service = EmailService()
    engine._init_latest_chatgpt_session = lambda: True
    engine._refresh_mailbox_before_ids = lambda: calls.__setitem__("refresh", calls["refresh"] + 1)
    engine._reset_latest_chatgpt_session_for_retry = lambda: calls.__setitem__("reset", calls["reset"] + 1)
    engine._get_verification_code = lambda **_kwargs: "654321"
    engine._latest_chatgpt_validate_email_otp = lambda code: {"continue_url": "https://auth.openai.com/about-you"}
    engine._latest_chatgpt_open_about_you = lambda url: True
    engine._latest_chatgpt_create_account_with_retry = lambda: True

    def init_oauth():
        calls["init"] += 1
        if calls["init"] == 1:
            return False, "signin_no_authorize_url_http_403:body=Forbidden by edge policy"
        return True, ""

    def finish(result):
        result.success = True
        result.email = engine.email
        result.account_id = "acct_123"
        result.access_token = "access-token"
        return result

    engine._latest_chatgpt_init_email_oauth = init_oauth
    engine._latest_chatgpt_fetch_session_result = finish
    monkeypatch.setattr(register_module.time, "sleep", lambda _seconds: None)

    result = engine.run_chatgpt_register_latest()

    assert result.success is True
    assert calls["init"] == 2
    assert calls["reset"] == 1
    assert calls["refresh"] == 2


def test_latest_chatgpt_register_sends_otp_after_password_email_otp_send(monkeypatch):
    engine = _bare_engine()
    calls = []

    class EmailService:
        service_type = type("ST", (), {"value": "outlook_email_api"})()

        def create_email(self):
            return {"email": "new@example.com", "service_id": "mailbox-1"}

    engine.email_service = EmailService()
    engine._init_latest_chatgpt_session = lambda: True
    engine._refresh_mailbox_before_ids = lambda: None

    def init_oauth():
        calls.append("init")
        engine._latest_chatgpt_init_final_url = "https://auth.openai.com/create-account/password"
        return True, ""

    def register_password():
        calls.append("password")
        engine._otp_page_type = "email_otp_send"
        engine._email_otp_continue_url = "https://auth.openai.com/email-verification"
        return True, engine.password

    def send_verification_code():
        calls.append("send")
        engine._otp_sent_at = 2000.0
        return True

    def get_verification_code(**_kwargs):
        calls.append("otp")
        assert calls[:3] == ["init", "password", "send"]
        assert engine._otp_sent_at == 2000.0
        return "654321"

    def finish(result):
        result.success = True
        result.email = engine.email
        result.account_id = "acct_123"
        result.access_token = "access-token"
        return result

    engine._latest_chatgpt_init_email_oauth = init_oauth
    engine._register_password = register_password
    engine._send_verification_code = send_verification_code
    engine._get_verification_code = get_verification_code
    engine._latest_chatgpt_validate_email_otp = lambda code: {"continue_url": "https://auth.openai.com/about-you"}
    engine._latest_chatgpt_open_about_you = lambda url: True
    engine._latest_chatgpt_create_account_with_retry = lambda: True
    engine._latest_chatgpt_fetch_session_result = finish
    monkeypatch.setattr(register_module.time, "sleep", lambda _seconds: None)

    result = engine.run_chatgpt_register_latest()

    assert result.success is True
    assert calls == ["init", "password", "send", "otp"]


def test_latest_chatgpt_register_refreshes_otp_once_after_invalid_state(monkeypatch):
    engine = _bare_engine()
    calls = {"init": 0, "refresh": 0, "reset": 0, "send": 0, "otp": [], "validate": []}

    class EmailService:
        service_type = type("ST", (), {"value": "outlook_email_api"})()

        def create_email(self):
            return {"email": "new@example.com", "service_id": "mailbox-1"}

    engine.email_service = EmailService()
    engine._init_latest_chatgpt_session = lambda: True
    engine._refresh_mailbox_before_ids = lambda: calls.__setitem__("refresh", calls["refresh"] + 1)
    engine._reset_latest_chatgpt_session_for_retry = lambda: calls.__setitem__("reset", calls["reset"] + 1)
    engine._send_verification_code = lambda: calls.__setitem__("send", calls["send"] + 1) or True

    def init_oauth():
        calls["init"] += 1
        engine._latest_chatgpt_init_final_url = "https://auth.openai.com/email-verification"
        return True, ""

    def get_verification_code(*, mark_invalid_on_timeout=True, resend_on_timeout=True):
        calls["otp"].append((mark_invalid_on_timeout, resend_on_timeout))
        return "111111" if len(calls["otp"]) == 1 else "222222"

    def validate_email_otp(code):
        calls["validate"].append(code)
        if code == "111111":
            raise RuntimeError("invalid_state")
        return {"continue_url": "https://auth.openai.com/about-you"}

    def finish(result):
        result.success = True
        result.email = engine.email
        result.account_id = "acct_123"
        result.access_token = "access-token"
        return result

    engine._latest_chatgpt_init_email_oauth = init_oauth
    engine._get_verification_code = get_verification_code
    engine._latest_chatgpt_validate_email_otp = validate_email_otp
    engine._latest_chatgpt_open_about_you = lambda url: True
    engine._latest_chatgpt_create_account_with_retry = lambda: True
    engine._latest_chatgpt_fetch_session_result = finish
    monkeypatch.setattr(register_module.time, "sleep", lambda _seconds: None)

    result = engine.run_chatgpt_register_latest()

    assert result.success is True
    assert calls["init"] == 2
    assert calls["refresh"] == 2
    assert calls["reset"] == 1
    assert calls["send"] == 1
    assert calls["otp"] == [(True, False), (False, False)]
    assert calls["validate"] == ["111111", "222222"]


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


def test_get_verification_code_can_wait_without_resending_for_latest_flow(monkeypatch):
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

    assert engine._get_verification_code(resend_on_timeout=False) == "654321"
    assert send_calls == []
    assert [item["timeout"] for item in waits] == [60, 60, 60]
    assert any("继续等待已触发的验证码 (2/3)" in message for message in engine.logs)


def test_get_verification_code_stops_on_mailbox_account_not_found(monkeypatch):
    engine = _bare_engine()
    engine._otp_sent_at = 1000.0
    waits = []
    marks = []
    send_calls = []

    class EmailService:
        def get_verification_code(self, **kwargs):
            waits.append(kwargs)
            raise TimeoutError(
                "等待验证码超时 (10s)，最后一次错误: "
                "outlookEmail GET /api/external/messages 请求失败: 账号不存在"
            )

        def mark_invalid_email(self, *, reason: str = ""):
            marks.append(reason)
            return ["无效邮箱"]

    engine.email_service = EmailService()
    engine._send_verification_code = lambda: send_calls.append("send") or True
    monkeypatch.setenv("CHATGPT_OTP_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("CHATGPT_EMAIL_OTP_MAX_ATTEMPTS", "3")

    assert engine._get_verification_code() is None
    assert len(waits) == 1
    assert send_calls == []
    assert marks == ["mailbox_account_not_found"]
    assert engine._email_otp_exhausted is True
    assert engine._email_otp_failure_reason == "mailbox_account_not_found"
    assert engine._email_otp_failure_message() == "邮箱账号不存在或不可读，已标记无效邮箱"
    assert any("邮箱账号不可读，停止等待验证码" in message for message in engine.logs)


def test_get_verification_code_defaults_to_10s_timeout(monkeypatch):
    engine = _bare_engine()
    engine._otp_sent_at = 1000.0
    waits = []

    class EmailService:
        def get_verification_code(self, **kwargs):
            waits.append(kwargs)
            return "654321"

    engine.email_service = EmailService()
    monkeypatch.delenv("CHATGPT_OTP_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("CHATGPT_EMAIL_OTP_MAX_ATTEMPTS", raising=False)

    assert engine._get_verification_code() == "654321"
    assert [item["timeout"] for item in waits] == [10]


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
    assert any("邮箱无效打标完成: 当前邮箱 new@example.com; 无效邮箱" in message for message in engine.logs)


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
