from __future__ import annotations

from application import tasks as tasks_module
from application.tasks import _mark_get_rt_upload_status
from core.account_graph import load_account_graphs
from core.base_platform import Account, RegisterConfig
from core.db import AccountModel, engine
from domain.actions import ActionExecutionResult
from domain.actions import ActionExecutionCommand
from infrastructure import platform_runtime as runtime_module
from sqlmodel import Session


class _FakeLogger:
    def __init__(self):
        self.events = []
        self.result_data = None
        self.finished = None
        self.cancel_requested = False

    def log(self, message, **kwargs):
        self.events.append(("log", message, kwargs))

    def record_error(self, error):
        self.events.append(("error", error, {}))

    def record_success(self):
        self.events.append(("success", "", {}))

    def set_result_data(self, data):
        self.result_data = data

    def set_progress(self, current, total):
        self.events.append(("progress", current, {"total": total}))

    def is_cancel_requested(self):
        return self.cancel_requested

    def set_subtask(self, subtask_id, label=""):
        self.events.append(("subtask", subtask_id, {"label": label}))

    def clear_subtask(self):
        self.events.append(("clear_subtask", "", {}))

    def finish(self, status, *, error=""):
        self.finished = (status, error)


def test_task_logger_finish_waits_for_smspool_release_queue(monkeypatch):
    from platforms.gopay import sms_channel

    task = tasks_module.create_task(
        task_type=tasks_module.TASK_TYPE_GET_RT,
        platform="chatgpt",
        payload={"ids": [1]},
        progress_total=1,
    )
    drain_calls = []

    monkeypatch.setattr(sms_channel, "get_smspool_release_queue_size", lambda **_kwargs: 1)

    def fake_wait_for_smspool_release_queue_drain(**kwargs):
        drain_calls.append(kwargs)
        return True

    monkeypatch.setattr(
        sms_channel,
        "wait_for_smspool_release_queue_drain",
        fake_wait_for_smspool_release_queue_drain,
    )

    logger = tasks_module.TaskLogger(task["id"])
    logger.finish(tasks_module.TASK_STATUS_FAILED, error="boom")

    assert drain_calls
    assert drain_calls[0]["max_wait_seconds"] == 0
    with Session(engine) as session:
        model = session.get(tasks_module.TaskModel, task["id"])
        assert model.status == tasks_module.TASK_STATUS_FAILED
        assert model.error == "boom"


def test_platform_action_task_passes_task_logger_to_runtime(monkeypatch):
    seen = {}

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check):
            seen["log_fn"] = log_fn
            seen["cancel_check"] = cancel_check
            if log_fn:
                log_fn("checkout step log")
            return ActionExecutionResult(ok=True, data={"message": "summary"})

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "payment_link",
            "params": {"auto_checkout": "true"},
        },
        logger,
    )

    assert getattr(seen["log_fn"], "__self__", None) is logger
    assert getattr(seen["log_fn"], "__name__", "") == "log"
    assert getattr(seen["cancel_check"], "__self__", None) is logger
    assert getattr(seen["cancel_check"], "__name__", "") == "is_cancel_requested"
    assert seen["cancel_check"]() is False
    assert ("log", "checkout step log", {}) in logger.events
    assert logger.result_data == {"message": "summary"}
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_chatgpt_register_task_succeeds_after_successful_registration(monkeypatch):
    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Secret123!",
                user_id="acct_123",
                extra={"access_token": "access-token"},
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr(tasks_module, "_auto_upload_cpa", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_auto_push_any2api", lambda *args, **kwargs: None)

    logger = _FakeLogger()

    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "registered@example.com",
            "password": "Secret123!",
            "extra": {
                "identity_provider": "oauth_browser",
                "auto_chatgpt_plus_payment": False,
            },
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert any(event[0] == "success" for event in logger.events)
    assert not any(
        "cannot access local variable 'extra'" in str(event)
        for event in logger.events
    )


def test_chatgpt_sms_oauth_register_falls_back_to_next_sms_provider(monkeypatch):
    attempts = []

    class FakePlatform:
        def __init__(self, provider: str):
            self.provider = provider

        def register(self, email=None, password=None):
            attempts.append(self.provider)
            if self.provider == "smsbower_api":
                raise RuntimeError("voip_phone_disallowed: Invalid phone number")
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Secret123!",
                user_id="acct_123",
                extra={"access_token": "access-token"},
            )

    def fake_build_platform_instance(platform_name, payload, logger, resolved_proxy=None, shared_mailbox=None):
        provider = str((payload.get("extra") or {}).get("sms_provider") or "")
        return FakePlatform(provider)

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_list_register_sms_provider_candidates",
        lambda payload: [{"provider": "smsbower_api"}, {"provider": "herosms_api"}],
    )
    monkeypatch.setattr(tasks_module, "_build_platform_instance", fake_build_platform_instance)
    monkeypatch.setattr(tasks_module, "save_account", lambda account: account)
    monkeypatch.setattr(tasks_module, "_auto_upload_cpa", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_auto_push_any2api", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda **kwargs: object())

    logger = _FakeLogger()

    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "registered@example.com",
            "password": "Secret123!",
            "extra": {
                "identity_provider": "sms_oauth",
                "mail_provider": "outlook_email_api",
                "auto_chatgpt_plus_payment": False,
            },
        },
        logger,
    )

    assert attempts == ["smsbower_api", "herosms_api"]
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_chatgpt_sms_oauth_register_skips_exhausted_sms_provider(monkeypatch):
    attempts = []

    class FakePlatform:
        def __init__(self, provider: str):
            self.provider = provider

        def register(self, email=None, password=None):
            attempts.append(self.provider)
            if self.provider == "smsbower_api":
                raise RuntimeError("no balance: insufficient balance")
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Secret123!",
                user_id="acct_123",
                extra={"access_token": "access-token"},
            )

    def fake_build_platform_instance(platform_name, payload, logger, resolved_proxy=None, shared_mailbox=None):
        provider = str((payload.get("extra") or {}).get("sms_provider") or "")
        return FakePlatform(provider)

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_list_register_sms_provider_candidates",
        lambda payload: [{"provider": "smsbower_api"}, {"provider": "smspool_api"}],
    )
    monkeypatch.setattr(tasks_module, "_build_platform_instance", fake_build_platform_instance)
    monkeypatch.setattr(tasks_module, "save_account", lambda account: account)
    monkeypatch.setattr(tasks_module, "_auto_upload_cpa", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_auto_push_any2api", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda **kwargs: object())

    logger = _FakeLogger()

    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 2,
            "concurrency": 1,
            "email": "registered@example.com",
            "password": "Secret123!",
            "extra": {
                "identity_provider": "sms_oauth",
                "mail_provider": "outlook_email_api",
                "auto_chatgpt_plus_payment": False,
            },
        },
        logger,
    )

    assert attempts == ["smsbower_api", "smspool_api", "smspool_api"]
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert any("SMS provider exhausted" in str(event[1]) for event in logger.events)


def test_chatgpt_sms_oauth_register_candidates_use_enabled_provider_alias(monkeypatch):
    from infrastructure import provider_settings_repository

    class FakeSetting:
        def __init__(self, provider_key):
            self.provider_key = provider_key

    class FakeSettingsRepo:
        def list_enabled(self, provider_type):
            assert provider_type == "sms"
            return [FakeSetting("smspool_api")]

    monkeypatch.setattr(provider_settings_repository, "ProviderSettingsRepository", FakeSettingsRepo)

    candidates = tasks_module._list_register_sms_provider_candidates(
        {
            "platform": "chatgpt",
            "extra": {
                "identity_provider": "sms_oauth",
                "sms_provider": "smsbower_api",
            },
        }
    )

    assert [item["provider"] for item in candidates] == ["smspool_api"]


def test_chatgpt_sms_oauth_register_keeps_trying_next_provider_after_non_switch_error(monkeypatch):
    attempts = []

    class FakePlatform:
        def __init__(self, provider: str):
            self.provider = provider

        def register(self, email=None, password=None):
            attempts.append(self.provider)
            if self.provider == "smsbower_api":
                raise RuntimeError("cloudflare blocked during registration")
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Secret123!",
                user_id="acct_123",
                extra={"access_token": "access-token"},
            )

    def fake_build_platform_instance(platform_name, payload, logger, resolved_proxy=None, shared_mailbox=None):
        provider = str((payload.get("extra") or {}).get("sms_provider") or "")
        return FakePlatform(provider)

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_list_register_sms_provider_candidates",
        lambda payload: [{"provider": "smsbower_api"}, {"provider": "herosms_api"}],
    )
    monkeypatch.setattr(tasks_module, "_build_platform_instance", fake_build_platform_instance)
    monkeypatch.setattr(tasks_module, "save_account", lambda account: account)
    monkeypatch.setattr(tasks_module, "_auto_upload_cpa", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_auto_push_any2api", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda **kwargs: object())

    logger = _FakeLogger()

    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "registered@example.com",
            "password": "Secret123!",
            "extra": {
                "identity_provider": "sms_oauth",
                "mail_provider": "outlook_email_api",
                "auto_chatgpt_plus_payment": False,
            },
        },
        logger,
    )

    assert attempts == ["smsbower_api", "herosms_api"]
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_chatgpt_sms_oauth_register_fails_only_after_all_sms_providers_are_exhausted(monkeypatch):
    attempts = []

    class FakePlatform:
        def __init__(self, provider: str):
            self.provider = provider

        def register(self, email=None, password=None):
            attempts.append(self.provider)
            if self.provider == "smsbower_api":
                raise RuntimeError("voip_phone_disallowed: Invalid phone number")
            raise RuntimeError("no_balance: insufficient balance")

    def fake_build_platform_instance(platform_name, payload, logger, resolved_proxy=None, shared_mailbox=None):
        provider = str((payload.get("extra") or {}).get("sms_provider") or "")
        return FakePlatform(provider)

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_list_register_sms_provider_candidates",
        lambda payload: [{"provider": "smsbower_api"}, {"provider": "herosms_api"}],
    )
    monkeypatch.setattr(tasks_module, "_build_platform_instance", fake_build_platform_instance)
    monkeypatch.setattr(tasks_module, "_auto_upload_cpa", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_auto_push_any2api", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda **kwargs: object())

    logger = _FakeLogger()

    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "registered@example.com",
            "password": "Secret123!",
            "extra": {
                "identity_provider": "sms_oauth",
                "mail_provider": "outlook_email_api",
                "auto_chatgpt_plus_payment": False,
            },
        },
        logger,
    )

    assert attempts == ["smsbower_api", "herosms_api"]
    assert logger.finished[0] == tasks_module.TASK_STATUS_FAILED
    assert "All enabled SMS providers failed during ChatGPT sms_oauth registration" in logger.finished[1]
    assert "smsbower_api, herosms_api" in logger.finished[1]
    assert "voip_phone_disallowed: Invalid phone number" in logger.finished[1]
    assert "no_balance: insufficient balance" in logger.finished[1]


