from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import uuid
import html
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from .adspower_client import AdsPowerApiError
from .roxybrowser_client import RoxyBrowserApiError
from .adspower_worker import (
    AdsPowerWorkerError,
    build_session_log_summary,
    open_url_in_profile,
    read_chatgpt_session_via_browser,
    click_hosted_checkout_verification_resend_in_profile,
    continue_hosted_checkout_in_profile,
    run_url_in_profile_until_terminal,
    run_hosted_checkout_in_profile,
)

try:
    from curl_cffi.requests import Session as CurlCffiSession  # type: ignore
except ImportError:  # pragma: no cover
    CurlCffiSession = None  # type: ignore


DEFAULT_STRIPE_PK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRac"
    "ViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
STRIPE_VERSION_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
DEFAULT_TIMEOUT = 30
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_PROXY = os.getenv("OPENAI_PAY_DEFAULT_PROXY", "").strip()
PROVIDER_STAGE_PROXY = os.getenv("OPENAI_PAY_PROVIDER_PROXY", "").strip()
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"
PAYPAL_BILLING_COUNTRY = "DE"
PAYPAL_BILLING_CURRENCY = "EUR"
JOB_LOG_LIMIT = 200

JAPAN_BILLING_NAMES = [
    ("Taro", "Yamada"),
    ("Hanako", "Sato"),
    ("Ken", "Suzuki"),
    ("Yui", "Takahashi"),
    ("Haruto", "Tanaka"),
]
JAPAN_BILLING_STREETS = [
    ("1-2-3 Shibuya", "Shibuya-ku", "Tokyo", "150-0002"),
    ("2-1-1 Namba", "Chuo-ku", "Osaka", "542-0076"),
    ("3-4-5 Sakae", "Naka-ku", "Aichi", "460-0008"),
    ("4-2-8 Hakata", "Hakata-ku", "Fukuoka", "812-0011"),
]
LOCALE_MAP = {
    "de": ("de-DE", "de"),
    "en": ("en-US", "en"),
    "en-US": ("en-US", "en"),
    "es": ("es-ES", "es"),
    "fr": ("fr-FR", "fr"),
    "id": ("id-ID", "id"),
    "it": ("it-IT", "it"),
    "ja": ("ja-JP", "ja"),
    "ko": ("ko-KR", "ko"),
    "pt-BR": ("pt-BR", "pt-BR"),
    "zh-CN": ("zh-CN", "zh-CN"),
    "zh-TW": ("zh-TW", "zh-TW"),
}


class PayPalLinkRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    access_token: str = Field(..., alias="accessToken")
    proxy: str = ""
    default_proxy: str = Field(default="", alias="defaultProxy")
    provider_proxy: str = Field(default="", alias="providerProxy")
    billing_country: str = Field(default=PAYPAL_BILLING_COUNTRY, alias="billingCountry")
    billing_currency: str = Field(default=PAYPAL_BILLING_CURRENCY, alias="billingCurrency")
    billing_name: str = Field(default="", alias="billingName")
    billing_email: str = Field(default="", alias="billingEmail")
    promo_campaign_id: str = Field(default="plus-1-month-free", alias="promoCampaignId")
    stripe_publishable_key: str = Field(default="", alias="stripePublishableKey")
    payment_locale: str = Field(default="en", alias="paymentLocale")
    device_id: str = Field(default="", alias="deviceId")
    user_agent: str = Field(default="", alias="userAgent")
    max_attempts: int = Field(default=10, alias="maxAttempts", ge=1, le=20)


class AdsPowerPayPalLinkJobRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    browser_backend: str = Field(default="adspower", alias="browserBackend")
    ads_power_api_base: str = Field(default="http://127.0.0.1:50325", alias="adsPowerApiBase")
    ads_power_api_key: str = Field(default="", alias="adsPowerApiKey")
    roxybrowser_api_base: str = Field(default="http://127.0.0.1:50000", alias="roxyBrowserApiBase")
    roxybrowser_api_key: str = Field(default="", alias="roxyBrowserApiKey")
    ads_power_profile_id: str = Field(default="", alias="adsPowerProfileId")
    close_profile_on_finish: bool = Field(default=False, alias="closeProfileOnFinish")
    default_proxy: str = Field(default="", alias="defaultProxy")
    provider_proxy: str = Field(default="", alias="providerProxy")
    billing_country: str = Field(default=PAYPAL_BILLING_COUNTRY, alias="billingCountry")
    billing_currency: str = Field(default=PAYPAL_BILLING_CURRENCY, alias="billingCurrency")
    billing_name: str = Field(default="", alias="billingName")
    billing_email: str = Field(default="", alias="billingEmail")
    promo_campaign_id: str = Field(default="plus-1-month-free", alias="promoCampaignId")
    stripe_publishable_key: str = Field(default="", alias="stripePublishableKey")
    payment_locale: str = Field(default="en", alias="paymentLocale")
    device_id: str = Field(default="", alias="deviceId")
    user_agent: str = Field(default="", alias="userAgent")
    max_attempts: int = Field(default=10, alias="maxAttempts", ge=1, le=20)


class AdsPowerOpenUrlRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    browser_backend: str = Field(default="adspower", alias="browserBackend")
    ads_power_api_base: str = Field(default="http://127.0.0.1:50325", alias="adsPowerApiBase")
    ads_power_api_key: str = Field(default="", alias="adsPowerApiKey")
    roxybrowser_api_base: str = Field(default="http://127.0.0.1:50000", alias="roxyBrowserApiBase")
    roxybrowser_api_key: str = Field(default="", alias="roxyBrowserApiKey")
    ads_power_profile_id: str = Field(default="", alias="adsPowerProfileId")
    target_url: str = Field(default="", alias="targetUrl")
    clear_cache_before_start: bool = Field(default=True, alias="clearCacheBeforeStart")
    close_profile_on_finish: bool = Field(default=False, alias="closeProfileOnFinish")


class AdsPowerHostedCheckoutRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    browser_backend: str = Field(default="adspower", alias="browserBackend")
    ads_power_api_base: str = Field(default="http://127.0.0.1:50325", alias="adsPowerApiBase")
    ads_power_api_key: str = Field(default="", alias="adsPowerApiKey")
    roxybrowser_api_base: str = Field(default="http://127.0.0.1:50000", alias="roxyBrowserApiBase")
    roxybrowser_api_key: str = Field(default="", alias="roxyBrowserApiKey")
    ads_power_profile_id: str = Field(default="", alias="adsPowerProfileId")
    target_url: str = Field(default="", alias="targetUrl")
    guest_profile: dict[str, Any] = Field(default_factory=dict, alias="guestProfile")
    verification_url: str = Field(default="", alias="verificationUrl")
    verification_resend_max_attempts: int = Field(default=1, alias="verificationResendMaxAttempts", ge=0, le=10)
    verification_poll_attempts: int = Field(default=6, alias="verificationPollAttempts", ge=1, le=60)
    verification_poll_interval_seconds: int = Field(default=5, alias="verificationPollIntervalSeconds", ge=1, le=60)
    external_verification: bool = Field(default=False, alias="externalVerification")
    timeout_seconds: int = Field(default=240, alias="timeoutSeconds", ge=30, le=1800)
    clear_cache_before_start: bool = Field(default=True, alias="clearCacheBeforeStart")
    close_profile_on_finish: bool = Field(default=False, alias="closeProfileOnFinish")


class AdsPowerHostedCheckoutContinueRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    browser_backend: str = Field(default="adspower", alias="browserBackend")
    ads_power_api_base: str = Field(default="http://127.0.0.1:50325", alias="adsPowerApiBase")
    ads_power_api_key: str = Field(default="", alias="adsPowerApiKey")
    roxybrowser_api_base: str = Field(default="http://127.0.0.1:50000", alias="roxyBrowserApiBase")
    roxybrowser_api_key: str = Field(default="", alias="roxyBrowserApiKey")
    ads_power_profile_id: str = Field(default="", alias="adsPowerProfileId")
    guest_profile: dict[str, Any] = Field(default_factory=dict, alias="guestProfile")
    verification_code: str = Field(default="", alias="verificationCode")
    timeout_seconds: int = Field(default=240, alias="timeoutSeconds", ge=30, le=1800)
    close_profile_on_finish: bool = Field(default=False, alias="closeProfileOnFinish")


class AdsPowerHostedCheckoutResendRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    browser_backend: str = Field(default="adspower", alias="browserBackend")
    ads_power_api_base: str = Field(default="http://127.0.0.1:50325", alias="adsPowerApiBase")
    ads_power_api_key: str = Field(default="", alias="adsPowerApiKey")
    roxybrowser_api_base: str = Field(default="http://127.0.0.1:50000", alias="roxyBrowserApiBase")
    roxybrowser_api_key: str = Field(default="", alias="roxyBrowserApiKey")
    ads_power_profile_id: str = Field(default="", alias="adsPowerProfileId")
    close_profile_on_finish: bool = Field(default=False, alias="closeProfileOnFinish")


