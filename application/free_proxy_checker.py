from __future__ import annotations

import importlib.util
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from curl_cffi import requests as cffi_requests
except Exception:  # pragma: no cover - project requirements include curl_cffi
    import requests as cffi_requests  # type: ignore


DEFAULT_PROXY_CHECKER_DIR = r"D:\work\ai\proxy-checker"
TARGET_CHAT = "https://chat.openai.com/"
TARGET_API = "https://api.openai.com/v1/models"
TARGET_SIGNUP = (
    "https://auth0.openai.com/u/signup/authorize"
    "?client_id=DRivsnm2Mu42T3KOpqdtwB3NYviHYzwD"
    "&scope=openid%20email%20profile%20offline_access%20model.request%20model.read"
    "%20organization.read%20organization.write"
    "&response_type=code"
    "&redirect_uri=https%3A%2F%2Fchatgpt.com%2Fapi%2Fauth%2Fcallback%2Flogin-web"
    "&audience=https%3A%2F%2Fapi.openai.com%2Fv1"
    "&prompt=login&screen_hint=signup"
)
TARGET_IP = "https://api.ipify.org?format=json"

CF_BODY_INDICATORS = (
    "challenge-platform",
    "cf_chl_opt",
    "cf-chl-b",
    "cf-turnstile",
    "just a moment",
    "checking your browser",
    "verify you are human",
    "enable javascript and cookies",
    "challenges.cloudflare.com",
    "managed-challenge",
    "cf_mitigated",
)
OPENAI_REAL_PAGE_INDICATORS = (
    "__next",
    "chat.openai.com",
    "chatgpt",
    "prompt-textarea",
    "conversation-turn",
)
OPENAI_SIGNUP_INDICATORS = ("signup", "auth0", "create your account", "email", "password")
PROXY_PREFIXES = ("http://", "https://", "socks4://", "socks5://", "socks5h://")


def _proxy_checker_dir() -> Path:
    return Path(os.environ.get("PROXY_CHECKER_DIR") or DEFAULT_PROXY_CHECKER_DIR)