def test_phone_bind_task_passes_logger_and_browser_mode(monkeypatch):
    seen = {}

    class FakePhoneBindingService:
        def bind(self, **kwargs):
            seen.update(kwargs)
            kwargs["log_fn"]("phone bind step")
            return {"success_count": 1, "failure_count": 0, "phones": []}

    monkeypatch.setattr(tasks_module, "PhoneBindingService", FakePhoneBindingService, raising=False)
    logger = _FakeLogger()

    tasks_module._execute_phone_bind_task(
        {
            "platform": "chatgpt",
            "ids": [123],
            "fallback_ids": [],
            "phone_lines": "7857019646----https://mail-api.yuecheng.shop/api/sms/recordText?key=abc",
            "browser_mode": "camoufox_headed",
            "bit_profile_id": "profile-1",
            "concurrency": 7,
        },
        logger,
    )

    assert seen["ids"] == [123]
    assert seen["browser_mode"] == "camoufox_headed"
    assert seen["bit_profile_id"] == "profile-1"
    assert seen["concurrency"] == 7
    assert getattr(seen["log_fn"], "__self__", None) is logger
    assert ("log", "phone bind step", {}) in logger.events
    assert logger.result_data == {"success_count": 1, "failure_count": 0, "phones": []}
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_codex_oauth_task_passes_logger_and_browser_mode(monkeypatch):
    seen = []

    class FakeCtfPlusAccountsService:
        def run_codex_oauth_browser(self, **kwargs):
            seen.append(kwargs)
            kwargs["log_fn"]("oauth step")
            return {"ok": True, "account_id": kwargs["account_id"], "email": "oauth@test.com"}

    monkeypatch.setattr(tasks_module, "CtfPlusAccountsService", FakeCtfPlusAccountsService, raising=False)
    logger = _FakeLogger()

    tasks_module._execute_codex_oauth_task(
        {
            "ids": [456],
            "browser_mode": "bitbrowser_hidden",
            "bit_profile_id": "profile-2",
            "concurrency": 9,
        },
        logger,
    )

    assert seen[0]["account_id"] == 456
    assert seen[0]["browser_mode"] == "bitbrowser_hidden"
    assert seen[0]["bit_profile_id"] == "profile-2"
    assert getattr(seen[0]["log_fn"], "__self__", None) is logger
    assert any(event[0] == "log" and event[1] == "oauth step" for event in logger.events)
    assert logger.result_data["success_count"] == 1
    assert logger.result_data["concurrency"] == 1
    assert logger.result_data["results"][0]["account_id"] == 456
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_codex_oauth_task_runs_multiple_accounts_without_capping_concurrency(monkeypatch):
    seen = []

    class FakeCtfPlusAccountsService:
        def run_codex_oauth_browser(self, **kwargs):
            seen.append(kwargs["account_id"])
            return {"ok": True, "account_id": kwargs["account_id"], "email": f"{kwargs['account_id']}@test.com"}

    monkeypatch.setattr(tasks_module, "CtfPlusAccountsService", FakeCtfPlusAccountsService, raising=False)
    logger = _FakeLogger()

    tasks_module._execute_codex_oauth_task(
        {
            "ids": [1, 2, 3],
            "browser_mode": "camoufox_headed",
            "concurrency": 99,
        },
        logger,
    )

    assert sorted(seen) == [1, 2, 3]
    assert logger.result_data["total"] == 3
    assert logger.result_data["success_count"] == 3
    assert logger.result_data["failure_count"] == 0
    assert logger.result_data["concurrency"] == 3
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_platform_action_task_finishes_cancelled_without_starting_runtime(monkeypatch):
    class FakeRuntime:
        def execute_action(self, *args, **kwargs):
            raise AssertionError("runtime should not start after cancellation")

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()
    logger.cancel_requested = True

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "payment_link",
            "params": {"auto_checkout": "true"},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")


def test_platform_action_task_marks_cancelled_after_runtime_cancel(monkeypatch):
    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check):
            assert cancel_check() is False
            logger.cancel_requested = True
            return ActionExecutionResult(ok=False, error="任务已取消")

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "payment_link",
            "params": {"auto_checkout": "true"},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")


def test_chatgpt_refresh_token_action_builds_complete_refresh_account(monkeypatch):
    from platforms.chatgpt.plugin import ChatGPTPlatform
    from platforms.chatgpt.token_refresh import TokenRefreshResult

    seen = {}

    class FakeTokenRefreshManager:
        def __init__(self, proxy_url=None):
            seen["proxy_url"] = proxy_url

        def refresh_account(self, account):
            seen["account"] = account
            return TokenRefreshResult(success=True, access_token="new-access", refresh_token="new-refresh")

    monkeypatch.setattr("platforms.chatgpt.token_refresh.TokenRefreshManager", FakeTokenRefreshManager)
    monkeypatch.setattr(
        "platforms.chatgpt.switch.fetch_chatgpt_account_state",
        lambda **_kwargs: {"valid": True},
    )

    platform = ChatGPTPlatform(config=RegisterConfig(executor_type="protocol"))
    account = Account(
        platform="chatgpt",
        email="user@example.com",
        password="Secret123!",
        token="old-access",
        extra={
            "refresh_token": "old-refresh",
            "session_token": "session-token",
            "client_id": "client-1",
            "cookies": "cookie-data",
        },
    )

    result = platform.execute_action("refresh_token", account, {})

    assert result["ok"] is True
    assert result["data"]["access_token"] == "new-access"
    assert result["data"]["refresh_token"] == "new-refresh"
    assert result["data"]["account_state"] == {"valid": True}
    assert seen["account"].email == "user@example.com"
    assert seen["account"].access_token == "old-access"
    assert seen["account"].refresh_token == "old-refresh"
    assert seen["account"].session_token == "session-token"
    assert seen["account"].client_id == "client-1"
    assert seen["account"].cookies == "cookie-data"


def test_chatgpt_auto_plus_followup_generates_payment_link(monkeypatch):
    saved_accounts = []

    class FakeLogger(_FakeLogger):
        def add_cashier_url(self, url):
            self.events.append(("cashier_url", url, {}))

    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {"access_token": "access-token"}

    class FakePlatform:
        def __init__(self):
            self.calls = []

        def execute_action(self, action_id, account, params):
            self.calls.append((action_id, params))
            return {
                "ok": True,
                "data": {
                    "cashier_url": "https://checkout.example/plus",
                    "checkout_url": "https://checkout.example/plus",
                    "message": "Payment link generated.",
                },
            }

    monkeypatch.setattr(tasks_module, "save_account", lambda account: saved_accounts.append(dict(account.extra)))
    logger = FakeLogger()
    platform = FakePlatform()
    account = FakeAccount()

    error = tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={
            "extra": {
                "auto_chatgpt_plus_payment": True,
                "chatgpt_payment": {
                    "country": "US",
                    "currency": "USD",
                    "headless": "true",
                    "checkout_hold_seconds": 0,
                },
            }
        },
        platform=platform,
        account=account,
        logger=logger,
    )

    assert error == ""
    assert platform.calls == [
        (
            "payment_link",
            {
                "plan": "plus",
                "country": "US",
                "currency": "USD",
                "auto_checkout": "true",
                "payment_method": "paypal",
                "headless": "true",
                "checkout_timeout": 180,
                "checkout_hold_seconds": 0,
            },
        )
    ]
    assert account.extra["cashier_url"] == "https://checkout.example/plus"
    assert saved_accounts[-1]["cashier_url"] == "https://checkout.example/plus"
    assert ("cashier_url", "https://checkout.example/plus", {}) in logger.events
    assert account.status == tasks_module.AccountStatus.SUBSCRIBED
    assert account.extra["account_overview"]["plan_state"] == "subscribed"
    assert account.extra["account_overview"]["plan_name"] == "Plus"
    assert "Plus" in account.extra["account_overview"]["chips"]