class AttemptResult(BaseModel):
    attempt_no: int = Field(alias="attemptNo")
    success: bool
    cs_id: str = Field(default="", alias="csId")
    stripe_hosted_url: str = Field(default="", alias="stripeHostedUrl")
    stripe_redirect_url: str = Field(default="", alias="stripeRedirectUrl")
    provider_redirect_url: str = Field(default="", alias="providerRedirectUrl")
    payment_method_id: str = Field(default="", alias="paymentMethodId")
    long_url: str = Field(default="", alias="longUrl")
    processor_entity: str = Field(default="", alias="processorEntity")
    billing_country: str = Field(default=PAYPAL_BILLING_COUNTRY, alias="billingCountry")
    currency: str = Field(default=PAYPAL_BILLING_CURRENCY)
    expected_amount: int = Field(default=0, alias="expectedAmount")
    expected_currency: str = Field(default="", alias="expectedCurrency")
    error: str = ""


class JobLogEntry(BaseModel):
    timestamp: str
    level: str = "info"
    message: str
    attempt_no: int = Field(default=0, alias="attemptNo")


class PayPalLinkResponse(BaseModel):
    ok: bool
    success: bool
    attempts_used: int = Field(alias="attemptsUsed")
    max_attempts: int = Field(alias="maxAttempts")
    cs_id: str = Field(default="", alias="csId")
    processor_entity: str = Field(default="", alias="processorEntity")
    billing_country: str = Field(default=PAYPAL_BILLING_COUNTRY, alias="billingCountry")
    currency: str = Field(default=PAYPAL_BILLING_CURRENCY)
    payment_locale: str = Field(alias="paymentLocale")
    payment_method_type: str = Field(default="paypal", alias="paymentMethodType")
    payment_method_id: str = Field(default="", alias="paymentMethodId")
    stripe_redirect_url: str = Field(default="", alias="stripeRedirectUrl")
    provider_redirect_url: str = Field(default="", alias="providerRedirectUrl")
    provider_error: str = Field(default="", alias="providerError")
    stripe_hosted_url: str = Field(default="", alias="stripeHostedUrl")
    long_url: str = Field(default="", alias="longUrl")
    attempts: list[AttemptResult] = Field(default_factory=list)


class JobStatusResponse(BaseModel):
    job_id: str = Field(alias="jobId")
    status: str
    current_attempt: int = Field(alias="currentAttempt")
    max_attempts: int = Field(alias="maxAttempts")
    progress_percent: int = Field(alias="progressPercent")
    message: str = ""
    pause_requested: bool = Field(default=False, alias="pauseRequested")
    result: PayPalLinkResponse | None = None
    attempts: list[AttemptResult] = Field(default_factory=list)
    logs: list[JobLogEntry] = Field(default_factory=list)


def normalize_browser_backend(value: str = "") -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "roxybrowser":
        return "roxybrowser"
    if normalized == "adspower":
        return "adspower"
    return "adspower"


def resolve_browser_backend_api_base(req: Any) -> str:
    backend = normalize_browser_backend(getattr(req, "browser_backend", "adspower"))
    if backend == "roxybrowser":
        return str(getattr(req, "roxybrowser_api_base", "") or "").strip() or "http://127.0.0.1:50000"
    return str(getattr(req, "ads_power_api_base", "") or "").strip() or "http://127.0.0.1:50325"


def resolve_browser_backend_api_key(req: Any) -> str:
    backend = normalize_browser_backend(getattr(req, "browser_backend", "adspower"))
    if backend == "roxybrowser":
        api_key = str(getattr(req, "roxybrowser_api_key", "") or "").strip()
        if not api_key:
            raise HTTPException(status_code=400, detail="roxyBrowserApiKey is required for RoxyBrowser backend")
        return api_key
    return str(getattr(req, "ads_power_api_key", "") or "").strip()


JOB_STORE: dict[str, dict[str, Any]] = {}
JOB_STORE_LOCK = threading.Lock()
app = FastAPI(title="PPBoom")


@app.on_event("startup")
def log_startup_proxy_status() -> None:
    for message in startup_proxy_messages():
        print(message, flush=True)


def new_session() -> Any:
    if CurlCffiSession is not None:
        session = CurlCffiSession(impersonate="chrome136")
    else:
        session = requests.Session()
    # Ignore OS/user-level proxy env so "unconfigured" really means direct.
    try:
        session.trust_env = False
    except Exception:
        pass
    try:
        session.proxies = {}
    except Exception:
        pass
    return session


def locale_parts(locale: str) -> tuple[str, str]:
    return LOCALE_MAP.get(str(locale or "").strip(), LOCALE_MAP["en"])


