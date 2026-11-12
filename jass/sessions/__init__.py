"""Session sources: where finished agent sessions are read from.

A source reads one store strictly read-only (SQLite through a `mode=ro` URI; never
checkpoint, never write) and yields `SessionRecord`s. The export pipeline
(`jass.sessions.export`) filters, scrubs and renders them; sources do none of that.

To write a source:

- Subclass `Source`, set `name`, and register the class in `SOURCES` below.
- `available()` returns `(True, "")`, or `(False, reason)` when the store is missing
  or its schema is not the one the source was written for. The pipeline and
  `doctor` print `<name> source disabled: <reason>` once; the source never raises.
- `records(changed_after)` yields top-level sessions only, each with its subagents
  already folded in (`subagent_count`, `delegated`). With `changed_after`, it may skip
  sessions last updated at or before that time without loading them. It may skip
  sessions that are deleted or have no completed turn; idle, `--since` and exclusion
  filters are the pipeline's.
- `projects()` lists `(name, path)` pairs for the box card.

Config selects sources by spec in `sessions.sources`: `t3`, `claude`, `codex`, or
`json:/path/file.json` (a list of `SessionRecord` objects as JSON, used by tests).
`sessions.t3_store` overrides the T3 database path.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class SessionRecord:
    id: str
    title: str
    project: str
    cwd: str
    started: dt.datetime  # timezone-aware
    updated: dt.datetime  # timezone-aware; last activity
    provider: str = ""
    model: str = ""
    parent_id: str = ""  # set on subagent sessions; the pipeline never exports those
    pr_urls: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)  # human prompts only, oldest first
    turn_finals: list[str] = field(default_factory=list)  # last assistant text of each turn
    tokens: dict[str, int] = field(default_factory=dict)  # e.g. {"input": n, "cached": n, "output": n}
    turns: int = 0  # turns run; 0 means count prompts and finals
    peak_context: int = 0  # largest context window use seen, in tokens
    subagent_count: int = 0
    delegated: list[dict[str, str]] = field(default_factory=list)  # {"title", "model", "status"} per child
    environment_id: str = ""  # T3 environment id, for opening the full thread
    native_ref: str = ""  # "claude:<session id>" or "codex:<thread id>"
    branch: str = ""
    created_by: str = ""  # user, agent or system


class Source:
    name = ""

    def __init__(self, arg: str = "") -> None:
        self.arg = arg

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    def records(self, changed_after: dt.datetime | None = None) -> Iterator[SessionRecord]:
        raise NotImplementedError

    def projects(self) -> list[tuple[str, str]]:
        return []


def sources(settings: dict) -> list[Source]:
    """The sources named in a box config's `sessions` section."""
    from jass.sessions import claude, codex, jsonfile, t3

    registry = {cls.name: cls for cls in (claude.ClaudeSource, codex.CodexSource, jsonfile.JsonSource)}
    out: list[Source] = []
    for spec in settings.get("sources", []):
        name, _, arg = spec.partition(":")
        if name == "t3":
            out.append(t3.T3Source(arg, settings.get("t3_store", "")))
        elif name in registry:
            out.append(registry[name](arg))
        else:
            out.append(Unknown(spec))
    return out


class Unknown(Source):
    def __init__(self, spec: str) -> None:
        super().__init__(spec)
        self.name = spec

    def available(self) -> tuple[bool, str]:
        return False, "unknown source; use t3, claude, codex or json:/path in sessions.sources"

    def records(self, changed_after: dt.datetime | None = None) -> Iterator[SessionRecord]:
        return iter(())
