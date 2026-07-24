from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from application import ba_link_extract as mod


def test_billing_address_for_known_country():
    addr = mod.billing_address_for("TR", email="x@y.com")
    assert addr["country"] == "TR"
    assert addr["email"] == "x@y.com"
    assert addr["city"]
    gb = mod.billing_address_for("GB", email="x@y.com")
    assert gb["country"] == "GB"
    assert gb["city"] == "London"
    assert gb["postal_code"]
    assert mod.checkout_currency_for_country("GB") == "GBP"
    assert mod.checkout_currency_for_country("BR") == "USD"
    assert mod.paypal_checkout_country_for_region("JP") == "DE"
    assert mod.paypal_checkout_currency_for_country("DE") == "EUR"
    assert mod.paypal_checkout_country_for_region("GB") == "GB"


def test_extract_email_from_token():
    import base64

    payload = base64.urlsafe_b64encode(json.dumps({"email": "demo@example.com"}).encode()).decode().rstrip("=")
    token = f"aaa.{payload}.bbb"
    assert mod.extract_email_from_token(token) == "demo@example.com"


def test_has_paypal():
    assert mod._has_paypal(["card", "paypal"]) is True
    assert mod._has_paypal(["card", "link"]) is False


def test_parse_proxy_pool_and_region():
    raw = (
        "gate.kookeey.info:1000:8239626-70e45c43e5:6a81dcf160-US-3385b816-5m\n"
        "gate.kookeey.info:1000:8239626-70e45c43e5:6a81dcf160-TR-05028908-5m\n"
    )
    lines = mod.parse_proxy_pool_lines(raw)
    assert len(lines) == 2
    assert lines[0].startswith("http://")
    assert mod.infer_region_from_proxy_text(raw.splitlines()[0]) == "US"
    assert mod.infer_region_from_proxy_text(raw.splitlines()[1]) == "TR"
    with patch.object(mod.random, "choice", side_effect=lambda items: items[-1]) as choice:
        picked = mod.pick_proxy_from_pool(raw, attempt=1)
    assert picked == lines[-1]
    choice.assert_called_once()


def test_extract_ba_link_missing_token():
    result = mod.extract_ba_link(access_token="")
    assert result["ok"] is False
    assert "access_token" in result["error"]


def test_extract_ba_link_stops_on_amount_check_failure():
    events = []
    with patch.object(mod, "_extract_once", return_value={
        "ok": False,
        "error": "Plus 首月免费优惠未生效：Stripe 今日应付 amount=1933",
        "steps": {"amount": "1933"},
    }) as extract_once:
        result = mod.extract_ba_link(
            access_token="token",
            billing_proxy="gate.kookeey.info:1000:u:p-DE-1-5m",
            promo_proxy="gate.kookeey.info:1000:u:p-JP-2-5m",
            max_attempts=20,
            progress_cb=lambda e: events.append(e),
        )
    assert result["ok"] is False
    assert result["data"]["attempts"] == 1
    assert extract_once.call_count == 1
    assert any(e.get("desc") == "金额校验失败，终止任务" for e in events)


