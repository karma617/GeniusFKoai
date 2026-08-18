"""Mailbox wrapper that registers with plus-address aliases."""

from __future__ import annotations

import re
import secrets
import string
import threading
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from core.base_mailbox import BaseMailbox, MailboxAccount
from core.db import AccountModel, ProviderResourceModel, TaskEventModel, engine


EMAIL_ALIAS_HARD_LIMIT = 6
EMAIL_ALIAS_SELECT_ATTEMPTS = 16
_EMAIL_RE = re.compile(r"^([^@\s]+)@([^@\s]+\.[^@\s]+)$")
_ALIAS_CHARS = string.ascii_lowercase + string.digits
_ALIAS_LOCK = threading.Lock()
_RESERVED_ALIASES: set[str] = set()
_ALLOCATED_ALIAS_PREFIX = "Email alias allocated:"


@dataclass(frozen=True)
class EmailAliasUsage:
    parent_email: str
    platform: str
    main_success_count: int
    alias_success_count: int
    total_success_count: int


def normalize_email_alias_limit(value: Any) -> int:
    try:
        limit = int(value or EMAIL_ALIAS_HARD_LIMIT)
    except Exception:
        limit = EMAIL_ALIAS_HARD_LIMIT
    return min(max(limit, 1), EMAIL_ALIAS_HARD_LIMIT)


def normalize_email_address(value: Any) -> str:
    return str(value or "").strip().lower()


def _resource_metadata(resource: ProviderResourceModel) -> dict[str, Any]:
    try:
        return dict(resource.get_metadata() or {})
    except Exception:
        return {}


def _metadata_text(metadata: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list, tuple, set)):
            return normalize_email_address(value)
    email_alias = metadata.get("email_alias")
    if isinstance(email_alias, dict):
        for key in keys:
            value = email_alias.get(key)
            if value not in (None, "") and not isinstance(value, (dict, list, tuple, set)):
                return normalize_email_address(value)
    return ""


def get_email_alias_usage(parent_email: str, *, platform: str = "") -> EmailAliasUsage:
    parent = normalize_email_address(parent_email)
    platform_key = str(platform or "").strip()
    main_account_ids: set[int] = set()
    alias_account_ids: set[int] = set()

    if not parent:
        return EmailAliasUsage(parent, platform_key, 0, 0, 0)

    with Session(engine) as session:
        account_statement = select(AccountModel.id).where(AccountModel.email == parent)
        if platform_key:
            account_statement = account_statement.where(AccountModel.platform == platform_key)
        main_account_ids.update(
            int(account_id)
            for account_id in session.exec(account_statement).all()
            if account_id is not None
        )

        resource_rows = session.exec(
            select(ProviderResourceModel).where(ProviderResourceModel.provider_type == "mailbox")
        ).all()
        account_cache: dict[int, AccountModel | None] = {}
        for resource in resource_rows:
            account_id = int(resource.account_id or 0)
            if account_id <= 0:
                continue
            if account_id not in account_cache:
                account_cache[account_id] = session.get(AccountModel, account_id)
            account = account_cache.get(account_id)
            if not account:
                continue
            if platform_key and str(account.platform or "") != platform_key:
                continue

            metadata = _resource_metadata(resource)
            alias_parent = _metadata_text(
                metadata,
                "alias_parent_email",
                "email_alias_parent",
                "parent_email",
                "main_email",
            )
            alias_email = _metadata_text(metadata, "alias_email", "email_alias")
            is_alias = bool(metadata.get("is_email_alias") or metadata.get("email_alias_enabled"))
            if isinstance(metadata.get("email_alias"), dict):
                is_alias = is_alias or bool(metadata["email_alias"].get("enabled"))

            handle = normalize_email_address(resource.handle)
            account_email = normalize_email_address(account.email)

            if is_alias and alias_parent == parent:
                alias_account_ids.add(account_id)
                continue

            if alias_email and alias_parent == parent:
                alias_account_ids.add(account_id)
                continue

            if handle == parent or account_email == parent:
                main_account_ids.add(account_id)

    total_ids = set(main_account_ids) | set(alias_account_ids)
    return EmailAliasUsage(
        parent_email=parent,
        platform=platform_key,
        main_success_count=len(main_account_ids),
        alias_success_count=len(alias_account_ids),
        total_success_count=len(total_ids),
    )


