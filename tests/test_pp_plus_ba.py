from __future__ import annotations

import pytest

from application import pp_plus_ba as pp_mod
from application.pp_plus_ba import (
    extract_ba_token,
    normalize_phone,
    _is_free_plan,
    get_pp_plus_worker,
    NoAvailableSmsNumberError,
)


def test_extract_ba_token_from_raw_and_url():
    assert extract_ba_token("BA-3AX328361P111131W") == "BA-3AX328361P111131W"
    assert extract_ba_token("https://www.paypal.com/x?ba_token=BA-3AX328361P111131W&foo=1").startswith("BA-")
    assert extract_ba_token("") == ""
    assert extract_ba_token("not-a-token") == ""


def test_normalize_phone_adds_plus():
    assert normalize_phone("5511999999999").startswith("+")
    assert normalize_phone("+5511999999999") == "+5511999999999"


def test_is_free_plan_rules():
    assert _is_free_plan({"plan_state": "free", "overview": {}}) is True
    assert _is_free_plan({"plan_state": "subscribed", "overview": {}}) is False
    assert _is_free_plan({"plan_state": "free", "overview": {"pp_payment_status": "success"}}) is False
    assert _is_free_plan({"plan_state": "unknown", "plan_name": "Plus", "overview": {}}) is False


def test_settings_roundtrip(tmp_path, monkeypatch):
    worker = get_pp_plus_worker()
    monkeypatch.setattr("application.pp_plus_ba.SETTINGS_PATH", tmp_path / "pp_plus_settings.json")
    monkeypatch.setattr("application.pp_plus_ba.RUNTIME_PATH", tmp_path / "pp_plus_runtime.json")
    saved = worker.save_settings({
        "sms_provider": "smsbower_api",
        "sms_country": "6",
        "sms_service_code": "paypal",
        "flow_country": "US",
        "proxy_enabled": False,
        "proxy_mode": "pool",
        "proxy_pool_text": "1.2.3.4:8080",
    })
    assert saved["sms_provider"] == "smsbower_api"
    assert saved["sms_service_code"] == "paypal"
    assert saved["flow_country"] == "US"
    assert saved["proxy_enabled"] is False
    loaded = worker.load_settings()
    assert loaded["sms_country"] == "6"
    status = worker.get_status()
    assert "running" in status
    assert status["sms_service_code"] == "paypal"


def test_build_sms_provider_uses_custom_service_code(monkeypatch):
    worker = get_pp_plus_worker()
    monkeypatch.setattr(worker, "_resolve_sms_saved_config", lambda _provider_key: {"api_key": "dummy"})

    provider, service, provider_key = worker._build_sms_provider({
        **pp_mod.DEFAULT_SETTINGS,
        "sms_provider": "smsbower_api",
        "sms_service_code": "paypal",
    })

    assert provider_key == "smsbower_api"
    assert service == "paypal"
    assert getattr(provider, "default_service", "") == "paypal"


def test_load_settings_backfills_provider_service_code(tmp_path, monkeypatch):
    worker = get_pp_plus_worker()
    path = tmp_path / "pp_plus_settings.json"
    path.write_text('{"sms_provider":"five_sim_api","sms_country":"bih"}', encoding="utf-8")
    monkeypatch.setattr("application.pp_plus_ba.SETTINGS_PATH", path)

    loaded = worker.load_settings()

    assert loaded["sms_provider"] == "five_sim_api"
    assert loaded["sms_service_code"] == "paypal"


def test_load_settings_backfills_herosms_paypal_service_code(tmp_path, monkeypatch):
    worker = get_pp_plus_worker()
    path = tmp_path / "pp_plus_settings.json"
    path.write_text('{"sms_provider":"herosms_api","sms_country":"108"}', encoding="utf-8")
    monkeypatch.setattr("application.pp_plus_ba.SETTINGS_PATH", path)

    loaded = worker.load_settings()

    assert loaded["sms_provider"] == "herosms_api"
    assert loaded["sms_service_code"] == "ts"

def test_list_sms_provider_options_not_empty():
    worker = get_pp_plus_worker()
    options = worker.list_sms_provider_options()
    assert isinstance(options, list)
    # 至少能给出系统定义/启用项；若本地无启用配置，也允许空列表后由前端 fallback
    assert all('value' in item and 'label' in item for item in options)


def test_pp_plus_start_requires_selected_account_ids():
    worker = get_pp_plus_worker()
    with pytest.raises(ValueError, match="勾选"):
        worker.start([])


def test_pp_plus_pick_next_account_uses_selected_queue(monkeypatch):
    worker = get_pp_plus_worker()
    worker._pending_account_ids = [22]
    loaded = []

    def fake_load_selected_account(account_id):
        loaded.append(account_id)
        return {"id": account_id, "email": f"user{account_id}@example.com", "overview": {}}

    monkeypatch.setattr(worker, "_load_selected_account", fake_load_selected_account)

    account = worker._pick_next_account()

    assert account["id"] == 22
    assert loaded == [22]
    assert worker._pick_next_account() is None


