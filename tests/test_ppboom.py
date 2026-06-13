from __future__ import annotations

from types import SimpleNamespace

from application import ppboom


class _Response:
    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code
        self.text = str(data)

    def json(self):
        return self._data


def test_build_ppboom_payload_maps_config_fields():
    account = SimpleNamespace(
        email="user@example.com",
        token="fallback-token",
        extra={"access_token": "access-token"},
    )

    payload = ppboom.build_ppboom_payload(
        account,
        {
            "ppboom_default_proxy": "http://default.proxy:8080",
            "ppboom_provider_proxy": "http://provider.proxy:8080",
            "ppboom_billing_country": "us",
            "ppboom_billing_currency": "usd",
            "ppboom_billing_name": "Test User",
            "ppboom_promo_campaign_id": "campaign",
            "ppboom_payment_locale": "en-US",
            "ppboom_device_id": "device-id",
            "ppboom_user_agent": "ua",
            "ppboom_max_attempts": "25",
        },
    )

    assert payload == {
        "accessToken": "access-token",
        "proxy": "",
        "defaultProxy": "http://default.proxy:8080",
        "providerProxy": "http://provider.proxy:8080",
        "billingCountry": "US",
        "billingCurrency": "USD",
        "billingName": "Test User",
        "billingEmail": "user@example.com",
        "promoCampaignId": "campaign",
        "stripePublishableKey": "",
        "paymentLocale": "en-US",
        "deviceId": "device-id",
        "userAgent": "ua",
        "maxAttempts": 20,
    }


def test_run_ppboom_paypal_link_normalizes_provider_url(monkeypatch):
    captured = {}

    def fake_post(url, *, json, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _Response(
            {
                "ok": True,
                "success": True,
                "attemptsUsed": 2,
                "maxAttempts": 10,
                "providerRedirectUrl": "https://paypal.example/approve",
                "stripeRedirectUrl": "https://pm-redirects.stripe.com/x",
            }
        )

    monkeypatch.setattr(ppboom.requests, "post", fake_post)
    account = SimpleNamespace(email="user@example.com", token="access-token", extra={})

    result = ppboom.run_ppboom_paypal_link(
        account,
        {"ppboom_base_url": "http://127.0.0.1:8787", "ppboom_max_attempts": 10},
    )

    assert captured["url"] == "http://127.0.0.1:8787/api/paypal-link"
    assert captured["json"]["accessToken"] == "access-token"
    assert captured["timeout"] == 900
    assert result["ok"] is True
    assert result["checkout_mode"] == "ppboom"
    assert result["subscription_submitted"] is False
    assert result["url"] == "https://paypal.example/approve"
    assert result["paypal_authorize_url"] == "https://paypal.example/approve"
