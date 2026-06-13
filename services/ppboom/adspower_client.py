from __future__ import annotations

import time
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests


class AdsPowerApiError(RuntimeError):
    pass


def normalize_api_base(value: str) -> str:
    base = str(value or "").strip()
    if not base:
        return "http://127.0.0.1:50325"
    if "://" not in base:
        base = f"http://{base}"
    parts = urlsplit(base)
    if (parts.hostname or "").lower() == "local.adspower.net":
        netloc = "127.0.0.1"
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        base = urlunsplit((parts.scheme or "http", netloc, parts.path, parts.query, parts.fragment))
    return base.rstrip("/")


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _extract_nested(data: Any, keys: list[str]) -> str:
    if not isinstance(data, dict):
        return ""
    for key in keys:
        text = _first_non_empty(data.get(key))
        if text:
            return text
    for value in data.values():
        text = _extract_nested(value, keys)
        if text:
            return text
    return ""


def _extract_debug_port(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        candidate = text if "://" in text else f"http://{text}"
        try:
            parts = urlsplit(candidate)
            if parts.port:
                return str(parts.port)
        except Exception:
            pass
        match = re.search(r":(\d{2,5})(?:/|$)", text)
        if match:
            return match.group(1)
        if text.isdigit():
            return text
    return ""


def _response_data(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload
    return {}


def _is_success(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("success") is True:
        return True
    if payload.get("code") in (0, "0"):
        return True
    if payload.get("status") in ("success", "ok", 0, "0"):
        return True
    data = payload.get("data")
    return isinstance(data, dict) and not payload.get("msg") and not payload.get("message")


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    response = requests.request(method, url, params=params, json=json_body, headers=headers, timeout=timeout)
    if response.status_code >= 400:
        raise AdsPowerApiError(f"AdsPower API http {response.status_code}: {response.text[:300]}")
    try:
        payload = response.json() or {}
    except Exception as exc:  # pragma: no cover
        raise AdsPowerApiError(f"AdsPower API returned non-JSON response: {response.text[:300]}") from exc
    if not _is_success(payload):
        detail = _first_non_empty(payload.get("msg"), payload.get("message"), payload.get("error"), payload)
        raise AdsPowerApiError(f"AdsPower API returned error: {detail}")
    return _response_data(payload)


def _extract_browser_launch_info(data: dict[str, Any]) -> dict[str, Any]:
    ws_selenium = _extract_nested(data, ["selenium"])
    ws_puppeteer = _extract_nested(data, ["puppeteer"])
    webdriver_path = _extract_nested(data, ["webdriver", "webdriver_path", "driver", "driver_path"])
    debug_port = _extract_debug_port(
        data.get("debug_port"),
        data.get("debugPort"),
        data.get("port"),
        data.get("http"),
        _extract_nested(data, ["debug_port", "debugPort", "port", "http"]),
        ws_selenium,
        ws_puppeteer,
    )
    return {
        "ws_selenium": ws_selenium,
        "ws_puppeteer": ws_puppeteer,
        "webdriver_path": webdriver_path,
        "debug_port": debug_port,
        "raw": data,
    }


class AdsPowerLocalClient:
    def __init__(self, api_base: str, timeout: int = 30, api_key: str = "") -> None:
        self.api_base = normalize_api_base(api_base)
        self.timeout = max(5, int(timeout or 30))
        self.api_key = str(api_key or "").strip()

    def _headers(self) -> dict[str, str] | None:
        if not self.api_key:
            return None
        return {"token": self.api_key}

    def health(self) -> dict[str, Any]:
        candidates = [
            "/status",
            "/api/v1/browser/status",
        ]
        last_error: Exception | None = None
        for path in candidates:
            try:
                return _request_json("GET", f"{self.api_base}{path}", timeout=self.timeout, headers=self._headers())
            except Exception as exc:
                last_error = exc
        if last_error:
            raise AdsPowerApiError(f"AdsPower Local API unavailable: {last_error}")
        raise AdsPowerApiError("AdsPower Local API unavailable")

    def get_active_profile(self, profile_id: str) -> dict[str, Any] | None:
        normalized_id = str(profile_id or "").strip()
        if not normalized_id:
            return None

        candidates = [
            ("GET", "/api/v1/browser/active", {"user_id": normalized_id}),
            ("GET", "/api/v1/browser/active", {"profile_id": normalized_id}),
            ("POST", "/api/v2/browser-profile/active", {"profile_id": normalized_id, "user_id": normalized_id}),
        ]
        for method, path, payload in candidates:
            try:
                if method == "POST":
                    data = _request_json(
                        method,
                        f"{self.api_base}{path}",
                        json_body=payload,
                        timeout=self.timeout,
                        headers=self._headers(),
                    )
                else:
                    data = _request_json(
                        method,
                        f"{self.api_base}{path}",
                        params=payload,
                        timeout=self.timeout,
                        headers=self._headers(),
                    )
                launch = _extract_browser_launch_info(data)
                if launch["webdriver_path"] and launch["debug_port"]:
                    launch["reused"] = True
                    launch["launch_action"] = "attached"
                    return launch
            except Exception:
                continue
        return None

    def start_profile(self, profile_id: str) -> dict[str, Any]:
        normalized_id = str(profile_id or "").strip()
        if not normalized_id:
            raise AdsPowerApiError("AdsPower profile id is required")

        active_launch = self.get_active_profile(normalized_id)
        if active_launch:
            return active_launch

        candidates = [
            ("POST", "/api/v2/browser-profile/start", {"profile_id": normalized_id, "user_id": normalized_id, "open_tabs": 1}),
            ("GET", "/api/v1/browser/start", {"user_id": normalized_id}),
        ]
        last_error: Exception | None = None
        for method, path, payload in candidates:
            try:
                if method == "POST":
                    data = _request_json(method, f"{self.api_base}{path}", json_body=payload, timeout=self.timeout, headers=self._headers())
                else:
                    data = _request_json(method, f"{self.api_base}{path}", params=payload, timeout=self.timeout, headers=self._headers())
                launch = _extract_browser_launch_info(data)
                if not launch["webdriver_path"] or not launch["debug_port"]:
                    raise AdsPowerApiError(f"AdsPower start response missing webdriver/debug port: {data}")
                launch["reused"] = False
                launch["launch_action"] = "started"
                return launch
            except Exception as exc:
                last_error = exc
        if last_error:
            raise AdsPowerApiError(f"Failed to start AdsPower profile {normalized_id}: {last_error}")
        raise AdsPowerApiError(f"Failed to start AdsPower profile {normalized_id}")

    def stop_profile(self, profile_id: str) -> None:
        normalized_id = str(profile_id or "").strip()
        if not normalized_id:
            return
        candidates = [
            ("POST", "/api/v2/browser-profile/stop", {"profile_id": normalized_id, "user_id": normalized_id}),
            ("GET", "/api/v1/browser/stop", {"user_id": normalized_id}),
        ]
        for method, path, payload in candidates:
            try:
                if method == "POST":
                    _request_json(method, f"{self.api_base}{path}", json_body=payload, timeout=self.timeout, headers=self._headers())
                else:
                    _request_json(method, f"{self.api_base}{path}", params=payload, timeout=self.timeout, headers=self._headers())
                return
            except Exception:
                continue

    def clear_profile_cache(self, profile_id: str, cache_types: list[str] | None = None) -> dict[str, Any]:
        normalized_id = str(profile_id or "").strip()
        if not normalized_id:
            return {"cleared": False}
        types = [str(item or "").strip() for item in (cache_types or ["cookie", "local_storage", "history", "cache"])]
        payload = {
            "profile_id": [normalized_id],
            "type": [item for item in types if item],
        }
        candidates = [
            ("POST", "/api/v2/browser-profile/delete-cache", payload),
            ("POST", "/api/v2/browser-profile/clear-cache", payload),
            ("POST", "/api/v1/browser/delete-cache", payload),
            ("POST", "/api/v1/browser/clear-cache", payload),
        ]
        last_error: Exception | None = None
        for method, path, body in candidates:
            try:
                data = _request_json(method, f"{self.api_base}{path}", json_body=body, timeout=self.timeout, headers=self._headers())
                return {"cleared": True, "raw": data}
            except Exception as exc:
                last_error = exc
                if "404" in str(exc):
                    continue
        if last_error:
            return {"cleared": False, "error": str(last_error)}
        return {"cleared": False}

    def wait_after_start(self, seconds: float = 2.0) -> None:
        time.sleep(max(0.5, float(seconds or 2.0)))
