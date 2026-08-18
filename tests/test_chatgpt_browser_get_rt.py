from __future__ import annotations

import inspect
from types import SimpleNamespace

from platforms.chatgpt import browser_get_rt
from platforms.chatgpt import browser_register


class _FakePage:
    def __init__(self):
        self.routes = []

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))


class _FakeRequest:
    url = "https://auth.openai.com/oauth/authorize?state=state_123&client_id=test"


class _FakeRoute:
    request = _FakeRequest()

    def __init__(self):
        self.fallback_called = False

    def fallback(self):
        self.fallback_called = True


def test_get_rt_route_handlers_are_sync_playwright_handlers():
    page = _FakePage()
    browser_get_rt._state_store.clear()

    browser_get_rt.setup_phone_otp_skip_interception(page, log=lambda _message: None)

    assert page.routes
    for _pattern, handler in page.routes:
        assert not inspect.iscoroutinefunction(handler)

    pattern, oauth_handler = page.routes[-1]
    assert pattern == "**/oauth/authorize*"

    route = _FakeRoute()
    result = oauth_handler(route)

    assert not inspect.isawaitable(result)
    assert route.fallback_called is True
    assert browser_get_rt._state_store["oauth_state"] == "state_123"


def test_get_rt_phone_timeout_releases_and_switches_number(monkeypatch):
    events = []
    attempts = []

    class PhoneCallback:
        phase = "need_code"
        activation = "activation-1"
        completed = False

        def set_code_timeout(self, timeout):
            events.append(("timeout", timeout))

        def cleanup(self):
            events.append("cleanup")

    def fake_attempt(*_args, **kwargs):
        attempts.append(kwargs["phone_code_timeout"])
        if len(attempts) == 1:
            raise RuntimeError(
                f"{browser_register.PHONE_CODE_TIMEOUT_SENTINEL}: 等待短信验证码超过 60s"
            )
        return {"page_type": "consent"}

    monkeypatch.setattr(browser_register, "_do_add_phone_attempt", fake_attempt)
    monkeypatch.setattr(browser_register, "_goto_with_retry", lambda *_args, **_kwargs: None)

    result = browser_register._handle_add_phone_challenge(
        object(),
        PhoneCallback(),
        device_id="device-1",
        user_agent="Mozilla/5.0",
        log=events.append,
        max_phone_attempts=2,
        phone_code_timeout=60,
        retry_on_timeout=True,
    )

    assert result == {"page_type": "consent"}
    assert attempts == [60, 60]
    assert "cleanup" in events
    assert any("切换下一个" in event for event in events if isinstance(event, str))


def test_oauth_state_capture_is_sync_and_does_not_rewrite_responses():
    page = _FakePage()
    browser_get_rt._state_store.clear()

    browser_get_rt.setup_oauth_state_capture(page, log=lambda _message: None)

    assert len(page.routes) == 1
    pattern, oauth_handler = page.routes[0]
    assert pattern == "**/oauth/authorize*"
    assert not inspect.iscoroutinefunction(oauth_handler)

    route = _FakeRoute()
    result = oauth_handler(route)

    assert not inspect.isawaitable(result)
    assert route.fallback_called is True
    assert browser_get_rt._state_store["oauth_state"] == "state_123"