def test_chatgpt_auto_plus_followup_ppboom_link_does_not_mark_subscribed(monkeypatch):
    saved_accounts = []

    class FakeLogger(_FakeLogger):
        def add_cashier_url(self, url):
            self.events.append(("cashier_url", url, {}))

    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {"access_token": "access-token"}

    class FakePlatform:
        def __init__(self):
            self.calls = []

        def execute_action(self, action_id, account, params):
            self.calls.append((action_id, params))
            return {
                "ok": True,
                "data": {
                    "cashier_url": "https://paypal.example/approve",
                    "paypal_authorize_url": "https://paypal.example/approve",
                    "checkout_mode": "ppboom",
                    "subscription_submitted": False,
                    "ppboom": {"ok": True},
                },
            }

    monkeypatch.setattr(tasks_module, "save_account", lambda account: saved_accounts.append(dict(account.extra)))
    logger = FakeLogger()
    platform = FakePlatform()
    account = FakeAccount()

    error = tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={
            "extra": {
                "auto_chatgpt_plus_payment": True,
                "chatgpt_payment": {
                    "country": "US",
                    "currency": "USD",
                    "use_ppboom": "true",
                    "ppboom_base_url": "http://127.0.0.1:8787",
                    "ppboom_max_attempts": 20,
                },
            }
        },
        platform=platform,
        account=account,
        logger=logger,
    )

    assert error == ""
    assert platform.calls[0][0] == "payment_link"
    assert platform.calls[0][1]["use_ppboom"] == "true"
    assert platform.calls[0][1]["ppboom_base_url"] == "http://127.0.0.1:8787"
    assert platform.calls[0][1]["ppboom_max_attempts"] == 20
    assert account.extra["cashier_url"] == "https://paypal.example/approve"
    assert account.extra["subscription_submitted"] is False
    assert "account_overview" not in account.extra
    assert not hasattr(account, "status")
    assert saved_accounts[-1]["cashier_url"] == "https://paypal.example/approve"
    assert ("cashier_url", "https://paypal.example/approve", {}) in logger.events


def test_chatgpt_auto_plus_followup_logs_paypal_authorize_url_when_available(monkeypatch):
    class FakeLogger(_FakeLogger):
        def add_cashier_url(self, url):
            self.events.append(("cashier_url", url, {}))

    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {"access_token": "access-token"}

    class FakePlatform:
        def execute_action(self, action_id, account, params):
            return {
                "ok": True,
                "data": {
                    "cashier_url": "https://pay.openai.com/c/pay/cs_live_demo",
                    "checkout_url": "https://pm-redirects.stripe.com/authorize/acct_x/sa_nonce_y",
                    "paypal_authorize_url": "https://pm-redirects.stripe.com/authorize/acct_x/sa_nonce_y",
                    "paypal_protocol_extract": {"ok": True},
                },
            }

    monkeypatch.setattr(tasks_module, "save_account", lambda account: None)
    logger = FakeLogger()
    account = FakeAccount()

    error = tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={"extra": {"auto_chatgpt_plus_payment": True}},
        platform=FakePlatform(),
        account=account,
        logger=logger,
    )

    assert error == ""
    assert (
        "cashier_url",
        "https://pm-redirects.stripe.com/authorize/acct_x/sa_nonce_y",
        {},
    ) in logger.events
    assert any(
        event[0] == "log" and "原始 cashier_url: https://pay.openai.com/c/pay/cs_live_demo" in event[1]
        for event in logger.events
    )
    assert account.extra["cashier_url"] == "https://pay.openai.com/c/pay/cs_live_demo"


def test_chatgpt_auto_plus_followup_forwards_checkout_mode_and_record_har(monkeypatch):
    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {"access_token": "access-token"}

    class FakeLogger(_FakeLogger):
        def add_cashier_url(self, url):
            self.events.append(("cashier_url", url, {}))

    class FakePlatform:
        def __init__(self):
            self.calls = []

        def execute_action(self, action_id, account, params):
            self.calls.append((action_id, dict(params)))
            return {"ok": True, "data": {"cashier_url": "https://checkout.example/plus"}}

    monkeypatch.setattr(tasks_module, "save_account", lambda account: None)
    logger = FakeLogger()
    platform = FakePlatform()
    account = FakeAccount()

    tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={
            "extra": {
                "auto_chatgpt_plus_payment": True,
                "chatgpt_payment": {
                    "country": "US",
                    "currency": "USD",
                    "headless": "false",
                    "checkout_mode": "camoufox_headed",
                    "record_har": "true",
                },
            }
        },
        platform=platform,
        account=account,
        logger=logger,
    )

    assert len(platform.calls) == 1
    forwarded = platform.calls[0][1]
    assert forwarded["checkout_mode"] == "camoufox_headed"
    assert forwarded["record_har"] == "true"


def test_chatgpt_auto_plus_followup_omits_unset_checkout_mode_and_record_har(monkeypatch):
    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {}

    class FakePlatform:
        def __init__(self):
            self.calls = []

        def execute_action(self, action_id, account, params):
            self.calls.append((action_id, dict(params)))
            return {"ok": True, "data": {}}

    monkeypatch.setattr(tasks_module, "save_account", lambda account: None)
    logger = _FakeLogger()
    platform = FakePlatform()
    account = FakeAccount()

    tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={
            "extra": {
                "auto_chatgpt_plus_payment": True,
                "chatgpt_payment": {"country": "US", "currency": "USD"},
            }
        },
        platform=platform,
        account=account,
        logger=logger,
    )

    forwarded = platform.calls[0][1]
    assert "checkout_mode" not in forwarded
    assert "record_har" not in forwarded


def test_get_rt_task_forwards_record_har_to_platform_action(monkeypatch):
    seen_params = []

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            seen_params.append(dict(command.params))
            return ActionExecutionResult(ok=True, data={"message": "ok"})

    monkeypatch.setattr(tasks_module, "_filter_registered_get_rt_ids", lambda ids, *, platform="chatgpt": (list(ids), []))
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [123],
            "browser_mode": "camoufox_headed",
            "record_har": "true",
            "concurrency": 1,
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert seen_params[0]["record_har"] == "true"
    assert seen_params[0]["sms_provider"] == "default"
    assert seen_params[0]["executor_type"] == "browser"


def test_get_rt_task_forwards_executor_type_to_platform_action(monkeypatch):
    seen_params = []

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            seen_params.append(dict(command.params))
            return ActionExecutionResult(ok=True, data={"message": "ok"})

    monkeypatch.setattr(tasks_module, "_filter_registered_get_rt_ids", lambda ids, *, platform="chatgpt": (list(ids), []))
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [123],
            "browser_mode": "camoufox_headed",
            "executor_type": "protocol",
            "concurrency": 1,
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert seen_params[0]["executor_type"] == "protocol"


def test_get_rt_api_request_preserves_executor_type():
    from api.task_commands import GetRtTaskRequest

    body = GetRtTaskRequest(
        platform="chatgpt",
        ids=[123],
        executor_type="protocol",
        browser_mode="camoufox_headless",
    )

    assert body.model_dump()["executor_type"] == "protocol"


def test_get_rt_api_request_preserves_task_mode():
    from api.task_commands import GetRtTaskRequest

    body = GetRtTaskRequest(platform="chatgpt", ids=[123], task_mode="target")

    assert body.model_dump()["task_mode"] == "target"


def test_auto_upload_sub2api_retries_request_exception_six_times(monkeypatch):
    from core import config_store
    from platforms.chatgpt import sub2api_upload

    calls = []

    monkeypatch.setattr(config_store.config_store, "get", lambda key, default="": "https://sub2api.example" if key == "sub2api_url" else default)
    monkeypatch.setattr(tasks_module.time, "sleep", lambda _seconds: None)

    def fake_upload(_target):
        calls.append(1)
        return False, "SUB2API 请求异常：curl: (35) TLS connect error"

    monkeypatch.setattr(sub2api_upload, "upload_to_sub2api", fake_upload)
    logger = _FakeLogger()
    account = Account(
        platform="chatgpt",
        email="rt@test.com",
        password="Secret123!",
        extra={
            "access_token": "access-token",
            "refresh_token": "refresh-token",
        },
    )

    result = tasks_module._auto_upload_sub2api(logger, account)

    assert result is False
    assert len(calls) == 6
    assert any("重试 6 次仍失败" in str(event[1]) for event in logger.events)


def test_mark_get_rt_upload_status_persists_new_lifecycle_status():
    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="rt-status@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id or 0)

    _mark_get_rt_upload_status(account_id, uploaded=False, upload_message="SUB2API 上传失败")
    with Session(engine) as session:
        graph = load_account_graphs(session, [account_id])[account_id]
        assert graph["lifecycle_status"] == "rt_pending_upload"
        assert graph["display_status"] == "rt_pending_upload"
        assert graph["overview"]["rt_upload_status"] == "pending_upload"

    _mark_get_rt_upload_status(account_id, uploaded=True, upload_message="SUB2API 上传成功")
    with Session(engine) as session:
        graph = load_account_graphs(session, [account_id])[account_id]
        assert graph["lifecycle_status"] == "rt_uploaded"
        assert graph["display_status"] == "rt_uploaded"
        assert graph["overview"]["rt_upload_status"] == "uploaded"


def test_get_rt_sms_provider_aliases_are_normalized():
    assert tasks_module._normalize_get_rt_sms_provider("smspool_api") == "smspool"
    assert tasks_module._normalize_get_rt_sms_provider("sms_pool_api") == "smspool"
    assert tasks_module._normalize_get_rt_sms_provider("sms_api") == "smsapi"


