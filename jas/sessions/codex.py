"""`codex`: Codex's native JSONL sessions. Not implemented yet."""

from __future__ import annotations

from typing import Iterator

from jas.sessions import SessionRecord, Source


class CodexSource(Source):
    name = "codex"

    def available(self) -> tuple[bool, str]:
        return False, "not implemented yet"

    def records(self) -> Iterator[SessionRecord]:
        return iter(())
