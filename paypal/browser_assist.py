"""Headed browser assist for DataDome / authchallenge.

Uses Playwright Chromium in headed mode. Cookies (and optional OTP initiate
result) are exported back into the protocol httpx session so the rest of the
flow stays HTTP.
"""
from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from loguru import logger


@dataclass
class BrowserAssistResult:
    ok: bool
    final_url: str = ""
    cookies: list[dict[str, Any]] | None = None
    reason: str = ""
    page_bytes: int = 0
    otp_auth_id: str = ""
    otp_challenge_id: str = ""
    otp_state: str = ""
    signup_result: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_PLAYWRIGHT_INSTALL_ATTEMPTED = False


def _ensure_playwright():
    global _PLAYWRIGHT_INSTALL_ATTEMPTED
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return
    except Exception:
        pass
    if _PLAYWRIGHT_INSTALL_ATTEMPTED:
        raise RuntimeError("playwright is not available")
    _PLAYWRIGHT_INSTALL_ATTEMPTED = True
    logger.warning("Installing playwright + chromium for headed browser assist...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"])
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
    importlib.invalidate_caches()
    from playwright.sync_api import sync_playwright  # noqa: F401


def _proxy_for_playwright(proxy_url: str | None) -> dict[str, str] | None:
    """Convert httpx proxy URL to Playwright proxy config.

    Credentials are unquoted. Fragment suffixes (log fingerprints) are ignored.
    Returns None only when proxy_url is empty/invalid.
    """
    if not proxy_url:
        return None
    raw = str(proxy_url).strip()
    if not raw:
        return None
    # Drop accidental log fingerprint like host:port#abcdef
    if "#" in raw and "://" in raw:
        raw = raw.split("#", 1)[0]
    parts = urlsplit(raw)
    host = parts.hostname
    if not host:
        return None
    scheme = (parts.scheme or "http").lower()
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        if scheme in {"http", "socks5", "socks5h"}:
            port = 80 if scheme == "http" else 1080
        elif scheme == "https":
            port = 443
        else:
            return None
    server = f"{scheme}://{host}:{port}"
    conf: dict[str, str] = {"server": server}
    username = unquote(parts.username) if parts.username else ""
    password = unquote(parts.password) if parts.password else ""
    if username:
        conf["username"] = username
    if password:
        conf["password"] = password
    return conf


def _mask_proxy_for_log(proxy_url: str | None) -> str:
    if not proxy_url:
        return "off"
    conf = _proxy_for_playwright(proxy_url)
    if not conf:
        return "invalid"
    server = conf.get("server", "")
    if conf.get("username") or conf.get("password"):
        return server.replace("://", "://***:***@", 1)
    return server or "on"


def _is_hard_challenge_html(text: str) -> bool:
    """True only for real captcha/authchallenge shells, not normal signup pages."""
    low = (text or "").lower()
    hard = (
        "authchallengenodeweb",
        "geo.captcha-delivery.com",
        "ads-dd-captcha",
        "adsddtoken",
        "please verify you are a human",
        "confirm you are human",
        "move the slider",
        "slide the puzzle",
        "press and hold",
        "captcha__puzzle",
    )
    if any(m in low for m in hard):
        if "authchallengenodeweb" in low:
            return True
        if len(text or "") < 50000:
            return True
    if "datadome" in low and len(text or "") < 25000:
        return True
    return False


def _is_signup_form_html(url: str, html: str) -> bool:
    """True when headed browser already reached the real BR/US signup form."""
    low = (html or "").lower()
    # strip accents for robust matching
    trans = str.maketrans({
        "á": "a", "à": "a", "ã": "a", "â": "a",
        "é": "e", "ê": "e",
        "í": "i",
        "ó": "o", "ô": "o", "õ": "o",
        "ú": "u", "ü": "u",
        "ç": "c",
    })
    norm = low.translate(trans)
    form_markers = (
        "numero do cartao",
        "card number",
        "pague com cartao",
        "pay with debit",
        "pay with credit",
        "e-mail",
        "email",
        "data de vencimento",
        "expiration date",
        "seu endereco de cobranca",
        "billing address",
        "nome",
        "sobrenome",
        "first name",
        "last name",
        'name="cardnumber"',
        'autocomplete="cc-number"',
        'name="email"',
        'type="email"',
        "numero de telefone",
        "phone number",
    )
    hits = sum(1 for m in form_markers if m in norm)
    on_signup = "checkoutweb/signup" in (url or "").lower()
    return hits >= 3 and (len(html or "") > 12000 or on_signup)


