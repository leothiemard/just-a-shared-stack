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
    (re.compile(r"(?i)\b([A-Za-z0-9_.-]*(?:key|token|password|passwd|secret)\"?\s*=\s*\"?)[^\s\"'&,;]+"), r"\1" + R),
    (re.compile(r"(://[^/\s:@]+:)[^/\s@]+@"), r"\1" + R + "@"),
]


def text(value: str) -> str:
    for pattern, replacement in PATTERNS:
        value = pattern.sub(replacement, value)
    return value


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
