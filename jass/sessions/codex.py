"""`codex`: Codex's native JSONL sessions. Not implemented yet."""

from __future__ import annotations

import datetime as dt
from typing import Iterator

from jass.sessions import SessionRecord, Source


class CodexSource(Source):
    name = "codex"

    def available(self) -> tuple[bool, str]:
        return False, "not implemented yet"

    def records(self, changed_after: dt.datetime | None = None) -> Iterator[SessionRecord]:
        return iter(())