def _page_looks_usable(url: str, html: str) -> bool:
    text = html or ""
    low = text.lower()
    url_l = (url or "").lower()

    # Highest priority: real signup form already visible. Operator need do nothing.
    if _is_signup_form_html(url, text):
        return True

    # Hermes / pay billing shells after card contingency.
    if "webapps/hermes" in url_l or "/pay/billing" in url_l:
        if _is_hard_challenge_html(text):
            return False
        if len(text) >= 8000 and "paypal" in low:
            return True
        if len(text) >= 20000:
            return True

    if _is_hard_challenge_html(text):
        return False

    if re.search(r"EC-[A-Za-z0-9]+", f"{url}\n{text}"):
        if "checkoutweb/signup" in (url or "") or len(text) > 30000:
            return True
    if "checkoutweb/signup" in (url or "") and len(text) > 20000:
        return True
    if "agreements/approve" in (url or "") and len(text) > 40000:
        return True
    if "ssrt=" in (url or "") and len(text) > 30000 and "paypal" in low:
        return True
    return False


def _browser_initiate_otp(
    page,
    *,
    phone_local: str,
    country: str,
    lang: str,
    token: str,
    mutation: str,
) -> dict[str, str]:
    """Run InitiateRiskBasedTwoFactorPhoneConfirmationMutation inside page context."""
    payload = {
        "operationName": "InitiateRiskBasedTwoFactorPhoneConfirmationMutation",
        "variables": {
            "phoneNumber": phone_local,
            "locale": {"country": country, "lang": lang},
            "phoneCountry": country,
            "token": token,
        },
        "query": mutation,
    }
    result = page.evaluate(
        """async ({payload, token, country, lang}) => {
            const resp = await fetch(
              'https://www.paypal.com/graphql?InitiateRiskBasedTwoFactorPhoneConfirmationMutation',
              {
                method: 'POST',
                credentials: 'include',
                headers: {
                  'content-type': 'application/json',
                  'x-app-name': 'checkoutuinodeweb_weasley',
                  'x-requested-with': 'fetch',
                  'paypal-client-context': token,
                  'paypal-client-metadata-id': token,
                  'x-country': country,
                  'x-locale': `${lang}_${country}`,
                  'origin': 'https://www.paypal.com',
                  'referer': location.href,
                },
                body: JSON.stringify(payload),
              }
            );
            const text = await resp.text();
            return { status: resp.status, text, contentType: resp.headers.get('content-type') || '' };
        }""",
        {
            "payload": payload,
            "token": token,
            "country": country,
            "lang": lang,
        },
    )
    text = (result or {}).get("text") or ""
    status = (result or {}).get("status")
    logger.info(
        "Browser OTP initiate HTTP {} content_type={} bytes={}",
        status,
        (result or {}).get("contentType"),
        len(text),
    )
    if "authchallengenodeweb" in text.lower():
        return {"error": "authchallenge_html"}
    try:
        data = json.loads(text)
    except Exception:
        return {"error": f"non_json status={status} bytes={len(text)}"}
    node = None
    if isinstance(data, list) and data:
        node = (data[0].get("data") or {}).get("initiateRiskBasedTwoFactorPhoneConfirmation")
    elif isinstance(data, dict):
        node = (data.get("data") or {}).get("initiateRiskBasedTwoFactorPhoneConfirmation")
    if not isinstance(node, dict):
        return {"error": f"missing_node body={text[:300]}"}
    return {
        "authId": str(node.get("authId") or ""),
        "challengeId": str(node.get("challengeId") or ""),
        "state": str(node.get("state") or ""),
    }


