"""Codex 本地接码池 provider 注册。"""
from core.base_sms import CodexSmsPoolProvider  # noqa: F401
from providers.registry import register_provider

register_provider("sms", "codex_sms_pool")(CodexSmsPoolProvider)
