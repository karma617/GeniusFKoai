"""接码服务基类 + SMS-Activate / HeroSMS 实现。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

logger = logging.getLogger(__name__)


def _is_virtual_or_voip_phone_rejection(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(
        marker in text
        for marker in (
            "voip_phone_disallowed",
            "virtual phone",
            "voip",
            "non-virtual phone",
        )
    )


@dataclass
class SmsActivation:
    """Represents an active phone number rental."""
    activation_id: str
    phone_number: str
    country: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class CodexSmsPoolEntry:
    """本地接码池单条记录。"""
    index: int
    key: str
    phone: str
    phone_e164: str
    verification_url: str


class BaseSmsProvider(ABC):
    """Base class for SMS verification code providers."""

    auto_report_success_on_code = True

    @abstractmethod
    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        """Rent a phone number for the given service."""
        ...

    @abstractmethod
    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        """Wait for and return the SMS verification code."""
        ...

    @abstractmethod
    def cancel(self, activation_id: str) -> bool:
        """Cancel/release an activation. Returns True on success."""
        ...

    def report_success(self, activation_id: str) -> bool:
        """Report that the code was used successfully (optional)."""
        return True

    def set_resend_callback(self, callback: Callable[[], None] | None) -> None:
        """Optional hook used by providers that can request upstream resend."""
        return None

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        """Optional hook used when the target service rejects a received code."""
        return None

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        """Optional hook used when the target service rejects the rented phone."""
        return None

    def mark_send_succeeded(self, activation_id: str) -> None:
        """Optional hook used when the target service accepts the rented phone."""
        return None

    def get_reuse_info(self) -> dict:
        """Return provider-specific reuse state for task scheduling."""
        return {}

    def get_current_price_info(self, *, service: str, country: str = "") -> dict:
        """查询当前服务/国家的号码价格；provider 不支持时返回空字典。"""
        return {}


# ---------------------------------------------------------------------------
# SMS-Activate implementation (https://sms-activate.guru)
# ---------------------------------------------------------------------------

SMS_ACTIVATE_SERVICES = {
    "cursor": "ot",
    "chatgpt": "dr",
    "openai": "dr",
    "google": "go",
    "microsoft": "mg",
    "default": "ot",
}

SMS_ACTIVATE_COUNTRIES = {
    "ru": "0",
    "us": "187",
    "uk": "16",
    "in": "22",
    "id": "6",
    "ph": "4",
    "th": "52",
    "br": "73",
    "default": "0",
}


def _normalize_country_lookup_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _slugify_country_lookup_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def _resolve_sms_activate_country_id(country: str, default_country: str) -> str:
    raw = str(country or default_country or "").strip().lower()
    if not raw:
        raw = "default"
    if raw.isdigit():
        return raw
    return SMS_ACTIVATE_COUNTRIES.get(raw, SMS_ACTIVATE_COUNTRIES["default"])


class SmsActivateProvider(BaseSmsProvider):
    """SMS-Activate (sms-activate.guru) provider."""

    BASE_URL = "https://api.sms-activate.guru/stubs/handler_api.php"

    def __init__(self, api_key: str, *, default_country: str = "", proxy: str = None):
        self.api_key = api_key
        self.default_country = default_country or "ru"
        self._proxy = {"http": proxy, "https": proxy} if proxy else None

    def _request(self, action: str, **params) -> str:
        params["api_key"] = self.api_key
        params["action"] = action
        resp = requests.get(
            self.BASE_URL,
            params=params,
            timeout=20,
            proxies=self._proxy,
        )
        resp.raise_for_status()
        return resp.text.strip()

    def get_balance(self) -> float:
        result = self._request("getBalance")
        if result.startswith("ACCESS_BALANCE:"):
            return float(result.split(":")[1])
        raise RuntimeError(f"SMS-Activate getBalance failed: {result}")

    def get_current_price_info(self, *, service: str, country: str = "") -> dict:
        service_code = SMS_ACTIVATE_SERVICES.get(service, SMS_ACTIVATE_SERVICES["default"])
        country_id = _resolve_sms_activate_country_id(country, self.default_country)
        try:
            data = json.loads(self._request("getPrices", service=service_code, country=country_id))
        except Exception:
            return {}
        info = _extract_price_info_from_services(data, service=service_code, country=country_id)
        if info:
            info.update({"service": service_code, "country": country_id})
        return info

    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        service_code = SMS_ACTIVATE_SERVICES.get(service, SMS_ACTIVATE_SERVICES["default"])
        country_id = _resolve_sms_activate_country_id(country, self.default_country)

        result = self._request("getNumber", service=service_code, country=country_id)
        if result.startswith("ACCESS_NUMBER:"):
            parts = result.split(":")
            return SmsActivation(
                activation_id=parts[1],
                phone_number=parts[2],
                country=country or self.default_country,
                metadata={"service": service_code, "country": country_id},
            )

        if "NO_NUMBERS" in result:
            raise RuntimeError(f"SMS-Activate: 当前无可用号码 (service={service_code}, country={country_id})")
        if "NO_BALANCE" in result:
            raise RuntimeError("SMS-Activate: 余额不足")
        raise RuntimeError(f"SMS-Activate getNumber failed: {result}")

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self._request("getStatus", id=activation_id)
            if result.startswith("STATUS_OK:"):
                return result.split(":")[1]
            if result == "STATUS_WAIT_CODE":
                time.sleep(3)
                continue
            if result == "STATUS_WAIT_RETRY":
                self._request("setStatus", id=activation_id, status="6")
                time.sleep(3)
                continue
            if result == "STATUS_CANCEL":
                return ""
            time.sleep(3)

        self.cancel(activation_id)
        return ""

    def cancel(self, activation_id: str) -> bool:
        result = self._request("setStatus", id=activation_id, status="8")
        return "ACCESS" in result

    def report_success(self, activation_id: str) -> bool:
        result = self._request("setStatus", id=activation_id, status="6")
        return "ACCESS" in result


# ---------------------------------------------------------------------------
# HeroSMS implementation (https://hero-sms.com/stubs/handler_api.php)
# ---------------------------------------------------------------------------

HERO_SMS_DEFAULT_SERVICE = "dr"
HERO_SMS_DEFAULT_COUNTRY = "187"
HERO_SMS_PHONE_LIFETIME = 20 * 60
SMS_PHONE_FAILURES_PER_COUNTRY = 10
SMS_COUNTRY_RETRY_LIMIT = 2
_HERO_SMS_CACHE_LOCK = threading.Lock()
_HERO_SMS_VERIFY_LOCK = threading.RLock()
_HERO_SMS_CACHE: dict | None = None


def _project_data_dir() -> Path:
    root = Path(__file__).resolve().parent.parent
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def hero_sms_cache_file() -> Path:
    return _project_data_dir() / ".herosms_phone_cache.json"


def _hash_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _redact_sms_error_text(value: Any) -> str:
    text = str(value or "")
    return re.sub(
        r"(?i)([?&](?:api_key|apikey|token|access_token|key)=)[^&\s)]+",
        r"\1***",
        text,
    )


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _maybe_float(value) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _format_number_for_log(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = f"{number:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _extract_price_info_from_services(
    prices: dict,
    *,
    service: str,
    country: str,
) -> dict:
    """从 SMS-Activate/HeroSMS/SMSBower getPrices 响应中解析价格与库存。"""
    if not isinstance(prices, dict):
        return {}
    service_code = str(service or "").strip()
    country_id = str(country or "").strip()
    candidates: list[Any] = []
    country_prices = prices.get(country_id)
    if country_prices is None and country_id.isdigit():
        country_prices = prices.get(int(country_id))
    if isinstance(country_prices, dict):
        candidates.append(country_prices.get(service_code))
        candidates.append(country_prices)
    candidates.append(prices.get(service_code))
    candidates.append(prices)

    for item in candidates:
        if not isinstance(item, dict):
            continue
        price = _maybe_float(
            item.get("cost")
            or item.get("price")
            or item.get("retail_price")
            or item.get("retailPrice")
            or item.get("activationCost")
            or item.get("activationPrice")
        )
        count = _maybe_int(
            item.get("count")
            or item.get("qty")
            or item.get("available")
            or item.get("stock")
            or item.get("total")
        )
        if price is None and count is None:
            continue
        return {
            "price": price,
            "count": count,
            "currency": str(item.get("currency") or "USD"),
            "raw": item,
        }
    return {}


def _safe_bool(value, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _first_nonempty_text(*values) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _normalize_hero_proxy(proxy: str | None) -> str | None:
    proxy = str(proxy or "").strip()
    if not proxy or proxy.startswith("singbox://"):
        return None
    return proxy


def _parse_hero_status_text(text: str) -> dict:
    text = str(text or "").strip()
    if text == "STATUS_WAIT_CODE":
        return {"status": "wait_code"}
    if text.startswith("STATUS_WAIT_RETRY"):
        return {"status": "wait_retry", "raw": text}
    if text == "STATUS_WAIT_RESEND":
        return {"status": "wait_resend"}
    if text.startswith("STATUS_OK:"):
        return {"status": "ok", "code": text.split(":", 1)[1]}
    if text == "STATUS_CANCEL":
        return {"status": "cancel"}
    return {"status": "unknown", "raw": text}


def _canonical_sms_event_fields(event_fields: dict | None) -> dict:
    event_fields = event_fields or {}
    canonical: dict[str, str] = {}
    channel = str(event_fields.get("channel") or "").strip()
    if channel:
        canonical["channel"] = channel
    sms_time = (
        event_fields.get("dateTime")
        or event_fields.get("date")
        or event_fields.get("smsDate")
        or event_fields.get("smsTime")
        or ""
    )
    if sms_time:
        canonical["time"] = str(sms_time)
    text = event_fields.get("text") or event_fields.get("smsText")
    if text:
        canonical["text"] = str(text)
    if channel == "call":
        for key in ("from", "url"):
            if event_fields.get(key):
                canonical[key] = str(event_fields[key])
    if not sms_time:
        for key in ("repeated", "activationStatus", "verificationType"):
            if event_fields.get(key) is not None:
                canonical[key] = str(event_fields[key])
    return canonical


def _has_real_sms_time(event_fields: dict | None) -> bool:
    raw_time = (
        (event_fields or {}).get("dateTime")
        or (event_fields or {}).get("date")
        or (event_fields or {}).get("smsDate")
        or (event_fields or {}).get("smsTime")
        or ""
    )
    raw_time = str(raw_time).strip()
    return bool(raw_time and raw_time not in {"0", "0000-00-00 00:00:00", "0000-00-00T00:00:00"})


def _sms_event_key(activation_id: str, code: str, event_fields: dict | None) -> str:
    identity = {"activation_id": str(activation_id), "code": str(code)}
    identity.update(_canonical_sms_event_fields(event_fields))
    raw = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _make_sms_candidate(activation_id: str, source: str, code, event_fields: dict | None = None) -> dict | None:
    code = str(code or "").strip()
    if not code or code in {"null", "None"}:
        return None
    canonical = _canonical_sms_event_fields(event_fields)
    sms_key = _sms_event_key(activation_id, code, event_fields) if event_fields else ""
    return {
        "status": "ok",
        "code": code,
        "source": source,
        "sms_key": sms_key,
        "sms_time": canonical.get("time", ""),
        "sms_text": canonical.get("text", ""),
        "allow_same_code": _has_real_sms_time(event_fields),
    }


def _candidate_is_attempted(candidate: dict, used_codes: set, attempted_sms_keys: set) -> bool:
    sms_key = str(candidate.get("sms_key") or "")
    code = str(candidate.get("code") or "")
    if sms_key and sms_key in attempted_sms_keys:
        return True
    return bool(code in used_codes and not candidate.get("allow_same_code"))


class HeroSmsProvider(BaseSmsProvider):
    """HeroSMS provider with resend, SMS event dedupe, and short-lived phone reuse."""

    BASE_URL = "https://hero-sms.com/stubs/handler_api.php"
    DISPLAY_NAME = "HeroSMS"
    auto_report_success_on_code = False

    def __init__(
        self,
        api_key: str,
        *,
        default_service: str = HERO_SMS_DEFAULT_SERVICE,
        default_country: str = HERO_SMS_DEFAULT_COUNTRY,
        max_price: float = -1,
        proxy: str | None = None,
        reuse_phone_to_max: bool = True,
        phone_success_max: int = 3,
    ):
        self.api_key = str(api_key or "").strip()
        self.default_service = str(default_service or HERO_SMS_DEFAULT_SERVICE).strip()
        self.default_country = str(default_country or HERO_SMS_DEFAULT_COUNTRY).strip()
        self.max_price = float(max_price or -1)
        self.proxy = _normalize_hero_proxy(proxy)
        self.proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        self.reuse_phone_to_max = bool(reuse_phone_to_max)
        self.phone_success_max = max(0, int(phone_success_max or 0))
        self.openai_resend_callback: Callable[[], None] | None = None
        self.last_code_result: dict | None = None
        self.current_activation: SmsActivation | None = None
        self._country_alias_cache: dict[str, str] | None = None

    def _request(self, params: dict, *, needs_key: bool = True, timeout: int = 30) -> requests.Response:
        payload = dict(params)
        if needs_key:
            payload["api_key"] = self.api_key
        resp = requests.get(self.BASE_URL, params=payload, timeout=timeout, proxies=self.proxies)
        resp.raise_for_status()
        return resp

    @property
    def display_name(self) -> str:
        return str(getattr(self, "DISPLAY_NAME", "") or self.__class__.__name__)

    def get_balance(self) -> float:
        text = self._request({"action": "getBalance"}).text.strip()
        if text.startswith("ACCESS_BALANCE:"):
            return float(text.split(":", 1)[1])
        raise RuntimeError(f"{self.display_name} getBalance failed: {text}")

    def get_services(self, country: str | int | None = None, lang: str = "cn") -> list:
        params = {"action": "getServicesList", "lang": lang}
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params, needs_key=False).json()
        if isinstance(data, dict) and data.get("status") == "success":
            return list(data.get("services") or [])
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 可能是 {"dr": {"name": "OpenAI", ...}, ...} 格式
            result = []
            for key, value in data.items():
                if key in ("status", "message", "error"):
                    continue
                if isinstance(value, dict):
                    if "code" not in value:
                        value["code"] = key
                    result.append(value)
                elif isinstance(value, str):
                    result.append({"code": key, "name": value})
            if result:
                return result
        raise RuntimeError(f"{self.display_name} getServicesList returned unexpected response")

    def get_countries(self) -> list:
        data = self._request({"action": "getCountries"}, needs_key=False).json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 检查是否是错误响应 {"status":0,"message":"No access","data":[]}
            if data.get("status") == 0 or data.get("message") == "No access":
                raise RuntimeError(f"SMS API access denied: {data.get('message', 'unknown')}")
            # HeroSMS 可能返回 {"0": {"id": 0, "eng": "Russia"}, ...} 格式
            result = []
            for key, value in data.items():
                if key in ("status", "message", "data", "error"):
                    continue
                if isinstance(value, dict):
                    if "id" not in value:
                        value["id"] = key
                    result.append(value)
                elif isinstance(value, str):
                    result.append({"id": key, "eng": value, "name": value})
            if result:
                return result
        raise RuntimeError("SMS getCountries returned unexpected response")

    def _country_aliases(self) -> dict[str, str]:
        if self._country_alias_cache is not None:
            return self._country_alias_cache
        aliases: dict[str, str] = {}

        def add(alias: str, country_id: str) -> None:
            alias_text = str(alias or "").strip()
            country_text = str(country_id or "").strip()
            if not alias_text or not country_text:
                return
            aliases.setdefault(alias_text.lower(), country_text)
            normalized = _normalize_country_lookup_text(alias_text)
            if normalized:
                aliases.setdefault(normalized, country_text)
            slug = _slugify_country_lookup_text(alias_text)
            if slug:
                aliases.setdefault(slug, country_text)

        for alias, country_id in SMS_ACTIVATE_COUNTRIES.items():
            add(str(alias), str(country_id))
        try:
            countries = self.get_countries()
        except Exception:
            countries = []
        for item in countries:
            if not isinstance(item, dict):
                continue
            country_id = str(item.get("id") or item.get("countryId") or item.get("country_id") or "").strip()
            if not country_id:
                continue
            add(country_id, country_id)
            for key in ("eng", "name", "title", "countryName", "country_name"):
                add(str(item.get(key) or ""), country_id)
        self._country_alias_cache = aliases
        return aliases

    def _resolve_country_alias(self, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        if raw.isdigit():
            return raw
        aliases = self._country_aliases()
        return (
            aliases.get(raw.lower())
            or aliases.get(_normalize_country_lookup_text(raw))
            or aliases.get(_slugify_country_lookup_text(raw))
            or ""
        )

    def get_prices(self, service: str | None = None, country: str | int | None = None) -> dict:
        params = {"action": "getPrices"}
        if service:
            params["service"] = service
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params).json()
        if isinstance(data, dict):
            return data
        raise RuntimeError(f"{self.display_name} getPrices returned unexpected response")

    def get_current_price_info(self, *, service: str, country: str = "") -> dict:
        service_code = str(service or self.default_service or HERO_SMS_DEFAULT_SERVICE).strip()
        country_id = str(country or self.default_country or HERO_SMS_DEFAULT_COUNTRY).strip()
        try:
            prices = self.get_prices(service=service_code, country=country_id)
        except Exception:
            return {}
        info = _extract_price_info_from_services(prices, service=service_code, country=country_id)
        if info:
            info.update({"service": service_code, "country": country_id})
        return info

    def get_top_countries(self, service: str | None = None) -> list[dict]:
        """获取指定服务按价格排序的国家列表（含价格和库存）。

        优先使用 getTopCountriesByServiceRank API，降级到 getPrices 全量解析。
        返回格式: [{"country": "66", "name": "Thailand", "price": 0.12, "count": 150}, ...]
        """
        service_code = str(service or self.default_service or HERO_SMS_DEFAULT_SERVICE).strip()

        # 策略1: 使用 getTopCountriesByServiceRank（HeroSMS 专用排名接口）
        for action in ("getTopCountriesByServiceRank", "getTopCountriesByService"):
            try:
                data = self._request({"action": action, "service": service_code}).json()
                rows = self._parse_top_countries_response(data)
                if rows:
                    rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
                    return rows
            except Exception:
                continue

        # 策略2: 从 getPrices 全量数据中解析
        try:
            prices = self.get_prices(service=service_code)
            rows = []
            for country_id, services in prices.items():
                if not isinstance(services, dict):
                    continue
                svc_data = services.get(service_code)
                if not isinstance(svc_data, dict):
                    continue
                price = svc_data.get("cost") or svc_data.get("price")
                count = svc_data.get("count") or svc_data.get("qty") or svc_data.get("available")
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None and count > 0:
                    rows.append({"country": str(country_id), "price": price, "count": count})
            rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
            return rows
        except Exception:
            return []

    def _parse_top_countries_response(self, data) -> list[dict]:
        """解析 getTopCountriesByServiceRank 响应。"""
        rows = []
        items = data
        # 可能嵌套在 data/result 键下
        if isinstance(data, dict):
            items = data.get("data") or data.get("result") or data.get("response") or data
        if isinstance(items, dict):
            # {country_id: {price, count, ...}} 格式
            for key, value in items.items():
                if not isinstance(value, dict):
                    continue
                country_id = self._resolve_country_alias(str(key))
                if not country_id:
                    continue
                candidates = [value]
                if not any(field in value for field in ("price", "cost", "retail_price", "retailPrice")):
                    nested = [item for item in value.values() if isinstance(item, dict)]
                    if nested:
                        candidates = nested
                best: dict[str, Any] | None = None
                best_price: float | None = None
                best_count = 0
                for candidate in candidates:
                    price_value = (
                        candidate.get("price")
                        or candidate.get("cost")
                        or candidate.get("retail_price")
                        or candidate.get("retailPrice")
                    )
                    count_value = (
                        candidate.get("count")
                        or candidate.get("qty")
                        or candidate.get("available")
                        or candidate.get("stock")
                        or candidate.get("total")
                    )
                    try:
                        candidate_price = float(price_value) if price_value is not None else None
                    except (TypeError, ValueError):
                        candidate_price = None
                    try:
                        candidate_count = int(count_value) if count_value is not None else 0
                    except (TypeError, ValueError):
                        candidate_count = 0
                    if candidate_price is None:
                        continue
                    if best is None or candidate_price < (best_price if best_price is not None else 999999.0):
                        best = candidate
                        best_price = candidate_price
                        best_count = candidate_count
                    elif candidate_price == best_price:
                        best_count += candidate_count
                if best is None or best_price is None:
                    continue
                try:
                    price = float(best_price)
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(best_count)
                except (TypeError, ValueError):
                    count = 0
                name = (
                    best.get("name")
                    or best.get("countryName")
                    or best.get("country_name")
                    or str(key)
                )
                if price is not None:
                    rows.append({"country": country_id, "name": str(name), "price": price, "count": count})
        elif isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                country_id = item.get("country") or item.get("countryId") or item.get("country_id") or item.get("id")
                if country_id is None:
                    continue
                price = item.get("price") or item.get("cost") or item.get("retail_price") or item.get("retailPrice")
                count = item.get("count") or item.get("qty") or item.get("available") or item.get("stock") or item.get("total")
                name = item.get("name") or item.get("countryName") or item.get("country_name") or item.get("title") or ""
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None:
                    rows.append({"country": str(country_id), "name": str(name), "price": price, "count": count})
        return rows

    def get_best_country(self, service: str | None = None, *, min_stock: int = 20, max_price: float = 0) -> str | None:
        """自动选择最优国家：价格最低且库存充足。

        Args:
            service: 服务代码（默认使用 self.default_service）
            min_stock: 最低库存要求（默认 20）
            max_price: 最高价格限制（0 表示不限）

        Returns:
            最优国家 ID 字符串，或 None（无可用国家）
        """
        # HeroSMS/SMSBower 中已验证对 OpenAI 走 SMS（非 WhatsApp）的国家白名单
        # OpenAI 2025年起对绝大多数国家改用 WhatsApp 验证
        # 目前只有泰国确认走 SMS
        ALLOWED_COUNTRIES = {
            "52",   # Thailand (已验证走SMS)
        }

        try:
            rows = self.get_top_countries(service=service)
        except Exception as exc:
            logger.warning("get_best_country 查询失败: %s", exc)
            return None

        if not rows:
            return None

        for row in rows:
            country_id = str(row.get("country") or "")
            if country_id not in ALLOWED_COUNTRIES:
                continue
            price = row.get("price") or 0
            count = row.get("count") or 0
            if count < min_stock:
                continue
            if max_price > 0 and price > max_price:
                continue
            return country_id

        # 如果没有满足 min_stock 的，放宽到 count > 0
        for row in rows:
            country_id = str(row.get("country") or "")
            if country_id not in ALLOWED_COUNTRIES:
                continue
            price = row.get("price") or 0
            count = row.get("count") or 0
            if count <= 0:
                continue
            if max_price > 0 and price > max_price:
                continue
            return country_id

        return None

    def _cache_identity(self, service: str, country: str) -> dict:
        return {
            "api_key_hash": _hash_secret(self.api_key),
            "service": str(service),
            "country": str(country),
        }

    def _load_cache(self, service: str, country: str) -> dict | None:
        global _HERO_SMS_CACHE
        if _HERO_SMS_CACHE is not None:
            cache = _HERO_SMS_CACHE
        else:
            path = hero_sms_cache_file()
            if not path.exists():
                return None
            try:
                cache = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
        identity = self._cache_identity(service, country)
        if any(str(cache.get(key) or "") != str(value) for key, value in identity.items()):
            return None
        elapsed = time.time() - float(cache.get("acquired_at") or 0)
        if elapsed >= HERO_SMS_PHONE_LIFETIME or cache.get("reuse_stopped"):
            self._clear_cache()
            return None
        if self.phone_success_max > 0 and int(cache.get("use_count") or 0) >= self.phone_success_max:
            cache["reuse_stopped"] = True
            cache["stop_reason"] = f"success max reached ({self.phone_success_max})"
            self._save_cache(cache)
            return None
        cache["used_codes"] = set(cache.get("used_codes") or [])
        cache["attempted_sms_keys"] = set(cache.get("attempted_sms_keys") or [])
        _HERO_SMS_CACHE = cache
        return cache

    def _save_cache(self, cache: dict | None) -> None:
        global _HERO_SMS_CACHE
        _HERO_SMS_CACHE = cache
        path = hero_sms_cache_file()
        if cache is None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return
        serializable = dict(cache)
        serializable["used_codes"] = sorted(serializable.get("used_codes") or [])
        serializable["attempted_sms_keys"] = sorted(serializable.get("attempted_sms_keys") or [])
        serializable.pop("client", None)
        path.write_text(json.dumps(serializable, ensure_ascii=False), encoding="utf-8")

    def _clear_cache(self) -> None:
        self._save_cache(None)

    def _stop_reuse(self, reason: str) -> None:
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE
            if not cache:
                return
            cache["reuse_stopped"] = True
            cache["stop_reason"] = reason
            self._save_cache(cache)

    def _request_number_raw(self, service: str, country: str) -> dict:
        common = {"service": service, "country": country}

        # 动态获取该国家该服务的实际价格，用实际价格作为 maxPrice
        # 这样能确保拿到物理号码（而不是被分配虚拟号码）
        effective_max_price = self.max_price if self.max_price > 0 else 1
        price_info: dict = {}
        try:
            price_info = self.get_current_price_info(service=service, country=country)
            actual_cost = price_info.get("price")
            if actual_cost is not None:
                actual_cost = float(actual_cost)
                # 用实际价格的 3 倍作为 maxPrice（留足余量），但不超过用户配置的上限
                dynamic_max = round(actual_cost * 3, 4)
                if self.max_price > 0:
                    effective_max_price = min(self.max_price, max(dynamic_max, 0.2))
                else:
                    effective_max_price = max(dynamic_max, 0.2)
        except Exception:
            pass  # 查询失败就用默认值

        common["maxPrice"] = effective_max_price

        v2_error = ""
        try:
            resp = self._request({"action": "getNumberV2", **common})
            try:
                data = resp.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("activationId"):
                data.setdefault("_price_info", price_info)
                data.setdefault("_max_price", effective_max_price)
                return data
            v2_error = resp.text.strip()[:200]
        except Exception as exc:
            v2_error = _redact_sms_error_text(exc)

        # 如果 NO_NUMBERS 且 maxPrice 低于用户配置的上限，提高 maxPrice 重试
        if "NO_NUMBERS" in v2_error and self.max_price > 0 and effective_max_price < self.max_price:
            common["maxPrice"] = self.max_price
            try:
                resp = self._request({"action": "getNumberV2", **common})
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if isinstance(data, dict) and data.get("activationId"):
                    data.setdefault("_price_info", price_info)
                    data.setdefault("_max_price", common.get("maxPrice"))
                    return data
                v2_error = resp.text.strip()[:200]
            except Exception as exc:
                v2_error = _redact_sms_error_text(exc)

        try:
            text = self._request({"action": "getNumber", **common}).text.strip()
            if text.startswith("ACCESS_NUMBER:"):
                parts = text.split(":", 2)
                if len(parts) == 3:
                    return {
                        "activationId": parts[1],
                        "phoneNumber": parts[2],
                        "countryPhoneCode": "",
                        "activationCost": None,
                        "_price_info": price_info,
                        "_max_price": common.get("maxPrice"),
                    }
            raise RuntimeError(text[:200])
        except Exception as exc:
            raise RuntimeError(
                f"{self.display_name} \u83b7\u53d6\u53f7\u7801\u5931\u8d25: "
                f"V2={_redact_sms_error_text(v2_error)}; V1={_redact_sms_error_text(exc)}"
            ) from exc

    @staticmethod
    def _format_phone(number_info: dict) -> str:
        raw = str(number_info.get("phoneNumber") or "").strip()
        country_phone_code = str(number_info.get("countryPhoneCode") or "").strip()
        if raw.startswith("+"):
            return raw
        if country_phone_code and raw.startswith(country_phone_code):
            return f"+{raw}"
        if country_phone_code:
            return f"+{country_phone_code}{raw}"
        return f"+{raw}"

    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        service_code = str(self.default_service or service or HERO_SMS_DEFAULT_SERVICE).strip()
        country_id = str(country or self.default_country or HERO_SMS_DEFAULT_COUNTRY).strip()
        with _HERO_SMS_VERIFY_LOCK:
            with _HERO_SMS_CACHE_LOCK:
                cache = self._load_cache(service_code, country_id) if self.reuse_phone_to_max else None
                if cache:
                    activation = SmsActivation(
                        activation_id=str(cache["activation_id"]),
                        phone_number=str(cache["phone_number"]),
                        country=country_id,
                        metadata={
                            "reused": True,
                            "use_count": int(cache.get("use_count") or 0),
                            "price_info": cache.get("price_info") or {},
                            "activation_cost": cache.get("activation_cost"),
                            "max_price": cache.get("max_price"),
                        },
                    )
                    self.current_activation = activation
                    return activation

                number_info = self._request_number_raw(service_code, country_id)
                activation_id = str(number_info.get("activationId") or "")
                phone = self._format_phone(number_info)
                if not activation_id or not phone.strip("+"):
                    raise RuntimeError(f"{self.display_name} \u8fd4\u56de\u7684\u53f7\u7801\u4fe1\u606f\u4e0d\u5b8c\u6574")
                cache = {
                    **self._cache_identity(service_code, country_id),
                    "activation_id": activation_id,
                    "phone_number": phone,
                    "acquired_at": time.time(),
                    "use_count": 0,
                    "used_codes": set(),
                    "attempted_sms_keys": set(),
                    "reuse_stopped": False,
                    "stop_reason": "",
                    "price_info": number_info.get("_price_info") or {},
                    "activation_cost": (
                        number_info.get("activationCost")
                        or number_info.get("activationPrice")
                        or number_info.get("cost")
                        or number_info.get("price")
                    ),
                    "max_price": number_info.get("_max_price"),
                }
                self._save_cache(cache)
                activation = SmsActivation(
                    activation_id=activation_id,
                    phone_number=phone,
                    country=country_id,
                    metadata={
                        "reused": False,
                        "number_info": number_info,
                        "price_info": number_info.get("_price_info") or {},
                        "activation_cost": (
                            number_info.get("activationCost")
                            or number_info.get("activationPrice")
                            or number_info.get("cost")
                            or number_info.get("price")
                        ),
                        "max_price": number_info.get("_max_price"),
                    },
                )
                self.current_activation = activation
                return activation

    def get_status(self, activation_id: str) -> dict:
        return _parse_hero_status_text(self._request({"action": "getStatus", "id": activation_id}).text)

    def get_status_v2(self, activation_id: str) -> dict:
        resp = self._request({"action": "getStatusV2", "id": activation_id})
        text = resp.text.strip()
        try:
            data = resp.json()
        except ValueError:
            return _parse_hero_status_text(text)
        if isinstance(data, str):
            return _parse_hero_status_text(data)
        if not isinstance(data, dict):
            return {"status": "unknown", "raw": data}
        raw_status = data.get("status")
        if isinstance(raw_status, str):
            parsed = _parse_hero_status_text(raw_status)
            if parsed.get("status") != "unknown":
                return parsed
        for channel in ("sms", "call"):
            item = data.get(channel)
            if isinstance(item, dict):
                candidate = _make_sms_candidate(
                    activation_id,
                    f"getStatusV2.{channel}",
                    item.get("code"),
                    {
                        "channel": channel,
                        "dateTime": item.get("dateTime"),
                        "text": item.get("text"),
                        "from": item.get("from"),
                        "url": item.get("url"),
                        "verificationType": data.get("verificationType"),
                    },
                )
                if candidate:
                    return candidate
        return {"status": "wait_code", "raw": data}

    def get_active_activations(self, start: int = 0, limit: int = 20) -> list:
        data = self._request({"action": "getActiveActivations", "start": start, "limit": limit}).json()
        if isinstance(data, dict) and "data" in data:
            return list(data.get("data") or [])
        return []

    def set_status(self, activation_id: str, status: int) -> str:
        return self._request({"action": "setStatus", "id": activation_id, "status": status}).text.strip()

    def cancel_activation(self, activation_id: str) -> bool:
        try:
            resp = self._request({"action": "cancelActivation", "id": activation_id})
            if resp.status_code == 204 or "ACCESS_CANCEL" in resp.text:
                return True
        except Exception:
            pass
        try:
            return "ACCESS_CANCEL" in self.set_status(activation_id, 8)
        except Exception:
            return False

    def finish_activation(self, activation_id: str) -> bool:
        try:
            resp = self._request({"action": "finishActivation", "id": activation_id})
            text = resp.text.strip()
            return resp.status_code in (200, 204) or "ACCESS" in text
        except Exception:
            try:
                return "ACCESS" in self.set_status(activation_id, 6)
            except Exception:
                return False

    def request_resend_sms(self, activation_id: str) -> bool:
        try:
            self.set_status(activation_id, 3)
            return True
        except Exception:
            return False

    def wait_for_code(self, activation_id: str, *, timeout: int = 180, poll_interval: int = 3) -> dict | None:
        deadline = time.time() + timeout
        start = time.time()
        last_hero_resend = start
        openai_resent = False
        warned_v2 = False
        while time.time() < deadline:
            with _HERO_SMS_CACHE_LOCK:
                cache = _HERO_SMS_CACHE or {}
                used_codes = set(cache.get("used_codes") or [])
                attempted_sms_keys = set(cache.get("attempted_sms_keys") or [])

            for source in ("v2", "v1", "active"):
                try:
                    candidate = None
                    if source == "v2":
                        result = self.get_status_v2(activation_id)
                        if result.get("status") == "cancel":
                            return None
                        if result.get("status") == "ok":
                            candidate = result
                    elif source == "v1":
                        result = self.get_status(activation_id)
                        if result.get("status") == "cancel":
                            return None
                        if result.get("status") == "ok":
                            candidate = _make_sms_candidate(activation_id, "getStatus", result.get("code"))
                    else:
                        for item in self.get_active_activations():
                            if str(item.get("activationId")) == str(activation_id):
                                candidate = _make_sms_candidate(
                                    activation_id,
                                    "getActiveActivations",
                                    item.get("smsCode"),
                                    {
                                        "channel": "sms",
                                        "smsText": item.get("smsText"),
                                        "activationStatus": item.get("activationStatus"),
                                        "repeated": item.get("repeated"),
                                        "dateTime": item.get("dateTime"),
                                        "date": item.get("date") or item.get("smsDate") or item.get("smsTime"),
                                    },
                                )
                                break
                    if candidate and not _candidate_is_attempted(candidate, used_codes, attempted_sms_keys):
                        return candidate
                except Exception as exc:
                    if source == "v2" and not warned_v2:
                        logger.warning("HeroSMS getStatusV2 failed: %s", exc)
                        warned_v2 = True
                    else:
                        logger.debug("HeroSMS status check failed via %s: %s", source, exc)

            elapsed = time.time() - start
            if not openai_resent and elapsed >= 90 and self.openai_resend_callback:
                try:
                    self.openai_resend_callback()
                except Exception as exc:
                    logger.warning("OpenAI phone resend callback failed: %s", exc)
                self.request_resend_sms(activation_id)
                last_hero_resend = time.time()
                openai_resent = True
            elif time.time() - last_hero_resend >= 30:
                self.request_resend_sms(activation_id)
                last_hero_resend = time.time()

            time.sleep(poll_interval)
        return None

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        requested_timeout = max(1, int(timeout or 120))
        candidate = self.wait_for_code(activation_id, timeout=requested_timeout)
        self.last_code_result = candidate
        return str((candidate or {}).get("code") or "")

    def cancel(self, activation_id: str) -> bool:
        try:
            return self.cancel_activation(activation_id)
        finally:
            with _HERO_SMS_CACHE_LOCK:
                cache = _HERO_SMS_CACHE
                if cache and str(cache.get("activation_id")) == str(activation_id):
                    self._clear_cache()

    def report_success(self, activation_id: str) -> bool:
        should_finish = False
        should_clear_cache = False
        handled_cached_activation = False
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                handled_cached_activation = True
                cache["use_count"] = int(cache.get("use_count") or 0) + 1
                self._record_last_attempt(cache, failed=False)
                remaining = HERO_SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0))
                if not self.reuse_phone_to_max:
                    cache["reuse_stopped"] = True
                    cache["stop_reason"] = "reuse disabled"
                    should_finish = True
                    should_clear_cache = True
                elif self.phone_success_max > 0 and int(cache["use_count"]) >= self.phone_success_max:
                    cache["reuse_stopped"] = True
                    cache["stop_reason"] = f"success max reached ({self.phone_success_max})"
                    should_finish = True
                elif remaining <= 30:
                    cache["reuse_stopped"] = True
                    cache["stop_reason"] = "phone lifetime nearly expired"
                    should_finish = True
                    should_clear_cache = True
                self._save_cache(cache)
                if should_clear_cache:
                    self._clear_cache()
        if handled_cached_activation:
            if should_finish:
                self.finish_activation(activation_id)
            return True
        return self.finish_activation(activation_id)

    def _record_last_attempt(self, cache: dict, *, failed: bool) -> None:
        candidate = self.last_code_result or {}
        code = str(candidate.get("code") or "")
        sms_key = str(candidate.get("sms_key") or "")
        used_codes = set(cache.get("used_codes") or [])
        attempted_sms_keys = set(cache.get("attempted_sms_keys") or [])
        if code:
            used_codes.add(code)
        if sms_key:
            attempted_sms_keys.add(sms_key)
        cache["used_codes"] = used_codes
        cache["attempted_sms_keys"] = attempted_sms_keys
        if failed:
            cache["last_failed_reason"] = "invalid otp"

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                self._record_last_attempt(cache, failed=True)
                self._save_cache(cache)
        if self.openai_resend_callback:
            try:
                self.openai_resend_callback()
            except Exception:
                pass
        self.request_resend_sms(activation_id)

    def mark_send_succeeded(self, activation_id: str) -> None:
        try:
            self.set_status(activation_id, 1)
        except Exception:
            pass

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        reason_text = str(reason or "").lower()
        if any(keyword in reason_text for keyword in ("limit", "already", "too many", "exceeded", "maximum", "上限", "已达")):
            self._stop_reuse("phone limit reached")
        else:
            self._stop_reuse(reason or "phone rejected")
        # 一旦手机号被 OpenAI 拒接（反欺诈 / 限额 / VOIP 等），这个 activation 在服务端还占着费用；
        # SMS-Activate 风格的 set_status=8 是同步取消，不会冷却，此处直接 cancel，避免号泄漏。
        try:
            self.cancel(activation_id)
        except Exception as exc:
            try:
                logger.warning("%s mark_send_failed cancel failed: %s", self.__class__.__name__, exc)
            except Exception:
                pass

    def set_resend_callback(self, callback: Callable[[], None] | None) -> None:
        self.openai_resend_callback = callback

    def get_reuse_info(self) -> dict:
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE or self._load_cache(self.default_service, self.default_country) or {}
            if not cache:
                return {"alive": False}
            remaining = max(0, int(HERO_SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0))))
            return {
                "alive": remaining > 0 and not bool(cache.get("reuse_stopped")),
                "phone_number": cache.get("phone_number", ""),
                "use_count": int(cache.get("use_count") or 0),
                "remaining_seconds": remaining,
                "reuse_stopped": bool(cache.get("reuse_stopped")),
                "stop_reason": cache.get("stop_reason", ""),
            }


class SmsBowerProvider(HeroSmsProvider):
    """SMSBower provider — API 兼容 HeroSMS，仅 base URL 不同。"""

    BASE_URL = "https://smsbower.page/stubs/handler_api.php"
    DISPLAY_NAME = "SMSBower"

    def _request(self, params: dict, *, needs_key: bool = True, timeout: int = 30) -> requests.Response:
        payload = dict(params)
        if needs_key or self.api_key:
            payload["api_key"] = self.api_key
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = requests.get(self.BASE_URL, params=payload, timeout=timeout, proxies=self.proxies)
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= 2:
                    break
                time.sleep(0.6 * (attempt + 1))
        if last_exc:
            raise last_exc
        raise RuntimeError(f"{self.display_name} request failed")


class GrizzlySmsProvider(SmsBowerProvider):
    """GrizzlySMS provider using the SMS-Activate compatible API."""

    BASE_URL = "https://api.grizzlysms.com/stubs/handler_api.php"
    DISPLAY_NAME = "GrizzlySMS"


class SmsVerificationNumberProvider(SmsBowerProvider):
    """SMS Verification Number provider using the SMS-Activate compatible API."""

    BASE_URL = "https://sms-verification-number.com/stubs/handler_api"
    DISPLAY_NAME = "SMSVerificationNumber"
    DEFAULT_LANG = "en"

    def _request(self, params: dict, *, needs_key: bool = True, timeout: int = 30) -> requests.Response:
        payload = dict(params)
        if payload.get("action") in {"getPrices", "getServicesList"}:
            payload.setdefault("lang", self.DEFAULT_LANG)
        if needs_key or self.api_key:
            payload["api_key"] = self.api_key
        resp = requests.get(self.BASE_URL, params=payload, timeout=timeout, proxies=self.proxies)
        resp.raise_for_status()
        return resp

    def get_countries(self) -> list:
        try:
            data = self._request({"action": "getCountryAndOperators", "lang": self.DEFAULT_LANG}).json()
        except Exception:
            return super().get_countries()
        if not isinstance(data, list):
            return super().get_countries()
        rows = []
        for item in data:
            if not isinstance(item, dict):
                continue
            country_id = str(item.get("id") or item.get("countryId") or "").strip()
            name = str(item.get("name") or item.get("eng") or item.get("chn") or country_id).strip()
            if not country_id or not name:
                continue
            rows.append(
                {
                    "id": country_id,
                    "name": name,
                    "eng": name,
                    "chn": name,
                    "operators": item.get("operators") or {},
                    "raw": item,
                }
            )
        return rows


REMOTE_SMS_POLL_INTERVAL = 5
SMSPOOL_DEFAULT_BASE_URL = "https://api.smspool.net"
SMSPOOL_DEFAULT_COMPAT_BASE_URL = "https://api.smspool.net/stubs/handler_api.php?setting=smspool"
SMSPOOL_DEFAULT_SERVICE = "671"
SMSPOOL_DEFAULT_COUNTRY = "1"
FIVE_SIM_DEFAULT_BASE_URL = "https://5sim.net"
FIVE_SIM_DEFAULT_PRODUCT = "openai"
FIVE_SIM_DEFAULT_COUNTRY = "vietnam"
FIVE_SIM_DEFAULT_OPERATOR = "any"
NEXSMS_DEFAULT_BASE_URL = "https://api.nexsms.net"
NEXSMS_DEFAULT_SERVICE = "ot"
GENERIC_CHATGPT_SERVICE_NAMES = {"", "default", "chatgpt", "openai", "cursor", "gpt", "gptplus", "dr", "ot"}


def _remote_sms_base_url(value: str, default: str) -> str:
    raw = str(value or "").strip() or default
    return raw.rstrip("/")


def _remote_sms_join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{str(path or '').lstrip('/')}"


def _remote_sms_query_url(base_url: str, params: dict) -> str:
    parsed = urlsplit(str(base_url or ""))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in (params or {}).items():
        if value not in (None, ""):
            query[str(key)] = str(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _remote_sms_payload(text: str):
    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _remote_sms_describe(payload) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, dict):
        for key in ("message", "msg", "error", "title", "statusText", "status"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value).strip()
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return str(payload)
    if isinstance(payload, list):
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return str(payload)
    return str(payload or "").strip()


def _remote_sms_success(payload) -> bool:
    if isinstance(payload, str):
        text = payload.strip().lower()
        return text.startswith(("access_", "success", "ok"))
    if not isinstance(payload, dict):
        return False
    for key in ("success", "ok"):
        if key in payload:
            value = payload.get(key)
            return value is True or str(value).strip().lower() in {"1", "true", "ok", "success"}
    if "code" in payload:
        try:
            return int(payload.get("code")) == 0
        except (TypeError, ValueError):
            return False
    status = str(payload.get("status") or "").strip().lower()
    return status in {"ok", "success", "access", "ready"}


def _remote_sms_error_is_waiting(payload) -> bool:
    text = _remote_sms_describe(payload).lower()
    return bool(re.search(r"wait|pending|no\s*sms|no\s*code|not\s*arrived|empty", text))


def _remote_sms_error_is_terminal(payload) -> bool:
    text = _remote_sms_describe(payload).lower()
    return bool(re.search(r"cancel|expired|timeout|closed|banned|finished|invalid|not\s*found", text))


def _remote_sms_normalize_phone(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("+"):
        return text
    digits = re.sub(r"\D+", "", text)
    return f"+{digits}" if digits else text


def _remote_sms_code(payload) -> str:
    try:
        code = extract_codex_sms_verification_code(payload)
        if code:
            return code
    except Exception:
        pass
    match = re.search(r"\b(\d{4,8})\b", _remote_sms_describe(payload))
    return match.group(1) if match else ""


def _remote_sms_first_dict(payload) -> dict:
    if isinstance(payload, dict):
        for key in ("data", "result", "response", "order", "activation"):
            child = payload.get(key)
            if isinstance(child, dict):
                return child
        return payload
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                return item
    return {}


def _remote_sms_service_code(service: str, default: str) -> str:
    raw = str(service or "").strip()
    if raw.lower() in GENERIC_CHATGPT_SERVICE_NAMES:
        return str(default or "").strip()
    return raw or str(default or "").strip()


class SmsPoolProvider(BaseSmsProvider):
    """SMSPool native API provider."""

    auto_report_success_on_code = False

    def __init__(
        self,
        api_key: str,
        *,
        default_service: str = SMSPOOL_DEFAULT_SERVICE,
        default_country: str = SMSPOOL_DEFAULT_COUNTRY,
        base_url: str = SMSPOOL_DEFAULT_BASE_URL,
        compat_base_url: str = SMSPOOL_DEFAULT_COMPAT_BASE_URL,
        max_price: float = -1,
        proxy: str | None = None,
        poll_interval: int = REMOTE_SMS_POLL_INTERVAL,
    ):
        self.api_key = str(api_key or "").strip()
        self.default_service = str(default_service or SMSPOOL_DEFAULT_SERVICE).strip()
        self.default_country = str(default_country or SMSPOOL_DEFAULT_COUNTRY).strip()
        self.base_url = _remote_sms_base_url(base_url, SMSPOOL_DEFAULT_BASE_URL)
        self.compat_base_url = str(compat_base_url or SMSPOOL_DEFAULT_COMPAT_BASE_URL).strip()
        self.max_price = float(max_price or -1)
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.poll_interval = max(1, int(poll_interval or REMOTE_SMS_POLL_INTERVAL))
        self._ignored_codes: dict[str, set[str]] = {}
        self._last_codes: dict[str, str] = {}
        self._activation_phones: dict[str, str] = {}

    def _post_form(self, path: str, data: dict, *, action_label: str) -> Any:
        if not self.api_key:
            raise RuntimeError("SMSPool API Key is not configured")
        body = dict(data or {})
        body.setdefault("key", self.api_key)
        response = requests.post(
            _remote_sms_join_url(self.base_url, path),
            data=body,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
            timeout=20,
            proxies=self.proxies,
        )
        payload = _remote_sms_payload(response.text)
        if not response.ok:
            raise RuntimeError(f"{action_label} failed: {_remote_sms_describe(payload) or response.status_code}")
        return payload

    def _compat_get(self, params: dict, *, action_label: str) -> Any:
        if not self.api_key:
            raise RuntimeError("SMSPool API Key is not configured")
        query = {"api_key": self.api_key, **dict(params or {})}
        response = requests.get(
            _remote_sms_query_url(self.compat_base_url, query),
            timeout=20,
            proxies=self.proxies,
        )
        payload = _remote_sms_payload(response.text)
        if not response.ok:
            raise RuntimeError(f"{action_label} failed: {_remote_sms_describe(payload) or response.status_code}")
        return payload

    def _normalize_activation(self, payload, *, service_code: str, country_code: str) -> SmsActivation | None:
        record = _remote_sms_first_dict(payload)
        activation_id = str(
            record.get("activationId")
            or record.get("orderid")
            or record.get("order_id")
            or record.get("orderCode")
            or record.get("id")
            or ""
        ).strip()
        phone_number = _remote_sms_normalize_phone(
            record.get("phoneNumber")
            or record.get("phonenumber")
            or record.get("number")
            or record.get("phone")
            or ""
        )
        if not activation_id or not phone_number:
            return None
        return SmsActivation(
            activation_id=activation_id,
            phone_number=phone_number,
            country=country_code,
            metadata={
                "provider": "smspool",
                "service": service_code,
                "country": country_code,
                "number_info": record,
                "activation_cost": record.get("cost") or record.get("price"),
                "max_price": self.max_price if self.max_price > 0 else "",
            },
        )

    def get_balance(self) -> float:
        payload = self._post_form("/request/balance", {}, action_label="SMSPool balance")
        record = _remote_sms_first_dict(payload)
        value = record.get("balance") if isinstance(record, dict) else None
        if value in (None, "") and isinstance(payload, dict):
            value = payload.get("balance")
        return float(value or 0)

    def get_current_price_info(self, *, service: str, country: str = "") -> dict:
        service_code = _remote_sms_service_code(service, self.default_service)
        country_code = str(country or self.default_country or "").strip()
        try:
            payload = self._compat_get(
                {"action": "getPrices", "service": service_code, "country": country_code},
                action_label="SMSPool getPrices",
            )
        except Exception:
            return {}
        info = _extract_price_info_from_services(payload, service=service_code, country=country_code)
        if info:
            info.update({"service": service_code, "country": country_code})
        return info

    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        from platforms.gopay.sms_channel import (
            get_smspool_release_queue_size,
            is_smspool_insufficient_balance_response,
            wait_for_smspool_release_queue_drain,
        )

        service_code = _remote_sms_service_code(service, self.default_service)
        country_code = str(country or self.default_country or SMSPOOL_DEFAULT_COUNTRY).strip()
        purchase_body = {"country": country_code, "service": service_code, "quantity": 1}
        if self.max_price > 0:
            purchase_body["max_price"] = self.max_price
        while True:
            try:
                payload = self._post_form(
                    "/purchase/sms",
                    purchase_body,
                    action_label="SMSPool purchase",
                )
            except Exception as exc:
                if (
                    get_smspool_release_queue_size(api_key=self.api_key, base_url=self.base_url) > 0
                    and is_smspool_insufficient_balance_response(str(exc))
                ):
                    logger.warning(
                        "SMSPool purchase balance error while release queue is pending; waiting for release queue"
                    )
                    wait_for_smspool_release_queue_drain(
                        api_key=self.api_key,
                        base_url=self.base_url,
                        log_fn=logger.warning,
                    )
                    continue
                raise
            if (
                get_smspool_release_queue_size(api_key=self.api_key, base_url=self.base_url) > 0
                and is_smspool_insufficient_balance_response(payload)
            ):
                logger.warning(
                    "SMSPool purchase returned balance error while release queue is pending; waiting for release queue"
                )
                wait_for_smspool_release_queue_drain(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    log_fn=logger.warning,
                )
                continue
            break
        activation = self._normalize_activation(payload, service_code=service_code, country_code=country_code)
        if not activation:
            raise RuntimeError(f"SMSPool purchase returned unusable response: {_remote_sms_describe(payload)}")
        self._activation_phones[activation.activation_id] = activation.phone_number
        return activation

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        activation_key = str(activation_id or "").strip()
        ignored = self._ignored_codes.setdefault(activation_key, set())
        deadline = time.time() + max(1, int(timeout or 120))
        while time.time() < deadline:
            payload = self._post_form(
                "/sms/check",
                {"orderid": activation_key},
                action_label="SMSPool check",
            )
            code = _remote_sms_code(payload)
            if code and code not in ignored:
                self._last_codes[activation_key] = code
                return code
            if _remote_sms_error_is_terminal(payload):
                return ""
            time.sleep(self.poll_interval)
        self.cancel(activation_key)
        return ""

    def cancel(self, activation_id: str) -> bool:
        from platforms.gopay.sms_channel import enqueue_smspool_release_retry, remove_smspool_release

        activation_key = str(activation_id or "").strip()
        try:
            payload = self._post_form("/sms/cancel", {"orderid": activation_key}, action_label="SMSPool cancel")
            ok = _remote_sms_success(payload)
            if ok:
                remove_smspool_release(activation_key)
                return True
            enqueue_smspool_release_retry(
                api_key=self.api_key,
                base_url=self.base_url,
                order_id=activation_key,
                phone=self._activation_phones.get(activation_key, ""),
                reason="cancel_failed",
                last_response=payload if isinstance(payload, dict) else {"raw": _remote_sms_describe(payload)},
            )
            return False
        except Exception as exc:
            logger.warning("SMSPool cancel failed: %s", exc)
            enqueue_smspool_release_retry(
                api_key=self.api_key,
                base_url=self.base_url,
                order_id=activation_key,
                phone=self._activation_phones.get(activation_key, ""),
                reason="cancel_exception",
                last_response={"error": str(exc)[:300]},
            )
            return False

    def report_success(self, activation_id: str) -> bool:
        return True

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        activation_key = str(activation_id or "").strip()
        last_code = self._last_codes.get(activation_key)
        if last_code:
            self._ignored_codes.setdefault(activation_key, set()).add(last_code)
        try:
            probe = self._post_form("/sms/check_resend", {"orderid": activation_key}, action_label="SMSPool check resend")
            if _remote_sms_success(probe):
                self._post_form("/sms/resend", {"orderid": activation_key}, action_label="SMSPool resend")
        except Exception:
            return None

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        self.cancel(activation_id)

    def get_countries(self) -> list:
        response = requests.post(
            _remote_sms_join_url(self.base_url, "/country/retrieve_all"),
            headers={"Accept": "application/json,text/plain,*/*"},
            timeout=20,
            proxies=self.proxies,
        )
        payload = _remote_sms_payload(response.text)
        if not response.ok:
            raise RuntimeError(f"SMSPool countries failed: {_remote_sms_describe(payload) or response.status_code}")
        if not isinstance(payload, list):
            return []
        rows = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            country_id = str(item.get("ID") or item.get("id") or item.get("country_id") or "").strip()
            name = str(item.get("name") or item.get("short_name") or item.get("shortName") or country_id).strip()
            if not country_id or not name:
                continue
            rows.append(
                {
                    "id": country_id,
                    "name": name,
                    "eng": name,
                    "chn": name,
                    "cc": str(item.get("cc") or "").strip(),
                    "raw": item,
                }
            )
        return rows

    def get_services(self, country: str | int | None = None) -> list:
        try:
            response = requests.post(
                _remote_sms_join_url(self.base_url, "/service/retrieve_all"),
                headers={"Accept": "application/json,text/plain,*/*"},
                timeout=20,
                proxies=self.proxies,
            )
            payload = _remote_sms_payload(response.text)
            if response.ok and isinstance(payload, list):
                rows = []
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    code = str(
                        item.get("ID")
                        or item.get("id")
                        or item.get("service_code")
                        or item.get("code")
                        or ""
                    ).strip()
                    name = str(item.get("name") or item.get("service") or item.get("title") or code).strip()
                    if code:
                        rows.append({"code": code, "name": name or code, "raw": item})
                if rows:
                    return rows
        except Exception:
            pass
        return [{"code": SMSPOOL_DEFAULT_SERVICE, "name": "OpenAI / ChatGPT"}]

    def get_top_countries(self, service: str | None = None) -> list[dict]:
        """SMSPool country ranking by price for the given service.

        Uses the compat getPrices endpoint which returns
        {country_id: {operator: {cost, count}}} for the service.
        Picks the cheapest operator per country and sorts ascending.
        """
        service_code = _remote_sms_service_code(service, self.default_service)
        try:
            payload = self._compat_get(
                {"action": "getPrices", "service": service_code},
                action_label="SMSPool getPrices",
            )
        except Exception:
            return []
        rows: list[dict[str, Any]] = []
        if not isinstance(payload, dict):
            return []
        for country_id, operators in payload.items():
            if not isinstance(operators, dict):
                continue
            best_price = None
            best_stock = 0
            for _op, info in operators.items():
                if not isinstance(info, dict):
                    continue
                cost = info.get("cost")
                count = info.get("count") or 0
                try:
                    cost_f = float(cost) if cost is not None else None
                except (TypeError, ValueError):
                    cost_f = None
                try:
                    count_i = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count_i = 0
                if cost_f is None or cost_f <= 0:
                    continue
                if best_price is None or cost_f < best_price:
                    best_price = cost_f
                    best_stock = count_i
            if best_price is not None:
                rows.append({"country": str(country_id), "price": best_price, "count": best_stock})
        rows.sort(key=lambda item: (item.get("price") or 999999.0, -(item.get("count") or 0)))
        return rows


class FiveSimProvider(BaseSmsProvider):
    """5sim native API provider."""

    def __init__(
        self,
        api_key: str,
        *,
        country: str = FIVE_SIM_DEFAULT_COUNTRY,
        operator: str = FIVE_SIM_DEFAULT_OPERATOR,
        product: str = FIVE_SIM_DEFAULT_PRODUCT,
        base_url: str = FIVE_SIM_DEFAULT_BASE_URL,
        max_price: float = -1,
        proxy: str | None = None,
        reuse: bool = True,
        poll_interval: int = REMOTE_SMS_POLL_INTERVAL,
    ):
        self.api_key = str(api_key or "").strip()
        self.country = self._normalize_slug(country or FIVE_SIM_DEFAULT_COUNTRY)
        self.operator = self._normalize_slug(operator or FIVE_SIM_DEFAULT_OPERATOR)
        self.product = self._normalize_slug(product or FIVE_SIM_DEFAULT_PRODUCT)
        self.base_url = _remote_sms_base_url(base_url, FIVE_SIM_DEFAULT_BASE_URL)
        self.max_price = float(max_price or -1)
        self.reuse = bool(reuse)
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.poll_interval = max(1, int(poll_interval or REMOTE_SMS_POLL_INTERVAL))

    @staticmethod
    def _normalize_slug(value: str) -> str:
        return re.sub(r"[^a-z0-9_-]+", "", str(value or "").strip().lower())

    def _request(self, path: str, *, query: dict | None = None, require_auth: bool = True) -> Any:
        if require_auth and not self.api_key:
            raise RuntimeError("5sim API Key is not configured")
        headers = {"Accept": "application/json"}
        if require_auth:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = requests.get(
            _remote_sms_query_url(_remote_sms_join_url(self.base_url, path), query or {}),
            headers=headers,
            timeout=20,
            proxies=self.proxies,
        )
        payload = _remote_sms_payload(response.text)
        if not response.ok:
            raise RuntimeError(f"5sim request failed: {_remote_sms_describe(payload) or response.status_code}")
        return payload

    def _product_for(self, service: str) -> str:
        return _remote_sms_service_code(service, self.product) or FIVE_SIM_DEFAULT_PRODUCT

    def get_balance(self) -> float:
        payload = self._request("/v1/user/profile")
        return float((payload or {}).get("balance") or 0) if isinstance(payload, dict) else 0.0

    def get_current_price_info(self, *, service: str, country: str = "") -> dict:
        country_slug = self._normalize_slug(country or self.country or FIVE_SIM_DEFAULT_COUNTRY)
        product = self._product_for(service)
        try:
            payload = self._request(
                f"/v1/guest/products/{country_slug}/{self.operator or FIVE_SIM_DEFAULT_OPERATOR}",
                require_auth=False,
            )
        except Exception:
            return {}
        product_info = payload.get(product) if isinstance(payload, dict) else None
        if not isinstance(product_info, dict):
            return {}
        price = _maybe_float(product_info.get("Price") or product_info.get("price") or product_info.get("cost"))
        count = _maybe_int(product_info.get("Qty") or product_info.get("qty") or product_info.get("count"))
        result = {"price": price, "count": count, "currency": "USD", "raw": product_info}
        result.update({"service": product, "country": country_slug})
        return result

    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        country_slug = self._normalize_slug(country or self.country or FIVE_SIM_DEFAULT_COUNTRY)
        operator = self.operator or FIVE_SIM_DEFAULT_OPERATOR
        product = self._product_for(service)
        query = {}
        if self.max_price > 0:
            query["maxPrice"] = _format_number_for_log(self.max_price)
        if self.reuse:
            query["reuse"] = "1"
        payload = self._request(f"/v1/user/buy/activation/{country_slug}/{operator}/{product}", query=query)
        record = _remote_sms_first_dict(payload)
        activation_id = str(record.get("id") or record.get("activationId") or "").strip()
        phone_number = _remote_sms_normalize_phone(record.get("phone") or record.get("phoneNumber") or "")
        if not activation_id or not phone_number:
            detail = _remote_sms_describe(payload)
            raise RuntimeError(
                "5sim purchase returned unusable response: "
                f"{detail}; country={country_slug}, operator={operator}, "
                f"product={product}, maxPrice={self.max_price if self.max_price > 0 else 'unlimited'}"
            )
        return SmsActivation(
            activation_id=activation_id,
            phone_number=phone_number,
            country=country_slug,
            metadata={
                "provider": "5sim",
                "service": product,
                "country": country_slug,
                "number_info": record,
                "activation_cost": record.get("price"),
                "max_price": self.max_price if self.max_price > 0 else "",
            },
        )

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        deadline = time.time() + max(1, int(timeout or 120))
        while time.time() < deadline:
            payload = self._request(f"/v1/user/check/{activation_id}")
            code = _remote_sms_code(payload)
            if code:
                return code
            status = str((payload or {}).get("status") or "").strip().upper() if isinstance(payload, dict) else ""
            if status in {"CANCELED", "BANNED", "FINISHED", "TIMEOUT"}:
                return ""
            time.sleep(self.poll_interval)
        self.cancel(activation_id)
        return ""

    def cancel(self, activation_id: str) -> bool:
        try:
            self._request(f"/v1/user/cancel/{activation_id}")
            return True
        except Exception as exc:
            logger.warning("5sim cancel failed: %s", exc)
            return False

    def report_success(self, activation_id: str) -> bool:
        try:
            self._request(f"/v1/user/finish/{activation_id}")
            return True
        except Exception:
            return False

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        self.cancel(activation_id)

    def get_countries(self) -> list:
        payload = self._request("/v1/guest/countries", require_auth=False)
        if not isinstance(payload, dict):
            return []
        rows = []
        for slug, item in payload.items():
            country_id = self._normalize_slug(slug)
            if not country_id:
                continue
            item = item if isinstance(item, dict) else {}
            name = str(item.get("text_en") or item.get("name") or slug).strip() or country_id
            rows.append(
                {
                    "id": country_id,
                    "name": name,
                    "eng": name,
                    "chn": name,
                    "raw": item,
                }
            )
        rows.sort(key=lambda item: str(item.get("name") or ""))
        return rows

    def get_services(self, country: str | int | None = None) -> list:
        country_slug = self._normalize_slug(country or self.country or FIVE_SIM_DEFAULT_COUNTRY)
        try:
            payload = self._request(
                f"/v1/guest/products/{country_slug}/{self.operator or FIVE_SIM_DEFAULT_OPERATOR}",
                require_auth=False,
            )
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return [{"code": FIVE_SIM_DEFAULT_PRODUCT, "name": FIVE_SIM_DEFAULT_PRODUCT}]
        rows = []
        for code, item in payload.items():
            service_code = self._normalize_slug(code)
            if not service_code:
                continue
            item = item if isinstance(item, dict) else {}
            name = str(item.get("Name") or item.get("name") or service_code).strip() or service_code
            rows.append({"code": service_code, "name": name, "raw": item})
        return rows or [{"code": FIVE_SIM_DEFAULT_PRODUCT, "name": FIVE_SIM_DEFAULT_PRODUCT}]


class NexSmsProvider(BaseSmsProvider):
    """NexSMS native API provider."""

    def __init__(
        self,
        api_key: str,
        *,
        country_order: str | list | tuple = "",
        service_code: str = NEXSMS_DEFAULT_SERVICE,
        base_url: str = NEXSMS_DEFAULT_BASE_URL,
        max_price: float = -1,
        proxy: str | None = None,
        poll_interval: int = REMOTE_SMS_POLL_INTERVAL,
    ):
        self.api_key = str(api_key or "").strip()
        self.country_order = self._normalize_country_order(country_order)
        self.service_code = self._normalize_service_code(service_code)
        self.base_url = _remote_sms_base_url(base_url, NEXSMS_DEFAULT_BASE_URL)
        self.max_price = float(max_price or -1)
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.poll_interval = max(1, int(poll_interval or REMOTE_SMS_POLL_INTERVAL))

    @staticmethod
    def _normalize_service_code(value: str) -> str:
        return re.sub(r"[^a-z0-9_-]+", "", str(value or "").strip().lower()) or NEXSMS_DEFAULT_SERVICE

    @staticmethod
    def _normalize_country_order(value) -> list[int]:
        source = value if isinstance(value, (list, tuple)) else re.split(r"[\s,;|]+", str(value or ""))
        countries: list[int] = []
        seen: set[int] = set()
        for item in source:
            try:
                country_id = int(float(str(item).strip()))
            except (TypeError, ValueError):
                continue
            if country_id < 0 or country_id in seen:
                continue
            seen.add(country_id)
            countries.append(country_id)
        return countries

    def _request(self, path: str, *, method: str = "GET", query: dict | None = None, body: dict | None = None) -> Any:
        if not self.api_key:
            raise RuntimeError("NexSMS API Key is not configured")
        url = _remote_sms_query_url(
            _remote_sms_join_url(self.base_url, path),
            {"apiKey": self.api_key, **dict(query or {})},
        )
        method = method.upper()
        response = requests.request(
            method,
            url,
            json=body if method not in {"GET", "HEAD"} else None,
            headers={"Accept": "application/json"},
            timeout=20,
            proxies=self.proxies,
        )
        payload = _remote_sms_payload(response.text)
        if not response.ok:
            raise RuntimeError(f"NexSMS request failed: {_remote_sms_describe(payload) or response.status_code}")
        return payload

    def _price_candidates(self, country_id: int) -> list[float | None]:
        try:
            payload = self._request(
                "/api/getCountryByService",
                query={"serviceCode": self.service_code, "countryId": country_id},
            )
        except Exception:
            return [None]
        data = payload.get("data") if isinstance(payload, dict) else {}
        values = []
        if isinstance(data, dict):
            for key in ("minPrice", "medianPrice", "maxPrice"):
                value = _maybe_float(data.get(key))
                if value is not None:
                    values.append(value)
            price_map = data.get("priceMap")
            if isinstance(price_map, dict):
                for price_key, count in price_map.items():
                    price = _maybe_float(price_key)
                    available = _maybe_int(count)
                    if price is not None and (available is None or available > 0):
                        values.append(price)
        if self.max_price > 0:
            values = [value for value in values if value <= self.max_price]
        values = sorted(set(round(value, 4) for value in values if value and value > 0))
        return values or [None]

    def get_current_price_info(self, *, service: str, country: str = "") -> dict:
        try:
            country_id = int(float(str(country or (self.country_order[0] if self.country_order else 0))))
        except (TypeError, ValueError):
            return {}
        if country_id < 0:
            return {}
        prices = [value for value in self._price_candidates(country_id) if value is not None]
        if not prices:
            return {}
        return {
            "price": prices[0],
            "count": None,
            "currency": "USD",
            "service": self.service_code,
            "country": str(country_id),
            "raw": {"prices": prices},
        }

    def _normalize_activation(self, payload, *, country_id: int, price: float | None) -> SmsActivation | None:
        record = _remote_sms_first_dict(payload)
        data = record.get("data") if isinstance(record.get("data"), dict) else record
        candidates = data.get("phoneNumbers") if isinstance(data, dict) else None
        if not isinstance(candidates, list):
            candidates = data.get("numbers") if isinstance(data, dict) and isinstance(data.get("numbers"), list) else []
        phone_number = _remote_sms_normalize_phone(
            (data or {}).get("phoneNumber")
            or (data or {}).get("phone")
            or (candidates[0] if candidates else "")
        )
        if not phone_number:
            return None
        return SmsActivation(
            activation_id=phone_number,
            phone_number=phone_number,
            country=str(country_id),
            metadata={
                "provider": "nexsms",
                "service": self.service_code,
                "country": str(country_id),
                "number_info": data,
                "activation_cost": price,
                "max_price": self.max_price if self.max_price > 0 else "",
            },
        )

    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        service_code = _remote_sms_service_code(service, self.service_code)
        if service_code:
            self.service_code = self._normalize_service_code(service_code)
        countries = self._normalize_country_order(country) if country else list(self.country_order)
        if not countries:
            raise RuntimeError("NexSMS country order is not configured")
        last_error = ""
        for country_id in countries:
            for price in self._price_candidates(country_id):
                body = {"serviceCode": self.service_code, "countryId": country_id, "quantity": 1}
                if price is not None:
                    body["price"] = price
                try:
                    payload = self._request("/api/order/purchase", method="POST", body=body)
                    if not _remote_sms_success(payload):
                        last_error = _remote_sms_describe(payload)
                        continue
                    activation = self._normalize_activation(payload, country_id=country_id, price=price)
                    if activation:
                        return activation
                    last_error = _remote_sms_describe(payload)
                except Exception as exc:
                    last_error = str(exc)
        raise RuntimeError(f"NexSMS purchase failed: {last_error or 'no available number'}")

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        deadline = time.time() + max(1, int(timeout or 120))
        while time.time() < deadline:
            payload = self._request(
                "/api/sms/messages",
                query={"phoneNumber": activation_id, "format": "json_latest"},
            )
            if _remote_sms_success(payload):
                code = _remote_sms_code(payload.get("data") if isinstance(payload, dict) else payload)
                if code:
                    return code
            elif not _remote_sms_error_is_waiting(payload) and _remote_sms_error_is_terminal(payload):
                return ""
            time.sleep(self.poll_interval)
        self.cancel(activation_id)
        return ""

    def cancel(self, activation_id: str) -> bool:
        try:
            payload = self._request(
                "/api/close/activation",
                method="POST",
                body={"phoneNumber": activation_id},
            )
            return _remote_sms_success(payload)
        except Exception as exc:
            logger.warning("NexSMS cancel failed: %s", exc)
            return False

    def report_success(self, activation_id: str) -> bool:
        return True

    def get_countries(self) -> list:
        payload = self._request("/api/countries")
        data = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(data, list):
            return []
        rows = []
        for item in data:
            if not isinstance(item, dict):
                continue
            country_id = str(item.get("id") or item.get("countryId") or item.get("country_id") or "").strip()
            name = str(item.get("name") or item.get("countryName") or item.get("title") or country_id).strip()
            if not country_id or not name:
                continue
            rows.append(
                {
                    "id": country_id,
                    "name": name,
                    "eng": name,
                    "chn": name,
                    "raw": item,
                }
            )
        rows.sort(key=lambda item: str(item.get("name") or ""))
        return rows

    def get_services(self, country: str | int | None = None) -> list:
        try:
            payload = self._request("/api/services")
            data = payload.get("data") if isinstance(payload, dict) else payload
            if isinstance(data, list):
                rows = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    code = str(item.get("code") or item.get("serviceCode") or item.get("id") or "").strip()
                    name = str(item.get("name") or item.get("title") or code).strip()
                    if code:
                        rows.append({"code": code, "name": name or code, "raw": item})
                if rows:
                    return rows
        except Exception:
            pass
        return [{"code": self.service_code or NEXSMS_DEFAULT_SERVICE, "name": self.service_code or NEXSMS_DEFAULT_SERVICE}]


# ---------------------------------------------------------------------------
# Codex local SMS pool implementation
# ---------------------------------------------------------------------------

CODEX_SMS_POOL_SEPARATOR = "----"
CODEX_SMS_POOL_POLL_INTERVAL = 5
CODEX_SMS_POOL_REQUEST_TIMEOUT = 20
CODEX_SMS_POOL_BLOCKED_PREFIX = "CODEX_SMS_POOL_BLOCKED"
CODEX_SMS_POOL_EXHAUSTED = "CODEX_SMS_POOL_EXHAUSTED"
_CODEX_SMS_POOL_STATE_LOCK = threading.Lock()
_CODEX_SMS_CODE_CONTEXT_PATTERN = re.compile(
    r"(?:verification\s*code|one[-\s]?time\s*(?:passcode|code)|passcode|otp|code|验证码|安全码)"
    r"[\s\S]{0,50}?((?:\d[\s-]?){4,8})"
    r"|((?:\d[\s-]?){4,8})[\s\S]{0,50}?"
    r"(?:verification\s*code|one[-\s]?time\s*(?:passcode|code)|passcode|otp|code|验证码|安全码)",
    re.IGNORECASE,
)
_CODEX_SMS_CODE_EXACT_PATTERN = re.compile(r"^\D*((?:\d[\s-]?){4,8})\D*$")
_CODEX_SMS_TRUSTED_TEXT_KEY_PATTERN = re.compile(
    r"^(sms|message|msg|text|content|body|code|otp|verification_code|verificationCode)$",
    re.IGNORECASE,
)
_CODEX_SMS_METADATA_KEY_PATTERN = re.compile(
    r"(^|[_-])(phone|mobile|tel|id|order|time|date|expired|expire|status|url)([_-]|$)",
    re.IGNORECASE,
)


def _normalize_codex_sms_text(value: str = "") -> str:
    return "\n".join(line.strip() for line in str(value or "").replace("\r", "").split("\n") if line.strip())


def _normalize_codex_sms_phone(value: str = "") -> tuple[str, str]:
    raw_value = str(value or "").strip()
    digits = re.sub(r"\D+", "", raw_value)
    if not digits:
        return raw_value, raw_value
    return digits, f"+{digits}"


def _normalize_codex_sms_url(value: str = "") -> str:
    raw_value = str(value or "").strip()
    if not raw_value:
        return ""
    try:
        parsed = urlsplit(raw_value)
        query = [
            (key, val)
            for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() != "t"
        ]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    except Exception:
        return re.sub(r"([?&])t=\d+(?=(&|$))", r"\1", raw_value, flags=re.IGNORECASE).rstrip("?&")


def _build_codex_sms_pool_key(phone: str, verification_url: str) -> str:
    normalized_phone, _ = _normalize_codex_sms_phone(phone)
    normalized_url = _normalize_codex_sms_url(verification_url)
    if not normalized_phone or not normalized_url:
        return ""
    return f"{normalized_phone}{CODEX_SMS_POOL_SEPARATOR}{normalized_url}"


def parse_codex_sms_pool_key(value: str = "") -> CodexSmsPoolEntry | None:
    normalized = str(value or "").strip()
    separator_index = normalized.find(CODEX_SMS_POOL_SEPARATOR)
    if separator_index <= 0:
        return None
    phone_raw = normalized[:separator_index]
    url_raw = normalized[separator_index + len(CODEX_SMS_POOL_SEPARATOR):]
    phone, phone_e164 = _normalize_codex_sms_phone(phone_raw)
    verification_url = _normalize_codex_sms_url(url_raw)
    key = _build_codex_sms_pool_key(phone, verification_url)
    if not phone or not verification_url or not key:
        return None
    return CodexSmsPoolEntry(
        index=0,
        key=key,
        phone=phone,
        phone_e164=phone_e164,
        verification_url=verification_url,
    )


def parse_codex_sms_pool_entries(text: str = "") -> list[CodexSmsPoolEntry]:
    """解析本地接码池文本。

    兼容用户输入的 ``+手机号|取码链接``，也兼容 GuJumpgate 旧格式
    ``手机号----取码链接``。若分成两行（号码一行、链接一行）也会识别。
    """
    lines = _normalize_codex_sms_text(text).split("\n")
    seen: set[str] = set()
    entries: list[CodexSmsPoolEntry] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        separator = ""
        separator_index = -1
        for candidate in ("|", CODEX_SMS_POOL_SEPARATOR):
            candidate_index = line.find(candidate)
            if candidate_index > 0 and (separator_index < 0 or candidate_index < separator_index):
                separator = candidate
                separator_index = candidate_index

        if separator:
            phone_raw = line[:separator_index]
            url_raw = line[separator_index + len(separator):]
        else:
            phone_raw = line
            url_raw = lines[index + 1] if index + 1 < len(lines) else ""
            if url_raw:
                index += 1

        phone, phone_e164 = _normalize_codex_sms_phone(phone_raw)
        verification_url = _normalize_codex_sms_url(url_raw)
        key = _build_codex_sms_pool_key(phone, verification_url)
        if phone and verification_url and key and key not in seen:
            seen.add(key)
            entries.append(
                CodexSmsPoolEntry(
                    index=len(entries),
                    key=key,
                    phone=phone,
                    phone_e164=phone_e164,
                    verification_url=verification_url,
                )
            )
        index += 1
    return entries


def _parse_codex_sms_payload_text(text: str):
    raw_text = str(text or "")
    try:
        return json.loads(raw_text) if raw_text else {}
    except Exception:
        return raw_text


def _collect_codex_sms_payload_candidates(value, path: str = "", seen: set[int] | None = None) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return [{"key": path.split(".")[-1] if path else "", "path": path, "text": text}] if text else []
    if not isinstance(value, (dict, list, tuple)):
        return []
    seen = seen or set()
    identity = id(value)
    if identity in seen:
        return []
    seen.add(identity)
    if isinstance(value, (list, tuple)):
        result: list[dict] = []
        for item_index, item in enumerate(value):
            result.extend(_collect_codex_sms_payload_candidates(item, f"{path}[{item_index}]", seen))
        return result
    result = []
    for key, child in value.items():
        child_path = f"{path}.{key}" if path else str(key)
        result.extend(_collect_codex_sms_payload_candidates(child, child_path, seen))
    return result


def _clean_codex_sms_code(value: str = "") -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    return digits if 4 <= len(digits) <= 8 else ""


def extract_codex_sms_verification_code(payload) -> str:
    """从本地取码接口响应中提取验证码。"""
    candidates = _collect_codex_sms_payload_candidates(payload)

    for candidate in candidates:
        match = _CODEX_SMS_CODE_CONTEXT_PATTERN.search(str(candidate.get("text") or ""))
        if match:
            code = _clean_codex_sms_code(match.group(1) or match.group(2) or "")
            if code:
                return code

    for candidate in candidates:
        key = str(candidate.get("key") or "")
        path = str(candidate.get("path") or "")
        text = str(candidate.get("text") or "")
        is_root_text = not path
        if not is_root_text:
            if not _CODEX_SMS_TRUSTED_TEXT_KEY_PATTERN.search(key):
                continue
            if _CODEX_SMS_METADATA_KEY_PATTERN.search(key) or _CODEX_SMS_METADATA_KEY_PATTERN.search(path):
                continue
        match = _CODEX_SMS_CODE_EXACT_PATTERN.match(text)
        if match:
            code = _clean_codex_sms_code(match.group(1))
            if code:
                return code
    return ""


def _safe_codex_sms_int(value, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _codex_sms_state_file(config: dict | None = None) -> Path:
    raw_path = str((config or {}).get("codex_sms_pool_state_file") or "").strip()
    if raw_path:
        return Path(raw_path).expanduser()
    return _project_data_dir() / ".codex_sms_pool_state.json"


class CodexSmsPoolProvider(BaseSmsProvider):
    """本地 Codex 接码池 provider。"""

    auto_report_success_on_code = False

    def __init__(
        self,
        pool_text: str,
        *,
        poll_interval: int = CODEX_SMS_POOL_POLL_INTERVAL,
        request_timeout: int = CODEX_SMS_POOL_REQUEST_TIMEOUT,
        state_file: str | Path | None = None,
        session: requests.Session | None = None,
    ):
        self.pool_text = str(pool_text or "")
        self.poll_interval = max(1, int(poll_interval or CODEX_SMS_POOL_POLL_INTERVAL))
        self.request_timeout = max(1, int(request_timeout or CODEX_SMS_POOL_REQUEST_TIMEOUT))
        self.state_file = Path(state_file).expanduser() if state_file else _codex_sms_state_file()
        self.session = session or requests.Session()
        self._activation_entries: dict[str, CodexSmsPoolEntry] = {}
        self._attempted_codes: dict[str, set[str]] = {}
        self._last_codes: dict[str, str] = {}

    def _load_state(self) -> dict:
        try:
            if not self.state_file.exists():
                return {}
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _update_usage(
        self,
        entry: CodexSmsPoolEntry,
        *,
        success: bool,
        error: str = "",
        increment_use_count: bool = False,
        increment_failure_count: bool = False,
        blocked: bool | None = None,
    ) -> None:
        with _CODEX_SMS_POOL_STATE_LOCK:
            state = self._load_state()
            usage = state.get("usage") if isinstance(state.get("usage"), dict) else {}
            previous = usage.get(entry.key) if isinstance(usage.get(entry.key), dict) else {}
            now = time.time()
            next_item = {
                "use_count": max(0, int(previous.get("use_count") or 0)) + (1 if increment_use_count else 0),
                "used_at": now if increment_use_count else float(previous.get("used_at") or 0),
                "last_attempt_at": now,
                "last_error": "" if success else str(error or "").strip(),
                "failure_count": 0 if success else max(0, int(previous.get("failure_count") or 0)) + (1 if increment_failure_count else 0),
                "blocked": bool(previous.get("blocked")) if blocked is None else bool(blocked),
            }
            usage[entry.key] = next_item
            state["usage"] = usage
            self._save_state(state)

    def _select_entry(self, entries: list[CodexSmsPoolEntry]) -> CodexSmsPoolEntry:
        if not entries:
            raise RuntimeError("Codex接码池未配置可用号码")
        with _CODEX_SMS_POOL_STATE_LOCK:
            state = self._load_state()
            usage = state.get("usage") if isinstance(state.get("usage"), dict) else {}
            available_entries = [
                entry
                for entry in entries
                if not bool((usage.get(entry.key) or {}).get("blocked"))
            ]
            if not available_entries:
                raise RuntimeError(f"{CODEX_SMS_POOL_EXHAUSTED}: all local phone entries are blocked")
            ranked = sorted(
                available_entries,
                key=lambda entry: (
                    max(0, int((usage.get(entry.key) or {}).get("failure_count") or 0)),
                    max(0, int((usage.get(entry.key) or {}).get("use_count") or 0)),
                    float((usage.get(entry.key) or {}).get("used_at") or 0),
                    entry.index,
                ),
            )
            selected = ranked[0]
            previous = usage.get(selected.key) if isinstance(usage.get(selected.key), dict) else {}
            now = time.time()
            usage[selected.key] = {
                "use_count": max(0, int(previous.get("use_count") or 0)) + 1,
                "used_at": now,
                "last_attempt_at": now,
                "last_error": "",
                "failure_count": max(0, int(previous.get("failure_count") or 0)),
                "blocked": False,
            }
            state["usage"] = usage
            state["current_activation"] = {
                "key": selected.key,
                "phone": selected.phone,
                "verification_url": selected.verification_url,
            }
            self._save_state(state)
            return selected

    def _resolve_entry(self, activation_id: str) -> CodexSmsPoolEntry | None:
        entry = self._activation_entries.get(str(activation_id))
        if entry:
            return entry
        entry = parse_codex_sms_pool_key(activation_id)
        if entry:
            self._activation_entries[entry.key] = entry
        return entry

    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        entries = parse_codex_sms_pool_entries(self.pool_text)
        entry = self._select_entry(entries)
        self._activation_entries[entry.key] = entry
        return SmsActivation(
            activation_id=entry.key,
            phone_number=entry.phone_e164,
            country=country,
            metadata={"verification_url": entry.verification_url, "provider": "codex_sms_pool"},
        )

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        entry = self._resolve_entry(activation_id)
        if not entry:
            raise RuntimeError("Codex接码池激活记录无效")
        deadline = time.time() + max(1, int(timeout or 120))
        last_status = ""
        headers = {
            "Accept": "application/json,text/plain,*/*",
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
        }
        attempted = self._attempted_codes.setdefault(entry.key, set())
        while time.time() < deadline:
            try:
                response = self.session.get(entry.verification_url, headers=headers, timeout=self.request_timeout)
                payload = _parse_codex_sms_payload_text(response.text)
                if not response.ok:
                    last_status = f"HTTP {response.status_code}: {str(response.text or '')[:160]}"
                else:
                    code = extract_codex_sms_verification_code(payload)
                    if code and code not in attempted:
                        self._last_codes[entry.key] = code
                        self._update_usage(entry, success=True)
                        return code
                    if code and code in attempted:
                        last_status = "验证码已尝试，等待下一条"
                    else:
                        preview = response.text.replace("\r", " ").replace("\n", " ").strip()[:180]
                        last_status = f"验证码接口暂未返回有效验证码: {preview}" if preview else "验证码接口暂未返回有效验证码"
            except Exception as exc:
                last_status = str(exc)
            time.sleep(self.poll_interval)

        self._update_usage(entry, success=False, error=last_status or "等待手机验证码超时", increment_failure_count=True)
        return ""

    def cancel(self, activation_id: str) -> bool:
        entry = self._resolve_entry(activation_id)
        if entry:
            self._update_usage(entry, success=False, error="取消接码订单", increment_failure_count=True)
        return True

    def report_success(self, activation_id: str) -> bool:
        entry = self._resolve_entry(activation_id)
        if entry:
            self._update_usage(entry, success=True)
        return True

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        entry = self._resolve_entry(activation_id)
        if not entry:
            return
        last_code = self._last_codes.get(entry.key)
        if last_code:
            self._attempted_codes.setdefault(entry.key, set()).add(last_code)
        should_penalize = self._is_blocking_phone_failure(reason)
        self._update_usage(
            entry,
            success=False,
            error=reason or "验证码被拒绝",
            increment_failure_count=should_penalize,
            blocked=should_penalize,
        )

    @staticmethod
    def _is_blocking_phone_failure(reason: str = "") -> bool:
        reason_text = str(reason or "").lower()
        return any(
            marker in reason_text
            for marker in (
                "http 429",
                "rate_limit",
                "rate limit",
                "rate-limit",
                "too many phone verification",
                "too many",
                "fraud",
                "suspicious behavior",
                "maximum",
                "exceeded",
            )
        )

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        entry = self._resolve_entry(activation_id)
        if entry:
            should_block = self._is_blocking_phone_failure(reason)
            error = reason or "手机号被拒绝"
            if should_block and not str(error).startswith(CODEX_SMS_POOL_BLOCKED_PREFIX):
                error = f"{CODEX_SMS_POOL_BLOCKED_PREFIX}: {error}"
            self._update_usage(
                entry,
                success=False,
                error=error,
                increment_failure_count=True,
                blocked=should_block,
            )


def is_herosms_phone_cache_alive(config: dict | None = None) -> tuple[bool, dict]:
    """Return whether the current HeroSMS cache is reusable for scheduling."""
    config = dict(config or {})
    api_key = str(config.get("herosms_api_key") or "").strip()
    if not api_key:
        return False, {"alive": False}
    provider = HeroSmsProvider(
        api_key,
        default_service=str(config.get("sms_service") or HERO_SMS_DEFAULT_SERVICE),
        default_country=str(config.get("sms_country") or config.get("herosms_country") or HERO_SMS_DEFAULT_COUNTRY),
        phone_success_max=max(0, _safe_int(config.get("register_phone_success_max"), 3)),
    )
    info = provider.get_reuse_info()
    return bool(info.get("alive")), info


# ---------------------------------------------------------------------------
# Factory and browser callback adapter
# ---------------------------------------------------------------------------

def create_sms_provider(provider_key: str, config: dict) -> BaseSmsProvider:
    """Create an SMS provider instance from config."""
    if provider_key in ("sms_activate", "sms_activate_api"):
        api_key = config.get("sms_activate_api_key", "")
        if not api_key:
            raise RuntimeError("SMS-Activate 未配置 API Key")
        return SmsActivateProvider(
            api_key=api_key,
            default_country=config.get("sms_activate_country", config.get("sms_activate_default_country", "")),
            proxy=config.get("sms_proxy") or config.get("proxy") or None,
        )
    if provider_key in ("herosms", "herosms_api"):
        api_key = str(config.get("herosms_api_key", "") or "").strip()
        if not api_key:
            raise RuntimeError("HeroSMS 未配置 API Key")
        return HeroSmsProvider(
            api_key=api_key,
            default_service=str(config.get("sms_service") or config.get("herosms_service") or config.get("herosms_default_service") or HERO_SMS_DEFAULT_SERVICE),
            default_country=str(config.get("sms_country") or config.get("herosms_country") or config.get("herosms_default_country") or HERO_SMS_DEFAULT_COUNTRY),
            max_price=_safe_float(config.get("herosms_max_price"), -1),
            proxy=str(config.get("sms_proxy") or config.get("proxy") or "") or None,
            reuse_phone_to_max=_safe_bool(config.get("register_reuse_phone_to_max"), True),
            phone_success_max=max(0, _safe_int(config.get("register_phone_extra_max") or config.get("register_phone_success_max"), 3)),
        )
    if provider_key in ("smsbower", "smsbower_api"):
        api_key = str(config.get("smsbower_api_key", "") or "").strip()
        if not api_key:
            raise RuntimeError("SMSBower 未配置 API Key")
        return SmsBowerProvider(
            api_key=api_key,
            default_service=str(config.get("sms_service") or config.get("smsbower_service") or config.get("smsbower_default_service") or HERO_SMS_DEFAULT_SERVICE),
            default_country=str(config.get("sms_country") or config.get("smsbower_country") or config.get("smsbower_default_country") or HERO_SMS_DEFAULT_COUNTRY),
            max_price=_safe_float(config.get("smsbower_max_price"), -1),
            proxy=str(config.get("sms_proxy") or config.get("proxy") or "") or None,
            reuse_phone_to_max=_safe_bool(config.get("register_reuse_phone_to_max"), True),
            phone_success_max=max(0, _safe_int(config.get("register_phone_extra_max") or config.get("register_phone_success_max"), 3)),
        )
    if provider_key in ("grizzlysms", "grizzlysms_api", "grizzly_sms", "grizzly_sms_api"):
        api_key = str(config.get("grizzlysms_api_key") or config.get("grizzly_sms_api_key") or "").strip()
        if not api_key:
            raise RuntimeError("GrizzlySMS API Key is not configured")
        provider = GrizzlySmsProvider(
            api_key=api_key,
            default_service=str(config.get("sms_service") or config.get("grizzlysms_service") or config.get("grizzlysms_default_service") or HERO_SMS_DEFAULT_SERVICE),
            default_country=str(config.get("sms_country") or config.get("grizzlysms_country") or config.get("grizzlysms_default_country") or "52"),
            max_price=_safe_float(config.get("grizzlysms_max_price"), -1),
            proxy=str(config.get("sms_proxy") or config.get("proxy") or "") or None,
            reuse_phone_to_max=_safe_bool(config.get("register_reuse_phone_to_max"), True),
            phone_success_max=max(0, _safe_int(config.get("register_phone_extra_max") or config.get("register_phone_success_max"), 3)),
        )
        base_url = str(config.get("grizzlysms_base_url") or "").strip()
        if base_url:
            provider.BASE_URL = base_url
        return provider
    if provider_key in ("sms_verification_number", "sms_verification_number_api", "sms-verification-number"):
        api_key = str(config.get("sms_verification_number_api_key") or "").strip()
        if not api_key:
            raise RuntimeError("SMS Verification Number API Key is not configured")
        provider = SmsVerificationNumberProvider(
            api_key=api_key,
            default_service=str(config.get("sms_service") or config.get("sms_verification_number_service") or config.get("sms_verification_number_default_service") or HERO_SMS_DEFAULT_SERVICE),
            default_country=str(config.get("sms_country") or config.get("sms_verification_number_country") or config.get("sms_verification_number_default_country") or "33"),
            max_price=_safe_float(config.get("sms_verification_number_max_price"), -1),
            proxy=str(config.get("sms_proxy") or config.get("proxy") or "") or None,
            reuse_phone_to_max=_safe_bool(config.get("register_reuse_phone_to_max"), True),
            phone_success_max=max(0, _safe_int(config.get("register_phone_extra_max") or config.get("register_phone_success_max"), 3)),
        )
        base_url = str(config.get("sms_verification_number_base_url") or "").strip()
        if base_url:
            provider.BASE_URL = base_url
        return provider
    if provider_key in ("smspool", "smspool_api", "sms_pool", "sms_pool_api"):
        api_key = str(config.get("smspool_api_key") or config.get("smsPoolApiKey") or "").strip()
        if not api_key:
            raise RuntimeError("SMSPool API Key is not configured")
        return SmsPoolProvider(
            api_key=api_key,
            default_service=str(config.get("smspool_service") or config.get("smspool_default_service") or config.get("smsPoolServiceCode") or SMSPOOL_DEFAULT_SERVICE),
            default_country=str(config.get("smspool_country") or config.get("smspool_default_country") or config.get("smsPoolCountry") or SMSPOOL_DEFAULT_COUNTRY),
            base_url=str(config.get("smspool_base_url") or SMSPOOL_DEFAULT_BASE_URL),
            compat_base_url=str(config.get("smspool_compat_base_url") or SMSPOOL_DEFAULT_COMPAT_BASE_URL),
            max_price=_safe_float(config.get("smspool_max_price"), -1),
            proxy=str(config.get("sms_proxy") or config.get("proxy") or "") or None,
            poll_interval=max(1, _safe_int(config.get("sms_poll_interval"), REMOTE_SMS_POLL_INTERVAL)),
        )
    if provider_key in ("5sim", "five_sim", "five_sim_api"):
        api_key = str(config.get("five_sim_api_key") or config.get("fiveSimApiKey") or "").strip()
        if not api_key:
            raise RuntimeError("5sim API Key is not configured")
        return FiveSimProvider(
            api_key=api_key,
            country=str(config.get("five_sim_country") or config.get("five_sim_default_country") or config.get("fiveSimCountryId") or FIVE_SIM_DEFAULT_COUNTRY),
            operator=str(config.get("five_sim_operator") or FIVE_SIM_DEFAULT_OPERATOR),
            product=str(config.get("five_sim_product") or config.get("fiveSimProduct") or FIVE_SIM_DEFAULT_PRODUCT),
            base_url=str(config.get("five_sim_base_url") or FIVE_SIM_DEFAULT_BASE_URL),
            max_price=_safe_float(config.get("five_sim_max_price"), -1),
            proxy=str(config.get("sms_proxy") or config.get("proxy") or "") or None,
            reuse=_safe_bool(config.get("five_sim_reuse"), True),
            poll_interval=max(1, _safe_int(config.get("sms_poll_interval"), REMOTE_SMS_POLL_INTERVAL)),
        )
    if provider_key in ("nexsms", "nexsms_api", "nex_sms", "nex_sms_api"):
        api_key = str(config.get("nexsms_api_key") or config.get("nexSmsApiKey") or "").strip()
        if not api_key:
            raise RuntimeError("NexSMS API Key is not configured")
        return NexSmsProvider(
            api_key=api_key,
            country_order=config.get("nexsms_default_country") or config.get("nexsms_country_order") or config.get("nexsms_country") or config.get("nexSmsCountryOrder") or "",
            service_code=str(config.get("nexsms_service") or config.get("nexsms_default_service") or config.get("nexSmsServiceCode") or NEXSMS_DEFAULT_SERVICE),
            base_url=str(config.get("nexsms_base_url") or NEXSMS_DEFAULT_BASE_URL),
            max_price=_safe_float(config.get("nexsms_max_price"), -1),
            proxy=str(config.get("sms_proxy") or config.get("proxy") or "") or None,
            poll_interval=max(1, _safe_int(config.get("sms_poll_interval"), REMOTE_SMS_POLL_INTERVAL)),
        )
    if provider_key in ("codex_sms_pool", "codex_sms_pool_api", "chatgpt-api", "chatgpt_api"):
        pool_text = str(
            config.get("codex_sms_pool_text")
            or config.get("codex_sms_pool")
            or config.get("chatGptApiSmsPoolText")
            or ""
        )
        if not parse_codex_sms_pool_entries(pool_text):
            raise RuntimeError("Codex接码池未配置可用号码，格式：+手机号|取码链接，一行一个")
        return CodexSmsPoolProvider(
            pool_text,
            poll_interval=_safe_codex_sms_int(config.get("codex_sms_pool_poll_interval"), CODEX_SMS_POOL_POLL_INTERVAL),
            request_timeout=_safe_codex_sms_int(config.get("codex_sms_pool_request_timeout"), CODEX_SMS_POOL_REQUEST_TIMEOUT),
            state_file=str(config.get("codex_sms_pool_state_file") or "") or None,
        )
    raise RuntimeError(f"未知的接码服务: {provider_key}")


class PhoneCallbackController:
    """Callable phone callback with optional lifecycle hooks for advanced providers."""

    def __init__(self, provider_key: str, config: dict, *, service: str, country: str = "", log_fn=None):
        self.provider_key = provider_key
        self.config = dict(config or {})
        self.service = service
        self.country = country
        self.log = log_fn or logger.info
        self.provider: Optional[BaseSmsProvider] = None
        self.activation: Optional[SmsActivation] = None
        self.phase = "need_number"
        self.completed = False
        self._verify_lock_acquired = False
        self.awaiting_external_success = False
        # get_rt add_phone 页等待短信只给 60 秒，普通注册仍可沿用默认值。
        self.code_timeout = max(1, _safe_int(self.config.get("sms_code_timeout") or self.config.get("phone_code_timeout"), 180))
        self._send_failure_count = 0
        self._account_create_failure_count = 0
        self._auto_country_candidates: list[dict[str, Any]] = []
        self._last_country_index = -1
        self._phone_failures_per_country = max(
            1,
            _safe_int(
                self.config.get("sms_phone_retry_limit")
                or self.config.get("phone_retry_limit")
                or self.config.get("sms_phone_failures_per_country"),
                SMS_PHONE_FAILURES_PER_COUNTRY,
            ),
        )
        self._country_retry_limit = max(
            1,
            _safe_int(
                self.config.get("sms_country_retry_limit")
                or self.config.get("phone_country_retry_limit"),
                SMS_COUNTRY_RETRY_LIMIT,
            ),
        )

    def _provider(self) -> BaseSmsProvider:
        if self.provider is None:
            self.provider = create_sms_provider(self.provider_key, self.config)
        return self.provider

    def _configured_country(self) -> str:
        return _first_nonempty_text(
            self.country,
            self.config.get("sms_country"),
            self.config.get("phone_country"),
            self.config.get("herosms_country"),
            self.config.get("herosms_default_country"),
            self.config.get("smsbower_country"),
            self.config.get("smsbower_default_country"),
            self.config.get("grizzlysms_country"),
            self.config.get("grizzlysms_default_country"),
            self.config.get("sms_verification_number_country"),
            self.config.get("sms_verification_number_default_country"),
            self.config.get("smspool_country"),
            self.config.get("smspool_default_country"),
            self.config.get("five_sim_country"),
            self.config.get("five_sim_default_country"),
            self.config.get("nexsms_default_country"),
            self.config.get("nexsms_country"),
            self.config.get("nexsms_country_order"),
            self.config.get("sms_activate_country"),
            self.config.get("sms_activate_default_country"),
        )

    def _country_plan_enabled(self, provider: BaseSmsProvider) -> bool:
        if not isinstance(provider, HeroSmsProvider):
            return False
        provider_key = str(self.provider_key or "").strip().lower()
        if provider_key in {"smsbower", "smsbower_api"}:
            raw = self.config.get("smsbower_auto_country")
        elif provider_key in {"grizzlysms", "grizzlysms_api", "grizzly_sms", "grizzly_sms_api"}:
            raw = self.config.get("grizzlysms_auto_country")
        elif provider_key in {"sms_verification_number", "sms_verification_number_api", "sms-verification-number"}:
            raw = self.config.get("sms_verification_number_auto_country")
        else:
            raw = self.config.get("herosms_auto_country")
        # 默认启用价格排序国家计划；用户显式设 false 时关闭。
        return _safe_bool(raw, True)

    def _country_price_limit(self) -> float:
        provider_key = str(self.provider_key or "").strip().lower()
        if provider_key in {"smsbower", "smsbower_api"}:
            return _safe_float(
                self.config.get("smsbower_auto_country_max_price")
                or self.config.get("smsbower_max_price"),
                0,
            )
        if provider_key in {"grizzlysms", "grizzlysms_api", "grizzly_sms", "grizzly_sms_api"}:
            return _safe_float(
                self.config.get("grizzlysms_auto_country_max_price")
                or self.config.get("grizzlysms_max_price"),
                0,
            )
        if provider_key in {"sms_verification_number", "sms_verification_number_api", "sms-verification-number"}:
            return _safe_float(
                self.config.get("sms_verification_number_auto_country_max_price")
                or self.config.get("sms_verification_number_max_price"),
                0,
            )
        return _safe_float(
            self.config.get("herosms_auto_country_max_price")
            or self.config.get("herosms_max_price"),
            0,
        )

    @staticmethod
    def _country_candidate_from_row(row: dict) -> dict[str, Any] | None:
        country_id = str(row.get("country") or "").strip()
        if not country_id:
            return None
        price = None
        count = None
        try:
            if row.get("price") not in (None, ""):
                price = float(row.get("price"))
        except (TypeError, ValueError):
            price = None
        try:
            if row.get("count") not in (None, ""):
                count = int(row.get("count"))
        except (TypeError, ValueError):
            count = None
        return {
            "country": country_id,
            "name": str(row.get("name") or "").strip(),
            "price": price,
            "count": count,
        }

    @staticmethod
    def _country_candidate_label(candidate: dict[str, Any]) -> str:
        country = str(candidate.get("country") or "")
        price = candidate.get("price")
        count = candidate.get("count")
        suffix = []
        if price not in (None, ""):
            suffix.append(f"price={price:g}")
        if count not in (None, ""):
            suffix.append(f"stock={count}")
        return country if not suffix else f"{country}({', '.join(suffix)})"

    def _ensure_country_attempt_plan(self, provider: BaseSmsProvider) -> list[dict[str, Any]]:
        if self._auto_country_candidates:
            return self._auto_country_candidates
        if not self._country_plan_enabled(provider):
            return []

        configured_country = self._configured_country()
        max_price = self._country_price_limit()
        min_stock = _safe_int(
            self.config.get("herosms_auto_country_min_stock")
            or self.config.get("smsbower_auto_country_min_stock")
            or self.config.get("grizzlysms_auto_country_min_stock")
            or self.config.get("sms_verification_number_auto_country_min_stock"),
            20,
        )

        try:
            raw_rows = provider.get_top_countries(service=self.service)  # type: ignore[attr-defined]
        except Exception as exc:
            self.log(f"接码国家价格列表获取失败({_redact_sms_error_text(exc)})，仅使用当前国家")
            raw_rows = []

        rows: list[dict[str, Any]] = []
        for raw in raw_rows or []:
            if not isinstance(raw, dict):
                continue
            candidate = self._country_candidate_from_row(raw)
            if not candidate:
                continue
            price = candidate.get("price")
            if max_price > 0 and price not in (None, "") and float(price) > max_price:
                continue
            rows.append(candidate)
        rows.sort(key=lambda item: (item.get("price") if item.get("price") is not None else 999999.0, -(item.get("count") or 0)))

        plan: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(candidate: dict[str, Any] | None) -> None:
            if not candidate:
                return
            country_id = str(candidate.get("country") or "").strip()
            if not country_id or country_id in seen:
                return
            seen.add(country_id)
            plan.append(candidate)

        if configured_country:
            matched = next((row for row in rows if str(row.get("country") or "") == configured_country), None)
            add(matched or {"country": configured_country, "price": None, "count": None})

        for pass_name in ("stock", "relaxed", "fallback"):
            for row in rows:
                if len(plan) >= self._country_retry_limit:
                    break
                count = row.get("count")
                if pass_name == "stock" and count is not None and int(count) < min_stock:
                    continue
                if pass_name == "relaxed" and count is not None and int(count) <= 0:
                    continue
                add(row)
            if len(plan) >= self._country_retry_limit:
                break

        self._auto_country_candidates = plan[: self._country_retry_limit]
        if self._auto_country_candidates:
            labels = " -> ".join(self._country_candidate_label(item) for item in self._auto_country_candidates)
            self.log(f"接码国家尝试计划(按价格排序，最多 {self._country_retry_limit} 国): {labels}")
        return self._auto_country_candidates

    @staticmethod
    def _amount_label(value, *, currency: str = "USD") -> str:
        if value in (None, ""):
            return "未知"
        text = str(value)
        if text.startswith("查询失败"):
            return text
        return f"{_format_number_for_log(value)} {currency}".strip()

    def _balance_label(self, provider: BaseSmsProvider, *, currency: str = "USD") -> str:
        getter = getattr(provider, "get_balance", None)
        if not callable(getter):
            return "未知"
        try:
            return self._amount_label(getter(), currency=currency)
        except Exception as exc:
            return f"查询失败({_redact_sms_error_text(exc)})"

    def _price_info_for_log(self, provider: BaseSmsProvider, *, country: str) -> dict:
        getter = getattr(provider, "get_current_price_info", None)
        if not callable(getter):
            return {}
        try:
            info = getter(service=self.service, country=country)
            return dict(info) if isinstance(info, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _merge_country_plan_price_info(info: dict, candidate: dict[str, Any] | None) -> dict:
        if not candidate:
            return dict(info or {})
        merged = dict(info or {})
        if candidate.get("price") not in (None, ""):
            merged["price"] = candidate.get("price")
        if candidate.get("count") not in (None, "") and merged.get("count") in (None, ""):
            merged["count"] = candidate.get("count")
        return merged

    @staticmethod
    def _candidate_price_cap(candidate: dict[str, Any] | None, provider: BaseSmsProvider) -> float:
        if not candidate or not hasattr(provider, "max_price"):
            return 0
        try:
            candidate_price = float(candidate.get("price")) if candidate.get("price") not in (None, "") else 0
        except (TypeError, ValueError):
            candidate_price = 0
        if candidate_price <= 0:
            return 0
        try:
            provider_cap = float(getattr(provider, "max_price", 0) or 0)
        except (TypeError, ValueError):
            provider_cap = 0
        if provider_cap > 0 and candidate_price > provider_cap:
            return 0
        return candidate_price

    def _price_label(self, info: dict, activation: SmsActivation | None = None) -> str:
        metadata = activation.metadata if activation and isinstance(activation.metadata, dict) else {}
        number_info = metadata.get("number_info") if isinstance(metadata.get("number_info"), dict) else {}
        price_info = metadata.get("price_info") if isinstance(metadata.get("price_info"), dict) else info
        currency = str((price_info or {}).get("currency") or info.get("currency") or "USD")
        price = (
            metadata.get("activation_cost")
            or number_info.get("activationCost")
            or number_info.get("activationPrice")
            or number_info.get("cost")
            or number_info.get("price")
            or (price_info or {}).get("price")
            or info.get("price")
        )
        max_price = metadata.get("max_price")
        try:
            price_float = float(price) if price not in (None, "") else 0
        except (TypeError, ValueError):
            price_float = 0
        try:
            max_price_float = float(max_price) if max_price not in (None, "") else 0
        except (TypeError, ValueError):
            max_price_float = 0
        try:
            plan_price = (price_info or {}).get("price")
            plan_price_float = float(plan_price) if plan_price not in (None, "") else 0
        except (TypeError, ValueError):
            plan_price = None
            plan_price_float = 0
        if max_price_float > 0 and price_float > max_price_float:
            if plan_price_float > 0 and plan_price_float <= max_price_float:
                price = plan_price
            else:
                price = max_price
        return self._amount_label(price, currency=currency)

    def _billing_log_suffix(self, *, balance: str, price_info: dict, activation: SmsActivation | None = None) -> str:
        metadata = activation.metadata if activation and isinstance(activation.metadata, dict) else {}
        effective_info = metadata.get("price_info") if isinstance(metadata.get("price_info"), dict) else price_info
        stock = (effective_info or {}).get("count")
        max_price = metadata.get("max_price")
        parts = [
            f"余额={balance or '未知'}",
            f"当前价={self._price_label(price_info, activation)}",
        ]
        if stock not in (None, ""):
            parts.append(f"stock={stock}")
        if max_price not in (None, ""):
            parts.append(f"maxPrice={_format_number_for_log(max_price)}")
        return ", ".join(parts)

    def get_add_phone_attempt_limit(self, default_limit: int) -> int:
        explicit_limit = _safe_int(
            _first_nonempty_text(
                self.config.get("add_phone_attempt_limit"),
                self.config.get("phone_change_limit"),
                self.config.get("sms_phone_change_limit"),
            ),
            0,
        )
        if explicit_limit > 0:
            return max(1, explicit_limit)
        provider = self._provider()
        if self._country_plan_enabled(provider):
            plan = self._ensure_country_attempt_plan(provider)
            return max(1, len(plan) or 1) * self._phone_failures_per_country
        return max(1, int(default_limit or 1))

    def __call__(self) -> str:
        provider = self._provider()
        if self.phase == "need_number":
            if self.provider_key == "herosms" and not self._verify_lock_acquired:
                _HERO_SMS_VERIFY_LOCK.acquire()
                self._verify_lock_acquired = True

            effective_country = self.country
            selected_candidate: dict[str, Any] | None = None
            country_plan_exhausted = False
            auto_select = self._country_plan_enabled(provider)
            if auto_select:
                try:
                    plan = self._ensure_country_attempt_plan(provider)
                    if plan:
                        # 仅注册帐号同款：同一国家先换 10 个号；仍失败则按价格切下一个国家。
                        country_index = self._send_failure_count // self._phone_failures_per_country
                        if country_index >= len(plan):
                            country_plan_exhausted = True
                        else:
                            candidate = plan[country_index]
                            selected_candidate = candidate
                            effective_country = str(candidate.get("country") or "").strip()
                            if country_index != self._last_country_index:
                                action = "自动选择国家" if self._send_failure_count <= 0 else f"当前国家已失败 {self._phone_failures_per_country} 次，切换下一国家"
                                self.log(
                                    f"{action}: {self._country_candidate_label(candidate)} "
                                    f"({country_index + 1}/{len(plan)})"
                                )
                                self._last_country_index = country_index
                    else:
                        self.log("未找到满足条件的国家，使用默认配置")
                except Exception as exc:
                    self.log(f"智能国家选择失败({_redact_sms_error_text(exc)})，使用默认配置")

            if auto_select:
                plan_len = len(self._auto_country_candidates or [])
                if country_plan_exhausted or (
                    plan_len and self._send_failure_count // self._phone_failures_per_country >= plan_len
                ):
                    raise RuntimeError(
                        f"SMS country plan exhausted provider={self.provider_key} "
                        f"service={self.service} countries={plan_len}"
                    )

            country_label = effective_country or self.config.get("sms_country") or self.config.get("sms_activate_country") or "default"
            self.log(f"已进入 add_phone，准备租用手机号: provider={self.provider_key} service={self.service} country={country_label}")
            price_info = self._price_info_for_log(provider, country=effective_country)
            price_info = self._merge_country_plan_price_info(price_info, selected_candidate)
            balance_label = self._balance_label(provider, currency=str(price_info.get("currency") or "USD"))
            self.log(
                f"正在从 {self.provider_key} 获取手机号... "
                f"{self._billing_log_suffix(balance=balance_label, price_info=price_info)}"
            )
            original_max_price = getattr(provider, "max_price", None)
            candidate_price_cap = self._candidate_price_cap(selected_candidate, provider)
            try:
                if candidate_price_cap > 0:
                    try:
                        setattr(provider, "max_price", candidate_price_cap)
                    except Exception:
                        pass
                self.activation = provider.get_number(service=self.service, country=effective_country)
            except Exception as first_exc:
                # Only fall back to the configured country on the first country
                # selection failure. After OpenAI has rejected phones, falling
                # back to an already-failed country just burns balance.
                fallback_country = self.country or self.config.get("sms_country") or self.config.get("herosms_country") or ""
                if auto_select and effective_country != fallback_country and fallback_country and self._send_failure_count <= 0:
                    self.log(f"自动选择的国家({effective_country})获取号码失败，回退到默认国家({fallback_country})...")
                    try:
                        self.activation = provider.get_number(service=self.service, country=fallback_country)
                    except Exception:
                        if self._verify_lock_acquired:
                            _HERO_SMS_VERIFY_LOCK.release()
                            self._verify_lock_acquired = False
                        raise first_exc
                else:
                    if self._verify_lock_acquired:
                        _HERO_SMS_VERIFY_LOCK.release()
                        self._verify_lock_acquired = False
                    raise first_exc
            finally:
                if candidate_price_cap > 0:
                    try:
                        setattr(provider, "max_price", original_max_price)
                    except Exception:
                        pass
            self.phase = "need_code"
            reused = bool((self.activation.metadata or {}).get("reused"))
            reuse_label = "复用号码" if reused else "新号码"
            self.log(
                f"已成功租到号码({reuse_label}): {self.activation.phone_number} "
                f"(activation_id={self.activation.activation_id}, "
                f"{self._billing_log_suffix(balance=balance_label, price_info=price_info, activation=self.activation)})"
            )
            return self.activation.phone_number

        if self.phase == "need_code" and self.activation:
            self.log(f"等待短信验证码... (activation_id={self.activation.activation_id})")
            code = provider.get_code(self.activation.activation_id, timeout=self.code_timeout)
            if code:
                self.log(f"收到验证码: {code}")
                if getattr(provider, "auto_report_success_on_code", True):
                    self.report_success()
                else:
                    self.awaiting_external_success = True
            else:
                self.log(f"⚠️ 未收到验证码: activation_id={self.activation.activation_id}")
            return code
        return ""

    def set_code_timeout(self, timeout: int) -> None:
        """设置当前流程等待短信验证码的秒数。"""
        self.code_timeout = max(1, _safe_int(timeout, self.code_timeout))

    def set_resend_callback(self, callback: Callable[[], None] | None) -> None:
        if self.provider is not None:
            self.provider.set_resend_callback(callback)
        else:
            original_provider = self._provider()
            original_provider.set_resend_callback(callback)

    def mark_code_failed(self, reason: str = "") -> None:
        if self.activation and self.provider:
            hook = getattr(self.provider, "mark_code_failed", None)
            if callable(hook):
                hook(self.activation.activation_id, reason=reason)
            self.phase = "need_code"
            self.awaiting_external_success = False

    def mark_send_failed(self, reason: str = "") -> None:
        self._send_failure_count += 1
        reason_text = str(reason or "").lower()
        strong_rejection = any(
            marker in reason_text
            for marker in (
                "voip_phone_disallowed",
                "virtual phone",
                "voip",
                "invalid phone number",
                "invalid_phone",
                "account_creation_failed",
                "failed to create account",
                "error creating your account",
                "unable to create account",
            )
        )
        is_virtual_phone_rejection = _is_virtual_or_voip_phone_rejection(reason_text)
        if strong_rejection:
            self._account_create_failure_count += 1
            if is_virtual_phone_rejection:
                next_country_failure_count = (
                    (self._send_failure_count // self._phone_failures_per_country) + 1
                ) * self._phone_failures_per_country
                self._send_failure_count = max(self._send_failure_count, next_country_failure_count)
                self.log("Phone country rejected as virtual/VoIP; switching to next country")
            elif "invalid phone" in reason_text:
                self._send_failure_count = max(self._send_failure_count, self._phone_failures_per_country)
        else:
            self._account_create_failure_count = 0
        if self.activation and self.provider:
            hook = getattr(self.provider, "mark_send_failed", None)
            activation_id_for_log = str(getattr(self.activation, "activation_id", "") or "")
            phone_for_log = str(getattr(self.activation, "phone_number", "") or "")
            if callable(hook):
                try:
                    hook(self.activation.activation_id, reason=reason)
                except Exception as exc:
                    self.log(
                        f"号码释放/标记失败 provider={self.provider_key} "
                        f"activation={activation_id_for_log} phone={phone_for_log}: {exc}"
                    )
            # 明确告诉使用者该号已交付供应商释放。反欺诈/冷却类拒绝下，
            # 供应商 provider 内部已负责同步 cancel 或入队后台释放 (data/smspool_release_queue.json)。
            release_hint = ""
            reason_lower = str(reason or "").lower()
            if any(
                keyword in reason_lower
                for keyword in (
                    "suspicious behavior",
                    "fraud_detected",
                    "\"fraud\"",
                    "voip_phone_disallowed",
                    "voip",
                    "invalid phone",
                )
            ):
                release_hint = " reason=fraud_or_cooldown"
            self.log(
                f"已提交释放手机号请求 provider={self.provider_key} "
                f"activation={activation_id_for_log} phone={phone_for_log}{release_hint}"
            )
            self.awaiting_external_success = False
            self.activation = None
            self.phase = "need_number"
            self.completed = False

    def mark_number_fetch_failed(self, reason: str = "") -> None:
        reason_text = str(reason or "").lower()
        if not any(
            marker in reason_text
            for marker in (
                "no_numbers",
                "no numbers",
                "no_number",
                "no available number",
                "no available phone",
            )
        ):
            return
        self._send_failure_count += 1
        self.phase = "need_number"
        self.activation = None
        self.completed = False

    def mark_send_succeeded(self) -> None:
        if self.activation and self.provider:
            hook = getattr(self.provider, "mark_send_succeeded", None)
            if callable(hook):
                hook(self.activation.activation_id)

    def report_success(self) -> None:
        if self.activation and self.provider and not self.completed:
            self.provider.report_success(self.activation.activation_id)
            self.completed = True
            self.phase = "done"
            self.awaiting_external_success = False
            self._send_failure_count = 0
            self._account_create_failure_count = 0
            self.log(f"短信验证成功，已标记号码完成使用: activation_id={self.activation.activation_id}")
        if self._verify_lock_acquired:
            _HERO_SMS_VERIFY_LOCK.release()
            self._verify_lock_acquired = False

    def cleanup(self) -> None:
        if self.activation and not self.completed:
            try:
                provider = self._provider()
                if self.awaiting_external_success and not getattr(provider, "auto_report_success_on_code", True):
                    self.report_success()
                else:
                    cancel_ok = provider.cancel(self.activation.activation_id)
                    if cancel_ok:
                        self.log(f"SMS phone released: activation_id={self.activation.activation_id}")
                    else:
                        self.log(
                            "SMS phone release queued/pending: "
                            f"activation_id={self.activation.activation_id}"
                        )
            except Exception:
                pass
        if self._verify_lock_acquired:
            _HERO_SMS_VERIFY_LOCK.release()
            self._verify_lock_acquired = False


def create_phone_callbacks(
    provider_key: str,
    config: dict,
    *,
    service: str,
    country: str = "",
    log_fn=None,
) -> tuple:
    """Create (phone_callback, cleanup) tuple for browser registration."""
    controller = PhoneCallbackController(
        provider_key,
        config,
        service=service,
        country=country,
        log_fn=log_fn,
    )
    return controller, controller.cleanup