def _browser_signup_new_member(
    page,
    *,
    variables: dict[str, Any],
    mutation: str,
    token: str,
    country: str,
    lang: str,
    fn_sync_data: str,
) -> dict[str, Any]:
    """Run SignUpNewMemberMutation inside page context with browser cookies."""
    payload = {
        "operationName": "SignUpNewMemberMutation",
        "variables": variables,
        "query": mutation,
        "fn_sync_data": fn_sync_data,
    }
    result = page.evaluate(
        """async ({payload, token, country, lang}) => {
            const resp = await fetch(
              'https://www.paypal.com/graphql?SignUpNewMemberMutation',
              {
                method: 'POST',
                credentials: 'include',
                headers: {
                  'content-type': 'application/json',
                  'x-app-name': 'checkoutuinodeweb_weasley',
                  'x-requested-with': 'fetch',
                  'paypal-client-context': token,
                  'paypal-client-metadata-id': token,
                  'x-country': country,
                  'x-locale': `${lang}_${country}`,
                  'origin': 'https://www.paypal.com',
                  'referer': location.href,
                },
                body: JSON.stringify(payload),
              }
            );
            const text = await resp.text();
            const headers = {};
            try {
              for (const [k, v] of resp.headers.entries()) {
                headers[k] = v;
              }
            } catch (e) {}
            return {
              status: resp.status,
              text,
              contentType: resp.headers.get('content-type') || '',
              headers,
            };
        }""",
        {
            "payload": payload,
            "token": token,
            "country": country,
            "lang": lang,
        },
    )
    text_body = (result or {}).get("text") or ""
    status = (result or {}).get("status")
    content_type = (result or {}).get("contentType") or ""
    headers = (result or {}).get("headers") or {}
    logger.info(
        "Browser SignUpNewMember HTTP {} content_type={} bytes={}",
        status,
        content_type,
        len(text_body),
    )
    if "authchallengenodeweb" in text_body.lower():
        return {
            "data": {},
            "errors": [{
                "message": "NON_JSON_RESPONSE",
                "errorData": {"browser": "authchallenge_html", "status": status},
            }],
        }
    try:
        data = json.loads(text_body)
    except Exception:
        token_match = re.search(
            r'(?:accessToken|euat|x-paypal-internal-euat)["\x27\s:=]+([A-Za-z0-9_\-]{40,})',
            text_body,
        )
        if token_match:
            extracted = token_match.group(1)
            logger.info("Extracted EUAT token from browser non-JSON SignUp response")
            return {
                "data": {
                    "onboardAccount": {
                        "buyer": {"auth": {"accessToken": extracted}, "userId": ""}
                    }
                },
                "errors": [],
            }
        header_euat = (
            headers.get("x-paypal-internal-euat")
            or headers.get("X-PayPal-Internal-EUAT")
            or ""
        )
        if header_euat:
            logger.info("Found EUAT token in browser SignUp response headers")
            return {
                "data": {
                    "onboardAccount": {
                        "buyer": {"auth": {"accessToken": header_euat}, "userId": ""}
                    }
                },
                "errors": [],
            }
        return {
            "data": {},
            "errors": [{
                "message": "NON_JSON_RESPONSE",
                "errorData": {
                    "browser": f"non_json status={status} bytes={len(text_body)}",
                    "status": status,
                },
            }],
        }
    if isinstance(data, list):
        return data[0] if data else {"data": {}, "errors": [{"message": "EMPTY_RESPONSE", "errorData": {}}]}
    if isinstance(data, dict):
        return data
    return {"data": {}, "errors": [{"message": "INVALID_RESPONSE", "errorData": {}}]}


