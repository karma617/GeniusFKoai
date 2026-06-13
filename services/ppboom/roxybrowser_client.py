from __future__ import annotations

import time
import re
from typing import Any
from urllib.parse import urlsplit

import requests


class RoxyBrowserApiError(RuntimeError):
    pass


_LAUNCH_CACHE: dict[str, dict[str, Any]] = {}


def normalize_api_base(value: str) -> str:
    base = str(value or "").strip()
    if not base:
        return "http://127.0.0.1:50000"
    if "://" not in base:
        base = f"http://{base}"
    return base.rstrip("/")


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
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


def _connection_info_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("rows", "list", "items", "data"):
        value = payload.get(key)
        rows = _connection_info_rows(value)
        if rows:
            return rows
    if any(payload.get(key) for key in ("ws", "websocket", "selenium", "http", "driver", "webdriver", "webdriver_path")):
        return [payload]
    return []


def _extract_browser_launch_info(data: dict[str, Any]) -> dict[str, Any]:
    ws = _first_non_empty(data.get("ws"), data.get("websocket"), data.get("selenium"))
    http = _first_non_empty(data.get("http"), data.get("debug_port"), data.get("port"))
    driver = _first_non_empty(data.get("driver"), data.get("webdriver"), data.get("webdriver_path"))
    debug_port = _extract_debug_port(http, ws, data.get("debug_port"), data.get("port"))
    return {
        "ws_selenium": ws,
        "ws_puppeteer": ws,
        "webdriver_path": driver,
        "debug_port": _first_non_empty(debug_port),
        "raw": data,
    }


def _cache_key(api_base: str, profile_id: str) -> str:
    return f"{normalize_api_base(api_base)}::{str(profile_id or '').strip()}"


def _request_json(
    method: str,
    url: str,
    *,
    api_key: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "token": str(api_key or "").strip(),
    }
    response = requests.request(method, url, params=params, json=json_body, headers=headers, timeout=timeout)
    if response.status_code >= 400:
        raise RoxyBrowserApiError(f"RoxyBrowser API http {response.status_code}: {response.text[:300]}")
    try:
        payload = response.json() or {}
    except Exception as exc:  # pragma: no cover
        raise RoxyBrowserApiError(f"RoxyBrowser API returned non-JSON response: {response.text[:300]}") from exc
    if not isinstance(payload, dict):
        raise RoxyBrowserApiError(f"RoxyBrowser API returned invalid payload: {payload!r}")
    if payload.get("code") not in (0, "0", None):
        detail = _first_non_empty(payload.get("msg"), payload.get("message"), payload.get("error"), payload)
        raise RoxyBrowserApiError(f"RoxyBrowser API returned error: {detail}")
    if "data" in payload:
        return payload.get("data")
    return payload


