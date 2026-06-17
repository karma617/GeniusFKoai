from core.base_platform import Account, RegisterConfig
from platforms.chatgpt import browser_get_rt as browser_get_rt_module
from platforms.chatgpt import browser_register as browser_register_module
from platforms.chatgpt import plugin as plugin_module
from platforms.chatgpt import protocol_get_rt as protocol_get_rt_module
from platforms.chatgpt.plugin import ChatGPTPlatform


class _FakePage:
    def __init__(self, context):
        self.context = context


class _FakeContext:
    def __init__(self):
        self.closed = False
        self.pages = []

    def new_page(self):
        page = _FakePage(self)
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.new_context_kwargs = None
        self.context = _FakeContext()
        self.new_page_called = False

    def new_context(self, **kwargs):
        self.new_context_kwargs = dict(kwargs)
        return self.context

    def new_page(self):
        self.new_page_called = True
        return _FakePage(_FakeContext())


class _FakeBrowserManager:
    def __init__(self, browser):
        self.browser = browser

    def __enter__(self):
        return self.browser

    def __exit__(self, exc_type, exc, tb):
        return False


def test_get_rt_record_har_creates_camoufox_context_and_returns_path(monkeypatch, tmp_path):
    fake_browser = _FakeBrowser()
    expected_har_path = str(tmp_path / "get-rt-user_example.com.har")

    monkeypatch.setattr(
        ChatGPTPlatform,
        "_build_get_rt_mailbox_otp_callback",
        lambda self, account, log_fn, proxy: (lambda: "123456", ""),
    )
    monkeypatch.setattr(
        browser_get_rt_module,
        "setup_oauth_state_capture",
        lambda page, log=None: None,
    )
    monkeypatch.setattr(
        browser_register_module.ChatGPTBrowserRegister,
        "_open_browser",
        lambda self, launch_opts: _FakeBrowserManager(fake_browser),
    )
    monkeypatch.setattr(
        browser_register_module,
        "_do_codex_oauth",
        lambda *args, **kwargs: {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
            "account_id": "acct-123",
        },
    )
    monkeypatch.setattr(
        plugin_module,
        "_build_get_rt_har_path",
        lambda email: expected_har_path,
        raising=False,
    )

    platform = ChatGPTPlatform(RegisterConfig())
    result = platform._handle_get_rt(
        Account(
            platform="chatgpt",
            email="user@example.com",
            password="Secret123!",
        ),
        {"browser_mode": "camoufox_headed", "record_har": "true"},
    )

    assert result["ok"] is True
    assert fake_browser.new_page_called is False
    assert fake_browser.new_context_kwargs == {
        "record_har_path": expected_har_path,
        "record_har_url_filter": "**/*",
    }
    assert fake_browser.context.closed is True
    assert result["data"]["record_har_path"] == expected_har_path


def test_get_rt_uses_supplied_phone_callback(monkeypatch):
    fake_browser = _FakeBrowser()
    supplied_phone_callback = lambda: "+15550000001"
    seen = {}

    monkeypatch.setattr(
        ChatGPTPlatform,
        "_build_get_rt_mailbox_otp_callback",
        lambda self, account, log_fn, proxy: (lambda: "123456", ""),
    )
    monkeypatch.setattr(
        browser_get_rt_module,
        "setup_oauth_state_capture",
        lambda page, log=None: None,
    )
    monkeypatch.setattr(
        browser_get_rt_module,
        "build_get_rt_phone_callback",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should use supplied callback")),
    )
    monkeypatch.setattr(
        browser_register_module.ChatGPTBrowserRegister,
        "_open_browser",
        lambda self, launch_opts: _FakeBrowserManager(fake_browser),
    )

    def fake_oauth(*args, **kwargs):
        seen["phone_callback"] = args[5]
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
            "account_id": "acct-123",
        }

    monkeypatch.setattr(browser_register_module, "_do_codex_oauth", fake_oauth)

    platform = ChatGPTPlatform(RegisterConfig())
    result = platform._handle_get_rt(
        Account(
            platform="chatgpt",
            email="user@example.com",
            password="Secret123!",
        ),
        {
            "browser_mode": "camoufox_headed",
            "sms_provider": "smspool",
            "phone_callback": supplied_phone_callback,
        },
    )

    assert result["ok"] is True
    assert seen["phone_callback"] is supplied_phone_callback