def test_register_sms_provider_switch_error_matches_smspool_price_pool_unavailable():
    assert tasks_module._is_register_sms_provider_switch_error(
        "SMSPool purchase returned unusable response: We could not find a suitable pool below the price of: 0.02"
    )


def test_register_sms_provider_switch_error_matches_phone_identity_stalled():
    assert tasks_module._is_register_sms_provider_switch_error(
        "PHONE_FIRST_PROVIDER_SWITCH: phone identity submit did not advance after phone entry"
    )


def test_get_rt_sms_provider_switch_error_matches_smspool_price_pool_unavailable():
    assert tasks_module._is_get_rt_sms_provider_switch_error(
        "SMSPool purchase returned unusable response: We could not find a suitable pool below the price of: 0.02"
    )


def test_get_rt_target_sms_candidates_ignore_disabled_providers_even_with_payload_keys(monkeypatch):
    from infrastructure import provider_settings_repository

    class FakeSetting:
        def __init__(self, provider_key, *, enabled=True, is_default=False):
            self.provider_key = provider_key
            self.enabled = enabled
            self.is_default = is_default

    class FakeSettingsRepo:
        def get_by_key(self, provider_type, provider_key):
            assert provider_type == "sms"
            if provider_key == "smsbower_api":
                return FakeSetting("smsbower_api", enabled=True, is_default=True)
            if provider_key == "herosms_api":
                return FakeSetting("herosms_api", enabled=False)
            if provider_key == "smspool_api":
                return FakeSetting("smspool_api", enabled=False)
            return None

        def resolve_runtime_settings(self, provider_type, provider_key, overrides=None):
            assert provider_type == "sms"
            if provider_key == "smsbower_api":
                return {
                    "smsbower_api_key": "SMSBOWER_KEY",
                    "smsbower_default_country": "76",
                    "smsbower_default_service": "chatgpt",
                }
            if provider_key == "herosms_api":
                return {"herosms_api_key": "HEROSMS_KEY"}
            if provider_key == "smspool_api":
                return {"smspool_api_key": "SMSPOOL_KEY"}
            return {}

        def list_enabled(self, provider_type):
            assert provider_type == "sms"
            return [FakeSetting("smsbower_api", enabled=True, is_default=True)]

    monkeypatch.setattr(provider_settings_repository, "ProviderSettingsRepository", FakeSettingsRepo)

    sms_runtime = {
        "sms_provider": "smsbower_api",
        "settings_provider_key": "smsbower_api",
    }
    candidates = tasks_module._list_get_rt_sms_provider_candidates(
        {
            "sms_provider": "smsbower_api",
            "herosms_api_key": "HEROSMS_KEY_FROM_PAYLOAD",
            "smspool_api_key": "SMSPOOL_KEY_FROM_PAYLOAD",
        },
        sms_runtime,
    )

    assert [item["provider"] for item in candidates] == ["smsbower_api"]


def test_create_get_rt_task_filters_non_registered_ids(monkeypatch):
    captured = {}

    def fake_filter(ids, *, platform="chatgpt"):
        assert ids == [1, 2, 3]
        assert platform == "chatgpt"
        return [2], [1, 3]

    def fake_create_task(**kwargs):
        captured.update(kwargs)
        return {"id": "task-1"}

    monkeypatch.setattr(tasks_module, "_filter_registered_get_rt_ids", fake_filter)
    monkeypatch.setattr(tasks_module, "create_task", fake_create_task)

    result = tasks_module.create_get_rt_task({"platform": "chatgpt", "ids": [1, 2, 3]})

    assert result == {"id": "task-1"}
    assert captured["payload"]["ids"] == [2]
    assert captured["payload"]["account_id"] == 0
    assert captured["payload"]["skipped_non_registered_ids"] == [1, 3]
    assert captured["progress_total"] == 1


def test_execute_get_rt_task_filters_non_registered_ids(monkeypatch):
    seen_account_ids = []

    def fake_filter(ids, *, platform="chatgpt"):
        assert ids == [1, 2, 3]
        return [2], [1, 3]

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            seen_account_ids.append(command.account_id)
            return ActionExecutionResult(ok=True, data={"message": "ok"})

    monkeypatch.setattr(tasks_module, "_filter_registered_get_rt_ids", fake_filter)
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [1, 2, 3],
            "browser_mode": "camoufox_headed",
            "concurrency": 3,
        },
        logger,
    )

    assert seen_account_ids == [2]
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert any("过滤" in str(event[1]) for event in logger.events)


def test_get_rt_task_allows_explicit_sms_disable(monkeypatch):
    seen_params = []

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            seen_params.append(dict(command.params))
            return ActionExecutionResult(ok=True, data={"message": "ok"})

    monkeypatch.setattr(tasks_module, "_filter_registered_get_rt_ids", lambda ids, *, platform="chatgpt": (list(ids), []))
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [123],
            "browser_mode": "camoufox_headed",
            "sms_provider": "none",
            "concurrency": 1,
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert seen_params[0]["sms_provider"] == ""


def test_get_rt_task_uses_shared_phone_reuse_pool(monkeypatch):
    from infrastructure import provider_settings_repository
    from platforms.chatgpt import browser_get_rt as browser_get_rt_module

    built = []
    callbacks = []

    class FakeSetting:
        provider_key = "smspool_api"
        enabled = True
        is_default = True

    class FakeSettingsRepo:
        def get_by_key(self, provider_type, provider_key):
            assert provider_type == "sms"
            if provider_key == "smspool_api":
                return FakeSetting()
            return None

        def resolve_runtime_settings(self, provider_type, provider_key, overrides=None):
            assert provider_type == "sms"
            assert provider_key == "smspool_api"
            return {"smspool_api_key": "KEY"}

        def list_enabled(self, provider_type):
            assert provider_type == "sms"
            return [FakeSetting()]

    class FakePhonePool:
        def __init__(self):
            self.cleaned = False

        def make_callback(self, *, label=""):
            callback = lambda: f"phone-for-{label}"
            callbacks.append((label, callback))
            return callback

        def cleanup(self):
            self.cleaned = True

    fake_pool = FakePhonePool()

    def fake_build_pool(**kwargs):
        built.append(dict(kwargs))
        return fake_pool, ""

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            assert callable(command.params["phone_callback"])
            assert command.params["phone_reuse_count"] == "3"
            return ActionExecutionResult(ok=True, data={"phone": command.params["phone_callback"]()})

    monkeypatch.setattr(tasks_module, "_filter_registered_get_rt_ids", lambda ids, *, platform="chatgpt": (list(ids), []))
    monkeypatch.setattr(provider_settings_repository, "ProviderSettingsRepository", FakeSettingsRepo)
    monkeypatch.setattr(browser_get_rt_module, "build_get_rt_phone_reuse_pool", fake_build_pool)
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [101, 102, 103],
            "browser_mode": "camoufox_headed",
            "sms_provider": "smspool",
            "smspool_api_key": "KEY",
            "phone_reuse_count": 2,
            "concurrency": 1,
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert len(built) == 1
    assert built[0]["reuse_count"] == 3
    assert len(callbacks) == 3
    assert callbacks[0][0] == "1/3"
    assert callbacks[-1][0] == "3/3"
    assert fake_pool.cleaned is True


def test_get_rt_task_uses_saved_default_smspool_settings(monkeypatch):
    from platforms.chatgpt import browser_get_rt as browser_get_rt_module

    built = []
    seen_params = []

    class FakeSettingsRepo:
        def get_default_provider_key(self, provider_type):
            assert provider_type == "sms"
            return "smspool_api"

        def resolve_runtime_settings(self, provider_type, provider_key, overrides=None):
            assert provider_type == "sms"
            assert provider_key == "smspool_api"
            return {
                "smspool_api_key": "SAVED_KEY",
                "smspool_max_price": "0.08",
                "smspool_default_country": "9",
                "smspool_default_service": "671",
                "smspool_base_url": "https://api.example.test",
                "smspool_compat_base_url": "https://compat.example.test",
                "smspool_pricing_option": "0",
                "sms_poll_interval": "2",
            }

    class FakePhonePool:
        def make_callback(self, *, label=""):
            return lambda: "+15550000001"

        def cleanup(self):
            pass

    def fake_build_pool(**kwargs):
        built.append(dict(kwargs))
        return FakePhonePool(), ""

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            seen_params.append(dict(command.params))
            return ActionExecutionResult(ok=True, data={"message": "ok"})

    monkeypatch.setattr(tasks_module, "_filter_registered_get_rt_ids", lambda ids, *, platform="chatgpt": (list(ids), []))
    monkeypatch.setattr(tasks_module, "ProviderSettingsRepository", FakeSettingsRepo, raising=False)
    monkeypatch.setattr(
        "infrastructure.provider_settings_repository.ProviderSettingsRepository",
        FakeSettingsRepo,
    )
    monkeypatch.setattr(browser_get_rt_module, "build_get_rt_phone_reuse_pool", fake_build_pool)
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [123],
            "browser_mode": "camoufox_headed",
            "sms_provider": "default",
            "concurrency": 1,
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert built[0]["sms_provider"] == "smspool"
    assert built[0]["smspool_api_key"] == "SAVED_KEY"
    assert built[0]["smspool_max_price"] == "0.08"
    assert built[0]["smspool_country"] == "9"
    assert built[0]["smspool_service"] == "671"
    assert built[0]["smspool_base_url"] == "https://api.example.test"
    assert built[0]["smspool_compat_base_url"] == "https://compat.example.test"
    assert built[0]["smspool_pricing_option"] == "0"
    assert built[0]["smspool_poll_interval"] == "2"
    assert seen_params[0]["sms_provider"] == "smspool"
    assert seen_params[0]["smspool_api_key"] == "SAVED_KEY"
    assert seen_params[0]["smspool_max_price"] == "0.08"
    assert seen_params[0]["smspool_country"] == "9"
    assert seen_params[0]["smspool_service"] == "671"
    assert seen_params[0]["smspool_base_url"] == "https://api.example.test"
    assert seen_params[0]["smspool_compat_base_url"] == "https://compat.example.test"
    assert seen_params[0]["smspool_pricing_option"] == "0"
    assert seen_params[0]["smspool_poll_interval"] == "2"


