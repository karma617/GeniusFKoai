"""NexSMS provider registration."""
from core.base_sms import NexSmsProvider  # noqa: F401
from providers.registry import register_provider

register_provider("sms", "nexsms_api")(NexSmsProvider)
