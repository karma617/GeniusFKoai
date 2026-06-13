"""SMSPool provider registration."""
from core.base_sms import SmsPoolProvider  # noqa: F401
from providers.registry import register_provider

register_provider("sms", "smspool_api")(SmsPoolProvider)