@lru_cache(maxsize=1)
def _load_fetch_module():
    module_path = _proxy_checker_dir() / "fetch_proxies.py"
    if not module_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("external_proxy_checker_fetch", module_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def _safe_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _request_get(url: str, **kwargs):
    try:
        return cffi_requests.get(url, impersonate="chrome", **kwargs)
    except TypeError:
        kwargs.pop("impersonate", None)
        return cffi_requests.get(url, **kwargs)


def _detect_cf_challenge(resp) -> tuple[bool, dict[str, Any]]:
    body = str(getattr(resp, "text", "") or "")
    body_lower = body.lower()
    headers = {str(k).lower(): str(v) for k, v in dict(getattr(resp, "headers", {}) or {}).items()}
    indicators: list[str] = []
    for item in CF_BODY_INDICATORS:
        if item in body_lower:
            indicators.append(f"body:{item}")
    for key in headers:
        if "cf-ray" in key or "cf-chl" in key or "cf-cache-status" in key:
            indicators.append(f"header:{key}")
    has_real_content = any(item in body_lower for item in OPENAI_REAL_PAGE_INDICATORS)
    challenge_type = None
    if indicators:
        if "turnstile" in body_lower:
            challenge_type = "turnstile"
        elif "managed-challenge" in body_lower or "challenge-platform" in body_lower:
            challenge_type = "managed"
        elif "just a moment" in body_lower:
            challenge_type = "js"
        elif int(getattr(resp, "status_code", 0) or 0) == 403:
            challenge_type = "block"
        else:
            challenge_type = "unknown"
    return bool(indicators), {
        "cf_challenge_type": "soft_challenge" if indicators and has_real_content else challenge_type,
        "cf_indicators": indicators,
        "has_real_content": has_real_content,
        "response_size": len(body),
    }


def _detect_signup_access(resp) -> tuple[bool, str]:
    status = int(getattr(resp, "status_code", 0) or 0)
    body = str(getattr(resp, "text", "") or "").lower()
    if status == 200:
        if any(item in body for item in OPENAI_SIGNUP_INDICATORS):
            return True, "signup_accessible"
        if "challenge-platform" in body or "just a moment" in body:
            return False, "cf_challenge_on_signup"
        return True, "signup_200"
    if status in (301, 302, 303, 307, 308):
        return True, f"signup_redirect_{status}"
    if status == 403:
        return False, "signup_blocked_403"
    if status == 407:
        return False, "proxy_auth_required"
    return False, f"signup_error_{status}"


def _classify_error(err: str) -> str:
    lowered = str(err or "").lower()
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if "refused" in lowered:
        return "connection_refused"
    if "resolve" in lowered or "dns" in lowered:
        return "dns_error"
    if "socks" in lowered:
        return "socks_error"
    if "ssl" in lowered or "certificate" in lowered:
        return "tls_error"
    if "auth" in lowered or "407" in lowered:
        return "proxy_auth_required"
    if "connection reset" in lowered:
        return "connection_reset"
    if "eof" in lowered:
        return "connection_closed"
    return str(err or "")[:120]


def _do_check_once(proxy_url: str, *, timeout: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "valid": False,
        "latency": None,
        "error": None,
        "status_code": None,
        "ip": None,
        "api_reachable": None,
        "cf_bypass": False,
        "cf_challenge": False,
        "cf_challenge_type": None,
        "cf_indicators": [],
        "registration_ready": False,
        "registration_detail": None,
        "checks_detail": {},
    }
    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        started = time.time()
        resp = _request_get(TARGET_CHAT, proxies=proxies, timeout=timeout, allow_redirects=True)
        result["latency"] = round((time.time() - started) * 1000)
        result["status_code"] = int(resp.status_code)
        is_cf, cf = _detect_cf_challenge(resp)
        result["checks_detail"]["chat"] = {
            "status": int(resp.status_code),
            "cf_detected": is_cf,
            "cf_type": cf.get("cf_challenge_type"),
            "has_content": cf.get("has_real_content", False),
            "size": cf.get("response_size", 0),
        }
        if int(resp.status_code) in (200, 301, 302, 303, 307, 308):
            if is_cf and not cf.get("has_real_content"):
                result["cf_challenge"] = True
                result["cf_challenge_type"] = cf.get("cf_challenge_type")
                result["cf_indicators"] = cf.get("cf_indicators", [])
                result["error"] = f"cf_blocked:{cf.get('cf_challenge_type') or 'unknown'}"
            else:
                result["valid"] = True
                result["cf_bypass"] = True
                result["cf_challenge"] = bool(is_cf)
                result["cf_challenge_type"] = cf.get("cf_challenge_type")
        else:
            result["error"] = f"http_{resp.status_code}"
            return result
    except Exception as exc:
        result["error"] = _classify_error(str(exc))
        return result

    if not result["valid"] and not result["cf_bypass"]:
        return result

    try:
        api_resp = _request_get(TARGET_API, proxies=proxies, timeout=timeout)
        result["api_reachable"] = int(api_resp.status_code) in (200, 401)
        result["checks_detail"]["api"] = {
            "status": int(api_resp.status_code),
            "reachable": result["api_reachable"],
        }
    except Exception:
        result["api_reachable"] = False
        result["checks_detail"]["api"] = {"status": None, "reachable": False}

    try:
        signup_resp = _request_get(TARGET_SIGNUP, proxies=proxies, timeout=timeout, allow_redirects=True)
        reg_ok, reg_detail = _detect_signup_access(signup_resp)
        result["registration_ready"] = reg_ok
        result["registration_detail"] = reg_detail
        result["checks_detail"]["signup"] = {
            "status": int(signup_resp.status_code),
            "accessible": reg_ok,
            "detail": reg_detail,
        }
    except Exception as exc:
        result["registration_detail"] = f"signup_error:{_classify_error(str(exc))}"
        result["checks_detail"]["signup"] = {"status": None, "accessible": False}

    try:
        ip_resp = _request_get(TARGET_IP, proxies=proxies, timeout=min(timeout, 6))
        if int(ip_resp.status_code) == 200:
            data = ip_resp.json()
            result["ip"] = data.get("ip") or data.get("origin")
    except Exception:
        pass
    return result


def _auto_detect(raw_proxy: str, *, timeout: int) -> tuple[str | None, bool]:
    for prefix in PROXY_PREFIXES:
        candidate = prefix + raw_proxy
        checked = _do_check_once(candidate, timeout=timeout)
        if checked.get("valid") or (checked.get("cf_bypass") and checked.get("status_code")):
            return candidate, True
    return None, False


def check_proxy(proxy: str, *, rounds: int = 1, timeout: int = 10) -> dict[str, Any] | None:
    rounds = _safe_int(rounds, 1, minimum=1, maximum=3)
    timeout = _safe_int(timeout, 10, minimum=3, maximum=30)
    original = str(proxy or "").strip()
    if not original or original.startswith("#"):
        return None
    checked_proxy = original
    if not checked_proxy.startswith(PROXY_PREFIXES):
        detected, found = _auto_detect(checked_proxy, timeout=timeout)
        if not found or not detected:
            return {
                "proxy": original,
                "original": original,
                "valid": False,
                "unstable": False,
                "grade": "F",
                "checks_passed": 0,
                "checks_total": rounds,
                "error": "all_protocols_failed",
                "latency": None,
                "status_code": None,
                "api_reachable": None,
                "cf_bypass": False,
                "registration_ready": False,
                "registration_detail": None,
                "detected_protocol": None,
                "timestamp": time.time(),
            }
        checked_proxy = detected

    passed = 0
    latencies: list[int] = []
    last: dict[str, Any] | None = None
    for _ in range(rounds):
        last = _do_check_once(checked_proxy, timeout=timeout)
        if last.get("valid"):
            passed += 1
            if last.get("latency"):
                latencies.append(int(last["latency"]))

    avg_latency = round(sum(latencies) / len(latencies)) if latencies else (last or {}).get("latency")
    chat_ok = passed == rounds
    api_ok = bool((last or {}).get("api_reachable"))
    reg_ok = bool((last or {}).get("registration_ready"))
    cf_ok = bool((last or {}).get("cf_bypass"))
    if chat_ok and api_ok and reg_ok and cf_ok:
        grade = "A"
    elif chat_ok and api_ok and cf_ok:
        grade = "B"
    elif chat_ok and api_ok:
        grade = "C"
    elif chat_ok:
        grade = "D"
    else:
        grade = "F"
    valid = chat_ok and api_ok
    return {
        "proxy": checked_proxy,
        "original": original,
        "valid": valid,
        "unstable": (chat_ok or api_ok) and not valid and passed > 0,
        "grade": grade,
        "checks_passed": passed,
        "checks_total": rounds,
        "error": (last or {}).get("error") if last and not last.get("valid") else None,
        "latency": avg_latency,
        "status_code": (last or {}).get("status_code"),
        "ip": (last or {}).get("ip"),
        "api_reachable": (last or {}).get("api_reachable"),
        "cf_bypass": (last or {}).get("cf_bypass", False),
        "cf_challenge": (last or {}).get("cf_challenge", False),
        "cf_challenge_type": (last or {}).get("cf_challenge_type"),
        "cf_indicators": (last or {}).get("cf_indicators", []),
        "registration_ready": (last or {}).get("registration_ready", False),
        "registration_detail": (last or {}).get("registration_detail"),
        "detected_protocol": checked_proxy.split("://", 1)[0] if "://" in checked_proxy else None,
        "timestamp": time.time(),
        "checks_detail": (last or {}).get("checks_detail", {}),
    }


def get_free_proxy_sources() -> dict[str, Any]:
    module = _load_fetch_module()
    sources = []
    if module is not None:
        for item in getattr(module, "PROXY_SOURCES", []) or []:
            sources.append({"id": str(item.get("id") or ""), "name": str(item.get("name") or "")})
    return {
        "available": module is not None,
        "path": str(_proxy_checker_dir()),
        "sources": sources,
    }


def fetch_free_proxies(source: str, *, limit: int = 200) -> dict[str, Any]:
    module = _load_fetch_module()
    if module is None:
        return {
            "ok": False,
            "error": "proxy_checker_fetch_unavailable",
            "path": str(_proxy_checker_dir()),
            "proxies": [],
            "count": 0,
        }
    source_id = str(source or "proxifly").strip() or "proxifly"
    limit = _safe_int(limit, 200, minimum=1, maximum=5000)
    source_items = list(getattr(module, "PROXY_SOURCES", []) or [])
    targets = [source_id]
    if source_id == "all":
        targets = [str(item.get("id") or "") for item in source_items if item.get("id")]

    collected: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    remaining = limit
    per_source_limit = limit
    if len(targets) > 1:
        per_source_limit = max(1, (limit + len(targets) - 1) // len(targets))
    for target in targets:
        if remaining <= 0:
            break
        try:
            proxies, source_name, err = module.fetch_proxies(target, min(remaining, per_source_limit))
        except Exception as exc:
            errors.append({"source": target, "error": _classify_error(str(exc))})
            continue
        if err:
            errors.append({"source": str(source_name or target), "error": str(err)})
            continue
        for item in proxies or []:
            raw = item.get("proxy") if isinstance(item, dict) else str(item)
            proxy_url = str(raw or "").strip()
            if not proxy_url or proxy_url.lower() in seen:
                continue
            seen.add(proxy_url.lower())
            if isinstance(item, dict):
                collected.append(dict(item))
            else:
                collected.append({"proxy": proxy_url})
            remaining -= 1
            if remaining <= 0:
                break
    return {
        "ok": True,
        "source": source_id,
        "count": len(collected),
        "proxies": collected,
        "errors": errors,
    }


def check_free_proxies(
    proxies: list[str],
    *,
    rounds: int = 1,
    timeout: int = 10,
    concurrency: int = 20,
    limit: int = 120,
) -> dict[str, Any]:
    rounds = _safe_int(rounds, 1, minimum=1, maximum=3)
    timeout = _safe_int(timeout, 10, minimum=3, maximum=30)
    concurrency = _safe_int(concurrency, 20, minimum=1, maximum=50)
    limit = _safe_int(limit, 120, minimum=1, maximum=500)
    queue: list[str] = []
    seen: set[str] = set()
    for item in proxies or []:
        proxy_url = str(item or "").strip()
        key = proxy_url.lower()
        if not proxy_url or key in seen:
            continue
        seen.add(key)
        queue.append(proxy_url)
        if len(queue) >= limit:
            break

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(concurrency, max(len(queue), 1))) as executor:
        futures = {executor.submit(check_proxy, proxy, rounds=rounds, timeout=timeout): proxy for proxy in queue}
        for future in as_completed(futures):
            item = future.result()
            if item is not None:
                results.append(item)

    results.sort(key=lambda item: (0 if item.get("valid") else 1, str(item.get("proxy") or "")))
    return {
        "total": len(queue),
        "checked": len(results),
        "valid_count": sum(1 for item in results if item.get("valid")),
        "unstable_count": sum(1 for item in results if item.get("unstable")),
        "registration_count": sum(1 for item in results if item.get("registration_ready")),
        "cf_bypass_count": sum(1 for item in results if item.get("cf_bypass")),
        "results": results,
    }
