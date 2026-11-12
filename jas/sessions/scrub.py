"""Redact secrets from text before it is written to the brain."""

from __future__ import annotations

import dataclasses
import re

R = "[redacted]"
PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), R),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), R),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), R),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), R),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), R),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), R),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), R),
    (re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/-]{8,}=*"), r"\1" + R),
    (re.compile(r"(://[^/\s:@]+:)[^/\s@]+@"), r"\1" + R + "@"),
]
# `key=value`, `key: value` and `"key": "value"` where the key is a credential word, alone or as a `_`, `-` or `.` suffix.
CREDENTIAL = re.compile(
    r"""(?<![\w.-])(["']?)((?:[\w.-]*[_.-])?(?:password|passwd|secret|token|api_key|apikey|access_key|auth|credentials?))\1(?![\w-])([ \t]*[:=][ \t]*)"""
    r"""(?:(")(?:[^"\\\n]|\\.)+"|(')[^'\n]+'|[^\s"'&,;}]+)""",
    re.I,
)


def credential(match: re.Match) -> str:
    quote = match.group(4) or match.group(5) or ""
    return match.group(1) + match.group(2) + match.group(1) + match.group(3) + quote + R + quote


def text(value: str) -> str:
    for pattern, replacement in PATTERNS:
        value = pattern.sub(replacement, value)
    return CREDENTIAL.sub(credential, value)


def value(item):
    if isinstance(item, str):
        return text(item)
    if isinstance(item, list):
        return [value(x) for x in item]
    if isinstance(item, dict):
        return {k: value(v) for k, v in item.items()}
    return item


def record(rec):
    """A copy of a SessionRecord with every text field scrubbed."""
    return dataclasses.replace(rec, **{f.name: value(getattr(rec, f.name)) for f in dataclasses.fields(rec)})
