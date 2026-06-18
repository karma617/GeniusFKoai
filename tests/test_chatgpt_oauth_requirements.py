from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.base_platform import RegisterConfig
from platforms.chatgpt import browser_register as browser_register_module
from platforms.chatgpt.plugin import (
    ChatGPTPlatform,
    _assert_complete_oauth_callback,
    _generate_chatgpt_registration_password,
)


class _SessionApiResponse:
    status = 200

    def text(self):
        return (
            '{"accessToken":"at_123","user":{"email":"user@example.com"},'
            '"expires":"2026-05-20T12:00:00Z"}'
        )


class _UnreadableSessionApiResponse:
    status = 200

    def text(self):
        raise RuntimeError("Response body is unavailable for redirect responses")


def test_assert_complete_oauth_callback_accepts_complete_payload():
    _assert_complete_oauth_callback({
        "account_id": "acct_123",
        "access_token": "at_123",
        "refresh_token": "rt_123",
        "id_token": "id_123",
    })


def test_assert_complete_oauth_callback_rejects_partial_payload():
    with pytest.raises(RuntimeError, match="OAuth callback"):
        _assert_complete_oauth_callback({
            "account_id": "acct_123",
            "access_token": "",
            "refresh_token": "",
            "id_token": "",
        })


def test_generate_chatgpt_registration_password_meets_openai_strength_requirements():
    for _ in range(8):
        password = _generate_chatgpt_registration_password()
        assert len(password) >= 12
        assert any(ch.islower() for ch in password)
        assert any(ch.isupper() for ch in password)
        assert any(ch.isdigit() for ch in password)
        assert any(ch in ",._!@#" for ch in password)


def test_add_phone_retryable_rejection_text_matches_english_and_chinese():
    matcher = browser_register_module._is_retryable_phone_rejection_text

    assert matcher("This phone number is not supported. Please try another phone.")
    assert matcher("We couldn't send a text message to this phone number, so we switched to WhatsApp.")
    assert matcher("You've made too many phone verification requests. Please try again later.")
    assert matcher("SMSPool 购号失败 (service=671 country=9 max_price=0.13)")
    assert matcher("get_rt: smspool failed to get phone")
    assert matcher("不支持虚拟手机号，请更换一个号码")
    assert not matcher("Enter the 6-digit security code we sent to your phone.")


def test_add_phone_attempt_limit_uses_codex_pool_size():
    class FakeCallback:
        provider_key = "codex_sms_pool"
        config = {
            "codex_sms_pool_text": "\n".join(
                [
                    "+15550000001|https://sms.example/1",
                    "+15550000002|https://sms.example/2",
                ]
            )
        }

    assert browser_register_module._resolve_add_phone_attempt_limit(FakeCallback(), 40) == 2


def test_codex_oauth_returns_retryable_error_after_phone_rejection(monkeypatch):
    logs = []

    class FakePage:
        url = "https://auth.openai.com/add-phone"

        def evaluate(self, _script):
            return "Mozilla/5.0"

    class FakePhoneCallback:
        completed = False

    monkeypatch.setattr(browser_register_module, "_goto_with_retry", lambda page, url, **_kwargs: setattr(page, "url", url))
    states = [
        {"page_type": "add_phone", "continue_url": "", "current_url": "https://auth.openai.com/add-phone"},
    ]
    monkeypatch.setattr(browser_register_module, "_derive_oauth_state_from_page", lambda _page: states[0])

    def fake_handle_add_phone(*_args, **_kwargs):
        raise RuntimeError(
            "PHONE_REJECTED_RETRYABLE: We couldn't send a text message to this phone number, "
            "so we switched to WhatsApp. Continue to send a verification code on WhatsApp."
        )

    monkeypatch.setattr(browser_register_module, "_handle_add_phone_challenge", fake_handle_add_phone)

    result = browser_register_module._do_codex_oauth(
        FakePage(),
        {},
        "user@example.com",
        "Secret123!",
        otp_callback=lambda: "123456",
        phone_callback=FakePhoneCallback(),
        proxy=None,
        log=logs.append,
        max_phone_attempts=1,
        oauth_start=SimpleNamespace(
            auth_url="https://auth.openai.com/oauth/authorize?state=state_1",
            state="state_1",
            code_verifier="verifier_1",
        ),
    )

    assert result["error_type"] == "phone_rejected_retryable"
    assert "switched to WhatsApp" in result["error"]
    assert any("短信验证失败" in message for message in logs)


