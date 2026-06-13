from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.base_sms import (
    FIVE_SIM_DEFAULT_BASE_URL,
    FIVE_SIM_DEFAULT_COUNTRY,
    FIVE_SIM_DEFAULT_OPERATOR,
    FIVE_SIM_DEFAULT_PRODUCT,
    HERO_SMS_DEFAULT_COUNTRY,
    HERO_SMS_DEFAULT_SERVICE,
    NEXSMS_DEFAULT_BASE_URL,
    NEXSMS_DEFAULT_SERVICE,
    SMSPOOL_DEFAULT_BASE_URL,
    SMSPOOL_DEFAULT_COMPAT_BASE_URL,
    SMSPOOL_DEFAULT_COUNTRY,
    SMSPOOL_DEFAULT_SERVICE,
    FiveSimProvider,
    GrizzlySmsProvider,
    HeroSmsProvider,
    NexSmsProvider,
    SmsBowerProvider,
    SmsPoolProvider,
    SmsVerificationNumberProvider,
)
from infrastructure.provider_settings_repository import ProviderSettingsRepository

router = APIRouter(prefix="/sms", tags=["sms"])


class HeroSmsQueryRequest(BaseModel):
    api_key: str = ""
    service: str = ""
    country: str = ""
    proxy: str = ""


def _saved_herosms_config() -> dict:
    repo = ProviderSettingsRepository()
    # 兼容旧版 provider_key "herosms" 和新版 "herosms_api"
    config = repo.resolve_runtime_settings("sms", "herosms_api", {})
    if not config.get("herosms_api_key"):
        config = repo.resolve_runtime_settings("sms", "herosms", {})
    return config


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _saved_sms_config(provider_key: str) -> dict:
    return ProviderSettingsRepository().resolve_runtime_settings("sms", provider_key, {})


def _provider_from_payload(payload: HeroSmsQueryRequest | None = None) -> HeroSmsProvider:
    payload = payload or HeroSmsQueryRequest()
    saved = _saved_herosms_config()
    api_key = str(payload.api_key or saved.get("herosms_api_key") or "").strip()
    return HeroSmsProvider(
        api_key=api_key,
        default_service=str(payload.service or saved.get("sms_service") or saved.get("herosms_service") or saved.get("herosms_default_service") or HERO_SMS_DEFAULT_SERVICE),
        default_country=str(payload.country or saved.get("sms_country") or saved.get("herosms_country") or saved.get("herosms_default_country") or HERO_SMS_DEFAULT_COUNTRY),
        max_price=_safe_float(saved.get("herosms_max_price"), -1),
        proxy=str(payload.proxy or saved.get("sms_proxy") or saved.get("proxy") or "") or None,
    )


@router.get("/herosms/countries")
def herosms_countries():
    try:
        return {"countries": _provider_from_payload().get_countries()}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/herosms/services")
def herosms_services(country: str = ""):
    try:
        return {"services": _provider_from_payload(HeroSmsQueryRequest(country=country)).get_services(country=country or None)}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.post("/herosms/balance")
def herosms_balance(body: HeroSmsQueryRequest | None = None):
    body = body or HeroSmsQueryRequest()
    provider = _provider_from_payload(body)
    if not provider.api_key:
        raise HTTPException(400, "HeroSMS API Key 未配置")
    try:
        return {"balance": provider.get_balance()}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.post("/herosms/prices")
def herosms_prices(body: HeroSmsQueryRequest | None = None):
    body = body or HeroSmsQueryRequest()
    provider = _provider_from_payload(body)
    if not provider.api_key:
        raise HTTPException(400, "HeroSMS API Key 未配置")
    try:
        service = str(body.service or provider.default_service or HERO_SMS_DEFAULT_SERVICE)
        country = str(body.country or provider.default_country or HERO_SMS_DEFAULT_COUNTRY)
        return {"prices": provider.get_prices(service=service, country=country)}
    except Exception as exc:
        raise HTTPException(502, str(exc))


