from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urljoin

import requests


DEFAULT_PPBOOM_BASE_URL = "http://127.0.0.1:8787"


def _text(value: Any, default: str = "") -> str:
    text = str(value if value is not None else "").strip()
    return text or default


def _int(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        number = int(value)
    except Exception:
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def is_ppboom_enabled(config: dict[str, Any]) -> bool:
    return _bool(config.get("use_ppboom"), False) or _bool(config.get("ppboom_enabled"), False)


def _account_access_token(account: Any) -> str:
    extra = getattr(account, "extra", None)
    if isinstance(extra, dict):
        token = _text(extra.get("access_token"))
        if token:
            return token
    return _text(getattr(account, "token", ""))


def _safe_payload_for_log(payload: dict[str, Any]) -> dict[str, Any]:
    safe = dict(payload)
    token = _text(safe.get("accessToken"))
    if token:
        safe["accessToken"] = token[:8] + "..." + token[-4:] if len(token) > 16 else "***"
    return safe


def build_ppboom_payload(account: Any, config: dict[str, Any]) -> dict[str, Any]:
    access_token = _account_access_token(account)
    if not access_token:
        raise ValueError("ChatGPT access_token is required for PPBoom")

    max_attempts = _int(config.get("ppboom_max_attempts"), 10, minimum=1, maximum=20)
    payload: dict[str, Any] = {
        "accessToken": access_token,
        "proxy": _text(config.get("ppboom_proxy") or config.get("proxy")),
        "defaultProxy": _text(config.get("ppboom_default_proxy")),
        "providerProxy": _text(config.get("ppboom_provider_proxy")),
        "billingCountry": _text(config.get("ppboom_billing_country"), "DE").upper(),
        "billingCurrency": _text(config.get("ppboom_billing_currency"), "EUR").upper(),
        "billingName": _text(config.get("ppboom_billing_name")),
        "billingEmail": _text(config.get("ppboom_billing_email"), _text(getattr(account, "email", ""))),
        "promoCampaignId": _text(config.get("ppboom_promo_campaign_id"), "plus-1-month-free"),
        "stripePublishableKey": _text(config.get("ppboom_stripe_publishable_key")),
        "paymentLocale": _text(config.get("ppboom_payment_locale"), "en"),
        "deviceId": _text(config.get("ppboom_device_id")),
        "userAgent": _text(config.get("ppboom_user_agent")),
        "maxAttempts": max_attempts,
    }
    return payload


def _pick_url(data: dict[str, Any]) -> str:
    for key in (
        "providerRedirectUrl",
        "longUrl",
        "stripeRedirectUrl",
        "stripeHostedUrl",
        "checkoutUrl",
        "checkout_url",
        "url",
    ):
        url = _text(data.get(key))
        if url:
            return url
    return ""


def normalize_ppboom_response(data: dict[str, Any], payload: dict[str, Any], base_url: str) -> dict[str, Any]:
    attempts = data.get("attempts")
    if not isinstance(attempts, list):
        attempts = []
    ok = bool(data.get("ok") or data.get("success"))
    open_url = _pick_url(data)
    provider_url = _text(data.get("providerRedirectUrl"))
    error = _text(data.get("providerError") or data.get("error"))
    if not ok and not error and attempts:
        last = attempts[-1] if isinstance(attempts[-1], dict) else {}
        error = _text(last.get("error"))
    if ok and not open_url:
        ok = False
        error = error or "PPBoom did not return a redirect URL"

    return {
        "ok": ok,
        "url": open_url,
        "checkout_url": open_url,
        "cashier_url": open_url,
        "paypal_authorize_url": provider_url or open_url,
        "plan": "plus",
        "payment_method": "paypal",
        "auto_checkout": False,
        "checkout_mode": "ppboom",
        "subscription_submitted": False,
        "ppboom": data,
        "ppboom_base_url": base_url,
        "ppboom_attempts_used": data.get("attemptsUsed") or len(attempts),
        "ppboom_max_attempts": data.get("maxAttempts") or payload.get("maxAttempts"),
        "ppboom_payload": _safe_payload_for_log(payload),
        "message": "PPBoom link generated." if ok else "PPBoom link generation failed.",
        "error": "" if ok else error,
    }


def run_ppboom_paypal_link(
    account: Any,
    config: dict[str, Any],
    *,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    base_url = _text(config.get("ppboom_base_url"), DEFAULT_PPBOOM_BASE_URL).rstrip("/")
    payload = build_ppboom_payload(account, config)
    max_attempts = _int(payload.get("maxAttempts"), 10, minimum=1, maximum=20)
    timeout_seconds = _int(
        config.get("ppboom_timeout"),
        max(300, max_attempts * 90),
        minimum=30,
        maximum=3600,
    )
    endpoint = urljoin(base_url + "/", "api/paypal-link")
    if callable(log_fn):
        log_fn(f"PPBoom: POST {endpoint} maxAttempts={max_attempts}")
    response = requests.post(endpoint, json=payload, timeout=timeout_seconds)
    try:
        response_data = response.json() if response.text else {}
    except Exception:
        response_data = {"error": response.text[:1000]}
    if not isinstance(response_data, dict):
        response_data = {"raw": response_data}
    if response.status_code >= 400:
        detail = _text(response_data.get("detail") or response_data.get("error") or response.text[:1000])
        raise RuntimeError(f"PPBoom HTTP {response.status_code}: {detail}")
    result = normalize_ppboom_response(response_data, payload, base_url)
    if callable(log_fn):
        if result.get("ok"):
            log_fn(f"PPBoom: generated {result.get('paypal_authorize_url') or result.get('url')}")
        else:
            log_fn(f"PPBoom: failed {result.get('error') or 'unknown error'}")
    return result
