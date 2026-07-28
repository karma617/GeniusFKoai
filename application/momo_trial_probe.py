"""ChatGPT 越南 MoMo 试用资格探测（只建未确认 Checkout + Stripe init，不支付）。"""

from __future__ import annotations

import json
from typing import Any, Callable

from application.ba_link_extract import (
    PAYMENT_CHECKOUT_URL,
    _auth_headers,
    _payment_method_types,
    _text,
)
from platforms.chatgpt.payment_protocol import build_protocol_session
from platforms.chatgpt.stripe_http import (
    STRIPE_PUBLISHABLE_KEY,
    extract_checkout_session_id,
    stripe_init,
)

LogFn = Callable[[str], None] | None

MOMO_TRIAL_LABEL = "MOMO试用"
DEFAULT_TRIAL_DAYS = 30
DEFAULT_BILLING_COUNTRY = "VN"
DEFAULT_BILLING_CURRENCY = "VND"


def _log(log_fn: LogFn, message: str) -> None:
    if log_fn:
        try:
            log_fn(message)
        except Exception:
            pass


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _dig(payload: Any, *keys: str) -> Any:
    cur = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _response_json(response: Any) -> Any:
    try:
        if hasattr(response, "json"):
            return response.json()
    except Exception:
        pass
    text = _text(getattr(response, "text", ""))
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}


def _has_cf_challenge(text: str) -> bool:
    blob = str(text or "").lower()
    return any(
        token in blob
        for token in (
            "cf-browser-verification",
            "cloudflare",
            "attention required",
            "just a moment",
            "cf-challenge",
            "checking your browser",
        )
    )


def _collect_trial_flags(payload: Any) -> dict[str, Any]:
    data = _dict(payload)
    one_click = _as_bool(
        data.get("one_click_trial_eligible")
        if "one_click_trial_eligible" in data
        else _dig(data, "checkout_session", "one_click_trial_eligible")
    )
    is_new = _as_bool(
        data.get("is_new_stripe_customer")
        if "is_new_stripe_customer" in data
        else _dig(data, "checkout_session", "is_new_stripe_customer")
    )
    trial_period_days = (
        data.get("trial_period_days")
        or _dig(data, "subscription_data", "trial_period_days")
        or _dig(data, "checkout_session", "subscription_data", "trial_period_days")
        or _dig(data, "invoice", "subscription_details", "metadata", "trial_period_days")
    )
    trial_end = (
        data.get("trial_end")
        or _dig(data, "subscription_data", "trial_end")
        or _dig(data, "checkout_session", "subscription_data", "trial_end")
    )
    try:
        trial_days_int = int(trial_period_days) if trial_period_days not in (None, "") else 0
    except Exception:
        trial_days_int = 0
    has_real_trial = bool(trial_days_int > 0 or trial_end)
    return {
        "one_click_trial_eligible": one_click,
        "is_new_stripe_customer": is_new,
        "trial_period_days": trial_days_int if trial_days_int > 0 else None,
        "trial_end": trial_end,
        "has_real_trial": has_real_trial,
    }


def _has_momo(payment_method_types: Any) -> bool:
    if not isinstance(payment_method_types, list):
        return False
    return "momo" in [str(item).lower() for item in payment_method_types]


def _classify_checkout_error(status_code: int, body: Any, text: str) -> str:
    blob = f"{text} {json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else ''}".lower()
    if status_code in (401, 403) and any(token in blob for token in ("unauthorized", "invalid", "token", "expired")):
        return "credential_invalid"
    if status_code in (401, 403) and _has_cf_challenge(text):
        return "cloudflare"
    if status_code == 403 and _has_cf_challenge(text):
        return "cloudflare"
    if status_code == 429:
        return "rate_limited"
    if any(token in blob for token in ("already", "active subscription", "already_subscribed", "paid", "is_paid")):
        return "already_paid"
    return "checkout_failed"


