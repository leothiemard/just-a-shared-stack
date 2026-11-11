"""`claude`: Claude Code's native JSONL transcripts. Not implemented yet."""

from __future__ import annotations

from typing import Iterator

from jas.sessions import SessionRecord, Source


class ClaudeSource(Source):
    name = "claude"

    def available(self) -> tuple[bool, str]:
        return False, "not implemented yet"

    def records(self) -> Iterator[SessionRecord]:
        return iter(())
