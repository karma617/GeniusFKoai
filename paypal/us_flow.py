from __future__ import annotations

import os
import re
import json
import urllib.parse
from typing import Any

from loguru import logger

from config import SCREEN, USER_AGENT
from paypal.analytics import send_observability_emit, send_weasley_log
from paypal.fingerprint import build_signup_fn_sync_data, send_device_fingerprint, send_otp_challenge, send_signup_field_events
from paypal.flow import PayPalFlow
from paypal.graphql import (
    ADDRESS_AUTOCOMPLETE_FROM_POSTAL_CODE_QUERY,
    CHECKOUT_SESSION_DATA_QUERY,
    DEFERRED_FEATURE_QUERY,
    GRIFFIN_METADATA_QUERY,
    INSTALLMENT_OPTIONS_QUERY,
    INITIATE_2FA_PHONE_MUTATION,
    SIGNUP_NEW_MEMBER_MUTATION,
    SUPPORTED_FUNDING_SOURCES_QUERY,
)
from paypal.session import sanitize_for_log
from paypal.tealeaf import send_tealeaf_data


class PayPalUSFlow(PayPalFlow):
    """US Billing Agreement flow based on the captured OpenAI PayPal HAR.

    This intentionally stays separate from the existing BR flow. The shared
    phases are reused, while country-specific signup payloads and final
    /pay/billing approval follow the US capture.
    """

    country = "US"
    lang = "en"
    locale = "en_US"
    flow_name = "US"
    phone_calling_code = "1"
    billing_reason = "Ul9FUlJPUg=="
    billing_next_action_fallback = ""
    billing_send_rsc_header = True
    send_installment_options = True
    # US HAR guest path goes checkoutweb/signup with EC token; avoid /pay compact form.
    direct_signup_from_initial_ec = True
    require_ec_signup_context = True

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.state.country = self.country
        self.state.lang = self.lang
        self.state.locale = self.locale

    def _build_signup_url(self) -> str:
        params: list[tuple[str, str]] = []
        if self.state.ssrt:
            params.append(("ssrt", self.state.ssrt))
        params.extend([
            ("ul", "1"),
            ("locale.x", self.locale),
            ("country.x", self.country),
            ("ba_token", self.ba_token),
            ("token", self.state.ec_token),
            ("rcache", "1"),
        ])
        return "https://www.paypal.com/checkoutweb/signup?" + urllib.parse.urlencode(params)

    def _update_user_phone(self, phone: str):
        raw = (phone or "").strip()
        if raw.lower().startswith("phone:"):
            raw = raw.split(":", 1)[1].strip()

        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) < 8:
            raise ValueError("phone number is too short")

        if digits.startswith(self.phone_calling_code) and len(digits) > 10:
            local = digits[len(self.phone_calling_code):]
        else:
            local = digits
        if len(local) < 8:
            raise ValueError("local phone number is too short")

        self.user.phone = f"+{self.phone_calling_code}{local}"
        self.user.phone_country_code = f"+{self.phone_calling_code}"
        self.user.phone_local = local
        logger.info("Phone updated for OTP retry: {}", self._masked_phone())

    def _initiate_2fa_phone_confirmation(self, token: str, signup_url: str) -> tuple[str, str]:
        logger.info(
            "Step 1: Initiating {} 2FA phone confirmation for {}...",
            self.flow_name,
            self._masked_phone(),
        )
        send_weasley_log(
            self.session,
            self.state.ec_token,
            signup_url,
            [
                "weasley_risk_based_phone_confirmation_modal_component_mounted",
                "weasley_initiate_phone_confirmation_start",
                "weasley_api_request_initiate_risk_based_two_factor_phone_confirmation_mutation",
            ],
            country=self.country,
            lang=self.lang,
        )
        initiate_result = self.session.graphql(
            "InitiateRiskBasedTwoFactorPhoneConfirmationMutation",
            INITIATE_2FA_PHONE_MUTATION,
            {
                "phoneNumber": self.user.phone_local,
                "locale": {"country": self.country, "lang": self.lang},
                "phoneCountry": self.country,
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
            raise RuntimeError("Failed to get authId/challengeId from 2FA initiation")
        return auth_id, challenge_id

    def _build_signup_variables(self, token: str) -> dict:
        card_type = self._card_issuer_type()
        variables = {
            "card": {
                "cardNumber": self.card.number,
                "expirationDate": self._card_expiration_date(),
                "securityCode": self.card.cvv,
                "type": card_type,
            },
            "country": self.country,
            "email": self.user.email,
            "firstName": self.user.first_name,
            "lastName": self.user.last_name,
            "phone": {
                "countryCode": self.phone_calling_code,
                "number": self.user.phone_local,
                "type": "MOBILE",
            },
            "supportedThreeDsExperiences": ["IFRAME"],
            "token": token,
            "billingAddress": {
                "postalCode": self.address.postal_code,
                "line1": self.address.street,
                "city": self.address.city,
                "state": self.address.state,
                "accountQuality": {
                    "autoCompleteType": "MANUAL",
                    "isUserModified": True,
                },
                "country": self.country,
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
                "country": self.country,
                "familyName": self.user.last_name,
                "givenName": self.user.first_name,
            },
            "contentIdentifier": self.state.content_identifier or (
                f"{self.country}:{self.lang}:"
                f"{self.state.content_hash}:"
                "compliance.signupTerms"
                if self.state.content_hash
                else f"{self.country}:{self.lang}:compliance.signupTerms"
            ),
            "marketingOptOut": False,
            "password": self.user.password,
            "crsData": None,
            "legalAgreements": {},
        }
        return variables

    def _send_signup_attempt(
        self,
        token: str,
        signup_url: str,
        *,
        allow_browser_assist: bool = True,
        **_kwargs,
    ) -> dict:
        """US/BA signup submit.

        Must accept allow_browser_assist because base _signup_with_card_retry
        always passes it. Keep US payload (no BR CPF/DOB fields) separate from
        BR flow, then reuse the same one-shot headed assist on challenge shells.
        """
        card_type = self._card_issuer_type()
        if self.send_installment_options:
            try:
                self.session.graphql(
                    "InstallmentOptionsQuery",
                    INSTALLMENT_OPTIONS_QUERY,
                    {
                        "buyerCountry": self.country,
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
            country=self.country,
            lang=self.lang,
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
        extra_body = self._signup_extra_body(token)
        fn_sync = None
        if isinstance(extra_body, dict):
            fn_sync = extra_body.get("fn_sync_data")
        try:
            signup_result = self.session.graphql(
                "SignUpNewMemberMutation",
                SIGNUP_NEW_MEMBER_MUTATION,
                variables,
                extra_body=extra_body,
            )
        except ValueError:
            logger.warning(
                "SignUpNewMember returned non-JSON and no token found; returning synthetic error"
            )
            signup_result = {
                "data": {},
                "errors": [{"message": "NON_JSON_RESPONSE", "errorData": {}}],
            }

        # Match base-flow contract: one headed assist if SignUp lands on challenge HTML.
        result_obj = signup_result[0] if isinstance(signup_result, list) else signup_result
        errors = (result_obj or {}).get("errors") or []
        onboard = ((result_obj or {}).get("data") or {}).get("onboardAccount")
        if (
            allow_browser_assist
            and not onboard
            and self._is_signup_challenge_error(errors)
        ):
            # Prefer US/BA locale.country from flow class, not polluted CN redirect URLs.
            assist_url = self._build_signup_url()
            if signup_url and f"country.x={self.country}" in signup_url:
                assist_url = signup_url
            logger.warning(
                "US/BA SignUpNewMember challenged (authchallenge/non-JSON). "
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
                logger.info(
                    "Using SignUpNewMember result from headed browser page context"
                )
            elif assist:
                logger.warning(
                    "Browser assist cleared page but no signup payload returned; "
                    "retrying SignUpNewMember over HTTP once"
                )
                try:
                    signup_result = self.session.graphql(
                        "SignUpNewMemberMutation",
                        SIGNUP_NEW_MEMBER_MUTATION,
                        variables,
                        extra_body=extra_body,
                    )
                except ValueError:
                    logger.warning(
                        "HTTP SignUpNewMember still non-JSON after browser assist"
                    )
                    signup_result = {
                        "data": {},
                        "errors": [
                            {
                                "message": "NON_JSON_RESPONSE",
                                "errorData": {"after": "browser_assist"},
                            }
                        ],
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

    def _signup_extra_body(self, token: str) -> dict | None:
        return {"fn_sync_data": build_signup_fn_sync_data(token)}

    def _phase2_create_account(self):
        super()._phase2_create_account()
        if not self.state.signup_url and self.state.ec_token:
            self.state.signup_url = self._build_signup_url()

    def _phase2_country_bootstrap(self, signup_resp_text: str):
        return None

    def _phase2_create_account_graphql_warmup(self):
        return None

    def _phase4_authorize(self) -> dict:
        logger.info("--- Phase 4: {} /pay/billing approval ---", self.flow_name)
        billing_url = self._us_billing_url()
        hermes_url = self._us_hermes_url()
        base_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": self.state.signup_url,
            "Upgrade-Insecure-Requests": "1",
        }

        try:
            logger.info("Loading {} Hermes review context...", self.flow_name)
            hermes_resp = self.session.get(hermes_url, headers=base_headers)
            redirect_url = hermes_resp.headers.get("Location", "")
            if hermes_resp.status_code in (301, 302, 303, 307, 308) and redirect_url:
                redirect_url = urllib.parse.urljoin(hermes_url, redirect_url)
                hermes_resp = self.session.get(
                    redirect_url,
                    headers={**base_headers, "Referer": hermes_url},
                )
            review_url = self._us_hermes_review_url()
            if review_url != hermes_url and str(hermes_resp.url) != review_url:
                hermes_resp = self.session.get(
                    review_url,
                    headers={**base_headers, "Referer": hermes_url},
                )
            logger.info(
                "{} Hermes loaded: {} bytes={}",
                self.flow_name,
                hermes_resp.status_code,
                len(hermes_resp.content),
            )
            billing_url = self._extract_billing_url(hermes_resp.text, billing_url)
            next_action = self._extract_pay_billing_action_id(hermes_resp.text)
            if not next_action:
                next_action = os.getenv(
                    f"PAYPAL_{self.flow_name}_BILLING_NEXT_ACTION",
                    "",
                ).strip()
            if not next_action:
                next_action = self.billing_next_action_fallback
                if next_action:
                    logger.warning(
                        "Using {} HAR-captured /pay/billing Next-Action fallback.",
                        self.flow_name,
                    )
            if not next_action:
                logger.warning(
                    "{} /pay/billing Next-Action id was not found. "
                    "Set PAYPAL_{}_BILLING_NEXT_ACTION if PayPal does not expose it in HTML.",
                    self.flow_name,
                    self.flow_name,
                )
            user_match = re.search(r'(?:party_id|cust|userId|payerId)["=:]+([A-Z0-9]{8,20})', hermes_resp.text)
            if user_match:
                self.state.user_id = user_match.group(1)
        except Exception as e:
            logger.warning("Loading {} Hermes context failed: {}", self.flow_name, e)
            next_action = os.getenv(
                f"PAYPAL_{self.flow_name}_BILLING_NEXT_ACTION",
                "",
            ).strip()
            if not next_action:
                next_action = self.billing_next_action_fallback

        send_tealeaf_data(self.session, billing_url)
        send_observability_emit(self.session, self.ba_token)

        if not next_action:
            return {
                "status": "error",
                "error": f"missing {self.flow_name} /pay/billing Next-Action id",
                "billing_url": billing_url,
            }

        logger.info("Submitting {} /pay/billing server action...", self.flow_name)
        billing_headers = {
            "Accept": "text/x-component",
            "Origin": "https://www.paypal.com",
            "Referer": self._billing_referer_url(),
            "Next-Action": next_action,
        }
        if self.billing_send_rsc_header:
            billing_headers["RSC"] = "1"
        billing_resp = self.session.post(
            billing_url,
            files=self._us_billing_form_files(),
            headers=billing_headers,
        )
        logger.info(
            "{} /pay/billing result: {} bytes={}",
            self.flow_name,
            billing_resp.status_code,
            len(billing_resp.content),
        )

        return_url = self._extract_return_url_from_text(billing_resp.text)
        if not return_url:
            action_redirect = billing_resp.headers.get("x-action-redirect", "")
            if action_redirect:
                return_url = action_redirect.split(";", 1)[0]
        if not return_url:
            return_url = self._extract_return_url_from_cookies_or_history()
        if not return_url and billing_resp.status_code in (301, 302, 303, 307, 308):
            return_url = billing_resp.headers.get("Location", "")

        final_redirect_url = ""
        if return_url:
            try:
                logger.info("Following {} merchant return URL...", self.flow_name)
                final_redirect_url = self._follow_return_url(return_url, billing_url)
            except Exception as e:
                logger.warning("Following {} merchant return URL failed: {}", self.flow_name, e)

        if return_url or final_redirect_url:
            logger.success("{} billing agreement approved; return URL captured.", self.flow_name)
            return {
                "status": "success",
                "ba_token": self.ba_token,
                "ec_token": self.state.ec_token,
                "user_id": self.state.user_id,
                "return_url": return_url,
                "final_redirect_url": final_redirect_url,
                "payment_action": "BILLING_AGREEMENT",
            }

        return {
            "status": "error",
            "error": f"{self.flow_name} /pay/billing did not expose a return URL",
            "http_status": billing_resp.status_code,
            "raw_response": billing_resp.text[:2000],
        }

    def _us_hermes_url(self) -> str:
        params = [
            ("ssrt", self.state.ssrt),
            ("ul", "1"),
            ("locale.x", self.locale),
            ("country.x", self.country),
            ("ba_token", self.ba_token),
            ("token", self.state.ec_token or self.ba_token),
            ("rcache", "1"),
            ("fromSignupLite", "true"),
            ("addFIContingency", "noretry"),
            ("redirectToHermes", "true"),
            ("fallback", "1"),
            ("reason", self.billing_reason),
        ]
        return "https://www.paypal.com/webapps/hermes?" + urllib.parse.urlencode(params)

    def _us_billing_url(self) -> str:
        params = [
            ("ssrt", self.state.ssrt),
            ("token", self.state.ec_token or self.ba_token),
            ("rcache", "1"),
            ("country.x", self.country),
            ("locale.x", self.locale),
            ("fromSignupLite", "true"),
            ("addFIContingency", "noretry"),
            ("redirectToHermes", "true"),
            ("fallback", "1"),
            ("reason", self.billing_reason),
            ("ul", "1"),
            ("paypal_client_cfci", "modxo_vaulted_not_recurring-Approve_Billing_Agreement"),
        ]
        return "https://www.paypal.com/pay/billing?" + urllib.parse.urlencode(params)

    def _us_hermes_review_url(self) -> str:
        return self._us_hermes_url()

    def _billing_referer_url(self) -> str:
        return self._us_billing_url()

    @staticmethod
    def _extract_billing_url(html: str, fallback: str) -> str:
        match = re.search(r'https://www\.paypal\.com/pay/billing[^"\\\s<]+', html or "")
        if match:
            return match.group(0).replace("\\u0026", "&").replace("\\/", "/")
        match = re.search(r'(/pay/billing\?[^"\\\s<]+)', html or "")
        if match:
            return urllib.parse.urljoin("https://www.paypal.com", match.group(1).replace("\\u0026", "&"))
        return fallback

    @staticmethod
    def _extract_pay_billing_action_id(html: str) -> str:
        for marker in ("select_agree_and_continue", "Approve_Billing_Agreement", "_1_ciTask"):
            idx = (html or "").find(marker)
            if idx < 0:
                continue
            window = html[max(0, idx - 2000): idx + 2000]
            ids = re.findall(r'"([0-9a-f]{32,64})"', window)
            if ids:
                return ids[-1]
        ids = re.findall(r'Next-Action["\']?\s*[:=]\s*["\']([0-9a-f]{32,64})', html or "", re.I)
        return ids[-1] if ids else ""

    @staticmethod
    def _extract_return_url_from_text(text: str) -> str:
        patterns = [
            r'"returnUrl"\s*:\s*"([^"]+)"',
            r'"returnURL"\s*:\s*\{\s*"href"\s*:\s*"([^"]+)"',
            r'(https://pm-redirects\.stripe\.com/return/[^"\\\s<]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, text or "")
            if match:
                return match.group(1).replace("\\u0026", "&").replace("\\/", "/")
        return ""

    def _extract_return_url_from_cookies_or_history(self) -> str:
        return ""

    def _follow_return_url(self, return_url: str, referer: str) -> str:
        current_url = return_url
        resp = self.session.get(
            current_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": referer,
                "Upgrade-Insecure-Requests": "1",
            },
        )
        for _ in range(8):
            if resp.status_code not in (301, 302, 303, 307, 308):
                return str(resp.url)
            location = resp.headers.get("Location", "")
            if not location:
                return str(resp.url)
            current_url = urllib.parse.urljoin(str(resp.url), location)
            resp = self.session.get(
                current_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": str(resp.url),
                    "Upgrade-Insecure-Requests": "1",
                },
            )
        return str(resp.url)

    @staticmethod
    def _us_billing_form_files() -> list[tuple[str, tuple[None, str]]]:
        return [
            ("_1_balancePayload", (None, '{"bal_availabilities":"","bal_available_amounts":"","bal_charged_amounts":"","bal_currencies":"","bal_number":"0","bal_selection_state":"false","bal_selection_states":"","bal_subtypes":"","bal_type":"none","bal_types":"","balance_available":"0","balance_subtype":"BALANCE","balance_used":"0","domestic_balance_checked":"false","foreign_balance_checked":"false","is_balance_checked":"0"}')),
            ("_1_issuerRewardsPayload", (None, '{"rewards_shown":"0"}')),
            ("_1_payPalRewardsPayload", (None, '{"gold_enabled":"N","rewards_available_balance":"none"}')),
            ("_1_splitTenderPayload", (None, '{"split_tender_enabled":false,"split_tender_funding_instruments":"","split_tender_valid":false}')),
            ("_1_isSdkFlow", (None, "false")),
            ("_1_isIframe", (None, "false")),
            ("_1_ciTask", (None, "select_agree_and_continue")),
            ("_1_userAction", (None, "CONTINUE")),
            ("_1_preferredAddressRank", (None, "1")),
            ("_1_threeDsColorDepth", (None, str(SCREEN.get("colorDepth", 24)))),
            ("_1_threeDsIndicator", (None, "LOOKUP")),
            ("_1_threeDsJavaEnabled", (None, "false")),
            ("_1_threeDsScreenHeight", (None, str(SCREEN.get("height", 1152)))),
            ("_1_threeDsScreenWidth", (None, str(SCREEN.get("width", 2048)))),
            ("_1_threeDsTimeZoneOffset", (None, "480")),
            ("_1_threeDsUserAgent", (None, USER_AGENT)),
            ("0", (None, '["$K1"]')),
        ]