def test_parse_phone_country_and_local_strips_non_digits():
    assert browser_register_module._parse_phone_country_and_local("+81 831 908 43766") == (
        "81",
        "83190843766",
        "Japan",
    )
    assert browser_register_module._parse_phone_country_and_local("+856-64-890-950") == (
        "856",
        "64890950",
        "Laos",
    )


def test_phone_input_matches_expected_requires_exact_local_or_country_plus_local(monkeypatch):
    class FakeLocator:
        def __init__(self, value):
            self._value = value

        def input_value(self):
            return self._value

        @property
        def first(self):
            return self

    class FakePage:
        def __init__(self, value):
            self._value = value

        def locator(self, selector):
            return FakeLocator(self._value)

    assert browser_register_module._phone_input_matches_expected(FakePage("64890950"), "input", "856", "64890950") is True
    assert browser_register_module._phone_input_matches_expected(FakePage("85664890950"), "input", "856", "64890950") is True
    assert browser_register_module._phone_input_matches_expected(FakePage("85685664890950"), "input", "856", "64890950") is False


def test_phone_input_helpers_accept_locators_without_first():
    class FakeLocator:
        def __init__(self, value):
            self._value = value

        def wait_for(self, **_kwargs):
            return None

        def input_value(self):
            return self._value

        def click(self, **_kwargs):
            return None

        def fill(self, value):
            self._value = value

        def type(self, value, delay=0):
            self._value = value

    class FakePage:
        def __init__(self, value):
            self._value = value

        def locator(self, selector):
            return FakeLocator(self._value)

    assert browser_register_module._fill_input_like_user(FakePage("64890950"), "input", "64890950") is True
    assert browser_register_module._phone_input_matches_expected(FakePage("64890950"), "input", "856", "64890950") is True


def test_add_phone_default_attempt_limit_is_two_countries_times_ten():
    class FakeCallback:
        provider_key = "smsbower_api"

    default_limit = (
        browser_register_module.PHONE_ATTEMPTS_PER_COUNTRY
        * browser_register_module.PHONE_MAX_COUNTRIES
    )

    assert browser_register_module.PHONE_ATTEMPTS_PER_COUNTRY == 10
    assert browser_register_module.PHONE_MAX_COUNTRIES == 2
    assert default_limit == 20
    assert browser_register_module._resolve_add_phone_attempt_limit(FakeCallback(), default_limit) == 20


def test_playwright_pageerror_location_patch_is_idempotent(tmp_path):
    bundle = tmp_path / "coreBundle.js"
    bundle.write_text(
        "url: pageError.location.url,\n"
        "line: pageError.location.lineNumber,\n"
        "column: pageError.location.columnNumber\n",
        encoding="utf-8",
    )

    assert browser_register_module._patch_playwright_firefox_pageerror_location_bug(bundle_path=bundle)
    patched = bundle.read_text(encoding="utf-8")
    assert 'url: pageError.location?.url || "",' in patched
    assert browser_register_module._patch_playwright_firefox_pageerror_location_bug(bundle_path=bundle) is False


def test_auth_timeout_retry_text_detects_openai_retry_page():
    text = "Oops, an error occurred! Operation timed out Try again Terms of Use"

    assert browser_register_module._is_auth_timeout_retry_text(text) is True


def test_auth_timeout_retry_text_ignores_plain_try_again_copy():
    assert browser_register_module._is_auth_timeout_retry_text("Try again later") is False