def test_email_otp_retry_resends_after_incorrect_code(monkeypatch):
    calls = {"submit": 0, "resend": 0, "refresh": 0}
    codes = []

    class FakePage:
        url = "https://auth.openai.com/email-verification"

    def otp_callback():
        code = "111111" if len(codes) == 0 else "222222"
        codes.append(code)
        return code

    def refresh_before_ids():
        calls["refresh"] += 1
        return {"old-message-id"}

    otp_callback.refresh_before_ids = refresh_before_ids

    def fake_submit(_page, code, _log):
        calls["submit"] += 1
        if code == "111111":
            return {"ok": False, "status": 400, "url": FakePage.url, "text": "Incorrect code"}
        return {"ok": True, "status": 200, "url": "https://chatgpt.com/", "text": ""}

    def fake_click(_page, selectors, *, timeout=10):
        calls["resend"] += 1
        assert any("Resend email" in selector for selector in selectors)
        return 'button:has-text("Resend email")'

    monkeypatch.setattr(browser_register, "_submit_otp_via_page", fake_submit)
    monkeypatch.setattr(browser_register, "_click_first_no_wait", fake_click)

    result = browser_register._submit_email_otp_with_retry(
        FakePage(),
        otp_callback,
        lambda _message: None,
        max_invalid_retries=3,
    )

    assert result["ok"] is True
    assert codes == ["111111", "222222"]
    assert calls == {"submit": 2, "resend": 1, "refresh": 1}


def test_email_otp_retry_stops_after_three_invalid_resends(monkeypatch):
    calls = {"submit": 0, "resend": 0, "refresh": 0}

    class FakePage:
        url = "https://auth.openai.com/email-verification"

    def otp_callback():
        return "111111"

    def refresh_before_ids():
        calls["refresh"] += 1
        return {"old-message-id"}

    otp_callback.refresh_before_ids = refresh_before_ids

    def fake_submit(_page, _code, _log):
        calls["submit"] += 1
        return {"ok": False, "status": 400, "url": FakePage.url, "text": "Incorrect code"}

    def fake_click(_page, _selectors, *, timeout=10):
        calls["resend"] += 1
        return 'button:has-text("Resend email")'

    monkeypatch.setattr(browser_register, "_submit_otp_via_page", fake_submit)
    monkeypatch.setattr(browser_register, "_click_first_no_wait", fake_click)

    result = browser_register._submit_email_otp_with_retry(
        FakePage(),
        otp_callback,
        lambda _message: None,
        max_invalid_retries=3,
    )

    assert result["ok"] is False
    assert result["text"] == "Incorrect code"
    assert calls == {"submit": 4, "resend": 3, "refresh": 3}


def test_email_otp_retry_recovers_transient_missing_input_six_times(monkeypatch):
    events = []
    calls = {"submit": 0, "otp": 0, "recover": 0}

    class FakePage:
        url = "https://auth.openai.com/email-verification"

    def otp_callback():
        calls["otp"] += 1
        return "123456"

    def fake_submit(_page, _code, _log):
        calls["submit"] += 1
        if calls["submit"] <= 6:
            return {
                "ok": False,
                "status": 0,
                "url": FakePage.url,
                "text": "验证码页未找到可填写输入框",
            }
        return {"ok": True, "status": 200, "url": "https://auth.openai.com/add-phone", "text": ""}

    def fake_recover(_page, _log, *, recover_url=""):
        calls["recover"] += 1
        return None

    monkeypatch.setattr(browser_register, "_otp_page_transition_result", lambda _page: None)
    monkeypatch.setattr(browser_register, "_submit_otp_via_page", fake_submit)
    monkeypatch.setattr(browser_register, "_recover_otp_submit_page", fake_recover)

    result = browser_register._submit_email_otp_with_retry(
        FakePage(),
        otp_callback,
        events.append,
        max_invalid_retries=3,
        max_transient_retries=6,
        recover_url="https://auth.openai.com/oauth/authorize?state=retry",
    )

    assert result["ok"] is True
    assert calls == {"submit": 7, "otp": 1, "recover": 6}
    assert any("recovery retry 6/6" in item for item in events)


