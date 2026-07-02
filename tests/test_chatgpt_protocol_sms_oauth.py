from __future__ import annotations

import json
from types import SimpleNamespace

from platforms.chatgpt import protocol_sms_oauth


class _FakeResponse:
    def __init__(self, status_code=200, *, url="", data=None, text=""):
        self.status_code = status_code
        self.url = url
        self._data = data
        self.text = text
        self.headers = {}

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


def test_protocol_sms_oauth_runs_phone_first_register_sequence(monkeypatch):
    captured = {"gets": [], "get_kwargs": [], "posts": [], "post_kwargs": []}

    class PhoneCallback:
        def __init__(self):
            self.values = ["+966572217637", "567149"]
            self.events = []

        def __call__(self):
            value = self.values.pop(0)
            self.events.append(("call", value))
            return value

        def mark_send_succeeded(self):
            self.events.append(("send_ok",))

        def report_success(self):
            self.events.append(("success",))

    phone_callback = PhoneCallback()

    class FakeSession:
        def __init__(self):
            self.cookies = {
                "__Secure-next-auth.session-token": "sess_1",
                "_account": "acct_cookie",
            }

        def get(self, url, **_kwargs):
            captured["gets"].append(url)
            captured["get_kwargs"].append((url, dict(_kwargs)))
            if url.endswith("/api/auth/csrf"):
                return _FakeResponse(200, url=url, data={"csrfToken": "csrf_1"})
            if url.startswith("https://auth.openai.com/api/accounts/authorize"):
                return _FakeResponse(200, url="https://auth.openai.com/create-account/password", data={})
            if url.startswith("https://chatgpt.com/api/auth/callback/openai"):
                return _FakeResponse(200, url=url, data={})
            if url.endswith("/api/auth/session"):
                return _FakeResponse(
                    200,
                    url=url,
                    data={
                        "accessToken": "session_at_1",
                        "user": {"email": None},
                        "expires": "2026-06-20T00:00:00.000Z",
                    },
                )
            if url.endswith("/api/accounts/phone-otp/send"):
                resp = _FakeResponse(302, url=url, data={})
                resp.headers["location"] = "https://auth.openai.com/contact-verification"
                return resp
            return _FakeResponse(200, url=url, data={})

        def post(self, url, **kwargs):
            body_text = kwargs.get("data") or "{}"
            if isinstance(body_text, bytes):
                body_text = body_text.decode("utf-8")
            if str(kwargs.get("headers", {}).get("content-type") or "").startswith("application/x-www-form-urlencoded"):
                body = {"_raw": body_text}
            else:
                body = json.loads(body_text)
            captured["posts"].append((url, body))
            captured["post_kwargs"].append((url, dict(kwargs)))
            if url.startswith("https://chatgpt.com/api/auth/signin/openai"):
                return _FakeResponse(
                    200,
                    url=url,
                    data={"url": "https://auth.openai.com/api/accounts/authorize?state=state_1"},
                )
            if url.endswith("/api/accounts/authorize/continue"):
                return _FakeResponse(
                    200,
                    url=url,
                    data={"continue_url": "https://auth.openai.com/create-account/password", "page": {"type": "create_account_password"}},
                )
            if url.endswith("/api/accounts/user/register"):
                return _FakeResponse(
                    200,
                    url=url,
                    data={"continue_url": "https://auth.openai.com/api/accounts/phone-otp/send", "page": {"type": "phone_otp_send"}},
                )
            if url.endswith("/api/accounts/phone-otp/validate"):
                return _FakeResponse(
                    200,
                    url=url,
                    data={"continue_url": "https://auth.openai.com/about-you", "page": {"type": "about_you"}},
                )
            if url.endswith("/api/accounts/create_account"):
                return _FakeResponse(
                    200,
                    url=url,
                    data={
                        "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=code_1&state=state_1",
                        "page": {"type": "external_url"},
                    },
                )
            return _FakeResponse(500, url=url, data={"unexpected": url})

        def close(self):
            return None

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = FakeSession()
            self.default_headers = {}

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.http_client = None
            self.session = None
            self.email = ""
            self.password = ""
            self._create_account_continue_url = ""

        def _set_oai_did_for_session(self, _session, _device_id):
            return None

        def _platform_reference_authorize(self, _client, _device_id):
            return self._build_platform_oauth_start("", _device_id)

        def _build_platform_oauth_start(self, _email, _device_id):
            return SimpleNamespace(auth_url="https://auth.openai.com/api/accounts/authorize?state=state_1")

        def _platform_nav_headers(self, **_kwargs):
            return {"referer": _kwargs.get("referer") or ""}

        def _platform_json_headers(self, **_kwargs):
            return {"referer": _kwargs.get("referer") or "", "content-type": "application/json"}

        def _build_sentinel_header_for_client(self, *_args, **_kwargs):
            return "sentinel"

        def _complete_platform_oauth(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(protocol_sms_oauth, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(protocol_sms_oauth, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(protocol_sms_oauth, "_extract_chatgpt_account_id", lambda _token: "acct_1")

    worker = protocol_sms_oauth.ChatGPTProtocolSmsOAuthWorker(
        phone_callback=phone_callback,
        proxy_url=None,
        log_fn=lambda _message: None,
        phone_change_limit=3,
    )
    result = worker.run(email="user@example.com", password="Secret123!")

    assert result.success is True
    assert result.access_token == "session_at_1"
    assert result.refresh_token == ""
    assert result.session_token == "sess_1"
    assert result.metadata["refresh_token_source"] == ""
    assert result.metadata["session"] == {
        "accessToken": "session_at_1",
        "user": {"email": None},
        "expires": "2026-06-20T00:00:00.000Z",
    }
    assert any(url.startswith("https://chatgpt.com/api/auth/callback/openai") for url in captured["gets"])
    assert any(url.endswith("/api/auth/session") for url in captured["gets"])
    phone_send_get = next(item for item in captured["get_kwargs"] if item[0].endswith("/api/accounts/phone-otp/send"))
    assert phone_send_get[1]["allow_redirects"] is False
    phone_validate_post = next(item for item in captured["post_kwargs"] if item[0].endswith("/api/accounts/phone-otp/validate"))
    assert phone_validate_post[1]["headers"]["referer"] == "https://auth.openai.com/contact-verification"
    continue_posts = [item for item in captured["posts"] if item[0].endswith("/api/accounts/authorize/continue")]
    assert [item[1]["username"]["value"] for item in continue_posts] == ["+966572217637"]
    assert any(item[0].endswith("/api/accounts/user/register") for item in captured["posts"])
    assert any(item[0].endswith("/api/accounts/phone-otp/validate") for item in captured["posts"])
    assert any(item[0].endswith("/api/accounts/create_account") for item in captured["posts"])
    assert phone_callback.events == [("call", "+966572217637"), ("send_ok",), ("call", "567149"), ("success",)]


def test_protocol_sms_oauth_skips_phone_that_redirects_to_login_password(monkeypatch):
    captured = {"gets": [], "posts": []}

    class PhoneCallback:
        def __init__(self):
            self.values = ["+15550000001", "+15550000002", "123456"]
            self.failed = []

        def __call__(self):
            return self.values.pop(0)

        def mark_send_failed(self, reason):
            self.failed.append(str(reason))

        def mark_send_succeeded(self):
            return None

        def report_success(self):
            return None

    phone_callback = PhoneCallback()

    class FakeSession:
        def __init__(self):
            self.cookies = {"__Secure-next-auth.session-token": "sess_2"}
            self.authorize_gets = 0
            self.continue_posts = 0

        def get(self, url, **_kwargs):
            captured["gets"].append(url)
            if url.endswith("/api/auth/csrf"):
                return _FakeResponse(200, url=url, data={"csrfToken": "csrf_1"})
            if url.startswith("https://auth.openai.com/api/accounts/authorize"):
                self.authorize_gets += 1
                return _FakeResponse(200, url="https://auth.openai.com/create-account/password", data={})
            if url.endswith("/api/accounts/phone-otp/send"):
                return _FakeResponse(302, url=url, data={})
            if url.startswith("https://chatgpt.com/api/auth/callback/openai"):
                return _FakeResponse(200, url=url, data={})
            if url.endswith("/api/auth/session"):
                return _FakeResponse(
                    200,
                    url=url,
                    data={"accessToken": "session_at_2", "user": {}, "expires": "2026-06-20T00:00:00.000Z"},
                )
            return _FakeResponse(200, url=url, data={})

        def post(self, url, **kwargs):
            body_text = kwargs.get("data") or "{}"
            if isinstance(body_text, bytes):
                body_text = body_text.decode("utf-8")
            if str(kwargs.get("headers", {}).get("content-type") or "").startswith("application/x-www-form-urlencoded"):
                body = {"_raw": body_text}
            else:
                body = json.loads(body_text)
            captured["posts"].append((url, body))
            if url.startswith("https://chatgpt.com/api/auth/signin/openai"):
                return _FakeResponse(
                    200,
                    url=url,
                    data={"url": "https://auth.openai.com/api/accounts/authorize?state=state_2"},
                )
            if url.endswith("/api/accounts/authorize/continue"):
                self.continue_posts += 1
                if self.continue_posts == 1:
                    # \u7b2c\u4e00\u4e2a\u53f7\u7801\u5df2\u5b58\u5728\u8d26\u53f7\uff0c\u8fd4\u56de login_password\u3002
                    return _FakeResponse(
                        200,
                        url=url,
                        data={"continue_url": "https://auth.openai.com/log-in/password", "page": {"type": "login_password"}},
                    )
                return _FakeResponse(
                    200,
                    url=url,
                    data={"continue_url": "https://auth.openai.com/create-account/password", "page": {"type": "create_account_password"}},
                )
            if url.endswith("/api/accounts/user/register"):
                return _FakeResponse(200, url=url, data={"page": {"type": "phone_otp_send"}})
            if url.endswith("/api/accounts/phone-otp/validate"):
                return _FakeResponse(200, url=url, data={"page": {"type": "about_you"}})
            if url.endswith("/api/accounts/create_account"):
                return _FakeResponse(
                    200,
                    url=url,
                    data={"continue_url": "https://chatgpt.com/api/auth/callback/openai?code=code_2&state=state_2"},
                )
            return _FakeResponse(500, url=url, data={"unexpected": url})

        def close(self):
            return None

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = FakeSession()
            self.default_headers = {}

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.http_client = None
            self.session = None
            self.email = ""
            self.password = ""
            self._create_account_continue_url = ""

        def _set_oai_did_for_session(self, _session, _device_id):
            return None

        def _platform_reference_authorize(self, _client, _device_id):
            return self._build_platform_oauth_start("", _device_id)

        def _platform_nav_headers(self, **_kwargs):
            return {"referer": _kwargs.get("referer") or ""}

        def _platform_json_headers(self, **_kwargs):
            return {"referer": _kwargs.get("referer") or "", "content-type": "application/json"}

        def _build_sentinel_header_for_client(self, *_args, **_kwargs):
            return "sentinel"

        def _build_platform_oauth_start(self, _email, _device_id):
            return SimpleNamespace(auth_url="https://auth.openai.com/api/accounts/authorize?state=state_2")

        def _complete_platform_oauth(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(protocol_sms_oauth, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(protocol_sms_oauth, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(protocol_sms_oauth, "_extract_chatgpt_account_id", lambda _token: "acct_2")

    worker = protocol_sms_oauth.ChatGPTProtocolSmsOAuthWorker(
        phone_callback=phone_callback,
        proxy_url=None,
        log_fn=lambda _message: None,
        phone_change_limit=2,
    )

    result = worker.run(email="user@example.com", password="Secret123!")

    assert result.success is True
    assert result.access_token == "session_at_2"
    # \u7b2c\u4e00\u4e2a\u53f7\u7801\u5728 authorize/continue \u88ab\u8bc6\u522b\u4e3a login_password\uff08\u5df2\u5b58\u5728\u8d26\u53f7\uff09\u5e76\u8df3\u8fc7\u3002
    assert len(phone_callback.failed) == 1
    assert "login_password" in phone_callback.failed[0]
    continue_posts = [item for item in captured["posts"] if item[0].endswith("/api/accounts/authorize/continue")]
    assert [item[1]["username"]["value"] for item in continue_posts] == ["+15550000001", "+15550000002"]


def test_protocol_sms_oauth_retries_phone_when_sms_switches_to_whatsapp(monkeypatch):
    captured = {"send_gets": 0, "posts": []}
    whatsapp_message = (
        "We couldn't send a text message to this phone number, so we switched to WhatsApp. "
        "Continue to send a verification code on WhatsApp."
    )

    class PhoneCallback:
        def __init__(self):
            self.values = ["+15550000001", "+15550000002", "123456"]
            self.events = []

        def __call__(self):
            value = self.values.pop(0)
            self.events.append(("call", value))
            return value

        def mark_send_failed(self, reason):
            self.events.append(("send_failed", str(reason)))

        def mark_send_succeeded(self):
            self.events.append(("send_ok",))

        def report_success(self):
            self.events.append(("success",))

    phone_callback = PhoneCallback()

    class FakeSession:
        def __init__(self):
            self.cookies = {"__Secure-next-auth.session-token": "sess_3"}

        def get(self, url, **kwargs):
            if url.endswith("/api/auth/csrf"):
                return _FakeResponse(200, url=url, data={"csrfToken": "csrf_1"})
            if url.endswith("/api/accounts/phone-otp/send"):
                assert kwargs["allow_redirects"] is False
                captured["send_gets"] += 1
                if captured["send_gets"] == 1:
                    return _FakeResponse(200, url=url, data={"error": {"message": whatsapp_message}})
                resp = _FakeResponse(302, url=url, data={})
                resp.headers["location"] = "https://auth.openai.com/contact-verification"
                return resp
            if url.startswith("https://chatgpt.com/api/auth/callback/openai"):
                return _FakeResponse(200, url=url, data={})
            if url.endswith("/api/auth/session"):
                return _FakeResponse(
                    200,
                    url=url,
                    data={"accessToken": "session_at_3", "user": {}, "expires": "2026-06-20T00:00:00.000Z"},
                )
            return _FakeResponse(200, url=url, data={})

        def post(self, url, **kwargs):
            body_text = kwargs.get("data") or "{}"
            if isinstance(body_text, bytes):
                body_text = body_text.decode("utf-8")
            if str(kwargs.get("headers", {}).get("content-type") or "").startswith("application/x-www-form-urlencoded"):
                body = {"_raw": body_text}
            else:
                body = json.loads(body_text)
            captured["posts"].append((url, body))
            if url.startswith("https://chatgpt.com/api/auth/signin/openai"):
                return _FakeResponse(
                    200,
                    url=url,
                    data={"url": "https://auth.openai.com/api/accounts/authorize?state=state_3"},
                )
            if url.endswith("/api/accounts/authorize/continue"):
                return _FakeResponse(
                    200,
                    url=url,
                    data={"continue_url": "https://auth.openai.com/create-account/password", "page": {"type": "create_account_password"}},
                )
            if url.endswith("/api/accounts/user/register"):
                return _FakeResponse(200, url=url, data={"page": {"type": "phone_otp_send"}})
            if url.endswith("/api/accounts/phone-otp/validate"):
                return _FakeResponse(200, url=url, data={"page": {"type": "about_you"}})
            if url.endswith("/api/accounts/create_account"):
                return _FakeResponse(
                    200,
                    url=url,
                    data={"continue_url": "https://chatgpt.com/api/auth/callback/openai?code=code_3&state=state_3"},
                )
            return _FakeResponse(500, url=url, data={"unexpected": url})

        def close(self):
            return None

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = FakeSession()
            self.default_headers = {}

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.http_client = None
            self.session = None
            self.email = ""
            self.password = ""
            self._create_account_continue_url = ""

        def _set_oai_did_for_session(self, _session, _device_id):
            return None

        def _platform_reference_authorize(self, _client, _device_id):
            return self._build_platform_oauth_start("", _device_id)

        def _platform_nav_headers(self, **_kwargs):
            return {"referer": _kwargs.get("referer") or ""}

        def _platform_json_headers(self, **_kwargs):
            return {"referer": _kwargs.get("referer") or "", "content-type": "application/json"}

        def _build_sentinel_header_for_client(self, *_args, **_kwargs):
            return "sentinel"

        def _build_platform_oauth_start(self, _email, _device_id):
            return SimpleNamespace(auth_url="https://auth.openai.com/api/accounts/authorize?state=state_3")

        def _complete_platform_oauth(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(protocol_sms_oauth, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(protocol_sms_oauth, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(protocol_sms_oauth, "_extract_chatgpt_account_id", lambda _token: "acct_3")

    worker = protocol_sms_oauth.ChatGPTProtocolSmsOAuthWorker(
        phone_callback=phone_callback,
        proxy_url=None,
        log_fn=lambda _message: None,
        phone_change_limit=2,
    )

    result = worker.run(email="user@example.com", password="Secret123!")

    assert result.success is True
    assert result.access_token == "session_at_3"
    assert captured["send_gets"] == 2
    authorize_continue = [item for item in captured["posts"] if item[0].endswith("/api/accounts/authorize/continue")]
    assert [item[1]["username"]["value"] for item in authorize_continue] == ["+15550000001", "+15550000002"]
    assert phone_callback.events[0] == ("call", "+15550000001")
    assert phone_callback.events[1][0] == "send_failed"
    assert "switched to WhatsApp" in phone_callback.events[1][1]
    assert phone_callback.events[-3:] == [("send_ok",), ("call", "123456"), ("success",)]