def test_extract_once_success_path_with_progress():
    class FakeResp:
        def __init__(self, status_code=200, payload=None, text="", url=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text
            self.url = url

        def json(self):
            return self._payload

    bill_session = MagicMock()
    provider_session = MagicMock()
    bill_session.post.side_effect = [
        FakeResp(200, {
            "checkout_session_id": "cs_live_test123",
            "processor_entity": "openai_llc",
            "promo_campaign": {"status": "accepted"},
        }),
        FakeResp(200, {}),
        FakeResp(200, {"result": "approved"}),
    ]
    provider_session.post.side_effect = [
        FakeResp(200, {"promo_id": "plus-1-month-free"}),
    ]
    bill_session.get.side_effect = [
        FakeResp(200, {
            "submission_attempt": {
                "state": "requires_action",
                "next_action": {
                    "type": "redirect_to_url",
                    "redirect_to_url": {"url": "https://pm-redirects.stripe.com/authorize/acct/sa"},
                },
            }
        }),
        FakeResp(200, text="ok", url="https://www.paypal.com/agreements/approve?ba_token=BA-TESTTOKEN123456"),
    ]

    init_payload = {
        "init_checksum": "ck_1",
        "config_id": "cfg_1",
        "payment_method_types": ["card", "paypal"],
        "invoice": {"amount_due": 1667, "total": 1667},
        "elements_options": {"amount": 1667},
    }
    synced_payload = {
        "init_checksum": "ck_2",
        "config_id": "cfg_1",
        "payment_method_types": ["card", "paypal"],
        "invoice": {"amount_due": 0, "total": 0},
        "elements_options": {"amount": 0},
    }
    confirm_payload = {"submission_attempt": {"state": "requires_approval"}}
    pm_payload = {"id": "pm_test_1"}
    events = []

    with patch.object(mod, "build_protocol_session", side_effect=[bill_session, provider_session]), \
         patch.object(mod, "pick_proxy_from_pool", side_effect=["http://bill:1", "http://promo:1"]), \
         patch.object(mod.stripe_http, "stripe_init", side_effect=[init_payload, synced_payload]) as stripe_init, \
         patch.object(mod.stripe_http, "merge_checkout_payload", side_effect=lambda a, b: {**a, **b}), \
         patch.object(mod.stripe_http, "extract_expected_amount", side_effect=["1667", "0"]), \
         patch.object(mod.stripe_http, "extract_confirm_expected_amounts", return_value=("0", "0")), \
         patch.object(mod.stripe_http, "extract_display_amounts", return_value={"subtotal": "0"}), \
         patch.object(mod.stripe_http, "build_confirm_return_url", return_value="https://pay.openai.com/c/pay/cs_live_test123/confirm"), \
         patch.object(mod.stripe_http, "build_confirm_referrer_url", return_value="https://pay.openai.com/c/pay/cs_live_test123"), \
         patch.object(mod.stripe_http, "StripeDeviceContext", return_value=MagicMock()), \
         patch.object(mod.stripe_http, "stripe_create_paypal_payment_method", return_value=pm_payload) as stripe_pm, \
         patch.object(mod.stripe_http, "stripe_confirm_paypal_with_payment_method", return_value=confirm_payload) as stripe_confirm, \
         patch.object(mod.stripe_http, "extract_paypal_redirect_url", side_effect=ValueError("no")), \
         patch.object(mod.proxy_pool, "report_success", return_value=None):
        result = mod.extract_ba_link(
            access_token="token",
            email="demo@example.com",
            billing_proxy="gate-gb.kookeey.info:1000:u:p-GB-1-5m",
            promo_proxy="gate-jp.kookeey.info:1000:u:p-JP-2-5m",
            max_attempts=1,
            progress_cb=lambda e: events.append(e),
        )
    assert result["ok"] is True, result
    assert result["ba_token"].startswith("BA-")
    checkout_body = bill_session.post.call_args_list[0].kwargs["json"]
    assert checkout_body["billing_details"]["country"] == "GB"
    assert checkout_body["billing_details"]["currency"] == "GBP"
    assert checkout_body["checkout_ui_mode"] == "custom"
    assert "promo_campaign" not in checkout_body
    promo_body = provider_session.post.call_args_list[0].kwargs["json"]
    assert provider_session.post.call_args_list[0].args[0].endswith("/backend-api/payments/checkout/update")
    assert promo_body["checkout_session_id"] == "cs_live_test123"
    assert promo_body["promo_campaign"]["promo_campaign_id"] == "plus-1-month-free"
    assert stripe_init.call_args_list[0].args[0] is bill_session
    assert stripe_init.call_args_list[1].args[0] is bill_session
    assert stripe_pm.call_args.args[0] is bill_session
    assert stripe_confirm.call_args.args[0] is bill_session
    assert any(e.get("type") == "started" for e in events)
    assert any(e.get("type") == "progress" and e.get("desc") == "创建原价 PayPal checkout" for e in events)
    assert any(e.get("type") == "done" and e.get("ok") for e in events)