class HeroSmsBestCountryRequest(BaseModel):
    api_key: str = ""
    service: str = ""
    proxy: str = ""
    min_stock: int = 20
    max_price: float = 0
    top_n: int = 10


@router.post("/herosms/top-countries")
def herosms_top_countries(body: HeroSmsBestCountryRequest | None = None):
    """获取按价格排序的国家列表（含价格和库存）。"""
    body = body or HeroSmsBestCountryRequest()
    provider = _provider_from_payload(HeroSmsQueryRequest(
        api_key=body.api_key, service=body.service, proxy=body.proxy,
    ))
    if not provider.api_key:
        raise HTTPException(400, "HeroSMS API Key 未配置")
    try:
        service = str(body.service or provider.default_service or HERO_SMS_DEFAULT_SERVICE)
        rows = provider.get_top_countries(service=service)
        # 只返回有库存的
        rows = [r for r in rows if (r.get("count") or 0) > 0]
        if body.top_n > 0:
            rows = rows[:body.top_n]
        return {"countries": rows, "service": service}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.post("/herosms/best-country")
def herosms_best_country(body: HeroSmsBestCountryRequest | None = None):
    """自动选择最优国家（价格最低 + 库存充足）。"""
    body = body or HeroSmsBestCountryRequest()
    provider = _provider_from_payload(HeroSmsQueryRequest(
        api_key=body.api_key, service=body.service, proxy=body.proxy,
    ))
    if not provider.api_key:
        raise HTTPException(400, "HeroSMS API Key 未配置")
    try:
        service = str(body.service or provider.default_service or HERO_SMS_DEFAULT_SERVICE)
        best = provider.get_best_country(
            service=service,
            min_stock=body.min_stock,
            max_price=body.max_price,
        )
        if best:
            # 获取详细信息
            rows = provider.get_top_countries(service=service)
            detail = next((r for r in rows if str(r.get("country")) == str(best)), None)
            return {
                "country": best,
                "detail": detail,
                "service": service,
            }
        return {"country": None, "detail": None, "service": service}
    except Exception as exc:
        raise HTTPException(502, str(exc))


# ── SMSBower endpoints ──────────────────────────────────────────────────────

def _saved_smsbower_config() -> dict:
    return _saved_sms_config("smsbower_api")


def _smsbower_from_payload(payload: HeroSmsQueryRequest | None = None) -> SmsBowerProvider:
    payload = payload or HeroSmsQueryRequest()
    saved = _saved_smsbower_config()
    api_key = str(payload.api_key or saved.get("smsbower_api_key") or "").strip()
    return SmsBowerProvider(
        api_key=api_key,
        default_service=str(payload.service or saved.get("sms_service") or saved.get("smsbower_service") or saved.get("smsbower_default_service") or HERO_SMS_DEFAULT_SERVICE),
        default_country=str(payload.country or saved.get("sms_country") or saved.get("smsbower_country") or saved.get("smsbower_default_country") or HERO_SMS_DEFAULT_COUNTRY),
        max_price=_safe_float(saved.get("smsbower_max_price"), -1),
        proxy=str(payload.proxy or saved.get("sms_proxy") or saved.get("proxy") or "") or None,
    )


@router.get("/smsbower/countries")
def smsbower_countries():
    try:
        provider = _smsbower_from_payload()
        if not provider.api_key:
            return {"countries": []}
        return {"countries": provider.get_countries()}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/smsbower/services")
def smsbower_services(country: str = ""):
    try:
        provider = _smsbower_from_payload(HeroSmsQueryRequest(country=country))
        if not provider.api_key:
            return {"services": []}
        return {"services": provider.get_services(country=country or None)}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.post("/smsbower/balance")
def smsbower_balance(body: HeroSmsQueryRequest | None = None):
    body = body or HeroSmsQueryRequest()
    provider = _smsbower_from_payload(body)
    if not provider.api_key:
        raise HTTPException(400, "SMSBower API Key 未配置")
    try:
        return {"balance": provider.get_balance()}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.post("/smsbower/prices")
