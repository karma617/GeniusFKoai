import json
import importlib
import subprocess
import sys
from http.cookiejar import Cookie
import re

import httpx
from loguru import logger
from typing import Any, Optional
from socksio.exceptions import SOCKSError
from paypal.models import SessionState
from paypal.proxy import ProxyConfig, ProxyEntry
from config import USER_AGENT


def build_common_headers() -> dict:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        "sec-ch-ua-arch": '"x86"',
        "sec-ch-device-memory": "32",
    }


def _mask_middle(value: str, left: int = 6, right: int = 4) -> str:
    if len(value) <= left + right:
        return "<redacted>"
    return f"{value[:left]}...{value[-right:]}"


def _mask_email(value: str) -> str:
    if "@" not in value:
        return "<redacted>"
    local, domain = value.split("@", 1)
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[:2]}***{local[-1:]}@{domain}"


def _mask_digits(value: str, keep: int = 4) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) <= keep:
        return "<redacted>"
    return f"{'*' * (len(digits) - keep)}{digits[-keep:]}"


def sanitize_for_log(value: Any, key: str = "") -> Any:
    """Remove secrets and high-risk PII before writing diagnostics."""
    if isinstance(value, dict):
        return {k: sanitize_for_log(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_for_log(item, key) for item in value]
    if not isinstance(value, str):
        return value

    lowered_key = key.lower()
    compact_key = lowered_key.replace("_", "").replace("-", "")

    if compact_key in {"password", "securitycode", "cvv", "pin"}:
        return "<redacted>"
    if "authorization" in compact_key or "cookie" in compact_key:
        return "<redacted>"
    if "accesstoken" in compact_key or "euat" in compact_key:
        return "<redacted>"
    if compact_key in {"token", "batoken", "ectoken", "billingagreementid"}:
        return _mask_middle(value)
    if compact_key in {"cardnumber", "encryptednumber"}:
        return _mask_digits(value)
    if compact_key in {"cpf", "identitydocument", "document", "value"}:
        return "<redacted>"
    if compact_key == "email":
        return _mask_email(value)
    if compact_key in {"phonenumber", "phone", "number"} and sum(ch.isdigit() for ch in value) >= 8:
        return _mask_digits(value)

    return value


def _paypal_debug_id(headers: httpx.Headers) -> str:
    for name in ("paypal-debug-id", "Paypal-Debug-Id", "PayPal-Debug-Id"):
        value = headers.get(name)
        if value:
            return value
    return ""


class PayPalSession:
    """Manages HTTP session with cookie persistence and logging."""

    PROXY_REQUEST_ATTEMPTS = 3
    PROXY_MAX_ENTRIES_PER_REQUEST = 6
    _socks_dependency_install_attempted = False

    def __init__(
        self,
        state: SessionState,
        proxy_url: str | None = None,
        proxy_label: str = "",
        proxy_config: ProxyConfig | None = None,
    ):
        self.state = state
        self.proxy_entries: tuple[ProxyEntry, ...] = ()
        self.proxy_index = 0
        if proxy_config and proxy_config.enabled and proxy_config.entry:
            self.proxy_entries = proxy_config.entries or (proxy_config.entry,)
            self.proxy_index = max(0, min(proxy_config.current_index, len(self.proxy_entries) - 1))
            proxy_url = self.proxy_entries[self.proxy_index].url
            proxy_label = self.proxy_entries[self.proxy_index].masked
        self.proxy_url = proxy_url
        self.proxy_label = proxy_label or ("代理已开启" if proxy_url else "代理关闭")
        self._base_client_kwargs = {
            "follow_redirects": False,
            "timeout": httpx.Timeout(30.0),
            "headers": build_common_headers(),
            # 保证“关闭代理”时不被 HTTP_PROXY/HTTPS_PROXY 环境变量意外接管。
            "trust_env": False,
        }
        self.client = self._new_client()
        logger.info("HTTP outbound proxy: {}", self.proxy_label)

    def _new_client(self, cookies: httpx.Cookies | None = None) -> httpx.Client:
        client_kwargs = {
            **self._base_client_kwargs,
        }
        if self.proxy_url:
            client_kwargs["proxy"] = self.proxy_url
        if cookies is not None:
            client_kwargs["cookies"] = cookies
        try:
            return httpx.Client(**client_kwargs)
        except (ImportError, RuntimeError) as exc:
            if self.proxy_url and self.proxy_url.startswith(("socks5://", "socks5h://")) and self._is_socks_dependency_error(exc):
                self._ensure_socks_dependency(exc)
                return httpx.Client(**client_kwargs)
            raise

    @classmethod
    def _is_socks_dependency_error(cls, exc: BaseException) -> bool:
        text = str(exc).lower()
        return "socks" in text and ("not installed" in text or "socksio" in text)

    @classmethod
    def _ensure_socks_dependency(cls, original_exc: BaseException) -> None:
        if cls._socks_dependency_install_attempted:
            raise original_exc
        cls._socks_dependency_install_attempted = True
        logger.warning("SOCKS proxy dependency is missing; installing httpx[socks] with current Python...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "httpx[socks]", "httpcore[socks]"])
            importlib.invalidate_caches()
            for module_name in ("httpcore._sync", "httpcore", "httpx._transports.default"):
                module = sys.modules.get(module_name)
                if module is not None:
                    importlib.reload(module)
        except Exception as install_exc:
            raise RuntimeError("自动安装 httpx[socks] 失败，请检查 pip 网络或权限") from install_exc

    def _switch_proxy(self, used_proxy_count: int) -> bool:
        if not self.proxy_entries or used_proxy_count >= self.PROXY_MAX_ENTRIES_PER_REQUEST:
            return False
        if used_proxy_count >= len(self.proxy_entries):
            return False
        self.proxy_index = (self.proxy_index + 1) % len(self.proxy_entries)
        self.proxy_url = self.proxy_entries[self.proxy_index].url
        self.proxy_label = self.proxy_entries[self.proxy_index].masked
        cookies = self.client.cookies
        self.client.close()
        self.client = self._new_client(cookies=cookies)
        logger.warning("Proxy transport retries exhausted; switched outbound proxy to {}", self.proxy_label)
        return True

    def rotate_proxy_clean_session(self, exclude_urls: set[str] | None = None) -> bool:
        """Switch to next unused proxy with a clean cookie jar (DataDome/403)."""
        if not self.proxy_entries or len(self.proxy_entries) <= 1:
            return False
        excluded = set(exclude_urls or ())
        n = len(self.proxy_entries)
        start = self.proxy_index
        chosen = None
        for step in range(1, n + 1):
            idx = (start + step) % n
            entry = self.proxy_entries[idx]
            if entry.url in excluded:
                continue
            chosen = idx
            break
        if chosen is None:
            return False
        self.proxy_index = chosen
        self.proxy_url = self.proxy_entries[self.proxy_index].url
        self.proxy_label = self.proxy_entries[self.proxy_index].masked
        try:
            self.client.close()
        except Exception:
            pass
        # Fresh cookies: keep prior datadome/session cookies would re-taint the next hop.
        self.client = self._new_client(cookies=httpx.Cookies())
        for attr in ("datadome_cookie", "nsid", "d_id", "tltsid", "tltdid"):
            if hasattr(self.state, attr):
                try:
                    setattr(self.state, attr, "")
                except Exception:
                    pass
        logger.warning(
            "Phase0 dirty session; rotated proxy and cleared cookies -> {}",
            self.proxy_label,
        )
        return True

    @staticmethod
    def _is_proxy_transport_error(exc: BaseException) -> bool:
        current: BaseException | None = exc
        while current is not None:
            if isinstance(current, (httpx.TransportError, SOCKSError)):
                return True
            current = current.__cause__ or current.__context__
        return False

    def _request_with_proxy_retries(self, method: str, url: str, **kwargs) -> httpx.Response:
        used_proxy_count = 1 if self.proxy_url else 0
        attempt = 1
        while True:
            try:
                resp = self.client.request(method, url, **kwargs)
                self._sync_state_cookies()
                logger.debug(f"  -> {resp.status_code} ({len(resp.content)} bytes)")
                return resp
            except Exception as exc:
                if not self.proxy_url or not self._is_proxy_transport_error(exc):
                    raise
                logger.warning(
                    "Proxy transport error via {} on attempt {}/{}: {}",
                    self.proxy_label,
                    attempt,
                    self.PROXY_REQUEST_ATTEMPTS,
                    exc,
                )
                if attempt < self.PROXY_REQUEST_ATTEMPTS:
                    attempt += 1
                    continue
                if not self._switch_proxy(used_proxy_count):
                    raise
                used_proxy_count += 1
                attempt = 1

    def close(self):
        self.client.close()

    def export_cookie_list(self) -> list[dict]:
        """Export current jar as browser-friendly cookie dicts."""
        out: list[dict] = []
        for cookie in self.client.cookies.jar:
            if not isinstance(cookie, Cookie):
                continue
            item = {
                "name": cookie.name,
                "value": cookie.value or "",
                "domain": cookie.domain or ".paypal.com",
                "path": cookie.path or "/",
                "secure": bool(cookie.secure),
            }
            out.append(item)
        return out

    def import_browser_cookies(self, cookies: list[dict] | None) -> int:
        """Import cookies produced by headed browser assist into httpx jar."""
        if not cookies:
            return 0
        imported = 0
        for c in cookies:
            name = str(c.get("name") or "").strip()
            value = c.get("value")
            if not name or value is None:
                continue
            domain = str(c.get("domain") or ".paypal.com").strip() or ".paypal.com"
            path = str(c.get("path") or "/").strip() or "/"
            try:
                self.client.cookies.set(name, str(value), domain=domain, path=path)
                imported += 1
            except Exception:
                try:
                    self.client.cookies.set(name, str(value))
                    imported += 1
                except Exception:
                    pass
        self._sync_state_cookies()
        return imported

    def _sync_state_cookies(self):
        """Pull important cookies into SessionState after each request."""
        jar = self.client.cookies
        cookie_dict = {}
        # PayPal may set the same cookie name for multiple domain/path scopes
        # (ddgl is a common example). httpx.Cookies.items() raises
        # CookieConflict in that case, so iterate the underlying jar instead.
        for cookie in jar.jar:
            if isinstance(cookie, Cookie):
                cookie_dict[cookie.name] = cookie.value
        self.state.update_from_cookies(cookie_dict)

    def get(self, url: str, **kwargs) -> httpx.Response:
        logger.debug(f"GET {url}")
        return self._request_with_proxy_retries("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        logger.debug(f"POST {url}")
        return self._request_with_proxy_retries("POST", url, **kwargs)

    def graphql(self, operation_name: str, query: str, variables: dict,
                extra_headers: Optional[dict] = None,
                extra_body: Optional[dict] = None,
                batched: bool = False,
                endpoint: Optional[str] = None) -> dict:
        """Send a GraphQL request to PayPal's graphql endpoint."""
        url = endpoint or "https://www.paypal.com/graphql"
        if operation_name and endpoint is None:
            url = f"{url}?{operation_name}"

        context_token = str(
            variables.get("token")
            or variables.get("billingAgreementId")
            or self.state.ec_token
            or self.state.ba_token
        )
        referer = (
            self.state.signup_url
            if self.state.ec_token
            else f"https://www.paypal.com/pay?token={self.state.ba_token}&ul=1"
        )
        app_name = "checkoutuinodeweb" if operation_name == "authorize" else "checkoutuinodeweb_weasley"
        country = getattr(self.state, "country", "BR") or "BR"
        locale = getattr(self.state, "locale", "pt_BR") or "pt_BR"
        headers = {
            "Content-Type": "application/json",
            "X-App-Name": app_name,
            "X-Requested-With": "fetch",
            "PayPal-Client-Context": context_token,
            "PayPal-Client-Metadata-Id": context_token,
            "X-Country": country,
            "X-Locale": locale,
            "Origin": "https://www.paypal.com",
            "Referer": referer,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        if self.state.euat_token:
            headers["X-PayPal-Internal-EUAT"] = self.state.euat_token
        if extra_headers:
            # Passing None removes a default header. This is needed for the
            # browser-captured final Hagrid authorize call, which posts to
            # /graphql/ without PayPal-Client-Context/X-Country/X-Locale.
            for key, value in extra_headers.items():
                if value is None:
                    headers.pop(key, None)
                else:
                    headers[key] = value

        payload_item = {
            "operationName": operation_name,
            "variables": variables,
            "query": query,
        }
        if extra_body:
            # checkoutweb/weasley injects fn_sync_data at the top level of the
            # GraphQL JSON body for SignUpNewMemberMutation.
            payload_item.update(extra_body)

        payload = [payload_item] if batched else payload_item

        resp = self.post(url, json=payload, headers=headers)
        debug_id = _paypal_debug_id(resp.headers)
        logger.info(
            "GraphQL {} HTTP {} bytes={} paypal_debug_id={}",
            operation_name,
            resp.status_code,
            len(resp.content),
            debug_id or "<missing>",
        )

        try:
            result = resp.json()
        except ValueError:
            # PayPal sometimes returns an auth-challenge or interstitial HTML
            # page (status 200) instead of JSON.  For SignUpNewMember this can
            # happen when the card-add step triggers a contingency but the
            # member account has already been created with an EUAT token
            # embedded in the page.  Try to extract the token from the HTML
            # so the flow can continue to Phase 4 billing without re-submitting
            # the card.
            html_body = resp.text or ""
            logger.warning(
                "GraphQL {} returned non-JSON response: status={} paypal_debug_id={} "
                "body_len={} - attempting token extraction",
                operation_name,
                resp.status_code,
                debug_id or "<missing>",
                len(html_body),
            )
            token_match = re.search(
                r'(?:accessToken|euat|x-paypal-internal-euat)["\x27\s:=]+([A-Za-z0-9_\-]{40,})',
                html_body,
            )
            if token_match:
                extracted_token = token_match.group(1)
                logger.info("Extracted EUAT token from non-JSON response")
                self.state.euat_token = extracted_token
                return {"data": {"onboardAccount": {"buyer": {"auth": {"accessToken": extracted_token}, "userId": ""}}}, "errors": []}

            # Check response headers for EUAT token
            header_euat = (
                resp.headers.get("x-paypal-internal-euat", "")
                or resp.headers.get("X-PayPal-Internal-EUAT", "")
            )
            if header_euat:
                logger.info("Found EUAT token in response headers after non-JSON response")
                self.state.euat_token = header_euat
                return {"data": {"onboardAccount": {"buyer": {"auth": {"accessToken": header_euat}, "userId": ""}}}, "errors": []}

            # Also check cookies for the EUAT cookie
            euat_key = "AV894Kt2TSumQQrJwe-8mzmyREO"
            euat_cookie = ""
            try:
                euat_cookie = self.client.cookies.get(euat_key, "") or ""
            except Exception:
                # httpx raises if multiple cookies share the same name
                try:
                    for cookie in self.client.cookies.jar:
                        if cookie.name == euat_key and cookie.value:
                            euat_cookie = cookie.value
                            break
                except Exception:
                    euat_cookie = ""
            if euat_cookie:
                logger.info("Found EUAT token in cookies after non-JSON response")
                self.state.euat_token = euat_cookie
                return {"data": {"onboardAccount": {"buyer": {"auth": {"accessToken": euat_cookie}, "userId": ""}}}, "errors": []}

            logger.error(
                "GraphQL {} non-JSON response body (first 2000): {}",
                operation_name,
                html_body[:2000],
            )
            raise

        result_items = result if isinstance(result, list) else [result]
        for item in result_items:
            if not isinstance(item, dict) or not item.get("errors"):
                continue

            logger.error(
                "GraphQL {} returned errors: status={} paypal_debug_id={} errors={}",
                operation_name,
                resp.status_code,
                debug_id or "<missing>",
                json.dumps(
                    sanitize_for_log(item.get("errors")),
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            logger.debug(
                "GraphQL {} sanitized variables: {}",
                operation_name,
                json.dumps(
                    sanitize_for_log(variables),
                    ensure_ascii=False,
                    indent=2,
                ),
            )

        return result