def choose_decision(
    *,
    checkout_ok: bool,
    checkout_error: str,
    trial_flags: dict[str, Any],
    payment_methods: list[str],
    mode: str,
    stripe_ok: bool,
    stripe_error: str = "",
) -> dict[str, Any]:
    if not checkout_ok:
        decision = checkout_error or "checkout_failed"
        return {
            "decision": decision,
            "supported": False,
            "conclusive": decision in {"already_paid", "credential_invalid"},
            "reason": checkout_error or "checkout_failed",
        }

    one_click = trial_flags.get("one_click_trial_eligible")
    has_real_trial = bool(trial_flags.get("has_real_trial"))
    if one_click is False and not has_real_trial:
        return {
            "decision": "account_trial_ineligible",
            "supported": False,
            "conclusive": True,
            "reason": "one_click_trial_eligible=false and no trial fields",
        }

    if not stripe_ok:
        if one_click is False and not has_real_trial:
            return {
                "decision": "account_trial_ineligible",
                "supported": False,
                "conclusive": True,
                "reason": "no trial",
            }
        return {
            "decision": "stripe_init_failed",
            "supported": False,
            "conclusive": False,
            "reason": stripe_error or "stripe_init_failed",
        }

    if mode and mode not in {"subscription", "payment", "setup", ""}:
        return {
            "decision": "unexpected_mode",
            "supported": False,
            "conclusive": True,
            "reason": f"mode={mode}",
        }

    if not payment_methods:
        return {
            "decision": "payment_methods_unknown",
            "supported": False,
            "conclusive": False,
            "reason": "empty payment_method_types",
        }

    momo = _has_momo(payment_methods)
    eligible_trial = bool(has_real_trial or one_click is True)
    if not eligible_trial:
        return {
            "decision": "trial_not_applied",
            "supported": False,
            "conclusive": True,
            "reason": "no real trial after stripe init",
        }
    if not momo:
        return {
            "decision": "momo_not_enabled",
            "supported": False,
            "conclusive": True,
            "reason": "trial ok but momo missing",
        }
    return {
        "decision": "ready",
        "supported": True,
        "conclusive": True,
        "reason": "real trial + momo enabled",
    }


