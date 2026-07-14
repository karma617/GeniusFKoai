from __future__ import annotations

from sqlmodel import Session

from application.account_checks import AccountChecksService
from core.db import AccountModel, engine


def test_refresh_plan_sync_returns_plan_fields_for_log_dialog(monkeypatch):
    with Session(engine) as session:
        account = AccountModel(platform="chatgpt", email="plan-log@test.com", password="Secret123!")
        session.add(account)
        session.commit()
        session.refresh(account)
        account_id = int(account.id or 0)

    def _fake_check(target_id: int):
        assert target_id == account_id
        return True, {
            "account_id": target_id,
            "email": "plan-log@test.com",
            "platform": "chatgpt",
            "valid": True,
            "plan_state": "subscribed",
            "plan_name": "plus",
            "display_status": "subscribed",
            "subscription_status": "plus",
            "usage_plan_type": "plus",
        }

    monkeypatch.setattr("application.account_checks._run_single_account_check", _fake_check)

    result = AccountChecksService().refresh_plan_sync(
        platform="chatgpt",
        account_ids=[account_id],
        max_workers=1,
    )

    assert result["updated"] == 1
    assert result["timed_out"] == 0
    assert result["items"] == [
        {
            "account_id": account_id,
            "email": "plan-log@test.com",
            "platform": "chatgpt",
            "valid": True,
            "plan_state": "subscribed",
            "plan_name": "plus",
            "display_status": "subscribed",
            "subscription_status": "plus",
            "usage_plan_type": "plus",
            "ok": True,
        }
    ]