def find_token(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("accessToken", "access_token", "token"):
            token = str(value.get(key) or "").strip()
            if token:
                return token
        for item in value.values():
            token = find_token(item)
            if token:
                return token
    if isinstance(value, list):
        for item in value:
            token = find_token(item)
            if token:
                return token
    return ""


def normalize_access_token(raw: str) -> str:
    token = str(raw or "").strip()
    if not token:
        return ""
    if token.startswith("{") or token.startswith("["):
        try:
            return find_token(json.loads(token)) or token
        except json.JSONDecodeError:
            return token
    return token


def normalize_billing_country(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z]", "", str(value or "").strip()).upper()
    return normalized[:2] or PAYPAL_BILLING_COUNTRY


def normalize_billing_currency(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z]", "", str(value or "").strip()).upper()
    return normalized[:3] or PAYPAL_BILLING_CURRENCY


def normalized_checkout_billing(req: PayPalLinkRequest) -> tuple[str, str]:
    return normalize_billing_country(req.billing_country), normalize_billing_currency(req.billing_currency)


def normalized_promo_campaign_id(value: str) -> str:
    promo = str(value or "").strip()
    if promo == "-":
        return ""
    return promo or "plus-1-month-free"


def set_proxy_url(session: Any, proxy: str) -> None:
    proxy = str(proxy or "").strip()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    else:
        try:
            session.proxies = {}
        except Exception:
            pass


def set_proxy(session: Any, proxy: str) -> None:
    proxy = str(proxy or "").strip()
    set_proxy_url(session, proxy)


def initial_stage_proxy(req: PayPalLinkRequest) -> tuple[str, str]:
    explicit_default = str(req.default_proxy or "").strip()
    if explicit_default:
        return explicit_default, "request.defaultProxy"
    explicit = str(req.proxy or "").strip()
    if explicit:
        return explicit, "request.proxy"
    if DEFAULT_PROXY:
        return DEFAULT_PROXY, "OPENAI_PAY_DEFAULT_PROXY"
    return "", "direct"


def provider_stage_proxy(req: PayPalLinkRequest) -> tuple[str, str]:
    explicit_provider = str(req.provider_proxy or "").strip()
    if explicit_provider:
        return explicit_provider, "request.providerProxy"
    explicit = str(req.proxy or "").strip()
    if explicit:
        return explicit, "request.proxy"
    request_default = str(req.default_proxy or "").strip()
    if request_default:
        return request_default, "request.defaultProxy"
    if PROVIDER_STAGE_PROXY:
        return PROVIDER_STAGE_PROXY, "OPENAI_PAY_PROVIDER_PROXY"
    fallback = DEFAULT_PROXY
    if fallback:
        return fallback, "OPENAI_PAY_DEFAULT_PROXY"
    return "", "direct"


def masked_proxy(proxy: str) -> str:
    proxy = str(proxy or "").strip()
    if not proxy:
        return "direct"
    parsed = urlsplit(proxy)
    if not parsed.scheme and not parsed.netloc:
        return "***"
    scheme = parsed.scheme or "proxy"
    host = parsed.hostname or parsed.netloc or "proxy"
    port = f":{parsed.port}" if parsed.port else ""
    auth = "***@" if parsed.username or parsed.password or "@" in (parsed.netloc or "") else ""
    return f"{scheme}://{auth}{host}{port}"


def startup_proxy_messages() -> list[str]:
    messages: list[str] = []
    if DEFAULT_PROXY:
        messages.append(
            f"[PPBoom] OPENAI_PAY_DEFAULT_PROXY configured: {masked_proxy(DEFAULT_PROXY)}"
        )
    else:
        messages.append(
            "[PPBoom] OPENAI_PAY_DEFAULT_PROXY not configured; ChatGPT / Checkout stage will use direct connection."
        )
    if PROVIDER_STAGE_PROXY:
        messages.append(
            f"[PPBoom] OPENAI_PAY_PROVIDER_PROXY configured: {masked_proxy(PROVIDER_STAGE_PROXY)}"
        )
    else:
        messages.append(
            "[PPBoom] OPENAI_PAY_PROVIDER_PROXY not configured; provider stage will use request proxy if provided, "
            "otherwise fall back to OPENAI_PAY_DEFAULT_PROXY, and if that is also empty it will use direct connection."
        )
    return messages


def request_proxy_messages(req: PayPalLinkRequest) -> list[str]:
    initial_proxy, initial_source = initial_stage_proxy(req)
    provider_proxy, provider_source = provider_stage_proxy(req)
    messages: list[str] = []
    if initial_proxy:
        messages.append(
            f"初始阶段代理：{masked_proxy(initial_proxy)}（来源：{initial_source}）。"
        )
    else:
        messages.append("初始阶段代理未配置：将直连 ChatGPT / Checkout。")
    if provider_proxy:
        messages.append(
            f"Provider 阶段代理：{masked_proxy(provider_proxy)}（来源：{provider_source}）。"
        )
    else:
        messages.append("Provider 阶段代理未配置：将直连 Stripe / PayPal Provider。")
    return messages


def error_message(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, str):
            return detail
        return json.dumps(detail, ensure_ascii=False)
    return str(exc) or exc.__class__.__name__


def append_job_log(job_id: str, message: str, level: str = "info", attempt_no: int = 0) -> None:
    entry = JobLogEntry(
        timestamp=time.strftime("%H:%M:%S"),
        level=level,
        message=str(message or "").strip(),
        attemptNo=max(0, int(attempt_no or 0)),
    )
    with JOB_STORE_LOCK:
        job = JOB_STORE.get(job_id)
        if not job:
            return
        logs = list(job.get("logs") or [])
        logs.append(entry)
        if len(logs) > JOB_LOG_LIMIT:
            logs = logs[-JOB_LOG_LIMIT:]
        job["logs"] = logs


def snapshot_job(job_id: str) -> JobStatusResponse:
    with JOB_STORE_LOCK:
        job = JOB_STORE.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        result = job.get("result")
        return JobStatusResponse(
            jobId=job_id,
            status=str(job.get("status") or "pending"),
            currentAttempt=int(job.get("currentAttempt") or 0),
            maxAttempts=int(job.get("maxAttempts") or 1),
            progressPercent=int(job.get("progressPercent") or 0),
            message=str(job.get("message") or ""),
            pauseRequested=bool(job.get("pauseRequested")),
            result=result if isinstance(result, PayPalLinkResponse) else None,
            attempts=list(job.get("attempts") or []),
            logs=list(job.get("logs") or []),
        )


def update_job(job_id: str, **updates: Any) -> None:
    with JOB_STORE_LOCK:
        job = JOB_STORE.get(job_id)
        if not job:
            return
        job.update(updates)


def extract_processor_entity(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    direct = data.get("processor_entity") or data.get("processorEntity")
    if direct:
        return str(direct).strip()
    for key in ("checkout_session", "session", "checkout", "data"):
        nested = data.get(key)
        if isinstance(nested, dict):
            found = extract_processor_entity(nested)
            if found:
                return found
    return ""


def deep_search_checkout_url(data: Any, depth: int = 0) -> str:
    if depth > 10:
        return ""
    if isinstance(data, str):
        value = data.strip()
        if re.match(r"^https://(?:pay\.openai\.com|checkout\.stripe\.com)/c/pay/", value, re.I):
            return value
        return ""
    if isinstance(data, dict):
        for item in data.values():
            found = deep_search_checkout_url(item, depth + 1)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = deep_search_checkout_url(item, depth + 1)
            if found:
                return found
    return ""


def extract_publishable_key(data: Any, depth: int = 0) -> str:
    if depth > 10:
        return ""
    if isinstance(data, dict):
        for key in ("publishable_key", "publishableKey", "stripe_publishable_key", "stripePublishableKey"):
            value = str(data.get(key) or "").strip()
            if value.startswith("pk_"):
                return value
        for item in data.values():
            found = extract_publishable_key(item, depth + 1)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = extract_publishable_key(item, depth + 1)
            if found:
                return found
    return ""


def to_openai_pay_url(stripe_hosted_url: str) -> str:
    url = str(stripe_hosted_url or "").strip()
    if not url:
        return ""
    if url.startswith("https://checkout.stripe.com"):
        return "https://pay.openai.com" + url[len("https://checkout.stripe.com") :]
    parsed = urlsplit(url)
    if parsed.netloc.lower() == "checkout.stripe.com":
        return urlunsplit((parsed.scheme or "https", "pay.openai.com", parsed.path, parsed.query, parsed.fragment))
    return url


def resolve_stripe_publishable_key(req: PayPalLinkRequest, checkout: dict[str, Any] | None = None) -> str:
    checkout_key = str((checkout or {}).get("stripe_publishable_key") or "").strip()
    if checkout_key.startswith("pk_"):
        return checkout_key
    request_key = str(req.stripe_publishable_key or "").strip()
    if request_key.startswith("pk_"):
        return request_key
    return DEFAULT_STRIPE_PK


def processor_entity_for_country(country: str, processor_entity: str = "") -> str:
    entity = str(processor_entity or "").strip()
    if entity:
        return entity
    return "openai_llc" if str(country or "").upper() == "US" else "openai_ie"


def chatgpt_success_return_url(cs_id: str, country: str, processor_entity: str = "") -> str:
    entity = processor_entity_for_country(country, processor_entity)
    return f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}&processor_entity={entity}&plan_type=plus"


def stripe_confirm_return_url(cs_id: str, checkout: dict[str, Any], stripe_hosted_url: str) -> str:
    hosted_url = to_openai_pay_url(stripe_hosted_url)
    if not hosted_url:
        return ""
    parsed = urlsplit(hosted_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault(
        "success_return_url",
        chatgpt_success_return_url(
            cs_id,
            checkout["billing_country"],
            checkout.get("processor_entity", ""),
        ),
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def build_chatgpt_session(req: PayPalLinkRequest) -> Any:
    access_token = normalize_access_token(req.access_token)
    if not access_token:
        raise HTTPException(status_code=400, detail="accessToken is required")
    device_id = req.device_id.strip() or str(uuid.uuid4())
    user_agent = req.user_agent.strip() or DEFAULT_USER_AGENT
    session = new_session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Authorization": f"Bearer {access_token}",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "Content-Type": "application/json",
            "oai-device-id": device_id,
            "oai-language": "en-US",
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "Cookie": f"oai-did={device_id}",
        }
    )
    proxy, _proxy_source = initial_stage_proxy(req)
    set_proxy(session, proxy)
    return session


def create_checkout(req: PayPalLinkRequest, chatgpt_session: Any) -> dict[str, Any]:
    billing_country, billing_currency = normalized_checkout_billing(req)
    body = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {
            "country": billing_country,
            "currency": billing_currency,
        },
        "checkout_ui_mode": "hosted",
    }
    promo_campaign_id = normalized_promo_campaign_id(req.promo_campaign_id)
    if promo_campaign_id:
        body["promo_campaign"] = {
            "promo_campaign_id": promo_campaign_id,
            "is_coupon_from_query_param": False,
        }
    headers = {
        "Referer": "https://chatgpt.com/",
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
    }
    response = chatgpt_session.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json=body,
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        if "cannot combine currencies" in detail.lower():
            detail = f"CURRENCY_CONFLICT::{detail}"
        raise HTTPException(
            status_code=response.status_code,
            detail=f"checkout create failed: {detail}",
        )
    data = response.json() or {}
    cs_id = data.get("checkout_session_id") or data.get("session_id") or data.get("id")
    if not cs_id or not str(cs_id).startswith("cs_"):
        raise HTTPException(status_code=502, detail=f"checkout response missing cs_id: {data}")
    stripe_publishable_key = extract_publishable_key(data)
    processor_entity = extract_processor_entity(data)
    stripe_hosted_url = deep_search_checkout_url(data) or str(data.get("url") or "").strip()
    return {
        "cs_id": str(cs_id),
        "processor_entity": processor_entity,
        "stripe_publishable_key": stripe_publishable_key,
        "stripe_hosted_url": stripe_hosted_url,
        "billing_country": billing_country,
        "currency": billing_currency,
    }


def build_stripe_init_body(cs_id: str, req: PayPalLinkRequest, stripe_pk: str) -> dict[str, str]:
    browser_locale, elements_locale = locale_parts(req.payment_locale)
    return {
        "browser_locale": browser_locale,
        "browser_timezone": "Asia/Shanghai",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
        "elements_session_client[locale]": elements_locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }


