from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlparse

from core.proxy_pool import normalize_proxy_url, proxy_pool
from paypal.proxy import ProxyEntry, parse_proxy_pool_text
from platforms.chatgpt import paypal_http, stripe_http
from platforms.chatgpt.payment_protocol import build_protocol_session

PAYMENT_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
CHECKOUT_UPDATE_PATH = "/backend-api/payments/checkout/update"
APPROVE_PATH = "/backend-api/payments/checkout/approve"
BA_TOKEN_RE = re.compile(r"BA-[A-Za-z0-9]{8,80}", re.I)
PM_REDIRECT_RE = re.compile(r"https://pm-redirects\.stripe\.com/authorize/[^\"'\s<>]+")
URL_RE = re.compile(r"https?://[^\"'\s<>\\]+")
PAYPAL_REDIRECT_MARKERS = (
    "pm-redirects.stripe.com/authorize",
    "paypal.com/checkoutnow",
    "paypal.com/agreements/approve",
    "paypal.com/signin/authorize",
    "paypal.com/webapps/hermes",
)
REDIRECT_DIAG_KEYWORDS = ("paypal", "redirect", "next_action", "return_url", "authorize", "approve", "ba_token")
MAX_STATIC_NO_REDIRECT_POLLS = 3

COUNTRY_CURRENCY = {
    "US": "USD",
    "TR": "TRY",
    "VN": "VND",
    "BR": "BRL",
    "JP": "JPY",
    "ID": "IDR",
    "SG": "SGD",
    "HK": "HKD",
    "GB": "GBP",
    "AU": "AUD",
    "CA": "CAD",
    "IN": "INR",
    "MX": "MXN",
    "DE": "EUR",
    "NL": "EUR",
    "IE": "EUR",
    "FR": "EUR",
    "BE": "EUR",
}

CHECKOUT_CURRENCY_OVERRIDES = {
    "BR": "USD",
}
PAYPAL_CHECKOUT_FALLBACK_COUNTRY = "DE"
PAYPAL_CHECKOUT_COUNTRIES = {"DE", "NL", "IE", "FR", "BE", "GB", "US"}


def checkout_currency_for_country(country: str) -> str:
    code = _text(country, "US").upper()
    return CHECKOUT_CURRENCY_OVERRIDES.get(code) or COUNTRY_CURRENCY.get(code, "USD")


def paypal_checkout_country_for_region(region: str) -> str:
    code = _text(region).upper()
    if code in PAYPAL_CHECKOUT_COUNTRIES:
        return code
    return PAYPAL_CHECKOUT_FALLBACK_COUNTRY


def paypal_checkout_currency_for_country(country: str) -> str:
    return COUNTRY_CURRENCY.get(_text(country, PAYPAL_CHECKOUT_FALLBACK_COUNTRY).upper(), "EUR")

ADDRS = {
    "US": {
        "name": "John Smith",
        "email": "buyer@example.com",
        "country": "US",
        "state": "NY",
        "city": "New York",
        "postal_code": "10001",
        "line1": "350 5th Ave",
        "line2": "",
    },
    "TR": {
        "name": "Ahmet Yilmaz",
        "email": "buyer@example.com",
        "country": "TR",
        "state": "34",
        "city": "Istanbul",
        "postal_code": "34000",
        "line1": "Istiklal Cad 1",
        "line2": "",
    },
    "VN": {
        "name": "Nguyen Van A",
        "email": "buyer@example.com",
        "country": "VN",
        "state": "",
        "city": "Ho Chi Minh",
        "postal_code": "700000",
        "line1": "1 Nguyen Hue",
        "line2": "",
    },
    "BR": {
        "name": "Joao Silva",
        "email": "buyer@example.com",
        "country": "BR",
        "state": "SP",
        "city": "Sao Paulo",
        "postal_code": "01310-100",
        "line1": "Av Paulista 1000",
        "line2": "",
    },
    "JP": {
        "name": "Taro Yamada",
        "email": "buyer@example.com",
        "country": "JP",
        "state": "Tokyo",
        "city": "Tokyo",
        "postal_code": "100-0001",
        "line1": "1-1 Chiyoda",
        "line2": "",
    },
    "GB": {
        "name": "James Smith",
        "email": "buyer@example.com",
        "country": "GB",
        "state": "",
        "city": "London",
        "postal_code": "SW1A 1AA",
        "line1": "10 Downing Street",
        "line2": "",
    },
    "IE": {
        "name": "Sean Murphy",
        "email": "buyer@example.com",
        "country": "IE",
        "state": "",
        "city": "Dublin",
        "postal_code": "D01 F5P2",
        "line1": "1 Grafton Street",
        "line2": "",
    },
    "NL": {
        "name": "Jan de Vries",
        "email": "buyer@example.com",
        "country": "NL",
        "state": "",
        "city": "Amsterdam",
        "postal_code": "1011 AA",
        "line1": "Damrak 1",
        "line2": "",
    },
    "DE": {
        "name": "Hans Mueller",
        "email": "buyer@example.com",
        "country": "DE",
        "state": "Berlin",
        "city": "Berlin",
        "postal_code": "10115",
        "line1": "Invalidenstrasse 1",
        "line2": "",
    },
}


def _text(value: Any, default: str = "") -> str:
    text = str(value if value is not None else "").strip()
    return text or default


def _log(log_fn: Callable[[str], None] | None, message: str) -> None:
    if callable(log_fn):
        log_fn(message)


def _raise_if_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if callable(cancel_check) and cancel_check():
        raise RuntimeError("任务已取消")