def test_get_rt_target_mode_retries_existing_rt_upload_until_success(monkeypatch):
    from core.account_graph import patch_account_graph

    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="target-upload@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)
        patch_account_graph(
            session,
            model,
            lifecycle_status="rt_pending_upload",
            summary_updates={"display_status": "rt_pending_upload"},
            credential_updates={"refresh_token": "refresh-token", "access_token": "access-token"},
        )
        session.commit()
        account_id = int(model.id or 0)

    upload_calls = []

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            raise AssertionError("existing refresh_token should retry upload before OAuth")

    def fake_upload(_logger, _account):
        upload_calls.append(1)
        return len(upload_calls) >= 2

    monkeypatch.setattr(tasks_module, "_filter_get_rt_target_ids", lambda ids, *, platform="chatgpt": (list(ids), []))
    monkeypatch.setattr(tasks_module, "_auto_upload_sub2api", fake_upload)
    monkeypatch.setattr(tasks_module, "_is_sub2api_configured", lambda: True)
    monkeypatch.setattr(tasks_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [account_id],
            "task_mode": "target",
            "sms_provider": "none",
            "concurrency": 1,
        },
        logger,
    )

    assert upload_calls == [1, 1]
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success_count"] == 1


def test_get_rt_target_mode_switches_sms_provider_after_balance_error(monkeypatch):
    from infrastructure import provider_settings_repository

    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="target-sms@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id or 0)

    runtime_sms_providers = []

    class FakeSetting:
        def __init__(self, provider_key, *, enabled=True, is_default=False, setting_id=1):
            self.provider_key = provider_key
            self.enabled = enabled
            self.is_default = is_default
            self.id = setting_id

    class FakeSettingsRepo:
        def get_default_provider_key(self, provider_type):
            assert provider_type == "sms"
            return "smspool_api"

        def get_by_key(self, provider_type, provider_key):
            assert provider_type == "sms"
            if provider_key == "smspool_api":
                return FakeSetting("smspool_api", is_default=True, setting_id=1)
            if provider_key == "smsbower_api":
                return FakeSetting("smsbower_api", setting_id=2)
            return None

        def resolve_runtime_settings(self, provider_type, provider_key, overrides=None):
            assert provider_type == "sms"
            if provider_key == "smspool_api":
                return {
                    "smspool_api_key": "SMSPOOL_KEY",
                    "smspool_max_price": "0.08",
                    "smspool_default_country": "9",
                    "smspool_default_service": "671",
                }
            if provider_key == "smsbower_api":
                return {
                    "smsbower_api_key": "SMSBOWER_KEY",
                    "smsbower_default_country": "6",
                    "smsbower_default_service": "chatgpt",
                }
            return {}

        def list_enabled(self, provider_type):
            assert provider_type == "sms"
            return [
                FakeSetting("smspool_api", is_default=True, setting_id=1),
                FakeSetting("smsbower_api", setting_id=2),
            ]

    class FakePhonePool:
        def make_callback(self, *, label=""):
            return lambda: "+15550000001"

        def cleanup(self):
            pass

    def fake_build_pool(**kwargs):
        return FakePhonePool(), ""

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            provider = command.params["sms_provider"]
            runtime_sms_providers.append(provider)
            if provider == "smspool":
                return ActionExecutionResult(ok=False, error="SMSPool purchase failed: insufficient balance")
            return ActionExecutionResult(ok=True, data={"message": "ok"})

    monkeypatch.setattr(provider_settings_repository, "ProviderSettingsRepository", FakeSettingsRepo)
    monkeypatch.setattr(tasks_module, "_filter_get_rt_target_ids", lambda ids, *, platform="chatgpt": (list(ids), []))
    monkeypatch.setattr(tasks_module, "_auto_upload_sub2api", lambda _logger, _account: True)
    monkeypatch.setattr(tasks_module, "_is_sub2api_configured", lambda: True)
    monkeypatch.setattr(tasks_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    from platforms.chatgpt import browser_get_rt as browser_get_rt_module

    monkeypatch.setattr(browser_get_rt_module, "build_get_rt_phone_reuse_pool", fake_build_pool)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [account_id],
            "task_mode": "target",
            "sms_provider": "default",
            "concurrency": 1,
        },
        logger,
    )

    assert runtime_sms_providers == ["smspool", "smsbower_api"]
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success_count"] == 1


def test_get_rt_target_mode_switches_sms_provider_after_smspool_purchase_rate_limit(monkeypatch):
    from infrastructure import provider_settings_repository

    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="target-sms-ratelimit@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id or 0)

    runtime_sms_providers = []

    class FakeSetting:
        def __init__(self, provider_key, *, enabled=True, is_default=False, setting_id=1):
            self.provider_key = provider_key
            self.enabled = enabled
            self.is_default = is_default
            self.id = setting_id

    class FakeSettingsRepo:
        def get_default_provider_key(self, provider_type):
            assert provider_type == "sms"
            return "smspool_api"

        def get_by_key(self, provider_type, provider_key):
            assert provider_type == "sms"
            if provider_key == "smspool_api":
                return FakeSetting("smspool_api", is_default=True, setting_id=1)
            if provider_key == "smsbower_api":
                return FakeSetting("smsbower_api", setting_id=2)
            return None

        def resolve_runtime_settings(self, provider_type, provider_key, overrides=None):
            assert provider_type == "sms"
            if provider_key == "smspool_api":
                return {"smspool_api_key": "SMSPOOL_KEY", "smspool_default_country": "9", "smspool_default_service": "671"}
            if provider_key == "smsbower_api":
                return {"smsbower_api_key": "SMSBOWER_KEY", "smsbower_default_country": "6", "smsbower_default_service": "chatgpt"}
            return {}

        def list_enabled(self, provider_type):
            assert provider_type == "sms"
            return [
                FakeSetting("smspool_api", is_default=True, setting_id=1),
                FakeSetting("smsbower_api", setting_id=2),
            ]

    class FakePhonePool:
        def make_callback(self, *, label=""):
            return lambda: "+15550000001"

        def cleanup(self):
            pass

    def fake_build_pool(**kwargs):
        return FakePhonePool(), ""

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            provider = command.params["sms_provider"]
            runtime_sms_providers.append(provider)
            if provider == "smspool":
                return ActionExecutionResult(
                    ok=False,
                    error=(
                        "SMSPool 购号失败: You have made too many failed purchases, "
                        "please improve your success rate and try again in 6 hours. "
                        "You can circumvent this by opening a business account in your settings page "
                        "for an increased ratelimit."
                    ),
                )
            return ActionExecutionResult(ok=True, data={"message": "ok"})

    monkeypatch.setattr(provider_settings_repository, "ProviderSettingsRepository", FakeSettingsRepo)
    monkeypatch.setattr(tasks_module, "_filter_get_rt_target_ids", lambda ids, *, platform="chatgpt": (list(ids), []))
    monkeypatch.setattr(tasks_module, "_auto_upload_sub2api", lambda _logger, _account: True)
    monkeypatch.setattr(tasks_module, "_is_sub2api_configured", lambda: True)
    monkeypatch.setattr(tasks_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    from platforms.chatgpt import browser_get_rt as browser_get_rt_module

    monkeypatch.setattr(browser_get_rt_module, "build_get_rt_phone_reuse_pool", fake_build_pool)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [account_id],
            "task_mode": "target",
            "sms_provider": "default",
            "concurrency": 1,
        },
        logger,
    )

    assert runtime_sms_providers == ["smspool", "smsbower_api"]
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success_count"] == 1


