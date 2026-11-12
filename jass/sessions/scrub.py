"""Redact secrets from text before it is written to the lore."""

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
# An `Authorization:` or `Proxy-Authorization:` header: the whole value, whatever the scheme.
HEADER = re.compile(r"""(?i)\b((?:proxy-)?authorization[ \t]*:[ \t]*)(?=[^\s"'\\])[^\n]*""")
# `key=value`, `key: value` and `"key": "value"` where the key is a credential word, alone or as a `_`, `-` or `.` suffix.
# The key's quotes may be escaped (`\"key\"`), as in JSON inside a string.
KEY = re.compile(
    r"""(?<![\w.-])(\\?["']|)((?:[\w.-]*[_.-])?(?:password|passwd|secret|token|api_key|apikey|access_key|auth|authorization|credentials?))\1(?![\w-])"""
    r"""[ \t]*[:=][ \t]*""",
    re.I,
)
OPENING = re.compile(r"""\\?["']""")
UNQUOTED = re.compile(r"""[^\s"'&,;}]+""")


def closing(value: str, start: int, quote: str) -> int | None:
    """The end of the quote that closes a value opened by `quote` (`"`, `'`, or escaped `\\"`, `\\'`), or None.

    A plain quote closes at the next one after an even run of backslashes; an escaped quote at the next one after exactly one."""
    char = quote[-1]
    at = value.find(char, start)
    while at != -1:
        run = 0
        while at - run > start and value[at - run - 1] == "\\":
            run += 1
        if (run % 2 == 0) if len(quote) == 1 else run == 1:
            return at + 1
        at = value.find(char, at + 1)
    return None


def credentials(value: str) -> str:
    """Credential values replaced; when in doubt, more is redacted, never less.

    A quoted value goes through its closing quote, across lines, or to the end of its line when it has none."""
    out, pos = [], 0
    while True:
        match = KEY.search(value, pos)
        if not match:
            return "".join(out) + value[pos:]
        start = match.end()
        out.append(value[pos:start])
        quote = OPENING.match(value, start)
        if quote:
            end = closing(value, quote.end(), quote.group())
            if end is None:
                line = value.find("\n", quote.end())
                out.append(quote.group() + R)
                pos = len(value) if line == -1 else line
            else:
                out.append(quote.group() + R + quote.group())
                pos = end
        else:
            bare = UNQUOTED.match(value, start)
            out.append(R if bare else "")
            pos = bare.end() if bare else start


def text(value: str) -> str:
    for pattern, replacement in PATTERNS:
        value = pattern.sub(replacement, value)
    return credentials(HEADER.sub(lambda m: m.group(1) + R, value))


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