def test_get_rt_protocol_executor_uses_protocol_runner_without_browser(monkeypatch):
    seen = {}

    monkeypatch.setattr(
        ChatGPTPlatform,
        "_build_get_rt_mailbox_otp_callback",
        lambda self, account, log_fn, proxy: (lambda: "123456", ""),
    )
    monkeypatch.setattr(
        plugin_module,
        "_save_get_rt_token_backup",
        lambda account, result, action_label="get_rt": "backup.json",
    )

    def fake_run_protocol_get_rt(**kwargs):
        seen.update(kwargs)
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
            "account_id": "acct-123",
            "email": kwargs["email"],
        }

    def fail_open_browser(*_args, **_kwargs):
        raise AssertionError("protocol executor should not open browser")

    monkeypatch.setattr(protocol_get_rt_module, "run_protocol_get_rt", fake_run_protocol_get_rt)
    monkeypatch.setattr(browser_register_module.ChatGPTBrowserRegister, "_open_browser", fail_open_browser)

    platform = ChatGPTPlatform(RegisterConfig())
    result = platform._handle_get_rt(
        Account(
            platform="chatgpt",
            email="user@example.com",
            password="Secret123!",
        ),
        {
            "executor_type": "protocol",
            "browser_mode": "bitbrowser_hidden",
            "sms_provider": "none",
            "record_har": "true",
        },
    )

    assert result["ok"] is True
    assert result["data"]["refresh_token"] == "refresh-token"
    assert result["data"]["record_har_path"] == ""
    assert result["data"]["token_backup_path"] == "backup.json"
    assert seen["email"] == "user@example.com"
    assert seen["password"] == "Secret123!"
    assert seen["sms_provider"] == ""


def test_get_rt_protocol_executor_cleans_phone_callback_after_failure(monkeypatch):
    class PhoneCallback:
        def __init__(self):
            self.cleaned = False

        def __call__(self):
            return ""

        def cleanup(self):
            self.cleaned = True

    phone_callback = PhoneCallback()

    monkeypatch.setattr(
        ChatGPTPlatform,
        "_build_get_rt_mailbox_otp_callback",
        lambda self, account, log_fn, proxy: (lambda: "123456", ""),
    )

    def fake_run_protocol_get_rt(**_kwargs):
        raise RuntimeError("phone otp submitted but callback failed")

    monkeypatch.setattr(protocol_get_rt_module, "run_protocol_get_rt", fake_run_protocol_get_rt)

    platform = ChatGPTPlatform(RegisterConfig())
    result = platform._handle_get_rt(
        Account(
            platform="chatgpt",
            email="user@example.com",
            password="Secret123!",
        ),
        {
            "executor_type": "protocol",
            "sms_provider": "smspool",
            "phone_callback": phone_callback,
        },
    )

    assert result["ok"] is False
    assert phone_callback.cleaned is True


def test_chatgpt_upload_actions_return_structured_data(monkeypatch):
    from platforms.chatgpt import cpa_upload as cpa_upload_module
    from platforms.chatgpt import sub2api_upload as sub2api_upload_module

    monkeypatch.setattr(cpa_upload_module, "generate_token_json", lambda account: {"email": account.email})
    monkeypatch.setattr(cpa_upload_module, "upload_to_cpa", lambda *args, **kwargs: (True, "CPA ok"))
    monkeypatch.setattr(cpa_upload_module, "upload_to_team_manager", lambda *args, **kwargs: (True, "TM ok"))
    monkeypatch.setattr(sub2api_upload_module, "upload_to_sub2api", lambda *args, **kwargs: (True, "SUB2API ok"))

    platform = ChatGPTPlatform(RegisterConfig())
    account = Account(
        platform="chatgpt",
        email="user@example.com",
        password="Secret123!",
        token="access-token",
        extra={"refresh_token": "refresh-token"},
    )

    sub2api_result = platform.execute_action("upload_sub2api", account, {})
    cpa_result = platform.execute_action("upload_cpa", account, {})
    tm_result = platform.execute_action("upload_tm", account, {})

    assert sub2api_result["ok"] is True
    assert sub2api_result["data"]["upload_target"] == "sub2api"
    assert sub2api_result["data"]["upload_status"] == "uploaded"
    assert cpa_result["data"]["upload_target"] == "cpa"
    assert tm_result["data"]["upload_target"] == "team_manager"