def _existing_account_email(email: str, *, platform: str = "") -> bool:
    target = normalize_email_address(email)
    if not target:
        return False
    with Session(engine) as session:
        statement = select(AccountModel).where(AccountModel.email == target)
        if platform:
            statement = statement.where(AccountModel.platform == platform)
        return session.exec(statement).first() is not None


def _metadata_has_alias(metadata: dict[str, Any], target: str) -> bool:
    if not metadata:
        return False
    if _metadata_text(metadata, "alias_email", "email_alias", "email") == target:
        return True
    nested = metadata.get("email_alias")
    if isinstance(nested, dict):
        return _metadata_text(nested, "alias_email", "email") == target
    return False


def _existing_provider_resource_alias(email: str, *, platform: str = "") -> bool:
    target = normalize_email_address(email)
    if not target:
        return False
    with Session(engine) as session:
        rows = session.exec(
            select(ProviderResourceModel, AccountModel)
            .join(AccountModel, AccountModel.id == ProviderResourceModel.account_id)
            .where(ProviderResourceModel.provider_type == "mailbox")
        ).all()
        for resource, account in rows:
            if platform and str(account.platform or "") != platform:
                continue
            if normalize_email_address(resource.handle) == target:
                return True
            if normalize_email_address(account.email) == target:
                return True
            if _metadata_has_alias(_resource_metadata(resource), target):
                return True
    return False


def _allocated_alias_seen(email: str) -> bool:
    target = normalize_email_address(email)
    if not target:
        return False
    with Session(engine) as session:
        return session.exec(
            select(TaskEventModel.id)
            .where(TaskEventModel.message.like(f"%{_ALLOCATED_ALIAS_PREFIX} {target} %"))
            .limit(1)
        ).first() is not None


def _alias_already_used(email: str, *, platform: str = "") -> bool:
    return (
        _existing_account_email(email, platform=platform)
        or _existing_provider_resource_alias(email, platform=platform)
        or _allocated_alias_seen(email)
    )


def _random_alias(parent_email: str, *, platform: str = "") -> str:
    match = _EMAIL_RE.match(parent_email)
    if not match:
        raise RuntimeError(f"Email alias requires a normal email address: {parent_email}")
    local, domain = match.group(1), match.group(2)
    local_base = local.split("+", 1)[0] or local
    for _ in range(64):
        suffix = "".join(secrets.choice(_ALIAS_CHARS) for _ in range(8))
        alias = f"{local_base}+{suffix}@{domain}".lower()
        with _ALIAS_LOCK:
            if alias in _RESERVED_ALIASES:
                continue
            if _alias_already_used(alias, platform=platform):
                continue
            _RESERVED_ALIASES.add(alias)
            return alias
    raise RuntimeError(f"Unable to allocate a unique email alias for {parent_email}")


def _release_reserved_alias(alias_email: str) -> None:
    alias = normalize_email_address(alias_email)
    if not alias:
        return
    with _ALIAS_LOCK:
        _RESERVED_ALIASES.discard(alias)


def _copy_provider_identity(item: dict[str, Any], *, alias_email: str, parent_email: str, parent_account_id: str, alias_limit: int) -> dict[str, Any]:
    copied = dict(item or {})
    metadata = dict(copied.get("metadata") or {})
    metadata.update(
        {
            "is_email_alias": True,
            "email_alias_enabled": True,
            "alias_email": alias_email,
            "alias_parent_email": parent_email,
            "alias_parent_account_id": parent_account_id,
            "alias_limit": alias_limit,
            "email_alias": {
                "enabled": True,
                "alias_email": alias_email,
                "parent_email": parent_email,
                "parent_account_id": parent_account_id,
                "limit": alias_limit,
            },
        }
    )
    copied["metadata"] = metadata
    copied["login_identifier"] = alias_email if copied.get("login_identifier") is not None else alias_email
    copied["handle"] = alias_email if copied.get("handle") is not None else alias_email
    copied["display_name"] = alias_email
    if copied.get("resource_type") is not None:
        copied["resource_type"] = copied.get("resource_type") or "mailbox"
    if copied.get("resource_identifier") is not None:
        copied["resource_identifier"] = copied.get("resource_identifier") or parent_account_id or parent_email
    return copied


