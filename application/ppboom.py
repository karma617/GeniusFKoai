from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests


DEFAULT_PPBOOM_BASE_URL = "http://127.0.0.1:8787"
DEFAULT_PPBOOM_START_TIMEOUT = 30


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


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _base_url_port(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.port:
        return str(parsed.port)
    if parsed.scheme == "https":
        return "443"
    return "80" if parsed.scheme == "http" else "8787"


def _health_url(base_url: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", "health")


def _ppboom_is_healthy(base_url: str) -> bool:
    try:
        response = requests.get(_health_url(base_url), timeout=2)
    except Exception:
        return False
    return response.status_code < 400


def ensure_ppboom_service(
    base_url: str,
    *,
    log_fn: Callable[[str], None] | None = None,
    timeout_seconds: int = DEFAULT_PPBOOM_START_TIMEOUT,
) -> None:
    if _ppboom_is_healthy(base_url):
        if callable(log_fn):
            log_fn(f"PPBoom: helper is healthy at {base_url}")
        return

    root = _project_root()
    script = root / "start-ppboom.bat"
    if not script.exists():
        raise RuntimeError(f"PPBoom launcher not found: {script}")

    port = _base_url_port(base_url)
    log_dir = root / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "ppboom-auto-start.log"
    cmd = ["cmd.exe", "/c", str(script), port] if os.name == "nt" else [str(script), port]
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    if callable(log_fn):
        log_fn(f"PPBoom: starting helper via {script} port={port}")
    with log_path.open("ab") as log_file:
        subprocess.Popen(
            cmd,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )

    deadline = time.time() + max(1, int(timeout_seconds or DEFAULT_PPBOOM_START_TIMEOUT))
    while time.time() < deadline:
        if _ppboom_is_healthy(base_url):
            if callable(log_fn):
                log_fn(f"PPBoom: helper started at {base_url}")
            return
        time.sleep(0.75)
    raise RuntimeError(
        f"PPBoom helper did not become healthy within {timeout_seconds}s; see {log_path}"
    )


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


def _ppboom_checkout_mode(config: dict[str, Any]) -> str:
    mode = _text(config.get("ppboom_plus_checkout_mode"), "jp_pp").lower()
    return "us_pp" if mode == "us_pp" else "jp_pp"


def _ppboom_billing_defaults(config: dict[str, Any]) -> tuple[str, str]:
    if _ppboom_checkout_mode(config) == "us_pp":
        return "US", "USD"
    return "JP", "JPY"


def build_ppboom_payload(account: Any, config: dict[str, Any]) -> dict[str, Any]:
    access_token = _account_access_token(account)
    if not access_token:
        raise ValueError("ChatGPT access_token is required for PPBoom")

    max_attempts = _int(config.get("ppboom_max_attempts"), 10, minimum=1, maximum=20)
    billing_country, billing_currency = _ppboom_billing_defaults(config)
    payload: dict[str, Any] = {
        "accessToken": access_token,
        "defaultProxy": _text(config.get("ppboom_default_proxy")),
        "providerProxy": _text(config.get("ppboom_provider_proxy")),
        "billingCountry": billing_country,
        "billingCurrency": billing_currency,
        "billingEmail": _text(getattr(account, "email", "")),
        "promoCampaignId": "plus-1-month-free",
        "stripePublishableKey": _text(config.get("ppboom_stripe_publishable_key")),
        "paymentLocale": _text(config.get("ppboom_payment_locale"), "en"),
        "deviceId": _text(config.get("ppboom_device_id")),
        "userAgent": _text(config.get("ppboom_user_agent")),
        "maxAttempts": max_attempts,
        "plusCheckoutMode": _ppboom_checkout_mode(config),
        "checkoutRebuildMaxAttempts": _int(
            config.get("ppboom_checkout_rebuild_max_attempts"),
            3,
            minimum=1,
            maximum=10,
        ),
        "successDelaySeconds": _int(
            config.get("ppboom_success_delay_seconds"),
            10,
            minimum=0,
            maximum=3600,
        ),
        "conversionProxyUrl": _text(config.get("ppboom_conversion_proxy_url")),
        "cloudConversionEnabled": _bool(config.get("ppboom_cloud_conversion_enabled"), False),
        "verificationUrl": _text(config.get("ppboom_verification_url")),
        "paypalPhone": _text(config.get("ppboom_paypal_phone")),
        "firstDirectResendEnabled": _bool(
            config.get("ppboom_first_direct_resend_enabled"),
            False,
        ),
        "firstResendWaitSeconds": _int(
            config.get("ppboom_first_resend_wait_seconds"),
            20,
            minimum=0,
            maximum=300,
        ),
        "subsequentResendWaitSeconds": _int(
            config.get("ppboom_subsequent_resend_wait_seconds"),
            25,
            minimum=0,
            maximum=300,
        ),
        "verificationPollAttempts": _int(
            config.get("ppboom_verification_poll_attempts"),
            6,
            minimum=1,
            maximum=60,
        ),
        "verificationPollIntervalSeconds": _int(
            config.get("ppboom_verification_poll_interval_seconds"),
            5,
            minimum=1,
            maximum=60,
        ),
        "verificationResendMaxAttempts": _int(
            config.get("ppboom_verification_resend_max_attempts"),
            1,
            minimum=0,
            maximum=10,
        ),
    }
    if _bool(config.get("record_har"), False):
        payload["recordHar"] = True
        payload["recordHarPath"] = _text(config.get("record_har_path"))
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
        "record_har": bool(payload.get("recordHar")),
        "record_har_path": _text(
            data.get("recordHarPath")
            or data.get("record_har_path")
            or payload.get("recordHarPath")
        ),
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
    ensure_ppboom_service(base_url, log_fn=log_fn)
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
