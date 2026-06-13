from __future__ import annotations

import json
import random
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from .adspower_client import AdsPowerLocalClient
from .roxybrowser_client import RoxyBrowserLocalClient


class AdsPowerWorkerError(RuntimeError):
    pass


PAYPAL_STAGE_OUTSIDE = "outside_paypal"
PAYPAL_STAGE_LOGIN = "pay_login"
PAYPAL_STAGE_ACCOUNT_CREATE_EMAIL = "account_create_email"
PAYPAL_STAGE_GUEST_CHECKOUT = "guest_checkout"
PAYPAL_STAGE_VERIFICATION = "verification"
PAYPAL_STAGE_REVIEW = "review_consent"
PAYPAL_STAGE_REDIRECTING = "redirecting"
PAYPAL_STAGE_APPROVAL = "approval"
PAYPAL_STAGE_WALLET_MODAL = "wallet_modal"
PAYPAL_STAGE_BLOCKED = "blocked"
PAYPAL_STAGE_GENERIC_ERROR = "generic_error"
PAYPAL_STAGE_CARD_LINKED_ERROR = "card_linked_error"
PAYPAL_STAGE_UNKNOWN = "unknown"


def _normalize_browser_backend(value: str = "") -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "roxybrowser":
        return "roxybrowser"
    if normalized == "adspower":
        return "adspower"
    return "adspower"


def _create_browser_client(
    *,
    browser_backend: str,
    api_base: str,
    ads_power_api_key: str = "",
    roxybrowser_api_key: str = "",
):
    normalized_backend = _normalize_browser_backend(browser_backend)
    if normalized_backend == "roxybrowser":
        return RoxyBrowserLocalClient(api_base, roxybrowser_api_key)
    return AdsPowerLocalClient(api_base, api_key=str(ads_power_api_key or "").strip())


def _ensure_supported_launch(browser_backend: str, launch: dict[str, Any]) -> None:
    normalized_backend = _normalize_browser_backend(browser_backend)
    if normalized_backend != "roxybrowser":
        return
    if _detect_driver_family(launch) == "firefox":
        raise AdsPowerWorkerError(
            "RoxyBrowser 当前 dirId 使用的是 Firefox 内核窗口（geckodriver / firefox-bin），"
            "PPBoom 目前只支持 RoxyBrowser 的 Chrome 内核窗口。"
            "请改用 connection_info.driver 指向 chromedriver.exe 的窗口。"
        )


def read_chatgpt_session_via_browser(
    *,
    browser_backend: str = "adspower",
    api_base: str,
    ads_power_api_key: str = "",
    roxybrowser_api_key: str = "",
    profile_id: str,
    clear_cache_before_start: bool = False,
    close_profile_on_finish: bool = False,
) -> dict[str, Any]:
    client = _create_browser_client(
        browser_backend=browser_backend,
        api_base=api_base,
        ads_power_api_key=ads_power_api_key,
        roxybrowser_api_key=roxybrowser_api_key,
    )
    if clear_cache_before_start:
        client.clear_profile_cache(profile_id, ["cookie", "local_storage", "cache"])
    launch = client.start_profile(profile_id)
    _ensure_supported_launch(browser_backend, launch)
    client.wait_after_start()

    driver = None
    try:
        driver = _create_connected_driver(launch)
        if clear_cache_before_start:
            _prepare_connected_browser_for_fresh_flow(driver)
        driver.get("https://chatgpt.com/")
        script = """
const callback = arguments[arguments.length - 1];
(async () => {
  try {
    const response = await fetch('/api/auth/session', {
      credentials: 'include',
      cache: 'no-store',
    });
    const text = await response.text();
    let data = {};
    try {
      data = JSON.parse(text || '{}');
    } catch {
      data = { rawText: text };
    }
    callback({ ok: true, status: response.status, data });
  } catch (error) {
    callback({ ok: false, error: String(error && error.message ? error.message : error) });
  }
})();
"""
        result = driver.execute_async_script(script)
        if not isinstance(result, dict) or not result.get("ok"):
            raise AdsPowerWorkerError(
                f"Failed to read ChatGPT session in AdsPower profile {profile_id}: "
                f"{result.get('error') if isinstance(result, dict) else result}"
            )
        session = result.get("data") if isinstance(result.get("data"), dict) else {}
        access_token = str(session.get("accessToken") or "").strip()
        if not access_token:
            raise AdsPowerWorkerError(
                f"AdsPower profile {profile_id} is not logged in to ChatGPT or accessToken is unavailable"
            )
        return {
            "access_token": access_token,
            "session": session,
            "page_url": str(driver.current_url or "").strip(),
            "page_title": str(driver.title or "").strip(),
            "launch": launch,
        }
    finally:
        try:
            if driver is not None:
                driver.quit()
        except Exception:
            pass
        if close_profile_on_finish:
            client.stop_profile(profile_id)


def build_session_log_summary(session_info: dict[str, Any]) -> str:
    session = session_info.get("session") if isinstance(session_info, dict) else {}
    user_email = ""
    if isinstance(session, dict):
        user = session.get("user")
        if isinstance(user, dict):
            user_email = str(user.get("email") or "").strip()
    title = str(session_info.get("page_title") or "").strip()
    url = str(session_info.get("page_url") or "").strip()
    parts = []
    if user_email:
        parts.append(f"email={user_email}")
    if title:
        parts.append(f"title={title}")
    if url:
        parts.append(f"url={url}")
    return ", ".join(parts) or "session captured"


def _detect_driver_family(launch: dict[str, Any]) -> str:
    webdriver_path = str(launch.get("webdriver_path") or "").strip().lower()
    if "geckodriver" in webdriver_path or "firefox" in webdriver_path:
        return "firefox"
    return "chromium"


def _extract_debug_http_base(launch: dict[str, Any]) -> str:
    raw = launch.get("raw") if isinstance(launch, dict) else {}
    candidates: list[str] = []
    if isinstance(raw, dict):
        for key in ("http", "debug_http", "debugHttp"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        connection_info = raw.get("connection_info")
        if isinstance(connection_info, list):
            for item in connection_info:
                if isinstance(item, dict):
                    value = item.get("http")
                    if isinstance(value, str) and value.strip():
                        candidates.append(value.strip())
        matched = raw.get("matched")
        if isinstance(matched, dict):
            value = matched.get("http")
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
    for candidate in candidates:
        if candidate.startswith("http://") or candidate.startswith("https://"):
            return candidate.rstrip("/")
    debug_port = str(launch.get("debug_port") or "").strip()
    if debug_port:
        return f"http://127.0.0.1:{debug_port}"
    return ""


def _open_url_via_debug_target(launch: dict[str, Any], target_url: str) -> dict[str, Any]:
    http_base = _extract_debug_http_base(launch)
    normalized_url = str(target_url or "").strip()
    if not http_base or not normalized_url:
        raise AdsPowerWorkerError("debug target open requires http base and target_url")

    request_path = f"/json/new?{quote(normalized_url, safe=':/?&=%#')}"
    last_error: Exception | None = None
    for method in ("PUT", "GET"):
        try:
            response = requests.request(method, f"{http_base}{request_path}", timeout=30)
            if response.status_code >= 400:
                raise AdsPowerWorkerError(
                    f"debug target open failed: http {response.status_code} {response.text[:200]}"
                )
            if not response.text.strip():
                return {"url": normalized_url}
            try:
                return response.json() or {"url": normalized_url}
            except Exception:
                return {"url": normalized_url}
        except Exception as exc:
            last_error = exc
            continue
    raise AdsPowerWorkerError(f"debug target open failed: {last_error}")


def _navigate_connected_browser_to_url(driver, launch: dict[str, Any], target_url: str, *, browser_backend: str = "adspower") -> dict[str, Any]:
    normalized_url = str(target_url or "").strip()
    if not normalized_url:
        raise AdsPowerWorkerError("target_url is required")

    driver_family = _detect_driver_family(launch)
    normalized_backend = _normalize_browser_backend(browser_backend)
    if driver_family == "chromium" and normalized_backend == "roxybrowser":
        try:
            data = _open_url_via_debug_target(launch, normalized_url)
            time.sleep(1.2)
            _switch_to_checkout_window(driver)
            return {
                "url": str(driver.current_url or data.get("url") or normalized_url).strip(),
                "title": str(driver.title or "").strip(),
                "target_id": str(data.get("id") or "").strip(),
            }
        except Exception:
            pass

    driver.get(normalized_url)
    return {
        "url": str(driver.current_url or normalized_url).strip(),
        "title": str(driver.title or "").strip(),
        "target_id": "",
    }


def _create_connected_driver(launch: dict[str, Any]):
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        from selenium.webdriver.chrome.service import Service as ChromeService
        from selenium.webdriver.firefox.options import Options as FirefoxOptions
        from selenium.webdriver.firefox.service import Service as FirefoxService
    except Exception as exc:  # pragma: no cover
        raise AdsPowerWorkerError(
            "AdsPower bridge requires selenium. Please install PPBoom requirements first."
        ) from exc

    debug_port = str(launch.get("debug_port") or "").strip()
    webdriver_path = str(launch.get("webdriver_path") or "").strip()
    driver_family = _detect_driver_family(launch)
    if not debug_port or (driver_family != "firefox" and not webdriver_path):
      raise AdsPowerWorkerError("AdsPower launch data missing debug_port/webdriver_path")
    if driver_family == "firefox":
        options = FirefoxOptions()
        candidate_ports = []
        for candidate in ("2828", debug_port):
            candidate_text = str(candidate or "").strip()
            if candidate_text and candidate_text not in candidate_ports:
                candidate_ports.append(candidate_text)
        last_error: Exception | None = None
        for marionette_port in candidate_ports:
            try:
                service = FirefoxService(
                    executable_path=webdriver_path,
                    service_args=[
                        "--connect-existing",
                        "--marionette-host",
                        "127.0.0.1",
                        "--marionette-port",
                        str(marionette_port),
                    ],
                )
                driver = webdriver.Firefox(service=service, options=options)
                driver.set_page_load_timeout(60)
                return driver
            except Exception as exc:
                last_error = exc
        raise AdsPowerWorkerError(
            f"Firefox attach failed for ports {', '.join(candidate_ports)}: {last_error}"
        )
    else:
        options = ChromeOptions()
        options.add_experimental_option("debuggerAddress", f"127.0.0.1:{debug_port}")
        service = ChromeService(executable_path=webdriver_path)
        driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(60)
    return driver


def _origin_from_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except Exception:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}".lower()