def test_chatgpt_platform_preserves_user_supplied_password():
    platform = object.__new__(ChatGPTPlatform)
    assert platform._prepare_registration_password("Secret123!") == "Secret123!"


def test_protocol_mailbox_mapper_rejects_partial_oauth_result():
    platform = object.__new__(ChatGPTPlatform)
    platform.mailbox = None
    platform.config = RegisterConfig()
    adapter = ChatGPTPlatform.build_protocol_mailbox_adapter(platform)
    ctx = SimpleNamespace(password="Secret123!", proxy=None, log=lambda message: None)
    result = SimpleNamespace(
        email="user@example.com",
        password="Secret123!",
        account_id="acct_123",
        access_token="",
        refresh_token="",
        id_token="",
        session_token="sess_123",
        workspace_id="",
    )

    with pytest.raises(RuntimeError, match="OAuth callback"):
        adapter.result_mapper(ctx, result)


def test_protocol_mailbox_mapper_preserves_registration_refresh_token_without_formal_rt():
    platform = object.__new__(ChatGPTPlatform)
    platform.mailbox = None
    platform.config = RegisterConfig()
    adapter = ChatGPTPlatform.build_protocol_mailbox_adapter(platform)
    ctx = SimpleNamespace(password="Secret123!", proxy=None, log=lambda message: None)
    result = SimpleNamespace(
        email="user@example.com",
        password="Secret123!",
        account_id="acct_123",
        access_token="access-token",
        refresh_token="",
        id_token="id-token",
        session_token="sess_123",
        workspace_id="ws_123",
        metadata={
            "registration_refresh_token": "registration-only-refresh",
            "cookies": "session=abc",
        },
    )

    mapped = adapter.result_mapper(ctx, result)

    assert mapped.extra["refresh_token"] == ""
    assert mapped.extra["registration_refresh_token"] == "registration-only-refresh"
    assert mapped.extra["registration_refresh_token_usable"] is False
    assert mapped.extra["refresh_token_source"] == ""


def test_browser_registration_mapper_accepts_completed_registration_without_codex_tokens():
    platform = object.__new__(ChatGPTPlatform)

    mapped = platform._map_chatgpt_result({
        "email": "user@example.com",
        "password": "Secret123!",
        "account_id": "",
        "access_token": "",
        "refresh_token": "",
        "id_token": "",
        "session_token": "",
        "workspace_id": "",
        "cookies": "{\"login_session\":\"yes\"}",
        "profile": {},
    })

    assert mapped.email == "user@example.com"
    assert mapped.password == "Secret123!"
    assert mapped.user_id == ""
    assert mapped.token == ""
    assert mapped.extra["access_token"] == ""
    assert mapped.extra["cookies"] == "{\"login_session\":\"yes\"}"


def test_browser_registration_mapper_prefers_formal_refresh_token_for_phone_first_oauth():
    platform = object.__new__(ChatGPTPlatform)

    mapped = platform._map_chatgpt_result(
        {
            "email": "user@example.com",
            "password": "Secret123!",
            "account_id": "acct_123",
            "access_token": "access-token",
            "refresh_token": "formal-refresh-token",
            "registration_refresh_token": "registration-only-refresh-token",
            "refresh_token_source": "phone_first_oauth",
            "id_token": "id-token",
        },
        require_oauth=True,
    )

    assert mapped.extra["refresh_token"] == "formal-refresh-token"
    assert mapped.extra["registration_refresh_token"] == "formal-refresh-token"
    assert mapped.extra["refresh_token_source"] == "phone_first_oauth"


def test_browser_oauth_adapter_still_requires_complete_oauth_result():
    platform = object.__new__(ChatGPTPlatform)
    adapter = ChatGPTPlatform.build_browser_registration_adapter(platform)
    ctx = SimpleNamespace(identity=SimpleNamespace(identity_provider="oauth_browser"))

    with pytest.raises(RuntimeError, match="OAuth callback"):
        adapter.result_mapper(ctx, {
            "email": "user@example.com",
            "account_id": "",
            "access_token": "",
        })