def probe_momo_trial(
    *,
    access_token: str,
    proxy: str = "",
    cookies: str = "",
    trial_period_days: int = DEFAULT_TRIAL_DAYS,
    billing_country: str = DEFAULT_BILLING_COUNTRY,
    billing_currency: str = DEFAULT_BILLING_CURRENCY,
    check_methods_anyway: bool = True,
    log_fn: LogFn = None,
) -> dict[str, Any]:
    """对单个账号做 MoMo 试用资格探测。

    只创建未确认 Checkout Session 并 Stripe init，不 confirm、不创建 PaymentMethod。
    """
    token = _text(access_token)
    if not token:
        return {
            "ok": False,
            "decision": "credential_invalid",
            "supported": False,
            "conclusive": True,
            "error": "缺少 access_token",
            "payment_method_types": [],
            "trial": {},
            "has_momo": False,
        }

    country = (_text(billing_country) or DEFAULT_BILLING_COUNTRY).upper()
    currency = (_text(billing_currency) or DEFAULT_BILLING_CURRENCY).upper()
    days = max(int(trial_period_days or DEFAULT_TRIAL_DAYS), 1)

    session = build_protocol_session(proxy=_text(proxy), cookies_str=_text(cookies))
    headers = _auth_headers(token, cookies=_text(cookies))
    payload = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "subscription_data": {"trial_period_days": days},
        "checkout_ui_mode": "custom",
    }

    _log(log_fn, f"[MOMO试用] checkout country={country} currency={currency} trial_days={days}")
    try:
        resp = session.post(PAYMENT_CHECKOUT_URL, headers=headers, json=payload, timeout=45)
    except Exception as exc:
        return {
            "ok": False,
            "decision": "checkout_failed",
            "supported": False,
            "conclusive": False,
            "error": f"checkout 请求失败: {type(exc).__name__}",
            "payment_method_types": [],
            "trial": {},
            "has_momo": False,
        }

    status = int(getattr(resp, "status_code", 0) or 0)
    body = _response_json(resp)
    text = _text(getattr(resp, "text", ""))
    if status >= 400 or not isinstance(body, dict):
        kind = _classify_checkout_error(status, body, text)
        _log(log_fn, f"[MOMO试用] checkout 失败 status={status} kind={kind}")
        return {
            "ok": False,
            "decision": kind,
            "supported": False,
            "conclusive": kind in {"already_paid", "credential_invalid"},
            "error": f"checkout HTTP {status}",
            "status_code": status,
            "payment_method_types": [],
            "trial": {},
            "has_momo": False,
        }

    cs_id = _text(body.get("checkout_session_id") or body.get("cs_id") or body.get("id") or body.get("session_id"))
    if not cs_id.startswith("cs_"):
        # 某些响应只给 URL
        url_candidate = _text(
            body.get("url")
            or body.get("checkout_url")
            or body.get("checkout_session_url")
            or _dig(body, "checkout_session", "url")
        )
        if url_candidate:
            try:
                cs_id = extract_checkout_session_id(url_candidate)
            except Exception:
                cs_id = ""
        else:
            cs_id = ""
    publishable_key = (
        _text(body.get("publishable_key"))
        or _text(body.get("stripe_publishable_key"))
        or _text(_dig(body, "stripe", "publishable_key"))
        or STRIPE_PUBLISHABLE_KEY
    )

    trial_flags = _collect_trial_flags(body)
    _log(
        log_fn,
        "[MOMO试用] checkout ok "
        f"one_click={trial_flags.get('one_click_trial_eligible')} "
        f"real_trial={trial_flags.get('has_real_trial')} "
        f"trial_days={trial_flags.get('trial_period_days')}",
    )

    if not cs_id:
        return {
            "ok": False,
            "decision": "checkout_failed",
            "supported": False,
            "conclusive": False,
            "error": "checkout 响应缺少 cs_id",
            "payment_method_types": [],
            "trial": trial_flags,
            "has_momo": False,
        }

    one_click = trial_flags.get("one_click_trial_eligible")
    has_real_trial = bool(trial_flags.get("has_real_trial"))
    if one_click is False and not has_real_trial and not check_methods_anyway:
        decision = choose_decision(
            checkout_ok=True,
            checkout_error="",
            trial_flags=trial_flags,
            payment_methods=[],
            mode="",
            stripe_ok=False,
            stripe_error="skipped",
        )
        return {
            "ok": True,
            "decision": decision["decision"],
            "supported": False,
            "conclusive": decision["conclusive"],
            "error": "",
            "payment_method_types": [],
            "trial": trial_flags,
            "mode": "",
            "reason": decision["reason"],
            "has_momo": False,
        }

    stripe_ok = False
    stripe_error = ""
    payment_methods: list[str] = []
    mode = ""
    amount_due = None
    currency_out = currency
    try:
        init_payload = stripe_init(session, cs_id=cs_id, publishable_key=publishable_key)
        if not isinstance(init_payload, dict):
            raise RuntimeError("stripe init 响应非对象")
        payment_methods = _payment_method_types(init_payload)
        mode = _text(
            init_payload.get("mode")
            or _dig(init_payload, "session", "mode")
            or _dig(init_payload, "checkout_session", "mode")
        )
        invoice = _dict(init_payload.get("invoice"))
        amount_due = invoice.get("amount_due")
        if invoice.get("currency"):
            currency_out = _text(invoice.get("currency")).upper() or currency_out
        stripe_trial = _collect_trial_flags(init_payload)
        if stripe_trial.get("has_real_trial"):
            trial_flags["has_real_trial"] = True
            trial_flags["trial_period_days"] = trial_flags.get("trial_period_days") or stripe_trial.get(
                "trial_period_days"
            )
            trial_flags["trial_end"] = trial_flags.get("trial_end") or stripe_trial.get("trial_end")
        # 金额为 0 也常表示 trial 已落到 invoice
        try:
            if amount_due is not None and int(amount_due) == 0 and (one_click is True or days > 0):
                trial_flags["has_real_trial"] = True
        except Exception:
            pass
        stripe_ok = True
        _log(
            log_fn,
            f"[MOMO试用] stripe init ok methods={payment_methods} mode={mode or '-'} "
            f"trial={trial_flags.get('has_real_trial')} amount_due={amount_due} "
            f"momo={_has_momo(payment_methods)}",
        )
    except Exception as exc:
        stripe_error = f"{type(exc).__name__}"
        _log(log_fn, f"[MOMO试用] stripe init 失败: {stripe_error}")

    decision = choose_decision(
        checkout_ok=True,
        checkout_error="",
        trial_flags=trial_flags,
        payment_methods=payment_methods,
        mode=mode,
        stripe_ok=stripe_ok,
        stripe_error=stripe_error,
    )
    return {
        "ok": True,
        "decision": decision["decision"],
        "supported": bool(decision["supported"]),
        "conclusive": bool(decision["conclusive"]),
        "error": "" if decision["supported"] else (decision.get("reason") or stripe_error or ""),
        "reason": decision.get("reason") or "",
        "payment_method_types": payment_methods,
        "has_momo": _has_momo(payment_methods),
        "mode": mode,
        "amount_due": amount_due,
        "currency": currency_out,
        "trial": trial_flags,
        # 故意不回传 cs_id / token / checkout_url，避免日志泄漏
    }