def extract_email_from_token(token: str) -> str:
    parts = _text(token).split(".")
    if len(parts) != 3:
        return ""
    try:
        padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8"))
        profile = payload.get("https://api.openai.com/profile")
        if isinstance(profile, dict) and profile.get("email"):
            return str(profile.get("email") or "")
        return str(payload.get("email") or "")
    except Exception:
        return ""


def billing_address_for(country: str, *, email: str = "") -> dict[str, str]:
    code = _text(country, "US").upper()
    base = dict(ADDRS.get(code) or ADDRS["US"])
    base["country"] = code if code in ADDRS else base.get("country") or "US"
    if email:
        base["email"] = email
    if code not in ADDRS:
        # 未知国家时至少保证 country 字段正确，其它字段用 US 占位
        base["country"] = code
    return base


def resolve_proxy_input(value: str, *, region_hint: str = "") -> str | None:
    """支持完整代理 URL 或两位地区码（从代理池按 region 取）。"""
    text = _text(value)
    if not text:
        hint = _text(region_hint).upper()
        if hint:
            return proxy_pool.get_next(region=hint) or None
        return None
    if "://" in text or "@" in text or text.count(":") >= 1 and not re.fullmatch(r"[A-Za-z]{2}", text):
        return normalize_proxy_url(text) or text
    # 两位地区码
    if re.fullmatch(r"[A-Za-z]{2}", text):
        return proxy_pool.get_next(region=text.upper()) or None
    return normalize_proxy_url(text) or text



REGION_CODE_RE = re.compile(r"(?:^|[^A-Za-z])([A-Za-z]{2})(?:[^A-Za-z]|$)")
REGION_TAG_RE = re.compile(
    r"(?:region[-_]?|[-_]g[-_]?|[-_]country[-_]?|[-_]cc[-_]?)"
    r"([A-Za-z]{2})(?:[^A-Za-z]|$)",
    re.I,
)
# kookeey style: ...-US-3385... or ...-IE-5735... or username ends with -US-
KOOKE_REGION_RE = re.compile(r"(?:^|[-_])([A-Z]{2})(?:[-_]\d|$)", re.I)


def parse_proxy_pool_lines(raw: str) -> list[str]:
    """Parse multi-line proxy pool text (same formats as project proxy pool)."""
    lines = parse_proxy_pool_text(raw or "")
    out: list[str] = []
    for line in lines:
        text = _text(line)
        if not text:
            continue
        # two-letter region code alone is not a proxy line
        if re.fullmatch(r"[A-Za-z]{2}", text):
            continue
        try:
            entry = ProxyEntry.parse(text)
            out.append(entry.url)
            continue
        except Exception:
            pass
        normalized = normalize_proxy_url(text)
        if normalized:
            out.append(normalized)
            continue
        # keep raw if looks like host:port...
        if ":" in text:
            out.append(text)
    # de-dupe preserve order
    seen = set()
    uniq = []
    for item in out:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(item)
    return uniq


def infer_region_from_proxy_text(raw: str, *, default: str = "") -> str:
    """Infer country/region code from proxy pool text (kookeey username tags etc.)."""
    text = _text(raw)
    if not text:
        return _text(default).upper()
    # Prefer first non-empty line
    first = ""
    for line in text.replace(",", "\n").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            first = line
            break
    sample = first or text

    # explicit region tags
    for pattern in (REGION_TAG_RE, KOOKE_REGION_RE):
        m = pattern.search(sample)
        if m:
            code = m.group(1).upper()
            if code in COUNTRY_CURRENCY or code in ADDRS:
                return code

    # username segment often ends with -US-xxxxx
    m = re.search(r"[-_]([A-Z]{2})[-_]\d{3,}", sample, re.I)
    if m:
        code = m.group(1).upper()
        if code in COUNTRY_CURRENCY or code in ADDRS:
            return code

    # last resort: trailing two-letter token in username
    m = re.search(r"[-_]([A-Z]{2})(?:[-_@]|$)", sample, re.I)
    if m:
        code = m.group(1).upper()
        if code in COUNTRY_CURRENCY or code in ADDRS:
            return code

    if re.fullmatch(r"[A-Za-z]{2}", sample.strip()):
        return sample.strip().upper()
    return _text(default).upper()


def pick_proxy_from_pool(raw: str, *, region_hint: str = "", attempt: int = 1) -> str | None:
    """Pick proxy by attempt order from multi-line pool; fallback to region pool."""
    lines = parse_proxy_pool_lines(raw)
    if lines:
        index = max(0, int(attempt or 1) - 1) % len(lines)
        return lines[index]
    return resolve_proxy_input(raw, region_hint=region_hint)


def normalize_promo_create_mode(value: str) -> str:
    mode = _text(value, "update_after_checkout").lower().replace("-", "_")
    if mode in {"create_with_promo", "checkout_with_promo", "with_promo"}:
        return "create_with_promo"
    return "update_after_checkout"


def _emit(progress_cb, event: dict) -> None:
    if callable(progress_cb):
        try:
            progress_cb(event)
        except Exception:
            pass


def _auth_headers(token: str, cookies: str = "", extra: dict | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        ),
        "oai-language": "en-US",
    }
    if cookies:
        headers["cookie"] = cookies
    if extra:
        headers.update({k: str(v) for k, v in extra.items() if v is not None})
    return headers


def _has_paypal(payment_method_types: Any) -> bool:
    if not isinstance(payment_method_types, list):
        return False
    return "paypal" in [str(item).lower() for item in payment_method_types]


