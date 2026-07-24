from __future__ import annotations

from paypal.flow import PayPalFlow
from paypal.ba_flow import PayPalBAFlow
from paypal.gb_flow import PayPalGBFlow
from paypal.id_flow import PayPalIDFlow
from paypal.us_flow import PayPalUSFlow


def normalize_flow_country(country: str | None) -> str:
    value = (country or "BR").strip().upper()
    aliases = {"USA": "US", "UK": "GB", "GBR": "GB", "BRA": "BR", "IDN": "ID"}
    value = aliases.get(value, value)
    if value not in {"BR", "US", "BA", "ID", "GB"}:
        raise ValueError(f"unsupported PayPal flow country: {country!r}")
    return value


def flow_class_for_country(country: str | None):
    country = normalize_flow_country(country)
    if country == "BA":
        return PayPalBAFlow
    if country == "US":
        return PayPalUSFlow
    if country == "ID":
        return PayPalIDFlow
    if country == "GB":
        return PayPalGBFlow
    return PayPalFlow
