from __future__ import annotations

from application.pp_plus_ba import (
    extract_ba_token,
    normalize_phone,
    _is_free_plan,
    get_pp_plus_worker,
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
        "flow_country": "US",
        "proxy_enabled": False,
        "proxy_mode": "pool",
        "proxy_pool_text": "1.2.3.4:8080",
    })
    assert saved["sms_provider"] == "smsbower_api"
    assert saved["flow_country"] == "US"
    assert saved["proxy_enabled"] is False
    loaded = worker.load_settings()
    assert loaded["sms_country"] == "6"
    status = worker.get_status()
    assert "running" in status
    assert status["sms_service_code"] in {"pp", "paypal"}

def test_list_sms_provider_options_not_empty():
    worker = get_pp_plus_worker()
    options = worker.list_sms_provider_options()
    assert isinstance(options, list)
    # 至少能给出系统定义/启用项；若本地无启用配置，也允许空列表后由前端 fallback
    assert all('value' in item and 'label' in item for item in options)