def smsbower_prices(body: HeroSmsQueryRequest | None = None):
    body = body or HeroSmsQueryRequest()
    provider = _smsbower_from_payload(body)
    if not provider.api_key:
        raise HTTPException(400, "SMSBower API Key 未配置")
    try:
        service = str(body.service or provider.default_service or HERO_SMS_DEFAULT_SERVICE)
        country = str(body.country or provider.default_country or HERO_SMS_DEFAULT_COUNTRY)
        return {"prices": provider.get_prices(service=service, country=country)}
    except Exception as exc:
        raise HTTPException(502, str(exc))


# ── GuJumpgate SMS provider catalog endpoints ───────────────────────────────

def _grizzlysms_from_payload(payload: HeroSmsQueryRequest | None = None) -> GrizzlySmsProvider:
    payload = payload or HeroSmsQueryRequest()
    saved = _saved_sms_config("grizzlysms_api")
    provider = GrizzlySmsProvider(
        api_key=str(payload.api_key or saved.get("grizzlysms_api_key") or "").strip(),
        default_service=str(payload.service or saved.get("sms_service") or saved.get("grizzlysms_service") or saved.get("grizzlysms_default_service") or HERO_SMS_DEFAULT_SERVICE),
        default_country=str(payload.country or saved.get("sms_country") or saved.get("grizzlysms_country") or saved.get("grizzlysms_default_country") or "52"),
        max_price=_safe_float(saved.get("grizzlysms_max_price"), -1),
        proxy=str(payload.proxy or saved.get("sms_proxy") or saved.get("proxy") or "") or None,
    )
    base_url = str(saved.get("grizzlysms_base_url") or "").strip()
    if base_url:
        provider.BASE_URL = base_url
    return provider


def _sms_verification_number_from_payload(payload: HeroSmsQueryRequest | None = None) -> SmsVerificationNumberProvider:
    payload = payload or HeroSmsQueryRequest()
    saved = _saved_sms_config("sms_verification_number_api")
    provider = SmsVerificationNumberProvider(
        api_key=str(payload.api_key or saved.get("sms_verification_number_api_key") or "").strip(),
        default_service=str(payload.service or saved.get("sms_service") or saved.get("sms_verification_number_service") or saved.get("sms_verification_number_default_service") or HERO_SMS_DEFAULT_SERVICE),
        default_country=str(payload.country or saved.get("sms_country") or saved.get("sms_verification_number_country") or saved.get("sms_verification_number_default_country") or "33"),
        max_price=_safe_float(saved.get("sms_verification_number_max_price"), -1),
        proxy=str(payload.proxy or saved.get("sms_proxy") or saved.get("proxy") or "") or None,
    )
    base_url = str(saved.get("sms_verification_number_base_url") or "").strip()
    if base_url:
        provider.BASE_URL = base_url
    return provider


def _smspool_from_payload(payload: HeroSmsQueryRequest | None = None) -> SmsPoolProvider:
    payload = payload or HeroSmsQueryRequest()
    saved = _saved_sms_config("smspool_api")
    return SmsPoolProvider(
        api_key=str(payload.api_key or saved.get("smspool_api_key") or "").strip(),
        default_service=str(payload.service or saved.get("smspool_service") or saved.get("smspool_default_service") or SMSPOOL_DEFAULT_SERVICE),
        default_country=str(payload.country or saved.get("smspool_country") or saved.get("smspool_default_country") or SMSPOOL_DEFAULT_COUNTRY),
        base_url=str(saved.get("smspool_base_url") or SMSPOOL_DEFAULT_BASE_URL),
        compat_base_url=str(saved.get("smspool_compat_base_url") or SMSPOOL_DEFAULT_COMPAT_BASE_URL),
        max_price=_safe_float(saved.get("smspool_max_price"), -1),
        proxy=str(payload.proxy or saved.get("sms_proxy") or saved.get("proxy") or "") or None,
    )


