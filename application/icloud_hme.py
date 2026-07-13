from __future__ import annotations

from typing import Any

from core.icloud_hme import (
    ICloudHMEAccount,
    ICloudHMEClient,
    _derive_icloud_email,
    _utcnow_iso,
    normalize_icloud_host,
    parse_icloud_accounts_json,
    parse_icloud_cookie_input,
    serialize_icloud_accounts_json,
)
from infrastructure.provider_settings_repository import ProviderSettingsRepository


class ICloudHMEService:
    provider_type = "mailbox"
    provider_key = "icloud_hme"

    def __init__(self, repository: ProviderSettingsRepository | None = None):
        self.repository = repository or ProviderSettingsRepository()

    def _setting(self):
        item = self.repository.get_by_key(self.provider_type, self.provider_key)
        if item:
            return item
        return self.repository.save(
            setting_id=None,
            provider_type=self.provider_type,
            provider_key=self.provider_key,
            display_name="iCloud 隐私邮箱（HME）",
            auth_mode="cookie",
            enabled=True,
            is_default=False,
            config={},
            auth={"icloud_hme_accounts_json": serialize_icloud_accounts_json([])},
            metadata={},
        )

    def _load_accounts(self) -> tuple[Any, list[ICloudHMEAccount]]:
        setting = self._setting()
        auth = setting.get_auth()
        return setting, parse_icloud_accounts_json(auth.get("icloud_hme_accounts_json") or "")

    def _save_accounts(self, setting, accounts: list[ICloudHMEAccount]) -> None:
        auth = dict(setting.get_auth())
        auth["icloud_hme_accounts_json"] = serialize_icloud_accounts_json(accounts)
        self.repository.save(
            setting_id=int(setting.id or 0),
            provider_type=setting.provider_type,
            provider_key=setting.provider_key,
            display_name=setting.display_name,
            auth_mode=setting.auth_mode,
            enabled=bool(setting.enabled),
            is_default=bool(setting.is_default),
            config=setting.get_config(),
            auth=auth,
            metadata=setting.get_metadata(),
        )

    @staticmethod
    def _public_accounts(accounts: list[ICloudHMEAccount]) -> list[dict[str, Any]]:
        return [account.public_dict() for account in accounts]

    def list_accounts(self) -> dict[str, Any]:
        _setting, accounts = self._load_accounts()
        return {"ok": True, "accounts": self._public_accounts(accounts)}

    def upsert_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        setting, accounts = self._load_accounts()
        account_id = str(payload.get("id") or "").strip()
        existing = next((item for item in accounts if item.id == account_id), None)
        cookies = parse_icloud_cookie_input(payload.get("cookies") or payload.get("cookie_header") or "")
        if existing and not cookies:
            cookies = dict(existing.cookies)
        account = ICloudHMEAccount(
            id=account_id or "acc_" + __import__("uuid").uuid4().hex[:8],
            name=str(payload.get("name") or (existing.name if existing else "") or ""),
            real_email=str(payload.get("real_email") or (existing.real_email if existing else "") or ""),
            icloud_email=str(payload.get("icloud_email") or (existing.icloud_email if existing else "") or ""),
            cookies=cookies,
            host=normalize_icloud_host(str(payload.get("host") or (existing.host if existing else "") or "icloud.com")),
            proxy=str(payload.get("proxy") or (existing.proxy if existing else "") or ""),
            app_password=str(payload.get("app_password") or (existing.app_password if existing else "") or ""),
            status=existing.status if existing else "pending",
            alias_total=existing.alias_total if existing else 0,
            alias_active=existing.alias_active if existing else 0,
            last_validated=existing.last_validated if existing else "",
            last_error=existing.last_error if existing else "",
            created_at=existing.created_at if existing else _utcnow_iso(),
        )
        validation_requested = bool(cookies and bool(payload.get("validate", True)))
        if validation_requested:
            self._validate_account(account)
        if existing:
            accounts = [account if item.id == existing.id else item for item in accounts]
        else:
            accounts.append(account)
        self._save_accounts(setting, accounts)
        ok = not validation_requested or account.status == "active"
        return {
            "ok": ok,
            "account": account.public_dict(),
            "accounts": self._public_accounts(accounts),
            "error": account.last_error if not ok else "",
        }

    def delete_account(self, account_id: str) -> dict[str, Any]:
        setting, accounts = self._load_accounts()
        before = len(accounts)
        accounts = [item for item in accounts if item.id != account_id]
        if len(accounts) == before:
            return {"ok": False, "error": "iCloud 账号不存在"}
        self._save_accounts(setting, accounts)
        return {"ok": True, "id": account_id, "accounts": self._public_accounts(accounts)}

    def validate_account(self, account_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        setting, accounts = self._load_accounts()
        account = self._require_account(accounts, account_id)
        if payload:
            cookies = parse_icloud_cookie_input(payload.get("cookies") or payload.get("cookie_header") or "")
            if cookies:
                account.cookies = cookies
            if "host" in payload:
                account.host = normalize_icloud_host(str(payload.get("host") or account.host or "icloud.com"))
            if "proxy" in payload:
                account.proxy = str(payload.get("proxy") or "")
            if payload.get("app_password"):
                account.app_password = str(payload.get("app_password") or "")
            if "real_email" in payload:
                account.real_email = str(payload.get("real_email") or account.real_email or "")
            if "icloud_email" in payload:
                account.icloud_email = str(payload.get("icloud_email") or account.icloud_email or "")
        self._validate_account(account)
        self._save_accounts(setting, accounts)
        return {"ok": account.status == "active", "account": account.public_dict(), "error": account.last_error}

    def _validate_account(self, account: ICloudHMEAccount) -> None:
        if not account.cookies:
            account.status = "pending"
            account.last_error = "未配置 Cookie"
            return
        try:
            client = ICloudHMEClient(account.cookies, host=account.host, proxy=account.proxy)
            info = client.validate_session()
            aliases = client.list_aliases()
            account.cookies = client.cookies
            account.real_email = info.get("apple_id") or info.get("primary_email") or account.real_email
            if not account.icloud_email:
                account.icloud_email = _derive_icloud_email(account.real_email, info.get("primary_email") or "")
            account.alias_total = len(aliases)
            account.alias_active = sum(1 for item in aliases if item.get("active"))
            account.status = "active"
            account.last_error = ""
            account.last_validated = _utcnow_iso()
        except Exception as exc:
            account.status = "error"
            account.last_error = str(exc)[:300]

    @staticmethod
    def _require_account(accounts: list[ICloudHMEAccount], account_id: str) -> ICloudHMEAccount:
        for account in accounts:
            if account.id == account_id:
                return account
        raise ValueError("iCloud 账号不存在")

    def _client_for(self, account: ICloudHMEAccount) -> ICloudHMEClient:
        if not account.cookies:
            raise ValueError("账号未配置 Cookie")
        return ICloudHMEClient(account.cookies, host=account.host, proxy=account.proxy)

    def list_aliases(self, account_id: str) -> dict[str, Any]:
        setting, accounts = self._load_accounts()
        account = self._require_account(accounts, account_id)
        client = self._client_for(account)
        aliases = client.list_aliases()
        account.cookies = client.cookies
        account.alias_total = len(aliases)
        account.alias_active = sum(1 for item in aliases if item.get("active"))
        account.status = "active"
        account.last_error = ""
        account.last_validated = _utcnow_iso()
        self._save_accounts(setting, accounts)
        return {"ok": True, "account_id": account_id, "count": len(aliases), "aliases": aliases}

    def create_alias(self, account_id: str, label: str = "") -> dict[str, Any]:
        setting, accounts = self._load_accounts()
        account = self._require_account(accounts, account_id)
        client = self._client_for(account)
        result = client.create_alias(label)
        aliases = client.list_aliases()
        account.cookies = client.cookies
        account.alias_total = len(aliases)
        account.alias_active = sum(1 for item in aliases if item.get("active"))
        account.status = "active"
        account.last_error = ""
        account.last_validated = _utcnow_iso()
        self._save_accounts(setting, accounts)
        return {"ok": True, "alias": result, "account": account.public_dict(), "aliases": aliases}

    def alias_action(self, account_id: str, anonymous_id: str, action: str) -> dict[str, Any]:
        setting, accounts = self._load_accounts()
        account = self._require_account(accounts, account_id)
        client = self._client_for(account)
        if action == "deactivate":
            success = client.deactivate_alias(anonymous_id)
        elif action == "reactivate":
            success = client.reactivate_alias(anonymous_id)
        elif action == "delete":
            success = client.delete_alias(anonymous_id)
        else:
            raise ValueError(f"不支持的别名操作: {action}")
        account.cookies = client.cookies
        try:
            aliases = client.list_aliases()
            account.alias_total = len(aliases)
            account.alias_active = sum(1 for item in aliases if item.get("active"))
        except Exception:
            aliases = []
        self._save_accounts(setting, accounts)
        return {"ok": bool(success), "anonymous_id": anonymous_id, "aliases": aliases}
