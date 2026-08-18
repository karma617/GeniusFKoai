from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from platforms.chatgpt import protocol_get_rt


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        url: str = "https://auth.openai.com/test",
        data=None,
        text: str = "",
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self.url = url
        self.headers = {"content-type": "application/json"}
        if headers:
            self.headers.update(headers)
        self._data = data
        self.text = text if text else json.dumps(data or {}, ensure_ascii=False)

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


def test_protocol_get_rt_logs_authorize_continue_409_detail(monkeypatch):
    logs = []

    class FakeSession:
        def __init__(self):
            self.cookies = {}

        def get(self, url, **_kwargs):
            return _FakeResponse(403, url=url, data={"error": "forbidden"})

        def post(self, url, **_kwargs):
            return _FakeResponse(
                409,
                url=url,
                data={"error": {"code": "invalid_state", "message": "state no longer valid"}},
            )

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = FakeSession()

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.email = ""
            self.password = ""

        def _set_oai_did_for_session(self, session, device_id):
            session.cookies["oai-did"] = device_id

        def _build_sentinel_header_for_client(self, *_args, **_kwargs):
            return "sentinel"

        def _platform_json_headers(self, **_kwargs):
            return {"referer": "https://auth.openai.com/log-in", "content-type": "application/json"}

        @staticmethod
        def _is_invalid_state_response(_resp):
            return False

    monkeypatch.setattr(protocol_get_rt, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(protocol_get_rt, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(
        protocol_get_rt,
        "generate_oauth_url",
        lambda **_kwargs: SimpleNamespace(
            auth_url="https://auth.openai.com/oauth/authorize?state=state_1",
            state="state_1",
            code_verifier="verifier_1",
        ),
    )

    with pytest.raises(RuntimeError) as excinfo:
        protocol_get_rt.run_protocol_get_rt(
            email="user@example.com",
            password="Secret123!",
            proxy=None,
            otp_callback=lambda: "123456",
            log_fn=logs.append,
        )

    assert "HTTP 409" in str(excinfo.value)
    assert "invalid_state" in str(excinfo.value)
    assert any("authorize/continue debug: status=409" in item for item in logs)
    assert any("authorize/continue body:" in item and "state no longer valid" in item for item in logs)


def test_protocol_get_rt_uses_har_continue_body_and_does_not_pre_send_otp(monkeypatch):
    captured = {}

    class FakeSession:
        def __init__(self):
            self.cookies = {}
            self.get_count = 0

        def get(self, url, **_kwargs):
            self.get_count += 1
            if self.get_count == 1:
                return _FakeResponse(200, url="https://auth.openai.com/log-in", data={})
            return _FakeResponse(
                302,
                url="http://localhost:1455/auth/callback?code=code_1&state=state_1&scope=openid+email",
                data={},
            )

        def post(self, url, **kwargs):
            captured["continue_body"] = json.loads(kwargs["data"])
            return _FakeResponse(
                200,
                url=url,
                data={"page": {"type": "email_otp_verification"}},
            )

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = FakeSession()

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.email = ""
            self.password = ""
            self.send_count = 0
            captured["engine"] = self

        def _set_oai_did_for_session(self, session, device_id):
            session.cookies["oai-did"] = device_id

        def _build_sentinel_header_for_client(self, *_args, **_kwargs):
            return "sentinel"

        def _platform_json_headers(self, **_kwargs):
            return {"referer": "https://auth.openai.com/log-in", "content-type": "application/json"}

        @staticmethod
        def _is_invalid_state_response(_resp):
            return False

        def _send_platform_login_otp(self, _client):
            self.send_count += 1
            return True

        def _wait_platform_login_code(self, _client):
            return "673854"

        def _validate_platform_login_otp(self, _client, _device_id, _code):
            captured["send_count_at_validate"] = self.send_count
            return _FakeResponse(
                200,
                url="https://auth.openai.com/api/accounts/email-otp/validate",
                data={"page": {"type": "consent"}},
            )

        def _follow_platform_redirects_for_callback(self, *_args, **_kwargs):
            return ""

        def _complete_platform_oauth(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(protocol_get_rt, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(protocol_get_rt, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(
        protocol_get_rt,
        "generate_oauth_url",
        lambda **_kwargs: SimpleNamespace(
            auth_url="https://auth.openai.com/oauth/authorize?state=state_1",
            state="state_1",
            code_verifier="verifier_1",
        ),
    )
    monkeypatch.setattr(
        protocol_get_rt,
        "submit_callback_url",
        lambda **_kwargs: json.dumps({"access_token": "at_1", "refresh_token": "rt_1"}),
    )

    result = protocol_get_rt.run_protocol_get_rt(
        email="user@example.com",
        password="Secret123!",
        proxy=None,
        otp_callback=lambda: "673854",
        log_fn=lambda _msg: None,
    )

    assert result["refresh_token"] == "rt_1"
    assert captured["continue_body"] == {"username": {"kind": "email", "value": "user@example.com"}}
    assert "screen_hint" not in captured["continue_body"]
    assert captured["send_count_at_validate"] == 0
    assert captured["engine"].send_count == 0


def test_protocol_get_rt_resends_after_email_otp_401(monkeypatch):
    events = []

    class OtpCallback:
        def __call__(self):
            return "unused"

        def refresh_before_ids(self):
            events.append("refresh")
            return {"message-1"}

    class FakeSession:
        def __init__(self):
            self.cookies = {}
            self.get_count = 0

        def get(self, url, **_kwargs):
            self.get_count += 1
            if self.get_count == 1:
                return _FakeResponse(200, url="https://auth.openai.com/log-in", data={})
            return _FakeResponse(
                302,
                url="http://localhost:1455/auth/callback?code=code_1&state=state_1&scope=openid+email",
                data={},
            )

        def post(self, url, **kwargs):
            captured_body = json.loads(kwargs["data"])
            assert captured_body == {"username": {"kind": "email", "value": "user@example.com"}}
            return _FakeResponse(
                200,
                url=url,
                data={"page": {"type": "email_otp_verification"}},
            )

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = FakeSession()

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.email = ""
            self.password = ""
            self.codes = ["111111", "222222"]

        def _set_oai_did_for_session(self, session, device_id):
            session.cookies["oai-did"] = device_id

        def _build_sentinel_header_for_client(self, *_args, **_kwargs):
            return "sentinel"

        def _platform_json_headers(self, **_kwargs):
            return {"referer": "https://auth.openai.com/log-in", "content-type": "application/json"}

        @staticmethod
        def _is_invalid_state_response(_resp):
            return False

        def _send_platform_login_otp(self, _client):
            events.append("send")
            return True

        def _wait_platform_login_code(self, _client):
            events.append("wait")
            return self.codes.pop(0)

        def _validate_platform_login_otp(self, _client, _device_id, code):
            events.append(f"validate:{code}")
            if code == "111111":
                return _FakeResponse(
                    401,
                    url="https://auth.openai.com/api/accounts/email-otp/validate",
                    data={"error": {"message": "invalid code"}},
                )
            return _FakeResponse(
                200,
                url="https://auth.openai.com/api/accounts/email-otp/validate",
                data={"page": {"type": "consent"}},
            )

        def _follow_platform_redirects_for_callback(self, *_args, **_kwargs):
            return ""

        def _complete_platform_oauth(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(protocol_get_rt, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(protocol_get_rt, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(
        protocol_get_rt,
        "generate_oauth_url",
        lambda **_kwargs: SimpleNamespace(
            auth_url="https://auth.openai.com/oauth/authorize?state=state_1",
            state="state_1",
            code_verifier="verifier_1",
        ),
    )
    monkeypatch.setattr(
        protocol_get_rt,
        "submit_callback_url",
        lambda **_kwargs: json.dumps({"access_token": "at_1", "refresh_token": "rt_1"}),
    )

    result = protocol_get_rt.run_protocol_get_rt(
        email="user@example.com",
        password="Secret123!",
        proxy=None,
        otp_callback=OtpCallback(),
        log_fn=lambda _msg: None,
    )

    assert result["refresh_token"] == "rt_1"
    assert events == ["wait", "validate:111111", "refresh", "send", "wait", "validate:222222"]


def test_protocol_get_rt_login_password_uses_passwordless_otp(monkeypatch):
    captured = {"post_urls": [], "post_bodies": [], "passwordless_referer": ""}
    auth_url = "https://auth.openai.com/oauth/authorize?state=state_1"

    class FakeSession:
        def __init__(self):
            self.cookies = {}
            self.get_count = 0

        def get(self, url, **_kwargs):
            self.get_count += 1
            if self.get_count == 1:
                return _FakeResponse(200, url="https://auth.openai.com/log-in", data={})
            return _FakeResponse(
                302,
                url="http://localhost:1455/auth/callback?code=code_1&state=state_1&scope=openid+email",
                data={},
            )

        def post(self, url, **kwargs):
            captured["post_urls"].append(url)
            captured["post_bodies"].append(kwargs.get("data"))
            if url == "https://auth.openai.com/api/accounts/authorize/continue":
                assert json.loads(kwargs["data"]) == {"username": {"kind": "email", "value": "user@example.com"}}
                return _FakeResponse(
                    200,
                    url=url,
                    data={
                        "continue_url": "https://auth.openai.com/log-in/password",
                        "page": {"type": "login_password", "payload": {"passwordless_disabled": False}},
                    },
                )
            if url == "https://auth.openai.com/api/accounts/passwordless/send-otp":
                captured["passwordless_referer"] = (kwargs.get("headers") or {}).get("referer", "")
                assert kwargs.get("data") == ""
                return _FakeResponse(
                    200,
                    url=url,
                    data={
                        "continue_url": "https://auth.openai.com/email-verification",
                        "page": {
                            "type": "email_otp_verification",
                            "payload": {"email_verification_mode": "passwordless_login"},
                        },
                    },
                )
            return _FakeResponse(500, url=url, data={"unexpected_post": url})

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = FakeSession()

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.email = ""
            self.password = ""
            self.send_count = 0

        def _set_oai_did_for_session(self, session, device_id):
            session.cookies["oai-did"] = device_id

        def _build_sentinel_header_for_client(self, *_args, **_kwargs):
            return "sentinel"

        def _platform_json_headers(self, **_kwargs):
            return {"referer": _kwargs.get("referer") or "", "content-type": "application/json"}

        @staticmethod
        def _is_invalid_state_response(_resp):
            return False

        def _send_platform_login_otp(self, _client):
            self.send_count += 1
            return True

        def _wait_platform_login_code(self, _client):
            return "673854"

        def _validate_platform_login_otp(self, _client, _device_id, code):
            assert code == "673854"
            assert self.send_count == 0
            return _FakeResponse(
                200,
                url="https://auth.openai.com/api/accounts/email-otp/validate",
                data={"page": {"type": "consent"}},
            )

        def _follow_platform_redirects_for_callback(self, *_args, **_kwargs):
            return ""

    monkeypatch.setattr(protocol_get_rt, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(protocol_get_rt, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(
        protocol_get_rt,
        "generate_oauth_url",
        lambda **_kwargs: SimpleNamespace(auth_url=auth_url, state="state_1", code_verifier="verifier_1"),
    )
    monkeypatch.setattr(
        protocol_get_rt,
        "submit_callback_url",
        lambda **_kwargs: json.dumps({"access_token": "at_1", "refresh_token": "rt_1"}),
    )

    logs = []
    result = protocol_get_rt.run_protocol_get_rt(
        email="user@example.com",
        password="Secret123!",
        proxy=None,
        otp_callback=lambda: "673854",
        log_fn=logs.append,
    )

    assert result["refresh_token"] == "rt_1"
    assert "https://auth.openai.com/api/accounts/passwordless/send-otp" in captured["post_urls"]
    assert captured["passwordless_referer"] == "https://auth.openai.com/log-in/password"
    assert any("passwordless/send-otp" in item for item in logs)


def test_protocol_get_rt_password_totp_skips_email_otp(monkeypatch):
    events = []
    auth_url = "https://auth.openai.com/oauth/authorize?state=state_1"
    callback_url = "http://localhost:1455/auth/callback?code=code_1&state=state_1&scope=openid+email"

    class FakeSession:
        def __init__(self):
            self.cookies = {}

        def get(self, url, **_kwargs):
            assert url == auth_url
            return _FakeResponse(200, url="https://auth.openai.com/log-in", data={})

        def post(self, url, **kwargs):
            events.append(("post", url, kwargs.get("data")))
            if url == "https://auth.openai.com/api/accounts/authorize/continue":
                return _FakeResponse(
                    200,
                    url=url,
                    data={"continue_url": "https://auth.openai.com/log-in/password", "page": {"type": "login_password"}},
                )
            if url == "https://auth.openai.com/api/accounts/passwordless/send-otp":
                raise AssertionError("passwordless OTP should not run")
            return _FakeResponse(500, url=url, data={"unexpected_post": url})

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = FakeSession()

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.email = ""
            self.password = ""
            self.totp_secret = ""

        def _set_oai_did_for_session(self, session, device_id):
            session.cookies["oai-did"] = device_id

        def _build_sentinel_header_for_client(self, *_args, **_kwargs):
            return "sentinel"

        def _platform_json_headers(self, **_kwargs):
            return {"referer": _kwargs.get("referer") or "", "content-type": "application/json"}

        @staticmethod
        def _is_invalid_state_response(_resp):
            return False

        def _latest_chatgpt_verify_login_password(self):
            events.append(("password_verify", self.password, self.totp_secret))
            return {
                "continue_url": "https://auth.openai.com/mfa-challenge/factor-1",
                "page": {
                    "type": "mfa_challenge",
                    "payload": {"factor_id": "factor-1", "factors": [{"id": "factor-1", "factor_type": "totp"}]},
                },
            }

        def _latest_chatgpt_complete_mfa_challenge(self, payload):
            events.append(("mfa", payload["page"]["type"], self.totp_secret))
            return {"continue_url": callback_url, "page": {"type": "token_exchange"}}

        def _wait_platform_login_code(self, _client):
            raise AssertionError("email OTP should not be read")

        def _follow_platform_redirects_for_callback(self, *_args, **_kwargs):
            return ""

    monkeypatch.setattr(protocol_get_rt, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(protocol_get_rt, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(
        protocol_get_rt,
        "generate_oauth_url",
        lambda **_kwargs: SimpleNamespace(auth_url=auth_url, state="state_1", code_verifier="verifier_1"),
    )
    monkeypatch.setattr(
        protocol_get_rt,
        "submit_callback_url",
        lambda **_kwargs: json.dumps({"access_token": "at_1", "refresh_token": "rt_1"}),
    )

    result = protocol_get_rt.run_protocol_get_rt(
        email="user@example.com",
        password="Secret123!",
        proxy=None,
        otp_callback=lambda: (_ for _ in ()).throw(AssertionError("email OTP callback should not run")),
        log_fn=lambda _msg: None,
        totp_secret="JBSWY3DPEHPK3PXP",
    )

    assert result["refresh_token"] == "rt_1"
    assert ("password_verify", "Secret123!", "JBSWY3DPEHPK3PXP") in events
    assert ("mfa", "mfa_challenge", "JBSWY3DPEHPK3PXP") in events
    assert all(item[1] != "https://auth.openai.com/api/accounts/passwordless/send-otp" for item in events if item[0] == "post")


def test_protocol_get_rt_password_totp_allows_email_otp_before_mfa(monkeypatch):
    events = []
    auth_url = "https://auth.openai.com/oauth/authorize?state=state_1"
    callback_url = "http://localhost:1455/auth/callback?code=code_1&state=state_1&scope=openid+email"

    class FakeSession:
        def __init__(self):
            self.cookies = {}

        def get(self, url, **_kwargs):
            assert url == auth_url
            return _FakeResponse(200, url="https://auth.openai.com/log-in", data={})

        def post(self, url, **kwargs):
            events.append(("post", url, kwargs.get("data")))
            if url == "https://auth.openai.com/api/accounts/authorize/continue":
                return _FakeResponse(
                    200,
                    url=url,
                    data={"continue_url": "https://auth.openai.com/email-verification", "page": {"type": "email_otp_verification"}},
                )
            if url == "https://auth.openai.com/api/accounts/passwordless/send-otp":
                raise AssertionError("passwordless OTP should not run for password+TOTP")
            return _FakeResponse(500, url=url, data={"unexpected_post": url})

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = FakeSession()

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.email = ""
            self.password = ""
            self.totp_secret = ""

        def _set_oai_did_for_session(self, session, device_id):
            session.cookies["oai-did"] = device_id

        def _build_sentinel_header_for_client(self, *_args, **_kwargs):
            return "sentinel"

        def _platform_json_headers(self, **_kwargs):
            return {"referer": _kwargs.get("referer") or "", "content-type": "application/json"}

        @staticmethod
        def _is_invalid_state_response(_resp):
            return False

        def _latest_chatgpt_verify_login_password(self):
            raise AssertionError("password verify should not run when authorize returns email OTP")

        def _wait_platform_login_code(self, _client):
            events.append("wait_email_otp")
            return "654321"

        def _validate_platform_login_otp(self, _client, _device_id, code):
            events.append(("validate_email_otp", code))
            return _FakeResponse(
                200,
                url="https://auth.openai.com/api/accounts/email-otp/validate",
                data={
                    "continue_url": "https://auth.openai.com/mfa-challenge/factor-1",
                    "page": {
                        "type": "mfa_challenge",
                        "payload": {"factor_id": "factor-1", "factors": [{"id": "factor-1", "factor_type": "totp"}]},
                    },
                },
            )

        def _latest_chatgpt_complete_mfa_challenge(self, payload):
            events.append(("mfa", payload["page"]["type"], self.totp_secret))
            return {"continue_url": callback_url, "page": {"type": "token_exchange"}}

        def _follow_platform_redirects_for_callback(self, *_args, **_kwargs):
            return ""

    monkeypatch.setattr(protocol_get_rt, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(protocol_get_rt, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(
        protocol_get_rt,
        "generate_oauth_url",
        lambda **_kwargs: SimpleNamespace(auth_url=auth_url, state="state_1", code_verifier="verifier_1"),
    )
    monkeypatch.setattr(
        protocol_get_rt,
        "submit_callback_url",
        lambda **_kwargs: json.dumps({"access_token": "at_1", "refresh_token": "rt_1"}),
    )

    logs = []
    result = protocol_get_rt.run_protocol_get_rt(
        email="user@example.com",
        password="Secret123!",
        proxy=None,
        otp_callback=lambda: "654321",
        log_fn=logs.append,
        totp_secret="JBSWY3DPEHPK3PXP",
    )

    assert result["refresh_token"] == "rt_1"
    assert "wait_email_otp" in events
    assert ("validate_email_otp", "654321") in events
    assert ("mfa", "mfa_challenge", "JBSWY3DPEHPK3PXP") in events
    assert any("改用邮箱 OTP 后继续 TOTP" in item for item in logs)


def test_protocol_get_rt_uses_phone_otp_continue_url_for_callback(monkeypatch):
    captured = {"get_urls": [], "post_urls": [], "post_bodies": [], "follow_urls": [], "submit": None}
    auth_url = "https://auth.openai.com/oauth/authorize?state=state_1"
    consent_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
    ws_continue_url = "https://auth.openai.com/api/oauth/oauth2/auth?login_verifier=login_1&state=state_1"
    callback_url = "http://localhost:1455/auth/callback?code=code_1&state=state_1&scope=openid+email"

    class PhoneCallback:
        def __init__(self):
            self.values = ["+628123456789", "654321"]
            self.events = []
            self.code_timeout = None

        def __call__(self):
            return self.values.pop(0)

        def set_code_timeout(self, timeout):
            self.code_timeout = timeout

        def mark_send_succeeded(self):
            self.events.append("send_ok")

        def report_success(self):
            self.events.append("success")

    phone_callback = PhoneCallback()

    class FakeSession:
        def __init__(self):
            self.cookies = {}

        def get(self, url, **_kwargs):
            captured["get_urls"].append(url)
            if url == auth_url:
                return _FakeResponse(200, url="https://auth.openai.com/log-in", data={})
            if url == "https://auth.openai.com/sign-in-with-chatgpt/codex/consent.data?_routes=SIGN_IN_WITH_CHATGPT_CODEX_CONSENT":
                return _FakeResponse(200, url=url, data=None, text='[{"_1":2},"SIGN_IN_WITH_CHATGPT_CODEX_CONSENT"]')
            return _FakeResponse(500, url=url, data={"unexpected_get": url})

        def post(self, url, **kwargs):
            body = json.loads(kwargs["data"])
            captured["post_urls"].append(url)
            captured["post_bodies"].append(body)
            if url == "https://auth.openai.com/api/accounts/authorize/continue":
                return _FakeResponse(200, url=url, data={"page": {"type": "email_otp_verification"}})
            if url == "https://auth.openai.com/api/accounts/add-phone/send":
                assert body == {"phone_number": "+628123456789", "channel": "sms"}
                return _FakeResponse(200, url=url, data={"continue_url": "https://auth.openai.com/phone-verification"})
            if url == "https://auth.openai.com/api/accounts/phone-otp/validate":
                assert body == {"code": "654321"}
                return _FakeResponse(
                    200,
                    url=url,
                    data={
                        "continue_url": consent_url,
                        "page": {"type": "sign_in_with_chatgpt_codex_consent"},
                        "oai-client-auth-session": {"workspaces": [{"id": "ws_1", "kind": "personal"}]},
                    },
                )
            if url == "https://auth.openai.com/api/accounts/workspace/select":
                assert body == {"workspace_id": "ws_1"}
                return _FakeResponse(200, url=url, data={"continue_url": ws_continue_url})
            return _FakeResponse(500, url=url, data={"unexpected_post": url})

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = FakeSession()

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.email = ""
            self.password = ""

        def _set_oai_did_for_session(self, session, device_id):
            session.cookies["oai-did"] = device_id

        def _build_sentinel_header_for_client(self, *_args, **_kwargs):
            return "sentinel"

        def _platform_json_headers(self, **_kwargs):
            return {"referer": _kwargs.get("referer") or "", "content-type": "application/json"}

        @staticmethod
        def _is_invalid_state_response(_resp):
            return False

        def _wait_platform_login_code(self, _client):
            return "123456"

        def _validate_platform_login_otp(self, _client, _device_id, _code):
            return _FakeResponse(
                200,
                url="https://auth.openai.com/api/accounts/email-otp/validate",
                data={"continue_url": "https://auth.openai.com/add-phone", "page": {"type": "add_phone"}},
            )

        def _decode_client_auth_session_cookie(self, _session):
            return {}

        def _follow_platform_redirects_for_callback(self, _session, start_url, **_kwargs):
            captured["follow_urls"].append(start_url)
            if start_url == ws_continue_url:
                return callback_url
            return ""

    monkeypatch.setattr(protocol_get_rt, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(protocol_get_rt, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(
        protocol_get_rt,
        "generate_oauth_url",
        lambda **_kwargs: SimpleNamespace(auth_url=auth_url, state="state_1", code_verifier="verifier_1"),
    )

    def fake_submit_callback_url(**kwargs):
        captured["submit"] = kwargs
        return json.dumps({"access_token": "at_1", "refresh_token": "rt_1"})

    monkeypatch.setattr(protocol_get_rt, "submit_callback_url", fake_submit_callback_url)

    result = protocol_get_rt.run_protocol_get_rt(
        email="user@example.com",
        password="Secret123!",
        proxy=None,
        otp_callback=lambda: "123456",
        phone_callback=phone_callback,
        log_fn=lambda _msg: None,
    )

    assert result["refresh_token"] == "rt_1"
    assert phone_callback.events == ["send_ok", "success"]
    assert phone_callback.code_timeout == 60
    assert captured["get_urls"].count(auth_url) == 1
    assert "https://auth.openai.com/sign-in-with-chatgpt/codex/consent.data?_routes=SIGN_IN_WITH_CHATGPT_CODEX_CONSENT" in captured["get_urls"]
    assert "https://auth.openai.com/api/accounts/workspace/select" in captured["post_urls"]
    assert captured["follow_urls"] == [ws_continue_url]
    assert captured["submit"]["callback_url"] == callback_url
    assert captured["submit"]["client_id"] == protocol_get_rt.CODEX_CLIENT_ID


def test_protocol_get_rt_retries_phone_when_sms_switches_to_whatsapp(monkeypatch):
    captured = {"send_phones": [], "follow_urls": []}
    auth_url = "https://auth.openai.com/oauth/authorize?state=state_1"
    consent_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
    ws_continue_url = "https://auth.openai.com/api/oauth/oauth2/auth?login_verifier=login_1&state=state_1"
    callback_url = "http://localhost:1455/auth/callback?code=code_1&state=state_1&scope=openid+email"
    whatsapp_message = (
        "We couldn't send a text message to this phone number, so we switched to WhatsApp. "
        "Continue to send a verification code on WhatsApp."
    )

    class PhoneCallback:
        def __init__(self):
            self.values = ["+6281111111111", "+6282222222222", "654321"]
            self.events = []

        def __call__(self):
            value = self.values.pop(0)
            self.events.append(("call", value))
            return value

        def mark_send_failed(self, reason):
            self.events.append(("send_failed", reason))

        def mark_send_succeeded(self):
            self.events.append(("send_ok",))

        def report_success(self):
            self.events.append(("success",))

    phone_callback = PhoneCallback()

    class FakeSession:
        def __init__(self):
            self.cookies = {}

        def get(self, url, **_kwargs):
            if url == auth_url:
                return _FakeResponse(200, url="https://auth.openai.com/log-in", data={})
            if url == "https://auth.openai.com/sign-in-with-chatgpt/codex/consent.data?_routes=SIGN_IN_WITH_CHATGPT_CODEX_CONSENT":
                return _FakeResponse(200, url=url, data=None, text='[{"_1":2},"SIGN_IN_WITH_CHATGPT_CODEX_CONSENT"]')
            return _FakeResponse(500, url=url, data={"unexpected_get": url})

        def post(self, url, **kwargs):
            body = json.loads(kwargs["data"])
            if url == "https://auth.openai.com/api/accounts/authorize/continue":
                return _FakeResponse(200, url=url, data={"page": {"type": "email_otp_verification"}})
            if url == "https://auth.openai.com/api/accounts/add-phone/send":
                captured["send_phones"].append(body["phone_number"])
                if len(captured["send_phones"]) == 1:
                    return _FakeResponse(
                        200,
                        url=url,
                        data={"message": whatsapp_message, "page": {"type": "phone_otp_send"}},
                    )
                return _FakeResponse(200, url=url, data={"continue_url": "https://auth.openai.com/phone-verification"})
            if url == "https://auth.openai.com/api/accounts/phone-otp/validate":
                assert body == {"code": "654321"}
                return _FakeResponse(
                    200,
                    url=url,
                    data={
                        "continue_url": consent_url,
                        "page": {"type": "sign_in_with_chatgpt_codex_consent"},
                        "oai-client-auth-session": {"workspaces": [{"id": "ws_1", "kind": "personal"}]},
                    },
                )
            if url == "https://auth.openai.com/api/accounts/workspace/select":
                return _FakeResponse(200, url=url, data={"continue_url": ws_continue_url})
            return _FakeResponse(500, url=url, data={"unexpected_post": url})

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = FakeSession()

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.email = ""
            self.password = ""

        def _set_oai_did_for_session(self, session, device_id):
            session.cookies["oai-did"] = device_id

        def _build_sentinel_header_for_client(self, *_args, **_kwargs):
            return "sentinel"

        def _platform_json_headers(self, **_kwargs):
            return {"referer": _kwargs.get("referer") or "", "content-type": "application/json"}

        @staticmethod
        def _is_invalid_state_response(_resp):
            return False

        def _wait_platform_login_code(self, _client):
            return "123456"

        def _validate_platform_login_otp(self, _client, _device_id, _code):
            return _FakeResponse(
                200,
                url="https://auth.openai.com/api/accounts/email-otp/validate",
                data={"continue_url": "https://auth.openai.com/add-phone", "page": {"type": "add_phone"}},
            )

        def _decode_client_auth_session_cookie(self, _session):
            return {}

        def _follow_platform_redirects_for_callback(self, _session, start_url, **_kwargs):
            captured["follow_urls"].append(start_url)
            if start_url == ws_continue_url:
                return callback_url
            return ""

    monkeypatch.setattr(protocol_get_rt, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(protocol_get_rt, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(
        protocol_get_rt,
        "generate_oauth_url",
        lambda **_kwargs: SimpleNamespace(auth_url=auth_url, state="state_1", code_verifier="verifier_1"),
    )
    monkeypatch.setattr(
        protocol_get_rt,
        "submit_callback_url",
        lambda **_kwargs: json.dumps({"access_token": "at_1", "refresh_token": "rt_1"}),
    )

    result = protocol_get_rt.run_protocol_get_rt(
        email="user@example.com",
        password="Secret123!",
        proxy=None,
        otp_callback=lambda: "123456",
        phone_callback=phone_callback,
        phone_change_limit=2,
        log_fn=lambda _msg: None,
    )

    assert result["refresh_token"] == "rt_1"
    assert captured["send_phones"] == ["+6281111111111", "+6282222222222"]
    assert any(event[0] == "send_failed" and "switched to WhatsApp" in event[1] for event in phone_callback.events)
    assert ("send_ok",) in phone_callback.events
    assert ("success",) in phone_callback.events
    assert captured["follow_urls"] == [ws_continue_url]


def test_protocol_get_rt_keeps_session_when_getting_phone_number_fails(monkeypatch):
    captured = {"continue_calls": 0, "send_phones": [], "follow_urls": []}
    auth_url = "https://auth.openai.com/oauth/authorize?state=state_1"
    consent_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
    ws_continue_url = "https://auth.openai.com/api/oauth/oauth2/auth?login_verifier=login_1&state=state_1"
    callback_url = "http://localhost:1455/auth/callback?code=code_1&state=state_1&scope=openid+email"

    class PhoneCallback:
        def __init__(self):
            self.calls = 0
            self.events = []

        def __call__(self):
            self.calls += 1
            self.events.append(("call", self.calls))
            if self.calls <= 2:
                raise RuntimeError("SMSBower get number failed: NO_NUMBERS")
            if self.calls == 3:
                return "+15550000003"
            return "654321"

        def mark_send_failed(self, reason):
            self.events.append(("send_failed", reason))

        def mark_send_succeeded(self):
            self.events.append(("send_ok",))

        def report_success(self):
            self.events.append(("success",))

    phone_callback = PhoneCallback()

    class FakeSession:
        def __init__(self):
            self.cookies = {}

        def get(self, url, **_kwargs):
            if url == auth_url:
                return _FakeResponse(200, url="https://auth.openai.com/log-in", data={})
            if url == "https://auth.openai.com/sign-in-with-chatgpt/codex/consent.data?_routes=SIGN_IN_WITH_CHATGPT_CODEX_CONSENT":
                return _FakeResponse(200, url=url, data={"ok": True})
            return _FakeResponse(500, url=url, data={"unexpected_get": url})

        def post(self, url, **kwargs):
            body = json.loads(kwargs["data"])
            if url == "https://auth.openai.com/api/accounts/authorize/continue":
                captured["continue_calls"] += 1
                return _FakeResponse(200, url=url, data={"page": {"type": "email_otp_verification"}})
            if url == "https://auth.openai.com/api/accounts/add-phone/send":
                captured["send_phones"].append(body["phone_number"])
                return _FakeResponse(200, url=url, data={"continue_url": "https://auth.openai.com/phone-verification"})
            if url == "https://auth.openai.com/api/accounts/phone-otp/validate":
                assert body == {"code": "654321"}
                return _FakeResponse(
                    200,
                    url=url,
                    data={
                        "continue_url": consent_url,
                        "page": {"type": "sign_in_with_chatgpt_codex_consent"},
                        "oai-client-auth-session": {"workspaces": [{"id": "ws_1"}]},
                    },
                )
            if url == "https://auth.openai.com/api/accounts/workspace/select":
                return _FakeResponse(200, url=url, data={"continue_url": ws_continue_url})
            return _FakeResponse(500, url=url, data={"unexpected_post": url})

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = FakeSession()

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.email = ""
            self.password = ""

        def _set_oai_did_for_session(self, session, device_id):
            session.cookies["oai-did"] = device_id

        def _build_sentinel_header_for_client(self, *_args, **_kwargs):
            return "sentinel"

        def _platform_json_headers(self, **_kwargs):
            return {"referer": _kwargs.get("referer") or "", "content-type": "application/json"}

        @staticmethod
        def _is_invalid_state_response(_resp):
            return False

        def _wait_platform_login_code(self, _client):
            return "123456"

        def _validate_platform_login_otp(self, _client, _device_id, _code):
            return _FakeResponse(
                200,
                url="https://auth.openai.com/api/accounts/email-otp/validate",
                data={"continue_url": "https://auth.openai.com/add-phone", "page": {"type": "add_phone"}},
            )

        def _decode_client_auth_session_cookie(self, _session):
            return {}

        def _follow_platform_redirects_for_callback(self, _session, start_url, **_kwargs):
            captured["follow_urls"].append(start_url)
            if start_url == ws_continue_url:
                return callback_url
            return ""

    monkeypatch.setattr(protocol_get_rt, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(protocol_get_rt, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(
        protocol_get_rt,
        "generate_oauth_url",
        lambda **_kwargs: SimpleNamespace(auth_url=auth_url, state="state_1", code_verifier="verifier_1"),
    )
    monkeypatch.setattr(
        protocol_get_rt,
        "submit_callback_url",
        lambda **_kwargs: json.dumps({"access_token": "at_1", "refresh_token": "rt_1"}),
    )

    result = protocol_get_rt.run_protocol_get_rt(
        email="user@example.com",
        password="Secret123!",
        proxy=None,
        otp_callback=lambda: "123456",
        phone_callback=phone_callback,
        phone_change_limit=4,
        log_fn=lambda _msg: None,
    )

    assert result["refresh_token"] == "rt_1"
    assert captured["continue_calls"] == 1
    assert captured["send_phones"] == ["+15550000003"]
    assert [event[0] for event in phone_callback.events].count("send_failed") == 2
    assert ("send_ok",) in phone_callback.events
    assert ("success",) in phone_callback.events


def test_protocol_get_rt_restarts_login_when_add_phone_session_is_invalid(monkeypatch):
    captured = {"send_phones": []}
    auth_url = "https://auth.openai.com/oauth/authorize?state=state_1"

    class PhoneCallback:
        def __init__(self):
            self.values = ["+15550000001", "+15550000002"]
            self.events = []

        def __call__(self):
            value = self.values.pop(0)
            self.events.append(("call", value))
            return value

        def mark_send_failed(self, reason):
            self.events.append(("send_failed", reason))

    phone_callback = PhoneCallback()

    class FakeSession:
        def __init__(self):
            self.cookies = {}

        def get(self, url, **_kwargs):
            assert url == auth_url
            return _FakeResponse(200, url="https://auth.openai.com/log-in", data={})

        def post(self, url, **kwargs):
            body = json.loads(kwargs["data"])
            if url == "https://auth.openai.com/api/accounts/authorize/continue":
                return _FakeResponse(200, url=url, data={"page": {"type": "email_otp_verification"}})
            if url == "https://auth.openai.com/api/accounts/add-phone/send":
                captured["send_phones"].append(body["phone_number"])
                return _FakeResponse(
                    400,
                    url=url,
                    data={
                        "error": {
                            "message": "Your sign-in session is no longer valid. Please start over to continue.",
                            "type": "invalid_request_error",
                            "code": "invalid_state",
                            "redirect_uri": "https://auth.openai.com/log-in",
                        }
                    },
                )
            return _FakeResponse(500, url=url, data={"unexpected_post": url})

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = FakeSession()

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.email = ""
            self.password = ""

        def _set_oai_did_for_session(self, session, device_id):
            session.cookies["oai-did"] = device_id

        def _build_sentinel_header_for_client(self, *_args, **_kwargs):
            return "sentinel"

        def _platform_json_headers(self, **_kwargs):
            return {"referer": _kwargs.get("referer") or "", "content-type": "application/json"}

        @staticmethod
        def _is_invalid_state_response(_resp):
            return False

        def _wait_platform_login_code(self, _client):
            return "123456"

        def _validate_platform_login_otp(self, _client, _device_id, _code):
            return _FakeResponse(
                200,
                url="https://auth.openai.com/api/accounts/email-otp/validate",
                data={"continue_url": "https://auth.openai.com/add-phone", "page": {"type": "add_phone"}},
            )

    monkeypatch.setattr(protocol_get_rt, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(protocol_get_rt, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(
        protocol_get_rt,
        "generate_oauth_url",
        lambda **_kwargs: SimpleNamespace(auth_url=auth_url, state="state_1", code_verifier="verifier_1"),
    )

    with pytest.raises(RuntimeError) as excinfo:
        protocol_get_rt.run_protocol_get_rt(
            email="user@example.com",
            password="Secret123!",
            proxy=None,
            otp_callback=lambda: "123456",
            phone_callback=phone_callback,
            phone_change_limit=3,
            log_fn=lambda _msg: None,
        )

    assert "GET_RT_LOGIN_RESTART_REQUIRED" in str(excinfo.value)
    assert captured["send_phones"] == ["+15550000001"]
    assert len(phone_callback.values) == 1
    assert any(event[0] == "send_failed" and "invalid_state" in event[1] for event in phone_callback.events)


def test_protocol_get_rt_passwordless_too_many_tries_is_classified(monkeypatch):
    auth_url = "https://auth.openai.com/oauth/authorize?state=state_1"

    class FakeSession:
        def __init__(self):
            self.cookies = {}

        def get(self, url, **_kwargs):
            assert url == auth_url
            return _FakeResponse(200, url="https://auth.openai.com/log-in", data={})

        def post(self, url, **_kwargs):
            if url == "https://auth.openai.com/api/accounts/authorize/continue":
                return _FakeResponse(
                    200,
                    url=url,
                    data={"page": {"type": "login_password"}},
                )
            if url == "https://auth.openai.com/api/accounts/passwordless/send-otp":
                return _FakeResponse(
                    429,
                    url=url,
                    data={"error": {"message": "Too many tries. Please wait a few minutes before trying again."}},
                )
            return _FakeResponse(500, url=url, data={"unexpected_post": url})

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = FakeSession()

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.email = ""
            self.password = ""

        def _set_oai_did_for_session(self, session, device_id):
            session.cookies["oai-did"] = device_id

        def _build_sentinel_header_for_client(self, *_args, **_kwargs):
            return "sentinel"

        def _platform_json_headers(self, **_kwargs):
            return {"referer": _kwargs.get("referer") or "", "content-type": "application/json"}

        @staticmethod
        def _is_invalid_state_response(_resp):
            return False

    monkeypatch.setattr(protocol_get_rt, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(protocol_get_rt, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(
        protocol_get_rt,
        "generate_oauth_url",
        lambda **_kwargs: SimpleNamespace(auth_url=auth_url, state="state_1", code_verifier="verifier_1"),
    )

    with pytest.raises(RuntimeError) as excinfo:
        protocol_get_rt.run_protocol_get_rt(
            email="user@example.com",
            password="Secret123!",
            proxy=None,
            otp_callback=lambda: "123456",
            log_fn=lambda _msg: None,
        )

    assert "GET_RT_EMAIL_LOGIN_COOLDOWN" in str(excinfo.value)
    assert "passwordless/send-otp" in str(excinfo.value)