def stripe_init_with_session(stripe: Any, cs_id: str, req: PayPalLinkRequest, stripe_pk: str) -> dict[str, Any]:
    body = build_stripe_init_body(cs_id, req, stripe_pk)
    response = stripe.post(
        f"https://api.stripe.com/v1/payment_pages/{cs_id}/init",
        data=body,
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"stripe init failed: {response.text[:500]}",
        )
    payload = response.json() or {}
    amount, currency = extract_amount_and_currency(payload)
    payload["_ppboom_expected_amount"] = amount
    payload["_ppboom_expected_currency"] = currency
    return payload


def stripe_init(
    cs_id: str,
    req: PayPalLinkRequest,
    proxy_override: str = "",
    checkout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stripe_pk = resolve_stripe_publishable_key(req, checkout)
    stripe = new_session()
    stripe.headers.update(
        {
            "User-Agent": req.user_agent.strip() or DEFAULT_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    if proxy_override:
        set_proxy_url(stripe, proxy_override)
    else:
        proxy, _proxy_source = initial_stage_proxy(req)
        set_proxy(stripe, proxy)
    return stripe_init_with_session(stripe, cs_id, req, stripe_pk)


def coerce_amount_cents(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(round(value * 100)) if not value.is_integer() else int(value)
    text = str(value or "").strip()
    if not text:
        return 0
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return 0
    try:
        numeric_text = match.group(0)
        numeric_value = float(numeric_text)
        if "." in numeric_text:
            return int(round(numeric_value * 100))
        return int(numeric_value)
    except Exception:
        return 0


def amount_from_line_items(init_payload: dict[str, Any]) -> int:
    line_items = init_payload.get("line_items")
    if not isinstance(line_items, list):
        return 0
    total = 0
    for item in line_items:
        if isinstance(item, dict):
            total += coerce_amount_cents(item.get("amount"))
    return total


def extract_amount_and_currency(init_payload: Any) -> tuple[int, str]:
    if not isinstance(init_payload, dict):
        return 0, PAYPAL_BILLING_CURRENCY
    candidates: list[Any] = []
    total_summary = init_payload.get("total_summary")
    if isinstance(total_summary, dict):
        candidates.extend(
            [
                total_summary.get("due"),
                total_summary.get("amount_due"),
                total_summary.get("total"),
            ]
        )
    invoice = init_payload.get("invoice")
    if isinstance(invoice, dict):
        candidates.extend([invoice.get("amount_due"), invoice.get("total")])
    payment_page = init_payload.get("payment_page")
    if isinstance(payment_page, dict):
        candidates.extend([payment_page.get("amount_due"), payment_page.get("amount")])
    candidates.append(amount_from_line_items(init_payload))
    amount = 0
    for candidate in candidates:
        amount = coerce_amount_cents(candidate)
        if amount:
            break
    currency_candidates = [init_payload.get("currency")]
    if isinstance(payment_page, dict):
        currency_candidates.append(payment_page.get("currency"))
    if isinstance(invoice, dict):
        currency_candidates.append(invoice.get("currency"))
    currency = ""
    for candidate in currency_candidates:
        currency = str(candidate or "").strip().upper()
        if currency:
            break
    if not currency:
        currency = PAYPAL_BILLING_CURRENCY
    return amount, currency or PAYPAL_BILLING_CURRENCY


def expected_amount(init_payload: Any) -> str:
    amount, _currency = extract_amount_and_currency(init_payload)
    return str(amount)


def stripe_context(init_payload: dict[str, Any], req: PayPalLinkRequest) -> dict[str, Any]:
    _, elements_locale = locale_parts(req.payment_locale)
    amount, currency = extract_amount_and_currency(init_payload)
    return {
        "stripe_js_id": str(uuid.uuid4()),
        "elements_session_id": f"elements_session_{uuid.uuid4().hex[:11]}",
        "elements_session_config_id": str(init_payload.get("config_id") or uuid.uuid4()),
        "config_id": init_payload.get("config_id") or "",
        "init_checksum": init_payload.get("init_checksum") or "",
        "checkout_amount": str(amount),
        "checkout_currency": currency,
        "locale": elements_locale,
    }


def build_paypal_billing(req: PayPalLinkRequest | None = None) -> dict[str, str]:
    first_name, last_name = random.choice(JAPAN_BILLING_NAMES)
    line1, city, state, postal_code = random.choice(JAPAN_BILLING_STREETS)
    suffix = random.randint(1000, 9999)
    req_name = str(getattr(req, "billing_name", "") or "").strip() if req else ""
    req_email = str(getattr(req, "billing_email", "") or "").strip() if req else ""
    return {
        "name": req_name or f"{first_name} {last_name}",
        "email": req_email or f"{first_name.lower()}.{last_name.lower()}{suffix}@example.com",
        "country": "JP",
        "line1": line1,
        "city": city,
        "state": state,
        "postal_code": postal_code,
    }


def build_stripe_session(req: PayPalLinkRequest, proxy_override: str = "") -> Any:
    stripe = new_session()
    stripe.headers.update(
        {
            "User-Agent": req.user_agent.strip() or DEFAULT_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    if proxy_override:
        set_proxy_url(stripe, proxy_override)
    else:
        proxy, _proxy_source = initial_stage_proxy(req)
        set_proxy(stripe, proxy)
    return stripe


def stripe_create_paypal_payment_method(
    stripe: Any,
    cs_id: str,
    stripe_pk: str,
    billing: dict[str, str],
    ctx: dict[str, Any],
) -> str:
    runtime_version = DEFAULT_STRIPE_RUNTIME_VERSION
    body = {
        "billing_details[name]": billing.get("name") or "John Doe",
        "billing_details[email]": billing.get("email") or "buyer@example.com",
        "billing_details[address][country]": billing.get("country") or "JP",
        "billing_details[address][line1]": billing.get("line1") or "1-2-3 Shibuya",
        "billing_details[address][city]": billing.get("city") or "Shibuya-ku",
        "billing_details[address][postal_code]": billing.get("postal_code") or "150-0002",
        "billing_details[address][state]": billing.get("state") or "Tokyo",
        "type": "paypal",
        "payment_user_agent": f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    response = stripe.post("https://api.stripe.com/v1/payment_methods", data=body, timeout=DEFAULT_TIMEOUT)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=f"stripe payment_methods failed: {response.text[:500]}")
    pm_id = str((response.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise HTTPException(status_code=502, detail=f"stripe payment_methods bad response: {response.text[:300]}")
    return pm_id


def stripe_confirm_paypal(
    stripe: Any,
    cs_id: str,
    pm_id: str,
    stripe_pk: str,
    init_payload: dict[str, Any],
    ctx: dict[str, Any],
    checkout: dict[str, Any],
    stripe_hosted_url: str,
    req: PayPalLinkRequest,
) -> dict[str, Any]:
    body = {
        "guid": uuid.uuid4().hex,
        "muid": uuid.uuid4().hex,
        "sid": uuid.uuid4().hex,
        "payment_method": pm_id,
        "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
        "version": DEFAULT_STRIPE_RUNTIME_VERSION,
        "expected_amount": str(ctx.get("checkout_amount") or expected_amount(init_payload)),
        "expected_payment_method_type": "paypal",
        "return_url": stripe_confirm_return_url(cs_id, checkout, stripe_hosted_url),
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[locale]": str(ctx.get("locale") or "en"),
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "consent[terms_of_service]": "accepted",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    response = stripe.post(f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm", data=body, timeout=DEFAULT_TIMEOUT)
    text = response.text[:500]
    if response.status_code == 400 and "terms of service" in text.lower():
        body["consent[terms_of_service]"] = "accepted"
        response = stripe.post(f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm", data=body, timeout=DEFAULT_TIMEOUT)
        text = response.text[:500]
    if response.status_code == 400 and "checkout_amount_mismatch" in text:
        init_payload = stripe_init_with_session(stripe, cs_id, req, stripe_pk)
        body["expected_amount"] = expected_amount(init_payload)
        body["init_checksum"] = str(init_payload.get("init_checksum") or ctx.get("init_checksum") or "")
        response = stripe.post(f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm", data=body, timeout=DEFAULT_TIMEOUT)
        text = response.text[:500]
    if response.status_code in {400, 429, 500, 502, 503}:
        time.sleep(2)
        retry = stripe.post(f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm", data=body, timeout=DEFAULT_TIMEOUT)
        if retry.status_code < 400:
            return retry.json() or {}
        response = retry
        text = response.text[:500]
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=f"stripe confirm failed: {text}")
    return response.json() or {}


def looks_like_url(value: Any) -> bool:
    text = str(value or "").strip()
    return text.startswith("http://") or text.startswith("https://")


def extract_redirect_to_url(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    next_action = payload.get("next_action")
    if isinstance(next_action, dict) and next_action.get("type") == "redirect_to_url":
        redirect_to_url = next_action.get("redirect_to_url") or {}
        if isinstance(redirect_to_url, dict):
            url = str(redirect_to_url.get("url") or "").strip()
            if url:
                return url
    for key in ("setup_intent", "payment_intent"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = extract_redirect_to_url(nested)
            if found:
                return found
    return ""


def stripe_payment_page_redirect_url(
    stripe: Any,
    cs_id: str,
    stripe_pk: str,
    req: PayPalLinkRequest,
    timeout_seconds: float = 30,
) -> str:
    deadline = time.time() + max(1.0, float(timeout_seconds or 30))
    last_error = ""
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": f"elements_session_{uuid.uuid4().hex[:11]}",
        "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
        "elements_session_client[locale]": locale_parts(req.payment_locale)[1],
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    while time.time() < deadline:
        response = stripe.get(f"https://api.stripe.com/v1/payment_pages/{cs_id}", params=params, timeout=DEFAULT_TIMEOUT)
        if response.status_code == 200:
            payload = response.json() or {}
            redirect_url = extract_redirect_to_url(payload)
            if redirect_url:
                return redirect_url
            last_error = f"keys=[{','.join(sorted(payload.keys())[:8])}]"
        else:
            last_error = f"http {response.status_code}: {response.text[:120]}"
        time.sleep(1)
    raise HTTPException(status_code=504, detail=f"redirect url resolution timeout: {last_error}")


def chatgpt_approve(chatgpt: Any, cs_id: str, checkout: dict[str, Any]) -> None:
    country = checkout["billing_country"]
    processor_entity = processor_entity_for_country(country, checkout.get("processor_entity", ""))
    try:
        chatgpt.post(
            "https://chatgpt.com/backend-api/sentinel/ping",
            json={},
            headers={
                "Referer": "https://chatgpt.com/",
                "x-openai-target-path": "/backend-api/sentinel/ping",
                "x-openai-target-route": "/backend-api/sentinel/ping",
            },
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception:
        pass
    last_result = ""
    for attempt in range(1, 4):
        if attempt > 1:
            time.sleep(3)
        response = chatgpt.post(
            "https://chatgpt.com/backend-api/payments/checkout/approve",
            json={"checkout_session_id": cs_id, "processor_entity": processor_entity},
            headers={
                "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}",
                "x-openai-target-path": "/backend-api/payments/checkout/approve",
                "x-openai-target-route": "/backend-api/payments/checkout/approve",
            },
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=f"chatgpt approve failed: {response.text[:500]}")
        try:
            last_result = (response.json() or {}).get("result") or ""
        except Exception:
            last_result = ""
        if last_result == "approved":
            return
        if last_result != "blocked":
            break
    raise HTTPException(status_code=502, detail=f"chatgpt approve unexpected result: {last_result!r}")


def redirect_url_after_confirm(
    chatgpt: Any,
    stripe: Any,
    confirm_payload: dict[str, Any],
    cs_id: str,
    stripe_pk: str,
    checkout: dict[str, Any],
    req: PayPalLinkRequest,
) -> str:
    redirect_url = extract_redirect_to_url(confirm_payload)
    if redirect_url:
        return redirect_url
    submission = confirm_payload.get("submission_attempt") if isinstance(confirm_payload, dict) else None
    if isinstance(submission, dict) and submission.get("state") == "requires_approval":
        chatgpt_approve(chatgpt, cs_id, checkout)
        return stripe_payment_page_redirect_url(stripe, cs_id, stripe_pk, req, timeout_seconds=45)
    try:
        return stripe_payment_page_redirect_url(stripe, cs_id, stripe_pk, req, timeout_seconds=30)
    except HTTPException:
        chatgpt_approve(chatgpt, cs_id, checkout)
        return stripe_payment_page_redirect_url(stripe, cs_id, stripe_pk, req, timeout_seconds=45)


def resolve_external_redirect(stripe: Any, redirect_url: str, preferred_hosts: tuple[str, ...] = ("paypal.com",), max_hops: int = 5) -> str:
    current = str(redirect_url or "").strip()
    preferred = tuple(host.lower().lstrip(".") for host in preferred_hosts if host)
    for _ in range(max(1, int(max_hops or 1))):
        if not current:
            return ""
        host = (urlsplit(current).netloc or "").lower()
        if preferred and any(host == item or host.endswith(f".{item}") for item in preferred):
            return current
        try:
            response = stripe.get(current, allow_redirects=False, timeout=DEFAULT_TIMEOUT)
        except Exception:
            return current
        if response.status_code not in (301, 302, 303, 307, 308):
            return current
        location = str(response.headers.get("Location") or "").strip()
        if not location:
            return current
        current = urljoin(current, location)
    return current


def is_paypal_provider_url(url: str) -> bool:
    host = (urlsplit(str(url or "").strip()).netloc or "").lower()
    return host == "paypal.com" or host.endswith(".paypal.com")


def is_pm_redirect_url(url: str) -> bool:
    host = (urlsplit(str(url or "").strip()).netloc or "").lower()
    return host == "pm-redirects.stripe.com" or host.endswith(".pm-redirects.stripe.com")


def create_paypal_provider_link(
    chatgpt: Any,
    checkout: dict[str, Any],
    init_payload: dict[str, Any],
    stripe_hosted_url: str,
    req: PayPalLinkRequest,
    provider_proxy: str,
) -> dict[str, str]:
    stripe_pk = resolve_stripe_publishable_key(req, checkout)
    stripe = build_stripe_session(req, proxy_override=provider_proxy)
    ctx = stripe_context(init_payload, req)
    billing = build_paypal_billing(req)
    pm_id = stripe_create_paypal_payment_method(stripe, checkout["cs_id"], stripe_pk, billing, ctx)
    confirm_payload = stripe_confirm_paypal(
        stripe,
        checkout["cs_id"],
        pm_id,
        stripe_pk,
        init_payload,
        ctx,
        checkout,
        stripe_hosted_url,
        req,
    )
    stripe_redirect_url = redirect_url_after_confirm(
        chatgpt,
        stripe,
        confirm_payload,
        checkout["cs_id"],
        stripe_pk,
        checkout,
        req,
    )
    provider_url = resolve_external_redirect(stripe, stripe_redirect_url)
    return {
        "payment_method_id": pm_id,
        "stripe_redirect_url": stripe_redirect_url,
        "provider_redirect_url": provider_url,
        "long_url": provider_url or stripe_redirect_url,
    }


def run_single_attempt(req: PayPalLinkRequest, attempt_no: int) -> AttemptResult:
    checkout: dict[str, Any] = {}
    stripe_hosted_url = ""
    stripe_redirect_url = ""
    provider_redirect_url = ""
    payment_method_id = ""
    long_url = ""
    try:
        for message in request_proxy_messages(req):
            print(f"[PPBoom] attempt {attempt_no}: {message}", flush=True)
        chatgpt = build_chatgpt_session(req)
        checkout = create_checkout(req, chatgpt)
        post_checkout_proxy, _provider_source = provider_stage_proxy(req)
        set_proxy_url(chatgpt, post_checkout_proxy)
        init_payload = stripe_init(checkout["cs_id"], req, proxy_override=post_checkout_proxy, checkout=checkout)
        expected_total, expected_currency = extract_amount_and_currency(init_payload)
        if expected_total > 0:
            raise HTTPException(
                status_code=402,
                detail=f"PLUS_CHECKOUT_NON_FREE_TRIAL::{expected_total / 100:.2f} {expected_currency}",
            )
        stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or checkout.get("stripe_hosted_url") or "").strip()
        if not stripe_hosted_url:
            stripe_hosted_url = f"https://checkout.stripe.com/c/pay/{checkout['cs_id']}"
        provider = create_paypal_provider_link(
            chatgpt,
            checkout,
            init_payload,
            stripe_hosted_url,
            req,
            provider_proxy=post_checkout_proxy,
        )
        stripe_redirect_url = provider.get("stripe_redirect_url", "")
        provider_redirect_url = provider.get("provider_redirect_url", "")
        payment_method_id = provider.get("payment_method_id", "")
        long_url = provider.get("long_url", "")
        if not stripe_redirect_url and provider_redirect_url:
            stripe_redirect_url = provider_redirect_url
        if not (is_pm_redirect_url(stripe_redirect_url) or is_paypal_provider_url(stripe_redirect_url)):
            raise HTTPException(
                status_code=502,
                detail=(
                    "stripe redirect did not resolve to pm-redirects.stripe.com or PayPal: "
                    f"{stripe_redirect_url or provider_redirect_url or long_url or 'empty'}"
                ),
            )
        return AttemptResult(
            attemptNo=attempt_no,
            success=True,
            csId=checkout.get("cs_id", ""),
            stripeHostedUrl=stripe_hosted_url,
            stripeRedirectUrl=stripe_redirect_url,
            providerRedirectUrl=provider_redirect_url,
            paymentMethodId=payment_method_id,
            longUrl=long_url,
            processorEntity=processor_entity_for_country(checkout.get("billing_country", ""), checkout.get("processor_entity", "")),
            billingCountry=str(checkout.get("billing_country") or PAYPAL_BILLING_COUNTRY),
            currency=str(checkout.get("currency") or PAYPAL_BILLING_CURRENCY),
            expectedAmount=expected_total,
            expectedCurrency=expected_currency,
            error="",
        )
    except Exception as exc:
        billing_country = str(checkout.get("billing_country") or PAYPAL_BILLING_COUNTRY) if checkout else PAYPAL_BILLING_COUNTRY
        currency = str(checkout.get("currency") or PAYPAL_BILLING_CURRENCY) if checkout else PAYPAL_BILLING_CURRENCY
        return AttemptResult(
            attemptNo=attempt_no,
            success=False,
            csId=str(checkout.get("cs_id") or ""),
            stripeHostedUrl=stripe_hosted_url,
            stripeRedirectUrl=stripe_redirect_url,
            providerRedirectUrl=provider_redirect_url,
            paymentMethodId=payment_method_id,
            longUrl=long_url,
            processorEntity=processor_entity_for_country(billing_country, checkout.get("processor_entity", "") if checkout else ""),
            billingCountry=billing_country,
            currency=currency,
            error=error_message(exc),
        )


def build_attempt_progress_message(result: AttemptResult, max_attempts: int) -> tuple[str, str]:
    attempt_no = max(1, int(result.attempt_no or 1))
    if result.success:
        summary = f"第 {attempt_no} 次成功，停止继续运行。"
        if str(result.cs_id or "").strip():
            summary = f"{summary} csId={str(result.cs_id).strip()}"
        return summary, "info"

    reason = str(result.error or "").strip() or "未知错误"
    cs_id = str(result.cs_id or "").strip()
    message = f"第 {attempt_no} 次失败，准备继续。原因：{reason}"
    if cs_id:
        message = f"{message} | csId={cs_id}"
    if attempt_no >= max(1, int(max_attempts or 1)):
        message = f"第 {attempt_no} 次失败，已达到最大尝试次数。原因：{reason}" + (f" | csId={cs_id}" if cs_id else "")
    return message, "warn"


def execute_paypal_link_request(
    req: PayPalLinkRequest,
    progress_callback: Any | None = None,
    pause_guard: Any | None = None,
) -> PayPalLinkResponse:
    attempts: list[AttemptResult] = []
    success_result: AttemptResult | None = None
    for attempt_no in range(1, req.max_attempts + 1):
        if callable(pause_guard):
            pause_guard("before_attempt", attempt_no, attempts)
        if callable(progress_callback):
            progress_callback(
                "attempt_started",
                {
                    "attemptNo": attempt_no,
                    "maxAttempts": req.max_attempts,
                    "progressPercent": int(((attempt_no - 1) / max(1, req.max_attempts)) * 100),
                    "message": f"正在执行第 {attempt_no} / {req.max_attempts} 次...",
                },
            )
        result = run_single_attempt(req, attempt_no)
        attempts.append(result)
        if callable(progress_callback):
            progress_message, progress_level = build_attempt_progress_message(result, req.max_attempts)
            progress_callback(
                "attempt_finished",
                {
                    "attemptNo": attempt_no,
                    "maxAttempts": req.max_attempts,
                    "attempts": attempts,
                    "progressPercent": int((attempt_no / max(1, req.max_attempts)) * 100),
                    "message": progress_message,
                    "level": progress_level,
                },
            )
        if result.success:
            success_result = result
            break
        if callable(pause_guard):
            pause_guard("after_attempt", attempt_no, attempts)
    final_result = success_result or (attempts[-1] if attempts else AttemptResult(attemptNo=1, success=False, error="no attempts executed"))
    return PayPalLinkResponse(
        ok=bool(success_result),
        success=bool(success_result),
        attemptsUsed=len(attempts),
        maxAttempts=req.max_attempts,
        csId=final_result.cs_id,
        processorEntity=final_result.processor_entity or processor_entity_for_country(final_result.billing_country),
        billingCountry=final_result.billing_country or PAYPAL_BILLING_COUNTRY,
        currency=final_result.currency or PAYPAL_BILLING_CURRENCY,
        paymentLocale=locale_parts(req.payment_locale)[0],
        paymentMethodType="paypal",
        paymentMethodId=final_result.payment_method_id,
        stripeRedirectUrl=final_result.stripe_redirect_url,
        providerRedirectUrl=final_result.provider_redirect_url,
        providerError="" if success_result else final_result.error,
        stripeHostedUrl=final_result.stripe_hosted_url,
        longUrl=final_result.long_url,
        attempts=attempts,
    )


def build_paypal_link_request_from_adspower(req: AdsPowerPayPalLinkJobRequest, access_token: str) -> PayPalLinkRequest:
    return PayPalLinkRequest(
        accessToken=access_token,
        defaultProxy=str(req.default_proxy or "").strip(),
        providerProxy=str(req.provider_proxy or "").strip(),
        billingCountry=normalize_billing_country(req.billing_country),
        billingCurrency=normalize_billing_currency(req.billing_currency),
        billingName=str(req.billing_name or "").strip(),
        billingEmail=str(req.billing_email or "").strip(),
        promoCampaignId=normalized_promo_campaign_id(req.promo_campaign_id),
        stripePublishableKey=str(req.stripe_publishable_key or "").strip(),
        paymentLocale=str(req.payment_locale or "en").strip() or "en",
        deviceId=str(req.device_id or "").strip(),
        userAgent=str(req.user_agent or "").strip(),
        maxAttempts=max(1, min(20, int(req.max_attempts or 10))),
    )


def execute_adspower_paypal_link_request(
    req: AdsPowerPayPalLinkJobRequest,
    job_id: str = "",
    progress_callback: Any | None = None,
    pause_guard: Any | None = None,
) -> PayPalLinkResponse:
    browser_backend = normalize_browser_backend(req.browser_backend)
    backend_label = "RoxyBrowser" if browser_backend == "roxybrowser" else "AdsPower"
    profile_id = str(req.ads_power_profile_id or "").strip()
    if not profile_id:
        raise HTTPException(status_code=400, detail="browser profile id / dirId is required")
    if job_id:
        append_job_log(job_id, f"{backend_label}：准备启动 profile {profile_id}。")
    api_key = resolve_browser_backend_api_key(req)
    session_info = read_chatgpt_session_via_browser(
        browser_backend=browser_backend,
        api_base=resolve_browser_backend_api_base(req),
        ads_power_api_key=req.ads_power_api_key,
        roxybrowser_api_key=api_key if browser_backend == "roxybrowser" else req.roxybrowser_api_key,
        profile_id=profile_id,
        close_profile_on_finish=bool(req.close_profile_on_finish),
    )
    if job_id:
        append_job_log(job_id, f"{backend_label}：已捕获 ChatGPT 会话（{build_session_log_summary(session_info)}）。")
    access_token = str(session_info.get("access_token") or "").strip()
    if not access_token:
        raise HTTPException(status_code=401, detail=f"Browser profile {profile_id} did not provide a valid ChatGPT accessToken")
    inner_req = build_paypal_link_request_from_adspower(req, access_token)
    return execute_paypal_link_request(inner_req, progress_callback=progress_callback, pause_guard=pause_guard)


def run_paypal_link_job(job_id: str, req: PayPalLinkRequest) -> None:
    def wait_if_paused(stage: str, attempt_no: int, attempts: list[AttemptResult]) -> None:
        entered_pause = False
        while True:
            with JOB_STORE_LOCK:
                job = JOB_STORE.get(job_id) or {}
                pause_requested = bool(job.get("pauseRequested"))
            if not pause_requested:
                if entered_pause:
                    resume_message = (
                        f"已继续，准备开始第 {attempt_no} / {req.max_attempts} 次。"
                        if stage == "before_attempt"
                        else "已继续，准备进入下一次尝试。"
                    )
                    update_job(job_id, status="running", message=resume_message)
                    append_job_log(job_id, resume_message, "info", attempt_no)
                return
            if not entered_pause:
                pause_message = (
                    f"已暂停，停在第 {attempt_no} 次开始前。"
                    if stage == "before_attempt"
                    else f"已暂停，第 {attempt_no} 次已结束，等待继续。"
                )
                update_job(
                    job_id,
                    status="paused",
                    currentAttempt=attempt_no if stage == "before_attempt" else len(attempts),
                    maxAttempts=req.max_attempts,
                    attempts=attempts,
                    message=pause_message,
                )
                append_job_log(job_id, pause_message, "warn", attempt_no)
                entered_pause = True
            time.sleep(0.3)

    def on_progress(_event: str, payload: dict[str, Any]) -> None:
        attempt_no = int(payload.get("attemptNo") or 0)
        max_attempts = int(payload.get("maxAttempts") or req.max_attempts)
        attempts = list(payload.get("attempts") or [])
        message = str(payload.get("message") or "")
        progress_percent = int(payload.get("progressPercent") or 0)
        update_job(
            job_id,
            status="running",
            currentAttempt=attempt_no,
            maxAttempts=max_attempts,
            attempts=attempts,
            progressPercent=max(0, min(100, progress_percent)),
            message=message,
        )
        append_job_log(job_id, message, str(payload.get("level") or "info").strip().lower() or "info", attempt_no)

    try:
        for message in request_proxy_messages(req):
            append_job_log(job_id, message, "info")
        result = execute_paypal_link_request(req, progress_callback=on_progress, pause_guard=wait_if_paused)
        update_job(
            job_id,
            status="succeeded" if result.ok else "failed",
            currentAttempt=result.attempts_used,
            maxAttempts=result.max_attempts,
            attempts=result.attempts,
            progressPercent=100,
            pauseRequested=False,
            message=(
                f"已在第 {result.attempts_used} 次成功。"
                if result.ok
                else f"已跑满 {result.attempts_used} 次，仍未成功。"
            ),
            result=result,
        )
        append_job_log(
            job_id,
            (
                f"任务完成，第 {result.attempts_used} 次成功。"
                if result.ok
                else f"任务结束，已运行 {result.attempts_used} 次但仍未成功。"
            ),
            "info" if result.ok else "error",
            result.attempts_used,
        )
    except Exception as exc:
        update_job(
            job_id,
            status="failed",
            progressPercent=100,
            pauseRequested=False,
            message=error_message(exc),
        )
        append_job_log(job_id, error_message(exc), "error")


def run_adspower_paypal_link_job(job_id: str, req: AdsPowerPayPalLinkJobRequest) -> None:
    def wait_if_paused(stage: str, attempt_no: int, attempts: list[AttemptResult]) -> None:
        entered_pause = False
        while True:
            with JOB_STORE_LOCK:
                job = JOB_STORE.get(job_id) or {}
                pause_requested = bool(job.get("pauseRequested"))
            if not pause_requested:
                if entered_pause:
                    resume_message = (
                        f"已继续，准备开始第 {attempt_no} / {req.max_attempts} 次。"
                        if stage == "before_attempt"
                        else "已继续，准备进入下一次尝试。"
                    )
                    update_job(job_id, status="running", message=resume_message)
                    append_job_log(job_id, resume_message, "info", attempt_no)
                return
            if not entered_pause:
                pause_message = (
                    f"已暂停，停在第 {attempt_no} 次开始前。"
                    if stage == "before_attempt"
                    else f"已暂停，第 {attempt_no} 次已结束，等待继续。"
                )
                update_job(
                    job_id,
                    status="paused",
                    currentAttempt=attempt_no if stage == "before_attempt" else len(attempts),
                    maxAttempts=req.max_attempts,
                    attempts=attempts,
                    message=pause_message,
                )
                append_job_log(job_id, pause_message, "warn", attempt_no)
                entered_pause = True
            time.sleep(0.3)

    def on_progress(_event: str, payload: dict[str, Any]) -> None:
        attempt_no = int(payload.get("attemptNo") or 0)
        max_attempts = int(payload.get("maxAttempts") or req.max_attempts)
        attempts = list(payload.get("attempts") or [])
        message = str(payload.get("message") or "")
        progress_percent = int(payload.get("progressPercent") or 0)
        update_job(
            job_id,
            status="running",
            currentAttempt=attempt_no,
            maxAttempts=max_attempts,
            attempts=attempts,
            progressPercent=max(0, min(100, progress_percent)),
            message=message,
        )
        append_job_log(job_id, message, str(payload.get("level") or "info").strip().lower() or "info", attempt_no)

    try:
        backend_label = "RoxyBrowser" if normalize_browser_backend(req.browser_backend) == "roxybrowser" else "AdsPower"
        append_job_log(job_id, f"{backend_label}：将通过 profile {str(req.ads_power_profile_id or '').strip()} 独立创建 Plus Checkout。")
        result = execute_adspower_paypal_link_request(req, job_id=job_id, progress_callback=on_progress, pause_guard=wait_if_paused)
        update_job(
            job_id,
            status="succeeded" if result.ok else "failed",
            currentAttempt=result.attempts_used,
            maxAttempts=result.max_attempts,
            attempts=result.attempts,
            progressPercent=100,
            pauseRequested=False,
            message=(
                f"已在第 {result.attempts_used} 次成功。"
                if result.ok
                else f"已跑满 {result.attempts_used} 次，仍未成功。"
            ),
            result=result,
        )
        append_job_log(
            job_id,
            (
                f"任务完成，第 {result.attempts_used} 次成功。"
                if result.ok
                else f"任务结束，已运行 {result.attempts_used} 次但仍未成功。"
            ),
            "info" if result.ok else "error",
            result.attempts_used,
        )
    except Exception as exc:
        update_job(
            job_id,
            status="failed",
            progressPercent=100,
            pauseRequested=False,
            message=error_message(exc),
        )
        append_job_log(job_id, error_message(exc), "error")


@app.get("/health")
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "PPBoom"}


@app.post("/api/long-link", response_model=PayPalLinkResponse)
@app.post("/api/paypal-link", response_model=PayPalLinkResponse)
def generate_paypal_link(req: PayPalLinkRequest) -> PayPalLinkResponse:
    return execute_paypal_link_request(req)


@app.post("/api/paypal-link/jobs", response_model=JobStatusResponse)
def create_paypal_link_job(req: PayPalLinkRequest) -> JobStatusResponse:
    job_id = uuid.uuid4().hex
    with JOB_STORE_LOCK:
        JOB_STORE[job_id] = {
            "status": "pending",
            "currentAttempt": 0,
            "maxAttempts": req.max_attempts,
            "progressPercent": 0,
            "message": "任务已创建，准备开始。",
            "pauseRequested": False,
            "attempts": [],
            "logs": [],
            "result": None,
        }
    append_job_log(job_id, "任务已创建，准备开始。")
    worker = threading.Thread(target=run_paypal_link_job, args=(job_id, req), daemon=True)
    worker.start()
    return snapshot_job(job_id)


@app.post("/api/paypal-link/jobs/adspower", response_model=JobStatusResponse)
def create_adspower_paypal_link_job(req: AdsPowerPayPalLinkJobRequest) -> JobStatusResponse:
    job_id = uuid.uuid4().hex
    backend_label = "RoxyBrowser" if normalize_browser_backend(req.browser_backend) == "roxybrowser" else "AdsPower"
    with JOB_STORE_LOCK:
        JOB_STORE[job_id] = {
            "status": "pending",
            "currentAttempt": 0,
            "maxAttempts": req.max_attempts,
            "progressPercent": 0,
            "message": f"{backend_label} 任务已创建，准备开始。",
            "pauseRequested": False,
            "attempts": [],
            "logs": [],
            "result": None,
        }
    append_job_log(job_id, f"{backend_label} 任务已创建，准备开始。")
    worker = threading.Thread(target=run_adspower_paypal_link_job, args=(job_id, req), daemon=True)
    worker.start()
    return snapshot_job(job_id)


@app.post("/api/adspower/open-url")
def open_url_in_adspower(req: AdsPowerOpenUrlRequest) -> dict[str, Any]:
    profile_id = str(req.ads_power_profile_id or "").strip()
    target_url = str(req.target_url or "").strip()
    if not profile_id:
        raise HTTPException(status_code=400, detail="browser profile id / dirId is required")
    if not target_url:
        raise HTTPException(status_code=400, detail="targetUrl is required")
    try:
        browser_backend = normalize_browser_backend(req.browser_backend)
        result = open_url_in_profile(
            browser_backend=browser_backend,
            api_base=resolve_browser_backend_api_base(req),
            ads_power_api_key=req.ads_power_api_key,
            roxybrowser_api_key=resolve_browser_backend_api_key(req) if browser_backend == "roxybrowser" else req.roxybrowser_api_key,
            profile_id=profile_id,
            target_url=target_url,
            clear_cache_before_start=bool(req.clear_cache_before_start),
            close_profile_on_finish=bool(req.close_profile_on_finish),
        )
        return {
            "ok": True,
            "profileId": profile_id,
            "targetUrl": target_url,
            "pageUrl": str(result.get("page_url") or "").strip(),
            "pageTitle": str(result.get("page_title") or "").strip(),
        }
    except AdsPowerWorkerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except AdsPowerApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RoxyBrowserApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"AdsPower open url unexpected error: {type(exc).__name__}: {exc}",
        ) from exc


@app.post("/api/adspower/run-redirect-url")
def run_redirect_url_in_adspower(req: AdsPowerOpenUrlRequest) -> dict[str, Any]:
    profile_id = str(req.ads_power_profile_id or '').strip()
    target_url = str(req.target_url or '').strip()
    if not profile_id:
        raise HTTPException(status_code=400, detail="browser profile id / dirId is required")
    if not target_url:
        raise HTTPException(status_code=400, detail="targetUrl is required")
    try:
        browser_backend = normalize_browser_backend(req.browser_backend)
        result = run_url_in_profile_until_terminal(
            browser_backend=browser_backend,
            api_base=resolve_browser_backend_api_base(req),
            ads_power_api_key=req.ads_power_api_key,
            roxybrowser_api_key=resolve_browser_backend_api_key(req) if browser_backend == "roxybrowser" else req.roxybrowser_api_key,
            profile_id=profile_id,
            target_url=target_url,
            clear_cache_before_start=bool(req.clear_cache_before_start),
            close_profile_on_finish=bool(req.close_profile_on_finish),
        )
        return {
            "ok": bool(result.get("ok")),
            "status": str(result.get("status") or '').strip(),
            "reason": str(result.get("reason") or '').strip(),
            "profileId": profile_id,
            "targetUrl": target_url,
            "pageUrl": str(result.get("page_url") or '').strip(),
            "pageTitle": str(result.get("page_title") or '').strip(),
            "bodyText": str(result.get("body_text") or '').strip(),
        }
    except AdsPowerWorkerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except AdsPowerApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RoxyBrowserApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"AdsPower redirect unexpected error: {type(exc).__name__}: {exc}",
        ) from exc


@app.post("/api/adspower/run-hosted-checkout")
def run_hosted_checkout_in_adspower(req: AdsPowerHostedCheckoutRequest) -> dict[str, Any]:
    profile_id = str(req.ads_power_profile_id or '').strip()
    target_url = str(req.target_url or '').strip()
    if not profile_id:
        raise HTTPException(status_code=400, detail="browser profile id / dirId is required")
    if not target_url:
        raise HTTPException(status_code=400, detail="targetUrl is required")
    try:
        browser_backend = normalize_browser_backend(req.browser_backend)
        result = run_hosted_checkout_in_profile(
            browser_backend=browser_backend,
            api_base=resolve_browser_backend_api_base(req),
            ads_power_api_key=req.ads_power_api_key,
            roxybrowser_api_key=resolve_browser_backend_api_key(req) if browser_backend == "roxybrowser" else req.roxybrowser_api_key,
            profile_id=profile_id,
            target_url=target_url,
            guest_profile=req.guest_profile,
            verification_url=req.verification_url,
            verification_resend_max_attempts=int(req.verification_resend_max_attempts or 0),
            verification_poll_attempts=int(req.verification_poll_attempts or 6),
            verification_poll_interval_seconds=int(req.verification_poll_interval_seconds or 5),
            external_verification=bool(req.external_verification),
            timeout_seconds=int(req.timeout_seconds or 240),
            clear_cache_before_start=bool(req.clear_cache_before_start),
            close_profile_on_finish=bool(req.close_profile_on_finish),
        )
        return {
            "ok": bool(result.get("ok")),
            "status": str(result.get("status") or '').strip(),
            "reason": str(result.get("reason") or '').strip(),
            "profileId": profile_id,
            "targetUrl": target_url,
            "pageUrl": str(result.get("page_url") or '').strip(),
            "pageTitle": str(result.get("page_title") or '').strip(),
            "bodyText": str(result.get("body_text") or '').strip(),
            "history": result.get("history") or [],
        }
    except AdsPowerWorkerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except AdsPowerApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RoxyBrowserApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"AdsPower hosted checkout unexpected error: {type(exc).__name__}: {exc}",
        ) from exc


@app.post("/api/adspower/continue-hosted-checkout")
def continue_hosted_checkout_in_adspower(req: AdsPowerHostedCheckoutContinueRequest) -> dict[str, Any]:
    profile_id = str(req.ads_power_profile_id or '').strip()
    verification_code = str(req.verification_code or '').strip()
    if not profile_id:
        raise HTTPException(status_code=400, detail="browser profile id / dirId is required")
    if not verification_code:
        raise HTTPException(status_code=400, detail="verificationCode is required")
    try:
        browser_backend = normalize_browser_backend(req.browser_backend)
        result = continue_hosted_checkout_in_profile(
            browser_backend=browser_backend,
            api_base=resolve_browser_backend_api_base(req),
            ads_power_api_key=req.ads_power_api_key,
            roxybrowser_api_key=resolve_browser_backend_api_key(req) if browser_backend == "roxybrowser" else req.roxybrowser_api_key,
            profile_id=profile_id,
            verification_code=verification_code,
            guest_profile=req.guest_profile,
            timeout_seconds=int(req.timeout_seconds or 240),
            close_profile_on_finish=bool(req.close_profile_on_finish),
        )
        return {
            "ok": bool(result.get("ok")),
            "status": str(result.get("status") or '').strip(),
            "reason": str(result.get("reason") or '').strip(),
            "profileId": profile_id,
            "pageUrl": str(result.get("page_url") or '').strip(),
            "pageTitle": str(result.get("page_title") or '').strip(),
            "bodyText": str(result.get("body_text") or '').strip(),
            "history": result.get("history") or [],
        }
    except AdsPowerWorkerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except AdsPowerApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RoxyBrowserApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"AdsPower continue hosted checkout unexpected error: {type(exc).__name__}: {exc}",
        ) from exc


@app.post("/api/adspower/resend-hosted-checkout-verification")
def resend_hosted_checkout_verification_in_adspower(req: AdsPowerHostedCheckoutResendRequest) -> dict[str, Any]:
    profile_id = str(req.ads_power_profile_id or '').strip()
    if not profile_id:
        raise HTTPException(status_code=400, detail="browser profile id / dirId is required")
    try:
        browser_backend = normalize_browser_backend(req.browser_backend)
        result = click_hosted_checkout_verification_resend_in_profile(
            browser_backend=browser_backend,
            api_base=resolve_browser_backend_api_base(req),
            ads_power_api_key=req.ads_power_api_key,
            roxybrowser_api_key=resolve_browser_backend_api_key(req) if browser_backend == "roxybrowser" else req.roxybrowser_api_key,
            profile_id=profile_id,
            close_profile_on_finish=bool(req.close_profile_on_finish),
        )
        return {
            "ok": bool(result.get("ok")),
            "status": str(result.get("status") or '').strip(),
            "reason": str(result.get("reason") or '').strip(),
            "profileId": profile_id,
            "resendClicked": bool(result.get("resend_clicked")),
            "pageUrl": str(result.get("page_url") or '').strip(),
            "pageTitle": str(result.get("page_title") or '').strip(),
            "bodyText": str(result.get("body_text") or '').strip(),
        }
    except AdsPowerWorkerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except AdsPowerApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RoxyBrowserApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"AdsPower resend hosted checkout verification unexpected error: {type(exc).__name__}: {exc}",
        ) from exc


@app.get("/api/paypal-link/jobs/{job_id}", response_model=JobStatusResponse)
def get_paypal_link_job(job_id: str) -> JobStatusResponse:
    return snapshot_job(job_id)


@app.post("/api/paypal-link/jobs/{job_id}/pause", response_model=JobStatusResponse)
def pause_paypal_link_job(job_id: str) -> JobStatusResponse:
    with JOB_STORE_LOCK:
        job = JOB_STORE.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        status = str(job.get("status") or "")
        if status in {"succeeded", "failed"}:
            raise HTTPException(status_code=409, detail="job already finished")
        job["pauseRequested"] = True
        if status != "paused":
            job["message"] = "已收到暂停请求，当前尝试结束后会暂停。"
    append_job_log(job_id, "已收到暂停请求，当前尝试结束后会暂停。", "warn")
    return snapshot_job(job_id)


@app.post("/api/paypal-link/jobs/{job_id}/resume", response_model=JobStatusResponse)
def resume_paypal_link_job(job_id: str) -> JobStatusResponse:
    with JOB_STORE_LOCK:
        job = JOB_STORE.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        status = str(job.get("status") or "")
        if status in {"succeeded", "failed"}:
            raise HTTPException(status_code=409, detail="job already finished")
        job["pauseRequested"] = False
        if status == "paused":
            job["status"] = "running"
            job["message"] = "已收到继续请求，任务恢复中。"
    append_job_log(job_id, "已收到继续请求，任务恢复中。", "info")
    return snapshot_job(job_id)