class RoxyBrowserLocalClient:
    def __init__(self, api_base: str, api_key: str, timeout: int = 30) -> None:
        self.api_base = normalize_api_base(api_base)
        self.api_key = str(api_key or "").strip()
        self.timeout = max(5, int(timeout or 30))
        if not self.api_key:
            raise RoxyBrowserApiError("RoxyBrowser API key is required")
        self._workspace_id: int | None = None

    def _cache_key(self, profile_id: str) -> str:
        return _cache_key(self.api_base, profile_id)

    def _get_cached_launch(self, profile_id: str) -> dict[str, Any] | None:
        normalized_id = str(profile_id or "").strip()
        if not normalized_id:
            return None
        cached = _LAUNCH_CACHE.get(self._cache_key(normalized_id))
        if not isinstance(cached, dict):
            return None
        http = _first_non_empty(
            cached.get("raw", {}).get("http") if isinstance(cached.get("raw"), dict) else "",
            f"http://127.0.0.1:{cached.get('debug_port')}" if str(cached.get("debug_port") or "").strip() else "",
        )
        if not http:
            return None
        try:
            response = requests.get(f"{http.rstrip('/')}/json/version", timeout=min(10, self.timeout))
            if response.status_code >= 400:
                return None
            launch = dict(cached)
            launch["reused"] = True
            launch["launch_action"] = "attached"
            return launch
        except Exception:
            return None

    def _remember_launch(self, profile_id: str, launch: dict[str, Any]) -> None:
        normalized_id = str(profile_id or "").strip()
        if not normalized_id or not isinstance(launch, dict):
            return
        if not launch.get("webdriver_path") or not launch.get("debug_port"):
            return
        _LAUNCH_CACHE[self._cache_key(normalized_id)] = dict(launch)

    def health(self) -> dict[str, Any]:
        return _request_json("GET", f"{self.api_base}/health", api_key=self.api_key, timeout=self.timeout)

    def workspace_project(self) -> dict[str, Any]:
        return _request_json(
            "GET",
            f"{self.api_base}/browser/workspace",
            api_key=self.api_key,
            params={"page_index": 1, "page_size": 1000},
            timeout=self.timeout,
        )

    def _resolve_workspace_id(self) -> int:
        if isinstance(self._workspace_id, int) and self._workspace_id > 0:
            return self._workspace_id
        payload = self.workspace_project()
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise RoxyBrowserApiError("RoxyBrowser workspace list is empty")
        first = rows[0] if isinstance(rows[0], dict) else {}
        workspace_id = int(first.get("id") or first.get("workspaceId") or 0)
        if workspace_id <= 0:
            raise RoxyBrowserApiError(f"RoxyBrowser workspace id missing from payload: {first}")
        self._workspace_id = workspace_id
        return workspace_id

    def get_active_profile(self, profile_id: str) -> dict[str, Any] | None:
        normalized_id = str(profile_id or "").strip()
        if not normalized_id:
            return None

        cached_launch = self._get_cached_launch(normalized_id)
        if cached_launch:
            return cached_launch

        request_variants = (
            {"dirIds": normalized_id},
            None,
        )
        for params in request_variants:
            try:
                info = _request_json(
                    "GET",
                    f"{self.api_base}/browser/connection_info",
                    api_key=self.api_key,
                    params=params,
                    timeout=self.timeout,
                )
            except Exception:
                continue
            rows = _connection_info_rows(info)
            for row in rows:
                row_dir_id = _first_non_empty(row.get("dirId"), row.get("dir_id"), row.get("id"))
                if row_dir_id and row_dir_id != normalized_id:
                    continue
                launch = _extract_browser_launch_info(row)
                if launch["webdriver_path"] and launch["debug_port"]:
                    launch["raw"] = {"connection_info": info, "matched": row}
                    launch["reused"] = True
                    launch["launch_action"] = "attached"
                    self._remember_launch(normalized_id, launch)
                    return launch
        return None

    def start_profile(self, profile_id: str) -> dict[str, Any]:
        normalized_id = str(profile_id or "").strip()
        if not normalized_id:
            raise RoxyBrowserApiError("RoxyBrowser window dirId is required")

        active_launch = self.get_active_profile(normalized_id)
        if active_launch:
            return active_launch

        candidates = [
            {
                "dirId": normalized_id,
                "args": ["--remote-allow-origins=*", "--disable-audio-output"],
                "forceOpen": False,
                "headless": False,
            }
        ]
        try:
            workspace_id = self._resolve_workspace_id()
            candidates.append(
                {
                    "workspaceId": workspace_id,
                    "dirId": normalized_id,
                    "args": ["--remote-allow-origins=*", "--disable-audio-output"],
                    "forceOpen": False,
                    "headless": False,
                }
            )
        except Exception:
            pass

        last_error: Exception | None = None
        for payload in candidates:
            try:
                data = _request_json(
                    "POST",
                    f"{self.api_base}/browser/open",
                    api_key=self.api_key,
                    json_body=payload,
                    timeout=self.timeout,
                )
                launch = _extract_browser_launch_info(data)
                if not launch["webdriver_path"] or not launch["debug_port"]:
                    active_launch = self.get_active_profile(normalized_id)
                    if active_launch:
                        launch = active_launch
                if not launch["webdriver_path"] or not launch["debug_port"]:
                    raise RoxyBrowserApiError(f"RoxyBrowser open response missing webdriver/debug port: {data}")
                launch["reused"] = bool(launch.get("reused"))
                launch["launch_action"] = "attached" if launch["reused"] else "started"
                self._remember_launch(normalized_id, launch)
                return launch
            except Exception as exc:
                last_error = exc
        if last_error:
            raise RoxyBrowserApiError(f"Failed to open RoxyBrowser window {normalized_id}: {last_error}")
        raise RoxyBrowserApiError(f"Failed to open RoxyBrowser window {normalized_id}")

    def stop_profile(self, profile_id: str) -> None:
        normalized_id = str(profile_id or "").strip()
        if not normalized_id:
            return
        try:
            _request_json(
                "POST",
                f"{self.api_base}/browser/close",
                api_key=self.api_key,
                json_body={"dirId": normalized_id},
                timeout=self.timeout,
            )
        except Exception:
            return
        finally:
            _LAUNCH_CACHE.pop(self._cache_key(normalized_id), None)

    def clear_profile_cache(self, profile_id: str, cache_types: list[str] | None = None) -> dict[str, Any]:
        normalized_id = str(profile_id or "").strip()
        if not normalized_id:
            return {"cleared": False}
        try:
            data = _request_json(
                "POST",
                f"{self.api_base}/browser/clear_local_cache",
                api_key=self.api_key,
                json_body={"dirIds": [normalized_id]},
                timeout=self.timeout,
            )
            return {"cleared": True, "raw": data}
        except Exception as exc:
            return {"cleared": False, "error": str(exc)}

    def wait_after_start(self, seconds: float = 2.0) -> None:
        time.sleep(max(0.5, float(seconds or 2.0)))