def test_sms_oauth_browser_adapter_enables_phone_first_flow(monkeypatch):
    captured = {}

    class _FakeBrowserRegister:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(browser_register_module, "ChatGPTBrowserRegister", _FakeBrowserRegister)
    platform = object.__new__(ChatGPTPlatform)
    adapter = ChatGPTPlatform.build_browser_registration_adapter(platform)
    ctx = SimpleNamespace(
        executor_type="headless",
        proxy=None,
        log=lambda message: None,
        identity=SimpleNamespace(identity_provider="sms_oauth", email="user@example.com"),
        extra={},
    )
    artifacts = SimpleNamespace(otp_callback="otp-callback", phone_callback="phone-callback")

    worker = adapter.browser_worker_builder(ctx, artifacts)

    assert isinstance(worker, _FakeBrowserRegister)
    assert captured["phone_first_oauth"] is True
    assert captured["bind_email_after_phone_signup"] is True
    assert captured["otp_callback"] == "otp-callback"
    assert captured["phone_callback"] == "phone-callback"


def test_sms_oauth_protocol_mailbox_adapter_rejects_protocol_flow():
    platform = object.__new__(ChatGPTPlatform)
    platform.mailbox = None
    platform.config = RegisterConfig()
    adapter = ChatGPTPlatform.build_protocol_mailbox_adapter(platform)
    ctx = SimpleNamespace(identity=SimpleNamespace(identity_provider="sms_oauth"))

    with pytest.raises(RuntimeError, match="browser executors"):
        adapter.preflight(ctx)


def test_fetch_chatgpt_session_opens_session_api_directly():
    calls = []

    class FakePage:
        context = SimpleNamespace(cookies=lambda: [
            {"name": "__Secure-next-auth.session-token", "value": "sess_123"},
            {"name": "oai-did", "value": "did_123"},
        ])

        def goto(self, url, **kwargs):
            calls.append((url, kwargs))
            return _SessionApiResponse()

    logs = []

    result = browser_register_module._fetch_chatgpt_session_from_page(
        FakePage(),
        {"login_session": "yes"},
        logs.append,
        timeout=5,
    )

    assert calls[0][0] == "https://chatgpt.com/api/auth/session"
    assert "chatgpt.com/api/auth/session" in logs[0]
    assert result["access_token"] == "at_123"
    assert result["session_token"] == "sess_123"
    assert result["cookies"] == "login_session=yes; __Secure-next-auth.session-token=sess_123; oai-did=did_123"


def test_fetch_chatgpt_session_uses_same_origin_fetch_before_navigation():
    calls = {"evaluate": 0, "goto": 0}

    class FakePage:
        url = "https://chatgpt.com/"
        context = SimpleNamespace(cookies=lambda: [
            {"name": "__Secure-next-auth.session-token", "value": "sess_123"},
        ])

        def evaluate(self, script, arg=None):
            calls["evaluate"] += 1
            assert arg == "https://chatgpt.com/api/auth/session"
            return {
                "status": 200,
                "url": "https://chatgpt.com/api/auth/session",
                "text": (
                    '{"accessToken":"at_fetch","user":{"email":"user@example.com"},'
                    '"expires":"2026-05-20T12:00:00Z"}'
                ),
            }

        def goto(self, url, **kwargs):
            calls["goto"] += 1
            raise AssertionError("same-origin session fetch should avoid navigation")

    result = browser_register_module._fetch_chatgpt_session_from_page(
        FakePage(),
        {},
        lambda message: None,
        timeout=5,
    )

    assert calls == {"evaluate": 1, "goto": 0}
    assert result["access_token"] == "at_fetch"
    assert result["session_token"] == "sess_123"


