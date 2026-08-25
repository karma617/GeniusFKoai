"""API code mailbox provider backed by per-email fetch URLs."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import requests
from sqlmodel import Session, select

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link
from core.db import AccountModel, ProviderResourceModel, ProviderSettingModel, engine


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


@dataclass(frozen=True)
class GmailApiCodeEntry:
    email: str
    code_url: str


@dataclass(frozen=True)
class GmailApiCodePoolRow:
    email: str
    code_url: str
    status: str = "active"


class GmailApiCodeMailboxUnavailable(RuntimeError):
    """接码 API 明确返回邮箱不可用状态。"""


_POOL_STATUS_PREFIX_RE = re.compile(
    r"^#\s*(deleted|registered_exhausted|registered|invalid|unavailable|unusable)\s+",
    re.IGNORECASE,
)
_POOL_INACTIVE_STATUSES = {"deleted", "registered_exhausted", "registered", "invalid", "unavailable", "unusable"}
_POOL_TEXT_KEY = "gmail_api_code_pool_text"


def _normalize_pool_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status in {"unavailable", "unusable"}:
        return "invalid"
    if status in {"deleted", "registered_exhausted", "registered", "invalid"}:
        return status
    return "active"


def parse_gmail_api_code_pool_rows(value: Any) -> list[GmailApiCodePoolRow]:
    rows: list[GmailApiCodePoolRow] = []
    seen: set[str] = set()
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        status = "active"
        if line.startswith("#"):
            match = _POOL_STATUS_PREFIX_RE.match(line)
            if not match:
                continue
            status = _normalize_pool_status(match.group(1))
            line = line[match.end():].strip()
        if "----" not in line:
            continue
        email_part, url_part = line.split("----", 1)
        email = _normalize_email(email_part)
        code_url = url_part.strip()
        if not email or "@" not in email or not code_url.lower().startswith(("http://", "https://")):
            continue
        if email in seen:
            continue
        seen.add(email)
        rows.append(GmailApiCodePoolRow(email=email, code_url=code_url, status=status))
    return rows


def parse_gmail_api_code_entries(value: Any) -> list[GmailApiCodeEntry]:
    entries: list[GmailApiCodeEntry] = []
    for row in parse_gmail_api_code_pool_rows(value):
        if row.status in _POOL_INACTIVE_STATUSES:
            continue
        entries.append(GmailApiCodeEntry(email=row.email, code_url=row.code_url))
    return entries


class GmailApiCodeMailbox(BaseMailbox):
    """Allocate fixed API-backed addresses and fetch OTP codes from their API URLs."""

    _CLAIM_LOCK = threading.Lock()
    _POOL_TEXT_LOCK = threading.Lock()
    _ACTIVE_CLAIMS: dict[str, float] = {}
    _INVALID_EMAILS: set[str] = set()
    _CLAIM_TTL_SECONDS = 60 * 60

    def __init__(self, *, pool_text: str = "", poll_interval: str = "", proxy: str | None = None):
        pool_rows = parse_gmail_api_code_pool_rows(pool_text)
        self._registered_pool_emails = {
            row.email for row in pool_rows if row.status in {"registered", "registered_exhausted"}
        }
        self.entries = [
            GmailApiCodeEntry(email=row.email, code_url=row.code_url)
            for row in pool_rows
            if row.status not in {"deleted", "invalid", "registered_exhausted"}
        ]
        self.poll_interval = max(1, self._safe_int(poll_interval, 3))
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._debug_log_fn: Callable[[str], None] | None = None
        self._last_fetch_debug: dict[str, Any] = {}
        self._last_debug_signature = ""
        if not self.entries:
            raise RuntimeError("API接码邮箱未配置有效邮箱，格式：邮箱----接码链接")

    def set_debug_logger(self, log_fn: Callable[[str], None] | None) -> None:
        self._debug_log_fn = log_fn if callable(log_fn) else None

    def _debug_log(self, message: str) -> None:
        if not callable(self._debug_log_fn):
            return
        try:
            self._debug_log_fn(message)
        except Exception:
            return

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return default

    @staticmethod
    def email_alias_limit_for_parent(email: str) -> int:
        domain = _normalize_email(email).rsplit("@", 1)[-1]
        return 1 if domain in {"icloud.com", "me.com", "mac.com"} else 0

    @staticmethod
    def email_alias_uses_parent_account_for_parent(email: str) -> bool:
        domain = _normalize_email(email).rsplit("@", 1)[-1]
        return domain in {"icloud.com", "me.com", "mac.com"}

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
    def _resource_email_candidates(row: ProviderResourceModel, metadata: dict[str, Any]) -> set[str]:
        candidates = {
            _normalize_email(row.handle),
            _normalize_email(row.resource_identifier),
            _normalize_email(metadata.get("email")),
            _normalize_email(metadata.get("alias_parent_email")),
            _normalize_email(metadata.get("email_alias_parent")),
            _normalize_email(metadata.get("parent_email")),
            _normalize_email(metadata.get("main_email")),
        }
        nested = metadata.get("email_alias")
        if isinstance(nested, dict):
            candidates.update(
                {
                    _normalize_email(nested.get("parent_email")),
                    _normalize_email(nested.get("alias_parent_email")),
                    _normalize_email(nested.get("email_alias_parent")),
                }
            )
        return {item for item in candidates if item}

    @classmethod
    def _provider_resource_matches_email(cls, row: ProviderResourceModel, metadata: dict[str, Any], email: str) -> bool:
        target = _normalize_email(email)
        return bool(target and target in cls._resource_email_candidates(row, metadata))

    @staticmethod
    def _pool_marker_for_status(status: str) -> str:
        normalized = _normalize_pool_status(status)
        if normalized == "registered":
            return "# registered "
        if normalized == "registered_exhausted":
            return "# registered_exhausted "
        if normalized == "invalid":
            return "# invalid "
        if normalized == "deleted":
            return "# deleted "
        return ""

    @staticmethod
    def _setting_pool_text(setting: ProviderSettingModel) -> str:
        config = setting.get_config()
        auth = setting.get_auth()
        return str(auth.get(_POOL_TEXT_KEY) or config.get(_POOL_TEXT_KEY) or "")

    @staticmethod
    def _set_setting_pool_text(setting: ProviderSettingModel, pool_text: str) -> None:
        config = setting.get_config()
        auth = setting.get_auth()
        if _POOL_TEXT_KEY in auth or _POOL_TEXT_KEY not in config:
            auth[_POOL_TEXT_KEY] = pool_text
            setting.set_auth(auth)
        else:
            config[_POOL_TEXT_KEY] = pool_text
            setting.set_config(config)

    @classmethod
    def _mark_pool_text_email_status(cls, email: str, status: str) -> bool:
        target = _normalize_email(email)
        marker = cls._pool_marker_for_status(status)
        if not target or not marker:
            return False
        with cls._POOL_TEXT_LOCK:
            with Session(engine) as session:
                setting = session.exec(
                    select(ProviderSettingModel)
                    .where(ProviderSettingModel.provider_type == "mailbox")
                    .where(ProviderSettingModel.provider_key == "gmail_api_code")
                ).first()
                if not setting:
                    return False
                lines = cls._setting_pool_text(setting).splitlines()
                changed = False
                next_lines: list[str] = []
                for raw_line in lines:
                    row = parse_gmail_api_code_pool_rows(raw_line)
                    if not row or row[0].email != target or row[0].status == "deleted":
                        next_lines.append(raw_line)
                        continue
                    if row[0].status == "registered_exhausted" and status != "registered_exhausted":
                        next_lines.append(raw_line)
                        continue
                    next_line = f"{marker}{row[0].email}----{row[0].code_url}"
                    next_lines.append(next_line)
                    changed = changed or next_line != raw_line
                if not changed:
                    return False
                cls._set_setting_pool_text(setting, "\n".join(next_lines))
                setting.updated_at = datetime.now(timezone.utc)
                session.add(setting)
                session.commit()
                return True

    @staticmethod
    def _resource_status_exists(email: str, statuses: set[str]) -> bool:
        target = _normalize_email(email)
        if not target:
            return False
        try:
            with Session(engine) as session:
                setting = session.exec(
                    select(ProviderSettingModel)
                    .where(ProviderSettingModel.provider_type == "mailbox")
                    .where(ProviderSettingModel.provider_key == "gmail_api_code")
                ).first()
                if setting:
                    pool_text = GmailApiCodeMailbox._setting_pool_text(setting)
                    for row in parse_gmail_api_code_pool_rows(pool_text):
                        if row.email == target and row.status in statuses:
                            return True
        except Exception:
            pass
        with Session(engine) as session:
            rows = session.exec(
                select(ProviderResourceModel)
                .where(ProviderResourceModel.provider_type == "mailbox")
                .where(ProviderResourceModel.provider_name == "gmail_api_code")
            ).all()
            for row in rows:
                metadata = row.get_metadata()
                status = str(metadata.get("registration_status") or "").strip().lower()
                if not GmailApiCodeMailbox._provider_resource_matches_email(row, metadata, target):
                    continue
                if "registered_exhausted" in statuses and (
                    status == "registered_exhausted" or metadata.get("registration_alias_exhausted")
                ):
                    return True
                if "registered" in statuses and (status == "registered" or metadata.get("registration_success")):
                    return True
                if "invalid" in statuses and (status == "invalid" or metadata.get("registration_invalid")):
                    return True
        return False

    @classmethod
    def _registered_resource_exists(cls, email: str) -> bool:
        return cls._resource_status_exists(email, {"registered"})

    @classmethod
    def _invalid_resource_exists(cls, email: str) -> bool:
        return cls._resource_status_exists(email, {"invalid"})

    @classmethod
    def _unavailable_resource_exists(cls, email: str) -> bool:
        return cls._registered_resource_exists(email) or cls._invalid_resource_exists(email)

    def _entry_for_account(self, account: MailboxAccount) -> GmailApiCodeEntry:
        target = _normalize_email(account.email)
        for entry in self.entries:
            if entry.email == target:
                return entry
        metadata = ((account.extra or {}).get("provider_resource") or {}).get("metadata", {})
        code_url = str(metadata.get("code_url") or "").strip()
        if target and code_url:
            return GmailApiCodeEntry(email=target, code_url=code_url)
        raise RuntimeError(f"API接码邮箱未找到邮箱配置: {account.email}")

    def peek_email(self) -> str:
        return self.entries[0].email

    def email_alias_parent_registered(self, email: str) -> bool:
        target = _normalize_email(email)
        return target in self._registered_pool_emails or self._registered_resource_exists(target)

    def get_email(self) -> MailboxAccount:
        with self._CLAIM_LOCK:
            self._prune_active_claims_locked()
            unavailable_count = 0
            for entry in self.entries:
                alias_wrapper_enabled = bool(getattr(self, "_email_alias_wrapper_enabled", False))
                alias_uses_parent_account = self.email_alias_uses_parent_account_for_parent(entry.email)
                if self._resource_status_exists(entry.email, {"registered_exhausted"}):
                    unavailable_count += 1
                    continue
                if entry.email in self._INVALID_EMAILS or self._invalid_resource_exists(entry.email):
                    unavailable_count += 1
                    continue
                if self._unavailable_resource_exists(entry.email) and not (
                    alias_wrapper_enabled and alias_uses_parent_account
                ):
                    unavailable_count += 1
                    continue
                exhausted_parents = getattr(self, "_email_alias_exhausted_parents", set())
                if entry.email in exhausted_parents:
                    unavailable_count += 1
                    continue
                if entry.email in self._ACTIVE_CLAIMS:
                    continue
                if self._account_exists(entry.email) and not (alias_wrapper_enabled and alias_uses_parent_account):
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
            raise RuntimeError("API接码邮箱池已无可用邮箱：所有邮箱都已注册或标记无效")
        raise RuntimeError("API接码邮箱池暂未找到可用邮箱")

    @staticmethod
    def _flatten_json(value: Any) -> str:
        if isinstance(value, dict):
            return " ".join(GmailApiCodeMailbox._flatten_json(v) for v in value.values())
        if isinstance(value, list):
            return " ".join(GmailApiCodeMailbox._flatten_json(v) for v in value)
        if isinstance(value, str):
            decoded = GmailApiCodeMailbox._decode_data_uri(value)
            return f"{decoded} {value}" if decoded else value
        return str(value or "")

    @staticmethod
    def _decode_data_uri(value: Any) -> str:
        text = str(value or "").strip()
        if not text.lower().startswith("data:") or "," not in text:
            return ""
        header, payload = text.split(",", 1)
        try:
            if ";base64" in header.lower():
                return base64.b64decode(payload, validate=False).decode("utf-8", errors="replace")
            return urllib.parse.unquote(payload)
        except Exception:
            return ""

    @staticmethod
    def _preview(value: Any, limit: int = 500) -> str:
        text = str(value or "")
        text = re.sub(r"""data:text/html([^"' <>\r\n]*),[A-Za-z0-9+/=_%-]+""", r"data:text/html\1,<body omitted>", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", html.unescape(text)).strip()
        return text[:limit]

    @classmethod
    def _expand_html_body(cls, text: str, _depth: int = 0) -> str:
        value = str(text or "")
        if _depth > 4:
            return value
        decoded_parts: list[str] = []
        data_uris: list[str] = []
        for match in re.finditer(
            r"""src\s*=\s*(["'])(data:text/html.*?)(\1)""",
            value,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            data_uris.append(match.group(2))
        for match in re.finditer(r"""data:text/html[^"' <>\r\n]+""", value, flags=re.IGNORECASE):
            data_uris.append(match.group(0))
        seen: set[str] = set()
        for raw_uri in data_uris:
            uri = html.unescape(raw_uri).strip()
            if uri in seen:
                continue
            seen.add(uri)
            decoded = cls._decode_data_uri(uri)
            if decoded:
                decoded_parts.append(decoded)
        # iframe srcdoc 属性值：其中的 HTML 已被转义（&lt; &gt; &quot; &amp;），
        # 展开后可继续走 data URI / 嵌套 srcdoc / 正文提取流程。
        seen_srcdocs: set[str] = set()
        for match in re.finditer(
            r"""srcdoc\s*=\s*(["'])(.*?)(\1)""",
            value,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            doc = match.group(2)
            for _ in range(4):
                unescaped = html.unescape(doc)
                if unescaped == doc:
                    break
                doc = unescaped
            if doc in seen_srcdocs:
                continue
            seen_srcdocs.add(doc)
            decoded_parts.append(cls._expand_html_body(doc, _depth + 1))
        if decoded_parts:
            return " ".join(decoded_parts) + " " + value
        return value

    @staticmethod
    def _js_string_value(text: str, name: str) -> str:
        pattern = rf"""var\s+{re.escape(name)}\s*=\s*(['"])(.*?)\1"""
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        return html.unescape(match.group(2)) if match else ""

    @classmethod
    def _message_detail_url(cls, text: str, base_url: str) -> str:
        value = str(text or "")
        detail_base = cls._js_string_value(value, "detailBase")
        detail_suffix = cls._js_string_value(value, "detailSuffix")
        if not detail_base or not detail_suffix:
            return ""

        active = re.search(
            r"""(?is)<a\b(?=[^>]*\bactive\b)(?=[^>]*\bdata-id\s*=\s*["'](\d+)["'])[^>]*>""",
            value,
        )
        message_id = active.group(1) if active else ""
        if not message_id:
            first = re.search(r"""(?is)<a\b(?=[^>]*\bdata-id\s*=\s*["'](\d+)["'])[^>]*>""", value)
            message_id = first.group(1) if first else ""
        if not message_id:
            return ""
        return urllib.parse.urljoin(base_url, f"{detail_base}{message_id}{detail_suffix}")

    @classmethod
    def _message_identity_key(cls, text: str, base_url: str = "") -> str:
        value = str(text or "")
        base = str(base_url or "")
        for source in (value, base):
            if not source:
                continue
            for pattern in (
                r"""(?:/message/|#mail-|data-id=["'])(\d{3,})""",
                r"""href\s*=\s*["']#mail-(\d+)["']""",
                r"""data-id\s*=\s*["'](\d+)["']""",
                r"""/messages?/(\d+)/""",
                r"""id\s*=\s*["']mail-(\d+)["']""",
                r"""data-(?:message|mail)-id\s*=\s*["'](\d+)["']""",
                r"""[?&](?:message_id|msg_id|messageId)=(\d+)""",
                r"""id\s*=\s*["']message-(\d+)["']""",
            ):
                match = re.search(pattern, source, flags=re.IGNORECASE)
                if match:
                    return match.group(1)
        return ""

    @staticmethod
    def _html_to_text(value: str) -> str:
        text = str(value or "")
        text = re.sub(r"(?is)<script\b.*?</script>", " ", text)
        text = re.sub(r"(?is)<style\b.*?</style>", " ", text)
        text = re.sub(r"(?s)<!--.*?-->", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", html.unescape(text)).strip()

    @staticmethod
    def _strip_mail_list_noise(value: str) -> str:
        text = str(value or "")
        text = re.sub(r"""href\s*=\s*["']#?mail-\d+["']""", 'href=""', text, flags=re.IGNORECASE)
        text = re.sub(r"""href\s*=\s*["'][^"']*/message/\d+/[^"']*["']""", 'href=""', text, flags=re.IGNORECASE)
        text = re.sub(r"""data-id\s*=\s*["']\d+["']""", 'data-id=""', text, flags=re.IGNORECASE)
        text = re.sub(r"""id\s*=\s*["']mail-\d+["']""", 'id=""', text, flags=re.IGNORECASE)
        text = re.sub(r"""mail-\d+""", "", text, flags=re.IGNORECASE)
        text = re.sub(r"""/message/\d+/""", "/message/", text, flags=re.IGNORECASE)
        return text

    @classmethod
    def _extract_code_from_main_table(cls, value: str) -> str:
        text = str(value or "")
        text = cls._strip_mail_list_noise(text)
        table_pattern = re.compile(
            r"""(?is)<table\b[^>]*class\s*=\s*["'][^"']*\bmain\b[^"']*["'][^>]*>(.*?)</table>"""
        )
        blocks = [match.group(1) for match in table_pattern.finditer(text)] or [text]
        for block in blocks:
            match = re.search(
                r"""(?is)verification code to continue:.*?<p\b[^>]*>(.*?)</p>""",
                block,
            )
            if not match:
                continue
            plain = cls._html_to_text(match.group(1))
            code_match = re.search(r"(?<!#)(?<!\d)(\d{6})(?!\d)", plain)
            if code_match:
                return code_match.group(1)
        return ""

    @staticmethod
    def _api_status_value(value: Any) -> int | None:
        if isinstance(value, dict):
            for key in ("status", "status_code", "code", "error_code"):
                raw = value.get(key)
                if isinstance(raw, int) and raw in {502, 602}:
                    return raw
                if isinstance(raw, str) and raw.strip() in {"502", "602"}:
                    return int(raw.strip())
        return None

    def _fetch_text(self, entry: GmailApiCodeEntry) -> str:
        response = requests.get(entry.code_url, proxies=self.proxy, timeout=15)
        text = response.text or ""
        message_key = self._message_identity_key(text, getattr(response, "url", "") or entry.code_url)
        data = None
        try:
            data = json.loads(text)
        except Exception:
            data = None
        status = self._api_status_value(data)
        http_status = getattr(response, "status_code", None)
        if status is None and http_status in {502, 602}:
            status = http_status
        expanded = self._expand_html_body(self._flatten_json(data) if data is not None else text)
        self._last_fetch_debug = {
            "url": entry.code_url,
            "http_status": http_status,
            "api_status": status,
            "content_type": str(getattr(response, "headers", {}).get("content-type", "") or ""),
            "raw_len": len(text),
            "expanded_len": len(expanded),
            "data_uri_count": len(re.findall(r"data:text/html", text, flags=re.IGNORECASE)),
            "has_mail_view": bool(re.search(r"""id\s*=\s*["']mail-view["']""", text, flags=re.IGNORECASE)),
            "has_main_table": bool(re.search(r"""<table\b[^>]*class\s*=\s*["'][^"']*\bmain\b""", expanded, flags=re.IGNORECASE)),
            "json_type": type(data).__name__ if data is not None else "",
            "raw_preview": self._preview(text),
            "decoded_preview": self._preview(self._html_to_text(expanded)),
        }
        if status == 602:
            return ""
        if status == 502:
            raise GmailApiCodeMailboxUnavailable("API接码邮箱不可用或已下架 (api_status=502)")
        response.raise_for_status()
        detail_url = "" if data is not None else self._message_detail_url(text, getattr(response, "url", "") or entry.code_url)
        if detail_url:
            detail_response = requests.get(detail_url, proxies=self.proxy, timeout=15)
            detail_text = detail_response.text or ""
            detail_data = None
            try:
                detail_data = json.loads(detail_text)
            except Exception:
                detail_data = None
            detail_response.raise_for_status()
            expanded = self._expand_html_body(self._flatten_json(detail_data) if detail_data is not None else detail_text)
            if not message_key:
                message_key = self._message_identity_key(detail_text, detail_url)
            self._last_fetch_debug.update(
                {
                    "detail_url": detail_url,
                    "detail_http_status": getattr(detail_response, "status_code", None),
                    "detail_content_type": str(getattr(detail_response, "headers", {}).get("content-type", "") or ""),
                    "detail_raw_len": len(detail_text),
                    "expanded_len": len(expanded),
                    "data_uri_count": len(re.findall(r"data:text/html", detail_text, flags=re.IGNORECASE)),
                    "has_main_table": bool(re.search(r"""<table\b[^>]*class\s*=\s*["'][^"']*\bmain\b""", expanded, flags=re.IGNORECASE)),
                    "json_type": type(detail_data).__name__ if detail_data is not None else "",
                    "detail_raw_preview": self._preview(detail_text),
                    "decoded_preview": self._preview(self._html_to_text(expanded)),
                    "message_key": message_key,
                }
            )
        else:
            self._last_fetch_debug["message_key"] = message_key
        return expanded

    def _current_id(self, text: str, code_pattern: str | None = None) -> str:
        expanded_text = self._expand_html_body(str(text or ""))
        message_key = str((self._last_fetch_debug or {}).get("message_key") or "").strip()
        code = self._extract_code_from_text(expanded_text, code_pattern=code_pattern)
        if message_key and code:
            return f"mail:{message_key}|code:{code}"
        if message_key:
            return f"mail:{message_key}"
        if code:
            return f"code:{code}"
        digest = hashlib.sha1(expanded_text.encode("utf-8", "ignore")).hexdigest()
        return f"body:{digest}"

    @staticmethod
    def _extract_code_from_text(text: str, code_pattern: str | None = None) -> str:
        expanded_text = GmailApiCodeMailbox._expand_html_body(str(text or ""))
        cleaned_text = GmailApiCodeMailbox._strip_mail_list_noise(expanded_text)
        main_table_code = GmailApiCodeMailbox._extract_code_from_main_table(expanded_text)
        if main_table_code:
            return main_table_code

        plain = GmailApiCodeMailbox._html_to_text(cleaned_text)
        contextual_patterns = (
            r"(?is)you can also enter this temporary code[^\d]{0,100}(\d{6})(?!\d)",
            r"(?is)temporary chatgpt login code.*?temporary code[^\d]{0,120}(\d{6})(?!\d)",
            r"(?is)log in to chatgpt.*?temporary code[^\d]{0,120}(\d{6})(?!\d)",
            r"(?is)verification code to continue[^\d]{0,80}(\d{6})(?!\d)",
            r"(?is)enter this temporary verification code[^\d]{0,100}(\d{6})(?!\d)",
            r"(?is)security code[^\d]{0,80}(\d{6})(?!\d)",
            r"(?is)验证码[^\d]{0,80}(\d{6})(?!\d)",
            r"(?is)确认码[^\d]{0,80}(\d{6})(?!\d)",
        )
        for raw_pattern in contextual_patterns:
            match = re.search(raw_pattern, plain)
            if match:
                return match.group(1)

        if code_pattern:
            pattern = re.compile(code_pattern)
            match = pattern.search(plain)
            if not match:
                return ""
            return match.group(1) if match.groups() else match.group(0)

        pattern = re.compile(r"(?<!#)(?<!\d)(\d{6})(?!\d)")
        for match in pattern.finditer(plain):
            window = plain[max(0, match.start() - 12): match.end() + 12]
            if re.search(r"\d{4}[-/]\d{2}[-/]\d{2}", window) or re.search(r"\d{2}:\d{2}:\d{2}", window):
                continue
            return match.group(1)
        return ""

    @staticmethod
    def _extract_code(text: str, code_pattern: str | None = None) -> str:
        return GmailApiCodeMailbox._extract_code_from_text(text, code_pattern=code_pattern)

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
                self._emit_fetch_debug(code=code, current_id=current_id, seen=current_id in seen)
                if code and current_id not in seen:
                    return code
                seen.add(current_id)
            except GmailApiCodeMailboxUnavailable as exc:
                self._emit_fetch_debug(code="", current_id="", seen=False)
                self.mark_invalid_email(account, reason="gmail_api_code_502")
                raise RuntimeError(str(exc)) from exc
            except Exception as exc:
                self._emit_fetch_debug(code="", current_id="", seen=False)
                last_error = str(exc)
            time.sleep(self.poll_interval)
        suffix = f": {last_error}" if last_error else ""
        raise TimeoutError(f"等待 API接码邮箱验证码超时 ({timeout}s){suffix}")

    def _emit_fetch_debug(self, *, code: str, current_id: str, seen: bool) -> None:
        info = dict(self._last_fetch_debug or {})
        if not info:
            return
        signature = "|".join(
            [
                str(info.get("http_status") or ""),
                str(info.get("api_status") or ""),
                str(info.get("raw_len") or ""),
                str(info.get("expanded_len") or ""),
            str(current_id or ""),
            "seen" if seen else "new",
        ]
        )
        if signature == self._last_debug_signature:
            return
        self._last_debug_signature = signature
        self._debug_log(
            "[API邮箱] fetch "
            f"url={info.get('url') or ''} "
            f"detail_url={info.get('detail_url') or ''} "
            f"http={info.get('http_status') or ''} "
            f"detail_http={info.get('detail_http_status') or ''} "
            f"api_status={info.get('api_status') or ''} "
            f"content_type={info.get('content_type') or ''} "
            f"raw_len={info.get('raw_len') or 0} "
            f"expanded_len={info.get('expanded_len') or 0} "
            f"data_uri={info.get('data_uri_count') or 0} "
            f"mail_view={'yes' if info.get('has_mail_view') else 'no'} "
            f"main_table={'yes' if info.get('has_main_table') else 'no'} "
            f"message_key={info.get('message_key') or '-'} "
            f"extracted={code or '-'} "
            f"current_id={current_id or '-'} "
            f"seen={'yes' if seen else 'no'}"
        )
        self._debug_log(f"[API邮箱] raw_preview={info.get('raw_preview') or ''}")
        if info.get("detail_raw_preview"):
            self._debug_log(f"[API邮箱] detail_raw_preview={info.get('detail_raw_preview') or ''}")
        self._debug_log(f"[API邮箱] decoded_preview={info.get('decoded_preview') or ''}")

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
        raise TimeoutError(f"等待 API接码邮箱验证链接超时 ({timeout}s)")

    def mark_registration_success(self, account: MailboxAccount) -> list[str]:
        email = _normalize_email(getattr(account, "email", ""))
        self._release_active_claim(email)
        if not email:
            return []
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        pool_updated = self._mark_pool_text_email_status(email, "registered")
        updated = False
        with Session(engine) as session:
            rows = session.exec(
                select(ProviderResourceModel)
                .where(ProviderResourceModel.provider_type == "mailbox")
                .where(ProviderResourceModel.provider_name == "gmail_api_code")
            ).all()
            for row in rows:
                metadata = row.get_metadata()
                if not self._provider_resource_matches_email(row, metadata, email):
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
        return ["API接码邮箱已注册"] if pool_updated or updated or self._account_exists(email) else []

    def mark_alias_exhausted(self, account: MailboxAccount, reason: str = "") -> list[str]:
        email = _normalize_email(getattr(account, "email", ""))
        self._release_active_claim(email)
        if not email:
            return []
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        pool_updated = self._mark_pool_text_email_status(email, "registered_exhausted")
        updated = False
        with Session(engine) as session:
            rows = session.exec(
                select(ProviderResourceModel)
                .where(ProviderResourceModel.provider_type == "mailbox")
                .where(ProviderResourceModel.provider_name == "gmail_api_code")
            ).all()
            for row in rows:
                metadata = row.get_metadata()
                if not self._provider_resource_matches_email(row, metadata, email):
                    continue
                metadata.update(
                    {
                        "email": email,
                        "registration_status": "registered_exhausted",
                        "registration_alias_exhausted": True,
                        "registration_alias_exhausted_reason": reason or "registration_disallowed",
                        "registration_alias_exhausted_at": now,
                    }
                )
                row.set_metadata(metadata)
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
                updated = True
            if updated:
                session.commit()
        return ["API接码邮箱已注册"] if pool_updated or updated or self._account_exists(email) else []

    def mark_invalid_email(self, account: MailboxAccount, reason: str = "") -> list[str]:
        email = _normalize_email(getattr(account, "email", ""))
        if not email:
            return []
        with self._CLAIM_LOCK:
            self._ACTIVE_CLAIMS.pop(email, None)
            self._INVALID_EMAILS.add(email)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        pool_updated = self._mark_pool_text_email_status(email, "invalid")
        updated = False
        with Session(engine) as session:
            rows = session.exec(
                select(ProviderResourceModel)
                .where(ProviderResourceModel.provider_type == "mailbox")
                .where(ProviderResourceModel.provider_name == "gmail_api_code")
            ).all()
            for row in rows:
                metadata = row.get_metadata()
                if not self._provider_resource_matches_email(row, metadata, email):
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
        return ["API接码邮箱已标记无效"] if pool_updated or updated or email in self._INVALID_EMAILS else []
