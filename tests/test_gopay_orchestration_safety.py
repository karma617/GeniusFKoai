from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from application import gopay_pay_chatgpt as flow


def test_ephemeral_sms_uses_persisted_expiry():
    expires = datetime.now(timezone.utc) + timedelta(seconds=700)
    account = SimpleNamespace(created_at=datetime.now(timezone.utc) - timedelta(days=1))
    remaining = flow._remaining_sms_lifetime_seconds(
        account,
        {"sms_provider": "five_sim", "sms_expires_at": expires.isoformat()},
        1200,
    )
    assert 695 <= remaining <= 700


def test_historical_ephemeral_sms_falls_back_to_created_at():
    created = datetime.now(timezone.utc) - timedelta(seconds=300)
    account = SimpleNamespace(created_at=created)
    remaining = flow._remaining_sms_lifetime_seconds(
        account,
        {"sms_provider": "smspool"},
        1200,
    )
    assert 895 <= remaining <= 900


def test_fixed_smsapi_has_no_expiry():
    account = SimpleNamespace(created_at=datetime.now(timezone.utc) - timedelta(days=30))
    assert flow._remaining_sms_lifetime_seconds(
        account,
        {"sms_provider": "api_sms"},
        1200,
    ) is None


def test_resolve_gopay_client_uses_persisted_worker_account(monkeypatch):
    from platforms.gopay._opai_loader import ensure_opai_on_path

    ensure_opai_on_path()
    from opai.core import gopay_protocol_worker as worker

    expected_client = object()
    calls = []
    monkeypatch.setattr(
        worker,
        "_resume_account",
        lambda phone, proxy="": calls.append((phone, proxy))
        or {"client": expected_client},
    )

    client = flow._resolve_gopay_client(
        "+628123456789",
        "user:pass@proxy.example.test:8080",
        log=lambda _message: None,
    )

    assert client is expected_client
    assert calls == [
        ("+628123456789", "http://user:pass@proxy.example.test:8080")
    ]


def test_resolve_gopay_client_fails_closed_without_saved_account(monkeypatch):
    from platforms.gopay._opai_loader import ensure_opai_on_path

    ensure_opai_on_path()
    from opai.core import gopay_protocol_worker as worker

    monkeypatch.setattr(worker, "_resume_account", lambda *_args, **_kwargs: None)
    assert flow._resolve_gopay_client(
        "+628123456789", "http://proxy.example.test", log=lambda _message: None
    ) is None


def test_subscription_confirmation_accepts_only_paid_plan(monkeypatch):
    fake_platform_account = SimpleNamespace(
        email="masked@example.test",
        token="access-token",
        extra={"access_token": "access-token", "cookies": ""},
    )
    monkeypatch.setattr(flow, "build_platform_account", lambda *_args: fake_platform_account)

    from platforms.chatgpt import payment

    monkeypatch.setattr(payment, "check_subscription_status", lambda *_args, **_kwargs: "plus")
    status = flow._verify_chatgpt_subscription(
        SimpleNamespace(),
        proxy="http://proxy",
        cancel_check=lambda: False,
        timeout_seconds=1,
        log=lambda _message: None,
    )
    assert status == "plus"


def test_subscription_confirmation_rejects_free_plan(monkeypatch):
    fake_platform_account = SimpleNamespace(
        email="masked@example.test",
        token="access-token",
        extra={"access_token": "access-token", "cookies": ""},
    )
    monkeypatch.setattr(flow, "build_platform_account", lambda *_args: fake_platform_account)

    from platforms.chatgpt import payment

    monkeypatch.setattr(payment, "check_subscription_status", lambda *_args, **_kwargs: "free")
    ticks = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(flow.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(flow.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="付款已结算"):
        flow._verify_chatgpt_subscription(
            SimpleNamespace(),
            proxy="http://proxy",
            cancel_check=lambda: False,
            timeout_seconds=1,
            log=lambda _message: None,
        )