def test_fetch_chatgpt_session_falls_back_to_page_body_when_response_text_unavailable(monkeypatch):
    times = iter([100.0, 101.0, 106.0])
    monkeypatch.setattr(browser_register_module.time, "time", lambda: next(times))
    monkeypatch.setattr(browser_register_module.time, "sleep", lambda seconds: None)

    class FakeBody:
        def inner_text(self, timeout=3000):
            return (
                '{"accessToken":"at_from_body","user":{"email":"user@example.com"},'
                '"expires":"2026-05-20T12:00:00Z"}'
            )

    class FakePage:
        url = "https://chatgpt.com/api/auth/session"
        context = SimpleNamespace(cookies=lambda: [
            {"name": "__Secure-next-auth.session-token", "value": "sess_123"},
        ])

        def goto(self, url, **kwargs):
            self.url = url
            return _UnreadableSessionApiResponse()

        def locator(self, selector):
            assert selector == "body"
            return FakeBody()

    result = browser_register_module._fetch_chatgpt_session_from_page(
        FakePage(),
        {},
        lambda message: None,
        timeout=5,
    )

    assert result["access_token"] == "at_from_body"
    assert result["session_token"] == "sess_123"


def test_browser_registration_flow_starts_from_chatgpt_nextauth(monkeypatch):
    calls = {}

    class FakePage:
        url = "about:blank"
        context = SimpleNamespace(cookies=lambda: [
            {"name": "login_session", "value": "yes"},
        ])

        def evaluate(self, script, *args):
            return "Mozilla/5.0"

    def start_via_authorize(page, email, device_id, log):
        calls["authorize"] = (email, device_id)
        page.url = "https://chatgpt.com/api/auth/callback/openai?code=abc"
        return {"page_type": "oauth_callback", "current_url": page.url}

    def fail_via_page(*args, **kwargs):
        calls["page"] = True
        raise AssertionError("browser registration should start from ChatGPT NextAuth")

    monkeypatch.setattr(browser_register_module, "_seed_browser_device_id", lambda page, device_id: calls.setdefault("seed", device_id))
    monkeypatch.setattr(browser_register_module, "_start_browser_signup_via_authorize", start_via_authorize)
    monkeypatch.setattr(browser_register_module, "_start_browser_signup_via_page", fail_via_page)
    monkeypatch.setattr(browser_register_module, "_handle_post_signup_onboarding", lambda page, log: None)

    state = browser_register_module._browser_registration_flow(
        FakePage(),
        "user@example.com",
        "Secret123!",
        otp_callback=None,
        phone_callback=None,
        log=lambda message: None,
    )

    assert calls["authorize"][0] == "user@example.com"
    assert calls["authorize"][1] == calls["seed"]
    assert "page" not in calls
    assert state["page_type"] == "oauth_callback"