def test_submit_otp_uses_browser_validate_fallback_after_stuck_click(monkeypatch):
    events = []

    class FakePage:
        url = "https://auth.openai.com/email-verification"

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def locator(self, selector):
            class Locator:
                def __init__(self, name):
                    self.name = name
                    self.first = self

                def count(self):
                    return 0

                def wait_for(self, **_kwargs):
                    if self.name == "input":
                        return None
                    raise RuntimeError("not found")

                def click(self, **_kwargs):
                    return None

                def fill(self, _value):
                    return None

                def type(self, _value, **_kwargs):
                    return None

                def input_value(self):
                    return "123456"

            return Locator(selector)

        def get_by_label(self, *_args, **_kwargs):
            return self.locator("missing-label")

        def get_by_role(self, *_args, **_kwargs):
            return self.locator("missing-role")

    monkeypatch.setattr(browser_register, "_otp_page_transition_result", lambda _page: None)
    monkeypatch.setattr(browser_register, "_click_first", lambda *_args, **_kwargs: 'button[type="submit"]')
    monkeypatch.setattr(
        browser_register,
        "_validate_browser_email_otp",
        lambda *_args, **_kwargs: {"ok": True, "status": 200, "url": "https://auth.openai.com/about-you", "data": {}},
    )

    result = browser_register._submit_otp_via_page(
        FakePage(),
        "123456",
        events.append,
        device_id="device-1",
        user_agent="Mozilla/5.0",
    )

    assert result["ok"] is True
    assert result["url"] == "https://auth.openai.com/about-you"
    assert any("直连 validate 接口" in item for item in events)


