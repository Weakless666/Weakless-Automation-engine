"""Secret redaction for logs, execution traces and API payloads."""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

MASK = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[-_]?key|authorization|cookie|credential|private[-_]?key|signature|client[-_]?secret|bearer)",
    re.IGNORECASE,
)
_PATTERNS = [
    re.compile(r"(?i)(bearer|basic)\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(xox[abprs]-[A-Za-z0-9-]{10,})\b"),
    re.compile(r"\b(whsec|rk|pk)_(live|test)_[A-Za-z0-9]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot tokens
]


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, MASK)
    for p in _PATTERNS:
        text = p.sub(MASK, text)
    return text


def redact(value: Any, secrets: Iterable[str] = (), _depth: int = 0) -> Any:
    """Deep-copy ``value`` with sensitive keys masked and known secret values removed."""
    if _depth > 12:
        return MASK
    secrets = tuple(secrets)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _SENSITIVE_KEY.search(k) and v not in (None, "", False, True):
                out[k] = MASK
            else:
                out[k] = redact(v, secrets, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets, _depth + 1) for v in value]
    if isinstance(value, str):
        return redact_text(value, secrets)
    return value


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        record.msg = redact_text(msg)
        record.args = ()
        return True


def truncate(value: Any, limit: int = 20000) -> Any:
    """Keep stored step payloads bounded."""
    import json

    try:
        raw = json.dumps(value, default=str)
    except (TypeError, ValueError):
        return {"_unserializable": str(type(value))}
    if len(raw) <= limit:
        return json.loads(raw)
    return {"_truncated": True, "preview": raw[:limit]}