def _clear_current_origin_storage(driver) -> None:
    script = """
const done = arguments[arguments.length - 1];
(async () => {
  try { window.localStorage && window.localStorage.clear(); } catch {}
  try { window.sessionStorage && window.sessionStorage.clear(); } catch {}
  try {
    if (window.caches && caches.keys) {
      const keys = await caches.keys();
      await Promise.all(keys.map((key) => caches.delete(key)));
    }
  } catch {}
  try {
    if (navigator.serviceWorker && navigator.serviceWorker.getRegistrations) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map((reg) => reg.unregister()));
    }
  } catch {}
  try {
    if (window.indexedDB && indexedDB.databases) {
      const dbs = await indexedDB.databases();
      await Promise.all((dbs || []).map((db) => new Promise((resolve) => {
        if (!db || !db.name) return resolve(false);
        const req = indexedDB.deleteDatabase(db.name);
        req.onsuccess = () => resolve(true);
        req.onerror = () => resolve(false);
        req.onblocked = () => resolve(false);
      })));
    }
  } catch {}
  done(true);
})().catch(() => done(false));
"""
    try:
        driver.execute_async_script(script)
    except Exception:
        pass


def _clear_browser_cache_and_storage(driver, origins: set[str]) -> None:
    try:
        driver.delete_all_cookies()
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Network.clearBrowserCache", {})
    except Exception:
        pass
    storage_origins = {
        "https://www.paypal.com",
        "https://paypal.com",
        "https://checkout.stripe.com",
        "https://pay.openai.com",
        "https://pm-redirects.stripe.com",
    }
    storage_origins.update(origin for origin in origins if origin)
    for origin in sorted(storage_origins):
        try:
            driver.execute_cdp_cmd(
                "Storage.clearDataForOrigin",
                {"origin": origin, "storageTypes": "all"},
            )
        except Exception:
            continue


def _prepare_connected_browser_for_fresh_flow(driver, *, close_extra_tabs: bool = False) -> None:
    try:
        previous_script_timeout = None
        driver.set_script_timeout(10)
    except Exception:
        previous_script_timeout = None

    try:
        handles = list(driver.window_handles or [])
    except Exception:
        handles = []
    origins: set[str] = set()
    for handle in handles:
        try:
            driver.switch_to.window(handle)
            origins.add(_origin_from_url(str(driver.current_url or "")))
            _clear_current_origin_storage(driver)
        except Exception:
            continue
    _clear_browser_cache_and_storage(driver, origins)

    if close_extra_tabs and handles:
        keep_handle = handles[-1]
        for handle in handles:
            if handle == keep_handle:
                continue
            try:
                driver.switch_to.window(handle)
                driver.close()
            except Exception:
                pass
        try:
            driver.switch_to.window(keep_handle)
        except Exception:
            try:
                remaining = list(driver.window_handles or [])
                if remaining:
                    driver.switch_to.window(remaining[-1])
            except Exception:
                pass
        try:
            driver.get("about:blank")
        except Exception:
            pass
    elif handles:
        for handle in reversed(handles):
            try:
                driver.switch_to.window(handle)
                break
            except Exception:
                continue

    try:
        driver.set_script_timeout(previous_script_timeout or 30)
    except Exception:
        pass


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _is_hosted_completion_text(body_text: str) -> bool:
    body = _normalize_text(body_text or "")
    return bool(re.search(
        r"you're all set|you’re all set|you(?:'ve|’ve)\s+either\s+completed\s+your\s+payment\s+or\s+this\s+checkout\s+session\s+has\s+timed\s+out|you(?:'re|’re)\s+all\s+done\s+here|payment is complete|completed your payment|您已完成|已完成付款|checkout session has expired|checkout session has timed out|结账会话已超时",
        body,
        re.I,
    ))


def _page_snapshot(driver) -> dict[str, Any]:
    return driver.execute_script(
        """
const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
const bodyText = norm(document.body && (document.body.innerText || document.body.textContent || ''));
const url = String(location.href || '');
let redirectStatus = '';
try {
  redirectStatus = new URL(url).searchParams.get('redirect_status') || '';
} catch {}
return {
  url,
  host: String(location.host || ''),
  pathname: String(location.pathname || ''),
  title: String(document.title || ''),
  bodyText,
  redirectStatus,
  hasConsentButton: Boolean(document.getElementById('consentButton') || document.querySelector('button[data-testid="consentButton"]')),
  hasApproveButton: Boolean(Array.from(document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]')).find((el) => {
    const text = norm([el.textContent, el.value, el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('placeholder'), el.getAttribute('name'), el.id].filter(Boolean).join(' '));
    return /同意并继续|同意|授权|确认并继续|同意して続行|同意して支払う|承認|続行|agree\\s*(?:and)?\\s*continue|accept|authorize|agree|confirm|pay\\s*now/i.test(text);
  })),
};
        """
    )


def _click_first_matching(driver, patterns: list[str]) -> bool:
    script = """
const patterns = (Array.isArray(arguments[0]) ? arguments[0] : []).map((pattern) => new RegExp(pattern, 'i'));
const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]'));
const find = candidates.find((el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  if (el.hidden || el.disabled || el.getAttribute('aria-disabled') === 'true' || style.display === 'none' || style.visibility === 'hidden') {
    return false;
  }
  const text = norm([el.textContent, el.value, el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('placeholder'), el.getAttribute('name'), el.id].filter(Boolean).join(' '));
  return patterns.some((pattern) => pattern.test(text));
});
if (!find) return false;
try {
  find.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
} catch {}
try { find.click(); } catch {}
return true;
    """
    return bool(driver.execute_script(script, patterns))


def _maybe_accept_paypal_flow(driver) -> dict[str, Any]:
    snapshot = _page_snapshot(driver)
    url = snapshot.get("url") or ""
    host = str(snapshot.get("host") or "")
    pathname = str(snapshot.get("pathname") or "")
    body = _normalize_text(snapshot.get("bodyText") or "")

    # Completion / success pages
    if (
        (host.endswith("pay.openai.com") or host.endswith("checkout.stripe.com"))
        and pathname.startswith("/c/pay/cs_")
        and _is_hosted_completion_text(body)
    ):
        return {"terminal": True, "status": "success", "reason": "completion_page", "snapshot": snapshot}

    if host.lower().endswith("pay.openai.com") and pathname.startswith("/c/pay/cs_") and "redirect_status=succeeded" in url.lower():
        return {"terminal": False, "status": "pending", "reason": "succeeded_redirect", "snapshot": snapshot}

    # PayPal approval page
    if "paypal.com" in host.lower() and (
        pathname.startswith("/agreements/approve")
        or "sending you back to the merchant" in body.lower()
        or "saving your info" in body.lower()
    ):
        clicked = _click_first_matching(driver, [
            r"同意并继续",
            r"同意",
            r"授权",
            r"确认并继续",
            r"同意して続行",
            r"同意して支払う",
            r"承認",
            r"続行",
            r"agree\s*(?:and)?\s*continue",
            r"accept",
            r"authorize",
            r"agree",
            r"confirm",
            r"pay\s*now",
        ])
        return {"terminal": False, "status": "clicked" if clicked else "waiting", "reason": "paypal_approval", "snapshot": snapshot}

    if "paypal.com" in host.lower() and "/webapps/hermes" in pathname:
        clicked = _click_first_matching(driver, [
            r"同意并继续",
            r"同意",
            r"授权",
            r"确认并继续",
            r"同意して続行",
            r"同意して支払う",
            r"承認",
            r"続行",
            r"agree\s*(?:and)?\s*continue",
            r"accept",
            r"authorize",
            r"agree",
            r"confirm",
        ])
        return {"terminal": False, "status": "clicked" if clicked else "waiting", "reason": "pay_hermes", "snapshot": snapshot}

    # Generic error
    if "/checkoutweb/genericError" in pathname or (
        "something went wrong" in body.lower()
        and "return to merchant" in body.lower()
    ):
        return {"terminal": True, "status": "error", "reason": "generic_error", "snapshot": snapshot}

    return {"terminal": False, "status": "waiting", "reason": "unknown", "snapshot": snapshot}