def test_phone_first_entry_uses_chatgpt_homepage_flow(monkeypatch):
    class FakePage:
        url = "about:blank"

        def goto(self, url, **kwargs):
            self.url = url

    calls = {"goto": [], "clicks": [], "submitted": []}

    def fake_goto(page, url, **kwargs):
        calls["goto"].append(url)
        page.url = url

    monkeypatch.setattr(browser_register_module, "_goto_with_retry", fake_goto)
    monkeypatch.setattr(browser_register_module, "_wait_for_page_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_register_module, "_click_visible_text_control", lambda _page, _needles, label, _log: calls["clicks"].append(label) or True)
    monkeypatch.setattr(browser_register_module, "_find_phone_identity_input_selector", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(browser_register_module, "_is_session_ended_page", lambda _page: False)
    monkeypatch.setattr(
        browser_register_module,
        "_submit_phone_identity_via_page",
        lambda page, phone, log: calls["submitted"].append(phone) or {"page_type": "create_account_password"},
    )

    state, phone = browser_register_module._start_phone_first_signup_from_forced_entry(
        FakePage(),
        lambda: "+15550000001",
        lambda message: None,
    )

    assert calls["goto"][-1] == browser_register_module.CHATGPT_APP + "/"
    assert all("log-in-or-create-account" not in url for url in calls["goto"])
    assert calls["clicks"] == ["login/signup", "phone-number continue"]
    assert calls["submitted"] == ["+15550000001"]
    assert phone == "+15550000001"
    assert state["page_type"] == "create_account_password"


def test_phone_first_password_failure_restarts_full_round_when_edit_has_no_input(monkeypatch):
    class FakePage:
        url = "https://auth.openai.com/create-account/password"

        def evaluate(self, _script):
            return "ua"

    logs = []
    calls = {"homepage": 0, "old_authorize": 0}

    def fake_submit_password(*_args, **_kwargs):
        return {"ok": False, "status": 400, "text": "account_creation_failed"}

    def fake_homepage_entry(_page, _phone_callback, _log):
        calls["homepage"] += 1
        if calls["homepage"] == 1:
            _page.url = "https://auth.openai.com/create-account/password"
            return {"page_type": "create_account_password", "current_url": _page.url}, "+15550000001"
        _page.url = "https://chatgpt.com/"
        return {"page_type": "oauth_callback", "current_url": "https://chatgpt.com/"}, "+15550000002"

    monkeypatch.setattr(browser_register_module, "_seed_browser_device_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        browser_register_module,
        "_start_browser_phone_signup_via_authorize",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old authorize helper should not run")),
    )
    monkeypatch.setattr(browser_register_module, "_submit_password_via_page", fake_submit_password)
    monkeypatch.setattr(browser_register_module, "_return_phone_first_signup_to_phone_entry", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(browser_register_module, "_start_phone_first_signup_from_forced_entry", fake_homepage_entry)
    monkeypatch.setattr(browser_register_module, "_handle_post_signup_onboarding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_register_module, "_get_cookies", lambda _page: {})
    monkeypatch.setattr(browser_register_module.time, "sleep", lambda seconds: None)

    state = browser_register_module._browser_registration_flow(
        FakePage(),
        "user@example.com",
        "Secret123!",
        otp_callback=None,
        phone_callback=lambda: "+15550000001",
        log=logs.append,
        signup_method="phone",
        phone_change_limit=2,
    )

    assert calls["homepage"] == 2
    assert calls["old_authorize"] == 0
    assert state["page_type"] == "chatgpt_home"
    assert any("restarting from chatgpt.com homepage" in message for message in logs)


def test_phone_first_initial_flow_uses_homepage_entry(monkeypatch):
    class FakePage:
        url = "about:blank"

        def evaluate(self, _script):
            return "ua"

    calls = {"homepage": 0}

    def fake_homepage_entry(_page, _phone_callback, _log):
        calls["homepage"] += 1
        _page.url = "https://chatgpt.com/"
        return {"page_type": "oauth_callback", "current_url": _page.url}, "+15550000001"

    monkeypatch.setattr(browser_register_module, "_seed_browser_device_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        browser_register_module,
        "_start_browser_phone_signup_via_authorize",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old authorize helper should not run")),
    )
    monkeypatch.setattr(browser_register_module, "_start_phone_first_signup_from_forced_entry", fake_homepage_entry)
    monkeypatch.setattr(browser_register_module, "_handle_post_signup_onboarding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_register_module, "_get_cookies", lambda _page: {})
    monkeypatch.setattr(browser_register_module.time, "sleep", lambda seconds: None)

    state = browser_register_module._browser_registration_flow(
        FakePage(),
        "user@example.com",
        "Secret123!",
        otp_callback=None,
        phone_callback=lambda: "+15550000001",
        log=lambda message: None,
        signup_method="phone",
    )

    assert calls["homepage"] == 1
    assert state["page_type"] == "chatgpt_home"


def test_phone_first_submit_disables_passwordless_login_click(monkeypatch):
    class FakeInput:
        def __init__(self):
            self._value = ""

        def wait_for(self, **_kwargs):
            return None

        def input_value(self):
            return self._value

        def click(self, **_kwargs):
            return None

        def fill(self, value):
            self._value = value

        def type(self, value, delay=0):
            self._value = value

    class FakePage:
        url = "https://auth.openai.com/add-phone"

        def __init__(self):
            self._input = FakeInput()

        def locator(self, selector):
            return self._input

    calls = {"passwordless": 0, "fill": []}

    monkeypatch.setattr(browser_register_module, "_click_signup_link_if_on_login", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_register_module, "_find_phone_identity_input_selector", lambda *_args, **_kwargs: "input[type='tel']")
    monkeypatch.setattr(browser_register_module, "_select_phone_country_ui", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(browser_register_module, "_get_phone_country_select_state", lambda *_args, **_kwargs: {"hasTrigger": True, "matchesDial": True, "matchesCountry": True, "matchesIso": True})
    monkeypatch.setattr(browser_register_module, "_sync_generic_phone_hidden_value", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_register_module, "_click_first", lambda *args, **kwargs: "button[type='submit']")
    monkeypatch.setattr(browser_register_module, "_wait_for_signup_entry_transition", lambda *args, **kwargs: {"page_type": "create_account_password"})
    monkeypatch.setattr(browser_register_module, "_click_passwordless_login_if_available", lambda *_args, **_kwargs: calls.__setitem__("passwordless", calls["passwordless"] + 1) or True)

    result = browser_register_module._submit_phone_identity_via_page(FakePage(), "+85664890950", lambda message: None)

    assert result["page_type"] == "create_account_password"
    assert calls["passwordless"] == 0


def test_browser_register_run_returns_after_registration_without_codex_oauth(monkeypatch):
    class FakePage:
        def __init__(self):
            self.url = "about:blank"
            self.context = SimpleNamespace(cookies=lambda: [])

        def goto(self, url, **kwargs):
            self.url = url

    class FakeBrowser:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def new_page(self):
            return FakePage()

    called = {"oauth": False}

    def fail_if_oauth_runs(self, email, password):
        called["oauth"] = True
        raise AssertionError("Codex OAuth should not run after browser registration")

    monkeypatch.setattr(browser_register_module, "Camoufox", lambda **kwargs: FakeBrowser())
    monkeypatch.setattr(browser_register_module, "_browser_registration_flow", lambda *args, **kwargs: {"page_type": "oauth_callback"})
    monkeypatch.setattr(browser_register_module, "_click_first", lambda page, selectors, timeout=3: setattr(page, "url", "https://auth.openai.com/log-in") or selectors[0])
    monkeypatch.setattr(
        browser_register_module,
        "_get_cookies",
        lambda page: {"login_session": "yes", "__Secure-next-auth.session-token": "sess_123"},
    )
    monkeypatch.setattr(
        browser_register_module,
        "_fetch_chatgpt_session_from_page",
        lambda page, cookies, log: {
            "access_token": "at_123",
            "refresh_token": "",
            "id_token": "",
            "session_token": "sess_123",
            "account_id": "acct_123",
            "workspace_id": "",
            "profile": {"email": "user@example.com"},
            "expires_at": "2026-05-20T12:00:00Z",
            "cookies": "__Secure-next-auth.session-token=sess_123; login_session=yes",
        },
        raising=False,
    )
    monkeypatch.setattr(browser_register_module, "_do_codex_oauth", lambda *args, **kwargs: None)
    monkeypatch.setattr(browser_register_module.ChatGPTBrowserRegister, "_retry_oauth_fresh_browser", fail_if_oauth_runs)
    monkeypatch.setattr(browser_register_module.time, "sleep", lambda seconds: None)

    worker = browser_register_module.ChatGPTBrowserRegister(
        headless=True,
        proxy=None,
        otp_callback=None,
        log_fn=lambda message: None,
    )

    result = worker.run(email="user@example.com", password="Secret123!")

    assert called["oauth"] is False
    assert result["email"] == "user@example.com"
    assert result["password"] == "Secret123!"
    assert result["access_token"] == "at_123"
    assert result["account_id"] == "acct_123"
    assert result["session_token"] == "sess_123"
    assert result["cookies"] == "__Secure-next-auth.session-token=sess_123; login_session=yes"


def test_phone_first_oauth_falls_back_to_fresh_browser_retry(monkeypatch):
    class FakePage:
        def __init__(self):
            self.url = "about:blank"
            self.context = SimpleNamespace(cookies=lambda: [])

    class FakeBrowser:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def new_page(self):
            return FakePage()

    calls = {"oauth": 0, "retry": 0}

    def fake_do_codex_oauth(*_args, **_kwargs):
        calls["oauth"] += 1
        return {"error": "blocked"}

    def fake_retry(self, email, password):
        calls["retry"] += 1
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "account_id": "acct-123",
            "id_token": "id-token",
        }

    monkeypatch.setattr(browser_register_module, "Camoufox", lambda **kwargs: FakeBrowser())
    monkeypatch.setattr(browser_register_module, "_browser_registration_flow", lambda *args, **kwargs: {"page_type": "oauth_callback"})
    monkeypatch.setattr(browser_register_module, "_click_first", lambda page, selectors, timeout=3: selectors[0])
    monkeypatch.setattr(browser_register_module, "_get_cookies", lambda page: {})
    monkeypatch.setattr(browser_register_module, "_fetch_chatgpt_session_from_page", lambda *args, **kwargs: {"access_token": "at_123", "refresh_token": "", "session_token": "sess_123", "account_id": "acct_123", "cookies": ""})
    monkeypatch.setattr(browser_register_module, "_do_codex_oauth", fake_do_codex_oauth)
    monkeypatch.setattr(browser_register_module.ChatGPTBrowserRegister, "_retry_oauth_fresh_browser", fake_retry)
    monkeypatch.setattr(browser_register_module.time, "sleep", lambda seconds: None)

    worker = browser_register_module.ChatGPTBrowserRegister(
        headless=True,
        proxy=None,
        otp_callback=None,
        phone_callback=None,
        log_fn=lambda message: None,
        phone_first_oauth=True,
        bind_email_after_phone_signup=True,
    )

    result = worker.run(email="user@example.com", password="Secret123!")

    assert calls == {"oauth": 1, "retry": 1}
    assert result["refresh_token"] == "refresh-token"
    assert result["refresh_token_source"] == "phone_first_oauth"


def test_phone_first_oauth_requires_usable_refresh_token(monkeypatch):
    class FakePage:
        def __init__(self):
            self.url = "about:blank"
            self.context = SimpleNamespace(cookies=lambda: [])

    class FakeBrowser:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def new_page(self):
            return FakePage()

    monkeypatch.setattr(browser_register_module, "Camoufox", lambda **kwargs: FakeBrowser())
    monkeypatch.setattr(browser_register_module, "_browser_registration_flow", lambda *args, **kwargs: {"page_type": "oauth_callback"})
    monkeypatch.setattr(browser_register_module, "_click_first", lambda page, selectors, timeout=3: selectors[0])
    monkeypatch.setattr(browser_register_module, "_get_cookies", lambda page: {})
    monkeypatch.setattr(browser_register_module, "_fetch_chatgpt_session_from_page", lambda *args, **kwargs: {"access_token": "at_123", "refresh_token": "", "session_token": "sess_123", "account_id": "acct_123", "cookies": ""})
    monkeypatch.setattr(browser_register_module, "_do_codex_oauth", lambda *args, **kwargs: {"access_token": "access-only"})
    monkeypatch.setattr(browser_register_module.ChatGPTBrowserRegister, "_retry_oauth_fresh_browser", lambda self, email, password: {"access_token": "still-access-only"})
    monkeypatch.setattr(browser_register_module.time, "sleep", lambda seconds: None)

    worker = browser_register_module.ChatGPTBrowserRegister(
        headless=True,
        proxy=None,
        otp_callback=None,
        phone_callback=None,
        log_fn=lambda message: None,
        phone_first_oauth=True,
        bind_email_after_phone_signup=True,
    )

    with pytest.raises(RuntimeError, match="usable refresh_token"):
        worker.run(email="user@example.com", password="Secret123!")