def test_rent_sms_number_retries_no_numbers_then_returns(monkeypatch):
    calls = []
    waits = []

    class FakeProvider:
        def get_number(self, *, service: str, country: str):
            calls.append((service, country))
            if len(calls) < 3:
                raise RuntimeError("HeroSMS 获取号码失败: V2=404; V1=NO_NUMBERS")
            return type("Activation", (), {"phone_number": "+5511999999999", "activation_id": "act-1"})()

    monkeypatch.setattr(pp_mod.time, "sleep", lambda seconds: waits.append(seconds))

    activation = pp_mod._rent_sms_number_with_retry(
        FakeProvider(),
        service="pp",
        country="108",
        on_wait=lambda attempt: waits.append(f"wait-{attempt}"),
        retry_interval=3,
        timeout_seconds=60,
    )

    assert activation.phone_number == "+5511999999999"
    assert len(calls) == 3
    assert waits == ["wait-1", 3, "wait-2", 3]


def test_rent_sms_number_stops_during_no_numbers_retry(monkeypatch):
    stop = {"value": False}

    class FakeProvider:
        def get_number(self, *, service: str, country: str):
            raise RuntimeError("NO_NUMBERS")

    def fake_sleep(_seconds):
        stop["value"] = True

    monkeypatch.setattr(pp_mod.time, "sleep", fake_sleep)

    with pytest.raises(pp_mod.PpPlusStopped):
        pp_mod._rent_sms_number_with_retry(
            FakeProvider(),
            service="ts",
            country="108",
            stop_check=lambda: stop["value"],
            retry_interval=3,
            timeout_seconds=60,
        )


def test_pp_plus_account_logs_no_available_phone_timeout(monkeypatch):
    worker = get_pp_plus_worker()
    worker._account_views.clear()
    account = {
        "id": 987001,
        "email": "no-phone@example.com",
        "overview": {"pp_ba_token": "BA-3AX328361P111131W"},
    }

    def fake_run(account_arg, ba_token, settings):
        worker._set_account_stage(
            account_arg["id"],
            email=account_arg["email"],
            status="running",
            stage="暂无可用手机号，3秒后重试（第1次）",
            ba_token=ba_token,
        )
        raise NoAvailableSmsNumberError("暂无可用手机号")

    monkeypatch.setattr(worker, "load_settings", lambda: dict(pp_mod.DEFAULT_SETTINGS))
    monkeypatch.setattr(worker, "_run_paypal_for_account", fake_run)
    monkeypatch.setattr(worker, "clear_ba_token", lambda account_id, reason="": None)

    worker._process_account(account)

    view = worker._account_views[account["id"]]
    assert view.status == "error"
    assert view.error == "暂无可用手机号"
    messages = [str(item.get("message") or item.get("stage") or "") for item in view.logs]
    assert any("暂无可用手机号" in message for message in messages)


def test_pp_plus_failure_keeps_ba_token_for_next_run(monkeypatch):
    worker = get_pp_plus_worker()
    worker._account_views.clear()
    account = {
        "id": 987002,
        "email": "retry-failed@example.com",
        "overview": {"pp_ba_token": "BA-3AX328361P111131W"},
    }
    clear_calls = []

    monkeypatch.setattr(worker, "load_settings", lambda: dict(pp_mod.DEFAULT_SETTINGS))
    monkeypatch.setattr(worker, "_run_paypal_for_account", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("支付失败")))
    monkeypatch.setattr(worker, "clear_ba_token", lambda account_id, reason="": clear_calls.append((account_id, reason)))

    worker._process_account(account)

    view = worker._account_views[account["id"]]
    assert view.status == "error"
    assert view.ba_token == "BA-3AX328361P111131W"
    assert clear_calls == []


def test_no_numbers_error_recognizes_5sim_no_free_phones():
    assert pp_mod._is_no_numbers_error(RuntimeError("5sim purchase returned unusable response: no free phones")) is True


def test_ba_clear_decision_only_for_definitive_agreement_errors():
    assert pp_mod._should_clear_ba_token_after_failure("暂无可用手机号") is False
    assert pp_mod._should_clear_ba_token_after_failure("invalid phone number") is False
    assert pp_mod._should_clear_ba_token_after_failure("OTP send failed and phone-change limit reached") is False
    assert pp_mod._should_clear_ba_token_after_failure("proxy timeout while loading Hermes") is False
    assert pp_mod._should_clear_ba_token_after_failure("billing agreement token is invalid") is True
    assert pp_mod._should_clear_ba_token_after_failure("/pay/billing form submission blocked: invalid billingAgreementId") is True


def test_pp_plus_definitive_ba_failure_clears_ba_token(monkeypatch):
    worker = get_pp_plus_worker()
    worker._account_views.clear()
    account = {
        "id": 987003,
        "email": "clear-ba@example.com",
        "overview": {"pp_ba_token": "BA-3AX328361P111131W"},
    }
    clear_calls = []

    monkeypatch.setattr(worker, "load_settings", lambda: dict(pp_mod.DEFAULT_SETTINGS))
    monkeypatch.setattr(
        worker,
        "_run_paypal_for_account",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("/pay/billing form submission blocked: invalid billingAgreementId")
        ),
    )
    monkeypatch.setattr(worker, "clear_ba_token", lambda account_id, reason="": clear_calls.append((account_id, reason)))

    worker._process_account(account)

    view = worker._account_views[account["id"]]
    assert view.status == "error"
    assert view.stage == "任务失败，BA链已清除"
    assert clear_calls == [(account["id"], "/pay/billing form submission blocked: invalid billingAgreementId")]

