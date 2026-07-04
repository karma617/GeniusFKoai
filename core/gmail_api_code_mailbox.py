"""Gmail API code mailbox provider backed by per-email fetch URLs."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests
from sqlmodel import Session, select

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link
from core.db import AccountModel, ProviderResourceModel, engine


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


@dataclass(frozen=True)
class GmailApiCodeEntry:
    email: str
    code_url: str


def parse_gmail_api_code_entries(value: Any) -> list[GmailApiCodeEntry]:
    entries: list[GmailApiCodeEntry] = []
    seen: set[str] = set()
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "----" not in line:
            continue
        email_part, url_part = line.split("----", 1)
        email = _normalize_email(email_part)
        code_url = url_part.strip()
        if not email or "@" not in email or not code_url.lower().startswith(("http://", "https://")):
            continue
        if email in seen:
            continue
        seen.add(email)
        entries.append(GmailApiCodeEntry(email=email, code_url=code_url))
    return entries


class GmailApiCodeMailbox(BaseMailbox):
    """Allocate fixed Gmail addresses and fetch OTP codes from their API URLs."""

    _CLAIM_LOCK = threading.Lock()
    _ACTIVE_CLAIMS: dict[str, float] = {}
    _INVALID_EMAILS: set[str] = set()
    _CLAIM_TTL_SECONDS = 60 * 60

    def __init__(self, *, pool_text: str = "", poll_interval: str = "", proxy: str | None = None):
        self.entries = parse_gmail_api_code_entries(pool_text)
        self.poll_interval = max(1, self._safe_int(poll_interval, 3))
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        if not self.entries:
            raise RuntimeError("Gmail API接码未配置有效邮箱，格式：邮箱----接码链接")

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return default

    @classmethod
    def _prune_active_claims_locked(cls) -> None:
        now = time.time()
        expired = [email for email, claimed_at in cls._ACTIVE_CLAIMS.items() if now - claimed_at > cls._CLAIM_TTL_SECONDS]
        for email in expired:
            cls._ACTIVE_CLAIMS.pop(email, None)

    @classmethod
    def _release_active_claim(cls, email: str) -> None:
        target = _normalize_email(email)
        if not target:
            return
        with cls._CLAIM_LOCK:
            cls._ACTIVE_CLAIMS.pop(target, None)

    @staticmethod
    def _account_exists(email: str) -> bool:
        target = _normalize_email(email)
        if not target:
            return False
        with Session(engine) as session:
            return session.exec(select(AccountModel).where(AccountModel.email == target)).first() is not None

    @staticmethod
    def _unavailable_resource_exists(email: str) -> bool:
        target = _normalize_email(email)
        if not target:
            return False
        with Session(engine) as session:
            rows = session.exec(
                select(ProviderResourceModel)
                .where(ProviderResourceModel.provider_type == "mailbox")
                .where(ProviderResourceModel.provider_name == "gmail_api_code")
            ).all()
            for row in rows:
                metadata = row.get_metadata()
                row_email = _normalize_email(row.handle or metadata.get("email"))
                status = str(metadata.get("registration_status") or "").strip().lower()
                if row_email == target and (
                    status in {"registered", "invalid"}
                    or metadata.get("registration_success")
                    or metadata.get("registration_invalid")
                ):
                    return True
        return False

    def _entry_for_account(self, account: MailboxAccount) -> GmailApiCodeEntry:
        target = _normalize_email(account.email)
        for entry in self.entries:
            if entry.email == target:
                return entry
        metadata = ((account.extra or {}).get("provider_resource") or {}).get("metadata", {})
        code_url = str(metadata.get("code_url") or "").strip()
        if target and code_url:
            return GmailApiCodeEntry(email=target, code_url=code_url)
        raise RuntimeError(f"Gmail API接码未找到邮箱配置: {account.email}")

    def peek_email(self) -> str:
        return self.entries[0].email

    def get_email(self) -> MailboxAccount:
        with self._CLAIM_LOCK:
            self._prune_active_claims_locked()
            unavailable_count = 0
            for entry in self.entries:
                if entry.email in self._INVALID_EMAILS or self._unavailable_resource_exists(entry.email):
                    unavailable_count += 1
                    continue
                if entry.email in self._ACTIVE_CLAIMS:
                    continue
                if self._account_exists(entry.email):
                    continue
                self._ACTIVE_CLAIMS[entry.email] = time.time()
                return MailboxAccount(
                    email=entry.email,
                    account_id=entry.email,
                    extra={
                        "provider_resource": {
                            "provider_type": "mailbox",
                            "provider_name": "gmail_api_code",
                            "resource_type": "mailbox",
                            "resource_identifier": entry.email,
                            "handle": entry.email,
                            "display_name": entry.email,
                            "metadata": {
                                "email": entry.email,
                                "code_url": entry.code_url,
                                "gmail_api_code_claimed": True,
                                "gmail_api_code_claimed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                            },
                        },
                    },
                )
        if unavailable_count >= len(self.entries):
            raise RuntimeError("Gmail API接码邮箱池已无可用邮箱：所有邮箱都已注册或标记无效")
        raise RuntimeError("Gmail API接码邮箱池暂未找到可用邮箱")

    @staticmethod
    def _flatten_json(value: Any) -> str:
        if isinstance(value, dict):
            return " ".join(GmailApiCodeMailbox._flatten_json(v) for v in value.values())
        if isinstance(value, list):
            return " ".join(GmailApiCodeMailbox._flatten_json(v) for v in value)
        return str(value or "")

    def _fetch_text(self, entry: GmailApiCodeEntry) -> str:
        response = requests.get(entry.code_url, proxies=self.proxy, timeout=15)
        response.raise_for_status()
        text = response.text or ""
        try:
            return self._flatten_json(json.loads(text))
        except Exception:
            return text

    @staticmethod
    def _extract_code(text: str, code_pattern: str | None = None) -> str:
        pattern = re.compile(code_pattern) if code_pattern else re.compile(r"(?<!#)(?<!\d)(\d{6})(?!\d)")
        match = pattern.search(str(text or ""))
        if not match:
            return ""
        return match.group(1) if match.groups() else match.group(0)

    @classmethod
    def _current_id(cls, text: str, code_pattern: str | None = None) -> str:
        code = cls._extract_code(text, code_pattern=code_pattern)
        if code:
            return f"code:{code}"
        digest = hashlib.sha1(str(text or "").encode("utf-8", "ignore")).hexdigest()
        return f"body:{digest}"

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            text = self._fetch_text(self._entry_for_account(account))
            return {self._current_id(text)}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
    ) -> str:
        entry = self._entry_for_account(account)
        seen = set(before_ids or [])
        start = time.time()
        last_error = ""
        while time.time() - start < timeout:
            try:
                text = self._fetch_text(entry)
                current_id = self._current_id(text, code_pattern=code_pattern)
                code = self._extract_code(text, code_pattern=code_pattern)
                if code and current_id not in seen:
                    return code
                seen.add(current_id)
            except Exception as exc:
                last_error = str(exc)
            time.sleep(self.poll_interval)
        suffix = f": {last_error}" if last_error else ""
        raise TimeoutError(f"等待 Gmail API接码验证码超时 ({timeout}s){suffix}")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "", timeout: int = 120, before_ids: set = None) -> str:
        entry = self._entry_for_account(account)
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            text = self._fetch_text(entry)
            current_id = self._current_id(text)
            if current_id not in seen:
                link = _extract_verification_link(text, keyword)
                if link:
                    return link
                seen.add(current_id)
            time.sleep(self.poll_interval)
        raise TimeoutError(f"等待 Gmail API接码验证链接超时 ({timeout}s)")

    def mark_registration_success(self, account: MailboxAccount) -> list[str]:
        email = _normalize_email(getattr(account, "email", ""))
        self._release_active_claim(email)
        if not email:
            return []
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        updated = False
        with Session(engine) as session:
            rows = session.exec(
                select(ProviderResourceModel)
                .where(ProviderResourceModel.provider_type == "mailbox")
                .where(ProviderResourceModel.provider_name == "gmail_api_code")
            ).all()
            for row in rows:
                metadata = row.get_metadata()
                row_email = _normalize_email(row.handle or metadata.get("email"))
                if row_email != email:
                    continue
                metadata.update(
                    {
                        "email": email,
                        "registration_status": "registered",
                        "registration_success": True,
                        "registration_success_at": now,
                    }
                )
                row.set_metadata(metadata)
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
                updated = True
            if updated:
                session.commit()
        return ["Gmail API接码邮箱已注册"] if updated or self._account_exists(email) else []

    def mark_invalid_email(self, account: MailboxAccount, reason: str = "") -> list[str]:
        email = _normalize_email(getattr(account, "email", ""))
        if not email:
            return []
        with self._CLAIM_LOCK:
            self._ACTIVE_CLAIMS.pop(email, None)
            self._INVALID_EMAILS.add(email)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        updated = False
        with Session(engine) as session:
            rows = session.exec(
                select(ProviderResourceModel)
                .where(ProviderResourceModel.provider_type == "mailbox")
                .where(ProviderResourceModel.provider_name == "gmail_api_code")
            ).all()
            for row in rows:
                metadata = row.get_metadata()
                row_email = _normalize_email(row.handle or metadata.get("email"))
                if row_email != email:
                    continue
                metadata.update(
                    {
                        "email": email,
                        "registration_status": "invalid",
                        "registration_invalid": True,
                        "registration_invalid_reason": reason or "invalid_email_no_otp",
                        "registration_invalid_at": now,
                    }
                )
                row.set_metadata(metadata)
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
                updated = True
            if updated:
                session.commit()
        return ["Gmail API接码邮箱已标记无效"]
