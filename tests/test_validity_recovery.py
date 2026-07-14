from __future__ import annotations

import json

from sqlmodel import Session, select

from application import tasks as tasks_module
from application.tasks import _run_single_account_check, _run_single_chatgpt_health_check
from core.account_graph import patch_account_graph
from core.base_platform import RegisterConfig
from core.db import AccountModel, AccountOverviewModel, engine
from core.lifecycle import check_accounts_validity
from core.proxy_pool import proxy_pool
from platforms.chatgpt import payment
from platforms.chatgpt.plugin import ChatGPTPlatform


class _AlwaysValidPlatform:
    def __init__(self, config: RegisterConfig | None = None):
        self.config = config

    def check_valid(self, account) -> bool:
        return True


class _AlwaysInvalidPlatform:
    def __init__(self, config: RegisterConfig | None = None):
        self.config = config

    def check_valid(self, account) -> bool:
        return False


def _create_account(*, platform: str = "chatgpt", lifecycle_status: str = "registered") -> int:
    with Session(engine) as session:
        model = AccountModel(platform=platform, email=f"{platform}@example.com", password="secret")
        session.add(model)
        session.commit()
        session.refresh(model)
        patch_account_graph(
            session,
            model,
            lifecycle_status=lifecycle_status,
            summary_updates={"valid": lifecycle_status != "invalid"},
        )
        session.commit()
        return int(model.id or 0)


def _create_chatgpt_health_account() -> int:
    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="health-chatgpt@example.com", password="secret")
        session.add(model)
        session.commit()
        session.refresh(model)
        patch_account_graph(
            session,
            model,
            summary_updates={"valid": True, "account_id": "acct-health"},
            credential_updates={
                "access_token": "access-token",
                "session_token": "session-token",
                "cookies": "__Secure-next-auth.session-token=session-token",
            },
        )
        session.commit()
        return int(model.id or 0)


def _overview(account_id: int):
    with Session(engine) as session:
        return session.exec(
            select(AccountOverviewModel).where(AccountOverviewModel.account_id == account_id)
        ).one()


def test_single_account_check_recovers_previously_invalid_account(monkeypatch):
    account_id = _create_account(lifecycle_status="invalid")
    monkeypatch.setattr("application.tasks.get", lambda _platform: _AlwaysValidPlatform)

    valid, result = _run_single_account_check(account_id)

    assert valid is True
    assert result["valid"] is True
    overview = _overview(account_id)
    assert overview.lifecycle_status == "registered"
    assert overview.validity_status == "valid"
    assert overview.display_status == "registered"
    assert overview.checked_at


def test_lifecycle_validity_check_does_not_overwrite_lifecycle_status(monkeypatch):
    account_id = _create_account(lifecycle_status="registered")
    monkeypatch.setattr("core.lifecycle.get", lambda _platform: _AlwaysInvalidPlatform)

    results = check_accounts_validity(platform="chatgpt", limit=10)

    assert results["invalid"] == 1
    overview = _overview(account_id)
    assert overview.lifecycle_status == "registered"
    assert overview.validity_status == "invalid"
    assert overview.display_status == "invalid"
    assert overview.checked_at


