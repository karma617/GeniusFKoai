from __future__ import annotations

from typing import Any

from loguru import logger

from paypal.us_flow import PayPalUSFlow


class PayPalIDFlow(PayPalUSFlow):
    """Indonesia checkout signup flow.

    The captured Indonesia HAR reaches the risk-based phone submission step:
    InitiateRiskBasedTwoFactorPhoneConfirmationMutation with
    locale={country: "ID", lang: "id"}, phoneCountry="ID", and an Indonesia
    local phone number.  Everything after that boundary intentionally reuses
    the existing US-region flow implementation.
    """

    country = "ID"
    lang = "id"
    locale = "id_ID"
    flow_name = "ID"
    checkout_channel = "MOBILE"
    phone_calling_code = "62"
    direct_signup_from_initial_ec = True
    require_ec_signup_context = True

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        accept_language = "id-ID,id;q=0.9,en;q=0.8"
        self.session._base_client_kwargs["headers"]["Accept-Language"] = accept_language
        self.session.client.headers["Accept-Language"] = accept_language

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
        if local.startswith("0"):
            local = local[1:]
        if len(local) < 8:
            raise ValueError("local phone number is too short")

        self.user.phone = f"+{self.phone_calling_code}{local}"
        self.user.phone_country_code = f"+{self.phone_calling_code}"
        self.user.phone_local = local
        logger.info("Phone updated for OTP retry: {}", self._masked_phone())
