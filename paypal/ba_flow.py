from __future__ import annotations

import urllib.parse

from paypal.us_flow import PayPalUSFlow


class PayPalBAFlow(PayPalUSFlow):
    """Bosnia and Herzegovina flow derived from the captured BA PayPal chain."""

    country = "BA"
    lang = "en"
    locale = "en_BA"
    flow_name = "BA"
    checkout_channel = "MOBILE"
    direct_signup_from_initial_ec = True
    require_ec_signup_context = True
    phone_calling_code = "387"
    billing_reason = "Q0FSRF9HRU5FUklDX0VSUk9S"
    billing_next_action_fallback = "7fd4b0dd65f4e1bc4b2d14d4a0b4e1a2f9b2507fe6"
    billing_send_rsc_header = False
    send_installment_options = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        accept_language = "en-BA,en;q=0.9"
        self.session._base_client_kwargs["headers"]["Accept-Language"] = accept_language
        self.session.client.headers["Accept-Language"] = accept_language

    def _build_signup_variables(self, token: str) -> dict:
        variables = super()._build_signup_variables(token)
        variables["dateOfBirth"] = self._dob_payload()
        variables["nationality"] = self.country

        variables["billingAddress"]["line1"] = (
            f"{self.address.street},{self.address.city},"
            f"{self.address.postal_code},Bosnia and Herzegovina"
        )

        shipping_address = variables["shippingAddress"]
        shipping_address.pop("postalCode", None)
        shipping_address.pop("state", None)
        return variables

    def _signup_extra_body(self, token: str) -> dict | None:
        return None

    def _phase3_signup_and_2fa(self):
        parts = self.state.content_identifier.split(":")
        if len(parts) < 4 or not parts[2]:
            raise RuntimeError("BA signup content identifier is missing the live content hash")
        return super()._phase3_signup_and_2fa()

    def _us_hermes_url(self) -> str:
        params = [
            ("ba_token", self.ba_token),
            ("ssrt", self.state.ssrt),
            ("token", self.state.ec_token),
            ("rcache", "1"),
            ("country.x", self.country),
            ("locale.x", self.locale),
            ("fromSignupLite", "true"),
            ("addFIContingency", "noretry"),
            ("redirectToHermes", "true"),
            ("fallback", "1"),
            ("reason", self.billing_reason),
        ]
        return "https://www.paypal.com/webapps/hermes?" + urllib.parse.urlencode(params)

    def _us_hermes_review_url(self) -> str:
        params = [
            ("ba_token", self.ba_token),
            ("ssrt", self.state.ssrt),
            ("token", self.state.ec_token),
            ("rcache", "1"),
            ("country.x", self.country),
            ("locale.x", self.locale),
            ("fromSignupLite", "true"),
            ("fallback", "1"),
            ("reason", self.billing_reason),
        ]
        return "https://www.paypal.com/webapps/hermes?" + urllib.parse.urlencode(params)

    def _billing_referer_url(self) -> str:
        params = [
            ("ssrt", self.state.ssrt),
            ("token", self.ba_token),
            ("rcache", "1"),
            ("country.x", self.country),
            ("locale.x", self.locale),
            ("fromSignupLite", "true"),
            ("fallback", "1"),
            ("reason", self.billing_reason),
            ("ul", "1"),
        ]
        return "https://www.paypal.com/pay/billing?" + urllib.parse.urlencode(params)

    def _us_billing_url(self) -> str:
        return self._billing_referer_url() + (
            "&paypal_client_cfci="
            "modxo_vaulted_not_recurring-Approve_Billing_Agreement"
        )

    @staticmethod
    def _us_billing_form_files() -> list[tuple[str, tuple[None, str]]]:
        replacements = {
            "_1_threeDsScreenHeight": "594",
            "_1_threeDsScreenWidth": "384",
            "_1_threeDsTimeZoneOffset": "-480",
        }
        return [
            (name, (None, replacements.get(name, value)))
            for name, (_, value) in PayPalUSFlow._us_billing_form_files()
        ]
