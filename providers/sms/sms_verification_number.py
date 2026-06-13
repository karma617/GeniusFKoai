"""SMS Verification Number provider registration."""
from core.base_sms import SmsVerificationNumberProvider  # noqa: F401
from providers.registry import register_provider

register_provider("sms", "sms_verification_number_api")(SmsVerificationNumberProvider)
