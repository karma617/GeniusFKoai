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
            "ppboom_plus_checkout_mode": "us_pp",
            "ppboom_payment_locale": "en-US",
            "ppboom_checkout_rebuild_max_attempts": "12",
            "ppboom_success_delay_seconds": "8",
            "ppboom_conversion_proxy_url": "http://conversion.proxy:8080",
            "ppboom_cloud_conversion_enabled": "true",
            "ppboom_verification_url": "https://sms.example/api",
            "ppboom_paypal_phone": "5551234567",
            "ppboom_first_direct_resend_enabled": "true",
            "ppboom_first_resend_wait_seconds": "21",
            "ppboom_subsequent_resend_wait_seconds": "26",
            "ppboom_verification_poll_attempts": "7",
            "ppboom_verification_poll_interval_seconds": "6",
            "ppboom_verification_resend_max_attempts": "2",
            "ppboom_device_id": "device-id",
            "ppboom_user_agent": "ua",
            "ppboom_max_attempts": "25",
            "record_har": "true",
            "record_har_path": "D:/captures/ppboom.har",
        },
    )

    assert payload == {
        "accessToken": "access-token",
        "defaultProxy": "http://default.proxy:8080",
        "providerProxy": "http://provider.proxy:8080",
        "billingCountry": "US",
        "billingCurrency": "USD",
        "billingEmail": "user@example.com",
        "promoCampaignId": "plus-1-month-free",
        "stripePublishableKey": "",
        "paymentLocale": "en-US",
        "deviceId": "device-id",
        "userAgent": "ua",
        "maxAttempts": 20,
        "plusCheckoutMode": "us_pp",
        "checkoutRebuildMaxAttempts": 10,
        "successDelaySeconds": 8,
        "conversionProxyUrl": "http://conversion.proxy:8080",
        "cloudConversionEnabled": True,
        "verificationUrl": "https://sms.example/api",
        "paypalPhone": "5551234567",
        "firstDirectResendEnabled": True,
        "firstResendWaitSeconds": 21,
        "subsequentResendWaitSeconds": 26,
        "verificationPollAttempts": 7,
        "verificationPollIntervalSeconds": 6,
        "verificationResendMaxAttempts": 2,
        "recordHar": True,
        "recordHarPath": "D:/captures/ppboom.har",
    }


def test_build_ppboom_payload_defaults_to_gujumpgate_ppboom_billing():
    account = SimpleNamespace(
        email="user@example.com",
        token="fallback-token",
        extra={"access_token": "access-token"},
    )

    payload = ppboom.build_ppboom_payload(account, {})

    assert payload["billingCountry"] == "DE"
    assert payload["billingCurrency"] == "EUR"
    assert payload["plusCheckoutMode"] == "de_pp"


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

    def fake_get(url, *, timeout):
        captured["health_url"] = url
        captured["health_timeout"] = timeout
        return _Response({"ok": True})

    monkeypatch.setattr(ppboom.requests, "get", fake_get)
    monkeypatch.setattr(ppboom.requests, "post", fake_post)
    account = SimpleNamespace(email="user@example.com", token="access-token", extra={})

    result = ppboom.run_ppboom_paypal_link(
        account,
        {
            "ppboom_base_url": "http://127.0.0.1:8787",
            "ppboom_max_attempts": 10,
            "record_har": "true",
            "record_har_path": "D:/captures/ppboom.har",
        },
    )

    assert captured["url"] == "http://127.0.0.1:8787/api/paypal-link"
    assert captured["json"]["accessToken"] == "access-token"
    assert captured["json"]["recordHar"] is True
    assert captured["json"]["recordHarPath"] == "D:/captures/ppboom.har"
    assert captured["timeout"] == 900
    assert result["ok"] is True
    assert result["checkout_mode"] == "ppboom"
    assert result["subscription_submitted"] is False
    assert result["url"] == "https://paypal.example/approve"
    assert result["paypal_authorize_url"] == "https://paypal.example/approve"
    assert result["record_har"] is True
    assert result["record_har_path"] == "D:/captures/ppboom.har"


def test_ensure_ppboom_service_starts_launcher_when_unhealthy(monkeypatch, tmp_path):
    script = tmp_path / "start-ppboom.bat"
    script.write_text("@echo off\n", encoding="utf-8")
    calls = {"get": 0, "popen": None}

    def fake_get(url, *, timeout):
        calls["get"] += 1
        if calls["get"] == 1:
            return _Response({"ok": False}, status_code=503)
        return _Response({"ok": True})

    class FakePopen:
        def __init__(self, args, **kwargs):
            calls["popen"] = {"args": args, "kwargs": kwargs}

    monkeypatch.setattr(ppboom, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(ppboom.requests, "get", fake_get)
    monkeypatch.setattr(ppboom.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(ppboom.time, "sleep", lambda _seconds: None)

    ppboom.ensure_ppboom_service("http://127.0.0.1:8787", timeout_seconds=3)

    assert calls["get"] >= 2
    assert calls["popen"] is not None
    assert calls["popen"]["args"][-1] == "8787"
    assert calls["popen"]["kwargs"]["cwd"] == str(tmp_path)