def _find_text_button_js(patterns: list[str], require_enabled: bool = True) -> str:
    enabled_clause = """
  if (requireEnabled && (el.disabled || el.getAttribute('aria-disabled') === 'true')) {
    return false;
  }
    """
    return f"""
const patterns = (Array.isArray(arguments[0]) ? arguments[0] : []).map((pattern) => new RegExp(pattern, 'i'));
const requireEnabled = {str(require_enabled).lower()};
const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]'));
const target = candidates.find((el) => {{
  if (!el) return false;
  const style = window.getComputedStyle(el);
  if (el.hidden || style.display === 'none' || style.visibility === 'hidden') {{
    return false;
  }}
  {enabled_clause}
  const text = norm([el.textContent, el.value, el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('placeholder'), el.getAttribute('name'), el.id].filter(Boolean).join(' '));
  return patterns.some((pattern) => pattern.test(text));
}});
if (!target) return false;
try {{ target.scrollIntoView({{ block: 'center', inline: 'center', behavior: 'instant' }}); }} catch {{}}
try {{ target.click(); return true; }} catch {{}}
return false;
"""


def _fill_input_by_id(driver, input_id: str, value: str) -> bool:
    return bool(driver.execute_script(
        """
const inputId = String(arguments[0] || '').trim();
let input = document.getElementById(inputId);
if (!input) {
  input = document.querySelector(`input[name="${inputId}"], textarea[name="${inputId}"]`);
}
if (!input) return false;
try { input.focus(); } catch {}
const proto = input.tagName === 'TEXTAREA'
  ? window.HTMLTextAreaElement?.prototype
  : window.HTMLInputElement?.prototype;
const setter = proto ? Object.getOwnPropertyDescriptor(proto, 'value')?.set : null;
const tracker = input._valueTracker;
const previousValue = String(input.value ?? '');
if (setter) {
  setter.call(input, '');
} else {
  input.value = '';
}
input.dispatchEvent(new Event('input', { bubbles: true }));
if (setter) {
  setter.call(input, arguments[1]);
} else {
  input.value = arguments[1];
}
if (tracker && typeof tracker.setValue === 'function') {
  try { tracker.setValue(previousValue); } catch {}
}
input.dispatchEvent(new Event('input', { bubbles: true }));
input.dispatchEvent(new Event('change', { bubbles: true }));
try { input.blur(); } catch {}
return true;
        """,
        str(input_id or "").strip(),
        str(value or ""),
    ))


def _normalize_paypal_date_of_birth(value: str, country_code: str = "US") -> str:
    raw_value = str(value or "").strip()
    match = re.search(r"(\d{1,4})\D+(\d{1,2})\D+(\d{1,4})", raw_value)
    if not match:
        return "1990/05/09" if str(country_code or "").strip().upper() == "JP" else "09/05/1990"
    first = int(match.group(1))
    second = int(match.group(2))
    third = int(match.group(3))
    year = first if len(match.group(1)) == 4 else third
    month = second if len(match.group(1)) == 4 else first
    day = third if len(match.group(1)) == 4 else second
    if year < 1900 or year > 2008 or month < 1 or month > 12 or day < 1 or day > 31:
        return "1990/05/09" if str(country_code or "").strip().upper() == "JP" else "09/05/1990"
    if str(country_code or "").strip().upper() == "JP":
        return f"{year:04d}/{month:02d}/{day:02d}"
    return f"{month:02d}/{day:02d}/{year:04d}"


def _fill_input_by_selectors(driver, selectors: list[str], value: str) -> bool:
    normalized_value = str(value or "")
    normalized_selectors = [str(item or "").strip() for item in (selectors or []) if str(item or "").strip()]
    if not normalized_selectors or not normalized_value:
        return False
    return bool(driver.execute_script(
        """
const selectors = Array.isArray(arguments[0]) ? arguments[0] : [];
const nextValue = String(arguments[1] || '');
if (!selectors.length || !nextValue) return false;
const isVisible = (el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  return !el.hidden
    && !el.disabled
    && !el.readOnly
    && style.display !== 'none'
    && style.visibility !== 'hidden'
    && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
};
const getSetter = (el) => {
  const ctor = String(el?.tagName || '').toUpperCase() === 'TEXTAREA'
    ? window.HTMLTextAreaElement
    : window.HTMLInputElement;
  return Object.getOwnPropertyDescriptor(ctor?.prototype || {}, 'value')?.set || null;
};
const syncTracker = (el, previousValue) => {
  const tracker = el?._valueTracker;
  if (!tracker || typeof tracker.setValue !== 'function') return;
  try { tracker.setValue(String(previousValue ?? '')); } catch {}
};
for (const selector of selectors) {
  const inputs = Array.from(document.querySelectorAll(String(selector || '').trim()));
  for (const input of inputs) {
    if (!isVisible(input)) continue;
    try { input.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch {}
    try { input.focus({ preventScroll: true }); } catch {}
    const setter = getSetter(input);
    const previousValue = String(input.value || '');
    input.dispatchEvent(new InputEvent('beforeinput', {
      inputType: 'deleteContentBackward',
      data: null,
      bubbles: true,
      cancelable: true,
    }));
    if (typeof setter === 'function') setter.call(input, ''); else input.value = '';
    syncTracker(input, previousValue);
    input.dispatchEvent(new InputEvent('input', {
      inputType: 'deleteContentBackward',
      data: null,
      bubbles: true,
    }));
    let composed = '';
    for (const char of nextValue) {
      composed += char;
      const beforeValue = String(input.value || '');
      input.dispatchEvent(new InputEvent('beforeinput', {
        inputType: 'insertText',
        data: char,
        bubbles: true,
        cancelable: true,
      }));
      if (typeof setter === 'function') setter.call(input, composed); else input.value = composed;
      syncTracker(input, beforeValue);
      input.dispatchEvent(new InputEvent('input', {
        inputType: 'insertText',
        data: char,
        bubbles: true,
      }));
    }
    input.dispatchEvent(new Event('change', { bubbles: true }));
    try { input.blur(); } catch {}
    return true;
  }
}
return false;
        """,
        normalized_selectors,
        normalized_value,
    ))


def _select_by_id_match(driver, select_id: str, candidates: list[str]) -> bool:
    return bool(driver.execute_script(
        """
const selectId = String(arguments[0] || '').trim();
let select = document.getElementById(selectId);
if (!select) {
  select = document.querySelector(`select[name="${selectId}"]`);
}
const candidates = (Array.isArray(arguments[1]) ? arguments[1] : []).map((item) => String(item || '').trim().toLowerCase()).filter(Boolean);
if (!select || !candidates.length) return false;
const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const option = Array.from(select.options || []).find((item) => {
  const label = norm(item.textContent || item.label || '');
  const value = norm(item.value || '');
  return candidates.some((candidate) => label === candidate || value === candidate || label.includes(candidate) || value.includes(candidate));
});
if (!option) return false;
select.value = option.value;
option.selected = true;
select.dispatchEvent(new Event('input', { bubbles: true }));
select.dispatchEvent(new Event('change', { bubbles: true }));
return true;
        """,
        str(select_id or "").strip(),
        [str(item or "").strip() for item in (candidates or [])],
    ))


def _select_country_by_code(driver, country_code: str) -> bool:
    code = "JP" if str(country_code or "").strip().upper() == "JP" else "US"
    candidates = [code, "japan", "日本"] if code == "JP" else ["US", "united states", "united states of america", "usa", "美国"]
    return bool(driver.execute_script(
        """
const expectedCode = String(arguments[0] || 'US').trim().toUpperCase() === 'JP' ? 'JP' : 'US';
const candidates = (Array.isArray(arguments[1]) ? arguments[1] : []).map((item) => String(item || '').trim().toLowerCase()).filter(Boolean);
const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const selects = Array.from(document.querySelectorAll('select'));
for (const select of selects) {
  const label = norm([select.id, select.name, select.getAttribute('aria-label'), select.getAttribute('title')].filter(Boolean).join(' '));
  if (
    !/country|billingcountry|countryregion|国|国・地域|国\/地域|請求先住所の国|請求先国/i.test(label)
    && select.id !== 'country'
    && select.name !== 'country'
    && select.id !== 'billingCountry'
    && select.name !== 'billingCountry'
  ) {
    continue;
  }
  const option = Array.from(select.options || []).find((item) => {
    const optionLabel = norm(item.textContent || item.label || '');
    const optionValue = norm(item.value || '');
    return optionValue === expectedCode.toLowerCase()
      || candidates.some((candidate) => optionLabel === candidate || optionValue === candidate || optionLabel.includes(candidate));
  });
  if (!option) {
    continue;
  }
  select.value = option.value;
  option.selected = true;
  select.dispatchEvent(new Event('input', { bubbles: true }));
  select.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}
return false;
        """,
        code,
        candidates,
    ))


def _fill_verification_code(driver, code: str) -> bool:
    normalized = re.sub(r"\D+", "", str(code or ""))[:6]
    if len(normalized) != 6:
        return False
    return bool(driver.execute_script(
        """
const code = String(arguments[0] || '');
const inputs = Array.from({ length: 6 }, (_, index) => document.getElementById(`ci-ciBasic-${index}`)).filter(Boolean);
if (inputs.length < 6) return false;
for (let index = 0; index < inputs.length; index += 1) {
  const input = inputs[index];
  try { input.focus(); } catch {}
  const proto = window.HTMLInputElement?.prototype;
  const setter = proto ? Object.getOwnPropertyDescriptor(proto, 'value')?.set : null;
  if (setter) setter.call(input, code[index] || ''); else input.value = code[index] || '';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
}
return true;
        """,
        normalized,
    ))


