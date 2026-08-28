"""
SMS activation API helpers for GoPay protocol flows.

Supports two independent providers:
- SMSBower (SMS-Activate compatible): ``OPAI_SMSBOWER_*``
- SMSHero (SMS-Activate compatible): ``OPAI_SMSHERO_*``

This deployment defaults to SMSHero (``https://hero-sms.com``), the provider
the GoPay flows were originally wired to.  ``OPAI_HEROSMS_*`` is kept as an
alias for SMSHero so existing launch scripts still work.
"""
from __future__ import annotations

import logging
import json
import os
import re
import time
from pathlib import Path

import tls_client

from .log_redaction import install_sensitive_log_filter

log = logging.getLogger(__name__)
install_sensitive_log_filter(log)

SMS_TIMEOUT = 120

_SMS_PROVIDER_CONFIG = {
    "smsbower": {
        "aliases": ("OPAI_SMSBOWER_",),
        "handler_path": "/stubs/handler_api.php",
        "default_base_url": "https://smsbower.page",
    },
    "smshero": {
        "aliases": ("OPAI_SMSHERO_", "OPAI_HEROSMS_"),
        "handler_path": "/stubs/handler_api.php",
        "default_base_url": "https://hero-sms.com",
    },
}

_SMS_ENV_PREFIXES = tuple(
    prefix
    for cfg in _SMS_PROVIDER_CONFIG.values()
    for prefix in cfg["aliases"]
)


def _resolve_provider(provider: str) -> str:
    provider = (provider or "smshero").lower().strip()
    if provider in _SMS_PROVIDER_CONFIG:
        return provider
    # Accept historical / casual names.
    if provider in {"herosms", "hero", "smshero"}:
        return "smshero"
    if provider in {"smsbower", "bower"}:
        return "smsbower"
    return "smshero"


def _provider_env_first(provider: str, *names: str, default: str = "") -> str:
    cfg = _SMS_PROVIDER_CONFIG[_resolve_provider(provider)]
    for name in names:
        for prefix in cfg["aliases"]:
            value = os.environ.get(f"{prefix}{name}", "")
            if value:
                return value
    return default


def load_selected_env_file(prefixes: tuple[str, ...], path: str = "") -> None:
    """Load selected KEY=VALUE entries without requiring python-dotenv."""
    configured = (path or os.environ.get("OPAI_GOPAY_SMS_ENV_FILE", "")).strip()
    candidates = [Path(configured).expanduser()] if configured else [
        Path.cwd() / "config" / "sms.env",
        Path(__file__).resolve().parents[4] / "config" / "sms.env",
    ]
    for env_path in candidates:
        if not env_path.is_file():
            continue
        try:
            with env_path.open(encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key.startswith(prefixes) and not os.environ.get(key):
                        os.environ[key] = value
        except OSError as exc:
            log.debug("Could not load SMS env file %s: %s", env_path, exc)
        if configured:
            break


def _load_env_file(path: str = "") -> None:
    load_selected_env_file(_SMS_ENV_PREFIXES, path)


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "")
        if value:
            return value
    return default


