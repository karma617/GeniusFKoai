"""Main PayPal Billing Agreement approval flow orchestrator.

Implements the complete protocol:
  Phase 0: DataDome verification + initial page load
  Phase 1: Device fingerprint + Tealeaf + hCaptcha
  Phase 2: Create account (email submission → signup page)
  Phase 3: Fill signup form + submit (triggers 2FA SMS)
  Phase 4: OTP verification + final authorize mutation
"""
import re
import time
import json
import urllib.parse
from loguru import logger

from paypal.models import (
    SessionState,
    UserInfo,
    CardInfo,
    BillingAddress,
    generate_address,
    generate_card,
    generate_random_email,
)
from paypal.session import PayPalSession, sanitize_for_log
from paypal.browser_assist import solve_with_headed_browser
from paypal.proxy import build_proxy_config, ProxyConfig
from paypal.fingerprint import (
    build_fn_sync_data,
    build_signup_fn_sync_data,
    send_device_fingerprint,
    send_signup_field_events,
    send_otp_challenge,
)
from paypal.tealeaf import send_tealeaf_data
from paypal.analytics import (
    send_xo_logger,
    send_analytics_ts,
    send_observability_emit,
    send_weasley_log,
)
from paypal.graphql import (
    CHECKOUT_SESSION_DATA_QUERY,
    GRIFFIN_METADATA_QUERY,
    SUPPORTED_FUNDING_SOURCES_QUERY,
    DEFERRED_FEATURE_QUERY,
    INSTALLMENT_OPTIONS_QUERY,
    ADDRESS_AUTOCOMPLETE_FROM_POSTAL_CODE_QUERY,
    INITIATE_2FA_PHONE_MUTATION,
    CONFIRM_2FA_PHONE_MUTATION,
    SIGNUP_NEW_MEMBER_MUTATION,
    AUTHORIZE_BILLING_MUTATION,
    BILLING_AGREEMENT_CONTEXT_QUERY,
)