def test_chatgpt_health_check_uses_account_state_and_marks_403_banned(monkeypatch):
    account_id = _create_chatgpt_health_account()
    captured: dict = {}

    def _fake_fetch_state(**kwargs):
        captured.update(kwargs)
        return {
            "valid": False,
            "profile_error": {"status_code": 403, "body": "Forbidden"},
        }

    monkeypatch.setattr("platforms.chatgpt.plugin._resolve_action_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr("platforms.chatgpt.switch.fetch_chatgpt_account_state", _fake_fetch_state)

    result = _run_single_chatgpt_health_check(account_id)

    assert captured["access_token"] == "access-token"
    assert captured["session_token"] == "session-token"
    assert captured["cookies"] == "__Secure-next-auth.session-token=session-token"
    assert captured["chatgpt_account_id"] == "acct-health"
    assert captured["force_usage"] is True
    assert result["valid"] is False
    assert result["status_code"] == 403
    assert result.get("transient") is False

    overview = _overview(account_id)
    summary = overview.get_summary()
    assert overview.lifecycle_status == "banned"
    assert overview.validity_status == "invalid"
    assert overview.display_status == "banned"
    assert summary["health_status_code"] == 403
    assert "Forbidden" in summary["health_error"]


def test_chatgpt_health_check_marks_401_token_expired_relogin_required(monkeypatch):
    account_id = _create_chatgpt_health_account()

    def _fake_fetch_state(**_kwargs):
        return {
            "valid": False,
            "profile_error": {
                "status_code": 401,
                "body": {
                    "error": {
                        "message": "Provided authentication token is expired. Please try signing in again.",
                        "code": "token_expired",
                    }
                },
            },
        }

    monkeypatch.setattr("platforms.chatgpt.plugin._resolve_action_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr("platforms.chatgpt.switch.fetch_chatgpt_account_state", _fake_fetch_state)

    result = _run_single_chatgpt_health_check(account_id)

    assert result["valid"] is False
    assert result["status_code"] == 401
    assert result["relogin_required"] is True
    overview = _overview(account_id)
    summary = overview.get_summary()
    assert overview.lifecycle_status == "relogin_required"
    assert overview.validity_status == "unknown"
    assert overview.display_status == "relogin_required"
    assert summary["health_status_code"] == 401
    assert "token_expired" in summary["health_error"]


def test_chatgpt_health_check_persists_subscription_and_codex_usage(monkeypatch):
    account_id = _create_chatgpt_health_account()

    def _fake_fetch_state(**_kwargs):
        return {
            "valid": True,
            "account_id": "acct-health",
            "subscription_status": "free",
            "remote_user": {"email": "remote@example.com"},
            "codex_usage": {
                "source": "active",
                "five_hour": {"remaining_percent": 88},
                "seven_day": {"remaining_percent": 90},
            },
            "usage_breakdowns": [{"display_name": "Codex 5h", "remaining_usage": "88%"}],
            "prompt_remaining_percent": 88,
            "next_reset_at": "2026-07-12T10:00:00Z",
            "access_token": "refreshed-token",
        }

    monkeypatch.setattr("platforms.chatgpt.plugin._resolve_action_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr("platforms.chatgpt.switch.fetch_chatgpt_account_state", _fake_fetch_state)

    result = _run_single_chatgpt_health_check(account_id)

    assert result["valid"] is True
    overview = _overview(account_id)
    summary = overview.get_summary()
    assert overview.validity_status == "valid"
    assert overview.plan_state == "free"
    assert overview.plan_name == "free"
    assert summary["subscription_status"] == "free"
    assert summary["chatgpt_usage"]["five_hour"]["remaining_percent"] == 88
    assert summary["usage_breakdowns"][0]["display_name"] == "Codex 5h"
    assert summary["remote_email"] == "remote@example.com"
    assert "access_token" not in summary["chatgpt_account_state"]


def test_chatgpt_health_check_prefers_paid_usage_plan_over_free_subscription(monkeypatch):
    account_id = _create_chatgpt_health_account()

    def _fake_fetch_state(**_kwargs):
        return {
            "valid": True,
            "account_id": "acct-health",
            "subscription_status": "free",
            "codex_usage": {"plan_type": "plus"},
        }

    monkeypatch.setattr("platforms.chatgpt.plugin._resolve_action_proxy", lambda *args, **kwargs: None)
    monkeypatch.setattr("platforms.chatgpt.switch.fetch_chatgpt_account_state", _fake_fetch_state)

    result = _run_single_chatgpt_health_check(account_id)

    assert result["valid"] is True
    overview = _overview(account_id)
    summary = overview.get_summary()
    assert overview.plan_state == "subscribed"
    assert overview.plan_name == "plus"
    assert overview.display_status == "subscribed"
    assert summary["subscription_status"] == "plus"
    assert summary["chatgpt_usage"]["plan_type"] == "plus"


def test_account_health_check_task_counts_invalid_as_failure(monkeypatch):
    with Session(engine) as session:
        invalid = AccountModel(platform="chatgpt", email="invalid-health@example.com", password="secret")
        valid = AccountModel(platform="chatgpt", email="valid-health@example.com", password="secret")
        session.add(invalid)
        session.add(valid)
        session.commit()
        session.refresh(invalid)
        session.refresh(valid)
        invalid_id = int(invalid.id or 0)
        valid_id = int(valid.id or 0)

    def _fake_health_check(account_id: int, logger=None):
        if account_id == invalid_id:
            return {
                "account_id": account_id,
                "email": "invalid-health@example.com",
                "valid": False,
                "status_code": 403,
                "error": "账号状态/订阅 HTTP 403",
            }
        return {
            "account_id": account_id,
            "email": "valid-health@example.com",
            "valid": True,
            "status_code": 200,
        }

    monkeypatch.setattr(tasks_module, "_run_single_chatgpt_health_check", _fake_health_check)
    task = tasks_module.create_task(
        task_type=tasks_module.TASK_TYPE_ACCOUNT_HEALTH_CHECK,
        platform="chatgpt",
        payload={"platform": "chatgpt", "ids": [invalid_id, valid_id], "concurrency": 1},
        progress_total=2,
    )
    logger = tasks_module.TaskLogger(task["id"])
    logger.mark_running()

    tasks_module._execute_account_health_check_task(
        {"platform": "chatgpt", "ids": [invalid_id, valid_id], "concurrency": 1},
        logger,
    )

    with Session(engine) as session:
        stored = session.get(tasks_module.TaskModel, task["id"])
        assert stored is not None
        result = stored.get_result()
        assert stored.success_count == 1
        assert stored.error_count == 1
        assert result["data"]["valid"] == 1
        assert result["data"]["invalid"] == 1
        assert result["data"]["error"] == 0


def test_chatgpt_subscription_status_falls_back_to_wham_usage(monkeypatch):
    captured_headers: dict[str, str] = {}

    class _Resp:
        def __init__(self, data=None, error: Exception | None = None):
            self._data = data
            self._error = error

        def raise_for_status(self):
            if self._error:
                raise self._error

        def json(self):
            return self._data

    def _fake_get(url, **kwargs):
        if url.endswith("/backend-api/me"):
            return _Resp(error=RuntimeError("403"))
        captured_headers.update(kwargs.get("headers") or {})
        return _Resp(data={"plan_type": "free"})

    monkeypatch.setattr(payment.cffi_requests, "get", _fake_get)
    account = type(
        "AccountStub",
        (),
        {
            "access_token": "token",
            "cookies": "",
            "id_token": json.dumps({"chatgpt_account_id": "acct-123"}),
            "extra": {},
        },
    )()

    status = payment.check_subscription_status(account)

    assert status == "free"
    assert captured_headers["Authorization"] == "Bearer token"
    assert captured_headers["Chatgpt-Account-Id"] == "acct-123"


def test_chatgpt_subscription_status_prefers_paid_wham_usage_over_free_me(monkeypatch):
    class _Resp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    def _fake_get(url, **_kwargs):
        if url.endswith("/backend-api/me"):
            return _Resp({"plan_type": "free"})
        return _Resp({"plan_type": "plus"})

    monkeypatch.setattr(payment.cffi_requests, "get", _fake_get)
    account = type(
        "AccountStub",
        (),
        {
            "access_token": "token",
            "cookies": "",
            "id_token": json.dumps({"chatgpt_account_id": "acct-123"}),
            "extra": {},
        },
    )()

    details = payment.fetch_subscription_status_details(account)

    assert details["status"] == "plus"
    assert details["source"] == "backend-api/me"
    assert details["usage"]["plan_type"] == "plus"


def test_chatgpt_check_valid_uses_proxy_pool_before_direct(monkeypatch):
    calls: list[str | None] = []
    proxy_events: list[tuple[str, str]] = []

    def _fake_status(account, proxy=None):
        calls.append(proxy)
        if proxy != "http://127.0.0.1:7890":
            raise RuntimeError("should use proxy first")
        return {
            "status": "free",
            "source": "backend-api/wham/usage",
            "usage": {"plan_type": "free"},
        }

    monkeypatch.setattr(payment, "fetch_subscription_status_details", _fake_status)
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": "http://127.0.0.1:7890")
    monkeypatch.setattr(proxy_pool, "report_success", lambda url: proxy_events.append(("success", url)))
    monkeypatch.setattr(proxy_pool, "report_fail", lambda url: proxy_events.append(("fail", url)))

    plugin = ChatGPTPlatform.__new__(ChatGPTPlatform)
    plugin.config = RegisterConfig()
    plugin.mailbox = None
    account = type(
        "AccountStub",
        (),
        {
            "token": "token",
            "region": "",
            "extra": {
                "access_token": "token",
                "id_token": "",
                "cookies": "",
            },
        },
    )()

    assert plugin.check_valid(account) is True
    assert calls == ["http://127.0.0.1:7890"]
    assert proxy_events == [("success", "http://127.0.0.1:7890")]
    assert plugin.get_last_check_overview()["chatgpt_usage"] == {"plan_type": "free"}