def test_get_rt_target_mode_switches_sms_provider_after_smsbower_network_error(monkeypatch):
    from infrastructure import provider_settings_repository

    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="target-sms-network@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id or 0)

    runtime_sms_providers = []

    class FakeSetting:
        def __init__(self, provider_key, *, enabled=True, is_default=False, setting_id=1):
            self.provider_key = provider_key
            self.enabled = enabled
            self.is_default = is_default
            self.id = setting_id

    class FakeSettingsRepo:
        def get_default_provider_key(self, provider_type):
            assert provider_type == "sms"
            return "smsbower_api"

        def get_by_key(self, provider_type, provider_key):
            assert provider_type == "sms"
            if provider_key == "smsbower_api":
                return FakeSetting("smsbower_api", is_default=True, setting_id=1)
            if provider_key == "smspool_api":
                return FakeSetting("smspool_api", setting_id=2)
            return None

        def resolve_runtime_settings(self, provider_type, provider_key, overrides=None):
            assert provider_type == "sms"
            if provider_key == "smsbower_api":
                return {"smsbower_api_key": "SMSBOWER_KEY", "smsbower_default_country": "6", "smsbower_default_service": "chatgpt"}
            if provider_key == "smspool_api":
                return {"smspool_api_key": "SMSPOOL_KEY", "smspool_default_country": "9", "smspool_default_service": "671"}
            return {}

        def list_enabled(self, provider_type):
            assert provider_type == "sms"
            return [
                FakeSetting("smsbower_api", is_default=True, setting_id=1),
                FakeSetting("smspool_api", setting_id=2),
            ]

    class FakePhonePool:
        def make_callback(self, *, label=""):
            return lambda: "+15550000001"

        def cleanup(self):
            pass

    def fake_build_pool(**kwargs):
        return FakePhonePool(), ""

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            provider = command.params["sms_provider"]
            runtime_sms_providers.append(provider)
            if provider == "smsbower_api":
                return ActionExecutionResult(
                    ok=False,
                    error=(
                        "HTTPSConnectionPool(host='smsbower.page', port=443): Max retries exceeded with url: "
                        "/stubs/handler_api.php?action=getBalance&api_key=SMSBOWER_KEY "
                        "(Caused by SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred "
                        "in violation of protocol (_ssl.c:1032)')))"
                    ),
                )
            return ActionExecutionResult(ok=True, data={"message": "ok"})

    monkeypatch.setattr(provider_settings_repository, "ProviderSettingsRepository", FakeSettingsRepo)
    monkeypatch.setattr(tasks_module, "_filter_get_rt_target_ids", lambda ids, *, platform="chatgpt": (list(ids), []))
    monkeypatch.setattr(tasks_module, "_auto_upload_sub2api", lambda _logger, _account: True)
    monkeypatch.setattr(tasks_module, "_is_sub2api_configured", lambda: True)
    monkeypatch.setattr(tasks_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    from platforms.chatgpt import browser_get_rt as browser_get_rt_module

    monkeypatch.setattr(browser_get_rt_module, "build_get_rt_phone_reuse_pool", fake_build_pool)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [account_id],
            "task_mode": "target",
            "sms_provider": "default",
            "concurrency": 1,
        },
        logger,
    )

    assert runtime_sms_providers == ["smsbower_api", "smspool"]
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success_count"] == 1


def test_get_rt_target_mode_switches_sms_provider_after_phone_verification_rate_limit(monkeypatch):
    from infrastructure import provider_settings_repository

    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="target-phone-limit@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id or 0)

    runtime_sms_providers = []

    class FakeSetting:
        def __init__(self, provider_key, *, enabled=True, is_default=False, setting_id=1):
            self.provider_key = provider_key
            self.enabled = enabled
            self.is_default = is_default
            self.id = setting_id

    class FakeSettingsRepo:
        def get_default_provider_key(self, provider_type):
            assert provider_type == "sms"
            return "codex_sms_pool"

        def get_by_key(self, provider_type, provider_key):
            assert provider_type == "sms"
            if provider_key == "codex_sms_pool":
                return FakeSetting("codex_sms_pool", is_default=True, setting_id=1)
            if provider_key == "smsbower_api":
                return FakeSetting("smsbower_api", setting_id=2)
            return None

        def resolve_runtime_settings(self, provider_type, provider_key, overrides=None):
            assert provider_type == "sms"
            if provider_key == "codex_sms_pool":
                return {"codex_sms_pool_text": "+15550000001|https://sms.example.test/one"}
            if provider_key == "smsbower_api":
                return {"smsbower_api_key": "SMSBOWER_KEY", "smsbower_default_country": "6", "smsbower_default_service": "chatgpt"}
            return {}

        def list_enabled(self, provider_type):
            assert provider_type == "sms"
            return [
                FakeSetting("codex_sms_pool", is_default=True, setting_id=1),
                FakeSetting("smsbower_api", setting_id=2),
            ]

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            provider = command.params["sms_provider"]
            runtime_sms_providers.append(provider)
            if provider == "codex_sms_pool":
                return ActionExecutionResult(
                    ok=False,
                    error=(
                        "get_rt protocol add_phone failed: You've made too many phone verification requests. "
                        "rate_limit_exceeded"
                    ),
                )
            return ActionExecutionResult(ok=True, data={"message": "ok"})

    monkeypatch.setattr(provider_settings_repository, "ProviderSettingsRepository", FakeSettingsRepo)
    monkeypatch.setattr(tasks_module, "_filter_get_rt_target_ids", lambda ids, *, platform="chatgpt": (list(ids), []))
    monkeypatch.setattr(tasks_module, "_auto_upload_sub2api", lambda _logger, _account: True)
    monkeypatch.setattr(tasks_module, "_is_sub2api_configured", lambda: True)
    monkeypatch.setattr(tasks_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [account_id],
            "task_mode": "target",
            "sms_provider": "default",
            "concurrency": 1,
        },
        logger,
    )

    assert runtime_sms_providers == ["codex_sms_pool", "smsbower_api"]
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success_count"] == 1


def test_get_rt_target_mode_retries_after_phone_otp_429(monkeypatch):
    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="target-429@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id or 0)

    calls = []

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            calls.append(command.account_id)
            if len(calls) == 1:
                return ActionExecutionResult(
                    ok=False,
                    error="获取rt协议模式手机验证码校验失败: HTTP 429 rate_limit_exceeded",
                )
            return ActionExecutionResult(ok=True, data={"message": "ok"})

    monkeypatch.setattr(tasks_module, "_filter_get_rt_target_ids", lambda ids, *, platform="chatgpt": (list(ids), []))
    monkeypatch.setattr(tasks_module, "_auto_upload_sub2api", lambda _logger, _account: True)
    monkeypatch.setattr(tasks_module, "_is_sub2api_configured", lambda: True)
    monkeypatch.setattr(tasks_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [account_id],
            "task_mode": "target",
            "sms_provider": "none",
            "concurrency": 1,
        },
        logger,
    )

    assert calls == [account_id, account_id]
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success_count"] == 1
    assert any("可恢复失败" in str(event[1]) for event in logger.events)


def test_get_rt_target_mode_retries_after_browser_whatsapp_phone_rejection(monkeypatch):
    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="target-whatsapp@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id or 0)

    calls = []

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            calls.append(command.account_id)
            if len(calls) == 1:
                return ActionExecutionResult(
                    ok=False,
                    error=(
                        "获取rt失败: PHONE_REJECTED_RETRYABLE: We couldn't send a text message to this phone number, "
                        "so we switched to WhatsApp. Continue to send a verification code on WhatsApp."
                    ),
                )
            return ActionExecutionResult(ok=True, data={"message": "ok"})

    monkeypatch.setattr(tasks_module, "_filter_get_rt_target_ids", lambda ids, *, platform="chatgpt": (list(ids), []))
    monkeypatch.setattr(tasks_module, "_auto_upload_sub2api", lambda _logger, _account: True)
    monkeypatch.setattr(tasks_module, "_is_sub2api_configured", lambda: True)
    monkeypatch.setattr(tasks_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [account_id],
            "task_mode": "target",
            "sms_provider": "none",
            "concurrency": 1,
        },
        logger,
    )

    assert calls == [account_id, account_id]
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success_count"] == 1
    assert any("可恢复失败" in str(event[1]) for event in logger.events)


def test_get_rt_target_mode_retries_after_protocol_phone_change_limit(monkeypatch):
    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="target-phone-limit-retry@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id or 0)

    calls = []

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            calls.append(command.account_id)
            if len(calls) == 1:
                return ActionExecutionResult(
                    ok=False,
                    error="\u83b7\u53d6rt\u534f\u8bae\u6a21\u5f0f\u624b\u673a\u53f7\u63d0\u4ea4\u5931\u8d25\uff0c\u5df2\u8fbe\u6362\u53f7\u4e0a\u9650: NO_NUMBERS",
                )
            return ActionExecutionResult(ok=True, data={"message": "ok"})

    monkeypatch.setattr(tasks_module, "_filter_get_rt_target_ids", lambda ids, *, platform="chatgpt": (list(ids), []))
    monkeypatch.setattr(tasks_module, "_auto_upload_sub2api", lambda _logger, _account: True)
    monkeypatch.setattr(tasks_module, "_is_sub2api_configured", lambda: True)
    monkeypatch.setattr(tasks_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [account_id],
            "task_mode": "target",
            "sms_provider": "none",
            "concurrency": 1,
        },
        logger,
    )

    assert calls == [account_id, account_id]
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success_count"] == 1
    assert any("\u53ef\u6062\u590d\u5931\u8d25" in str(event[1]) for event in logger.events)


def test_get_rt_target_mode_pauses_pass_after_login_restart_error(monkeypatch):
    with Session(engine) as session:
        models = [
            AccountModel(platform="chatgpt", email=f"target-restart-{idx}@test.com", password="Secret123!")
            for idx in range(3)
        ]
        session.add_all(models)
        session.commit()
        account_ids = []
        for model in models:
            session.refresh(model)
            account_ids.append(int(model.id or 0))

    calls = []

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            calls.append(command.account_id)
            if len(calls) == 1:
                return ActionExecutionResult(
                    ok=False,
                    error=(
                        "GET_RT_LOGIN_RESTART_REQUIRED: add-phone/send: "
                        "Your sign-in session is no longer valid. Please start over to continue."
                    ),
                )
            return ActionExecutionResult(ok=True, data={"message": "ok"})

    monkeypatch.setattr(tasks_module, "_filter_get_rt_target_ids", lambda ids, *, platform="chatgpt": (list(ids), []))
    monkeypatch.setattr(tasks_module, "_auto_upload_sub2api", lambda _logger, _account: True)
    monkeypatch.setattr(tasks_module, "_is_sub2api_configured", lambda: True)
    monkeypatch.setattr(tasks_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": account_ids,
            "task_mode": "target",
            "sms_provider": "none",
            "concurrency": 1,
        },
        logger,
    )

    assert calls[:3] == [account_ids[0], account_ids[0], account_ids[1]]
    assert account_ids[1] not in calls[:2]
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success_count"] == 3
    assert any("session" in str(event[1]).lower() and "10s" in str(event[1]) for event in logger.events)


