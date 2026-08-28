"""GoPay 支付链接提取路径（纯协议版）。

从外部参考脚本移植而来，提供双兼容的纯协议 GoPay 提链路径：

    create(ID/IDR)
      -> authoritative fetch / coupon update
      -> Stripe Elements 动态映射 display_name=GoPay 的 cpmt_
      -> taxes
      -> authoritative fetch / 必要时重新映射 cpmt_
      -> custom confirm
      -> custom payment method start
      -> requires_action.next_action.url

普通 ``cs_`` 会话走另一条尾部：

    Stripe init
      -> capability check(gopay)
      -> Stripe PaymentMethod
      -> Stripe confirm
      -> approve/poll
      -> provider redirect

本模块只负责协议逻辑，不包含 HTTP Session、代理、TOKEN、重试、GUI、持久化
或命令行入口；``Transport.send`` 由
``platforms.chatgpt.gopay_link_transport.CurlCffiTransport`` 实现。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import parse_qsl, unquote, urlsplit


JsonObject = dict[str, Any]
Params = tuple[tuple[str, str], ...]
Trace = Callable[[str], None]

COUNTRY = "ID"
CURRENCY = "IDR"
ELEMENTS_CURRENCY = "idr"
PAYMENT_METHOD_TYPE = "gopay"
PAYMENT_METHOD_DISPLAY_NAME = "GoPay"
CHATGPT_BASE_URL = "https://chatgpt.com"
STRIPE_ELEMENTS_URL = "https://api.stripe.com/v1/elements/sessions"
STRIPE_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
TRUSTED_AMOUNT_SOURCE = "checkout_state.total.total.minorUnitsAmount"

OAICS_ID_RE = re.compile(r"^oaics_[A-Za-z0-9_]+$")
CHECKOUT_ID_RE = re.compile(r"^(?:oaics_|cs_)[A-Za-z0-9_]+$")
CPMT_RE = re.compile(r"^cpmt_[A-Za-z0-9_]+$")
PK_RE = re.compile(r"pk_(?:live|test)_[A-Za-z0-9]+")

COUPON_DISCOUNT_PERCENTAGES = {
    "plus-1-month-free": 100,
    "plus-1-month-50-pct-off": 50,
    "plus-2-months-50-pct-off": 50,
    "go-1-month-free": 100,
    "go-2-months-50-pct-off": 50,
    "go-3-months-50-pct-off": 50,
}


@dataclass(frozen=True)
class RequestSpec:
    """一条由宿主 Transport 发送并校验 2xx/JSON 的请求。"""

    stage: str
    method: str
    url: str
    headers: Mapping[str, str]
    json: JsonObject | None = None
    data: Mapping[str, str] | None = None
    params: Params = ()


class Transport(Protocol):
    """宿主适配器：负责认证、Cookie、代理、超时、重试及 HTTP 错误。"""

    def send(self, request: RequestSpec) -> JsonObject:
        ...


def _redact_diagnostic_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer ******", text)
    text = re.sub(r"(?:oaics_|cs_)[A-Za-z0-9_]+", "CHECKOUT_SESSION", text)
    text = re.sub(r"https?://[^\s'\"]+", "URL", text)
    return text[:180]


def _trace_response(trace: Trace | None, request: RequestSpec, payload: Any) -> None:
    """输出阶段摘要，不输出 token、完整会话 ID 或支付 URL。"""
    if trace is None:
        return
    keys = sorted(str(key) for key in payload)[:16] if isinstance(payload, dict) else []
    state = _submission_state(payload)
    amount, amount_source = checkout_amount(payload) if isinstance(payload, dict) else ("", "")
    method_count = len(extract_custom_method_ids(payload))
    candidates = _provider_url_candidates(payload)
    hosts: list[str] = []
    for candidate in candidates:
        host = (urlsplit(candidate).hostname or "").lower()
        if host and host not in hosts:
            hosts.append(host)
    fields = [f"stage={request.stage}", f"keys={keys}"]
    if state:
        fields.append(f"state={state}")
    if amount or amount_source:
        fields.append(f"amount={amount or 'missing'}({amount_source})")
    if method_count:
        fields.append(f"custom_methods={method_count}")
    fields.append(f"provider_candidates={len(candidates)}")
    if hosts:
        fields.append(f"provider_hosts={hosts[:3]}")
    trace("GoPay 协议响应摘要：" + "，".join(fields))


def _send(transport: Transport, request: RequestSpec, trace: Trace | None) -> JsonObject:
    try:
        payload = transport.send(request)
    except Exception as exc:
        if trace is not None:
            trace(
                f"GoPay 协议请求失败：stage={request.stage}，"
                f"error={type(exc).__name__}:{_redact_diagnostic_text(exc)}"
            )
        raise
    _trace_response(trace, request, payload)
    return payload


def _checkout_container(value: Any) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    for key in ("checkout_session", "checkoutSession"):
        nested = value.get(key)
        if isinstance(nested, dict):
            return nested
    if any(key in value for key in ("checkout_session_id", "checkoutSessionId")):
        return value
    for key in ("session", "checkout", "data"):
        nested = _checkout_container(value.get(key))
        if nested:
            return nested
    return {}


def _extract_checkout_id(value: Any) -> str:
    if isinstance(value, dict):
        for key in (
            "checkout_session_id",
            "checkoutSessionId",
            "stripe_checkout_session_id",
            "session_id",
            "id",
            "client_secret",
        ):
            found = _extract_checkout_id(value.get(key))
            if found:
                return found
        for child in value.values():
            found = _extract_checkout_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _extract_checkout_id(child)
            if found:
                return found
    elif isinstance(value, str):
        match = re.search(
            r"(?:oaics_|cs_)[A-Za-z0-9_]+",
            value.strip().split("_secret_", 1)[0],
        )
        if match:
            return match.group(0)
    return ""


def _extract_publishable_key(value: Any) -> str:
    if isinstance(value, dict):
        for key in (
            "stripe_publishable_key",
            "publishable_key",
            "publishableKey",
            "stripePublishableKey",
            "key",
        ):
            found = _extract_publishable_key(value.get(key))
            if found:
                return found
        for child in value.values():
            found = _extract_publishable_key(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _extract_publishable_key(child)
            if found:
                return found
    elif isinstance(value, str):
        match = PK_RE.search(value)
        if match:
            return match.group(0)
    return ""


def normalize_checkout(
    payload: Any,
    fallback: Mapping[str, Any] | None = None,
) -> JsonObject:
    """把 create/update/fetch 的不同响应外壳归一为同一 OAICS 结构。"""

    previous = dict(fallback or {})
    source = _checkout_container(payload) or (
        payload if isinstance(payload, dict) else {}
    )
    checkout_id = _extract_checkout_id(source or payload) or str(
        previous.get("checkout_session_id") or previous.get("cs_id") or ""
    )
    if not CHECKOUT_ID_RE.fullmatch(checkout_id):
        raise ValueError("Checkout 响应缺少有效 checkout_session_id")
    billing = source.get("billing_details")
    billing = billing if isinstance(billing, dict) else {}

    def pick(key: str, default: Any = None) -> Any:
        return source.get(key) if key in source else previous.get(key, default)

    return {
        **previous,
        "cs_id": checkout_id,
        "checkout_session_id": checkout_id,
        "checkout_provider": str(
            pick(
                "checkout_provider",
                "open_ai" if checkout_id.startswith("oaics_") else "stripe",
            )
            or ""
        ),
        "processor_entity": str(
            source.get("processor_entity")
            or source.get("processorEntity")
            or previous.get("processor_entity")
            or "openai_ie"
        ),
        "stripe_publishable_key": (
            _extract_publishable_key(source)
            or _extract_publishable_key(payload)
            or str(previous.get("stripe_publishable_key") or "")
        ),
        "billing_country": str(
            billing.get("country") or previous.get("billing_country") or COUNTRY
        ).upper(),
        "currency": str(
            billing.get("currency") or previous.get("currency") or CURRENCY
        ).upper(),
        "checkout_state": pick("checkout_state", {}) or {},
        "customer_session_client_secret": str(
            pick("customer_session_client_secret", "") or ""
        ),
        "payment_method_types": list(pick("payment_method_types", []) or []),
        "custom_payment_methods": list(pick("custom_payment_methods", []) or []),
    }


def checkout_url(checkout: Mapping[str, Any]) -> str:
    entity = str(checkout.get("processor_entity") or "")
    checkout_id = str(checkout.get("checkout_session_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", entity):
        raise ValueError("processor_entity 格式异常")
    if not CHECKOUT_ID_RE.fullmatch(checkout_id):
        raise ValueError("checkout_session_id 格式异常")
    return f"{CHATGPT_BASE_URL}/checkout/{entity}/{checkout_id}"


def route_headers(checkout: Mapping[str, Any], route: str) -> dict[str, str]:
    return {
        "Referer": checkout_url(checkout),
        "x-openai-target-path": route,
        "x-openai-target-route": route,
    }


def create_request(plan_name: str) -> RequestSpec:
    route = "/backend-api/payments/checkout"
    return RequestSpec(
        stage="checkout.create",
        method="POST",
        url=f"{CHATGPT_BASE_URL}{route}",
        headers={
            "Referer": f"{CHATGPT_BASE_URL}/",
            "x-openai-target-path": route,
            "x-openai-target-route": route,
        },
        json={
            "entry_point": "all_plans_pricing_modal",
            "plan_name": plan_name,
            "billing_details": {"country": COUNTRY, "currency": CURRENCY},
            "checkout_ui_mode": "custom",
        },
    )


def fetch_request(checkout: Mapping[str, Any]) -> RequestSpec:
    checkout_id = str(checkout["checkout_session_id"])
    route = (
        "/backend-api/payments/checkout/"
        f"{checkout['processor_entity']}/{checkout_id}"
    )
    return RequestSpec(
        stage="checkout.fetch",
        method="GET",
        url=f"{CHATGPT_BASE_URL}{route}",
        headers=route_headers(checkout, route),
    )


def coupon_update_request(
    checkout: Mapping[str, Any],
    plan_name: str,
    coupon_id: str,
) -> RequestSpec:
    route = "/backend-api/payments/checkout/update"
    return RequestSpec(
        stage="checkout.update",
        method="POST",
        url=f"{CHATGPT_BASE_URL}{route}",
        headers=route_headers(checkout, route),
        json={
            "checkout_session_id": checkout["checkout_session_id"],
            "processor_entity": checkout["processor_entity"],
            "plan_name": plan_name,
            "price_interval": "month",
            "seat_quantity": 1,
            "billing_details": {"country": COUNTRY, "currency": CURRENCY},
            "checkout_ui_mode": "custom",
            "promo_campaign": {
                "promo_campaign_id": coupon_id,
                "is_coupon_from_query_param": False,
            },
        },
    )


def checkout_amount(checkout: Mapping[str, Any]) -> tuple[str, str]:
    state = checkout.get("checkout_state")
    totals = state.get("total") if isinstance(state, dict) else None
    final = totals.get("total") if isinstance(totals, dict) else None
    if not isinstance(final, dict) or "minorUnitsAmount" not in final:
        return "", f"{TRUSTED_AMOUNT_SOURCE}.missing"
    raw = final.get("minorUnitsAmount")
    amount = str(raw).strip() if raw is not None else ""
    if isinstance(raw, bool) or not re.fullmatch(r"\d+", amount):
        return amount, f"{TRUSTED_AMOUNT_SOURCE}.invalid"
    return amount, TRUSTED_AMOUNT_SOURCE


def require_amount(checkout: Mapping[str, Any], expected: str, stage: str) -> None:
    amount, source = checkout_amount(checkout)
    if source != TRUSTED_AMOUNT_SOURCE or amount != str(expected):
        raise ValueError(
            f"{stage} 金额校验失败: expected={expected}, "
            f"actual={amount or 'missing'}, source={source}"
        )


def validate_coupon_amounts(
    coupon_id: str,
    before_amount: str,
    after_amount: str,
) -> JsonObject:
    percentage = COUPON_DISCOUNT_PERCENTAGES.get(coupon_id)
    if percentage is None:
        raise ValueError(f"未知优惠券: {coupon_id}")
    before = int(before_amount)
    after = int(after_amount)
    if percentage == 100:
        if after != 0:
            raise ValueError(f"100% 优惠后金额不是 0: {after}")
        return {"discount_check": "passed", "discount_percentage": 100}
    expected_numerator = before * (100 - percentage)
    actual_numerator = after * 100
    rounding_delta = abs(actual_numerator - expected_numerator)
    if not (before > 0 and 0 <= after < before and rounding_delta <= 50):
        raise ValueError(
            f"优惠比例校验失败: coupon={coupon_id}, before={before}, after={after}"
        )
    return {
        "discount_check": "passed",
        "discount_percentage": percentage,
        "discount_rounding_delta_hundredths": rounding_delta,
    }


def extract_custom_method_ids(value: Any) -> list[str]:
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            methods = item.get("custom_payment_methods")
            if isinstance(methods, list):
                for method in methods:
                    method_id = str(
                        method.get("id") if isinstance(method, dict) else method
                    ).strip()
                    if CPMT_RE.fullmatch(method_id) and method_id not in found:
                        found.append(method_id)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def elements_request(
    checkout: Mapping[str, Any],
    stripe_js_id: str,
) -> RequestSpec:
    checkout_id = str(checkout.get("checkout_session_id") or "")
    if not OAICS_ID_RE.fullmatch(checkout_id):
        raise ValueError("Elements 路径只接受 oaics_ 会话")
    amount, source = checkout_amount(checkout)
    if source != TRUSTED_AMOUNT_SOURCE:
        raise ValueError(f"OAICS 权威金额缺失: {source}")
    publishable_key = str(checkout.get("stripe_publishable_key") or "")
    if not PK_RE.fullmatch(publishable_key):
        raise ValueError("OAICS Checkout 缺少 Stripe publishable key")
    custom_ids = extract_custom_method_ids(checkout)
    if not custom_ids:
        raise ValueError("OAICS Checkout 缺少 custom_payment_methods")
    payment_methods = [
        str(item).strip().lower()
        for item in checkout.get("payment_method_types") or []
        if str(item).strip()
    ]
    params: list[tuple[str, str]] = [
        ("client_betas[0]", "custom_checkout_server_updates_1"),
        ("client_betas[1]", "custom_checkout_manual_approval_1"),
    ]
    customer_secret = str(checkout.get("customer_session_client_secret") or "")
    if customer_secret:
        params.append(("customer_session_client_secret", customer_secret))
    params.extend(
        [
            ("deferred_intent[mode]", "subscription"),
            ("deferred_intent[amount]", amount),
            ("deferred_intent[currency]", ELEMENTS_CURRENCY),
            ("deferred_intent[setup_future_usage]", "off_session"),
        ]
    )
    params.extend(
        (f"deferred_intent[payment_method_types][{index}]", method)
        for index, method in enumerate(payment_methods)
    )
    params.extend(
        [
            ("currency", ELEMENTS_CURRENCY),
            ("key", publishable_key),
            ("_stripe_version", STRIPE_VERSION),
            ("elements_init_source", "stripe.elements"),
            ("referrer_host", "chatgpt.com"),
            ("stripe_js_id", stripe_js_id),
            ("locale", "id-ID"),
        ]
    )
    params.extend(
        (f"custom_payment_methods[{index}]", method_id)
        for index, method_id in enumerate(custom_ids)
    )
    params.append(("type", "deferred_intent"))
    return RequestSpec(
        stage="stripe.elements.sessions",
        method="GET",
        url=STRIPE_ELEMENTS_URL,
        headers={"Referer": CHATGPT_BASE_URL},
        params=tuple(params),
    )


def select_gopay_method(
    elements_payload: Mapping[str, Any],
    allowed_ids: list[str],
) -> JsonObject:
    candidates: list[JsonObject] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            methods = item.get("custom_payment_method_data")
            if isinstance(methods, list):
                candidates.extend(
                    dict(method) for method in methods if isinstance(method, dict)
                )
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(elements_payload)
    allowed = set(allowed_ids)
    wanted = re.sub(r"\s+", "", PAYMENT_METHOD_DISPLAY_NAME).casefold()
    for item in candidates:
        method_id = str(item.get("type") or item.get("id") or "").strip()
        label = re.sub(
            r"\s+", "", str(item.get("display_name") or "")
        ).casefold()
        if (
            label == wanted
            and CPMT_RE.fullmatch(method_id)
            and method_id in allowed
        ):
            return {**item, "type": method_id}
    labels = [
        str(item.get("display_name") or item.get("type") or "")
        for item in candidates
    ]
    raise ValueError(f"Elements 未映射到 GoPay；available={labels}")


def taxes_request(
    checkout: Mapping[str, Any],
    billing: Mapping[str, str],
) -> RequestSpec:
    required = ("name", "email", "line1", "city", "state", "postal_code")
    missing = [key for key in required if not str(billing.get(key) or "").strip()]
    if missing:
        raise ValueError(f"账单字段缺失: {missing}")
    route = "/backend-api/payments/checkout/taxes"
    return RequestSpec(
        stage="checkout.taxes",
        method="POST",
        url=f"{CHATGPT_BASE_URL}{route}",
        headers=route_headers(checkout, route),
        json={
            "checkout_session_id": checkout["checkout_session_id"],
            "checkout_email": billing["email"],
            "billing_country": COUNTRY,
            "billing_name": billing["name"],
            "currency": CURRENCY,
            "processor_entity": checkout["processor_entity"],
            "billing_address": {
                "line1": billing["line1"],
                "city": billing["city"],
                "country": COUNTRY,
                "postal_code": billing["postal_code"],
                "state": billing["state"],
            },
        },
    )


def _require_current_method(checkout: Mapping[str, Any], method_id: str) -> None:
    if not CPMT_RE.fullmatch(method_id):
        raise ValueError("动态支付方式 ID 格式异常")
    if method_id not in extract_custom_method_ids(checkout):
        raise ValueError("动态支付方式不属于当前 Checkout")


def confirm_request(
    checkout: Mapping[str, Any],
    method_id: str,
) -> RequestSpec:
    _require_current_method(checkout, method_id)
    route = "/backend-api/payments/checkout/confirm"
    return RequestSpec(
        stage="checkout.custom_confirm",
        method="POST",
        url=f"{CHATGPT_BASE_URL}{route}",
        headers=route_headers(checkout, route),
        json={
            "checkout_session_id": checkout["checkout_session_id"],
            "selected_payment_method_type": method_id,
        },
    )


def start_request(
    checkout: Mapping[str, Any],
    method_id: str,
) -> RequestSpec:
    _require_current_method(checkout, method_id)
    route = "/backend-api/payments/checkout/custom_payment_method/start"
    return RequestSpec(
        stage="checkout.custom_payment_method.start",
        method="POST",
        url=f"{CHATGPT_BASE_URL}{route}",
        headers=route_headers(checkout, route),
        json={
            "checkout_session_id": checkout["checkout_session_id"],
            "custom_payment_method_type_id": method_id,
        },
    )


def is_provider_payment_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value or "").strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    host = parsed.hostname.lower()
    if host in {"chatgpt.com", "pay.openai.com"}:
        return False
    # Stripe 全域（checkout/api/js/m 等）与资源代理 CDN（stripe-camo.global.
    # ssl.fastly.net、d1wqzb5bdbcre6.cloudfront.net 等）都不是 GoPay 支付
    # 跳转目标本身；不排除会把 Stripe 的图片/静态资源 URL 误当成支付链接。
    # CAMO/CloudFront 包装的真实支付链接由 _decode_camo_url 还原后再判断。
    if host == "stripe.com" or host.endswith(".stripe.com"):
        return False
    if host.endswith(".fastly.net") or host.endswith(".cloudfront.net"):
        return False
    return not (
        host.endswith(".chatgpt.com")
        or host == "openai.com"
        or host.endswith(".openai.com")
    )


# Stripe CAMO / CloudFront 资源代理路径：/{sha256}/{hex(原始URL)}
_CAMO_PATH_RE = re.compile(r"^/[^/]+/([0-9a-fA-F]+)$")


def _decode_camo_url(value: str) -> str:
    """还原 Stripe CAMO/CloudFront 资源代理 URL 里包裹的真实 URL。

    https://{camo_host}/{sha256}/{hex(url)} 的末尾路径段是原始 URL 的 hex
    编码（实测 Stripe 会把 GoPay 支付跳转链接包进这种代理，浏览器跟随后会
    302 到真实支付页）。解码成功且为 http(s) URL 才返回，否则返回空串。
    """
    try:
        parsed = urlsplit(str(value or "").strip())
    except Exception:
        return ""
    match = _CAMO_PATH_RE.match(parsed.path or "")
    if not match:
        return ""
    encoded = match.group(1)
    if len(encoded) % 2 != 0:
        return ""
    try:
        decoded = bytes.fromhex(encoded).decode("utf-8")
    except Exception:
        return ""
    if decoded.startswith(("http://", "https://")):
        return decoded
    return ""


_EMBEDDED_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)


def _iter_url_values(value: Any, *, depth: int = 0):
    """展开响应中的 URL、URL 编码字符串和 JSON 字符串。"""
    if isinstance(value, dict):
        for child in value.values():
            yield from _iter_url_values(child, depth=depth)
        return
    if isinstance(value, list):
        for child in value:
            yield from _iter_url_values(child, depth=depth)
        return
    if not isinstance(value, str):
        return
    raw = value.strip()
    if not raw:
        return
    yield raw
    decoded = unquote(raw)
    if decoded != raw:
        yield decoded
    if depth >= 2:
        return
    for candidate in (raw, decoded):
        if candidate[:1] not in "[{":
            continue
        try:
            nested = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        yield from _iter_url_values(nested, depth=depth + 1)
    for match in _EMBEDDED_URL_RE.findall(decoded):
        yield match.rstrip(".,;)")


def _resolve_provider_url(value: str, *, depth: int = 0) -> str:
    """归一 provider URL，兼容 Stripe 包装、URL 编码和 JSON 字符串。"""
    seen: set[str] = set()
    for raw in _iter_url_values(value):
        candidate = str(raw or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        decoded = _decode_camo_url(candidate)
        for resolved in (decoded, candidate):
            if resolved and is_provider_payment_url(resolved):
                return resolved
        if depth < 1:
            try:
                query_values = parse_qsl(urlsplit(candidate).query, keep_blank_values=True)
            except Exception:
                query_values = []
            for _key, query_value in query_values:
                resolved = _resolve_provider_url(query_value, depth=depth + 1)
                if resolved:
                    return resolved
    return ""


def _provider_url_candidates(payload: Any) -> list[str]:
    """提取去重后的 provider URL，排除 return/cancel 等业务回跳地址。"""
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).casefold() in {
                    "return_url",
                    "success_return_url",
                    "cancel_url",
                    "checkout_url",
                    "stripe_hosted_url",
                }:
                    continue
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        else:
            resolved = _resolve_provider_url(str(item or ""))
            if resolved and resolved not in found:
                found.append(resolved)

    visit(payload)
    return found


def extract_gopay_result(start_payload: Mapping[str, Any]) -> JsonObject:
    status = str(start_payload.get("status") or "").strip().lower()
    candidates = _provider_url_candidates(start_payload)
    provider_url = next((item for item in candidates if item), "")
    if status != "requires_action" or not provider_url:
        raise ValueError(
            "GoPay custom start 未产生浏览器支付链接；"
            f"status={status or 'missing'}, fields={sorted(start_payload)[:12]}"
        )
    parsed = urlsplit(provider_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    is_adyen = (
        (parsed.hostname or "").lower().endswith("adyen.com")
        and parsed.path.rstrip("/")
        == "/checkoutshopper/checkoutPaymentRedirect"
        and bool(str(query.get("redirectData") or ""))
    )
    return {
        "payment_method_type": PAYMENT_METHOD_TYPE,
        "payment_link_type": "gopay_custom_payment_method_redirect",
        "provider_redirect_url": provider_url,
        "long_url": provider_url,
        "gopay_redirect_url": provider_url,
        "adyen_redirect_url": provider_url if is_adyen else "",
        "oaics_custom_payment_method_start_status": status,
        "oaics_approve_attempted": False,
        "redirect_followed": False,
        "candidate_urls": [url for url in candidates if url],
    }


def _extract_oaics_gopay_payment_link(
    transport: Transport,
    *,
    plan_name: str,
    billing: Mapping[str, str],
    coupon_id: str = "none",
    expected_amount: str = "",
    stripe_js_id: str = "STRIPE_JS_ID",
    initial_checkout: Mapping[str, Any] | None = None,
    trace: Trace | None = None,
) -> JsonObject:
    """OAICS GoPay 分支；网络行为全部通过注入的 Transport 完成。"""

    checkout = dict(initial_checkout or {})
    if not checkout:
        checkout = normalize_checkout(
            _send(transport, create_request(plan_name), trace),
            {"plan_name": plan_name, "billing_country": COUNTRY, "currency": CURRENCY},
        )
    if not OAICS_ID_RE.fullmatch(str(checkout["checkout_session_id"])):
        raise ValueError("GoPay 自定义支付路径要求 create 返回 oaics_ 会话")

    checkout = normalize_checkout(_send(transport, fetch_request(checkout), trace), checkout)
    before_amount, before_source = checkout_amount(checkout)
    if before_source != TRUSTED_AMOUNT_SOURCE:
        raise ValueError(f"优惠前权威金额缺失: {before_source}")

    discount_result: JsonObject = {}
    normalized_coupon = str(coupon_id or "none").strip()
    if normalized_coupon != "none":
        update_payload = _send(
            transport, coupon_update_request(checkout, plan_name, normalized_coupon), trace
        )
        if update_payload.get("success") is not True:
            raise ValueError("checkout.update 未明确返回 success=true")
        replacement = _checkout_container(update_payload)
        if replacement:
            checkout = normalize_checkout(replacement, checkout)
        checkout = normalize_checkout(
            _send(transport, fetch_request(checkout), trace), checkout
        )
        after_amount, after_source = checkout_amount(checkout)
        if after_source != TRUSTED_AMOUNT_SOURCE:
            raise ValueError(f"优惠后权威金额缺失: {after_source}")
        discount_result = validate_coupon_amounts(
            normalized_coupon,
            before_amount,
            after_amount,
        )

    if expected_amount:
        require_amount(checkout, expected_amount, "checkout.fetch")

    elements_payload = _send(transport, elements_request(checkout, stripe_js_id), trace)
    current_ids = extract_custom_method_ids(checkout)
    selected = select_gopay_method(elements_payload, current_ids)
    method_id = str(selected["type"])

    _send(transport, taxes_request(checkout, billing), trace)
    checkout = normalize_checkout(_send(transport, fetch_request(checkout), trace), checkout)
    if expected_amount:
        require_amount(checkout, expected_amount, "checkout.taxes_fetch")

    refreshed_ids = extract_custom_method_ids(checkout)
    if method_id not in refreshed_ids:
        elements_payload = _send(transport, elements_request(checkout, stripe_js_id), trace)
        selected = select_gopay_method(elements_payload, refreshed_ids)
        method_id = str(selected["type"])

    confirm_payload = _send(transport, confirm_request(checkout, method_id), trace)
    start_payload = _send(transport, start_request(checkout, method_id), trace)
    amount, amount_source = checkout_amount(checkout)
    return {
        **extract_gopay_result(start_payload),
        **discount_result,
        "checkout_session_id": checkout["checkout_session_id"],
        "checkout_provider": checkout.get("checkout_provider"),
        "processor_entity": checkout.get("processor_entity"),
        "payment_method_country": COUNTRY,
        "currency": CURRENCY,
        "custom_payment_method_type_id": method_id,
        "selected_payment_method_type": method_id,
        "oaics_custom_payment_method": selected,
        "supported_payment_methods": [
            *[
                str(item).strip().lower()
                for item in checkout.get("payment_method_types") or []
                if str(item).strip()
            ],
            PAYMENT_METHOD_TYPE,
        ],
        "stripe_amount": amount,
        "stripe_amount_source": amount_source,
        "stripe_elements_config_id": str(elements_payload.get("config_id") or ""),
        "confirm_status": str(confirm_payload.get("status") or ""),
        "stripe_payment_method_created": False,
        "approve_sent": False,
    }


def stripe_amount(payload: Mapping[str, Any]) -> tuple[str, str]:
    """读取普通 Stripe init 的可信金额字段。"""

    total_summary = payload.get("total_summary")
    if isinstance(total_summary, dict) and total_summary.get("due") is not None:
        return str(total_summary["due"]), "total_summary.due"
    invoice = payload.get("invoice")
    if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
        return str(invoice["amount_due"]), "invoice.amount_due"
    line_items = payload.get("line_items")
    if isinstance(line_items, list):
        total = 0
        found = False
        for item in line_items:
            if not isinstance(item, dict) or "amount" not in item:
                continue
            raw = item.get("amount")
            text = str(raw).strip() if raw is not None else ""
            if isinstance(raw, bool) or not re.fullmatch(r"-?\d+", text):
                return "", "line_items.amount_invalid"
            total += int(text)
            found = True
        if found:
            return str(total), "line_items.amount"
    return "", "stripe.init.amount.missing"


def stripe_supported_methods(payload: Mapping[str, Any]) -> list[str]:
    """合并 init 的所有支付方式来源，避免只看单一字段。"""

    found: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip().lower()
        if text and text not in found:
            found.append(text)

    for key in (
        "payment_method_types",
        "ordered_payment_method_types",
        "automatic_payment_method_types",
        "use_payment_methods",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                add(item)
    settings = payload.get("lpm_settings")
    if isinstance(settings, dict):
        for key in settings:
            add(key)
    specs = payload.get("payment_method_specs")
    if isinstance(specs, list):
        for item in specs:
            if isinstance(item, dict):
                add(item.get("type") or item.get("payment_method_type") or item.get("name"))
    return found


def stripe_context(
    checkout: Mapping[str, Any],
    stripe_js_id: str,
    init_payload: Mapping[str, Any] | None = None,
) -> JsonObject:
    """把 init 前后的 Elements 标识集中起来，供后续请求复用。"""

    init_payload = init_payload if isinstance(init_payload, dict) else {}
    return {
        "stripe_js_id": stripe_js_id,
        "elements_session_id": str(
            init_payload.get("elements_session_id")
            or init_payload.get("session_id")
            or f"elements_{stripe_js_id}"
        ),
        "elements_session_config_id": str(
            init_payload.get("elements_session_config_id")
            or init_payload.get("config_id")
            or "CONFIG_ID"
        ),
        "config_id": str(init_payload.get("config_id") or "CONFIG_ID"),
        "locale": str(init_payload.get("locale") or "en"),
        "browser_locale": "en-US",
        "browser_timezone": "Asia/Jakarta",
        "runtime_version": "STRIPE_RUNTIME_VERSION",
        "stripe_pk": str(
            init_payload.get("publishable_key")
            or checkout.get("stripe_publishable_key")
            or ""
        ),
    }


def stripe_init_request(
    checkout: Mapping[str, Any],
    context: Mapping[str, Any],
) -> RequestSpec:
    cs_id = str(checkout.get("checkout_session_id") or checkout.get("cs_id") or "")
    if not re.fullmatch(r"cs_[A-Za-z0-9_]+", cs_id):
        raise ValueError("普通 Stripe 分支需要 cs_ 会话")
    data = {
        "browser_locale": str(context.get("browser_locale") or "en-US"),
        "browser_timezone": str(context.get("browser_timezone") or "Asia/Jakarta"),
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": str(context.get("stripe_js_id") or ""),
        "elements_session_client[locale]": str(context.get("locale") or "en"),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": str(context.get("stripe_pk") or checkout.get("stripe_publishable_key") or ""),
        "_stripe_version": STRIPE_VERSION,
    }
    return RequestSpec(
        stage="stripe.init",
        method="POST",
        url=f"https://api.stripe.com/v1/payment_pages/{cs_id}/init",
        headers={"Referer": checkout_url(checkout)},
        data=data,
    )


def stripe_payment_method_request(
    checkout: Mapping[str, Any],
    context: Mapping[str, Any],
    billing: Mapping[str, str],
) -> RequestSpec:
    cs_id = str(checkout.get("checkout_session_id") or checkout.get("cs_id") or "")
    runtime = str(context.get("runtime_version") or "STRIPE_RUNTIME_VERSION")
    base = "billing_details"
    data: dict[str, str] = {
        f"{base}[name]": str(billing.get("name") or ""),
        f"{base}[email]": str(billing.get("email") or ""),
        f"{base}[phone]": str(billing.get("phone") or ""),
        f"{base}[address][country]": COUNTRY,
        f"{base}[address][line1]": str(billing.get("line1") or ""),
        f"{base}[address][line2]": str(billing.get("line2") or ""),
        f"{base}[address][city]": str(billing.get("city") or ""),
        f"{base}[address][postal_code]": str(billing.get("postal_code") or ""),
        f"{base}[address][state]": str(billing.get("state") or ""),
        "type": PAYMENT_METHOD_TYPE,
        "payment_user_agent": (
            f"stripe.js/{runtime}; stripe-js-v3/{runtime}; "
            "payment-element; deferred-intent"
        ),
        "referrer": CHATGPT_BASE_URL,
        "time_on_page": "45000",
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[client_session_id]": str(context.get("stripe_js_id") or ""),
        "client_attribution_metadata[checkout_config_id]": str(context.get("config_id") or ""),
        "client_attribution_metadata[elements_session_id]": str(context.get("elements_session_id") or ""),
        "client_attribution_metadata[elements_session_config_id]": str(context.get("elements_session_config_id") or ""),
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": str(context.get("stripe_pk") or checkout.get("stripe_publishable_key") or ""),
        "_stripe_version": STRIPE_VERSION,
    }
    return RequestSpec(
        stage="stripe.payment_methods",
        method="POST",
        url="https://api.stripe.com/v1/payment_methods",
        headers={"Referer": CHATGPT_BASE_URL},
        data=data,
    )


def stripe_confirm_request(
    checkout: Mapping[str, Any],
    context: Mapping[str, Any],
    init_payload: Mapping[str, Any],
    payment_method_id: str,
) -> RequestSpec:
    cs_id = str(checkout.get("checkout_session_id") or checkout.get("cs_id") or "")
    amount, _source = stripe_amount(init_payload)
    data: dict[str, str] = {
        "guid": "GUID",
        "muid": "MUID",
        "sid": "SID",
        "init_checksum": str(init_payload.get("init_checksum") or ""),
        "version": str(context.get("runtime_version") or "STRIPE_RUNTIME_VERSION"),
        "expected_amount": amount,
        "expected_payment_method_type": PAYMENT_METHOD_TYPE,
        "return_url": (
            f"{CHATGPT_BASE_URL}/checkout/verify?stripe_session_id={cs_id}"
        ),
        "elements_session_client[session_id]": str(context.get("elements_session_id") or ""),
        "elements_session_client[locale]": str(context.get("locale") or "en"),
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[stripe_js_id]": str(context.get("stripe_js_id") or ""),
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "client_attribution_metadata[client_session_id]": str(context.get("stripe_js_id") or ""),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[checkout_config_id]": str(context.get("config_id") or ""),
        "client_attribution_metadata[elements_session_id]": str(context.get("elements_session_id") or ""),
        "client_attribution_metadata[elements_session_config_id]": str(context.get("elements_session_config_id") or ""),
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "payment_method": payment_method_id,
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "consent[terms_of_service]": "accepted",
        "key": str(context.get("stripe_pk") or checkout.get("stripe_publishable_key") or ""),
        "_stripe_version": STRIPE_VERSION,
    }
    return RequestSpec(
        stage="stripe.confirm",
        method="POST",
        url=f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
        headers={"Referer": checkout_url(checkout)},
        data=data,
    )


def approve_request(checkout: Mapping[str, Any]) -> RequestSpec:
    route = "/backend-api/payments/checkout/approve"
    return RequestSpec(
        stage="checkout.approve",
        method="POST",
        url=f"{CHATGPT_BASE_URL}{route}",
        headers=route_headers(checkout, route),
        json={
            "checkout_session_id": checkout["checkout_session_id"],
            "processor_entity": checkout["processor_entity"],
        },
    )


def payment_page_request(
    checkout: Mapping[str, Any],
    context: Mapping[str, Any],
) -> RequestSpec:
    cs_id = str(checkout.get("checkout_session_id") or checkout.get("cs_id") or "")
    params = (
        ("elements_session_client[client_betas][0]", "custom_checkout_server_updates_1"),
        ("elements_session_client[client_betas][1]", "custom_checkout_manual_approval_1"),
        ("elements_session_client[elements_init_source]", "custom_checkout"),
        ("elements_session_client[referrer_host]", "chatgpt.com"),
        ("elements_session_client[session_id]", str(context.get("elements_session_id") or "")),
        ("elements_session_client[stripe_js_id]", str(context.get("stripe_js_id") or "")),
        ("elements_session_client[locale]", str(context.get("locale") or "en")),
        ("elements_session_client[is_aggregation_expected]", "false"),
        ("elements_options_client[saved_payment_method][enable_save]", "never"),
        ("elements_options_client[saved_payment_method][enable_redisplay]", "never"),
        ("key", str(context.get("stripe_pk") or checkout.get("stripe_publishable_key") or "")),
        ("_stripe_version", STRIPE_VERSION),
    )
    return RequestSpec(
        stage="stripe.payment_page",
        method="GET",
        url=f"https://api.stripe.com/v1/payment_pages/{cs_id}",
        headers={"Referer": checkout_url(checkout)},
        params=params,
    )


def extract_stripe_provider_redirect(payload: Any) -> str:
    """只接受 redirect_to_url 或 provider 外链，不拿 return URL 冒充支付链接。"""
    return next(iter(_provider_url_candidates(payload)), "")


def _submission_state(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("state", "status"):
            value = str(payload.get(key) or "").strip().lower()
            if value in {
                "requires_approval",
                "requires_action",
                "requires_confirmation",
                "pending",
                "processing",
                "failed",
                "succeeded",
                "complete",
            }:
                return value
        for child in payload.values():
            found = _submission_state(child)
            if found:
                return found
    elif isinstance(payload, list):
        for child in payload:
            found = _submission_state(child)
            if found:
                return found
    return ""


def resolve_standard_gopay_redirect(
    transport: Transport,
    checkout: Mapping[str, Any],
    context: Mapping[str, Any],
    confirm_payload: Mapping[str, Any],
    trace: Trace | None = None,
) -> tuple[str, bool, list[str]]:
    """解析 confirm；需要时发 approve，再读取 payment-page。"""

    candidates: list[str] = []

    def inspect(payload: Mapping[str, Any]) -> str:
        for candidate in _provider_url_candidates(payload):
            if candidate not in candidates:
                candidates.append(candidate)
        return extract_stripe_provider_redirect(payload)

    direct = inspect(confirm_payload)
    if direct:
        return direct, False, candidates
    state = _submission_state(confirm_payload)
    if state == "failed":
        raise ValueError("Stripe GoPay submission failed")

    payment_page = _send(transport, payment_page_request(checkout, context), trace)
    direct = inspect(payment_page)
    if direct:
        return direct, False, candidates
    state = _submission_state(payment_page) or state
    if state not in {"requires_approval", "requires_action", "requires_confirmation", "pending", "processing"}:
        raise ValueError(
            "Stripe payment-page 未产生 GoPay provider redirect；"
            f"state={state or 'missing'}, candidates={candidates[:5]}"
        )

    _send(transport, approve_request(checkout), trace)
    payment_page = _send(transport, payment_page_request(checkout, context), trace)
    direct = inspect(payment_page)
    if not direct:
        raise ValueError(
            "approve 后 Stripe payment-page 未产生 GoPay provider redirect；"
            f"candidates={candidates[:5]}"
        )
    return direct, True, candidates


def _extract_standard_gopay_payment_link(
    transport: Transport,
    checkout: Mapping[str, Any],
    *,
    billing: Mapping[str, str],
    plan_name: str,
    coupon_id: str,
    expected_amount: str,
    stripe_js_id: str,
    trace: Trace | None = None,
) -> JsonObject:
    """普通 cs_ 分支：Stripe init → PaymentMethod → confirm → approve/poll。"""

    checkout = dict(checkout)
    context = stripe_context(checkout, stripe_js_id)
    discount_result: JsonObject = {}
    normalized_coupon = str(coupon_id or "none").strip()
    init_payload: JsonObject | None = None

    if normalized_coupon != "none":
        before_init = _send(transport, stripe_init_request(checkout, context), trace)
        before_amount, before_source = stripe_amount(before_init)
        if not before_amount or before_source.startswith("stripe.init.amount.missing"):
            raise ValueError(f"优惠前 Stripe 金额缺失: {before_source}")
        update_payload = _send(
            transport, coupon_update_request(checkout, plan_name, normalized_coupon), trace
        )
        if update_payload.get("success") is not True:
            raise ValueError("checkout.update 未明确返回 success=true")
        replacement = _checkout_container(update_payload)
        if replacement:
            checkout = normalize_checkout(replacement, checkout)
        checkout = normalize_checkout(
            _send(transport, fetch_request(checkout), trace), checkout
        )
        context = stripe_context(checkout, stripe_js_id)
        init_payload = _send(transport, stripe_init_request(checkout, context), trace)
        after_amount, after_source = stripe_amount(init_payload)
        if not after_amount or after_source.startswith("stripe.init.amount.missing"):
            raise ValueError(f"优惠后 Stripe 金额缺失: {after_source}")
        discount_result = validate_coupon_amounts(
            normalized_coupon,
            before_amount,
            after_amount,
        )

    if init_payload is None:
        init_payload = _send(transport, stripe_init_request(checkout, context), trace)
    hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not hosted_url:
        raise ValueError("Stripe init 缺少 stripe_hosted_url")
    supported = stripe_supported_methods(init_payload)
    if PAYMENT_METHOD_TYPE not in supported:
        raise ValueError(
            f"Stripe init 未提供 {PAYMENT_METHOD_TYPE}；available={supported or ['unknown']}"
        )
    amount, amount_source = stripe_amount(init_payload)
    if expected_amount and amount != str(expected_amount):
        raise ValueError(
            f"Stripe 金额校验失败: expected={expected_amount}, actual={amount}, source={amount_source}"
        )
    context = stripe_context(checkout, stripe_js_id, init_payload)
    pm_payload = _send(
        transport, stripe_payment_method_request(checkout, context, billing), trace
    )
    payment_method_id = str(pm_payload.get("id") or "").strip()
    if not payment_method_id.startswith("pm_"):
        raise ValueError("Stripe PaymentMethod 响应缺少 pm_ id")
    confirm_payload = _send(
        transport,
        stripe_confirm_request(checkout, context, init_payload, payment_method_id),
        trace,
    )
    provider_url, approve_sent, candidate_urls = resolve_standard_gopay_redirect(
        transport,
        checkout,
        context,
        confirm_payload,
        trace,
    )
    return {
        **discount_result,
        "payment_method_type": PAYMENT_METHOD_TYPE,
        "payment_link_type": "gopay_redirect",
        "provider_redirect_url": provider_url,
        "long_url": provider_url,
        "gopay_redirect_url": provider_url,
        "checkout_session_id": checkout["checkout_session_id"],
        "checkout_provider": checkout.get("checkout_provider"),
        "processor_entity": checkout.get("processor_entity"),
        "payment_method_country": COUNTRY,
        "currency": CURRENCY,
        "payment_method_id": payment_method_id,
        "stripe_hosted_url": hosted_url,
        "stripe_amount": amount,
        "stripe_amount_source": amount_source,
        "supported_payment_methods": supported,
        "stripe_payment_method_created": True,
        "approve_sent": approve_sent,
        "candidate_urls": candidate_urls,
    }


def _route_checkout_extraction(
    transport: Transport,
    checkout: Mapping[str, Any],
    *,
    plan_name: str,
    billing: Mapping[str, str],
    coupon_id: str,
    expected_amount: str,
    stripe_js_id: str,
    trace: Trace | None,
) -> JsonObject:
    """按会话前缀路由到 OAICS / 普通 Stripe 尾部（供两个入口复用）。"""

    session_id = str(checkout.get("checkout_session_id") or "")
    if OAICS_ID_RE.fullmatch(session_id):
        return _extract_oaics_gopay_payment_link(
            transport,
            plan_name=plan_name,
            billing=billing,
            coupon_id=coupon_id,
            expected_amount=expected_amount,
            stripe_js_id=stripe_js_id,
            initial_checkout=checkout,
            trace=trace,
        )
    if re.fullmatch(r"cs_[A-Za-z0-9_]+", session_id):
        return _extract_standard_gopay_payment_link(
            transport,
            checkout,
            billing=billing,
            plan_name=plan_name,
            coupon_id=coupon_id,
            expected_amount=expected_amount,
            stripe_js_id=stripe_js_id,
            trace=trace,
        )
    raise ValueError(f"未知 Checkout 会话类型: {session_id or 'missing'}")


def extract_gopay_payment_link(
    transport: Transport,
    *,
    plan_name: str,
    billing: Mapping[str, str],
    coupon_id: str = "none",
    expected_amount: str = "",
    stripe_js_id: str = "STRIPE_JS_ID",
    trace: Trace | None = None,
) -> JsonObject:
    """双兼容入口：按 create 返回的会话前缀选择 OAICS 或普通 Stripe。"""

    checkout = normalize_checkout(
        _send(transport, create_request(plan_name), trace),
        {"plan_name": plan_name, "billing_country": COUNTRY, "currency": CURRENCY},
    )
    return _route_checkout_extraction(
        transport,
        checkout,
        plan_name=plan_name,
        billing=billing,
        coupon_id=coupon_id,
        expected_amount=expected_amount,
        stripe_js_id=stripe_js_id,
        trace=trace,
    )


def extract_gopay_payment_link_from_checkout(
    transport: Transport,
    *,
    checkout_session_id: str,
    processor_entity: str = "openai_llc",
    plan_name: str,
    billing: Mapping[str, str],
    coupon_id: str = "none",
    expected_amount: str = "",
    stripe_js_id: str = "STRIPE_JS_ID",
    trace: Trace | None = None,
) -> JsonObject:
    """从既有 cashier URL 反解出的 checkout 会话直接提链（不重新 create）。

    先用最小 fallback（checkout_session_id + processor_entity）经 normalize_checkout
    构造 checkout（校验会话格式），再 _send(fetch_request) 拉权威 checkout，
    随后按会话前缀路由到 oaics_/cs_ 尾部（复用，不复制逻辑）。
    """

    checkout = normalize_checkout(
        {
            "checkout_session_id": str(checkout_session_id or ""),
            "processor_entity": str(processor_entity or ""),
        }
    )
    checkout = normalize_checkout(_send(transport, fetch_request(checkout), trace), checkout)
    return _route_checkout_extraction(
        transport,
        checkout,
        plan_name=plan_name,
        billing=billing,
        coupon_id=coupon_id,
        expected_amount=expected_amount,
        stripe_js_id=stripe_js_id,
        trace=trace,
    )