def test_oauth_email_submit_uses_form_fallback_after_stuck_click(monkeypatch):
    events = []

    class FakePage:
        url = "https://auth.openai.com/log-in"

    transitions = [
        {"ok": False, "status": 0, "url": FakePage.url, "text": "stuck"},
        {"ok": True, "status": 200, "url": "https://auth.openai.com/email-verification", "text": ""},
    ]

    monkeypatch.setattr(browser_register, "_wait_for_any_selector", lambda *_args, **_kwargs: 'input[type="email"]')
    monkeypatch.setattr(browser_register, "_fill_input_like_user", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(browser_register, "_click_first_no_wait", lambda *_args, **_kwargs: 'button[type="submit"]')
    monkeypatch.setattr(browser_register, "_submit_login_email_form_fallback", lambda *_args, **_kwargs: "requestSubmit(button)")
    monkeypatch.setattr(browser_register, "_wait_for_login_email_transition", lambda *_args, **_kwargs: transitions.pop(0))

    result = browser_register._submit_login_email_via_page(FakePage(), "user@example.com", events.append)

    assert result["ok"] is True
    assert result["url"] == "https://auth.openai.com/email-verification"
    assert any("fallback submit" in item for item in events)


def test_oauth_email_submit_reopens_authorize_url_before_retry(monkeypatch):
    events = []
    opened = []

    class FakePage:
        url = "https://auth.openai.com/log-in"

    transitions = [
        {"ok": False, "status": 0, "url": FakePage.url, "text": "stuck"},
        {"ok": True, "status": 200, "url": "https://auth.openai.com/email-verification", "text": ""},
    ]

    def fake_goto(page, url, **_kwargs):
        opened.append(url)
        page.url = "https://auth.openai.com/log-in?retry=1"

    monkeypatch.setattr(browser_register, "_wait_for_any_selector", lambda *_args, **_kwargs: 'input[type="email"]')
    monkeypatch.setattr(browser_register, "_fill_input_like_user", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(browser_register, "_click_first_no_wait", lambda *_args, **_kwargs: 'button[type="submit"]')
    monkeypatch.setattr(browser_register, "_submit_login_email_form_fallback", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(browser_register, "_wait_for_login_email_transition", lambda *_args, **_kwargs: transitions.pop(0))
    monkeypatch.setattr(browser_register, "_oauth_login_page_diagnostic", lambda _page: {"url": FakePage.url, "buttons": [], "inputs": [], "text": "login"})
    monkeypatch.setattr(browser_register, "_goto_with_retry", fake_goto)
    monkeypatch.setattr(browser_register.time, "sleep", lambda _seconds: None)

    result = browser_register._submit_login_email_via_page(
        FakePage(),
        "user@example.com",
        events.append,
        recover_url="https://auth.openai.com/oauth/authorize?state=retry",
    )

    assert result["ok"] is True
    assert opened == ["https://auth.openai.com/oauth/authorize?state=retry"]
    assert any("reopening current OAuth authorize URL" in item for item in events)


def test_codex_oauth_continues_when_phone_resume_returns_login_email(monkeypatch):
    events = []
    calls = {"email_submit": 0, "callback_wait": 0}

    class FakePage:
        url = "https://auth.openai.com/start"

        def evaluate(self, _script):
            return "Mozilla/5.0"

    page = FakePage()
    oauth_start = SimpleNamespace(
        auth_url="https://auth.openai.com/oauth/authorize?state=state_1",
        state="state_1",
        code_verifier="verifier_1",
        redirect_uri="http://localhost:1455/auth/callback",
        client_id="client_1",
    )

    states = [
        {"page_type": "add_phone", "continue_url": ""},
        {"page_type": "login_email", "continue_url": ""},
        {"page_type": "login_email", "continue_url": ""},
        {"page_type": "oauth_callback", "continue_url": ""},
    ]

    def fake_goto(page_obj, url, **_kwargs):
        page_obj.url = url

    def fake_state(_page):
        if states:
            return states.pop(0)
        return {"page_type": "oauth_callback", "continue_url": ""}

    def fake_callback_wait(_page, _oauth_start, _proxy, _log, *, timeout_sec=90):
        calls["callback_wait"] += 1
        if calls["callback_wait"] == 1:
            return None
        if calls["callback_wait"] == 2:
            page.url = "https://auth.openai.com/log-in"
            return None
        page.url = "http://localhost:1455/auth/callback?code=ok&state=state_1"
        return {"access_token": "access-token", "refresh_token": "refresh-token"}

    def fake_email_submit(_page, _email, _log, **kwargs):
        calls["email_submit"] += 1
        assert kwargs.get("recover_url") == oauth_start.auth_url
        page.url = "http://localhost:1455/auth/callback?code=ok&state=state_1"
        return {"ok": True, "status": 200, "url": page.url, "text": ""}

    monkeypatch.setattr(browser_register, "_goto_with_retry", fake_goto)
    monkeypatch.setattr(browser_register, "_derive_oauth_state_from_page", fake_state)
    monkeypatch.setattr(browser_register, "_handle_add_phone_challenge", lambda *_args, **_kwargs: {"page_type": "add_phone"})
    monkeypatch.setattr(browser_register, "_wait_for_oauth_callback_result", fake_callback_wait)
    monkeypatch.setattr(browser_register, "_submit_login_email_via_page", fake_email_submit)
    monkeypatch.setattr(
        browser_register,
        "_submit_callback_result_or_error",
        lambda *_args, **_kwargs: {"access_token": "access-token", "refresh_token": "refresh-token"},
    )
    monkeypatch.setattr(browser_register, "_get_page_oauth_url", lambda _page: "")

    result = browser_register._do_codex_oauth(
        page,
        {},
        "user@example.com",
        "password",
        otp_callback=None,
        phone_callback=lambda: "phone",
        proxy=None,
        log=events.append,
        oauth_start=oauth_start,
    )

    assert result["refresh_token"] == "refresh-token"
    assert calls["email_submit"] == 1
    assert any("continue relogin flow" in item for item in events)


def test_codex_oauth_continues_after_phone_success_post_resume_exception(monkeypatch):
    events = []

    class FakePage:
        url = "https://auth.openai.com/start"

        def evaluate(self, _script):
            return "Mozilla/5.0"

    class PhoneCallback:
        completed = False

        def __call__(self):
            return "+628000000001"

    page = FakePage()
    phone_callback = PhoneCallback()
    oauth_start = SimpleNamespace(
        auth_url="https://auth.openai.com/oauth/authorize?state=state_1",
        state="state_1",
        code_verifier="verifier_1",
        redirect_uri="http://localhost:1455/auth/callback",
        client_id="client_1",
    )
    states = [
        {"page_type": "add_phone", "continue_url": ""},
        {"page_type": "oauth_callback", "continue_url": ""},
    ]

    def fake_state(_page):
        if states:
            return states.pop(0)
        return {"page_type": "oauth_callback", "continue_url": ""}

    def fake_handle(page_obj, cb, **_kwargs):
        cb.completed = True
        page_obj.url = "http://localhost:1455/auth/callback?code=ok&state=state_1"
        raise RuntimeError("resume navigation failed")

    monkeypatch.setattr(browser_register, "_goto_with_retry", lambda page_obj, url, **_kwargs: setattr(page_obj, "url", url))
    monkeypatch.setattr(browser_register, "_derive_oauth_state_from_page", fake_state)
    monkeypatch.setattr(browser_register, "_handle_add_phone_challenge", fake_handle)
    monkeypatch.setattr(
        browser_register,
        "_submit_callback_result_or_error",
        lambda *_args, **_kwargs: {"access_token": "access-token", "refresh_token": "refresh-token"},
    )
    monkeypatch.setattr(browser_register, "_get_page_oauth_url", lambda _page: "")

    result = browser_register._do_codex_oauth(
        page,
        {},
        "user@example.com",
        "password",
        otp_callback=None,
        phone_callback=phone_callback,
        proxy=None,
        log=events.append,
        oauth_start=oauth_start,
    )

    assert result["refresh_token"] == "refresh-token"
    assert any("手机验证已成功" in item and "继续状态机重试" in item for item in events)


def test_codex_oauth_submits_totp_for_mfa_challenge(monkeypatch):
    events = []
    calls = {"mfa": 0}

    class FakePage:
        url = "https://auth.openai.com/start"

        def evaluate(self, _script):
            return "Mozilla/5.0"

    page = FakePage()
    oauth_start = SimpleNamespace(
        auth_url="https://auth.openai.com/oauth/authorize?state=state_1",
        state="state_1",
        code_verifier="verifier_1",
        redirect_uri="http://localhost:1455/auth/callback",
        client_id="client_1",
    )
    states = [
        {"page_type": "mfa_challenge", "continue_url": "https://auth.openai.com/mfa-challenge/factor-1"},
        {"page_type": "oauth_callback", "continue_url": ""},
    ]

    def fake_state(_page):
        if states:
            return states.pop(0)
        return {"page_type": "oauth_callback", "continue_url": ""}

    def fake_mfa(page_obj, secret, _log):
        calls["mfa"] += 1
        assert secret == "JBSWY3DPEHPK3PXP"
        page_obj.url = "http://localhost:1455/auth/callback?code=ok&state=state_1"
        return {"ok": True, "status": 200, "url": page_obj.url, "text": ""}

    monkeypatch.setattr(browser_register, "_goto_with_retry", lambda page_obj, url, **_kwargs: setattr(page_obj, "url", url))
    monkeypatch.setattr(browser_register, "_derive_oauth_state_from_page", fake_state)
    monkeypatch.setattr(browser_register, "_submit_oauth_totp_challenge", fake_mfa)
    monkeypatch.setattr(
        browser_register,
        "_submit_callback_result_or_error",
        lambda *_args, **_kwargs: {"access_token": "access-token", "refresh_token": "refresh-token"},
    )
    monkeypatch.setattr(browser_register, "_get_page_oauth_url", lambda _page: "")

    result = browser_register._do_codex_oauth(
        page,
        {},
        "user@example.com",
        "password",
        otp_callback=None,
        phone_callback=None,
        proxy=None,
        log=events.append,
        oauth_start=oauth_start,
        totp_secret="JBSWY3DPEHPK3PXP",
    )

    assert result["refresh_token"] == "refresh-token"
    assert calls["mfa"] == 1
    assert any("2FA challenge" in item for item in events)
