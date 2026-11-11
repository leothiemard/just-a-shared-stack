"""Session sources: where finished agent sessions are read from.

A source reads one store strictly read-only (SQLite through a `mode=ro` URI; never
checkpoint, never write) and yields `SessionRecord`s. The export pipeline
(`jas.sessions.export`) filters, scrubs and renders them; sources do none of that.

To write a source:

- Subclass `Source`, set `name`, and register the class in `SOURCES` below.
- `available()` returns `(True, "")`, or `(False, reason)` when the store is missing
  or its schema is not the one the source was written for. The pipeline and
  `doctor` print `<name> source disabled: <reason>` once; the source never raises.
- `records()` yields top-level sessions only, each with its subagents already folded
  in (`subagent_count`, `delegated`). It may skip sessions that are deleted or have
  no completed turn; idle, `--since`, exclusion and change filters are the pipeline's.
- `projects()` lists `(name, path)` pairs for the box card.

Config selects sources by spec: `t3`, `claude`, `codex`, or `json:/path/file.json`
(a list of `SessionRecord` objects as JSON, used by tests).
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

    def records(self) -> Iterator[SessionRecord]:
        raise NotImplementedError

    def projects(self) -> list[tuple[str, str]]:
        return []


def sources(specs: list[str]) -> list[Source]:
    from jas.sessions import claude, codex, jsonfile, t3

    registry = {cls.name: cls for cls in (t3.T3Source, claude.ClaudeSource, codex.CodexSource, jsonfile.JsonSource)}
    out = []
    for spec in specs:
        name, _, arg = spec.partition(":")
        if name not in registry:
            out.append(Unknown(spec))
        else:
            out.append(registry[name](arg))
    return out


class Unknown(Source):
    def __init__(self, spec: str) -> None:
        super().__init__(spec)
        self.name = spec

    def available(self) -> tuple[bool, str]:
        return False, "unknown source; use t3, claude, codex or json:/path in sessions.sources"

    def records(self) -> Iterator[SessionRecord]:
        return iter(())