def _payment_method_types(payload: Any) -> list[str]:
    found: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("payment_method_types", "ordered_payment_method_types"):
                items = value.get(key)
                if isinstance(items, list):
                    for item in items:
                        text = str(item or "").strip().lower()
                        if text and text not in found:
                            found.append(text)
            specs = value.get("payment_method_specs")
            if isinstance(specs, list):
                for spec in specs:
                    if isinstance(spec, dict):
                        text = str(spec.get("type") or "").strip().lower()
                        if text and text not in found:
                            found.append(text)
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    return found


def _iter_payload_strings(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _iter_payload_strings(item, child_path)
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            yield from _iter_payload_strings(item, f"{path}[{idx}]")
    elif isinstance(value, str):
        yield path, value


def _clean_candidate_url(url: str) -> str:
    return str(url or "").strip().rstrip('.,);]}')


def _is_paypal_redirect_url(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in PAYPAL_REDIRECT_MARKERS)


def _find_redirect(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    try:
        url, _ = stripe_http.extract_paypal_redirect_url(payload)
        if url:
            return _clean_candidate_url(str(url))
    except Exception:
        pass
    text = json.dumps(payload, ensure_ascii=False)
    match = PM_REDIRECT_RE.search(text)
    if match:
        return _clean_candidate_url(match.group(0))
    for _, value in _iter_payload_strings(payload):
        for match in URL_RE.finditer(value):
            candidate = _clean_candidate_url(match.group(0))
            if _is_paypal_redirect_url(candidate):
                return candidate
    return ""


def _payload_redirect_diagnostics(payload: Any, *, limit: int = 260) -> str:
    if not isinstance(payload, dict):
        return ""
    details: list[str] = []
    payload_id = _text(payload.get("id"))
    payload_object = _text(payload.get("object"))
    if payload_id or payload_object:
        details.append(f"id={payload_id or '-'} object={payload_object or '-'}")
    for path, value in _iter_payload_strings(payload):
        haystack = f"{path} {value}".lower()
        if not any(keyword in haystack for keyword in REDIRECT_DIAG_KEYWORDS):
            continue
        compact = _compact_payload(value, limit=120)
        item = f"{path}={compact}" if path else compact
        if item not in details:
            details.append(item)
        if len(details) >= 4:
            break
    return _compact_payload("; ".join(details), limit=limit)


def _follow_ba(session, redirect_url: str) -> tuple[bool, str, str]:
    response = session.get(
        redirect_url,
        allow_redirects=True,
        timeout=25,
        headers={"Referer": "https://pay.openai.com/", "User-Agent": "Mozilla/5.0"},
    )
    final_url = str(getattr(response, "url", "") or "")
    body = str(getattr(response, "text", "") or "")
    for candidate in (final_url, body, redirect_url):
        token = paypal_http.extract_ba_token(candidate) if hasattr(paypal_http, "extract_ba_token") else ""
        if not token:
            match = BA_TOKEN_RE.search(candidate or "")
            token = match.group(0) if match else ""
        if token:
            return True, token, final_url or redirect_url
    return False, "", final_url or redirect_url


def _mask_proxy(proxy: str | None) -> str:
    value = _text(proxy)
    if not value:
        return "直连"
    suffix = hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:6]
    try:
        parsed = urlparse(value if "://" in value else f"http://{value}")
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{host}{port}#{suffix}" if host else f"{value[:48]}#{suffix}"
    except Exception:
        return f"{value[:48]}#{suffix}"


def _compact_payload(value: Any, *, limit: int = 260) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            text = str(value)
    except Exception:
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _response_preview(response: Any, *, limit: int = 260) -> str:
    try:
        payload = response.json() if hasattr(response, "json") else None
        if payload is not None:
            return _compact_payload(payload, limit=limit)
    except Exception:
        pass
    return _compact_payload(getattr(response, "text", "") or "", limit=limit)


def _payload_error_summary(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("error", "last_payment_error"):
        err = payload.get(key)
        if err:
            return _compact_payload(err, limit=220)
    submission = payload.get("submission_attempt")
    if isinstance(submission, dict):
        err = submission.get("error")
        if err:
            return _compact_payload(err, limit=220)
    return ""



def extract_ba_link(
    *,
    access_token: str,
    cookies: str = "",
    email: str = "",
    billing_proxy: str = "",
    promo_proxy: str = "",
    billing_country: str = "",
    promo_country: str = "",
    billing_currency: str = "",
    confirm_mode: str = "pm",
    promo_create_mode: str = "update_after_checkout",
    max_attempts: int = 20,
    log_fn: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    progress_cb: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """双 IP 撞 BA 链：账单代理池 / 优惠代理池 + SSE 进度回调。"""
    token = _text(access_token)
    if not token:
        return {"ok": False, "error": "账号缺少 access_token", "data": {}}

    billing_proxy_country = infer_region_from_proxy_text(billing_proxy, default=_text(billing_country, "US")) or "US"
    promo_country_code = infer_region_from_proxy_text(
        promo_proxy,
        default=_text(promo_country, billing_proxy_country),
    ) or billing_proxy_country
    bill_country = paypal_checkout_country_for_region(billing_proxy_country)
    currency = paypal_checkout_currency_for_country(bill_country)
    mode = _text(confirm_mode, "pm").lower()
    if mode not in {"pm", "direct"}:
        mode = "pm"
    promo_mode = normalize_promo_create_mode(promo_create_mode)
    attempts = max(1, min(int(max_attempts or 20), 50))
    resolved_email = _text(email) or extract_email_from_token(token) or "buyer@example.com"

    billing_pool = parse_proxy_pool_lines(billing_proxy)
    promo_pool = parse_proxy_pool_lines(promo_proxy)
    _emit(progress_cb, {
        "type": "started",
        "billing_country": bill_country,
        "billing_proxy_country": billing_proxy_country,
        "promo_country": promo_country_code,
        "checkout_currency": currency,
        "billing_pool_size": len(billing_pool),
        "promo_pool_size": len(promo_pool),
        "max_attempts": attempts,
        "promo_create_mode": promo_mode,
    })
    _log(log_fn, f"[BA提取] 账单池={len(billing_pool)} 优惠池={len(promo_pool)} checkout代理国={billing_proxy_country} 账单={bill_country}/{currency} 优惠更新={promo_country_code} promo模式={promo_mode} 重试={attempts}")

    last_error = ""
    last_steps: dict[str, Any] = {}
    final_attempt = 0
    for attempt in range(1, attempts + 1):
        final_attempt = attempt
        _raise_if_cancelled(cancel_check)
        if attempt > 1:
            _emit(progress_cb, {"type": "progress", "step": 1, "total": 7, "desc": f"[第{attempt}次] 重跑...", "attempt": attempt})
        _emit(progress_cb, {
            "type": "progress",
            "step": 1,
            "total": 7,
            "desc": f"刷新 {billing_proxy_country}/{promo_country_code} 代理",
            "attempt": attempt,
        })
        _log(log_fn, f"[BA提取] 第 {attempt}/{attempts} 次：账单/支付={billing_proxy_country}->{bill_country}/{currency} 优惠更新={promo_country_code}")
        try:
            result = _extract_once(
                token=token,
                cookies=cookies,
                email=resolved_email,
                billing_proxy_input=billing_proxy,
                promo_proxy_input=promo_proxy,
                billing_proxy_country=billing_proxy_country,
                billing_country=bill_country,
                promo_country=promo_country_code,
                currency=currency,
                confirm_mode=mode,
                promo_create_mode=promo_mode,
                attempt=attempt,
                log_fn=log_fn,
                cancel_check=cancel_check,
                progress_cb=progress_cb,
            )
        except Exception as exc:
            last_error = str(exc)[:300]
            last_steps = {"exception": last_error}
            _log(log_fn, f"[BA提取] 第 {attempt} 次异常: {last_error}")
            _emit(progress_cb, {"type": "progress", "step": 7, "total": 7, "desc": f"异常: {last_error}", "attempt": attempt})
            continue

        if result.get("ok"):
            result["attempt"] = attempt
            result.setdefault("billing_country", bill_country)
            result.setdefault("promo_country", promo_country_code)
            _emit(progress_cb, {
                "type": "done",
                "ok": True,
                "ba_token": result.get("ba_token"),
                "ba_url": result.get("ba_url"),
                "attempt": attempt,
                "data": result.get("data") or {},
            })
            return result
        last_error = _text(result.get("error"), "提取失败")
        last_steps = result.get("steps") if isinstance(result.get("steps"), dict) else {}
        _log(log_fn, f"[BA提取] 第 {attempt} 次失败: {last_error}")
        _emit(progress_cb, {"type": "progress", "step": 7, "total": 7, "desc": f"失败: {last_error}", "attempt": attempt})
        if "Plus 首月免费优惠未生效" in last_error:
            _log(log_fn, "[BA提取] 金额校验失败，当前 IP 组合终止后续重试")
            _emit(progress_cb, {"type": "progress", "step": 7, "total": 7, "desc": "金额校验失败，终止任务", "attempt": attempt})
            break

    _emit(progress_cb, {"type": "done", "ok": False, "error": last_error or "提取 BA 链失败", "attempts": final_attempt or attempts})
    return {
        "ok": False,
        "error": last_error or "提取 BA 链失败",
        "data": {
            "steps": last_steps,
            "billing_country": bill_country,
            "promo_country": promo_country_code,
            "attempts": final_attempt or attempts,
        },
    }



def _extract_once(
    *,
    token: str,
    cookies: str,
    email: str,
    billing_proxy_input: str,
    promo_proxy_input: str,
    billing_proxy_country: str,
    billing_country: str,
    promo_country: str,
    currency: str,
    confirm_mode: str,
    promo_create_mode: str,
    attempt: int = 1,
    log_fn: Callable[[str], None] | None,
    cancel_check: Callable[[], bool] | None,
    progress_cb: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    steps: dict[str, Any] = {}
    billing_proxy = pick_proxy_from_pool(billing_proxy_input, region_hint=billing_proxy_country, attempt=attempt)
    promo_proxy = pick_proxy_from_pool(promo_proxy_input, region_hint=promo_country, attempt=attempt)
    if not promo_proxy:
        promo_proxy = billing_proxy
    if not billing_proxy:
        return {"ok": False, "error": "账单代理池为空，请填写 checkout 代理链接", "steps": steps}

    checkout_proxy = billing_proxy
    approve_proxy = checkout_proxy
    steps["billing_proxy"] = _mask_proxy(billing_proxy)
    steps["promo_proxy"] = _mask_proxy(promo_proxy)
    steps["checkout_proxy"] = _mask_proxy(checkout_proxy)
    steps["payment_proxy"] = steps["checkout_proxy"]
    steps["promo_update_proxy"] = steps["promo_proxy"]
    steps["approve_proxy"] = _mask_proxy(approve_proxy)
    steps["billing_proxy_country"] = billing_proxy_country
    steps["checkout_country"] = billing_country
    steps["checkout_currency"] = currency
    steps["promo_country"] = promo_country
    steps["promo_create_mode"] = promo_create_mode
    steps["attempt"] = attempt
    _log(log_fn, f"[BA提取] checkout/Stripe/PayPal代理={steps['checkout_proxy']} promotion代理={steps['promo_update_proxy']}")
    _emit(progress_cb, {"type": "progress", "step": 1, "total": 7, "desc": f"使用代理 {steps['checkout_proxy']} / {steps['promo_update_proxy']}", "attempt": attempt})

    s_checkout = build_protocol_session(cookies_str=cookies, proxy=checkout_proxy)
    s_promo = s_checkout if promo_proxy == checkout_proxy else build_protocol_session(cookies_str=cookies, proxy=promo_proxy)
    s_approve = s_checkout if approve_proxy == checkout_proxy else build_protocol_session(cookies_str=cookies, proxy=approve_proxy)

    bill_addr = billing_address_for(billing_country, email=email)

    # 1) checkout create on checkout IP
    _raise_if_cancelled(cancel_check)
    create_with_promo = promo_create_mode == "create_with_promo"
    checkout_desc = "创建带优惠 PayPal checkout" if create_with_promo else "创建原价 PayPal checkout"
    _emit(progress_cb, {"type": "progress", "step": 2, "total": 7, "desc": checkout_desc, "attempt": attempt})
    _log(log_fn, f"[BA提取] {checkout_desc} session（checkout IP）")
    payload = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": billing_country, "currency": currency},
        "entry_point": "all_plans_pricing_modal",
        "checkout_ui_mode": "custom",
    }
    promo_campaign = {
        "promo_campaign_id": "plus-1-month-free",
        "is_coupon_from_query_param": False,
    }
    if create_with_promo:
        payload["promo_campaign"] = promo_campaign
    checkout_resp = s_checkout.post(
        PAYMENT_CHECKOUT_URL,
        headers=_auth_headers(token, cookies),
        json=payload,
        timeout=30,
    )
    steps["checkout_http"] = getattr(checkout_resp, "status_code", None)
    if getattr(checkout_resp, "status_code", 0) != 200:
        body = str(getattr(checkout_resp, "text", "") or "")[:180]
        return {"ok": False, "error": f"checkout HTTP {steps['checkout_http']}: {body}", "steps": steps}
    data = checkout_resp.json() if hasattr(checkout_resp, "json") else {}
    if not isinstance(data, dict):
        return {"ok": False, "error": "checkout 响应非 JSON 对象", "steps": steps}

    cs_id = _text(data.get("checkout_session_id") or data.get("cs_id"))
    entity = _text(data.get("processor_entity"), "openai_llc")
    publishable_key = _text(data.get("publishable_key")) or getattr(stripe_http, "STRIPE_PUBLISHABLE_KEY", "")
    if not cs_id:
        return {"ok": False, "error": f"checkout 未返回 session id: {str(data)[:180]}", "steps": steps}
    steps["cs_id"] = cs_id
    steps["processor_entity"] = entity
    steps["publishable_key"] = publishable_key[:18] + "..." if publishable_key else ""
    steps["promo_campaign"] = data.get("promo_campaign")
    _log(log_fn, f"[BA提取] cs={cs_id} entity={entity}")

    # 2) stripe init on checkout/payment IP. Pool 2 is the PayPal billing/payment side.
    _raise_if_cancelled(cancel_check)
    _emit(progress_cb, {"type": "progress", "step": 3, "total": 7, "desc": "stripe init", "attempt": attempt})
    _log(log_fn, "[BA提取] Stripe init（账单/支付 IP）")
    latest = stripe_http.stripe_init(s_checkout, cs_id=cs_id, publishable_key=publishable_key)
    steps["init"] = 200
    init_checksum = _text(latest.get("init_checksum"))
    config_id = _text(latest.get("config_id"))
    pmt = _payment_method_types(latest)
    amount = stripe_http.extract_expected_amount(latest)
    steps["amount_after_init"] = amount
    steps["paypal_after_init"] = _has_paypal(pmt)
    steps["pmt_after_init"] = pmt
    _log(log_fn, f"[BA提取] init amount={amount} paypal={steps['paypal_after_init']} pmt={pmt}")

    if not _has_paypal(pmt):
        return {
            "ok": False,
            "error": f"当前支付线路未开放 PayPal，可用方式：{', '.join(pmt) or '-'}",
            "steps": steps,
        }

    # 3) Promo mode:
    # - update_after_checkout: create original checkout first, then apply promotion on pool 1.
    # - create_with_promo: create checkout with promo_campaign, then only refresh Stripe.
    _raise_if_cancelled(cancel_check)
    if create_with_promo:
        steps["chatgpt_promo_http"] = "create_with_promo"
        _emit(progress_cb, {"type": "progress", "step": 3, "total": 7, "desc": "checkout 已带优惠，正在刷新 Stripe", "attempt": attempt})
        _log(log_fn, "[BA提取] checkout 创建时已带 promotion，跳过后置 update")
    else:
        _emit(progress_cb, {"type": "progress", "step": 3, "total": 7, "desc": "正在应用优惠", "attempt": attempt})
        _log(log_fn, f"[BA提取] 后置应用 ChatGPT promotion（优惠更新 IP={promo_country}）")
        promo_update_resp = s_promo.post(
            f"https://chatgpt.com{CHECKOUT_UPDATE_PATH}",
            headers=_auth_headers(
                token,
                cookies,
                {
                    "Referer": f"https://chatgpt.com/checkout/{entity}/{cs_id}",
                    "x-openai-target-path": CHECKOUT_UPDATE_PATH,
                    "x-openai-target-route": CHECKOUT_UPDATE_PATH,
                },
            ),
            json={
                "checkout_session_id": cs_id,
                "processor_entity": entity,
                "plan_name": "chatgptplusplan",
                "price_interval": "month",
                "seat_quantity": 1,
                "promo_campaign": promo_campaign,
            },
            timeout=20,
        )
        steps["chatgpt_promo_http"] = getattr(promo_update_resp, "status_code", None)
        body = str(getattr(promo_update_resp, "text", "") or "")
        steps["chatgpt_promo_body"] = body[:160]
        _log(log_fn, f"[BA提取] promotion update HTTP {steps['chatgpt_promo_http']}: {body[:120]}")
        if getattr(promo_update_resp, "status_code", 0) != 200:
            return {"ok": False, "error": f"应用 Plus 优惠失败：HTTP {steps['chatgpt_promo_http']} {body[:180]}", "steps": steps}
        try:
            promo_body = promo_update_resp.json() if hasattr(promo_update_resp, "json") else {}
        except Exception:
            promo_body = {}
        if isinstance(promo_body, dict) and promo_body.get("success") is False:
            return {"ok": False, "error": f"优惠更新失败: {body[:180]}", "steps": steps}
        _emit(progress_cb, {"type": "progress", "step": 3, "total": 7, "desc": "优惠已应用，正在刷新 Stripe", "attempt": attempt})
    for sync_idx in range(1, 7):
        _raise_if_cancelled(cancel_check)
        time.sleep(1)
        latest = stripe_http.stripe_init(s_checkout, cs_id=cs_id, publishable_key=publishable_key)
        init_checksum = _text(latest.get("init_checksum"), init_checksum)
        config_id = _text(latest.get("config_id"), config_id)
        amount = stripe_http.extract_expected_amount(latest)
        pmt = _payment_method_types(latest) or pmt
        steps[f"promo_sync_{sync_idx}_amount"] = amount
        _log(log_fn, f"[BA提取] 优惠同步检查 {sync_idx}/6：amount={amount} pmt={pmt}")
        if str(amount) == "0":
            break

    # 4) amount/payment-method check after promotion sync.
    _emit(progress_cb, {"type": "progress", "step": 4, "total": 7, "desc": "金额校验", "attempt": attempt, "amount": amount, "paypal": _has_paypal(pmt)})
    if not _has_paypal(pmt):
        return {
            "ok": False,
            "error": f"当前 session 无 PayPal 支付方式: pmt={pmt} amount={amount}",
            "steps": steps,
        }
    if str(amount) != "0":
        return {
            "ok": False,
            "error": f"Plus 首月免费优惠未生效：Stripe 今日应付 amount={amount}",
            "steps": steps,
        }
    if not init_checksum:
        return {"ok": False, "error": "缺少 init_checksum，无法 confirm", "steps": steps}

    expected_amount, expected_on_bca = stripe_http.extract_confirm_expected_amounts(
        latest, fallback_amount=str(amount or "0")
    )
    displayed = stripe_http.extract_display_amounts(latest) if hasattr(stripe_http, "extract_display_amounts") else {}
    steps["expected_amount"] = expected_amount
    steps["expected_amount_on_bca"] = expected_on_bca

    success_url = (
        f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}"
        f"&processor_entity={entity}&plan_type=plus"
    )
    return_url = stripe_http.build_confirm_return_url(latest, cs_id=cs_id, fallback_url=success_url)
    referrer = stripe_http.build_confirm_referrer_url(
        latest, cs_id=cs_id, fallback_url=f"https://pay.openai.com/c/pay/{cs_id}"
    )

    # 5) confirm paypal on billing/payment IP
    _raise_if_cancelled(cancel_check)
    _emit(progress_cb, {"type": "progress", "step": 5, "total": 7, "desc": "创建 PayPal PaymentMethod", "attempt": attempt})
    _log(log_fn, f"[BA提取] Stripe confirm PayPal（mode={confirm_mode}）")
    redirect_url = ""
    if confirm_mode == "direct":
        confirm_resp = stripe_http.stripe_confirm_paypal_direct(
            s_checkout,
            cs_id=cs_id,
            init_checksum=init_checksum,
            email=email,
            address=bill_addr,
            return_url=return_url,
            expected_amount=expected_amount,
            expected_amount_on_bca=expected_on_bca,
            displayed_amounts=displayed,
            referrer=referrer,
            publishable_key=publishable_key,
        )
        _emit(progress_cb, {"type": "progress", "step": 6, "total": 7, "desc": "confirm -> approve -> poll", "attempt": attempt})
        steps["confirm"] = "direct"
        redirect_url = _find_redirect(confirm_resp)
    else:
        device = stripe_http.StripeDeviceContext()
        pm_resp = stripe_http.stripe_create_paypal_payment_method(
            s_checkout,
            cs_id=cs_id,
            address=bill_addr,
            email=email,
            device=device,
            config_id=config_id,
            publishable_key=publishable_key,
        )
        payment_method_id = _text(pm_resp.get("id"))
        steps["payment_method_id"] = payment_method_id
        if not payment_method_id.startswith("pm_"):
            return {"ok": False, "error": f"创建 PayPal payment_method 失败: {payment_method_id or pm_resp}", "steps": steps}
        confirm_resp = stripe_http.stripe_confirm_paypal_with_payment_method(
            s_checkout,
            cs_id=cs_id,
            payment_method_id=payment_method_id,
            init_checksum=init_checksum,
            return_url=return_url,
            expected_amount=expected_amount,
            expected_amount_on_bca=expected_on_bca,
            displayed_amounts=displayed,
            referrer=referrer,
            config_id=config_id,
            publishable_key=publishable_key,
        )
        _emit(progress_cb, {"type": "progress", "step": 6, "total": 7, "desc": "confirm -> approve -> poll", "attempt": attempt})
        steps["confirm"] = "pm"
        redirect_url = _find_redirect(confirm_resp)

    sa = (confirm_resp or {}).get("submission_attempt") if isinstance(confirm_resp, dict) else {}
    sa_state = _text((sa or {}).get("state")) if isinstance(sa, dict) else ""
    confirm_error = _payload_error_summary(confirm_resp)
    confirm_preview = _compact_payload(confirm_resp, limit=300)
    confirm_diag = "" if redirect_url else _payload_redirect_diagnostics(confirm_resp, limit=220)
    steps["confirm_state"] = sa_state
    steps["confirm_error"] = confirm_error
    steps["confirm_preview"] = confirm_preview
    steps["confirm_redirect_diag"] = confirm_diag
    confirm_tail = (" err=" + confirm_error) if confirm_error else ((" diag=" + confirm_diag) if confirm_diag else " body=" + confirm_preview)
    _log(log_fn, f"[BA提取] confirm state={sa_state or '-'} redirect={'yes' if redirect_url else 'no'} {confirm_tail.strip()}")
    _emit(progress_cb, {
        "type": "progress",
        "step": 6,
        "total": 7,
        "desc": f"confirm={sa_state or '-'} redirect={'有' if redirect_url else '无'}{confirm_tail}",
        "attempt": attempt,
    })

    # 6) optional chatgpt approve when requires_approval
    if sa_state == "requires_approval" or not redirect_url:
        _raise_if_cancelled(cancel_check)
        _emit(progress_cb, {"type": "progress", "step": 6, "total": 7, "desc": "正在 approve", "attempt": attempt})
        _log(log_fn, "[BA提取] ChatGPT approve checkout")
        try:
            s_approve.post(
                "https://chatgpt.com/backend-api/sentinel/ping",
                json={},
                headers={
                    "x-openai-target-path": "/backend-api/sentinel/ping",
                    "x-openai-target-route": "/backend-api/sentinel/ping",
                },
                timeout=4,
            )
        except Exception:
            pass
        approve_resp = s_approve.post(
            f"https://chatgpt.com{APPROVE_PATH}",
            json={"checkout_session_id": cs_id, "processor_entity": entity},
            headers=_auth_headers(
                token,
                cookies,
                {
                    "Referer": f"https://chatgpt.com/checkout/{entity}/{cs_id}",
                    "x-openai-target-path": APPROVE_PATH,
                    "x-openai-target-route": APPROVE_PATH,
                },
            ),
            timeout=20,
        )
        steps["approve_http"] = getattr(approve_resp, "status_code", None)
        approve_preview = _response_preview(approve_resp, limit=300)
        try:
            approve_body = approve_resp.json() if hasattr(approve_resp, "json") else {}
        except Exception:
            approve_body = {}
        approve_result = _text((approve_body or {}).get("result"), f"http_{steps['approve_http']}")
        steps["approve_result"] = approve_result
        steps["approve_preview"] = approve_preview
        _log(log_fn, f"[BA提取] approve http={steps['approve_http']} result={approve_result} body={approve_preview}")
        _emit(progress_cb, {
            "type": "progress",
            "step": 6,
            "total": 7,
            "desc": f"approve http={steps['approve_http']} result={approve_result} body={approve_preview}",
            "attempt": attempt,
        })
        if int(steps.get("approve_http") or 0) != 200 or approve_result != "approved":
            return {"ok": False, "error": f"approve 未通过: {approve_result}", "steps": steps}

        # poll payment_pages for redirect
        _log(log_fn, "[BA提取] 轮询 Stripe redirect")
        poll_params = {
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[session_id]": "es_" + uuid.uuid4().hex[:11],
            "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
            "elements_session_client[locale]": "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": publishable_key or stripe_http.STRIPE_PUBLISHABLE_KEY,
            "_stripe_version": getattr(stripe_http, "STRIPE_VERSION", "2020-08-27;custom_checkout_beta=v1"),
        }
        poll_headers = {
            "Origin": "https://pay.openai.com",
            "Referer": "https://pay.openai.com/",
            "Accept": "application/json",
        }
        max_poll_attempts = 10
        deadline = time.time() + 15
        poll_attempt = 0
        static_no_redirect_polls = 0
        last_poll_page_id = ""
        while time.time() < deadline and poll_attempt < max_poll_attempts and not redirect_url:
            _raise_if_cancelled(cancel_check)
            time.sleep(1.5)
            poll_attempt += 1
            _emit(progress_cb, {
                "type": "progress",
                "step": 6,
                "total": 7,
                "desc": f"轮询 PayPal 跳转 {poll_attempt}/{max_poll_attempts}",
                "attempt": attempt,
            })
            poll_resp = s_approve.get(
                f"https://api.stripe.com/v1/payment_pages/{cs_id}",
                params=poll_params,
                headers=poll_headers,
                timeout=10,
            )
            poll_status = getattr(poll_resp, "status_code", None)
            poll_preview = _response_preview(poll_resp, limit=300)
            steps["last_poll_http"] = poll_status
            steps["last_poll_preview"] = poll_preview
            if getattr(poll_resp, "status_code", 0) != 200:
                steps["poll_http"] = poll_status
                _emit(progress_cb, {
                    "type": "progress",
                    "step": 6,
                    "total": 7,
                    "desc": f"poll {poll_attempt}/{max_poll_attempts} http={poll_status} body={poll_preview}",
                    "attempt": attempt,
                })
                continue
            try:
                poll_body = poll_resp.json()
            except Exception:
                _emit(progress_cb, {
                    "type": "progress",
                    "step": 6,
                    "total": 7,
                    "desc": f"poll {poll_attempt}/{max_poll_attempts} JSON解析失败 body={poll_preview}",
                    "attempt": attempt,
                })
                continue
            redirect_url = _find_redirect(poll_body)
            sa_payload = (poll_body or {}).get("submission_attempt") if isinstance(poll_body, dict) else {}
            poll_state = _text((sa_payload or {}).get("state")) if isinstance(sa_payload, dict) else ""
            poll_error = _payload_error_summary(poll_body)
            poll_diag = "" if redirect_url else _payload_redirect_diagnostics(poll_body, limit=220)
            poll_tail = (" err=" + poll_error) if poll_error else ((" diag=" + poll_diag) if poll_diag else " body=" + poll_preview)
            steps["last_poll_redirect_diag"] = poll_diag
            _log(log_fn, f"[BA提取] poll {poll_attempt}/{max_poll_attempts} http={poll_status} state={poll_state or '-'} redirect={'yes' if redirect_url else 'no'} {poll_tail.strip()}")
            _emit(progress_cb, {
                "type": "progress",
                "step": 6,
                "total": 7,
                "desc": f"poll {poll_attempt}/{max_poll_attempts} http={poll_status} state={poll_state or '-'} redirect={'有' if redirect_url else '无'}{poll_tail}",
                "attempt": attempt,
            })
            if isinstance(sa_payload, dict):
                steps["poll_state"] = sa_payload.get("state")
                if sa_payload.get("error"):
                    err = sa_payload.get("error")
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    return {"ok": False, "error": f"Stripe poll failed: {msg}", "steps": steps}
            poll_page_id = _text(poll_body.get("id")) if isinstance(poll_body, dict) else ""
            if not redirect_url and not poll_state and not poll_error and poll_page_id:
                if poll_page_id == last_poll_page_id:
                    static_no_redirect_polls += 1
                else:
                    last_poll_page_id = poll_page_id
                    static_no_redirect_polls = 1
                if static_no_redirect_polls >= MAX_STATIC_NO_REDIRECT_POLLS:
                    steps["poll_static_no_redirect"] = static_no_redirect_polls
                    message = f"连续 {static_no_redirect_polls} 次 poll 只返回 {poll_page_id}，无 PayPal redirect，切换下一代理"
                    _log(log_fn, f"[BA提取] {message}")
                    _emit(progress_cb, {
                        "type": "progress",
                        "step": 6,
                        "total": 7,
                        "desc": message,
                        "attempt": attempt,
                    })
                    break
            else:
                static_no_redirect_polls = 0

    if not redirect_url:
        detail = _text(steps.get("last_poll_preview") or steps.get("approve_preview") or steps.get("confirm_preview"))
        suffix = f": {detail}" if detail else ""
        return {"ok": False, "error": f"未拿到 Stripe/PayPal redirect{suffix}", "steps": steps}

    steps["redirect_url"] = redirect_url[:180]
    _emit(progress_cb, {"type": "progress", "step": 7, "total": 7, "desc": "解析 BA 链", "attempt": attempt})
    _log(log_fn, "[BA提取] 解析 PayPal BA 链")
    ok, ba_token, ba_url = _follow_ba(s_checkout, redirect_url)
    if not ok or not ba_token:
        return {
            "ok": False,
            "error": f"redirect 未包含 ba_token: {(ba_url or redirect_url)[:120]}",
            "steps": steps,
            "data": {"redirect_url": redirect_url, "final_url": ba_url},
        }

    _log(log_fn, f"[BA提取] 成功 {ba_token}")
    if billing_proxy:
        try:
            proxy_pool.report_success(billing_proxy, region=billing_proxy_country)
        except Exception:
            pass
    if promo_proxy and promo_proxy != billing_proxy:
        try:
            proxy_pool.report_success(promo_proxy, region=promo_country)
        except Exception:
            pass

    return {
        "ok": True,
        "error": "",
        "ba_token": ba_token,
        "ba_url": ba_url or redirect_url,
        "redirect_url": redirect_url,
        "cs_id": cs_id,
        "amount": amount,
        "expected_amount": expected_amount,
        "billing_country": billing_country,
        "promo_country": promo_country,
        "billing_proxy": steps["billing_proxy"],
        "promo_proxy": steps["promo_proxy"],
        "steps": steps,
        "data": {
            "ba_token": ba_token,
            "ba_url": ba_url or redirect_url,
            "url": ba_url or redirect_url,
            "pp_ba_token": ba_token,
            "cashier_url": ba_url or redirect_url,
            "cs_id": cs_id,
            "amount": amount,
            "billing_country": billing_country,
            "promo_country": promo_country,
            "steps": steps,
            "message": f"已提取 BA 链 {ba_token}",
        },
    }
