from __future__ import annotations

from typing import Any

from loguru import logger

from paypal.us_flow import PayPalUSFlow


class PayPalGBFlow(PayPalUSFlow):
    """United Kingdom Billing Agreement flow from pay.openai.com-GB.har.

    Shape matches the US guest-signup + /pay/billing chain, with GB locale,
    +44 phone, and UK signup fields observed in the HAR:
      - dateOfBirth / nationality / residentialAddress
      - crsData.taxDetails countryCode=GB
      - Phase4 /pay/billing country.x=GB locale.x=en_GB reason=Ul9FUlJPUg==
    """

    country = "GB"
    lang = "en"
    locale = "en_GB"
    flow_name = "GB"
    phone_calling_code = "44"
    billing_reason = "Ul9FUlJPUg=="
    # Captured Next-Action from GB HAR /pay/billing POST.
    billing_next_action_fallback = "7fd4b0dd65f4e1bc4b2d14d4a0b4e1a2f9b2507fe6"
    billing_send_rsc_header = True
    send_installment_options = True
    direct_signup_from_initial_ec = True
    require_ec_signup_context = True

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        accept_language = "en-GB,en;q=0.9"
        self.session._base_client_kwargs["headers"]["Accept-Language"] = accept_language
        self.session.client.headers["Accept-Language"] = accept_language

    def _update_user_phone(self, phone: str):
        raw = (phone or "").strip()
        if raw.lower().startswith("phone:"):
            raw = raw.split(":", 1)[1].strip()

        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) < 8:
            raise ValueError("phone number is too short")

        if digits.startswith(self.phone_calling_code) and len(digits) > len(self.phone_calling_code) + 6:
            local = digits[len(self.phone_calling_code):]
        else:
            local = digits
        # UK locals sometimes include leading 0 trunk prefix.
        if local.startswith("0"):
            local = local[1:]
        if len(local) < 8:
            raise ValueError("local phone number is too short")

        self.user.phone = f"+{self.phone_calling_code}{local}"
        self.user.phone_country_code = f"+{self.phone_calling_code}"
        self.user.phone_local = local
        logger.info("Phone updated for OTP retry: {}", self._masked_phone())

    def _build_signup_variables(self, token: str) -> dict:
        variables = super()._build_signup_variables(token)

        # GB HAR formats line1 as a single full UK address string.
        full_line1 = (
            f"{self.address.street}, {self.address.city}, "
            f"{self.address.postal_code}, United Kingdom"
        )
        variables["billingAddress"]["line1"] = full_line1
        # Keep city/state/postal as generated GB materials.
        variables["billingAddress"]["city"] = self.address.city
        variables["billingAddress"]["state"] = self.address.state
        variables["billingAddress"]["postalCode"] = self.address.postal_code

        # shippingAddress in HAR omits state and uses empty line/city/postal.
        shipping = variables["shippingAddress"]
        shipping.pop("state", None)
        shipping["line1"] = ""
        shipping["city"] = ""
        shipping["postalCode"] = ""

        variables["residentialAddress"] = {
            "line1": full_line1,
            "city": self.address.city,
            "state": self.address.state,
            "postalCode": self.address.postal_code,
            "accountQuality": {
                "autoCompleteType": "MANUAL",
                "isUserModified": True,
            },
            "country": self.country,
            "familyName": self.user.last_name,
            "givenName": self.user.first_name,
        }
        variables["dateOfBirth"] = self._dob_payload()
        variables["nationality"] = self.country
        variables["crsData"] = {
            "firstName": self.user.first_name,
            "lastName": self.user.last_name,
            "subjectToTaxOutsideLegalCountry": False,
            "taxDetails": [{"countryCode": self.country}],
        }
        return variables