def test_get_rt_target_mode_stops_after_email_login_cooldown(monkeypatch):
    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="target-email-cooldown@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id or 0)

    calls = []

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            calls.append(command.account_id)
            return ActionExecutionResult(
                ok=False,
                error=(
                    "GET_RT_EMAIL_LOGIN_COOLDOWN: passwordless/send-otp: "
                    "Too many tries. Please wait a few minutes before trying again."
                ),
            )

    monkeypatch.setattr(tasks_module, "_filter_get_rt_target_ids", lambda ids, *, platform="chatgpt": (list(ids), []))
    monkeypatch.setattr(tasks_module, "_auto_upload_sub2api", lambda _logger, _account: True)
    monkeypatch.setattr(tasks_module, "_is_sub2api_configured", lambda: True)
    monkeypatch.setattr(tasks_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [account_id],
            "task_mode": "target",
            "sms_provider": "none",
            "concurrency": 1,
        },
        logger,
    )

    assert calls == [account_id]
    assert logger.finished[0] == tasks_module.TASK_STATUS_FAILED
    assert logger.result_data["success_count"] == 0
    assert any("Too many tries" in str(event[1]) for event in logger.events)


def test_get_rt_target_mode_retries_after_token_exchange_timeout(monkeypatch):
    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="target-timeout@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id or 0)

    calls = []

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            calls.append(command.account_id)
            if len(calls) == 1:
                return ActionExecutionResult(
                    ok=False,
                    error=(
                        "token exchange failed: network error: Failed to perform, curl: (28) "
                        "Operation timed out after 30012 milliseconds with 0 bytes received."
                    ),
                )
            return ActionExecutionResult(ok=True, data={"message": "ok"})

    monkeypatch.setattr(tasks_module, "_filter_get_rt_target_ids", lambda ids, *, platform="chatgpt": (list(ids), []))
    monkeypatch.setattr(tasks_module, "_auto_upload_sub2api", lambda _logger, _account: True)
    monkeypatch.setattr(tasks_module, "_is_sub2api_configured", lambda: True)
    monkeypatch.setattr(tasks_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [account_id],
            "task_mode": "target",
            "sms_provider": "none",
            "concurrency": 1,
        },
        logger,
    )

    assert calls == [account_id, account_id]
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success_count"] == 1


def test_chatgpt_auto_plus_followup_returns_error_when_payment_link_fails(monkeypatch):
    class FakeLogger(_FakeLogger):
        def add_cashier_url(self, url):
            self.events.append(("cashier_url", url, {}))

    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {}

    class FakePlatform:
        def execute_action(self, action_id, account, params):
            return {
                "ok": False,
                "error": "checkout failed",
                "data": {
                    "cashier_url": "https://checkout.example/partial",
                },
            }

    saved_accounts = []
    monkeypatch.setattr(tasks_module, "save_account", lambda account: saved_accounts.append(dict(account.extra)))
    logger = FakeLogger()
    account = FakeAccount()

    error = tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={"extra": {"auto_chatgpt_plus_payment": True}},
        platform=FakePlatform(),
        account=account,
        logger=logger,
    )

    assert error == "ChatGPT Plus 支付链接生成失败: checkout failed"
    assert account.extra["cashier_url"] == "https://checkout.example/partial"
    assert saved_accounts[-1]["cashier_url"] == "https://checkout.example/partial"
    assert ("cashier_url", "https://checkout.example/partial", {}) in logger.events


def test_chatgpt_auto_plus_followup_does_not_output_pay_url_when_protocol_extract_fails(monkeypatch):
    class FakeLogger(_FakeLogger):
        def add_cashier_url(self, url):
            self.events.append(("cashier_url", url, {}))

    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {}

    class FakePlatform:
        def execute_action(self, action_id, account, params):
            return {
                "ok": False,
                "error": "Stripe /confirm 响应缺少 pm-redirects.stripe.com/authorize URL",
                "data": {
                    "cashier_url": "https://pay.openai.com/c/pay/cs_live_demo",
                    "checkout_url": "https://pay.openai.com/c/pay/cs_live_demo",
                    "paypal_authorize_url": "",
                    "paypal_protocol_extract": {"ok": False, "error": "missing authorize"},
                },
            }

    monkeypatch.setattr(tasks_module, "save_account", lambda account: None)
    logger = FakeLogger()

    error = tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={"extra": {"auto_chatgpt_plus_payment": True}},
        platform=FakePlatform(),
        account=FakeAccount(),
        logger=logger,
    )

    assert error.startswith("ChatGPT Plus 支付链接生成失败:")
    assert not any(event[0] == "cashier_url" for event in logger.events)
    assert not any(
        event[0] == "log" and "ChatGPT Plus 测试支付链接已生成: https://pay.openai.com" in event[1]
        for event in logger.events
    )


def test_platform_runtime_wires_log_fn_to_platform(monkeypatch):
    logs = []
    seen = {}

    class FakeSession:
        def __init__(self, engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model_cls, account_id):
            return type("Model", (), {"platform": "chatgpt"})()

    class FakePlatform:
        def __init__(self, config=None):
            self._log_fn = print

        def set_logger(self, logger):
            self._log_fn = logger

        def set_cancel_checker(self, checker):
            seen["cancel_check"] = checker

        def execute_action(self, action_id, account, params):
            self._log_fn("runtime platform log")
            assert self.is_cancel_requested() is False
            return {"ok": True, "data": {"message": "ok"}}

        def is_cancel_requested(self):
            return seen["cancel_check"]()

    monkeypatch.setattr(runtime_module, "Session", FakeSession)
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: FakePlatform)
    monkeypatch.setattr(runtime_module, "build_platform_account", lambda session, model: object())

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=123,
            action_id="payment_link",
            params={"auto_checkout": "true"},
        ),
        log_fn=logs.append,
        cancel_check=lambda: False,
    )

    assert result.ok is True
    assert logs == ["runtime platform log"]
    assert seen["cancel_check"]() is False


def test_platform_runtime_persists_cashier_url_even_when_action_fails_after_link(monkeypatch):
    patched = {}

    class FakeSession:
        def __init__(self, engine):
            self.added = []
            self.committed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model_cls, account_id):
            return type("Model", (), {"id": account_id, "platform": "chatgpt", "updated_at": None})()

        def add(self, model):
            self.added.append(model)

        def commit(self):
            self.committed = True

    class FakePlatform:
        def __init__(self, config=None):
            pass

        def execute_action(self, action_id, account, params):
            return {
                "ok": False,
                "error": "checkout failed",
                "data": {
                    "cashier_url": "https://checkout.stripe.com/c/pay/cs_test_link",
                    "message": "Payment link generated, but checkout failed.",
                },
            }

    def fake_patch_account_graph(session, model, **kwargs):
        patched.update(kwargs)

    monkeypatch.setattr(runtime_module, "Session", FakeSession)
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: FakePlatform)
    monkeypatch.setattr(runtime_module, "build_platform_account", lambda session, model: object())
    monkeypatch.setattr(runtime_module, "patch_account_graph", fake_patch_account_graph)

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=123,
            action_id="payment_link",
            params={"auto_checkout": "true"},
        )
    )

    assert result.ok is False
    assert result.error == "checkout failed"
    assert patched["cashier_url"] == "https://checkout.stripe.com/c/pay/cs_test_link"
    assert patched["summary_updates"]["cashier_url"] == "https://checkout.stripe.com/c/pay/cs_test_link"


def test_platform_runtime_persists_get_rt_tokens_and_user_info(monkeypatch):
    patched = {}

    class FakeSession:
        def __init__(self, engine):
            self.added = []
            self.committed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model_cls, account_id):
            return type("Model", (), {"id": account_id, "platform": "chatgpt", "updated_at": None})()

        def add(self, model):
            self.added.append(model)

        def commit(self):
            self.committed = True

    class FakePlatform:
        def __init__(self, config=None):
            pass

        def execute_action(self, action_id, account, params):
            return {
                "ok": True,
                "data": {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "id_token": "id-token",
                    "account_id": "acct-123",
                    "email": "real@example.com",
                    "expired": "2026-06-10T15:00:00Z",
                    "last_refresh": "2026-06-10T14:00:00Z",
                    "type": "codex",
                    "profile": {"email": "real@example.com", "name": "Real User"},
                    "id_token_claims": {"email": "real@example.com", "sub": "auth0|abc"},
                },
            }

    def fake_patch_account_graph(session, model, **kwargs):
        patched.update(kwargs)

    monkeypatch.setattr(runtime_module, "Session", FakeSession)
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: FakePlatform)
    monkeypatch.setattr(runtime_module, "build_platform_account", lambda session, model: object())
    monkeypatch.setattr(runtime_module, "patch_account_graph", fake_patch_account_graph)

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=123,
            action_id="get_rt",
            params={},
        )
    )

    assert result.ok is True
    assert patched["credential_updates"]["access_token"] == "access-token"
    assert patched["credential_updates"]["refresh_token"] == "refresh-token"
    assert patched["credential_updates"]["id_token"] == "id-token"
    assert patched["credential_updates"]["account_id"] == "acct-123"
    summary = patched["summary_updates"]
    assert summary["remote_email"] == "real@example.com"
    assert summary["codex_oauth"]["account_id"] == "acct-123"
    assert summary["codex_oauth"]["profile"]["name"] == "Real User"
    assert summary["codex_oauth"]["id_token_claims"]["sub"] == "auth0|abc"
    assert summary["lifecycle_status"] == "rt_pending_upload"


