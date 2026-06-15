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

        def __call__(self):
            return self.values.pop(0)

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
    assert captured["get_urls"].count(auth_url) == 1
    assert "https://auth.openai.com/sign-in-with-chatgpt/codex/consent.data?_routes=SIGN_IN_WITH_CHATGPT_CODEX_CONSENT" in captured["get_urls"]
    assert "https://auth.openai.com/api/accounts/workspace/select" in captured["post_urls"]
    assert captured["follow_urls"] == [ws_continue_url]
    assert captured["submit"]["callback_url"] == callback_url
    assert captured["submit"]["client_id"] == protocol_get_rt.CODEX_CLIENT_ID
