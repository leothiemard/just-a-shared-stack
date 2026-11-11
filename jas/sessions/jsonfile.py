"""`json:/path/file.json`: a list of SessionRecord objects as JSON, for tests and imports."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Iterator

from jas.sessions import SessionRecord, Source

FIELDS = {f.name for f in dataclasses.fields(SessionRecord)}
REQUIRED = {"id", "title", "project", "cwd", "started", "updated"}


def timestamp(text: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


class JsonSource(Source):
    name = "json"

    def load(self) -> list[dict]:
        data = json.loads(Path(self.arg).read_text())
        if not isinstance(data, list) or not all(isinstance(row, dict) and REQUIRED <= set(row) for row in data):
            raise ValueError(f"expected a list of objects with {', '.join(sorted(REQUIRED))}")
        return data

    def available(self) -> tuple[bool, str]:
        if not Path(self.arg).is_file():
            return False, f"no file {self.arg}"
        try:
            self.load()
        except ValueError as error:
            return False, f"unrecognised schema in {self.arg}: {error}"
        return True, ""

    def records(self) -> Iterator[SessionRecord]:
        for row in self.load():
            values = {k: v for k, v in row.items() if k in FIELDS}
            values["started"], values["updated"] = timestamp(row["started"]), timestamp(row["updated"])
            yield SessionRecord(**values)

    def projects(self) -> list[tuple[str, str]]:
        return sorted({(row["project"], row["cwd"]) for row in self.load() if row.get("cwd") and not row.get("parent_id")})