def _five_sim_from_payload(payload: HeroSmsQueryRequest | None = None) -> FiveSimProvider:
    payload = payload or HeroSmsQueryRequest()
    saved = _saved_sms_config("five_sim_api")
    return FiveSimProvider(
        api_key=str(payload.api_key or saved.get("five_sim_api_key") or "").strip(),
        country=str(payload.country or saved.get("five_sim_country") or saved.get("five_sim_default_country") or FIVE_SIM_DEFAULT_COUNTRY),
        operator=str(saved.get("five_sim_operator") or FIVE_SIM_DEFAULT_OPERATOR),
        product=str(payload.service or saved.get("five_sim_product") or saved.get("fiveSimProduct") or FIVE_SIM_DEFAULT_PRODUCT),
        base_url=str(saved.get("five_sim_base_url") or FIVE_SIM_DEFAULT_BASE_URL),
        max_price=_safe_float(saved.get("five_sim_max_price"), -1),
        proxy=str(payload.proxy or saved.get("sms_proxy") or saved.get("proxy") or "") or None,
    )


def _nexsms_from_payload(payload: HeroSmsQueryRequest | None = None) -> NexSmsProvider:
    payload = payload or HeroSmsQueryRequest()
    saved = _saved_sms_config("nexsms_api")
    return NexSmsProvider(
        api_key=str(payload.api_key or saved.get("nexsms_api_key") or "").strip(),
        country_order=payload.country or saved.get("nexsms_default_country") or saved.get("nexsms_country_order") or saved.get("nexsms_country") or "",
        service_code=str(payload.service or saved.get("nexsms_service") or saved.get("nexsms_default_service") or NEXSMS_DEFAULT_SERVICE),
        base_url=str(saved.get("nexsms_base_url") or NEXSMS_DEFAULT_BASE_URL),
        max_price=_safe_float(saved.get("nexsms_max_price"), -1),
        proxy=str(payload.proxy or saved.get("sms_proxy") or saved.get("proxy") or "") or None,
    )


@router.get("/grizzlysms/countries")
def grizzlysms_countries():
    try:
        provider = _grizzlysms_from_payload()
        if not provider.api_key:
            return {"countries": []}
        return {"countries": provider.get_countries()}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/grizzlysms/services")
def grizzlysms_services(country: str = ""):
    try:
        provider = _grizzlysms_from_payload(HeroSmsQueryRequest(country=country))
        if not provider.api_key:
            return {"services": []}
        return {"services": provider.get_services(country=country or None)}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/sms-verification-number/countries")
def sms_verification_number_countries():
    try:
        provider = _sms_verification_number_from_payload()
        if not provider.api_key:
            return {"countries": []}
        return {"countries": provider.get_countries()}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/sms-verification-number/services")
def sms_verification_number_services(country: str = ""):
    try:
        provider = _sms_verification_number_from_payload(HeroSmsQueryRequest(country=country))
        if not provider.api_key:
            return {"services": []}
        return {"services": provider.get_services(country=country or None)}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/smspool/countries")
def smspool_countries():
    try:
        return {"countries": _smspool_from_payload().get_countries()}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/smspool/services")
def smspool_services(country: str = ""):
    try:
        return {"services": _smspool_from_payload(HeroSmsQueryRequest(country=country)).get_services(country=country or None)}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/five-sim/countries")
def five_sim_countries():
    try:
        return {"countries": _five_sim_from_payload().get_countries()}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/five-sim/services")
def five_sim_services(country: str = ""):
    try:
        return {"services": _five_sim_from_payload(HeroSmsQueryRequest(country=country)).get_services(country=country or None)}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/nexsms/countries")
def nexsms_countries():
    try:
        provider = _nexsms_from_payload()
        if not provider.api_key:
            return {"countries": []}
        return {"countries": provider.get_countries()}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/nexsms/services")
def nexsms_services(country: str = ""):
    try:
        provider = _nexsms_from_payload(HeroSmsQueryRequest(country=country))
        if not provider.api_key:
            return {"services": []}
        return {"services": provider.get_services(country=country or None)}
    except Exception as exc:
        raise HTTPException(502, str(exc))