def _click_verification_resend(driver) -> bool:
    return bool(driver.execute_script(
        """
const patterns = (Array.isArray(arguments[0]) ? arguments[0] : []).map((pattern) => new RegExp(pattern, 'i'));
const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
const visible = (el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  return !el.hidden
    && style.display !== 'none'
    && style.visibility !== 'hidden'
    && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
};
const enabled = (el) => Boolean(el)
  && !el.disabled
  && el.getAttribute('aria-disabled') !== 'true';
const selectors = [
  'button[data-testid="resend-link"]',
  '[data-testid="resend-link"]',
  'button[data-testid*="resend" i]',
  '[data-testid*="resend" i]',
  'button[id*="resend" i]',
  '[role="button"][id*="resend" i]',
  'button[name*="resend" i]',
  '[role="button"][name*="resend" i]',
  'button[aria-label*="resend" i]',
  '[role="button"][aria-label*="resend" i]'
];
const direct = selectors
  .flatMap((selector) => Array.from(document.querySelectorAll(selector)))
  .find((el) => visible(el) && enabled(el));
const candidates = direct
  ? [direct]
  : Array.from(document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]'))
      .filter((el) => visible(el) && enabled(el));
const target = candidates.find((el) => {
  const text = norm([
    el.textContent,
    el.value,
    el.getAttribute('aria-label'),
    el.getAttribute('title'),
    el.getAttribute('placeholder'),
    el.getAttribute('name'),
    el.id,
    el.getAttribute('data-testid'),
  ].filter(Boolean).join(' '));
  return direct === el || patterns.some((pattern) => pattern.test(text));
});
if (!target) return false;
try { target.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch {}
try { target.click(); return true; } catch {}
return false;
        """,
        [
            r"resend",
            r"send\s+(?:a\s+)?new\s+code",
            r"get\s+a\s+new\s+code",
            r"try\s+again",
            r"重新发送",
            r"重新傳送",
            r"重发",
            r"重發",
            r"再送信",
            r"再送",
            r"コードを再送",
            r"確認コードを再送",
            r"認証コードを再送",
            r"新しいコード",
            r"もう一度送信",
            r"SMS\s*を再送",
        ],
    ))


def _click_review_consent(driver) -> bool:
    return bool(driver.execute_script(_find_text_button_js([
        r"同意并继续",
        r"同意",
        r"授权",
        r"确认并继续",
        r"同意して続行",
        r"同意して支払う",
        r"承認",
        r"続行",
        r"agree\s*(?:and)?\s*continue",
        r"accept",
        r"authorize",
        r"agree",
        r"confirm",
        r"continue",
    ])))


def _dismiss_add_payment_method_modal(driver) -> bool:
    return bool(driver.execute_script(
        """
const visible = (el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  return !el.hidden
    && style.display !== 'none'
    && style.visibility !== 'hidden'
    && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
};
const modal = document.querySelector('.vx_modal-content');
if (!visible(modal)) return false;
const header = String(modal.querySelector('#js_modalHeader, .flow-modal-header, h1, h2, [role="heading"]')?.textContent || '').replace(/\\s+/g, ' ').trim();
if (!/支払方法を登録|支払い方法を登録|register payment method|add payment method/i.test(header)) return false;
const scope = modal.closest('#mainModal, .vx_modal-wrapper, .vx_modal-flow') || modal.parentElement || document;
const selectors = [
  '#modalClose',
  'a#modalClose',
  'a[data-name="modalClose"]',
  'a.modal_dismiss-btn',
  'a.test_dismissFlow',
  'a[role="button"][aria-label*="閉じる"]',
  'a[role="button"][aria-label*="Close"]',
  'button[aria-label*="閉じる"]',
  'button[aria-label*="Close"]',
  'button[data-testid*="close" i]',
  'button[id*="close" i]',
  '.vx_modal-header button',
  '.vx_modal-wrapper a[role="button"]',
];
for (const selector of selectors) {
  const button = scope.querySelector(selector);
  if (visible(button) && !button.disabled && button.getAttribute('aria-disabled') !== 'true') {
    try { button.click(); return true; } catch {}
  }
}
const buttons = Array.from(scope.querySelectorAll('button, a[role="button"], a'));
const fallback = buttons.find((button) => {
  if (!visible(button) || button.disabled || button.getAttribute('aria-disabled') === 'true') return false;
  const text = String([
    button.textContent,
    button.getAttribute('aria-label'),
    button.getAttribute('title'),
    button.getAttribute('data-name'),
    button.getAttribute('data-testid'),
    button.id,
    button.className,
  ].filter(Boolean).join(' ')).replace(/\\s+/g, ' ').trim();
  return /閉じる|close|cancel|dismiss|戻る/i.test(text) || (!text && button.querySelector('svg, [class*="icon" i]'));
});
if (!fallback) return false;
try { fallback.click(); return true; } catch {}
return false;
        """
    ))


def _click_next_or_submit(driver) -> bool:
    clicked_direct = bool(driver.execute_script(
        """
const directSelectors = [
  '#btnNext',
  '#btnLogin',
  '#login-submit',
  '#payment-submit-btn',
  '#consentButton',
  'button[data-testid="consentButton"]',
  'button[data-testid="login-submit"]',
  'button[data-testid="submitButton"]',
  'button[type="submit"]',
  'input[type="submit"]',
  '[role="button"][data-testid*="submit" i]',
  '[role="button"][id*="next" i]',
  '[role="button"][id*="continue" i]'
];
const isUsable = (el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  return !el.hidden
    && !el.disabled
    && el.getAttribute('aria-disabled') !== 'true'
    && style.display !== 'none'
    && style.visibility !== 'hidden'
    && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
};
for (const selector of directSelectors) {
  const elements = Array.from(document.querySelectorAll(selector));
  for (const el of elements) {
    if (!isUsable(el)) continue;
    try { el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch {}
    try { el.click(); return true; } catch {}
  }
}
return false;
        """
    ))
    if clicked_direct:
        return True
    return bool(driver.execute_script(_find_text_button_js([
        r"continue\s+(?:to\s+)?pay(?:ment)?",
        r"continue\s+(?:to\s+)?paypal",
        r"pay\s+with\s+paypal",
        r"继续付款",
        r"继续支付",
        r"继续",
        r"次へ",
        r"続行",
        r"支払う",
        r"ログイン",
        r"同意して続行",
        r"同意して支払う",
        r"pay",
        r"continue",
        r"next",
        r"agree",
        r"confirm",
        r"authorize",
        r"subscribe",
        r"登录",
        r"登入",
    ])))


def _press_enter_by_id(driver, input_id: str) -> bool:
    normalized_id = str(input_id or "").strip()
    if not normalized_id:
        return False
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
    except Exception:
        return False
    selectors = [
        f"#{normalized_id}",
        f"input[name='{normalized_id}']",
        f"textarea[name='{normalized_id}']",
    ]
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            elements = []
        for element in elements:
            try:
                if not element.is_displayed() or not element.is_enabled():
                    continue
                element.click()
                time.sleep(0.1)
                element.send_keys(Keys.ENTER)
                return True
            except Exception:
                continue
    return False


def _advance_external_redirect_page(driver) -> bool:
    try:
        snapshot = _page_snapshot(driver)
    except Exception:
        snapshot = {}
    host = str(snapshot.get("host") or "").lower()
    body = _normalize_text(snapshot.get("bodyText") or "")
    if "paypal." in host:
        return False
    if not (
        host.endswith("pm-redirects.stripe.com")
        or host.endswith("checkout.stripe.com")
        or host.endswith("pay.openai.com")
    ):
        return False
    if _is_hosted_completion_text(body):
        return False
    return _click_first_matching(driver, [
        r"continue\s+(?:to\s+)?paypal",
        r"continue\s+(?:to\s+)?pay(?:ment)?",
        r"pay\s+with\s+paypal",
        r"paypal",
        r"continue",
        r"next",
        r"继续",
        r"继续付款",
        r"继续支付",
    ])


def _wait_for_page_ready(driver, timeout_seconds: float = 60.0, poll_seconds: float = 2.0) -> bool:
    started_at = time.time()
    while time.time() - started_at < max(1.0, float(timeout_seconds or 0)):
        try:
            ready = driver.execute_script(
                """
const body = document.body?.innerText || '';
const readyState = document.readyState || '';
const hasInputs = Boolean(document.querySelector('#email, input[type="email"], #password, input[type="password"], #cardNumber, #billingLine1'));
const hasButtons = Boolean(Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]')).find((el) => {
  const text = String(el?.textContent || el?.value || '').trim();
  return text.length > 0 && el.offsetWidth > 0;
}));
return Boolean(
  (readyState === 'complete' || readyState === 'interactive')
  && (
    body.length > 50
    || hasInputs
    || hasButtons
  )
  && !document.querySelector('.loading-spinner')
);
                """
            )
            if ready:
                return True
        except Exception:
            pass
        time.sleep(max(0.2, float(poll_seconds or 0.2)))
    return False


def _switch_to_checkout_window(driver) -> bool:
    try:
        handles = list(driver.window_handles or [])
    except Exception:
        handles = []
    if not handles:
        return False
    fallback = handles[-1]
    for handle in reversed(handles):
        try:
            driver.switch_to.window(handle)
            url = str(driver.current_url or "")
        except Exception:
            continue
        if re.search(r"paypal\.|pay\.openai\.com|checkout\.stripe\.com|pm-redirects\.stripe\.com", url, re.I):
            return True
    try:
        driver.switch_to.window(fallback)
    except Exception:
        return False
    return False


def _remove_inline_captcha_artifacts(driver) -> bool:
    return bool(driver.execute_script(
        """
let removed = false;
['#captcha-standalone', '.captcha-overlay', '.captcha-container', '#captchaHeading'].forEach((selector) => {
  document.querySelectorAll(selector).forEach((node) => {
    try {
      node.remove();
      removed = true;
    } catch {}
  });
});
return removed;
        """
    ))