def solve_with_headed_browser(
    url: str,
    *,
    proxy_url: str | None = None,
    seed_cookies: list[dict[str, Any]] | None = None,
    user_agent: str | None = None,
    timeout_sec: float = 120.0,
    wait_for_manual: bool = True,
    purpose: str = "challenge",
    bootstrap_url: str | None = None,
    otp_phone_local: str | None = None,
    otp_country: str = "BR",
    otp_lang: str = "pt",
    otp_token: str | None = None,
    otp_mutation: str | None = None,
    signup_variables: dict[str, Any] | None = None,
    signup_mutation: str | None = None,
    signup_token: str | None = None,
    signup_country: str = "BR",
    signup_lang: str = "pt",
    signup_fn_sync_data: str | None = None,
) -> BrowserAssistResult:
    """Open headed Chromium, wait until challenge clears, return cookies.

    For otp_authchallenge / signup_authchallenge, once signup form is visible,
    also attempt OTP initiate or SignUpNewMember from the browser page context.
    Operator only needs to act if a real captcha/authchallenge UI appears.
    """
    _ensure_playwright()
    from playwright.sync_api import sync_playwright
    from config import USER_AGENT, VIEWPORT

    ua = user_agent or USER_AGENT
    proxy = _proxy_for_playwright(proxy_url)
    if proxy_url and not proxy:
        raise RuntimeError(
            f"Headed browser proxy parse failed; refusing bare connect. proxy={_mask_proxy_for_log(proxy_url)}"
        )
    logger.info(
        "Headed browser assist start purpose={} url={} proxy={} timeout={}s",
        purpose,
        (url or "")[:160],
        _mask_proxy_for_log(proxy_url) if proxy else "off",
        int(timeout_sec),
    )

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                user_agent=ua,
                viewport=VIEWPORT,
                locale="pt-BR",
                proxy=proxy,
            )
            if seed_cookies:
                try:
                    normalized = []
                    for c in seed_cookies:
                        item = {
                            "name": c.get("name"),
                            "value": c.get("value"),
                            "domain": c.get("domain") or ".paypal.com",
                            "path": c.get("path") or "/",
                        }
                        if c.get("secure") is not None:
                            item["secure"] = bool(c.get("secure"))
                        if item["name"] and item["value"] is not None:
                            normalized.append(item)
                    if normalized:
                        context.add_cookies(normalized)
                except Exception as e:
                    logger.warning("Seed cookies into browser failed: {}", e)

            page = context.new_page()
            # HAR-aligned navigation: for Hermes bind, first open signup shell
            # (same-origin referer + cookies), then navigate to target Hermes URL.
            # Cold-opening Hermes often yields 403 / connection closed.
            first_url = (bootstrap_url or "").strip() or url
            page.goto(first_url, wait_until="domcontentloaded", timeout=60000)
            if bootstrap_url and url and bootstrap_url.rstrip("/") != url.rstrip("/"):
                try:
                    page.wait_for_timeout(800)
                    logger.info(
                        "Headed browser bootstrap done; navigating to target purpose={} url={}",
                        purpose,
                        (url or "")[:160],
                    )
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    logger.warning(
                        "Headed browser target navigation failed purpose={} err={}; keeping bootstrap page",
                        purpose,
                        e,
                    )
            deadline = time.time() + max(15.0, float(timeout_sec))
            last_url = page.url
            last_len = 0
            last_html = ""
            usable = False
            while time.time() < deadline:
                try:
                    last_url = page.url
                    last_html = page.content()
                    last_len = len(last_html or "")
                    if _page_looks_usable(last_url, last_html):
                        if _is_signup_form_html(last_url, last_html):
                            logger.success(
                                "Headed browser already on signup form; no manual action needed. url={} bytes={}",
                                last_url[:160],
                                last_len,
                            )
                            usable = True
                            break
                        page.wait_for_timeout(1000)
                        last_html = page.content()
                        last_url = page.url
                        last_len = len(last_html or "")
                        if _page_looks_usable(last_url, last_html):
                            usable = True
                            break
                    if wait_for_manual:
                        if _is_hard_challenge_html(last_html):
                            logger.info(
                                "Headed browser: please complete captcha/authchallenge in the window... url={} bytes={}",
                                last_url[:140],
                                last_len,
                            )
                        else:
                            logger.info(
                                "Headed browser waiting page settle... url={} bytes={}",
                                last_url[:140],
                                last_len,
                            )
                    page.wait_for_timeout(1200)
                except Exception as e:
                    logger.warning("Headed browser poll error: {}", e)
                    page.wait_for_timeout(1000)

            # Soft success: large signup URL without hard challenge even if form
            # markers were partially missing (SPA hydration differences).
            if not usable and "checkoutweb/signup" in (last_url or "") and last_len > 20000 and not _is_hard_challenge_html(last_html):
                logger.warning(
                    "Headed browser timeout soft-pass on signup shell url={} bytes={}",
                    last_url[:160],
                    last_len,
                )
                usable = True
            if (
                not usable
                and ("webapps/hermes" in (last_url or "") or "/pay/billing" in (last_url or ""))
                and last_len > 8000
                and not _is_hard_challenge_html(last_html)
            ):
                logger.warning(
                    "Headed browser timeout soft-pass on hermes/billing shell url={} bytes={}",
                    last_url[:160],
                    last_len,
                )
                usable = True

            otp_auth_id = ""
            otp_challenge_id = ""
            otp_state = ""
            signup_result = None
            if (
                usable
                and purpose == "otp_authchallenge"
                and otp_phone_local
                and otp_token
                and otp_mutation
            ):
                try:
                    logger.info("Attempting OTP initiate from headed browser page context...")
                    otp_res = _browser_initiate_otp(
                        page,
                        phone_local=otp_phone_local,
                        country=otp_country,
                        lang=otp_lang,
                        token=otp_token,
                        mutation=otp_mutation,
                    )
                    if otp_res.get("authId") and otp_res.get("challengeId"):
                        otp_auth_id = otp_res["authId"]
                        otp_challenge_id = otp_res["challengeId"]
                        otp_state = otp_res.get("state") or ""
                        logger.success(
                            "Browser OTP initiate success state={} authId/challengeId present",
                            otp_state or "?",
                        )
                    else:
                        logger.warning("Browser OTP initiate failed: {}", otp_res.get("error") or otp_res)
                except Exception as e:
                    logger.warning("Browser OTP initiate exception: {}", e)

            if (
                usable
                and purpose == "signup_authchallenge"
                and signup_variables
                and signup_mutation
                and signup_token
                and signup_fn_sync_data is not None
            ):
                try:
                    logger.info("Attempting SignUpNewMember from headed browser page context...")
                    signup_result = _browser_signup_new_member(
                        page,
                        variables=signup_variables,
                        mutation=signup_mutation,
                        token=signup_token,
                        country=signup_country,
                        lang=signup_lang,
                        fn_sync_data=signup_fn_sync_data,
                    )
                    onboard = (signup_result or {}).get("data", {}).get("onboardAccount")
                    errs = (signup_result or {}).get("errors") or []
                    if onboard:
                        logger.success("Browser SignUpNewMember returned onboardAccount")
                    elif errs:
                        logger.warning(
                            "Browser SignUpNewMember returned errors: {}",
                            [str(e.get("message") or "") for e in errs if isinstance(e, dict)][:5],
                        )
                    else:
                        logger.warning("Browser SignUpNewMember returned empty onboardAccount")
                except Exception as e:
                    logger.warning("Browser SignUpNewMember exception: {}", e)
                    signup_result = {
                        "data": {},
                        "errors": [{"message": "BROWSER_SIGNUP_EXCEPTION", "errorData": {"detail": str(e)}}],
                    }

            cookies = context.cookies()
            browser.close()
            if not usable:
                return BrowserAssistResult(
                    ok=False,
                    final_url=last_url,
                    cookies=cookies,
                    reason="timeout_waiting_challenge_clear",
                    page_bytes=last_len,
                )
            reason = "cleared"
            if otp_auth_id and otp_challenge_id:
                reason = "cleared_with_otp"
            elif signup_result is not None:
                reason = "cleared_with_signup"
            logger.success(
                "Headed browser assist cleared purpose={} final_url={} bytes={} cookies={} otp={} signup={}",
                purpose,
                last_url[:160],
                last_len,
                len(cookies or []),
                bool(otp_auth_id and otp_challenge_id),
                signup_result is not None,
            )
            return BrowserAssistResult(
                ok=True,
                final_url=last_url,
                cookies=cookies,
                reason=reason,
                page_bytes=last_len,
                otp_auth_id=otp_auth_id,
                otp_challenge_id=otp_challenge_id,
                otp_state=otp_state,
                signup_result=signup_result,
            )
    except Exception as e:
        logger.error("Headed browser assist failed: {}", e)
        return BrowserAssistResult(ok=False, reason=f"browser_error:{e}")