class PayPalFlow:
    checkout_channel = "WEB"
    direct_signup_from_initial_ec = False
    require_ec_signup_context = False

    def __init__(
        self,
        ba_token: str,
        user: UserInfo,
        card: CardInfo,
        address: BillingAddress,
        max_card_attempts: int = 5,
        max_phone_changes: int = 5,
        proxy_enabled: bool | None = None,
        proxy_index: int | None = None,
        proxy_config: ProxyConfig | None = None,
    ):
        self.ba_token = ba_token
        self.user = user
        if not self.user.email:
            self.user.email = generate_random_email()
        self.card = card
        self.address = address
        self.max_card_attempts = max(1, max_card_attempts)
        self.max_phone_changes = max(0, min(int(max_phone_changes), 20))
        self.proxy_config: ProxyConfig = proxy_config or build_proxy_config(
            enabled=proxy_enabled,
            index=proxy_index,
        )
        self.state = SessionState(ba_token=ba_token)
        self.state.country = self.address.country
        self.state.lang = "pt"
        self.state.locale = "pt_BR"
        self.session = PayPalSession(
            self.state,
            proxy_url=self.proxy_config.url,
            proxy_label=self.proxy_config.label,
            proxy_config=self.proxy_config,
        )

    def close(self):
        self.session.close()

    def run(self) -> dict:
        """Execute the complete flow. Returns result dict with status and return_url."""
        try:
            logger.info(f"=== PayPal Billing Agreement Flow ===")
            logger.info("BA Token: {}", sanitize_for_log({"ba_token": self.ba_token})["ba_token"])
            logger.info("Email: {}", sanitize_for_log({"email": self.user.email})["email"])
            logger.info("Phone: {}", sanitize_for_log({"phone": self.user.phone})["phone"])
            logger.info(f"Proxy: {self.proxy_config.label}")

            self._phase0_initial_load()
            self._phase1_risk_controls()
            self._phase2_create_account()
            self._phase3_signup_and_2fa()
            result = self._phase4_authorize()

            if result.get("status") == "success":
                logger.success(f"=== Flow completed successfully ===")
            else:
                logger.error(f"=== Flow completed with error status ===")
            return result
        except Exception as e:
            logger.error(f"Flow failed: {e}")
            raise
        finally:
            self.close()

    def _phase0_page_signals(self, resp, html: str) -> dict:
        text = html or ""
        lowered = text.lower()
        page_len = len(text)
        has_context = bool(self.state.ec_token or self.state.ssrt)
        has_modxo = bool(
            self.state.show_create_account_action_id or self.state.create_user_action_id
        )
        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()[:80]
        return {
            "status": getattr(resp, "status_code", 0) or 0,
            "bytes": page_len,
            "has_context": has_context,
            "has_modxo": has_modxo,
            "has_datadome": "datadome" in lowered,
            "has_captcha_delivery": (
                "geo.captcha-delivery.com" in lowered or "captcha-delivery.com" in lowered
            ),
            "has_adsddtoken": "adsddtoken" in lowered,
            "title": title,
        }

    def _phase0_dirty_reason(self, resp, html: str) -> str:
        """Return empty string when Phase0 page is usable; otherwise a short reason."""
        if resp is None:
            return "no_response"
        status = getattr(resp, "status_code", 0) or 0
        if status in (401, 403, 429):
            return f"http_{status}"
        if status != 200:
            return f"http_{status}"

        sig = self._phase0_page_signals(resp, html)
        page_len = int(sig["bytes"])
        has_context = bool(sig["has_context"])
        has_modxo = bool(sig["has_modxo"])

        # Usable BA pages typically extract EC/SSRT/ModXO and are ~100KB+.
        # Soft-block shells are often ~8-20KB with no BA context.
        if page_len < 40000 and not has_context and not has_modxo:
            if sig["has_captcha_delivery"] or sig["has_adsddtoken"]:
                return f"soft_captcha_shell bytes={page_len}"
            if sig["has_datadome"]:
                return f"soft_datadome_shell bytes={page_len}"
            return f"soft_block_shell bytes={page_len} no_ec_ssrt_modxo"

        if page_len < 20000 and (
            sig["has_captcha_delivery"]
            or (sig["has_adsddtoken"] and not has_context)
        ):
            return f"captcha_markers bytes={page_len}"

        if sig["has_datadome"] and page_len < 20000 and not has_context and not has_modxo:
            return f"datadome_no_context bytes={page_len}"

        if page_len < 5000 and not has_context:
            return f"tiny_page bytes={page_len}"

        return ""

    def _browser_assist_enabled(self) -> bool:
        import os
        raw = (os.getenv("PAYPAL_BROWSER_ASSIST") or "1").strip().lower()
        return raw not in {"0", "false", "no", "off"}

    def _browser_assist_timeout_sec(self) -> float:
        import os
        try:
            return max(30.0, float((os.getenv("PAYPAL_BROWSER_ASSIST_TIMEOUT") or "120").strip() or "120"))
        except Exception:
            return 120.0

    def _run_headed_browser_assist(
        self,
        url: str,
        *,
        purpose: str,
        otp_phone_local: str | None = None,
        otp_token: str | None = None,
        signup_variables: dict | None = None,
        signup_token: str | None = None,
        signup_fn_sync_data: str | None = None,
        bootstrap_url: str | None = None,
    ):
        """Open headed browser for DataDome/authchallenge and import cookies.

        Returns BrowserAssistResult on success, None on failure.
        """
        if not self._browser_assist_enabled():
            logger.warning("Headed browser assist disabled by PAYPAL_BROWSER_ASSIST=0")
            return None
        if not url:
            return None
        seed = []
        try:
            seed = self.session.export_cookie_list()
        except Exception:
            seed = []
        logger.warning(
            "Launching headed browser assist for {} ... if a captcha appears, complete it; "
            "if signup form already shows, do nothing",
            purpose,
        )
        # Always reuse the same outbound proxy as the HTTP session.
        # Fall back to proxy_config only if session attribute is missing/empty.
        proxy_url = getattr(self.session, "proxy_url", None) or getattr(
            self.proxy_config, "url", None
        )
        proxy_label = (
            getattr(self.session, "proxy_label", None)
            or getattr(self.proxy_config, "label", None)
            or ("代理已开启" if proxy_url else "代理关闭")
        )
        if getattr(self.proxy_config, "enabled", False) and not proxy_url:
            raise RuntimeError(
                "Headed browser assist refused bare connect: proxy is enabled but session proxy_url is empty"
            )
        kwargs = {
            "proxy_url": proxy_url,
            "seed_cookies": seed,
            "timeout_sec": self._browser_assist_timeout_sec(),
            "purpose": purpose,
            "bootstrap_url": bootstrap_url,
        }
        logger.info(
            "Headed browser assist proxy binding purpose={} proxy_label={} proxy_bound={}",
            purpose,
            proxy_label,
            bool(proxy_url),
        )
        if purpose == "otp_authchallenge":
            from paypal.graphql import INITIATE_2FA_PHONE_MUTATION
            kwargs.update(
                {
                    "otp_phone_local": otp_phone_local or self.user.phone_local,
                    "otp_country": self.address.country,
                    "otp_lang": self.state.lang or "pt",
                    "otp_token": otp_token or self.state.ec_token,
                    "otp_mutation": INITIATE_2FA_PHONE_MUTATION,
                }
            )
        if purpose == "signup_authchallenge":
            kwargs.update(
                {
                    "signup_variables": signup_variables or self._build_signup_variables(
                        signup_token or self.state.ec_token or self.ba_token
                    ),
                    "signup_mutation": SIGNUP_NEW_MEMBER_MUTATION,
                    "signup_token": signup_token or self.state.ec_token or self.ba_token,
                    "signup_country": self.address.country,
                    "signup_lang": self.state.lang or "pt",
                    "signup_fn_sync_data": (
                        signup_fn_sync_data
                        if signup_fn_sync_data is not None
                        else build_signup_fn_sync_data(
                            signup_token or self.state.ec_token or self.ba_token
                        )
                    ),
                }
            )
        result = solve_with_headed_browser(url, **kwargs)
        if result.cookies:
            imported = self.session.import_browser_cookies(result.cookies)
            logger.info("Imported {} browser cookies after {}", imported, purpose)
        if not result.ok:
            logger.error(
                "Headed browser assist failed purpose={} reason={} final_url={} bytes={}",
                purpose,
                result.reason,
                (result.final_url or "")[:160],
                result.page_bytes,
            )
            return None
        # Refresh tokens from final URL when available.
        final_url = result.final_url or url
        ssrt_match = re.search(r"ssrt=(\d+)", final_url)
        if ssrt_match:
            self.state.ssrt = ssrt_match.group(1)
        ec_match = re.search(r"EC-[A-Za-z0-9]+", final_url)
        if ec_match:
            self.state.ec_token = ec_match.group(0)
        logger.success(
            "Headed browser assist success purpose={} final_url={} bytes={} otp={} signup={}",
            purpose,
            final_url[:160],
            result.page_bytes,
            bool(getattr(result, "otp_auth_id", "") and getattr(result, "otp_challenge_id", "")),
            getattr(result, "signup_result", None) is not None,
        )
        return result


    def _phase0_is_dirty(self, resp, html: str) -> bool:
        """True when Phase0 is clearly blocked (DataDome/captcha/tiny page).

        Normal PayPal approval HTML often embeds DataDome script references even
        when the session is usable. Only treat those markers as dirty when the
        page is tiny, missing BA context, or is an actual captcha challenge.
        """
        return bool(self._phase0_dirty_reason(resp, html))

    def _phase0_reset_partial_state(self) -> None:
        """Clear tokens captured from a dirty Phase0 attempt before retry."""
        self.state.ssrt = ""
        self.state.ctx_id = ""
        self.state.ec_token = ""
        self.state.show_create_account_action_id = ""
        self.state.create_user_action_id = ""

    def _phase0_attempt_once(self):
        """One Phase0 attempt: approve page load + parse context."""
        url = f"https://www.paypal.com/agreements/approve?ba_token={self.ba_token}"

        # First GET - may return 403 with DataDome challenge or 302 redirect
        resp = self.session.get(url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        })

        if resp.status_code == 403:
            logger.info("Got 403 - DataDome challenge detected")
            # Empty adsddtoken cannot solve browser captcha; treat as dirty and
            # let outer retry rotate proxy. Keep one soft follow only for logs.
            logger.warning(
                "DataDome challenge on current proxy/session; will rotate if more proxies available."
            )
            return resp, resp.text or ""

        if resp.status_code == 302:
            redirect_url = resp.headers.get("Location", "")
            logger.info(f"Redirected to: {redirect_url}")
            # Extract ssrt from redirect URL
            ssrt_match = re.search(r"ssrt=(\d+)", redirect_url)
            if ssrt_match:
                self.state.ssrt = ssrt_match.group(1)
            # Follow the redirect
            if redirect_url.startswith("/"):
                redirect_url = f"https://www.paypal.com{redirect_url}"
            # Captcha redirect with empty adsddtoken is still dirty.
            if "adsddtoken=" in redirect_url and "YWRzZGRjYXB0Y2hh=1" in redirect_url:
                logger.warning("Redirected into DataDome/captcha agreement path")
                resp = self.session.get(redirect_url)
                return resp, resp.text or ""
            resp = self.session.get(redirect_url)

        # Parse the login/signup page
        html = resp.text
        logger.info(f"Page loaded: {resp.status_code}, {len(html)} bytes")
        self._extract_modxo_action_ids(html, str(resp.url))

        # Extract ctxId
        ctx_match = re.search(r'"ctxId"[^"]*"([^"]+)"', html)
        if ctx_match:
            self.state.ctx_id = ctx_match.group(1)
            logger.info(f"Context ID: {self.state.ctx_id}")

        # Extract ssrt if not yet found
        if not self.state.ssrt:
            ssrt_match = re.search(r"ssrt=(\d+)", str(resp.url))
            if not ssrt_match:
                ssrt_match = re.search(r"ssrt=(\d+)", html)
            if ssrt_match:
                self.state.ssrt = ssrt_match.group(1)
                logger.info(f"SSRT: {self.state.ssrt}")

        if not self.state.ec_token:
            ec_match = re.search(r"EC-[A-Za-z0-9]+", f"{resp.url}\n{html}")
            if ec_match:
                self.state.ec_token = ec_match.group(0)
                logger.info(
                    "EC Token (from initial approval context): {}",
                    sanitize_for_log({"ec_token": self.state.ec_token})["ec_token"],
                )
        return resp, html

    def _phase0_initial_load(self):
        """Load the agreement approval page; rotate proxy on DataDome/dirty session."""
        logger.info("--- Phase 0: Initial page load ---")

        pool_size = len(getattr(self.session, "proxy_entries", None) or ())
        max_attempts = 1
        if pool_size > 0:
            max_attempts = min(3, max(1, pool_size))
        # refresh_proxy (API or pool) can supply a different exit even with 1 live entry.
        if callable(getattr(self, "refresh_proxy", None)):
            max_attempts = max(max_attempts, 3)
        last_status = 0
        last_bytes = 0
        used_proxy_urls: set[str] = set()
        current_url = getattr(self.session, "proxy_url", None) or ""
        if current_url:
            used_proxy_urls.add(current_url)

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                self._phase0_reset_partial_state()
                logger.info("Phase0 retry {}/{} with rotated proxy...", attempt, max_attempts)

            resp, html = self._phase0_attempt_once()
            last_status = getattr(resp, "status_code", 0) or 0
            last_bytes = len(html or "")
            current_url = getattr(self.session, "proxy_url", None) or current_url
            if current_url:
                used_proxy_urls.add(current_url)

            if not self._phase0_is_dirty(resp, html):
                if attempt > 1:
                    logger.success(
                        "Phase0 recovered after proxy rotation: status={} bytes={}",
                        last_status,
                        last_bytes,
                    )
                return

            dirty_reason = self._phase0_dirty_reason(resp, html) or "unknown"
            sig = self._phase0_page_signals(resp, html)
            logger.warning(
                "Phase0 dirty/blocked on attempt {}/{}: status={} bytes={} reason={} "
                "context={} modxo={} datadome={} captcha={} title={!r} proxy={}",
                attempt,
                max_attempts,
                last_status,
                last_bytes,
                dirty_reason,
                sig.get("has_context"),
                sig.get("has_modxo"),
                sig.get("has_datadome"),
                sig.get("has_captcha_delivery"),
                sig.get("title") or "",
                getattr(self.session, "proxy_label", self.proxy_config.label),
            )
            # Prefer headed browser on the current proxy before rotating away.
            assist_url = f"https://www.paypal.com/agreements/approve?ba_token={self.ba_token}"
            if self._run_headed_browser_assist(assist_url, purpose="phase0_datadome"):
                # Re-load via HTTP with imported cookies; if still dirty, continue rotate.
                try:
                    self._phase0_reset_partial_state()
                    resp2, html2 = self._phase0_attempt_once()
                    if not self._phase0_is_dirty(resp2, html2):
                        logger.success(
                            "Phase0 recovered after headed browser assist: status={} bytes={}",
                            getattr(resp2, "status_code", 0),
                            len(html2 or ""),
                        )
                        return
                    logger.warning(
                        "Headed browser assist finished but HTTP Phase0 still dirty: status={} bytes={} reason={}",
                        getattr(resp2, "status_code", 0),
                        len(html2 or ""),
                        self._phase0_dirty_reason(resp2, html2),
                    )
                except Exception as e:
                    logger.warning("Phase0 reload after browser assist failed: {}", e)
            if attempt >= max_attempts:
                break
            rotated = False
            try:
                rotated = self.session.rotate_proxy_clean_session(exclude_urls=used_proxy_urls)
            except TypeError:
                # Backward-compatible if session helper has not been updated yet.
                try:
                    rotated = self.session.rotate_proxy_clean_session()
                    if rotated:
                        next_url = getattr(self.session, "proxy_url", "") or ""
                        if next_url and next_url in used_proxy_urls:
                            rotated = False
                except Exception as e:
                    logger.warning("Proxy rotation failed: {}", e)
            except Exception as e:
                logger.warning("Proxy rotation failed: {}", e)

            # If pool rotation failed (single sticky line / all used), try refresh callback.
            if not rotated and callable(getattr(self, "refresh_proxy", None)):
                try:
                    try:
                        new_cfg = self.refresh_proxy(exclude_urls=used_proxy_urls)
                    except TypeError:
                        new_cfg = self.refresh_proxy()
                    if new_cfg and new_cfg.enabled and new_cfg.entry:
                        next_url = new_cfg.entry.url
                        if next_url in used_proxy_urls:
                            logger.warning(
                                "Phase0 proxy refresh returned already-used exit {}; stop rotating",
                                new_cfg.label,
                            )
                        else:
                            self.proxy_config = new_cfg
                            self.session.proxy_entries = new_cfg.entries or (new_cfg.entry,)
                            # Keep index on the chosen entry, not always 0.
                            try:
                                self.session.proxy_index = list(self.session.proxy_entries).index(new_cfg.entry)
                            except ValueError:
                                self.session.proxy_index = max(0, int(getattr(new_cfg, "current_index", 0) or 0))
                            self.session.proxy_url = new_cfg.entry.url
                            self.session.proxy_label = new_cfg.label
                            try:
                                self.session.client.close()
                            except Exception:
                                pass
                            import httpx as _httpx
                            self.session.client = self.session._new_client(cookies=_httpx.Cookies())
                            for attr in ("datadome_cookie", "nsid", "d_id", "tltsid", "tltdid"):
                                if hasattr(self.session.state, attr):
                                    setattr(self.session.state, attr, "")
                            used_proxy_urls.add(next_url)
                            rotated = True
                            logger.warning(
                                "Phase0 dirty session; refreshed proxy -> {}",
                                new_cfg.label,
                            )
                except Exception as e:
                    logger.warning("Proxy refresh failed: {}", e)

            if not rotated:
                break
            # Keep flow-level label in sync for subsequent logs.
            if self.session.proxy_entries:
                self.proxy_config = ProxyConfig(
                    enabled=True,
                    entry=self.session.proxy_entries[self.session.proxy_index],
                    entries=self.session.proxy_entries,
                    current_index=self.session.proxy_index,
                )
            logger.info("Retrying Phase0 with proxy: {}", self.proxy_config.label)

        raise RuntimeError(
            f"Phase0 blocked/dirty session: status={last_status} bytes={last_bytes}. "
            "Got soft-block/DataDome shell instead of BA page. Use multi-line "
            "residential sticky sessions (different session ids), prefer US/BR "
            "exits, then reopen the task."
        )

    @staticmethod
    def _extract_window_initial_data(html: str) -> dict:
        """Extract checkoutweb/weasley window.__INITIAL_DATA__ JSON."""
        # The page contains many reads of window.__INITIAL_DATA__ before the
        # actual server-side assignment.  Anchor on `= {` so we do not parse a
        # JavaScript function body from an earlier reference.
        marker = re.search(r"window\.__INITIAL_DATA__\s*=", html or "")
        if not marker:
            return {}

        start = html.find("{", marker.end())
        if start < 0:
            return {}

        depth = 0
        in_str = False
        escape = False
        for idx in range(start, len(html)):
            ch = html[idx]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue

            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[start:idx + 1])
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to parse __INITIAL_DATA__: {e}")
                        return {}

        return {}

    @staticmethod
    def _extract_content_identifier(html: str, country: str = "BR", lang: str = "pt") -> str:
        """Extract or build the dynamic signup terms contentIdentifier."""
        for pattern in (
            r'"contentIdentifier"\s*:\s*"([^"]*signupTerms[^"]*)"',
            r'\\"contentIdentifier\\"\s*:\s*\\"([^"\\]*signupTerms[^"\\]*)\\"',
            r'([A-Z]{2}:[a-z]{2}:[0-9a-f]{16,64}:compliance\.signupTerms)',
        ):
            match = re.search(pattern, html or "", re.I)
            if match:
                return match.group(1).replace("\\/", "/")
        return f"{country}:{lang}:compliance.signupTerms"

    def _build_signup_url(self) -> str:
        """Build the canonical checkoutweb/signup URL used as GraphQL Referer."""
        params: list[tuple[str, str]] = []
        if self.state.ssrt:
            params.append(("ssrt", self.state.ssrt))
        params.extend([
            ("ul", "1"),
            ("modxo_redirect_reason", "guest_user"),
            ("locale.x", self.state.locale),
            ("country.x", self.state.country),
            ("ba_token", self.ba_token),
            ("token", self.state.ec_token),
            ("rcache", "1"),
            ("cookieBannerVariant", "hidden"),
        ])
        return "https://www.paypal.com/checkoutweb/signup?" + urllib.parse.urlencode(params)

    @staticmethod
    def _extract_onboarding_redirect(rsc_text: str) -> str:
        """Extract onboardingRedirectUrl from Next/RSC server-action response."""
        match = re.search(r'"onboardingRedirectUrl"\s*:\s*"([^"]+)"', rsc_text or "")
        if not match:
            return ""
        return match.group(1).replace("\\/", "/")

    @staticmethod
    def _find_access_token(value) -> str:
        """Find an accessToken recursively in GraphQL data/errorData."""
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "accessToken" and isinstance(item, str) and item:
                    return item
                found = PayPalFlow._find_access_token(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = PayPalFlow._find_access_token(item)
                if found:
                    return found
        return ""

    @staticmethod
    def _has_buyer_not_set(result) -> bool:
        items = result if isinstance(result, list) else [result]
        for item in items:
            if not isinstance(item, dict):
                continue
            for err in item.get("errors") or []:
                data = err.get("data") or {}
                if data.get("contingency") == "BUYER_NOT_SET":
                    return True
                if err.get("message") == "BUYER_NOT_SET":
                    return True
        return False

    @staticmethod
    def _extract_buyer_id_from_html(html: str) -> str:
        text = html or ""
        patterns = (
            r'"userId"\s*:\s*"([A-Z0-9]{8,20})"',
            r'(?:party_id|cust|userId|payerId)["=:]+([A-Z0-9]{8,20})',
            r'"payerId"\s*:\s*"([A-Z0-9]{8,20})"',
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def _parse_authorize_payload(auth_result) -> tuple[str, str, dict]:
        """Return (return_url, ba_token, authorize_data)."""
        result_obj = auth_result[0] if isinstance(auth_result, list) else auth_result
        authorize_data = (
            ((result_obj or {}).get("data") or {})
            .get("billing", {})
            .get("authorize")
            or {}
        )
        if not isinstance(authorize_data, dict):
            return "", "", {}
        return_url = ((authorize_data.get("returnURL") or {}).get("href") or "").strip()
        ba_token = (authorize_data.get("billingAgreementToken") or "").strip()
        return return_url, ba_token, authorize_data

    def _build_pay_billing_url(self) -> str:
        import urllib.parse as _urlparse

        reason = self._encode_hermes_reason(
            self.state.signup_contingency_reason or "CARD_GENERIC_ERROR"
        )
        params = [
            ("ssrt", self.state.ssrt),
            ("token", self.state.ec_token or self.ba_token),
            ("rcache", "1"),
            ("country.x", self.address.country or self.state.country or "BR"),
            ("locale.x", self.state.locale or "pt_BR"),
            ("fromSignupLite", "true"),
            ("addFIContingency", "noretry"),
            ("redirectToHermes", "true"),
            ("fallback", "1"),
            ("reason", reason),
            ("ul", "1"),
        ]
        return "https://www.paypal.com/pay/billing?" + _urlparse.urlencode(params)

    def _extract_pay_billing_action_id(self, html: str) -> str:
        text = html or ""
        # Prefer nearby Next-Action hashes around billing action names.
        for key in (
            "approveBillingAgreement",
            "Approve_Billing_Agreement",
            "billingAgreement",
            "pay/billing",
        ):
            idx = text.find(key)
            if idx < 0:
                continue
            window = text[max(0, idx - 2000): idx + 2000]
            ids = re.findall(r'"([0-9a-f]{32,64})"', window)
            if ids:
                return ids[-1]
        ids = re.findall(
            r'Next-Action["\']?\s*[:=]\s*["\']([0-9a-f]{32,64})',
            text,
            re.I,
        )
        return ids[-1] if ids else ""

    def _extract_return_url_from_text(self, text: str) -> str:
        patterns = (
            r'"returnUrl"\s*:\s*"([^"]+)"',
            r'"returnURL"\s*:\s*\{\s*"href"\s*:\s*"([^"]+)"',
            r'(https://pm-redirects\.stripe\.com/return/[^"\\\s<]+)',
            r'(https://[^"\\\s<]*openai\.com[^"\\\s<]*)',
        )
        for pattern in patterns:
            match = re.search(pattern, text or "", re.I)
            if match:
                return match.group(1).replace("\\u0026", "&").replace("\\/", "/")
        return ""

    def _phase4_pay_billing_recovery(self, referer: str) -> dict | None:
        """Last-chance recovery when GraphQL authorize stays BUYER_NOT_SET.

        Some BR sessions only complete on /pay/billing after Hermes review.
        """
        import urllib.parse as _urlparse

        billing_url = self._build_pay_billing_url()
        try:
            logger.info("Phase4 recovery: loading /pay/billing after BUYER_NOT_SET...")
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": referer or self.state.signup_url or "",
                "Upgrade-Insecure-Requests": "1",
            }
            if self.state.euat_token:
                headers["X-PayPal-Internal-EUAT"] = self.state.euat_token
            resp = self.session.get(billing_url, headers=headers)
            html = resp.text or ""
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if location:
                    billing_url = _urlparse.urljoin(billing_url, location)
                    resp = self.session.get(
                        billing_url,
                        headers={**headers, "Referer": referer or billing_url},
                    )
                    html = resp.text or ""

            # If Hermes redirected into a real billing shell, refresh action id/url.
            m = re.search(r'https://www\.paypal\.com/pay/billing[^"\\\s<]+', html)
            if m:
                billing_url = m.group(0).replace("\\u0026", "&").replace("\\/", "/")
            next_action = self._extract_pay_billing_action_id(html)
            if not next_action:
                logger.warning("Phase4 recovery: /pay/billing Next-Action missing")
                # Still try to parse return URL if page already redirected.
                return_url = self._extract_return_url_from_text(html)
                if return_url:
                    final_redirect_url = self._follow_return_url(return_url, billing_url)
                    return {
                        "status": "success",
                        "ba_token": self.ba_token,
                        "ec_token": self.state.ec_token,
                        "user_id": self.state.user_id,
                        "return_url": return_url,
                        "final_redirect_url": final_redirect_url or return_url,
                        "payment_action": "BILLING_AGREEMENT",
                        "recovery": "pay_billing",
                    }
                return None

            logger.info("Phase4 recovery: submitting /pay/billing Next-Action...")
            post_headers = {
                "Accept": "text/x-component",
                "Origin": "https://www.paypal.com",
                "Referer": billing_url,
                "Next-Action": next_action,
            }
            if self.state.euat_token:
                post_headers["X-PayPal-Internal-EUAT"] = self.state.euat_token
            # Minimal multipart body; browser often posts empty form fields + 3DS placeholders.
            files = [
                ("1_token", (None, self.state.ec_token or self.ba_token)),
                ("1_ba_token", (None, self.ba_token)),
                ("0", (None, '["$K1"]')),
            ]
            billing_resp = self.session.post(billing_url, files=files, headers=post_headers)
            body = billing_resp.text or ""
            logger.info(
                "Phase4 recovery /pay/billing result: status={} bytes={}",
                billing_resp.status_code,
                len(billing_resp.content or b""),
            )
            return_url = self._extract_return_url_from_text(body)
            if not return_url:
                action_redirect = billing_resp.headers.get("x-action-redirect", "")
                if action_redirect:
                    return_url = action_redirect.split(";", 1)[0]
            if not return_url and billing_resp.status_code in (301, 302, 303, 307, 308):
                return_url = billing_resp.headers.get("Location", "")
            if not return_url:
                return None
            final_redirect_url = self._follow_return_url(return_url, billing_url)
            logger.success("Phase4 recovery /pay/billing produced returnURL")
            return {
                "status": "success",
                "ba_token": self.ba_token,
                "ec_token": self.state.ec_token,
                "user_id": self.state.user_id,
                "return_url": return_url,
                "final_redirect_url": final_redirect_url or return_url,
                "payment_action": "BILLING_AGREEMENT",
                "recovery": "pay_billing",
            }
        except Exception as e:
            logger.warning("Phase4 recovery /pay/billing failed: {}", e)
            return None

    def _extract_modxo_action_ids(self, html: str, base_url: str):
        """Extract Next server-action IDs from ModXO JS chunks.

        The browser sends these values in the Next-Action header. They are
        deployment-specific, so hard-coding the values from one capture breaks
        after PayPal ships a new bundle.
        """
        action_names = {
            "show_create_account_action_id": "showCreateAccountAction",
            "create_user_action_id": "createUserAction",
        }

        def scan(text: str) -> bool:
            changed = False
            for attr, action_name in action_names.items():
                if getattr(self.state, attr):
                    continue
                name_idx = text.find(f'"{action_name}"')
                if name_idx < 0:
                    continue
                window = text[max(0, name_idx - 500):name_idx]
                ids = re.findall(r'"([0-9a-f]{32,64})"', window)
                if ids:
                    action_id = ids[-1]
                    setattr(self.state, attr, action_id)
                    logger.info(f"ModXO action {attr}: {action_id}")
                    changed = True
            return changed

        scan(html or "")
        if self.state.show_create_account_action_id and self.state.create_user_action_id:
            return

        script_urls = []
        for src in re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html or "", re.I):
            if "/pay/_next/static/chunks/" not in src:
                continue
            url = urllib.parse.urljoin(base_url, src)
            if url not in script_urls:
                script_urls.append(url)

        for script_url in script_urls[:80]:
            try:
                js_resp = self.session.get(
                    script_url,
                    headers={
                        "Accept": "*/*",
                        "Referer": base_url,
                        "Sec-Fetch-Dest": "script",
                        "Sec-Fetch-Mode": "no-cors",
                        "Sec-Fetch-Site": "same-origin",
                    },
                )
                if js_resp.status_code == 200:
                    scan(js_resp.text)
                if self.state.show_create_account_action_id and self.state.create_user_action_id:
                    return
            except Exception as e:
                logger.debug(f"Failed to inspect ModXO chunk {script_url}: {e}")

    def _card_issuer_type(self) -> str:
        """PayPal GraphQL CardIssuerType enum."""
        prefix2 = int(self.card.number[:2]) if self.card.number[:2].isdigit() else 0
        prefix4 = int(self.card.number[:4]) if self.card.number[:4].isdigit() else 0
        if 51 <= prefix2 <= 55 or 2221 <= prefix4 <= 2720:
            return "MASTER_CARD"
        if self.card.number.startswith("4"):
            return "VISA"
        if self.card.number.startswith("3"):
            return "AMEX"
        if self.card.number.startswith("6"):
            return "DISCOVER"
        return "VISA"

    def _masked_card_number(self) -> str:
        return sanitize_for_log({"cardNumber": self.card.number})["cardNumber"]

    def _masked_phone(self) -> str:
        return sanitize_for_log({"phone": self.user.phone})["phone"]

    def _update_user_phone(self, phone: str):
        """Update the BR phone fields used by the signup/2FA GraphQL calls."""
        raw = (phone or "").strip()
        if raw.lower().startswith("phone:"):
            raw = raw.split(":", 1)[1].strip()

        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) < 8:
            raise ValueError("phone number is too short")

        # This flow is hard-coded for BR checkout. Accept either +55xxxxxxxxxx
        # or a local BR mobile number and normalize to the fields PayPal expects.
        if digits.startswith("55") and len(digits) > 10:
            country_code = "+55"
            local = digits[2:]
            full = f"+{digits}"
        else:
            country_code = "+55"
            local = digits
            full = f"+55{digits}"

        if len(local) < 8:
            raise ValueError("local phone number is too short")

        self.user.phone = full
        self.user.phone_country_code = country_code
        self.user.phone_local = local
        logger.info("Phone updated for OTP retry: {}", self._masked_phone())

    def _initiate_2fa_phone_confirmation(self, token: str, signup_url: str) -> tuple[str, str]:
        """Send a new 2FA SMS and return authId/challengeId."""
        logger.info("Step 1: Initiating 2FA phone confirmation for {}...", self._masked_phone())
        send_weasley_log(
            self.session,
            self.state.ec_token,
            signup_url,
            [
                "weasley_risk_based_phone_confirmation_modal_component_mounted",
                "weasley_initiate_phone_confirmation_start",
                "weasley_api_request_initiate_risk_based_two_factor_phone_confirmation_mutation",
            ],
            country=self.address.country,
            lang=self.state.lang or "pt",
        )
        initiate_result = self.session.graphql(
            "InitiateRiskBasedTwoFactorPhoneConfirmationMutation",
            INITIATE_2FA_PHONE_MUTATION,
            {
                "phoneNumber": self.user.phone_local,
                "locale": {"country": self.address.country, "lang": self.state.lang or "pt"},
                "phoneCountry": self.address.country,
                "token": token,
            },
        )
        logger.info(
            "2FA initiation result (sanitized): {}",
            json.dumps(sanitize_for_log(initiate_result), ensure_ascii=False, indent=2)[:500],
        )

        result_obj = initiate_result[0] if isinstance(initiate_result, list) else initiate_result
        tfa_data = result_obj.get("data", {}).get(
            "initiateRiskBasedTwoFactorPhoneConfirmation", {}
        )
        auth_id = tfa_data.get("authId", "")
        challenge_id = tfa_data.get("challengeId", "")
        state = tfa_data.get("state", "")
        logger.info("2FA state: {}, authId=<redacted>, challengeId=<redacted>", state)

        if not auth_id or not challenge_id:
            marker = json.dumps(sanitize_for_log(initiate_result), ensure_ascii=False)
            if self._is_session_challenge_error(marker):
                raise RuntimeError(
                    "2FA initiation challenged (authchallenge/non-JSON). "
                    "Reopen the task with a cleaner proxy/session."
                )
            raise RuntimeError("Failed to get authId/challengeId from 2FA initiation")
        return auth_id, challenge_id

    def _confirm_2fa_phone_confirmation(
        self,
        token: str,
        signup_url: str,
        auth_id: str,
        challenge_id: str,
        otp: str,
    ) -> bool:
        """Confirm one OTP attempt. Return True only on CONFIRMED."""
        logger.info("Step 2: Confirming OTP: <redacted>")
        send_weasley_log(
            self.session,
            self.state.ec_token,
            signup_url,
            [
                "weasley_confirm_phone_confirmation_start",
                "weasley_api_request_confirm_risk_based_two_factor_phone_confirmation_mutation",
            ],
            country=self.address.country,
            lang=self.state.lang or "pt",
        )
        confirm_result = self.session.graphql(
            "ConfirmRiskBasedTwoFactorPhoneConfirmationMutation",
            CONFIRM_2FA_PHONE_MUTATION,
            {
                "pin": otp,
                "authId": auth_id,
                "challengeId": challenge_id,
                "token": token,
            },
        )
        logger.info(
            "OTP confirmation result (sanitized): {}",
            json.dumps(sanitize_for_log(confirm_result), ensure_ascii=False, indent=2)[:500],
        )

        result_obj = confirm_result[0] if isinstance(confirm_result, list) else confirm_result
        confirm_data = result_obj.get("data", {}).get(
            "confirmRiskBasedTwoFactorPhoneConfirmation", {}
        ) or {}
        confirm_state = confirm_data.get("state", "")
        if confirm_state == "CONFIRMED":
            logger.success("OTP confirmed successfully!")
            return True

        errors = result_obj.get("errors") or []
        if errors:
            logger.warning(
                "OTP confirmation failed with errors: {}",
                json.dumps(sanitize_for_log(errors), ensure_ascii=False, indent=2),
            )
        else:
            logger.warning("OTP confirmation failed, state: {}", confirm_state or "<missing>")
        return False


    def _fallback_known_address(self) -> None:
        """Replace unusable normalized address with a known good local address."""
        if (self.address.country or "BR").upper() != "BR":
            logger.warning(
                "No known-address fallback for country {}; keeping original address",
                self.address.country,
            )
            return
        fb = generate_address("BR")
        self.address.street = fb.street
        self.address.house_number = fb.house_number
        self.address.district = fb.district
        self.address.city = fb.city
        self.address.state = fb.state
        self.address.postal_code = fb.postal_code
        logger.info(
            "Address fallback applied: {}, {}, {}-{}",
            self.address.street,
            self.address.district,
            self.address.city,
            self.address.state,
        )

    def _is_session_challenge_error(self, err: Exception | str) -> bool:
        text = str(err or "")
        lowered = text.lower()
        markers = (
            "authchallengenodeweb",
            "auth challenge",
            "expecting value: line 1 column 1",
            "non-json",
            "non_json",
            "challenged",
        )
        return any(m in lowered for m in markers)

    def _confirm_phone_with_retry(self, token: str, signup_url: str):
        """Loop until OTP is confirmed; allow configured phone changes on send/confirm failure."""
        phone_changes = 0
        max_phone_changes = getattr(self, "max_phone_changes", 5)
        browser_assist_phones: set[str] = set()
        prefer_browser_initiate = False

        def _assist_url() -> str:
            # Prefer the flow-owned signup URL so US/BA do not inherit CN hijacks.
            preferred = ""
            try:
                preferred = self._build_signup_url()
            except Exception:
                preferred = ""
            candidate = preferred or signup_url or self.state.signup_url or (
                f"https://www.paypal.com/checkoutweb/signup?token={token}&ul=1"
                f"&locale.x={self.state.locale or 'pt_BR'}&country.x={self.address.country}"
            )
            flow_country = (getattr(self, "country", None) or self.state.country or "").upper()
            if flow_country and f"country.x={flow_country}" not in candidate and preferred:
                return preferred
            return candidate

        def _initiate_with_optional_browser() -> tuple[str, str]:
            nonlocal prefer_browser_initiate
            phone_key = (self.user.phone_local or "").strip()
            if prefer_browser_initiate and phone_key not in browser_assist_phones:
                browser_assist_phones.add(phone_key)
                prefer_browser_initiate = False
                logger.warning(
                    "Preferring headed browser OTP initiate for phone {} after prior challenge/phone change",
                    self._masked_phone(),
                )
                assist = self._run_headed_browser_assist(
                    _assist_url(),
                    purpose="otp_authchallenge",
                    otp_phone_local=self.user.phone_local,
                    otp_token=token,
                )
                if assist and getattr(assist, "otp_auth_id", "") and getattr(assist, "otp_challenge_id", ""):
                    logger.success(
                        "OTP initiate via browser page context state={}",
                        getattr(assist, "otp_state", "") or "?",
                    )
                    return assist.otp_auth_id, assist.otp_challenge_id

            try:
                return self._initiate_2fa_phone_confirmation(token, signup_url)
            except Exception as e:
                logger.error("Failed to initiate OTP for {}: {}", self._masked_phone(), e)
                if self._is_session_challenge_error(e) and phone_key not in browser_assist_phones:
                    browser_assist_phones.add(phone_key)
                    assist = self._run_headed_browser_assist(
                        _assist_url(),
                        purpose="otp_authchallenge",
                        otp_phone_local=self.user.phone_local,
                        otp_token=token,
                    )
                    if not assist:
                        raise RuntimeError(
                            "Session challenged during OTP send (authchallenge/non-JSON). "
                            "Headed browser assist did not clear it. Reopen the task with a "
                            "cleaner residential sticky session."
                        ) from e
                    if getattr(assist, "otp_auth_id", "") and getattr(assist, "otp_challenge_id", ""):
                        logger.success(
                            "OTP initiate recovered via browser page context state={}",
                            getattr(assist, "otp_state", "") or "?",
                        )
                        return assist.otp_auth_id, assist.otp_challenge_id
                    try:
                        ids = self._initiate_2fa_phone_confirmation(token, signup_url)
                        logger.success("OTP initiate recovered after headed browser cookie import")
                        return ids
                    except Exception as e2:
                        raise RuntimeError(
                            "Session still challenged after headed browser assist "
                            f"({e2}). Reopen with a cleaner residential sticky session."
                        ) from e2
                if self._is_session_challenge_error(e):
                    # Bubble up so outer loop can offer another phone change if quota remains.
                    raise
                raise

        while True:
            try:
                auth_id, challenge_id = _initiate_with_optional_browser()
            except Exception as e:
                remaining = max_phone_changes - phone_changes
                if phone_changes >= max_phone_changes:
                    raise RuntimeError(
                        "OTP send failed and phone-change limit reached "
                        f"({max_phone_changes}). Reopen the task with a cleaner session."
                    ) from e
                while True:
                    value = input(
                        f"\n>>> 发送验证码失败。还可再换 {remaining} 次手机号；"
                        "若继续失败请输入 q 并重开任务（如 +5591980133818）: "
                    ).strip()
                    if value.lower() in {"q", "quit", "exit"}:
                        raise RuntimeError("OTP confirmation cancelled by user") from e
                    try:
                        self._update_user_phone(value)
                        phone_changes += 1
                        prefer_browser_initiate = True
                        break
                    except ValueError as phone_error:
                        logger.warning("手机号无效：{}。请重新输入。", phone_error)
                continue

            logger.info("SMS verification code sent to phone: {}", self._masked_phone())

            while True:
                value = input(
                    "\n>>> 输入当前手机收到的6位短信验证码；"
                    "只有要换号时才输入完整手机号（如 +5591980133818）；输入 q 退出: "
                ).strip()
                compact = re.sub(r"[\s-]+", "", value or "")

                if value.lower() in {"q", "quit", "exit"}:
                    raise RuntimeError("OTP confirmation cancelled by user")

                if len(compact) == 6 and compact.isdigit():
                    if self._confirm_2fa_phone_confirmation(
                        token,
                        signup_url,
                        auth_id,
                        challenge_id,
                        compact,
                    ):
                        return
                    logger.warning(
                        "验证码验证失败。请继续输入6位验证码；"
                        "不要把手机号填进验证码框。若必须换号，输入完整国际格式手机号。"
                    )
                    continue

                if phone_changes >= max_phone_changes:
                    logger.warning(
                        "换号次数已达上限（{}）。当前应输入6位验证码（不是手机号），或输入 q 退出后重开任务。",
                        max_phone_changes,
                    )
                    continue
                try:
                    self._update_user_phone(value)
                    phone_changes += 1
                    prefer_browser_initiate = True
                    logger.warning(
                        "已切换手机号，将优先用有头浏览器为新号重新发起 OTP（避免纯 HTTP authchallenge）..."
                    )
                    break
                except ValueError as e:
                    logger.warning(
                        "输入既不是6位验证码，也不是有效手机号：{}。请重新输入。",
                        e,
                    )


    def _card_expiration_date(self) -> str:
        exp_parts = self.card.expiry.split("/")
        return f"{exp_parts[0]}/{exp_parts[1]}" if len(exp_parts) == 2 else self.card.expiry

    def _dob_payload(self) -> dict:
        dob_parts = self.user.dob.split("/")
        return (
            {"day": dob_parts[0], "month": dob_parts[1], "year": dob_parts[2]}
            if len(dob_parts) == 3
            else {}
        )

    def _build_signup_variables(self, token: str) -> dict:
        card_type = self._card_issuer_type()
        return {
            "card": {
                "cardNumber": self.card.number,
                "expirationDate": self._card_expiration_date(),
                "securityCode": self.card.cvv,
                "type": card_type,
                "productClass": self.card.card_type,
            },
            "country": self.address.country,
            "email": self.user.email,
            "firstName": self.user.first_name,
            "lastName": self.user.last_name,
            "phone": {
                "countryCode": self.user.phone_country_code.lstrip("+"),
                "number": self.user.phone_local,
                "type": "MOBILE",
            },
            "supportedThreeDsExperiences": ["IFRAME"],
            "token": token,
            "billingAddress": {
                "postalCode": self.address.postal_code,
                "line1": f"{self.address.street}, {self.address.house_number}",
                "line2": self.address.district,
                "city": self.address.city,
                "state": self.address.state,
                "accountQuality": {
                    "autoCompleteType": "ANS",
                    "isUserModified": True,
                },
                "country": self.address.country,
                "familyName": self.user.last_name,
                "givenName": self.user.first_name,
            },
            "shippingAddress": {
                "postalCode": "",
                "line1": "",
                "city": "",
                "state": "",
                "accountQuality": {
                    "autoCompleteType": "MANUAL",
                    "isUserModified": False,
                },
                "country": self.address.country,
                "familyName": self.user.last_name,
                "givenName": self.user.first_name,
            },
            "contentIdentifier": self.state.content_identifier or (
                f"{self.address.country}:{self.state.lang or 'pt'}:"
                f"{self.state.content_hash or '759169e5b7de230616d673bd3498ac79'}:"
                "compliance.signupTerms"
            ),
            "marketingOptOut": False,
            "password": self.user.password,
            "dateOfBirth": self._dob_payload(),
            "identityDocument": {
                "type": "CPF",
                "value": self.user.cpf,
            },
            "crsData": None,
            "legalAgreements": {},
        }

    def _send_signup_attempt(
        self,
        token: str,
        signup_url: str,
        *,
        allow_browser_assist: bool = True,
    ) -> dict:
        card_type = self._card_issuer_type()
        try:
            self.session.graphql(
                "InstallmentOptionsQuery",
                INSTALLMENT_OPTIONS_QUERY,
                {
                    "buyerCountry": self.address.country,
                    "cardNumber": self.card.number,
                    "cardType": card_type,
                    "token": token,
                },
            )
        except Exception as e:
            logger.warning(f"InstallmentOptionsQuery failed: {e}")

        send_signup_field_events(
            self.session,
            token,
            [
                "email",
                "phone",
                "cardNumber",
                "cardExpiry",
                "cardCvv",
                "password",
                "firstName",
                "lastName",
                "billingLine1",
                "billingCity",
                "billingPostalCode",
                "billingState",
                "dateOfBirth",
                "identityDocumentNumber",
            ],
        )
        send_weasley_log(
            self.session,
            self.state.ec_token,
            signup_url,
            [
                "weasley_create_account_and_pay_submit",
                "weasley_api_request_sign_up_new_member_mutation",
            ],
            country=self.address.country,
            lang=self.state.lang or "pt",
        )
        # Re-send OTP challenge right before signup (BA HAR shows multiple calls)
        if self.state.ec_token:
            send_otp_challenge(
                self.session,
                self.state.ec_token,
                self.user.email,
                ctx_id=self.state.ctx_id,
                csrf_nonce=self.state.csrf_nonce,
            )

        variables = self._build_signup_variables(token)
        fn_sync = build_signup_fn_sync_data(token)
        try:
            signup_result = self.session.graphql(
                "SignUpNewMemberMutation",
                SIGNUP_NEW_MEMBER_MUTATION,
                variables,
                extra_body={"fn_sync_data": fn_sync},
            )
        except ValueError:
            # Non-JSON response and no EUAT token extracted.
            # Return a synthetic error so _signup_with_card_retry can decide.
            logger.warning("SignUpNewMember returned non-JSON and no token found; returning synthetic error")
            signup_result = {"data": {}, "errors": [{"message": "NON_JSON_RESPONSE", "errorData": {}}]}

        # If pure HTTP hit authchallenge HTML, replay SignUp inside headed browser once.
        result_obj = signup_result[0] if isinstance(signup_result, list) else signup_result
        errors = (result_obj or {}).get("errors") or []
        onboard = ((result_obj or {}).get("data") or {}).get("onboardAccount")
        if (
            allow_browser_assist
            and not onboard
            and self._is_signup_challenge_error(errors)
        ):
            assist_url = signup_url or self.state.signup_url or (
                f"https://www.paypal.com/checkoutweb/signup?token={token}&ul=1"
                f"&locale.x={self.state.locale or 'pt_BR'}&country.x={self.address.country}"
            )
            logger.warning(
                "SignUpNewMember challenged (authchallenge/non-JSON). "
                "Launching headed browser assist to submit from page context..."
            )
            assist = self._run_headed_browser_assist(
                assist_url,
                purpose="signup_authchallenge",
                signup_variables=variables,
                signup_token=token,
                signup_fn_sync_data=fn_sync,
            )
            browser_result = getattr(assist, "signup_result", None) if assist else None
            if browser_result:
                signup_result = browser_result
                logger.info("Using SignUpNewMember result from headed browser page context")
            elif assist:
                # Cookies imported; retry pure HTTP once without nesting another browser.
                logger.warning(
                    "Browser assist cleared page but no signup payload returned; "
                    "retrying SignUpNewMember over HTTP once"
                )
                try:
                    signup_result = self.session.graphql(
                        "SignUpNewMemberMutation",
                        SIGNUP_NEW_MEMBER_MUTATION,
                        variables,
                        extra_body={"fn_sync_data": build_signup_fn_sync_data(token)},
                    )
                except ValueError:
                    logger.warning(
                        "HTTP SignUpNewMember still non-JSON after browser assist"
                    )
                    signup_result = {
                        "data": {},
                        "errors": [{"message": "NON_JSON_RESPONSE", "errorData": {"after": "browser_assist"}}],
                    }
            else:
                logger.error("Headed browser assist did not clear SignUp challenge")

        logger.info(
            "Signup result (sanitized): {}",
            json.dumps(
                sanitize_for_log(signup_result),
                ensure_ascii=False,
                indent=2,
            )[:4000],
        )
        return signup_result

    def _consume_signup_result(self, signup_result) -> tuple[bool, list[dict]]:
        """Apply successful signup data to state. Return (success, errors)."""
        result_obj = signup_result[0] if isinstance(signup_result, list) else signup_result
        onboard_data = result_obj.get("data", {}).get("onboardAccount", {})
        if onboard_data:
            buyer = onboard_data.get("buyer", {})
            self.state.user_id = buyer.get("userId", "")
            auth = buyer.get("auth", {})
            if auth:
                self.state.euat_token = auth.get("accessToken", "")
            logger.success(f"Account created! User ID: {self.state.user_id}")
            return True, []

        errors = result_obj.get("errors", []) or []
        if errors:
            for err in errors:
                logger.error(
                    "Signup error detail: {}",
                    json.dumps(
                        sanitize_for_log({
                            "message": err.get("message"),
                            "name": err.get("_name"),
                            "statusCode": err.get("statusCode"),
                            "checkpoints": err.get("checkpoints"),
                            "contingency": err.get("contingency"),
                            "path": err.get("path"),
                            "data": err.get("data"),
                            "errorData": err.get("errorData"),
                            "meta": err.get("meta"),
                            "extensions": err.get("extensions"),
                        }),
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
        logger.error(
            "Signup failed because onboardAccount is empty. Sanitized response: {}",
            json.dumps(
                sanitize_for_log(result_obj),
                ensure_ascii=False,
                indent=2,
            )[:8000],
        )
        return False, errors

    @staticmethod
    def _dict_contains_card_field(value) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                compact_key = str(key).lower().replace("_", "").replace("-", "")
                if compact_key in {"cardnumber", "card", "cardnumberfield"}:
                    return True
                if isinstance(item, str):
                    item_lower = item.lower()
                    if item_lower in {"cardnumber", "card_generic_error"}:
                        return True
                if PayPalFlow._dict_contains_card_field(item):
                    return True
        elif isinstance(value, list):
            return any(PayPalFlow._dict_contains_card_field(item) for item in value)
        return False

    @staticmethod
    def _is_create_member_account_error(errors: list[dict]) -> bool:
        """True when PayPal rejected member creation itself (not card add)."""
        for err in errors or []:
            message = str(err.get("message") or "").upper()
            name = str(err.get("_name") or err.get("name") or "").upper()
            checkpoints = {str(x) for x in (err.get("checkpoints") or [])}
            if "createMemberAccount" in checkpoints:
                return True
            if message == "OAS_ERROR" or name == "OAS_ERROR":
                # OAS_ERROR without card checkpoints is treated as create-member failure.
                if not checkpoints.intersection({"addCard", "validate.fi", "card", "fi"}):
                    return True
        return False

    @staticmethod
    def _is_anonymous_auth_error(result) -> bool:
        items = result if isinstance(result, list) else [result]
        for item in items:
            if not isinstance(item, dict):
                continue
            for err in item.get("errors") or []:
                msg = str(err.get("message") or "")
                low = msg.lower()
                if "auth state is: anonymous" in low:
                    return True
                if "requires an auth state" in low and "anonymous" in low:
                    return True
                if "loggedin" in low and "remembered" in low and "anonymous" in low:
                    return True
        return False

    @staticmethod
    def _is_card_related_signup_error(errors: list[dict]) -> bool:
        """True only for real card/FI rejections, not authchallenge shells."""
        card_messages = {
            "CARD_GENERIC_ERROR",
            "INSTRUMENT_SHARING_LIMIT_EXCEEDED",
            "CC_LINKED_TO_FULL_ACCOUNT",
            "CREATE_CARD_ACCOUNT_CANDIDATE_VALIDATION_ERROR",
        }
        for err in errors or []:
            message = str(err.get("message") or "")
            # Challenge / non-JSON pages must not burn card retries.
            if message in {
                "NON_JSON_RESPONSE",
                "BROWSER_SIGNUP_EXCEPTION",
                "EMPTY_RESPONSE",
                "INVALID_RESPONSE",
            }:
                continue
            checkpoints = set(err.get("checkpoints") or [])
            if checkpoints.intersection({"addCard", "validate.fi", "card", "fi"}):
                return True
            if message in card_messages:
                return True
            if PayPalFlow._dict_contains_card_field(err.get("errorData")):
                return True
        return False

    @staticmethod
    def _is_signup_challenge_error(errors: list[dict]) -> bool:
        """True when SignUp hit authchallenge / non-JSON shell instead of card FI."""
        for err in errors or []:
            message = str(err.get("message") or "")
            if message in {"NON_JSON_RESPONSE", "BROWSER_SIGNUP_EXCEPTION"}:
                return True
            error_data = err.get("errorData") or {}
            browser = str(error_data.get("browser") or "").lower()
            if "authchallenge" in browser or "non_json" in browser:
                return True
            blob = f"{message} {error_data}".lower()
            if "authchallengenodeweb" in blob or "auth challenge" in blob:
                return True
        return False

    @staticmethod
    def _has_signup_error_message(errors: list[dict], message: str) -> bool:
        return any(str(err.get("message") or "") == message for err in errors or [])

    def _signup_with_card_retry(self, token: str, signup_url: str):
        """Retry SignUpNewMember with a fresh generated Visa/MasterCard on card errors."""
        self.state.euat_token = ""
        last_errors: list[dict] = []
        last_access_token = ""
        browser_challenge_assist_used = False

        for attempt in range(1, self.max_card_attempts + 1):
            logger.info(
                "Step 3: Creating account (SignUpNewMember), card attempt {}/{}: {}",
                attempt,
                self.max_card_attempts,
                self._masked_card_number(),
            )
            # Browser assist is allowed only once for challenge shells.
            signup_result = self._send_signup_attempt(
                token,
                signup_url,
                allow_browser_assist=not browser_challenge_assist_used,
            )
            success, errors = self._consume_signup_result(signup_result)
            if success:
                return

            last_errors = errors
            access_token = self._find_access_token(errors) or self._find_access_token(signup_result)
            if access_token:
                last_access_token = access_token

            if self._has_signup_error_message(errors, "ACCOUNT_ALREADY_EXISTS"):
                if last_access_token:
                    self.state.euat_token = last_access_token
                    logger.warning(
                        "Signup returned ACCOUNT_ALREADY_EXISTS after a previous "
                        "response already issued an access token. Reusing that "
                        "token and continuing instead of re-submitting signup."
                    )
                    return
                raise RuntimeError(
                    "Signup failed: ACCOUNT_ALREADY_EXISTS and no prior access "
                    "token is available for this session."
                )

            # Challenge shells: do not burn card retries.
            if self._is_signup_challenge_error(errors):
                browser_challenge_assist_used = True
                if access_token:
                    self.state.euat_token = access_token
                    logger.warning(
                        "SignUp challenge path still returned access token. "
                        "Continuing to Phase 4 without swapping cards."
                    )
                    return
                if attempt >= self.max_card_attempts:
                    if self.state.euat_token:
                        logger.warning(
                            "SignUp still challenged after assist/retry, but "
                            "EUAT/accessToken is available. Continuing to Phase 4."
                        )
                        return
                    raise RuntimeError(
                        "Signup failed: SignUpNewMember kept returning "
                        "authchallenge/non-JSON after headed browser assist, "
                        "and no accessToken/EUAT was obtained."
                    )
                logger.warning(
                    "SignUp challenged (authchallenge/non-JSON). "
                    "Not treating as card failure; will not regenerate card."
                )
                # If assist already ran inside _send_signup_attempt, further
                # attempts only use pure HTTP (allow_browser_assist=False).
                continue

            if self._is_card_related_signup_error(errors):
                if access_token:
                    self.state.euat_token = access_token
                    self.state.signup_contingency_reason = self._signup_card_reason_code(errors)
                    logger.warning(
                        "Card/addCard failed but PayPal returned an access token "
                        "(reason={}). The member account is already created at this "
                        "point, so re-sending SignUpNewMember with a new card would "
                        "produce ACCOUNT_ALREADY_EXISTS. Continuing with the returned token.",
                        self.state.signup_contingency_reason or "CARD_GENERIC_ERROR",
                    )
                    return

                if attempt >= self.max_card_attempts:
                    # Card was rejected after all retries, but the account
                    # may still have been created.  Check if we already have
                    # an EUAT token (from session.py non-JSON extraction or
                    # from a previous error response).  If so, skip the card
                    # verification and continue to Phase 4 billing.
                    if self.state.euat_token:
                        logger.warning(
                            "Card was rejected after {} attempts, but an EUAT "
                            "token was obtained. Skipping card verification "
                            "and continuing to Phase 4 billing.",
                            self.max_card_attempts,
                        )
                        return
                    raise RuntimeError(
                        "Signup failed: card was rejected after "
                        f"{self.max_card_attempts} attempts"
                    )

                logger.warning(
                    "Card rejected by signup/addCard. Fetching a fresh random "
                    "Visa/MasterCard from suijidaquan and retrying..."
                )
                self.card = generate_card(proxy_url=self.proxy_config.url)
                logger.info(
                    "New generated card for retry: {} exp={}",
                    self._masked_card_number(),
                    self.card.expiry,
                )
                continue

            if access_token:
                self.state.euat_token = access_token
                logger.info("Got access token from signup error response")
                return

            # Member creation rejected with no token: fail closed, do not burn Phase4.
            if self._is_create_member_account_error(errors):
                raise RuntimeError(
                    "Signup failed: createMemberAccount/OAS_ERROR without accessToken. "
                    "Member account was not created; Phase4 authorize would be ANONYMOUS. "
                    "Reopen with cleaner sticky proxy + fresh email/phone/profile. "
                    f"Last errors: {json.dumps(sanitize_for_log(errors), ensure_ascii=False)[:1000]}"
                )

            break

        # Only continue to Phase4 when we actually have a buyer auth token.
        if self.state.euat_token:
            logger.warning(
                "Signup retries exhausted, but an access token is available. "
                "Skipping card verification and continuing to Phase 4 billing."
            )
            return

        if self._is_create_member_account_error(last_errors):
            raise RuntimeError(
                "Signup failed: createMemberAccount/OAS_ERROR without accessToken. "
                "Refusing to continue with billingAgreementId-only anonymous session. "
                f"Last errors: {json.dumps(sanitize_for_log(last_errors), ensure_ascii=False)[:1000]}"
            )

        raise RuntimeError(
            "Signup failed: no accessToken/EUAT after OTP SignUpNewMember. "
            "Refusing Phase4 on anonymous session. "
            f"Last errors: {json.dumps(sanitize_for_log(last_errors), ensure_ascii=False)[:1000]}"
        )

    def _follow_modxo_action_redirect(self, resp, referer: str):
        """Follow Next server-action redirects emitted by ModXO.

        PayPal's server action may return a normal Location header or an
        x-action-redirect header such as "/?...;push". In the latter case the
        path is relative to the /pay app, not the site root.
        """
        redirect_url = resp.headers.get("Location") or resp.headers.get("x-action-redirect") or ""
        if not redirect_url:
            return resp
        redirect_url = redirect_url.split(";", 1)[0]
        if redirect_url.startswith("/?"):
            redirect_url = f"https://www.paypal.com/pay{redirect_url}"
        elif redirect_url.startswith("/"):
            redirect_url = f"https://www.paypal.com{redirect_url}"
        logger.info(f"Following ModXO action redirect: {redirect_url[:140]}...")
        return self.session.get(
            redirect_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": referer,
                "Upgrade-Insecure-Requests": "1",
            },
        )

    def _phase1_risk_controls(self):
        """Send device fingerprints, Tealeaf data, analytics."""
        logger.info("--- Phase 1: Risk control signals ---")

        # Device fingerprint (p1, p2, w endpoints)
        send_device_fingerprint(self.session, self.ba_token)

        # Tealeaf initial data
        page_url = f"https://www.paypal.com/pay?ssrt={self.state.ssrt}&token={self.ba_token}&ul=1"
        send_tealeaf_data(self.session, page_url)

        # Analytics
        send_analytics_ts(self.session, "main:xo:modxo:login", self.ba_token)
        send_observability_emit(self.session, self.ba_token)

        logger.info("Risk control signals sent")

    def _phase2_create_account(self):
        """Submit 'Create Account' action to get to the signup page."""
        logger.info("--- Phase 2: Create account flow ---")

        resp = None
        if self.direct_signup_from_initial_ec and self.state.ec_token:
            signup_url = self._build_signup_url()
            logger.info("Using EC token from initial approval context; loading signup directly...")
            resp = self.session.get(
                signup_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": f"https://www.paypal.com/agreements/approve?ba_token={self.ba_token}",
                    "Upgrade-Insecure-Requests": "1",
                },
            )
        # Browser trace (2026-07-04): ModXO is a Next server-action flow.
        # First click "Pay with Card", then submit an email/createAccount
        # action, whose RSC payload returns onboardingRedirectUrl.
        pay_url = (
            f"https://www.paypal.com/pay/?ssrt={self.state.ssrt}"
            f"&token={self.ba_token}&ul=1&ctxId={self.state.ctx_id}"
            f"&country.x={self.address.country}"
        )
        # When Phase0 already produced an EC token but the approve HTML did not
        # expose ModXO Next-Action ids (common on smaller recovered pages), go
        # straight to checkoutweb/signup. The compact form fallback often lands
        # on checkoutweb/genericError and poisons OTP with authchallenge.
        if (
            resp is None
            and self.state.ec_token
            and (
                not self.state.show_create_account_action_id
                or not self.state.create_user_action_id
            )
        ):
            signup_url = self._build_signup_url()
            logger.warning(
                "ModXO Next-Action ids missing after Phase0; EC token already present. "
                "Loading checkoutweb/signup directly to avoid genericError fallback."
            )
            resp = self.session.get(
                signup_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": f"https://www.paypal.com/agreements/approve?ba_token={self.ba_token}",
                    "Upgrade-Insecure-Requests": "1",
                },
            )

        # If still no ModXO ids and no direct signup response, probe /pay once
        # to harvest action ids before the compact-form fallback.
        if (
            resp is None
            and (
                not self.state.show_create_account_action_id
                or not self.state.create_user_action_id
            )
        ):
            try:
                logger.info("Probing /pay page to harvest ModXO Next-Action ids...")
                pay_probe = self.session.get(
                    pay_url,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": f"https://www.paypal.com/agreements/approve?ba_token={self.ba_token}",
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
                self._extract_modxo_action_ids(pay_probe.text, str(pay_probe.url))
            except Exception as e:
                logger.warning("ModXO /pay probe failed: {}", e)

        if resp is None:
            try:
                if not self.state.show_create_account_action_id or not self.state.create_user_action_id:
                    raise RuntimeError("missing dynamic ModXO Next-Action ids")
    
                logger.info("Submitting browser-like Pay_With_Card server action...")
                pay_with_card_url = f"{pay_url}&paypal_client_cfci=modxo_vaulted_not_recurring-Pay_With_Card"
                pay_resp = self.session.post(
                    pay_with_card_url,
                    files=[
                        ("_1_ctxId", (None, self.state.ctx_id)),
                        ("_1_formName", (None, "createAccountAction")),
                        ("0", (None, '["$K1"]')),
                    ],
                    headers={
                        "Accept": "text/x-component",
                        "Origin": "https://www.paypal.com",
                        "Referer": pay_url,
                        "Next-Action": self.state.show_create_account_action_id,
                    },
                )
                if pay_resp.status_code in (301, 302, 303, 307, 308) or pay_resp.headers.get("x-action-redirect"):
                    self._follow_modxo_action_redirect(pay_resp, pay_url)
    
                logger.info("Submitting browser-like Continue_To_Payment server action...")
                continue_url = f"{pay_url}&paypal_client_cfci=modxo_vaulted_not_recurring-Continue_To_Payment"
                rsc_resp = self.session.post(
                    continue_url,
                    files=[
                        ("_1_ctxId", (None, self.state.ctx_id)),
                        ("_1_token", (None, self.ba_token)),
                        ("_1_login_email", (None, self.user.email)),
                        ("_1_formName", (None, "createAccount")),
                        ("0", (None, f'["$K1",{{"emailSubmitTime":{int(time.time() * 1000)}}}]')),
                    ],
                    headers={
                        "Accept": "text/x-component",
                        "Origin": "https://www.paypal.com",
                        "Referer": pay_with_card_url,
                        "Next-Action": self.state.create_user_action_id,
                    },
                )
                onboarding_url = self._extract_onboarding_redirect(rsc_resp.text)
                if onboarding_url:
                    logger.info(f"Onboarding redirect URL: {onboarding_url[:140]}...")
                    resp = self.session.get(
                        onboarding_url,
                        headers={
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "Referer": pay_url,
                            "Upgrade-Insecure-Requests": "1",
                        },
                    )
            except Exception as e:
                logger.warning(f"Browser-like ModXO server-action path failed: {e}")
        if resp is None:
            # Fallback for older deployments that still accept a compact form.
            base_url = (
                f"https://www.paypal.com/pay?ssrt={self.state.ssrt}"
                f"&token={self.ba_token}&ul=1"
                f"&paypal_client_cfci=modxo_vaulted_not_recurring-Pay_With_Card"
            )

            form_data = {
                "ctxId": self.state.ctx_id,
                "formName": "createAccountAction",
                "fn_sync_data": build_fn_sync_data(self.ba_token),
            }

            resp = self.session.post(base_url, data=form_data, headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://www.paypal.com",
                "Referer": f"https://www.paypal.com/pay?ssrt={self.state.ssrt}&token={self.ba_token}&ul=1",
            })

        # Handle redirect chain
        while resp.status_code in (302, 303):
            redirect_url = resp.headers.get("Location", "")
            if redirect_url.startswith("/"):
                redirect_url = f"https://www.paypal.com{redirect_url}"
            logger.info(f"Following redirect: {redirect_url[:100]}...")
            if "genericError" in redirect_url:
                logger.warning(
                    "Phase2 redirect touched checkoutweb/genericError; session quality is degraded. "
                    "Will continue only if signup page can still be loaded."
                )
            resp = self.session.get(redirect_url)

        if "genericError" in str(resp.url):
            logger.warning(
                "Phase2 currently on checkoutweb/genericError; attempting signup fallback load."
            )

        html = resp.text

        # Extract EC token from the new URL or page content
        ec_match = re.search(r"token=(EC-\w+)", str(resp.url))
        if ec_match:
            self.state.ec_token = ec_match.group(1)
            logger.info("EC Token: {}", sanitize_for_log({"ec_token": self.state.ec_token})["ec_token"])
        else:
            ec_match = re.search(r"EC-\w+", html)
            if ec_match:
                self.state.ec_token = ec_match.group(0)
                logger.info("EC Token (from HTML): {}", sanitize_for_log({"ec_token": self.state.ec_token})["ec_token"])

        # The real browser next loads checkoutweb/weasley.  This request is not
        # just cosmetic: it sets checkout cookies (for example l7_az/x-pp-s),
        # exposes the current content hash, and matches the Referer/context
        # expected by the following GraphQL mutations.
        if self.state.ec_token:
            signup_url = self._build_signup_url()
            self.state.signup_url = signup_url
            if "/checkoutweb/signup" in str(resp.url):
                signup_resp = resp
                logger.info("Checkout signup app already loaded from initial EC context")
            else:
                logger.info(f"Loading checkout signup app: {signup_url}")
                signup_resp = self.session.get(
                    signup_url,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": str(resp.url),
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
            logger.info(
                "Checkout signup app loaded: {} bytes={}",
                signup_resp.status_code,
                len(signup_resp.content),
            )
            if signup_resp.status_code in (301, 302, 303, 307, 308):
                redirect_url = signup_resp.headers.get("Location", "")
                if redirect_url:
                    redirect_url = urllib.parse.urljoin(signup_url, redirect_url)
                    # Never adopt a locale/country hijack (e.g. country.x=CN) as the
                    # durable signup referer for US/BR/BA flows.
                    flow_country = (getattr(self, "country", None) or self.state.country or "").upper()
                    flow_locale = getattr(self, "locale", None) or self.state.locale or ""
                    redirect_country = ""
                    m = re.search(r"[?&]country\.x=([^&]+)", redirect_url)
                    if m:
                        redirect_country = urllib.parse.unquote(m.group(1)).upper()
                    if "/checkoutweb/signup" in redirect_url and (
                        not flow_country
                        or not redirect_country
                        or redirect_country == flow_country
                    ):
                        self.state.signup_url = redirect_url
                    else:
                        # Rebuild a clean flow-country signup URL and keep using it.
                        self.state.signup_url = self._build_signup_url()
                        logger.warning(
                            "Ignoring signup redirect country hijack {} -> keeping flow country {}",
                            redirect_country or "?",
                            flow_country or "?",
                        )
                    logger.warning(
                        "Checkout signup app redirected to {}; preserving signup referer {}",
                        redirect_url[:140],
                        self.state.signup_url[:140],
                    )
            initial_data = self._extract_window_initial_data(signup_resp.text)
            content_hash = initial_data.get("contentHash")
            if content_hash:
                self.state.content_hash = content_hash
                logger.info(f"Content hash: {self.state.content_hash}")
            content_identifier = self._extract_content_identifier(
                signup_resp.text,
                self.address.country,
                self.state.lang,
            )
            if content_hash and content_identifier.endswith(":compliance.signupTerms") and content_hash not in content_identifier:
                content_identifier = f"{self.address.country}:{self.state.lang}:{content_hash}:compliance.signupTerms"
            elif content_identifier == f"{self.address.country}:{self.state.lang}:compliance.signupTerms" and self.address.country == "BR":
                # The short value is accepted by GraphQL but is more likely to
                # be rejected later by OAS. Keep the known BR content hash as a
                # better fallback when PayPal does not expose a fresh one.
                content_identifier = (
                    f"{self.address.country}:{self.state.lang}:"
                    "759169e5b7de230616d673bd3498ac79:"
                    "compliance.signupTerms"
                )
            self.state.content_identifier = content_identifier
            logger.info(f"Content identifier: {self.state.content_identifier}")

            # Extract csrfNonce from signup page HTML if available
            if not self.state.csrf_nonce:
                csrf_match = re.search(r'"csrfNonce"\s*[:=]\s*"([^"]+)"', signup_resp.text)
                if csrf_match:
                    self.state.csrf_nonce = csrf_match.group(1)
                    logger.info("csrfNonce extracted from signup page")

        if self.require_ec_signup_context:
            missing = []
            if not self.state.ec_token:
                missing.append("EC token")
            if not self.state.signup_url:
                missing.append("signup URL")
            if not self.state.content_identifier:
                missing.append("content identifier")
            if missing:
                raise RuntimeError("Signup context incomplete: " + ", ".join(missing))

        # Send Tealeaf for new page
        send_tealeaf_data(
            self.session,
            self.state.signup_url if self.state.signup_url else str(resp.url),
        )
        send_observability_emit(self.session, self.ba_token)

        if self.state.ec_token:
            # Browser trace sends signup-page Weasley logs and EC-token risk
            # beacons before phone/card submission.  Missing these correlates
            # with opaque OAS_ERROR/createMemberAccount buckets.
            send_weasley_log(
                self.session,
                self.state.ec_token,
                self.state.signup_url,
                [
                    "weasley_client_eligibility_check_success",
                    "WEASLEY_PAGE_INTERACTIVE_FPTI",
                    "WEASLEY_PREPARE_BILLING_PAGE_FPTI",
                    "weasley_payment_request_api_available",
                ],
                country=self.address.country,
                lang=self.state.lang,
            )
            send_device_fingerprint(
                self.session,
                self.state.ec_token,
                app_id="CHECKOUTUINODEWEB_ONBOARDING_LITE",
                referer=self.state.signup_url,
                wrapped=True,
            )

        # Send the initial GraphQL queries
        logger.info("Sending checkout session GraphQL queries...")
        try:
            self.session.graphql(
                "DeferredFeature",
                DEFERRED_FEATURE_QUERY,
                {
                    "channel": self.checkout_channel,
                    "countryCodeAsString": self.address.country,
                    "integrationType": "XoSignupAuth",
                    "isBaslAsString": "false",
                    "isForcedGuest": "false",
                    "token": self.state.ec_token or self.ba_token,
                },
            )
        except Exception as e:
            logger.warning(f"DeferredFeature failed: {e}")

        try:
            self.session.graphql(
                "CheckoutSessionDataQuery",
                CHECKOUT_SESSION_DATA_QUERY,
                {"token": self.state.ec_token or self.ba_token},
            )
        except Exception as e:
            logger.warning(f"CheckoutSessionDataQuery failed: {e}")

        try:
            self.session.graphql(
                "GriffinMetadataQuery",
                GRIFFIN_METADATA_QUERY,
                {
                    "countryCode": self.address.country,
                    "languageCode": self.state.lang,
                    "shippingCountryCode": self.address.country,
                },
            )
        except Exception as e:
            logger.warning(f"GriffinMetadataQuery failed: {e}")

        try:
            self.session.graphql(
                "SupportedFundingSourcesQuery",
                SUPPORTED_FUNDING_SOURCES_QUERY,
                {
                    "token": self.state.ec_token or self.ba_token,
                    "userCountry": self.address.country,
                },
            )
        except Exception as e:
            logger.warning(f"SupportedFundingSourcesQuery failed: {e}")

        try:
            address_result = self.session.graphql(
                "AddressAutocompleteFromPostalCodeQuery",
                ADDRESS_AUTOCOMPLETE_FROM_POSTAL_CODE_QUERY,
                {
                    "country": self.address.country,
                    "postalCode": self.address.postal_code,
                    "token": self.state.ec_token or self.ba_token,
                },
            )
            result_obj = address_result[0] if isinstance(address_result, list) else address_result
            normalized = result_obj.get("data", {}).get("addressNormalization") or {}
            line1 = (normalized.get("line1") or "").strip() if isinstance(normalized, dict) else ""
            city = (normalized.get("city") or "").strip() if isinstance(normalized, dict) else ""
            if line1 and city:
                logger.info(
                    "Address normalized: {}, {}, {} {}",
                    normalized.get("line1"),
                    normalized.get("line2"),
                    normalized.get("city"),
                    normalized.get("state"),
                )
                self.address.street = normalized.get("line1") or self.address.street
                self.address.district = normalized.get("line2") or self.address.district
                self.address.city = normalized.get("city") or self.address.city
                self.address.state = normalized.get("state") or self.address.state
                self.address.postal_code = normalized.get("postalCode") or self.address.postal_code
            else:
                logger.warning(
                    "Address normalized empty ({}). Falling back to a known local {} address.",
                    normalized,
                    self.address.country,
                )
                self._fallback_known_address()
        except Exception as e:
            logger.warning(f"AddressAutocompleteFromPostalCodeQuery failed: {e}")
            self._fallback_known_address()

    def _phase3_signup_and_2fa(self):
        """Submit the signup form and trigger 2FA SMS.

        Actual flow discovered from traffic capture:
        1. InitiateRiskBasedTwoFactorPhoneConfirmationMutation → sends SMS, returns authId + challengeId
        2. ConfirmRiskBasedTwoFactorPhoneConfirmationMutation → verifies OTP pin with authId + challengeId
        3. SignUpNewMemberMutation → creates account with all user data + card + address
        """
        logger.info("--- Phase 3: Signup form + 2FA ---")

        # Send Tealeaf to simulate form interaction
        signup_url = self.state.signup_url or "https://www.paypal.com/checkoutweb/signup"
        send_tealeaf_data(self.session, signup_url)

        token = self.state.ec_token or self.ba_token

        # Send OTP challenge (idapps/graphql getOtpChallengeOperation) before
        # InitiateRiskBasedTwoFactor.  BA HAR shows this request is required;
        # without it PayPal returns an auth-challenge HTML page.
        otp_token = self.state.ec_token or token
        if otp_token:
            send_otp_challenge(
                self.session,
                otp_token,
                self.user.email,
                ctx_id=self.state.ctx_id,
                csrf_nonce=self.state.csrf_nonce,
            )

        # Step 1/2: Send SMS and confirm OTP. If the OTP is wrong, the
        # operator can either retry a code for the same phone or enter a new
        # phone number to trigger a fresh challenge.
        self._confirm_phone_with_retry(token, signup_url)

        # Step 3: Sign up new member with all user data. If PayPal rejects the
        # card at addCard/validate.fi/cardNumber, fetch a new generated
        # Visa/MasterCard and submit SignUpNewMember again.
        self._signup_with_card_retry(token, signup_url)

        # Optional: bind AT only if signup returned one. Phase 4 primarily
        # needs billingAgreementId + session cookies, then auto-follows returnURL.
        self._bind_euat()

        # Send analytics for signup completion
        send_analytics_ts(
            self.session,
            "main:billing:hagrid:billingwithoutpurchase:member:review",
            self.ba_token,
            ec_token=self.state.ec_token,
            user_id=self.state.user_id,
        )

    def _bind_euat(self) -> None:
        """Bind AT/EUAT into header cookie sources before final billing calls."""
        token = (self.state.euat_token or "").strip()
        if not token:
            return
        self.state.euat_token = token
        cookie_name = "AV894Kt2TSumQQrJwe-8mzmyREO"
        jar = self.session.client.cookies
        # Clear any prior duplicates first; httpx raises if multiple same-name cookies exist.
        try:
            while cookie_name in jar:
                del jar[cookie_name]
        except Exception:
            pass
        # Only one domain/path to avoid "Multiple cookies exist with name=..."
        jar.set(cookie_name, token, domain=".paypal.com", path="/")
        logger.info("EUAT/AT bound for billing calls (len={})", len(token))

    def _follow_return_url(self, return_url: str, referer: str) -> str:
        """Auto-open returnURL and follow merchant redirects to completion."""
        import re as _re
        import urllib.parse as _urlparse

        final_redirect_url = ""
        if not return_url:
            return final_redirect_url
        try:
            logger.info("Auto-following returnURL...")
            return_resp = self.session.get(
                return_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": referer,
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            final_redirect_url = str(return_resp.url)
            href_pat = "(?:url|href)=[\"']?(https?://[^\"'\\s>]+)"
            meta_pat = "http-equiv=[\"']?refresh[\"']?[^>]*content=[\"']?\\d+;\\s*url=([^\"'>\\s]+)"
            for _ in range(10):
                location = return_resp.headers.get("Location", "")
                if return_resp.status_code in (301, 302, 303, 307, 308) and location:
                    next_url = _urlparse.urljoin(str(return_resp.url), location)
                else:
                    body = return_resp.text or ""
                    m = _re.search(href_pat, body, _re.I)
                    meta = _re.search(meta_pat, body, _re.I)
                    next_url = ""
                    if meta:
                        next_url = _urlparse.urljoin(str(return_resp.url), meta.group(1))
                    elif m and any(x in m.group(1) for x in ("stripe.com", "openai.com", "paypal.com")):
                        candidate = m.group(1)
                        if candidate != final_redirect_url:
                            next_url = candidate
                    if not next_url:
                        break
                if next_url == final_redirect_url:
                    break
                final_redirect_url = next_url
                return_resp = self.session.get(
                    final_redirect_url,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": str(return_resp.url),
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
            logger.success("Final merchant URL: <redacted>")
        except Exception as e:
            logger.warning("Following return URL failed: {}", e)
            if not final_redirect_url:
                final_redirect_url = return_url
        return final_redirect_url


    def _signup_card_reason_code(self, errors: list[dict] | None = None) -> str:
        """Return best card contingency code for Hermes reason= base64 param."""
        # Prefer explicit prior state, then inspect errors, then default.
        existing = (getattr(self.state, "signup_contingency_reason", "") or "").strip()
        if existing:
            return existing
        for err in errors or []:
            msg = str(err.get("message") or "").strip()
            if msg in {
                "CARD_GENERIC_ERROR",
                "INSTRUMENT_SHARING_LIMIT_EXCEEDED",
                "CC_LINKED_TO_FULL_ACCOUNT",
                "CREATE_CARD_ACCOUNT_CANDIDATE_VALIDATION_ERROR",
            }:
                return msg
            error_data = err.get("errorData") or {}
            if isinstance(error_data, dict):
                for val in error_data.values():
                    if isinstance(val, dict):
                        code = str(val.get("code") or "").strip()
                        if code:
                            return code
                code = str(error_data.get("code") or "").strip()
                if code:
                    return code
            checkpoints = set(err.get("checkpoints") or [])
            if checkpoints.intersection({"addCard", "validate.fi", "card", "fi"}):
                return "CARD_GENERIC_ERROR"
        return "CARD_GENERIC_ERROR"

    def _hermes_reason_param(self, errors: list[dict] | None = None) -> str:
        import base64
        code = self._signup_card_reason_code(errors)
        self.state.signup_contingency_reason = code
        return base64.b64encode(code.encode("utf-8")).decode("ascii")

    def _encode_hermes_reason(self, reason_code: str) -> str:
        import base64
        raw = (reason_code or "CARD_GENERIC_ERROR").strip() or "CARD_GENERIC_ERROR"
        # Plain contingency codes contain underscores / short words.
        if "_" in raw or raw in {
            "CARD_GENERIC_ERROR",
            "INSTRUMENT_SHARING_LIMIT_EXCEEDED",
            "CC_LINKED_TO_FULL_ACCOUNT",
            "CREATE_CARD_ACCOUNT_CANDIDATE_VALIDATION_ERROR",
        }:
            return base64.b64encode(raw.encode("utf-8")).decode("ascii")
        # Already-encoded reason from HAR/logs (no underscore, base64 charset).
        if re.fullmatch(r"[A-Za-z0-9+/]+=*", raw) and len(raw) >= 12:
            try:
                decoded = base64.b64decode(raw).decode("utf-8")
                if re.fullmatch(r"[A-Z0-9_]+", decoded or ""):
                    return raw
            except Exception:
                pass
        return base64.b64encode(raw.encode("utf-8")).decode("ascii")

    def _build_hermes_urls(self) -> tuple[str, str]:
        """Build Hermes entry + review URLs matching successful card-contingency HAR.

        Successful browser path (card contingency after SignUp):
          1) /webapps/hermes?...&fromSignupLite=true&addFIContingency=noretry
             &redirectToHermes=true&fallback=1&reason=base64(CARD_GENERIC_ERROR)
          2) same without addFIContingency/redirectToHermes (review shell)

        Important: do NOT force billingLite=1 on first bind; HAR success did not.
        """
        import urllib.parse as _urlparse
        reason = self._hermes_reason_param()
        common = [
            ("ba_token", self.ba_token),
            ("ssrt", self.state.ssrt),
            ("token", self.state.ec_token or self.ba_token),
            ("rcache", "1"),
            ("country.x", self.address.country or self.state.country or "BR"),
            ("locale.x", self.state.locale or "pt_BR"),
            ("fromSignupLite", "true"),
            ("fallback", "1"),
            ("reason", reason),
        ]
        entry = list(common)
        # Insert contingency flags before fallback/reason to mirror HAR order closely.
        entry = [
            ("ba_token", self.ba_token),
            ("ssrt", self.state.ssrt),
            ("token", self.state.ec_token or self.ba_token),
            ("rcache", "1"),
            ("country.x", self.address.country or self.state.country or "BR"),
            ("locale.x", self.state.locale or "pt_BR"),
            ("fromSignupLite", "true"),
            ("addFIContingency", "noretry"),
            ("redirectToHermes", "true"),
            ("fallback", "1"),
            ("reason", reason),
        ]
        review = [
            ("ba_token", self.ba_token),
            ("ssrt", self.state.ssrt),
            ("token", self.state.ec_token or self.ba_token),
            ("rcache", "1"),
            ("country.x", self.address.country or self.state.country or "BR"),
            ("locale.x", self.state.locale or "pt_BR"),
            ("fromSignupLite", "true"),
            ("fallback", "1"),
            ("reason", reason),
        ]
        base = "https://www.paypal.com/webapps/hermes?"
        return base + _urlparse.urlencode(entry), base + _urlparse.urlencode(review)

    def _hermes_page_is_bound(self, resp, html: str = "") -> bool:
        """True only when Hermes HTML looks like a real billing shell, not 403/captcha shell."""
        status = getattr(resp, "status_code", 0) or 0
        text = html if html is not None else (getattr(resp, "text", "") or "")
        page_len = len(text or "")
        if status in (401, 403, 429):
            return False
        if status >= 500:
            return False
        if page_len < 5000:
            return False
        low = text.lower()
        hard = (
            "authchallengenodeweb",
            "geo.captcha-delivery.com",
            "please verify you are a human",
            "datadome",
            "access denied",
        )
        if any(m in low for m in hard) and page_len < 40000:
            return False
        # Positive signals from successful Hermes/pay shell.
        positive = (
            "billingweb",
            "billing agreement",
            "approve",
            "hagrid",
            "checkoutuinodeweb",
            "pay/billing",
            "balancepreference",
            "returnurl",
        )
        if any(m in low for m in positive):
            return True
        # Large authenticated app shell is acceptable.
        return page_len >= 20000 and "paypal" in low

    def _phase4_authorize(self) -> dict:
        """Phase 4: Hermes bind -> authorize(billingAgreementId) -> auto returnURL.

        Not /pay. Final billing is Hermes + GraphQL authorize:
          1) GET Hermes contingency/review to bind buyer session (avoid BUYER_NOT_SET)
          2) POST /graphql/ authorize with billingAgreementId=EC
          3) Auto-follow returned returnURL
        EUAT header remains optional.
        """
        import json as _json
        import urllib.parse as _urlparse

        logger.info("--- Phase 4: Final Hermes billing authorization ---")

        self._bind_euat()

        billing_agreement_id = (self.state.ec_token or self.ba_token or "").strip()
        if not billing_agreement_id:
            return {
                "status": "error",
                "error": "missing billingAgreementId (EC/BA) before authorize",
            }

        hermes_base_url, hermes_review_url = self._build_hermes_urls()
        # Keep review referer as the contingency shell URL (no forced billingLite).
        review_referer = hermes_review_url
        review_url = hermes_review_url
        signup_referer = (
            self.state.signup_url
            or f"https://www.paypal.com/checkoutweb/signup?token={billing_agreement_id}"
            f"&ul=1&locale.x={self.state.locale or 'pt_BR'}&country.x={self.address.country or 'BR'}"
        )
        referer = signup_referer

        # 1) Bind buyer session on Hermes before authorize GraphQL.
        # HAR-proven order after card contingency:
        #   reopen signup shell -> hermes(entry reason/addFIContingency) -> hermes(review)
        hermes_bound = False
        try:
            hermes_headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Referer": referer,
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-User": "?1",
            }
            if self.state.euat_token:
                hermes_headers["X-PayPal-Internal-EUAT"] = self.state.euat_token

            # Warm same-origin signup shell first (HAR referer source).
            try:
                logger.info("Phase4 step0: warm checkoutweb/signup before Hermes bind...")
                warm = self.session.get(
                    signup_referer,
                    headers={
                        **hermes_headers,
                        "Referer": signup_referer,
                    },
                )
                logger.info(
                    "Signup warm before Hermes: status={} bytes={}",
                    warm.status_code,
                    len(warm.content or b""),
                )
                referer = str(getattr(warm, "url", "") or signup_referer)
                hermes_headers["Referer"] = referer
            except Exception as warm_err:
                logger.warning("Signup warm before Hermes failed: {}", warm_err)

            logger.info(
                "Phase4 step1: GET Hermes contingency shell reason={}",
                self.state.signup_contingency_reason or "CARD_GENERIC_ERROR",
            )
            hermes_resp = self.session.get(hermes_base_url, headers=hermes_headers)
            redirect_url = hermes_resp.headers.get("Location", "")
            if hermes_resp.status_code in (301, 302, 303, 307, 308) and redirect_url:
                redirect_url = _urlparse.urljoin(hermes_base_url, redirect_url)
                hermes_resp = self.session.get(
                    redirect_url,
                    headers={**hermes_headers, "Referer": hermes_base_url},
                )
                logger.info("Hermes redirected to: {}", str(hermes_resp.url)[:180])

            # Second navigation: review shell without addFIContingency/redirectToHermes.
            if str(hermes_resp.url).split("#", 1)[0].rstrip("/") != hermes_review_url.rstrip("/"):
                logger.info("Phase4 step1b: GET Hermes review shell (fallback+reason)...")
                hermes_resp = self.session.get(
                    hermes_review_url,
                    headers={
                        **hermes_headers,
                        "Referer": str(hermes_resp.url) if hermes_resp is not None else referer,
                    },
                )

            html = hermes_resp.text or ""
            hermes_bound = self._hermes_page_is_bound(hermes_resp, html)
            logger.info(
                "Hermes review bound: status={} bytes={} bound={} url={}",
                hermes_resp.status_code,
                len(hermes_resp.content or b""),
                hermes_bound,
                str(hermes_resp.url)[:180],
            )
            if hermes_bound:
                referer = str(hermes_resp.url) or hermes_review_url
            else:
                logger.warning(
                    "Hermes bind looks blocked/empty (status={} bytes={}). "
                    "Recovering via headed browser: signup bootstrap -> Hermes.",
                    hermes_resp.status_code,
                    len(hermes_resp.content or b""),
                )
                # Recovery: open signup first (bootstrap), then Hermes target.
                assist = self._run_headed_browser_assist(
                    hermes_base_url,
                    purpose="phase4_hermes",
                    bootstrap_url=signup_referer,
                )
                if assist:
                    # Prefer final browser URL if it already landed on hermes/pay.
                    final_url = (getattr(assist, "final_url", "") or "").strip()
                    rebind_url = hermes_review_url
                    if "webapps/hermes" in final_url or "/pay/billing" in final_url:
                        rebind_url = final_url.split("#", 1)[0]
                    hermes_resp = self.session.get(
                        rebind_url,
                        headers={**hermes_headers, "Referer": signup_referer},
                    )
                    html = hermes_resp.text or ""
                    hermes_bound = self._hermes_page_is_bound(hermes_resp, html)
                    logger.info(
                        "Hermes rebind after browser assist: status={} bytes={} bound={} url={}",
                        hermes_resp.status_code,
                        len(hermes_resp.content or b""),
                        hermes_bound,
                        str(hermes_resp.url)[:180],
                    )
                    if hermes_bound:
                        referer = str(hermes_resp.url) or hermes_review_url
        except Exception as e:
            logger.warning("Hermes pre-bind failed: {}", e)

        if not hermes_bound:
            return {
                "status": "error",
                "error": (
                    "Hermes buyer session bind failed (403/empty shell). "
                    "Authorize skipped to avoid BUYER_NOT_SET. "
                    "Reopen with a cleaner residential sticky BR proxy/session."
                ),
                "billingAgreementId": billing_agreement_id,
                "euat_present": bool(self.state.euat_token),
                "signup_contingency_reason": self.state.signup_contingency_reason
                or "CARD_GENERIC_ERROR",
            }


        common_headers = {
            "Accept": "*/*",
            "Referer": referer,
            "X-App-Name": "checkoutuinodeweb",
            "X-Requested-With": "fetch",
            "PayPal-Client-Context": None,
            "PayPal-Client-Metadata-Id": self.state.paypal_client_metadata_id
            or billing_agreement_id,
            "X-Country": None,
            "X-Locale": None,
        }
        if self.state.euat_token:
            common_headers["X-PayPal-Internal-EUAT"] = self.state.euat_token

        # Capture buyer id from Hermes shell when signup contingency path skipped onboardAccount.
        try:
            if hermes_bound and not self.state.user_id:
                buyer_from_hermes = self._extract_buyer_id_from_html(html if "html" in locals() else "")
                if buyer_from_hermes:
                    self.state.user_id = buyer_from_hermes
                    logger.info("Buyer id extracted from Hermes HTML: {}", buyer_from_hermes)
        except Exception:
            pass

        logger.info(
            "Phase4 step2: authorize mutation billingAgreementId only; euat_optional_len={}",
            len(self.state.euat_token or ""),
        )
        logger.info(
            "billingAgreementId: {}",
            sanitize_for_log({"billingAgreementId": billing_agreement_id})[
                "billingAgreementId"
            ],
        )

        ba_token_resp = self.ba_token
        return_url = ""
        auth_result = None
        max_authorize_attempts = 2
        for auth_attempt in range(1, max_authorize_attempts + 1):
            try:
                auth_result = self.session.graphql(
                    "authorize",
                    AUTHORIZE_BILLING_MUTATION,
                    {
                        "billingAgreementId": billing_agreement_id,
                        "fundingPreference": {"balancePreference": "OPT_OUT"},
                        "legalAgreements": {},
                    },
                    extra_headers=common_headers,
                    batched=True,
                    endpoint="https://www.paypal.com/graphql/",
                )
            except Exception as e:
                logger.error("authorize mutation failed: {}", e)
                auth_result = {"errors": [{"message": str(e)}]}

            logger.info(
                "authorize result attempt {}/{} (sanitized): {}",
                auth_attempt,
                max_authorize_attempts,
                _json.dumps(sanitize_for_log(auth_result), ensure_ascii=False, indent=2)[:800],
            )

            try:
                parsed_return, parsed_ba, authorize_data = self._parse_authorize_payload(auth_result)
                if parsed_return:
                    return_url = parsed_return
                if parsed_ba:
                    ba_token_resp = parsed_ba
                buyer = (authorize_data or {}).get("buyer") or {}
                if buyer.get("userId"):
                    self.state.user_id = buyer["userId"]
            except Exception as e:
                logger.warning("Failed to parse authorize response: {}", e)

            if return_url:
                break

            buyer_not_set = self._has_buyer_not_set(auth_result)
            anonymous = self._is_anonymous_auth_error(auth_result)
            if anonymous and not self.state.euat_token:
                logger.error(
                    "authorize is ANONYMOUS without EUAT; skip Hermes rebind/retry"
                )
                break
            if (not buyer_not_set and not anonymous) or auth_attempt >= max_authorize_attempts:
                break

            logger.warning(
                "authorize BUYER_NOT_SET on attempt {}/{}; rebinding Hermes buyer session then retry",
                auth_attempt,
                max_authorize_attempts,
            )
            # Re-bind EUAT cookie and Hermes review shell once.
            try:
                self._bind_euat()
            except Exception:
                pass
            try:
                assist = self._run_headed_browser_assist(
                    hermes_review_url,
                    purpose="phase4_hermes_retry",
                    bootstrap_url=signup_referer,
                )
                rebind_url = hermes_review_url
                if assist and getattr(assist, "final_url", ""):
                    final_url = (assist.final_url or "").strip()
                    if "webapps/hermes" in final_url or "/pay/billing" in final_url:
                        rebind_url = final_url.split("#", 1)[0]
                rebind_headers = {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": signup_referer,
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-User": "?1",
                }
                if self.state.euat_token:
                    rebind_headers["X-PayPal-Internal-EUAT"] = self.state.euat_token
                hermes_resp = self.session.get(rebind_url, headers=rebind_headers)
                html = hermes_resp.text or ""
                hermes_bound = self._hermes_page_is_bound(hermes_resp, html)
                logger.info(
                    "Hermes rebind before authorize retry: status={} bytes={} bound={}",
                    hermes_resp.status_code,
                    len(hermes_resp.content or b""),
                    hermes_bound,
                )
                if hermes_bound:
                    referer = str(hermes_resp.url) or hermes_review_url
                    common_headers["Referer"] = referer
                buyer_from_hermes = self._extract_buyer_id_from_html(html)
                if buyer_from_hermes:
                    self.state.user_id = buyer_from_hermes
            except Exception as e:
                logger.warning("Hermes rebind before authorize retry failed: {}", e)

        # ContextQuery returnURL is merchant config only; do NOT treat it as authorize success.
        if not return_url and self._is_anonymous_auth_error(auth_result):
            logger.error(
                "authorize failed: auth state ANONYMOUS (needs LOGGEDIN/REMEMBERED); "
                "refusing ContextQuery fake-success fallback"
            )
            # Anonymous means createMember never produced a buyer session.
            # /pay/billing recovery without EUAT is not meaningful.
            return {
                "status": "error",
                "error": (
                    "authorize ANONYMOUS: member was not logged in/remembered. "
                    "Usually createMemberAccount/OAS_ERROR without accessToken. "
                    "Reopen with fresh email/phone/profile and cleaner sticky proxy."
                ),
                "raw_response": auth_result,
                "euat_present": bool(self.state.euat_token),
                "user_id": self.state.user_id,
                "signup_contingency_reason": self.state.signup_contingency_reason
                or "",
            }

        if not return_url and self._has_buyer_not_set(auth_result):
            logger.error(
                "authorize failed with BUYER_NOT_SET; refusing ContextQuery fake-success fallback"
            )
            recovery = self._phase4_pay_billing_recovery(referer)
            if recovery and recovery.get("status") == "success":
                return recovery
            return {
                "status": "error",
                "error": (
                    "authorize BUYER_NOT_SET after Hermes bind/retry; "
                    "buyer session not authorized. Reopen with cleaner sticky proxy "
                    "or complete captcha in headed browser during Phase4."
                ),
                "raw_response": auth_result,
                "euat_present": bool(self.state.euat_token),
                "user_id": self.state.user_id,
                "signup_contingency_reason": self.state.signup_contingency_reason
                or "",
            }

        if not return_url:
            # Non-BUYER failures: still no context-query fake success.
            return {
                "status": "error",
                "error": "no returnURL from authorize mutation",
                "raw_response": auth_result,
            }

        self.state.return_url = return_url
        logger.success(
            "Billing Agreement Token: {}",
            sanitize_for_log({"billingAgreementToken": ba_token_resp})[
                "billingAgreementToken"
            ],
        )
        logger.success("Return URL obtained; auto-following returnURL...")
        final_redirect_url = self._follow_return_url(self.state.return_url, referer)
        if not final_redirect_url:
            final_redirect_url = self.state.return_url
            logger.info(
                "ReturnURL auto-follow kept original href as final_redirect_url"
            )

        send_analytics_ts(
            self.session,
            "main:billing:hagrid:billingwithoutpurchase:member:submitButtonFullEvent",
            self.ba_token,
            ec_token=self.state.ec_token,
            user_id=self.state.user_id,
            event="cl",
        )
        return {
            "status": "success",
            "ba_token": ba_token_resp,
            "ec_token": self.state.ec_token,
            "user_id": self.state.user_id,
            "return_url": self.state.return_url,
            "final_redirect_url": final_redirect_url,
        }

