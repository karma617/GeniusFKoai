from types import SimpleNamespace

from sqlmodel import Session

from core import config_store, lifecycle
from core.account_graph import patch_account_graph
from core.db import AccountModel, engine
from infrastructure.config_repository import ConfigRepository


def test_lifecycle_services_default_to_core_jobs_enabled(monkeypatch):
    monkeypatch.setattr(
        config_store.config_store,
        "get",
        lambda key, default="": default,
    )

    flags = lifecycle.get_lifecycle_service_flags()

    assert lifecycle.is_lifecycle_manager_enabled() is True
    assert flags[lifecycle.LIFECYCLE_ACCOUNT_CHECK_ENABLED_KEY] is True
    assert flags[lifecycle.LIFECYCLE_TOKEN_REFRESH_ENABLED_KEY] is True
    assert flags[lifecycle.LIFECYCLE_TRIAL_WARNING_ENABLED_KEY] is True
    assert flags[lifecycle.LIFECYCLE_EXTERNAL_SYNC_ENABLED_KEY] is False


def test_lifecycle_manager_does_not_start_when_all_services_disabled(monkeypatch):
    monkeypatch.setattr(
        config_store.config_store,
        "get",
        lambda key, default="": "false"
        if key in lifecycle.LIFECYCLE_SERVICE_DEFAULTS
        else default,
    )

    assert lifecycle.is_lifecycle_manager_enabled() is False


def test_lifecycle_service_config_keys_are_exposed_with_expected_defaults():
    repository = ConfigRepository()

    allowed = repository.get_allowed_keys()

    assert lifecycle.LIFECYCLE_ACCOUNT_CHECK_ENABLED_KEY in allowed
    assert lifecycle.LIFECYCLE_TOKEN_REFRESH_ENABLED_KEY in allowed
    assert lifecycle.LIFECYCLE_TRIAL_WARNING_ENABLED_KEY in allowed
    assert lifecycle.LIFECYCLE_EXTERNAL_SYNC_ENABLED_KEY in allowed
    assert repository.DEFAULT_VALUES[lifecycle.LIFECYCLE_ACCOUNT_CHECK_ENABLED_KEY] == "true"
    assert repository.DEFAULT_VALUES[lifecycle.LIFECYCLE_TOKEN_REFRESH_ENABLED_KEY] == "true"
    assert repository.DEFAULT_VALUES[lifecycle.LIFECYCLE_TRIAL_WARNING_ENABLED_KEY] == "true"
    assert repository.DEFAULT_VALUES[lifecycle.LIFECYCLE_EXTERNAL_SYNC_ENABLED_KEY] == "false"


def test_lifecycle_token_refresh_uses_refresh_session_action(monkeypatch):
    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="refresh-web@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id or 0)
        patch_account_graph(session, model, lifecycle_status="registered")
        session.commit()

    calls = []

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            calls.append((command.platform, command.account_id, command.action_id))
            return SimpleNamespace(ok=True, data={"message": "ok"}, error="")

    class ForbiddenTokenRefreshManager:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Lifecycle token refresh must not use OAuth token refresh manager")

    monkeypatch.setattr("infrastructure.platform_runtime.PlatformRuntime", FakeRuntime)
    monkeypatch.setattr("platforms.chatgpt.token_refresh.TokenRefreshManager", ForbiddenTokenRefreshManager)

    result = lifecycle.refresh_expiring_tokens()

    assert result["refreshed"] == 1
    assert calls == [("chatgpt", account_id, "refresh_session")]


def test_lifecycle_token_refresh_deletes_confirmed_banned_account(monkeypatch):
    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="banned-web@test.com", password="Secret123!")
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id or 0)
        patch_account_graph(session, model, lifecycle_status="registered")
        session.commit()

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            return SimpleNamespace(
                ok=False,
                data={"delete_local_account": True, "error_type": "account_banned"},
                error="account deactivated",
            )

    monkeypatch.setattr("infrastructure.platform_runtime.PlatformRuntime", FakeRuntime)

    result = lifecycle.refresh_expiring_tokens()

    assert result["failed"] == 1
    with Session(engine) as session:
        assert session.get(AccountModel, account_id) is None
