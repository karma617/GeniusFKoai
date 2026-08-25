"""Logging redaction shared by GoPay protocol modules."""
from __future__ import annotations

import logging
import re

_PHONE_RE = re.compile(r"(?<![A-Za-z0-9])\+?\d{8,15}(?![A-Za-z0-9])")
_PROXY_AUTH_RE = re.compile(r"(?i)(https?|socks5h?)://[^\s/@]+@")
_SECRET_CONTEXT_RE = re.compile(
    r"(?i)(\b(?:pin|otp|code|api[_-]?key|access[_-]?token|refresh[_-]?token|pin[_-]?token|otp[_-]?token)"
    r"[\"']?\s*[:=]\s*[\"']?)([^\s,;)}\]\"']+)"
)


def redact_sensitive_log(message: object) -> str:
    text = str(message or "")
    text = _PROXY_AUTH_RE.sub(lambda match: match.group(1) + "://***@", text)
    text = _SECRET_CONTEXT_RE.sub(lambda match: match.group(1) + "******", text)
    text = _PHONE_RE.sub(lambda match: "***" + match.group(0)[-4:], text)
    return text


class SensitiveLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive_log(record.getMessage())
        record.args = ()
        return True


def install_sensitive_log_filter(logger: logging.Logger) -> None:
    if not any(isinstance(item, SensitiveLogFilter) for item in logger.filters):
        logger.addFilter(SensitiveLogFilter())
