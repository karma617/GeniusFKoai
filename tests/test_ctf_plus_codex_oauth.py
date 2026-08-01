from __future__ import annotations

from domain.accounts import AccountRecord
from application.ctf_plus import CtfPlusAccountsService


def test_codex_oauth_browser_uses_account_mailbox_otp_callback(monkeypatch):
    seen = {}

    account = AccountRecord(
        id=1,
        platform="chatgpt",
        email="user@example.com",
        password="Secret123!",
        provider_accounts=[{"provider_name": "outlook_email_api", "login_identifier": "user@example.com"}],
        provider_resources=[
            {
                "provider_type": "mailbox",
                "provider_name": "outlook_email_api",
                "resource_type": "mailbox",
                "resource_identifier": "2",
                "handle": "user@example.com",
                "metadata": {"email": "user@example.com", "account_id": "2"},
            }
        ],
    )

    class FakeRepository:
        def get(self, account_id):
            assert account_id == 1
            return account

        def update(self, account_id, command):
            seen["updated"] = (account_id, command)

    class FakeChatGPTPlatform:
        def __init__(self, config=None):
            seen["platform_config"] = config

        def _build_get_rt_mailbox_otp_callback(self, platform_account, log_fn, proxy):
            seen["platform_account_extra"] = dict(platform_account.extra or {})
            return (lambda: "123456"), ""

    class FakeBrowserRegister:
        def __init__(self, **kwargs):
            seen["otp_callback"] = kwargs.get("otp_callback")
            seen["backend_config"] = kwargs.get("backend_config")

        def _retry_oauth_fresh_browser(self, email, password):
            assert seen["otp_callback"]() == "123456"
            return {"access_token": "access-token", "refresh_token": "refresh-token"}

    import platforms.chatgpt.plugin as chatgpt_plugin
    import platforms.chatgpt.browser_register as browser_register

    monkeypatch.setattr(chatgpt_plugin, "ChatGPTPlatform", FakeChatGPTPlatform)
    monkeypatch.setattr(browser_register, "ChatGPTBrowserRegister", FakeBrowserRegister)

    result = CtfPlusAccountsService(FakeRepository()).run_codex_oauth_browser(
        account_id=1,
        browser_mode="camoufox_headed",
        log_fn=lambda _message: None,
    )

    assert result["ok"] is True
    assert seen["platform_account_extra"]["provider_resources"] == account.provider_resources
    assert seen["updated"][0] == 1


def test_codex_oauth_browser_password_totp_skips_mailbox_otp(monkeypatch):
    seen = {}

    account = AccountRecord(
        id=1,
        platform="chatgpt",
        email="user@example.com",
        password="Secret123!",
        credentials=[{"key": "totp_secret", "value": "JBSWY3DPEHPK3PXP"}],
    )

    class FakeRepository:
        def get(self, account_id):
            assert account_id == 1
            return account

        def update(self, account_id, command):
            seen["updated"] = (account_id, command)

    class FakeChatGPTPlatform:
        def __init__(self, config=None):
            raise AssertionError("密码+2FA 登录不应初始化邮箱 OTP 服务")

    class FakeBrowserRegister:
        def __init__(self, **kwargs):
            seen["otp_callback"] = kwargs.get("otp_callback")
            seen["totp_secret"] = kwargs.get("totp_secret")

        def _retry_oauth_fresh_browser(self, email, password):
            assert email == "user@example.com"
            assert password == "Secret123!"
            assert seen["otp_callback"] is None
            assert seen["totp_secret"] == "JBSWY3DPEHPK3PXP"
            return {"access_token": "access-token", "refresh_token": "refresh-token"}

    import platforms.chatgpt.plugin as chatgpt_plugin
    import platforms.chatgpt.browser_register as browser_register

    monkeypatch.setattr(chatgpt_plugin, "ChatGPTPlatform", FakeChatGPTPlatform)
    monkeypatch.setattr(browser_register, "ChatGPTBrowserRegister", FakeBrowserRegister)

    result = CtfPlusAccountsService(FakeRepository()).run_codex_oauth_browser(
        account_id=1,
        browser_mode="camoufox_headed",
        log_fn=lambda _message: None,
    )

    assert result["ok"] is True
    assert seen["updated"][0] == 1
