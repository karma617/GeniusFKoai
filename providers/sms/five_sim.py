"""5sim provider registration."""
from core.base_sms import FiveSimProvider  # noqa: F401
from providers.registry import register_provider

register_provider("sms", "five_sim_api")(FiveSimProvider)