def test_platform_runtime_marks_sub2api_manual_upload_success(monkeypatch):
    patched = {}

    class FakeSession:
        def __init__(self, engine):
            self.added = []
            self.committed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model_cls, account_id):
            return type("Model", (), {"id": account_id, "platform": "chatgpt", "updated_at": None})()

        def add(self, model):
            self.added.append(model)

        def commit(self):
            self.committed = True

    class FakePlatform:
        def __init__(self, config=None):
            pass

        def execute_action(self, action_id, account, params):
            return {
                "ok": True,
                "data": {
                    "message": "SUB2API 上传成功",
                    "upload_target": "sub2api",
                    "upload_status": "uploaded",
                },
            }

    def fake_patch_account_graph(session, model, **kwargs):
        patched.update(kwargs)

    monkeypatch.setattr(runtime_module, "Session", FakeSession)
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: FakePlatform)
    monkeypatch.setattr(runtime_module, "build_platform_account", lambda session, model: object())
    monkeypatch.setattr(runtime_module, "patch_account_graph", fake_patch_account_graph)

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=123,
            action_id="upload_sub2api",
            params={},
        )
    )

    assert result.ok is True
    assert patched["lifecycle_status"] == "rt_uploaded"
    assert patched["summary_updates"]["display_status"] == "rt_uploaded"
    assert patched["summary_updates"]["rt_upload_status"] == "uploaded"


def test_platform_runtime_persists_failed_get_rt_banned_status(monkeypatch):
    patched = {}

    class FakeSession:
        def __init__(self, engine):
            self.added = []
            self.committed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model_cls, account_id):
            return type("Model", (), {"id": account_id, "platform": "chatgpt", "updated_at": None})()

        def add(self, model):
            self.added.append(model)

        def commit(self):
            self.committed = True

    class FakePlatform:
        def __init__(self, config=None):
            pass

        def execute_action(self, action_id, account, params):
            return {
                "ok": False,
                "error": "account has been deleted or deactivated",
                "error_type": "account_banned",
                "lifecycle_status": "banned",
                "summary_updates": {"deactivated_reason": "deleted"},
                "data": {},
            }

    def fake_patch_account_graph(session, model, **kwargs):
        patched.update(kwargs)

    monkeypatch.setattr(runtime_module, "Session", FakeSession)
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: FakePlatform)
    monkeypatch.setattr(runtime_module, "build_platform_account", lambda session, model: object())
    monkeypatch.setattr(runtime_module, "patch_account_graph", fake_patch_account_graph)

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(platform="chatgpt", account_id=123, action_id="get_rt", params={})
    )

    assert result.ok is False
    assert patched["lifecycle_status"] == "banned"
    assert patched["summary_updates"]["display_status"] == "banned"
    assert patched["summary_updates"]["validity_status"] == "invalid"


def test_platform_runtime_persists_session_refresh_after_failed_get_rt(monkeypatch):
    patched = {}

    class FakeSession:
        def __init__(self, engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model_cls, account_id):
            return type("Model", (), {"id": account_id, "platform": "chatgpt", "updated_at": None})()

        def add(self, model):
            pass

        def commit(self):
            pass

    class FakePlatform:
        def __init__(self, config=None):
            pass

        def execute_action(self, action_id, account, params):
            return {
                "ok": False,
                "error": "invalid_state: Your sign-in session is no longer valid.",
                "error_type": "session_stale_refreshed",
                "data": {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "session_token": "new-session",
                },
            }

    def fake_patch_account_graph(session, model, **kwargs):
        patched.update(kwargs)

    monkeypatch.setattr(runtime_module, "Session", FakeSession)
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: FakePlatform)
    monkeypatch.setattr(runtime_module, "build_platform_account", lambda session, model: object())
    monkeypatch.setattr(runtime_module, "patch_account_graph", fake_patch_account_graph)

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(platform="chatgpt", account_id=123, action_id="get_rt", params={})
    )

    assert result.ok is False
    assert patched["credential_updates"]["access_token"] == "new-access"
    assert patched["credential_updates"]["refresh_token"] == "new-refresh"
    assert patched["credential_updates"]["session_token"] == "new-session"
    assert patched["summary_updates"]["session_refresh_status"] == "refreshed"


def test_load_account_graphs_normalizes_legacy_authorized_rt_status():
    from core.account_graph import load_account_graphs, patch_account_graph

    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="legacy-rt@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)
        patch_account_graph(
            session,
            model,
            lifecycle_status="authorized",
            summary_updates={
                "oauth": {"type": "codex"},
                "codex_oauth": {"type": "codex"},
                "valid": True,
            },
            credential_updates={
                "refresh_token": "refresh-token",
                "access_token": "access-token",
            },
        )
        session.commit()
        account_id = int(model.id or 0)

    with Session(engine) as session:
        graph = load_account_graphs(session, [account_id])[account_id]

    assert graph["lifecycle_status"] == "rt_pending_upload"
    assert graph["display_status"] == "rt_pending_upload"
    assert graph["overview"]["rt_upload_status"] == "pending_upload"


def test_load_account_graphs_preserves_uploaded_rt_status():
    from core.account_graph import load_account_graphs, patch_account_graph

    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="legacy-uploaded@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)
        patch_account_graph(
            session,
            model,
            lifecycle_status="authorized",
            summary_updates={
                "oauth": {"type": "codex"},
                "codex_oauth": {"type": "codex"},
                "valid": True,
                "rt_upload_status": "uploaded",
                "rt_uploaded_at": "2026-06-14T00:00:00Z",
            },
            credential_updates={
                "refresh_token": "refresh-token",
                "access_token": "access-token",
            },
        )
        session.commit()
        account_id = int(model.id or 0)

    with Session(engine) as session:
        graph = load_account_graphs(session, [account_id])[account_id]

    assert graph["lifecycle_status"] == "rt_uploaded"
    assert graph["display_status"] == "rt_uploaded"
    assert graph["overview"]["rt_upload_status"] == "uploaded"


def test_sync_platform_account_graph_clears_stale_refresh_token_for_registered_chatgpt():
    from core.account_graph import load_account_graphs, patch_account_graph, sync_platform_account_graph
    from core.base_platform import Account, AccountStatus

    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="registered-clear@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)
        patch_account_graph(
            session,
            model,
            lifecycle_status="rt_pending_upload",
            summary_updates={
                "oauth": {"type": "codex"},
                "codex_oauth": {"type": "codex"},
                "rt_upload_status": "pending_upload",
            },
            credential_updates={
                "access_token": "old-access",
                "refresh_token": "old-refresh",
            },
        )
        session.commit()

        account = Account(
            platform="chatgpt",
            email=model.email,
            password=model.password,
            user_id="acct-registered",
            token="new-access",
            status=AccountStatus.REGISTERED,
            extra={
                "account_id": "acct-registered",
                "access_token": "new-access",
                "refresh_token": "",
                "registration_refresh_token": "registration-only-refresh",
                "registration_refresh_token_usable": False,
                "refresh_token_source": "",
            },
        )
        sync_platform_account_graph(session, model, account)
        session.commit()
        account_id = int(model.id or 0)

    with Session(engine) as session:
        graph = load_account_graphs(session, [account_id])[account_id]

    credentials = {
        item["key"]: item["value"]
        for item in graph["credentials"]
        if item.get("scope") == "platform"
    }
    assert credentials["access_token"] == "new-access"
    assert "refresh_token" not in credentials
    assert "registration_refresh_token" not in credentials
    assert graph["lifecycle_status"] == "registered"
    assert graph["display_status"] == "registered"
    assert "rt_upload_status" not in graph["overview"]
    assert "rt_uploaded_at" not in graph["overview"]
    assert "rt_acquired_at" not in graph["overview"]
    assert "registration_refresh_token" not in graph["overview"]
    assert "registration_refresh_token_usable" not in graph["overview"]


def test_sync_platform_account_graph_strips_registration_refresh_token_from_legacy_extra():
    from core.account_graph import load_account_graphs, sync_platform_account_graph
    from core.base_platform import Account, AccountStatus

    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="registered-extra@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)

        account = Account(
            platform="chatgpt",
            email=model.email,
            password=model.password,
            user_id="acct-extra",
            token="access-token",
            status=AccountStatus.REGISTERED,
            extra={
                "account_id": "acct-extra",
                "access_token": "access-token",
                "registration_refresh_token": "registration-only-refresh",
                "registration_refresh_token_usable": False,
                "refresh_token_source": "",
                "debug_note": "keep-me",
            },
        )
        sync_platform_account_graph(session, model, account)
        session.commit()
        account_id = int(model.id or 0)

    with Session(engine) as session:
        graph = load_account_graphs(session, [account_id])[account_id]

    legacy_extra = dict((graph["overview"] or {}).get("legacy_extra") or {})
    assert legacy_extra.get("debug_note") == "keep-me"
    assert "registration_refresh_token" not in legacy_extra
    assert "registration_refresh_token_usable" not in legacy_extra