class EmailAliasMailbox(BaseMailbox):
    email_alias_enabled = True

    def __init__(
        self,
        mailbox: BaseMailbox,
        *,
        alias_limit: int = EMAIL_ALIAS_HARD_LIMIT,
        platform: str = "",
        log_fn=None,
    ):
        self.mailbox = mailbox
        self.alias_limit = normalize_email_alias_limit(alias_limit)
        self.platform = str(platform or "").strip()
        self._log_fn = log_fn
        self._parents_by_alias: dict[str, MailboxAccount] = {}
        self._local_parent_success_counts: dict[str, int] = {}
        self._local_alias_success_counts: dict[str, int] = {}
        self._exhausted_parent_emails: set[str] = set()
        self._state_lock = threading.Lock()
        try:
            setattr(self.mailbox, "_email_alias_wrapper_enabled", True)
            setattr(self.mailbox, "_email_alias_exhausted_parents", self._exhausted_parent_emails)
        except Exception:
            pass

    def _log(self, message: str) -> None:
        if not callable(self._log_fn):
            return
        try:
            self._log_fn(message)
        except Exception:
            return

    def _release_parent(self, parent: MailboxAccount | None) -> None:
        if parent is None:
            return
        released = False
        for target in (self.mailbox, self._resolve_wrapped_mailbox(parent)):
            release = getattr(target, "_release_local_account_reservation", None)
            if callable(release):
                try:
                    release(parent)
                    released = True
                except Exception:
                    pass
            release_claim = getattr(target, "_release_active_claim", None)
            if callable(release_claim):
                try:
                    release_claim(parent.email)
                    released = True
                except Exception:
                    pass
        if released:
            return

    def _mark_parent_alias_quota_exhausted(self, parent: MailboxAccount, usage: EmailAliasUsage) -> None:
        marker = getattr(self.mailbox, "mark_registration_success", None)
        if callable(marker):
            try:
                marker(parent)
                self._log(
                    "Email alias parent quota exhausted; marked parent as used: "
                    f"{parent.email} aliases={usage.alias_success_count}/{self.alias_limit} "
                    f"total={usage.total_success_count}"
                )
                return
            except Exception as exc:
                self._log(f"Email alias parent quota mark failed: {parent.email} error={exc}")
        self._release_parent(parent)

    def _resolve_wrapped_mailbox(self, parent: MailboxAccount):
        resolver = getattr(self.mailbox, "_resolve_mailbox", None)
        if callable(resolver):
            try:
                return resolver(parent)
            except Exception:
                return None
        return None

    def _alias_limit_for_parent(self, parent_email: str) -> int:
        limit = self.alias_limit
        resolver = getattr(self.mailbox, "email_alias_limit_for_parent", None)
        if callable(resolver):
            try:
                limit = min(limit, max(int(resolver(parent_email) or 0), 0))
            except Exception:
                limit = 0
        return limit

    def _is_alias_account(self, account: MailboxAccount) -> bool:
        extra = dict(getattr(account, "extra", {}) or {})
        nested = extra.get("email_alias")
        if isinstance(nested, dict) and nested.get("enabled"):
            return True
        resource = dict(extra.get("provider_resource") or {})
        metadata = dict(resource.get("metadata") or {})
        return bool(metadata.get("is_email_alias") or metadata.get("email_alias_enabled"))

    def _uses_parent_account_for_parent(self, parent_email: str) -> bool:
        resolver = getattr(self.mailbox, "email_alias_uses_parent_account_for_parent", None)
        if callable(resolver):
            try:
                return bool(resolver(parent_email))
            except Exception:
                return False
        return False

    def _parent_for(self, account: MailboxAccount) -> MailboxAccount:
        alias = normalize_email_address(getattr(account, "email", ""))
        parent = self._parents_by_alias.get(alias)
        if parent is not None:
            return parent

        extra = dict(getattr(account, "extra", {}) or {})
        resource = dict(extra.get("provider_resource") or {})
        metadata = dict(resource.get("metadata") or {})
        parent_email = _metadata_text(
            metadata,
            "alias_parent_email",
            "email_alias_parent",
            "parent_email",
            "main_email",
        )
        parent_account_id = str(
            metadata.get("alias_parent_account_id")
            or (metadata.get("email_alias") or {}).get("parent_account_id")
            or getattr(account, "account_id", "")
            or ""
        )
        if parent_email:
            parent_extra = dict(extra)
            if resource:
                parent_resource = dict(resource)
                parent_resource["handle"] = parent_email
                parent_resource["display_name"] = parent_email
                parent_resource["resource_identifier"] = parent_account_id or parent_email
                parent_extra["provider_resource"] = parent_resource
            return MailboxAccount(email=parent_email, account_id=parent_account_id or parent_email, extra=parent_extra)
        return account

    def _build_alias_account(
        self,
        parent: MailboxAccount,
        alias_email: str,
        usage: EmailAliasUsage,
        *,
        alias_limit: int | None = None,
    ) -> MailboxAccount:
        parent_email = normalize_email_address(parent.email)
        parent_account_id = str(parent.account_id or parent_email)
        parent_extra = dict(parent.extra or {})
        alias_extra = dict(parent_extra)
        effective_alias_limit = alias_limit or self.alias_limit

        provider_account = dict(parent_extra.get("provider_account") or {})
        if provider_account:
            alias_extra["provider_account"] = _copy_provider_identity(
                provider_account,
                alias_email=alias_email,
                parent_email=parent_email,
                parent_account_id=parent_account_id,
                alias_limit=effective_alias_limit,
            )
            alias_extra["provider_account"]["login_identifier"] = alias_email

        provider_resource = dict(parent_extra.get("provider_resource") or {})
        if provider_resource:
            alias_extra["provider_resource"] = _copy_provider_identity(
                provider_resource,
                alias_email=alias_email,
                parent_email=parent_email,
                parent_account_id=parent_account_id,
                alias_limit=effective_alias_limit,
            )
            alias_extra["provider_resource"]["handle"] = alias_email
            alias_extra["provider_resource"]["resource_identifier"] = parent_account_id

        alias_extra["email_alias"] = {
            "enabled": True,
            "alias_email": alias_email,
            "parent_email": parent_email,
            "parent_account_id": parent_account_id,
            "limit": effective_alias_limit,
            "alias_success_count_at_claim": usage.alias_success_count,
            "total_success_count_at_claim": usage.total_success_count,
        }
        return MailboxAccount(email=alias_email, account_id=parent_account_id, extra=alias_extra)

    def get_email(self) -> MailboxAccount:
        seen_full: set[str] = set()
        last_usage: EmailAliasUsage | None = None
        last_parent = ""
        for _ in range(EMAIL_ALIAS_SELECT_ATTEMPTS):
            try:
                self._log("Email alias selecting parent mailbox...")
                parent = self.mailbox.get_email()
                self._log(f"Email alias parent selected: {normalize_email_address(parent.email)}")
            except Exception as exc:
                self._log(f"Email alias parent selection failed: {exc}")
                if seen_full and last_usage is not None:
                    break
                raise
            parent_email = normalize_email_address(parent.email)
            last_parent = parent_email or last_parent
            with self._state_lock:
                parent_exhausted = bool(parent_email and parent_email in self._exhausted_parent_emails)
            if parent_exhausted:
                self._log(f"Email alias parent already exhausted in current task; skipping: {parent_email}")
                continue
            usage = get_email_alias_usage(parent_email, platform=self.platform)
            local_parent_success_count = self._local_parent_success_counts.get(parent_email, 0)
            local_alias_success_count = self._local_alias_success_counts.get(parent_email, 0)
            parent_alias_limit = self._alias_limit_for_parent(parent_email)
            if parent_alias_limit <= 0:
                self._log(f"Email alias disabled for parent mailbox: {parent_email}")
                return parent
            last_usage = usage
            if self._uses_parent_account_for_parent(parent_email):
                parent_used = usage.main_success_count + local_parent_success_count > 0
                if not parent_used:
                    registered_checker = getattr(self.mailbox, "email_alias_parent_registered", None)
                    if callable(registered_checker):
                        try:
                            parent_used = bool(registered_checker(parent_email))
                        except Exception:
                            parent_used = False
                if not parent_used:
                    self._log(f"Email alias using parent mailbox before child alias: {parent_email}")
                    return parent
                self._log(f"Email alias parent already used; allocating child alias: {parent_email}")
            if (
                usage.alias_success_count + local_alias_success_count >= parent_alias_limit
            ):
                should_mark = False
                with self._state_lock:
                    if parent_email and parent_email not in self._exhausted_parent_emails:
                        self._exhausted_parent_emails.add(parent_email)
                        should_mark = True
                seen_full.add(parent_email)
                if should_mark:
                    self._mark_parent_alias_quota_exhausted(parent, usage)
                continue

            alias_email = _random_alias(parent_email, platform=self.platform)
            alias_account = self._build_alias_account(parent, alias_email, usage, alias_limit=parent_alias_limit)
            self._parents_by_alias[normalize_email_address(alias_email)] = parent
            self._log(
                "Email alias allocated: "
                f"{alias_email} parent={parent_email} "
                f"aliases={usage.alias_success_count}/{parent_alias_limit} "
                f"total={usage.total_success_count}"
            )
            return alias_account

        detail = ""
        if last_usage:
            detail = (
                f" aliases={last_usage.alias_success_count}/{self.alias_limit}"
                f" total={last_usage.total_success_count}"
            )
        raise RuntimeError(f"Email alias quota exhausted for parent mailbox {last_parent or 'unknown'}{detail}")

    def get_current_ids(self, account: MailboxAccount) -> set:
        return self.mailbox.get_current_ids(self._parent_for(account))

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        wait = getattr(self.mailbox, "wait_for_code")
        return wait(
            self._parent_for(account),
            keyword=keyword,
            timeout=timeout,
            before_ids=before_ids,
            code_pattern=code_pattern,
            **kwargs,
        )

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
    ) -> str:
        return self.mailbox.wait_for_link(
            self._parent_for(account),
            keyword=keyword,
            timeout=timeout,
            before_ids=before_ids,
        )

    def delete_account(self, account: MailboxAccount, reason: str = "") -> bool:
        parent = self._parent_for(account)
        self._release_parent(parent)
        _release_reserved_alias(getattr(account, "email", ""))
        return False

    def release_account(self, account: MailboxAccount) -> bool:
        """释放父邮箱和 alias 的临时占用，不修改注册状态。"""
        parent = self._parent_for(account)
        _release_reserved_alias(getattr(account, "email", ""))
        self._release_parent(parent)
        return True

    def mark_invalid_email(self, account: MailboxAccount, reason: str = "") -> list[str]:
        parent = self._parent_for(account)
        if not self._is_alias_account(account):
            marker = getattr(self.mailbox, "mark_invalid_email", None)
            if callable(marker):
                return list(marker(parent, reason=reason) or [])
            self._release_parent(parent)
            return []
        alias_email = getattr(account, "email", "")
        _release_reserved_alias(alias_email)
        marker = getattr(self.mailbox, "mark_invalid_email", None)
        if callable(marker):
            try:
                applied = list(marker(parent, reason=reason) or [])
                if applied:
                    self._log(
                        "Email alias parent marked invalid: "
                        f"alias={alias_email} parent={parent.email} reason={reason}"
                    )
                    return [f"主号邮箱 {parent.email} 已标记无效: {', '.join(applied)}"]
            except Exception as exc:
                self._log(f"Email alias parent invalid mark failed: {parent.email} error={exc}")
        self._release_parent(parent)
        return []

    def mark_registration_success(self, account: MailboxAccount) -> list[str]:
        parent = self._parent_for(account)
        if not self._is_alias_account(account):
            parent_email = normalize_email_address(parent.email)
            parent_alias_limit = self._alias_limit_for_parent(parent_email)
            if parent_alias_limit > 0 and self._uses_parent_account_for_parent(parent_email):
                self._local_parent_success_counts[parent_email] = self._local_parent_success_counts.get(parent_email, 0) + 1
                self._log(
                    "Email alias parent registration success; keep parent available for child alias: "
                    f"{parent_email} child_limit={parent_alias_limit}"
                )
                self._release_parent(parent)
                return []
            marker = getattr(self.mailbox, "mark_registration_success", None)
            if callable(marker):
                return list(marker(parent) or [])
            self._release_parent(parent)
            return []
        parent_email = normalize_email_address(parent.email)
        alias_email = normalize_email_address(getattr(account, "email", ""))
        if parent_email and alias_email and not _existing_account_email(alias_email, platform=self.platform):
            self._local_alias_success_counts[parent_email] = self._local_alias_success_counts.get(parent_email, 0) + 1
        usage = get_email_alias_usage(parent.email, platform=self.platform)
        local_alias_success_count = self._local_alias_success_counts.get(parent_email, 0)
        effective_alias_count = usage.alias_success_count + local_alias_success_count
        effective_total_count = usage.total_success_count + local_alias_success_count
        parent_alias_limit = self._alias_limit_for_parent(parent_email)
        _release_reserved_alias(getattr(account, "email", ""))
        self._log(
            "Email alias registration success: "
            f"{getattr(account, 'email', '')} parent={parent.email} "
            f"aliases={effective_alias_count}/{parent_alias_limit or self.alias_limit} "
            f"total={effective_total_count}"
        )
        marker = getattr(self.mailbox, "mark_registration_success", None)
        if callable(marker) and parent_alias_limit > 0 and effective_alias_count >= parent_alias_limit:
            return list(marker(parent) or [])
        self._release_parent(parent)
        return []

    def mark_parent_exhausted(self, account: MailboxAccount, reason: str = "") -> list[str]:
        """Force-mark the parent email as registered/exhausted.

        Called when OpenAI rejects the registration - the parent's alias
        quota is burned on OpenAI's side (which may be ahead of our local DB
        count). This bypasses the usage-count gate in mark_registration_success
        and directly tags the parent mailbox so _select_account skips it on
        the next get_email() call.
        """
        parent = self._parent_for(account)
        parent_email = normalize_email_address(parent.email)
        if parent_email:
            self._exhausted_parent_emails.add(parent_email)
        _release_reserved_alias(getattr(account, "email", ""))
        marker_reason = reason or "user_already_exists"
        self._log(
            f"Email alias parent exhausted ({marker_reason}): "
            f"alias={getattr(account, 'email', '')} parent={parent.email}"
        )
        alias_exhausted_marker = getattr(self.mailbox, "mark_alias_exhausted", None)
        if callable(alias_exhausted_marker):
            try:
                applied = list(alias_exhausted_marker(parent, reason=marker_reason) or [])
                if applied:
                    return applied
            except Exception as exc:
                self._log(f"Email alias parent exhausted mark failed: {parent.email} error={exc}")
        marker = getattr(self.mailbox, "mark_registration_success", None)
        if callable(marker):
            try:
                return list(marker(parent) or [])
            except Exception:
                pass
        self._release_parent(parent)
        return []

    def mark_plus_success(self, account: MailboxAccount) -> list[str]:
        parent = self._parent_for(account)
        marker = getattr(self.mailbox, "mark_plus_success", None)
        if callable(marker):
            return list(marker(parent) or [])
        self._release_parent(parent)
        return []