def get_sms_api_key(api_key: str = "", provider: str = "") -> str:
    provider = _resolve_provider(provider)
    _load_env_file()
    if api_key:
        return api_key
    key = _provider_env_first(provider, "API_KEY")
    if key:
        return key
    key_file = _provider_env_first(provider, "API_KEY_FILE")
    if key_file and os.path.exists(key_file):
        try:
            return Path(key_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            log.warning("Could not read SMS API key file %s: %s", key_file, exc)
    return ""


def sms_api_base_url(provider: str = "") -> str:
    provider = _resolve_provider(provider)
    _load_env_file()
    default = _SMS_PROVIDER_CONFIG[provider]["default_base_url"]
    return _provider_env_first(provider, "API_BASE_URL", default=default).rstrip("/")


def sms_api_url(provider: str = "") -> str:
    """Return the configured handler endpoint without duplicating its path."""
    provider = _resolve_provider(provider)
    base = sms_api_base_url(provider)
    handler_path = _SMS_PROVIDER_CONFIG[provider]["handler_path"]
    if base.lower().endswith("handler_api.php"):
        return base
    return f"{base}{handler_path}"


def _json_response_payload(response: str) -> dict | None:
    try:
        value = json.loads(response)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _json_values(payload: object):
    """Yield scalar values from the provider's occasionally nested JSON body."""
    if isinstance(payload, dict):
        for value in payload.values():
            yield from _json_values(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            yield from _json_values(value)
    elif payload is not None:
        yield str(payload)


def sms_api(
    api_key: str,
    action: str,
    params: dict | None = None,
    retries: int = 3,
    provider: str = "",
) -> str:
    provider = _resolve_provider(provider)
    api_key = get_sms_api_key(api_key, provider)
    p = {"api_key": api_key, "action": action}
    if params:
        p.update(params)
    last_error: Exception | None = None
    for i in range(1, retries + 1):
        try:
            s = tls_client.Session(client_identifier="chrome_120")
            r = s.get(sms_api_url(provider), params=p, timeout_seconds=30)
            body = str(getattr(r, "text", "") or "").strip()
            status_code = int(getattr(r, "status_code", 200) or 200)
            if 200 <= status_code < 300:
                return body
            raise RuntimeError(f"HTTP {status_code}: {body[:200]}")
        except Exception as e:
            last_error = e
            log.warning("sms_api %s attempt %d/%d failed: %s", action, i, retries, e)
            if i < retries:
                time.sleep(3)
    raise RuntimeError(f"sms_api {action} failed after {retries} retries") from last_error


def sms_get_number(api_key: str = "", provider: str = "") -> tuple[str | None, str | None]:
    provider = _resolve_provider(provider)
    _load_env_file()
    service = _provider_env_first(provider, "SERVICE", default="ni")
    country = _provider_env_first(provider, "COUNTRY", default="6")
    resp = sms_api(api_key, "getNumber", {"service": service, "country": country}, provider=provider)
    log.info("[%s] getNumber: %s", provider, resp)
    if resp.startswith("ACCESS_NUMBER:"):
        parts = resp.split(":", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            number = parts[2].strip()
            return number if number.startswith("+") else f"+{number}", parts[1].strip()
    payload = _json_response_payload(resp)
    if payload:
        data = payload.get("data", payload)
        values = list(_json_values(data))
        aid = next((v for v in values if re.fullmatch(r"\d+", v)), "")
        number = next((v for v in values if re.search(r"\d{7,}", v) and v != aid), "")
        if aid and number:
            return number if number.startswith("+") else f"+{number}", aid
    log.warning("[%s] getNumber failed: %s", provider, resp)
    return None, None


def sms_wait_code(
    api_key: str,
    aid: str,
    timeout: int = SMS_TIMEOUT,
    *,
    ignore_code: str = "",
    provider: str = "",
) -> str | None:
    """Poll one activation while filtering stale OTPs from ``setStatus=3``.

    SMSHero/compatible gateways may return ``STATUS_WAIT_RETRY:<old-code>``
    after an activation is moved to retry.  The old code is not a new OTP and
    must not be handed to the next GoPay CVS/PIN step.
    """
    provider = _resolve_provider(provider)
    ignored = str(ignore_code or "").strip()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = sms_api(api_key, "getStatus", {"id": aid}, provider=provider)
        except Exception as e:
            log.warning("[%s] getStatus(%s) 异常: %s", provider, aid, e)
            time.sleep(5)
            continue
        if resp.startswith("STATUS_OK:"):
            code = resp.split(":", 1)[1]
            m = re.search(r"\b(\d{4,6})\b", code)
            candidate = m.group(1) if m else code.strip()
            if candidate and candidate != ignored:
                return candidate
            log.debug("[%s] Ignoring stale SMS code for aid=%s", provider, aid)
        if resp.startswith("STATUS_WAIT_RETRY:"):
            stale = resp.split(":", 1)[1].strip()
            if stale:
                log.debug("[%s] stale retry code for aid=%s: %s", provider, aid, stale)
        payload = _json_response_payload(resp)
        if payload:
            values = list(_json_values(payload.get("data", payload)))
            code = next((v for v in values if re.fullmatch(r"\d{4,8}", v) and v != ignored), "")
            if code:
                return code
            state = " ".join(values).upper()
            if "CANCEL" in state or "NO_ACTIVATION" in state:
                return None
        if resp == "STATUS_CANCEL":
            log.warning("[%s] SMS activation cancelled", provider)
            return None
        time.sleep(5)
    return None


def sms_request_another(api_key: str, aid: str, provider: str = "") -> bool:
    provider = _resolve_provider(provider)
    try:
        resp = sms_api(api_key, "setStatus", {"id": aid, "status": "3"}, provider=provider)
        log.info("[%s] sms_request_another: %s", provider, resp)
        return "ACCESS_RETRY_GET" in resp
    except Exception:
        return False


def sms_cancel(api_key: str, aid: str, provider: str = "") -> None:
    provider = _resolve_provider(provider)
    try:
        resp = sms_api(api_key, "setStatus", {"id": aid, "status": "8"}, provider=provider)
        log.info("[%s] sms_cancel %s: %s", provider, aid, resp)
    except Exception:
        pass


def sms_done(api_key: str, aid: str, provider: str = "") -> None:
    provider = _resolve_provider(provider)
    try:
        sms_api(api_key, "setStatus", {"id": aid, "status": "6"}, provider=provider)
    except Exception:
        pass


def _phone_digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def sms_get_active_numbers(provider: str = "") -> list[dict[str, str]]:
    """Query the provider for currently active activations.

    Returns a list of dicts with ``activation_id`` and ``phone`` keys, newest
    first.  Both SMS-Activate text protocol (``ACCESS_NUMBER:aid:phone``) and
    common JSON shapes are parsed.
    """
    provider = _resolve_provider(provider)
    out: list[dict[str, str]] = []
    try:
        resp = sms_api("", "getActiveActivations", provider=provider)
    except Exception as exc:
        log.warning("[%s] getActiveActivations failed: %s", provider, exc)
        return out

    payload = _json_response_payload(resp)
    if payload:
        # Common JSON keys across SMS-Activate compatible providers.
        raw_list = (
            payload.get("activeActivations")
            or payload.get("activations")
            or payload.get("data")
            or []
        )
        if isinstance(raw_list, dict):
            raw_list = list(raw_list.values())
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            aid = str(
                item.get("activationId")
                or item.get("activation_id")
                or item.get("id")
                or item.get("activationID")
                or ""
            ).strip()
            phone = str(
                item.get("phoneNumber")
                or item.get("phone_number")
                or item.get("phone")
                or item.get("number")
                or ""
            ).strip()
            if aid and phone:
                if not phone.startswith("+"):
                    phone = f"+{phone}"
                out.append({"activation_id": aid, "phone": phone})
        return out

    # Fallback: parse text lines like "ACCESS_NUMBER:aid:phone".
    for line in resp.splitlines():
        line = line.strip()
        if line.startswith("ACCESS_NUMBER:"):
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[1] and parts[2]:
                phone = parts[2].strip()
                if not phone.startswith("+"):
                    phone = f"+{phone}"
                out.append({"activation_id": parts[1].strip(), "phone": phone})
    return out


# ========== API Error Helpers ==========

def is_waf_block(result: dict) -> bool:
    body = result.get("body", {})
    if isinstance(body, dict) and "raw" in body:
        return "WAF Block Page" in body["raw"]
    return False


def is_rate_limited(result: dict) -> bool:
    errors = result.get("body", {}).get("errors", [])
    if errors:
        code = errors[0].get("code", "")
        return "ratelimit" in code.lower() or "rate_limit" in code.lower()
    return result.get("status") == 429


def get_error_code(result: dict) -> str:
    body = result.get("body", {})
    if not isinstance(body, dict):
        return str(body)
    errors = body.get("errors", [])
    if errors:
        first = errors[0]
        return " ".join(str(first.get(k, "")) for k in ("code", "message") if first.get(k))
    error = body.get("error", {})
    if isinstance(error, dict):
        return " ".join(str(error.get(k, "")) for k in ("code", "description") if error.get(k))
    if "raw" in body:
        return str(body["raw"])
    return ""


def api_call_with_retry(fn, *args, max_retries: int = 2, **kwargs) -> dict:
    """Retry API call on WAF block or transient errors."""
    result = {}
    for attempt in range(max_retries + 1):
        result = fn(*args, **kwargs)
        if result["status"] in (200, 201, 204):
            return result
        if is_waf_block(result):
            if attempt < max_retries:
                wait = 5 * (attempt + 1)
                log.warning("WAF blocked, retrying in %ds... (%d/%d)", wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
        if is_rate_limited(result):
            if attempt < max_retries:
                wait = 30 * (attempt + 1)
                log.warning("Rate limited, retrying in %ds...", wait)
                time.sleep(wait)
                continue
        return result
    return result