def _has_hcaptcha_frame(driver) -> bool:
    return bool(driver.execute_script(
        """
return Boolean(Array.from(document.querySelectorAll('iframe')).find((frame) => {
  const src = String(frame?.src || frame?.getAttribute?.('src') || '');
  return /hcaptcha\\.com/i.test(src);
}));
        """
    ))


def _click_hcaptcha_checkbox(driver) -> bool:
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.by import By
    except Exception:
        return False

    iframe = None
    try:
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe[src*='hcaptcha']")
        iframe = frames[0] if frames else None
    except Exception:
        iframe = None
    if iframe is None:
        return False

    clicked = False
    try:
        driver.switch_to.frame(iframe)
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, "#checkbox, [role='checkbox'], .checkbox, .cb-lb")
        except Exception:
            elements = []
        for element in elements:
            try:
                if not element.is_displayed():
                    continue
                driver.execute_script("arguments[0].click();", element)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            try:
                clicked = bool(driver.execute_script(
                    """
const target = document.querySelector('#checkbox, [role="checkbox"], .checkbox, .cb-lb, label');
if (!target) return false;
try { target.click(); return true; } catch {}
return false;
                    """
                ))
            except Exception:
                clicked = False
    finally:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

    if clicked:
        return True

    try:
        rect = iframe.rect or {}
        height = int(rect.get("height") or 0)
        offset_y = max(5, min(max(height - 5, 5), height // 2 if height else 20))
        ActionChains(driver).move_to_element_with_offset(iframe, 30, offset_y).click().perform()
        return True
    except Exception:
        return False


def _handle_hcaptcha_if_needed(driver) -> bool:
    try:
        state = driver.execute_script(
            """
const bodyText = String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
const url = String(location.href || '');
const hasFrame = Boolean(Array.from(document.querySelectorAll('iframe')).find((frame) => {
  const src = String(frame?.src || frame?.getAttribute?.('src') || '');
  return /hcaptcha\\.com/i.test(src);
}));
return {
  url,
  bodyText,
  hasFrame,
  challenge: /validatecaptcha/i.test(url)
    || /security challenge|confirm\\s+you.*human|hcaptcha|人間を確認/i.test(bodyText),
};
            """
        ) or {}
    except Exception:
        state = {}

    if not state.get("challenge") and not state.get("hasFrame"):
        return False

    _remove_inline_captcha_artifacts(driver)
    if _click_hcaptcha_checkbox(driver):
        time.sleep(8)
        _remove_inline_captcha_artifacts(driver)
        return True
    return bool(state.get("challenge"))


def _fill_password_like_inputs(driver, password: str) -> bool:
    if not str(password or "").strip():
        return False
    return bool(driver.execute_script(
        """
const password = String(arguments[0] || '');
const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const inputs = Array.from(document.querySelectorAll('input')).filter((input) => {
  const type = norm(input.type || input.getAttribute('type') || '');
  const label = norm([input.getAttribute('aria-label'), input.getAttribute('title'), input.getAttribute('placeholder'), input.getAttribute('name'), input.id].filter(Boolean).join(' '));
  return type === 'password' || /password|pass|密码/.test(label);
});
if (!inputs.length) return false;
for (const input of inputs) {
  const proto = window.HTMLInputElement?.prototype;
  const setter = proto ? Object.getOwnPropertyDescriptor(proto, 'value')?.set : null;
  if (setter) setter.call(input, password); else input.value = password;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
}
return true;
        """,
        str(password or ""),
    ))


def _type_input_by_id(driver, input_id: str, value: str) -> bool:
    normalized_id = str(input_id or "").strip()
    normalized_value = str(value or "")
    if not normalized_id or not normalized_value:
        return False
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
    except Exception:
        return False

    selectors = [
        f"#{normalized_id}",
        f"input[name='{normalized_id}']",
        f"textarea[name='{normalized_id}']",
    ]
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            elements = []
        for element in elements:
            try:
                if not element.is_displayed():
                    continue
                driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", element)
                element.click()
                time.sleep(0.1)
                element.send_keys(Keys.CONTROL, "a")
                element.send_keys(Keys.BACKSPACE)
                for char in normalized_value:
                    element.send_keys(char)
                    time.sleep(0.03)
                return True
            except Exception:
                continue
    return False


def _select_prefecture_or_state(driver, candidates: list[str]) -> bool:
    normalized_candidates = [str(item or "").strip() for item in (candidates or []) if str(item or "").strip()]
    if not normalized_candidates:
        return False
    return bool(driver.execute_script(
        """
const candidates = (Array.isArray(arguments[0]) ? arguments[0] : []).map((item) => String(item || '').trim()).filter(Boolean);
if (!candidates.length) return false;
const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const compact = (value) => norm(value).replace(/[^a-z0-9\\u4e00-\\u9fff]/g, '');
const compactCandidates = new Set(candidates.map(compact).filter(Boolean));
const selects = Array.from(document.querySelectorAll('select'));
for (const select of selects) {
  const label = norm([select.id, select.name, select.getAttribute('aria-label'), select.getAttribute('title')].filter(Boolean).join(' '));
  if (!/state|region|prefecture|都道府県/i.test(label) && select.id !== 'billingState' && select.name !== 'billingState') {
    continue;
  }
  const option = Array.from(select.options || []).find((item) => {
    const optionLabel = compact(item.textContent || item.label || '');
    const optionValue = compact(item.value || '');
    return compactCandidates.has(optionLabel) || compactCandidates.has(optionValue);
  });
  if (!option) {
    continue;
  }
  select.value = option.value;
  option.selected = true;
  select.dispatchEvent(new Event('input', { bubbles: true }));
  select.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}
return false;
        """,
        normalized_candidates,
    ))


def _switch_guest_checkout_to_english_if_needed(driver, country_code: str) -> bool:
    # JP mode now stays on the localized checkout surface.
    return False


def _switch_nationality_to_united_states_if_needed(driver, country_code: str) -> bool:
    if str(country_code or "").strip().upper() != "JP":
        return False
    switched_directly = bool(driver.execute_script(
        """
const expectedCode = String(arguments[0] || '').trim().toUpperCase();
if (expectedCode !== 'JP') return false;
const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
const visible = (el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  return !el.hidden
    && style.display !== 'none'
    && style.visibility !== 'hidden'
    && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
};
const isUsText = (value) => {
  const text = norm(value);
  return /^(United States|United States of America|US|USA|U\\.S\\.A\\.|米国|アメリカ合衆国|アメリカ)$/i.test(text);
};
const pageText = norm(document.body?.innerText || '');
if (/country of nationality is\\s+United States|国籍.*(?:米国|アメリカ合衆国|アメリカ)|(?:米国|アメリカ合衆国|アメリカ).*国籍/i.test(pageText)) {
  return true;
}
const nationalityLabel = /nationality|citizenship|country|国籍|市民権|居住国|国・地域/i;
for (const select of Array.from(document.querySelectorAll('select'))) {
  const label = norm([
    select.id,
    select.name,
    select.getAttribute('aria-label'),
    select.getAttribute('title'),
    select.closest('label')?.textContent,
    select.closest('[data-testid], fieldset, section, div')?.textContent,
  ].filter(Boolean).join(' '));
  const hasUsOption = Array.from(select.options || []).some((option) => {
    return String(option.value || '').trim().toUpperCase() === 'US'
      || isUsText(option.textContent || option.label || '')
      || isUsText(option.value || '');
  });
  if (!hasUsOption || (!nationalityLabel.test(label) && !/kyc/i.test(label))) {
    continue;
  }
  const option = Array.from(select.options || []).find((item) => {
    return String(item.value || '').trim().toUpperCase() === 'US'
      || isUsText(item.textContent || item.label || '')
      || isUsText(item.value || '');
  });
  if (!option) continue;
  select.value = option.value;
  option.selected = true;
  select.dispatchEvent(new Event('input', { bubbles: true }));
  select.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}
return false;
        """,
        str(country_code or ""),
    ))
    if switched_directly:
        time.sleep(1)
        return True

    opened = bool(driver.execute_script(
        """
const expectedCode = String(arguments[0] || '').trim().toUpperCase();
if (expectedCode !== 'JP') return false;
const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
const visible = (el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  return !el.hidden
    && style.display !== 'none'
    && style.visibility !== 'hidden'
    && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
};
const pageText = norm(document.body?.innerText || '');
if (/country of nationality is\\s+United States|国籍.*(?:米国|アメリカ合衆国|アメリカ)|(?:米国|アメリカ合衆国|アメリカ).*国籍/i.test(pageText)) {
  return false;
}
const nationalityRe = /nationality|citizenship|country|国籍|市民権|居住国|国・地域/i;
const changeRe = /change|edit|変更|編集|変える|切り替え|選択/i;
const controls = Array.from(document.querySelectorAll([
  '#kycCountryChangeButton',
  'button[data-testid="kycCountryChangeButton"]',
  '[data-testid*="kyc" i]',
  '[data-testid*="country" i]',
  '[data-testid*="nationality" i]',
  'button',
  '[role="button"]',
  '[role="selection-menu-button"]',
  '[role="combobox"]',
  '[aria-haspopup]'
].join(',')));
const button = controls.find((el) => {
  if (!visible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
  const text = norm([
    el.textContent,
    el.getAttribute('aria-label'),
    el.getAttribute('title'),
    el.id,
    el.getAttribute('data-testid'),
  ].filter(Boolean).join(' '));
  const parentText = norm(el.closest('[data-testid], fieldset, section, div')?.textContent || '');
  return /kycCountryChangeButton/i.test(text)
    || (/country|nationality/i.test(text) && /change|edit/i.test(text))
    || (nationalityRe.test(text) && changeRe.test(text))
    || (changeRe.test(text) && nationalityRe.test(parentText))
    || ((/日本|Japan/i.test(text) || /日本|Japan/i.test(parentText)) && nationalityRe.test(parentText));
});
if (!button) return false;
try { button.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch {}
try { button.click(); return true; } catch {}
return false;
        """,
        str(country_code or ""),
    ))
    if not opened:
        return False
    started_at = time.time()
    clicked_any = False
    while time.time() - started_at < 8.0:
        clicked = bool(driver.execute_script(
            """
const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
const visible = (el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  return !el.hidden
    && style.display !== 'none'
    && style.visibility !== 'hidden'
    && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
};
const isUsText = (value) => {
  const text = norm(value);
  return /^(United States|United States of America|US|USA|U\\.S\\.A\\.|米国|アメリカ合衆国|アメリカ)$/i.test(text);
};
const option = Array.from(document.querySelectorAll('button, [role="option"], [role="menuitem"], [role="button"], li, div, span, a')).find((el) => {
  if (!visible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
  const text = norm([
    el.textContent,
    el.getAttribute('aria-label'),
    el.getAttribute('title'),
    el.getAttribute('data-value'),
    el.getAttribute('value'),
  ].filter(Boolean).join(' '));
  return isUsText(text);
});
if (!option) return false;
const clickable = option.closest('button, [role="option"], [role="menuitem"], [role="button"], li, a') || option;
try { clickable.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch {}
try { clickable.click(); return true; } catch {}
return false;
            """
        ))
        if clicked:
            clicked_any = True
            break
        driver.execute_script(
            """
const input = Array.from(document.querySelectorAll('input[type="search"], input[role="combobox"], input[aria-autocomplete]')).find((el) => {
  const style = window.getComputedStyle(el);
  return !el.hidden && style.display !== 'none' && style.visibility !== 'hidden' && (el.offsetWidth > 0 || el.offsetHeight > 0);
});
if (input && !input.value) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  try { input.focus(); } catch {}
  if (setter) setter.call(input, 'United States'); else input.value = 'United States';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
}
            """
        )
        time.sleep(0.25)
    if not clicked_any:
        return False
    started_at = time.time()
    while time.time() - started_at < 8.0:
        confirmed = bool(driver.execute_script(
            """
const text = String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
return /country of nationality is\\s+United States|国籍.*(?:米国|アメリカ合衆国|アメリカ)|(?:米国|アメリカ合衆国|アメリカ).*国籍|data-testid=["']english-names["']/i.test(text)
  || Boolean(document.querySelector('[data-testid="english-names"]'));
            """
        ))
        if confirmed:
            return True
        time.sleep(0.3)
    return True


def _detect_paypal_stage(driver) -> dict[str, Any]:
    return driver.execute_script(
        """
const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
const bodyText = norm(document.body && (document.body.innerText || document.body.textContent || ''));
const host = String(location.host || '').toLowerCase();
const pathname = String(location.pathname || '').trim();
const isVisible = (el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  return !el.hidden
    && style.display !== 'none'
    && style.visibility !== 'hidden'
    && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
};
const hasVerification = Array.from({ length: 6 }, (_, index) => document.getElementById(`ci-ciBasic-${index}`)).filter(Boolean).length >= 6;
const hasCardFields = [document.getElementById('cardNumber'), document.getElementById('billingLine1'), document.getElementById('cardExpiry'), document.getElementById('cardCvv')].some(isVisible);
const hasEmailInput = [document.getElementById('email'), document.querySelector('input[type=\"email\"]'), document.querySelector('input[name=\"email\"]')].some(isVisible);
const hasConsentButton = [document.getElementById('consentButton'), document.querySelector('button[data-testid=\"consentButton\"]')].some(isVisible);
const walletModalVisible = (() => {
  const modal = document.querySelector('.vx_modal-content');
  if (!isVisible(modal)) return false;
  const header = norm(modal.querySelector('#js_modalHeader, .flow-modal-header, h1, h2, [role="heading"]')?.textContent || '');
  return /支払方法を登録|支払い方法を登録|register payment method|add payment method/i.test(header);
})();
const hasApproveButton = Boolean(Array.from(document.querySelectorAll('button, a, [role=\"button\"], input[type=\"button\"], input[type=\"submit\"]')).find((el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  if (el.hidden || style.display === 'none' || style.visibility === 'hidden' || !isVisible(el)) return false;
  const text = norm([el.textContent, el.value, el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('placeholder'), el.getAttribute('name'), el.id].filter(Boolean).join(' '));
  return /同意并继续|同意|授权|确认并继续|同意して続行|同意して支払う|承認|続行|agree\\s*(?:and)?\\s*continue|accept|authorize|agree|confirm|pay\\s*now/i.test(text);
}));
const genericError = /\\/checkoutweb\\/genericError/i.test(pathname)
  || (/something went wrong/i.test(bodyText) && /return to merchant/i.test(bodyText))
  || /things don[’']?t appear to be working at the moment/i.test(bodyText);
const cardLinkedError = Boolean(document.querySelector('[data-error-key="pageLevelError.ccLinked"]'))
  || /already\\s+(?:been\\s+)?(?:added|linked)\\s+to\\s+(?:another|a\\s+different)\\s+paypal\\s+account/i.test(bodyText)
  || /card\\s+is\\s+already\\s+(?:added|linked)/i.test(bodyText)
  || /このカードはすでに別のPayPalアカウントに追加されています/i.test(bodyText)
  || /別のPayPalアカウントからカードを削除するか、別の支払方法をお試しください/i.test(bodyText);
const blocked = /you have been blocked|security challenge/i.test(bodyText);
const redirecting = /saving\\s+your\\s+info.*sending\\s+you\\s+back\\s+to\\s+the\\s+merchant/i.test(bodyText);
const hasHcaptcha = Boolean(Array.from(document.querySelectorAll('iframe')).find((frame) => {
  const src = String(frame?.src || frame?.getAttribute?.('src') || '');
  return /hcaptcha\\.com/i.test(src);
}));
const accountCreate = hasEmailInput && !hasCardFields && /create\\s+(?:a\\s+)?paypal\\s+account|already\\s+have\\s+an?\\s+account|创建\\s*PayPal\\s*账户|您已有账号了吗|PayPal\\s*アカウントを作成|アカウントをお持ち|すでにアカウント/i.test(bodyText);
let stage = 'unknown';
if (!/paypal\\./i.test(host)) {
  stage = 'outside_paypal';
} else if (walletModalVisible) {
  stage = 'wallet_modal';
} else if (hasVerification) {
  stage = 'verification';
} else if (cardLinkedError) {
  stage = 'card_linked_error';
} else if (blocked) {
  stage = 'blocked';
} else if (genericError) {
  stage = 'generic_error';
} else if (accountCreate) {
  stage = 'account_create_email';
} else if (/\\/checkoutweb\\//i.test(pathname) || hasCardFields) {
  stage = 'guest_checkout';
} else if (redirecting) {
  stage = 'redirecting';
} else if (/\\/webapps\\/hermes/i.test(pathname) && hasConsentButton) {
  stage = 'review_consent';
} else if (hasApproveButton) {
  stage = 'approval';
} else if (pathname === '/pay' || hasEmailInput) {
  stage = 'pay_login';
}
return {
  stage,
  host,
  pathname,
  url: String(location.href || ''),
  title: String(document.title || ''),
  bodyText,
  genericError,
  cardLinkedError,
  blocked,
  redirecting,
  hasHcaptcha,
  hasVerification,
  hasCardFields,
  hasEmailInput,
  hasConsentButton,
  walletModalVisible,
  hasApproveButton,
};
        """
    )


def _extract_verification_code(payload: Any) -> str:
    if isinstance(payload, str):
        match = re.search(r"(?<!\d)(\d[\s-]?\d[\s-]?\d[\s-]?\d[\s-]?\d[\s-]?\d)(?!\d)", payload)
        return re.sub(r"\D+", "", match.group(1))[:6] if match else ""
    if isinstance(payload, list):
        for item in payload:
            code = _extract_verification_code(item)
            if code:
                return code
        return ""
    if isinstance(payload, dict):
        preferred = ["sms", "smsCode", "sms_code", "message", "msg", "text", "content", "body", "code", "otp", "verification_code", "verificationCode"]
        for key in preferred:
            code = _extract_verification_code(payload.get(key))
            if code:
                return code
        for value in payload.values():
            code = _extract_verification_code(value)
            if code:
                return code
    return ""


def _fetch_verification_code(verification_url: str) -> str:
    response = requests.get(
        verification_url,
        timeout=20,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )
    text = response.text or ""
    try:
        payload = json.loads(text)
    except Exception:
        payload = text
    return _extract_verification_code(payload)


def open_url_in_profile(
    *,
    browser_backend: str = "adspower",
    api_base: str,
    ads_power_api_key: str = "",
    roxybrowser_api_key: str = "",
    profile_id: str,
    target_url: str,
    clear_cache_before_start: bool = False,
    close_profile_on_finish: bool = False,
) -> dict[str, Any]:
    normalized_url = str(target_url or "").strip()
    if not normalized_url:
        raise AdsPowerWorkerError("target_url is required")

    client = _create_browser_client(
        browser_backend=browser_backend,
        api_base=api_base,
        ads_power_api_key=ads_power_api_key,
        roxybrowser_api_key=roxybrowser_api_key,
    )
    if clear_cache_before_start:
        client.clear_profile_cache(profile_id, ["cookie", "local_storage", "cache"])
    launch = client.start_profile(profile_id)
    _ensure_supported_launch(browser_backend, launch)
    client.wait_after_start()
    driver = None
    try:
        driver = _create_connected_driver(launch)
        if clear_cache_before_start:
            _prepare_connected_browser_for_fresh_flow(driver)
        data = _navigate_connected_browser_to_url(
            driver,
            launch,
            normalized_url,
            browser_backend=browser_backend,
        )
        return {
            "target_url": normalized_url,
            "page_url": str(data.get("url") or normalized_url).strip(),
            "page_title": str(data.get("title") or "").strip(),
            "target_id": str(data.get("target_id") or data.get("id") or "").strip(),
            "launch": launch,
        }
    finally:
        try:
            if driver is not None:
                driver.quit()
        except Exception:
            pass
        if close_profile_on_finish:
            client.stop_profile(profile_id)


def run_url_in_profile_until_terminal(
    *,
    browser_backend: str = "adspower",
    api_base: str,
    ads_power_api_key: str = "",
    roxybrowser_api_key: str = "",
    profile_id: str,
    target_url: str,
    clear_cache_before_start: bool = False,
    timeout_seconds: int = 240,
    close_profile_on_finish: bool = False,
) -> dict[str, Any]:
    normalized_url = str(target_url or "").strip()
    if not normalized_url:
        raise AdsPowerWorkerError("target_url is required")

    client = _create_browser_client(
        browser_backend=browser_backend,
        api_base=api_base,
        ads_power_api_key=ads_power_api_key,
        roxybrowser_api_key=roxybrowser_api_key,
    )
    if clear_cache_before_start:
        client.clear_profile_cache(profile_id, ["cookie", "local_storage", "cache"])
    launch = client.start_profile(profile_id)
    _ensure_supported_launch(browser_backend, launch)
    client.wait_after_start()

    driver = None
    started_at = time.time()
    try:
        driver = _create_connected_driver(launch)
        if clear_cache_before_start:
            _prepare_connected_browser_for_fresh_flow(driver)
        _navigate_connected_browser_to_url(
            driver,
            launch,
            normalized_url,
            browser_backend=browser_backend,
        )
        _wait_for_page_ready(driver, timeout_seconds=45, poll_seconds=1.5)
        last_snapshot = {}
        while True:
            elapsed = time.time() - started_at
            if elapsed >= float(timeout_seconds or 240):
                raise AdsPowerWorkerError(
                    f"AdsPower flow timed out after {int(timeout_seconds or 240)} seconds on {normalized_url}"
                )
            if _handle_hcaptcha_if_needed(driver):
                _wait_for_page_ready(driver, timeout_seconds=15, poll_seconds=1.0)
                time.sleep(2)
            state = _maybe_accept_paypal_flow(driver)
            last_snapshot = state.get("snapshot") or last_snapshot
            if state.get("terminal") is True:
                return {
                    "ok": state.get("status") == "success",
                    "status": state.get("status"),
                    "reason": state.get("reason"),
                    "target_url": normalized_url,
                    "page_url": last_snapshot.get("url") or driver.current_url,
                    "page_title": last_snapshot.get("title") or driver.title,
                    "body_text": last_snapshot.get("bodyText") or "",
                    "launch": launch,
                }
            time.sleep(1.5)
    finally:
        try:
            if driver is not None:
                driver.quit()
        except Exception:
            pass
        if close_profile_on_finish:
            client.stop_profile(profile_id)


def run_hosted_checkout_in_profile(
    *,
    browser_backend: str = "adspower",
    api_base: str,
    ads_power_api_key: str = "",
    roxybrowser_api_key: str = "",
    profile_id: str,
    target_url: str,
    guest_profile: dict[str, Any] | None = None,
    verification_url: str = "",
    verification_resend_max_attempts: int = 1,
    verification_poll_attempts: int = 6,
    verification_poll_interval_seconds: int = 5,
    external_verification: bool = False,
    verification_code: str = "",
    navigate_to_target: bool = True,
    timeout_seconds: int = 240,
    clear_cache_before_start: bool = False,
    close_profile_on_finish: bool = False,
) -> dict[str, Any]:
    normalized_url = str(target_url or "").strip()
    if navigate_to_target and not normalized_url:
        raise AdsPowerWorkerError("target_url is required")
    if not normalized_url:
        normalized_url = "about:blank"
    profile = guest_profile if isinstance(guest_profile, dict) else {}
    verification_url = str(verification_url or "").strip()
    initial_verification_code = re.sub(r"\D+", "", str(verification_code or ""))[:6]
    max_resend = max(0, int(verification_resend_max_attempts or 0))
    poll_attempts = max(1, int(verification_poll_attempts or 6))
    poll_interval = max(1, int(verification_poll_interval_seconds or 5))

    client = _create_browser_client(
        browser_backend=browser_backend,
        api_base=api_base,
        ads_power_api_key=ads_power_api_key,
        roxybrowser_api_key=roxybrowser_api_key,
    )
    if clear_cache_before_start:
        client.clear_profile_cache(profile_id, ["cookie", "local_storage", "cache"])
    launch = client.start_profile(profile_id)
    _ensure_supported_launch(browser_backend, launch)
    client.wait_after_start()

    driver = None
    started_at = time.time()
    resend_attempts = 0
    last_verification_code = ""
    history: list[dict[str, Any]] = []
    last_state: dict[str, Any] = {}
    try:
        driver = _create_connected_driver(launch)
        if clear_cache_before_start and navigate_to_target:
            _prepare_connected_browser_for_fresh_flow(driver)
        if navigate_to_target:
            _navigate_connected_browser_to_url(
                driver,
                launch,
                normalized_url,
                browser_backend=browser_backend,
            )
        else:
            _switch_to_checkout_window(driver)
        _wait_for_page_ready(driver, timeout_seconds=45, poll_seconds=1.5)
        if initial_verification_code:
            if not _fill_verification_code(driver, initial_verification_code):
                raise AdsPowerWorkerError("AdsPower hosted checkout verification inputs were not found")
            time.sleep(5)
        while True:
            if time.time() - started_at >= float(timeout_seconds or 240):
                last_stage = str(last_state.get("stage") or PAYPAL_STAGE_UNKNOWN).strip()
                last_url = str(last_state.get("url") or "").strip()
                last_title = str(last_state.get("title") or "").strip()
                last_body = _normalize_text(last_state.get("bodyText") or "")[:180]
                detail = "; ".join(
                    item for item in [
                        f"stage={last_stage}",
                        f"url={last_url}" if last_url else "",
                        f"title={last_title}" if last_title else "",
                        f"body={last_body}" if last_body else "",
                    ] if item
                )
                raise AdsPowerWorkerError(
                    f"AdsPower hosted checkout timed out after {int(timeout_seconds or 240)} seconds"
                    + (f"; last {detail}" if detail else "")
                )
            _switch_to_checkout_window(driver)
            if _dismiss_add_payment_method_modal(driver):
                time.sleep(2)
                continue
            if _handle_hcaptcha_if_needed(driver):
                _wait_for_page_ready(driver, timeout_seconds=15, poll_seconds=1.0)
                time.sleep(2)
                continue
            state = _detect_paypal_stage(driver)
            last_state = state if isinstance(state, dict) else {}
            stage = str(state.get("stage") or PAYPAL_STAGE_UNKNOWN).strip()
            history.append({
                "stage": stage,
                "url": str(state.get("url") or "").strip(),
                "title": str(state.get("title") or "").strip(),
            })

            url = str(state.get("url") or "").strip()
            body = _normalize_text(state.get("bodyText") or "")
            if (
                (str(state.get("host") or "").lower().endswith("pay.openai.com") or str(state.get("host") or "").lower().endswith("checkout.stripe.com"))
                and str(state.get("pathname") or "").startswith("/c/pay/cs_")
                and (
                    "redirect_status=succeeded" in url.lower()
                    or _is_hosted_completion_text(body)
                )
            ):
                return {
                    "ok": True,
                    "status": "success",
                    "reason": "completion_page",
                    "page_url": url,
                    "page_title": str(state.get("title") or "").strip(),
                    "body_text": body,
                    "history": history,
                }

            if stage == PAYPAL_STAGE_GENERIC_ERROR:
                return {
                    "ok": False,
                    "status": "error",
                    "reason": "generic_error",
                    "page_url": url,
                    "page_title": str(state.get("title") or "").strip(),
                    "body_text": body,
                    "history": history,
                }
            if stage == PAYPAL_STAGE_CARD_LINKED_ERROR:
                return {
                    "ok": False,
                    "status": "error",
                    "reason": "card_linked",
                    "page_url": url,
                    "page_title": str(state.get("title") or "").strip(),
                    "body_text": body,
                    "history": history,
                }
            if stage == PAYPAL_STAGE_BLOCKED:
                return {
                    "ok": False,
                    "status": "error",
                    "reason": "blocked",
                    "page_url": url,
                    "page_title": str(state.get("title") or "").strip(),
                    "body_text": body,
                    "history": history,
                }

            if stage == PAYPAL_STAGE_OUTSIDE:
                if _advance_external_redirect_page(driver):
                    _wait_for_page_ready(driver, timeout_seconds=15, poll_seconds=1.0)
                time.sleep(2)
                continue

            if stage == PAYPAL_STAGE_LOGIN:
                email = str(profile.get("email") or "").strip()
                password = str(profile.get("password") or "").strip()
                if email:
                    _fill_input_by_id(driver, "email", email)
                clicked = _click_next_or_submit(driver)
                if not clicked and email:
                    _type_input_by_id(driver, "email", email)
                    time.sleep(0.3)
                    clicked = _click_next_or_submit(driver)
                if not clicked:
                    _press_enter_by_id(driver, "email")
                time.sleep(3)
                if password:
                    _fill_password_like_inputs(driver, password)
                    clicked_password = _click_next_or_submit(driver)
                    if not clicked_password:
                        _press_enter_by_id(driver, "password")
                time.sleep(3)
                continue

            if stage == PAYPAL_STAGE_ACCOUNT_CREATE_EMAIL:
                email = str(profile.get("email") or "").strip()
                if email:
                    _fill_input_by_id(driver, "email", email)
                clicked = _click_next_or_submit(driver)
                if not clicked and email:
                    _type_input_by_id(driver, "email", email)
                    time.sleep(0.3)
                    clicked = _click_next_or_submit(driver)
                if not clicked:
                    _press_enter_by_id(driver, "email")
                time.sleep(3)
                continue

            if stage == PAYPAL_STAGE_GUEST_CHECKOUT:
                address = profile.get("address") if isinstance(profile.get("address"), dict) else {}
                country_code = str(address.get("countryCode") or "US").strip().upper() or "US"
                _remove_inline_captcha_artifacts(driver)
                if _switch_guest_checkout_to_english_if_needed(driver, country_code):
                    _wait_for_page_ready(driver, timeout_seconds=12, poll_seconds=1.0)
                    time.sleep(1)
                if _select_country_by_code(driver, country_code):
                    _wait_for_page_ready(driver, timeout_seconds=12, poll_seconds=1.0)
                    time.sleep(1)
                email_value = str(profile.get("email") or "").strip()
                _fill_input_by_id(driver, "email", email_value)
                _fill_input_by_selectors(driver, [
                    "#email",
                    "input[name='email']",
                    "input[autocomplete='email']",
                    "input[type='email']",
                ], email_value)
                _fill_input_by_id(driver, "phone", str(profile.get("phone") or "").strip())
                _fill_input_by_id(driver, "password", str(profile.get("password") or "").strip())
                date_of_birth = _normalize_paypal_date_of_birth(str(profile.get("dateOfBirth") or "").strip(), country_code)
                _fill_input_by_id(driver, "dateOfBirth", date_of_birth)
                _fill_input_by_selectors(driver, [
                    "#dateOfBirth",
                    "input[name='dateOfBirth']",
                    "input[id='dateOfBirth']",
                ], date_of_birth)
                _fill_input_by_id(driver, "firstName", str(profile.get("firstName") or "").strip())
                _fill_input_by_id(driver, "lastName", str(profile.get("lastName") or "").strip())
                _fill_input_by_id(driver, "countrySpecificFirstName", str(profile.get("countrySpecificFirstName") or "タロウ").strip())
                _fill_input_by_id(driver, "countrySpecificLastName", str(profile.get("countrySpecificLastName") or "ヤマダ").strip())
                _fill_input_by_id(driver, "billingLine1", str(address.get("street") or "").strip())
                _fill_input_by_id(driver, "billingCity", str(address.get("city") or "").strip())
                _fill_input_by_id(driver, "billingPostalCode", str(address.get("zip") or "").strip())
                state_candidates = [
                    str(address.get("prefecture") or "").strip(),
                    str(address.get("stateFull") or "").strip(),
                    str(address.get("State_Full") or "").strip(),
                    str(address.get("state") or "").strip(),
                    str(address.get("State") or "").strip(),
                    str(address.get("city") or "").strip(),
                ]
                _select_prefecture_or_state(driver, state_candidates)
                card_number = str(profile.get("cardNumber") or "").replace(" ", "")
                card_expiry = str(profile.get("cardExpiry") or "").strip()
                card_cvv = str(profile.get("cardCvv") or "").strip()
                if card_number:
                    _fill_input_by_id(driver, "cardNumber", card_number)
                    _fill_input_by_selectors(driver, [
                        "#cardNumber",
                        "input[name='cardNumber']",
                        "input[autocomplete='cc-number']",
                    ], card_number)
                if card_expiry:
                    _fill_input_by_id(driver, "cardExpiry", card_expiry)
                    _fill_input_by_selectors(driver, [
                        "#cardExpiry",
                        "input[name='exp-date']",
                        "input[autocomplete='cc-exp']",
                    ], card_expiry)
                if card_cvv:
                    _fill_input_by_id(driver, "cardCvv", card_cvv)
                    _fill_input_by_selectors(driver, [
                        "#cardCvv",
                        "input[name='cvv']",
                        "input[autocomplete='cc-csc']",
                    ], card_cvv)
                _click_next_or_submit(driver)
                time.sleep(3)
                continue

            if stage == PAYPAL_STAGE_WALLET_MODAL:
                _dismiss_add_payment_method_modal(driver)
                time.sleep(2)
                continue

            if stage == PAYPAL_STAGE_VERIFICATION:
                if external_verification:
                    return {
                        "ok": False,
                        "status": "verification_required",
                        "reason": "paypal_verification_required",
                        "page_url": url,
                        "page_title": str(state.get("title") or "").strip(),
                        "body_text": body,
                        "history": history,
                    }
                if verification_url:
                    code = ""
                    for _ in range(poll_attempts):
                        code = _fetch_verification_code(verification_url)
                        if code and code != last_verification_code:
                            break
                        time.sleep(poll_interval)
                    if code:
                        _fill_verification_code(driver, code)
                        last_verification_code = code
                        time.sleep(3)
                        continue
                if resend_attempts < max_resend and _click_verification_resend(driver):
                    resend_attempts += 1
                    time.sleep(2)
                    continue
                raise AdsPowerWorkerError("AdsPower hosted checkout verification stage did not receive a valid code")

            if stage in (PAYPAL_STAGE_REVIEW, PAYPAL_STAGE_APPROVAL):
                _click_review_consent(driver)
                time.sleep(3)
                continue

            if stage == PAYPAL_STAGE_REDIRECTING:
                time.sleep(2)
                continue

            time.sleep(1.5)
    finally:
        try:
            if driver is not None:
                driver.quit()
        except Exception:
            pass
        if close_profile_on_finish:
            client.stop_profile(profile_id)


def continue_hosted_checkout_in_profile(
    *,
    browser_backend: str = "adspower",
    api_base: str,
    ads_power_api_key: str = "",
    roxybrowser_api_key: str = "",
    profile_id: str,
    verification_code: str,
    guest_profile: dict[str, Any] | None = None,
    timeout_seconds: int = 240,
    close_profile_on_finish: bool = False,
) -> dict[str, Any]:
    normalized_code = re.sub(r"\D+", "", str(verification_code or ""))[:6]
    if len(normalized_code) != 6:
        raise AdsPowerWorkerError("verification_code must be a 6-digit code")
    return run_hosted_checkout_in_profile(
        browser_backend=browser_backend,
        api_base=api_base,
        ads_power_api_key=ads_power_api_key,
        roxybrowser_api_key=roxybrowser_api_key,
        profile_id=profile_id,
        target_url="about:blank",
        guest_profile=guest_profile,
        verification_code=normalized_code,
        external_verification=True,
        navigate_to_target=False,
        timeout_seconds=timeout_seconds,
        clear_cache_before_start=False,
        close_profile_on_finish=close_profile_on_finish,
    )


def click_hosted_checkout_verification_resend_in_profile(
    *,
    browser_backend: str = "adspower",
    api_base: str,
    ads_power_api_key: str = "",
    roxybrowser_api_key: str = "",
    profile_id: str,
    close_profile_on_finish: bool = False,
) -> dict[str, Any]:
    client = _create_browser_client(
        browser_backend=browser_backend,
        api_base=api_base,
        ads_power_api_key=ads_power_api_key,
        roxybrowser_api_key=roxybrowser_api_key,
    )
    launch = client.start_profile(profile_id)
    _ensure_supported_launch(browser_backend, launch)
    client.wait_after_start()

    driver = None
    try:
        driver = _create_connected_driver(launch)
        _switch_to_checkout_window(driver)
        _wait_for_page_ready(driver, timeout_seconds=20, poll_seconds=1.0)
        state = _detect_paypal_stage(driver)
        stage = str(state.get("stage") or PAYPAL_STAGE_UNKNOWN).strip()
        if stage != PAYPAL_STAGE_VERIFICATION:
            return {
                "ok": True,
                "status": stage,
                "reason": "not_on_verification_page",
                "resend_clicked": False,
                "page_url": str(state.get("url") or "").strip(),
                "page_title": str(state.get("title") or "").strip(),
                "body_text": _normalize_text(state.get("bodyText") or ""),
                "launch": launch,
            }
        clicked = _click_verification_resend(driver)
        time.sleep(1.5)
        next_state = _detect_paypal_stage(driver)
        return {
            "ok": bool(clicked),
            "status": str(next_state.get("stage") or PAYPAL_STAGE_UNKNOWN).strip(),
            "reason": "resend_clicked" if clicked else "resend_not_found",
            "resend_clicked": bool(clicked),
            "page_url": str(next_state.get("url") or "").strip(),
            "page_title": str(next_state.get("title") or "").strip(),
            "body_text": _normalize_text(next_state.get("bodyText") or ""),
            "launch": launch,
        }
    finally:
        try:
            if driver is not None:
                driver.quit()
        except Exception:
            pass
        if close_profile_on_finish:
            client.stop_profile(profile_id)
