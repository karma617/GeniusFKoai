from __future__ import annotations

from core.proxy_pool import DEFAULT_FALLBACK_PROXY_URL, PROXY_STRATEGY_POOL_THEN_DEFAULT
from core.config_store import config_store
from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository


class ConfigRepository:
    BASE_KEYS = {
        "default_executor",
        "default_identity_provider", "default_oauth_provider", "oauth_email_hint",
        "chrome_user_data_dir", "chrome_cdp_url",
        "cpa_api_url", "cpa_api_key",
        "team_manager_url", "team_manager_key",
        "any2api_url", "any2api_password",
        "sub2api_url", "sub2api_email", "sub2api_password",
        "sub2api_group_name", "sub2api_account_priority", "sub2api_default_proxy_name",
        "lifecycle_account_check_enabled",
        "lifecycle_token_refresh_enabled", "lifecycle_trial_warning_enabled",
        "lifecycle_external_sync_enabled",
        "proxy_strategy", "proxy_fallback_url",
    }
    DEFAULT_VALUES = {
        "lifecycle_account_check_enabled": "true",
        "lifecycle_token_refresh_enabled": "true",
        "lifecycle_trial_warning_enabled": "true",
        "lifecycle_external_sync_enabled": "false",
        "proxy_strategy": PROXY_STRATEGY_POOL_THEN_DEFAULT,
        "proxy_fallback_url": DEFAULT_FALLBACK_PROXY_URL,
    }

    def __init__(self, definitions: ProviderDefinitionsRepository | None = None):
        self.definitions = definitions or ProviderDefinitionsRepository()

    def get_allowed_keys(self) -> set[str]:
        keys = set(self.BASE_KEYS)
        for provider_type in ("mailbox", "captcha", "sms"):
            for definition in self.definitions.list_by_type(provider_type, enabled_only=False):
                for field in definition.get_fields():
                    field_key = str(field.get("key") or "").strip()
                    if field_key:
                        keys.add(field_key)
        return keys

    def get_flat(self) -> dict[str, str]:
        data = config_store.get_all()
        allowed = self.get_allowed_keys()
        result = {
            key: str(value or "")
            for key, value in data.items()
            if key in allowed
        }
        for key, value in self.DEFAULT_VALUES.items():
            if key in allowed and not result.get(key):
                result[key] = value
        return result

    def update_flat(self, data: dict[str, str]) -> list[str]:
        allowed = self.get_allowed_keys()
        safe = {key: value for key, value in data.items() if key in allowed}
        config_store.set_many(safe)
        return list(safe.keys())
