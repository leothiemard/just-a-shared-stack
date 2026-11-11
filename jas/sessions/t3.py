"""`t3`: T3 Code's local state database. Not implemented yet."""

from __future__ import annotations

from typing import Iterator

from jas.sessions import SessionRecord, Source


class T3Source(Source):
    name = "t3"

    def available(self) -> tuple[bool, str]:
        return False, "not implemented yet"

    def records(self) -> Iterator[SessionRecord]:
        return iter(())
