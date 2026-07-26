"""SmsBowerMailMailbox — register into unified registry."""
from core.smsbower_mail_mailbox import SmsBowerMailMailbox  # noqa: F401
from providers.registry import register_provider

register_provider("mailbox", "smsbower_mail_api")(SmsBowerMailMailbox)
