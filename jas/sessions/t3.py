"""`t3`: T3 Code's projection tables in `~/.t3/userdata/statev2.sqlite`, read strictly read-only.

The tables are internal to T3 Code and change often, so every table and column
selected here is pinned in `PINNED` and checked before reading. Top-level threads
(no parent, or a fork) become records; their descendants are folded in.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Iterator

from jas import paths
from jas.sessions import SessionRecord, Source

T = "orchestration_v2_projection_"
PINNED = {
    f"{T}threads": ("thread_id", "project_id", "title", "created_at", "updated_at", "deleted_at", "provider_instance_id", "payload_json"),
    f"{T}runs": ("run_id", "thread_id", "ordinal", "status"),
    f"{T}messages": ("thread_id", "run_id", "role", "created_at", "message_id", "payload_json"),
    f"{T}provider_turns": ("thread_id", "payload_json"),
    f"{T}provider_threads": ("thread_id", "first_run_ordinal", "payload_json"),
    f"{T}subagents": ("thread_id", "child_thread_id", "origin", "status", "started_at", "payload_json"),
    "projection_projects": ("project_id", "title", "workspace_root", "deleted_at"),
    "projection_thread_pull_requests": ("thread_id", "url"),
}
# Markers on user-role messages that T3 injected rather than a human typed.
INJECTED = ("delegatedCompletion", "notification", "scheduledTaskId", "senderThreadId", "idleCompaction")
DRIVERS = {"claudeAgent": "claude", "codex": "codex"}


def default_store() -> Path:
    return paths.home() / ".t3" / "userdata" / "statev2.sqlite"


def timestamp(text: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def project_key(workspace_root: str) -> str:
    """The brain project for a T3 workspace: its basename; anything under a `scratch` folder is `scratch`."""
    root = Path(workspace_root or "")
    if "scratch" in root.parts:
        return "scratch"
    return root.name or "no-project"


def connect(store: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{store.resolve().as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


class T3Source(Source):
    name = "t3"

    def __init__(self, arg: str = "", store: str = "") -> None:
        super().__init__(arg)
        self.store = paths.expand(arg or store) if (arg or store) else default_store()

    def drift(self, conn: sqlite3.Connection) -> str:
        """What no longer matches the pinned schema, or an empty string."""
        problems = []
        for table, columns in PINNED.items():
            have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not have:
                problems.append(f"table {table} missing")
                continue
            problems += [f"column {table}.{c} missing" for c in columns if c not in have]
        if problems:
            return "; ".join(problems)
        row = conn.execute(f"SELECT payload_json FROM {T}threads ORDER BY updated_at DESC LIMIT 1").fetchone()
        try:
            payload = json.loads(row[0]) if row else {}
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict) or (row and "lineage" not in payload):
            return f"thread payload in {T}threads no longer has the expected shape"
        return ""

    def available(self) -> tuple[bool, str]:
        if not self.store.is_file():
            return False, f"no store at {paths.tilde(self.store)}"
        try:
            with contextlib.closing(connect(self.store)) as conn:
                problem = self.drift(conn)
        except sqlite3.Error as error:
            return False, f"cannot read {paths.tilde(self.store)}: {error}"
        return (False, f"schema changed in {paths.tilde(self.store)}: {problem}") if problem else (True, "")

    def environment_id(self) -> str:
        try:
            return (self.store.parent / "environment-id").read_text().strip()
        except OSError:
            return ""

    def records(self, changed_after: dt.datetime | None = None) -> Iterator[SessionRecord]:
        with contextlib.closing(connect(self.store)) as conn:
            roots = {pid: root for pid, root in conn.execute("SELECT project_id, workspace_root FROM projection_projects")}
            threads = conn.execute(
                f"SELECT thread_id, project_id, title, created_at, updated_at, deleted_at, provider_instance_id,"
                f" json_extract(payload_json, '$.lineage.parentThreadId'), json_extract(payload_json, '$.lineage.relationshipToParent')"
                f" FROM {T}threads"
            ).fetchall()
            parent = {row[0]: row[7] for row in threads if row[7] and row[8] != "fork"}
            descendants: dict[str, int] = {}
            for child in parent:
                top, seen = child, set()
                while top in parent and top not in seen:
                    seen.add(top)
                    top = parent[top]
                descendants[top] = descendants.get(top, 0) + 1
            completed = {row[0] for row in conn.execute(f"SELECT DISTINCT thread_id FROM {T}runs WHERE status = 'completed'")}
            with_runs = {row[0] for row in conn.execute(f"SELECT DISTINCT thread_id FROM {T}runs")}
            env = self.environment_id()
            for thread_id, project_id, title, created, updated, deleted, provider, *_ in threads:
                if thread_id in parent or deleted or thread_id not in completed:
                    continue
                if changed_after and timestamp(updated) <= changed_after:
                    continue
                rec = load(conn, thread_id, with_runs)
                root = roots.get(project_id) or ""
                rec.update(
                    id=thread_id, title=title or "", project=project_key(root), started=timestamp(created),
                    updated=timestamp(updated), provider=provider or "", subagent_count=descendants.get(thread_id, 0),
                    environment_id=env,
                )
                rec["cwd"] = rec["cwd"] or root
                yield SessionRecord(**rec)

    def projects(self) -> list[tuple[str, str]]:
        with contextlib.closing(connect(self.store)) as conn:
            rows = conn.execute("SELECT title, workspace_root FROM projection_projects WHERE deleted_at IS NULL ORDER BY title")
            return [(title or project_key(root), root) for title, root in rows if root]


def load(conn: sqlite3.Connection, thread_id: str, with_runs: set[str]) -> dict:
    """Everything a digest needs from one top-level thread, through the per-thread indexes."""
    (payload_text,) = conn.execute(f"SELECT payload_json FROM {T}threads WHERE thread_id = ?", (thread_id,)).fetchone()
    payload = json.loads(payload_text)
    created_by = payload.get("createdBy") or ""
    prs = [pr.get("url") for pr in payload.get("pullRequests") or [] if isinstance(pr, dict) and pr.get("url")]
    for (url,) in conn.execute("SELECT url FROM projection_thread_pull_requests WHERE thread_id = ?", (thread_id,)):
        if url and url not in prs:
            prs.append(url)

    runs = [run_id for run_id, in conn.execute(f"SELECT run_id FROM {T}runs WHERE thread_id = ? ORDER BY ordinal", (thread_id,))]
    marker_sql = ", ".join(f"json_extract(payload_json, '$.{m}')" for m in INJECTED)
    prompts, finals = [], {}
    rows = conn.execute(
        f"SELECT role, run_id, json_extract(payload_json, '$.text'), json_extract(payload_json, '$.createdBy'), {marker_sql}"
        f" FROM {T}messages WHERE thread_id = ? ORDER BY created_at, message_id",
        (thread_id,),
    )
    first_user = True
    for role, run_id, text, author, *markers in rows:
        text = text if isinstance(text, str) else ""
        if role == "user":
            human = author == "user" and not any(markers)
            if (human or (first_user and created_by == "agent")) and text.strip():
                prompts.append(text)
            first_user = False
        elif role == "assistant" and text.strip():
            finals[run_id] = text

    tokens = {"input": 0, "cached": 0, "output": 0}
    peak = 0
    for used, *counts in conn.execute(
        "SELECT json_extract(payload_json, '$.tokenUsage.usedTokens'), json_extract(payload_json, '$.turnTokenUsage.inputTokens'),"
        " json_extract(payload_json, '$.turnTokenUsage.cachedInputTokens'), json_extract(payload_json, '$.turnTokenUsage.outputTokens')"
        f" FROM {T}provider_turns WHERE thread_id = ?",
        (thread_id,),
    ):
        peak = max(peak, used or 0)
        for key, value in zip(tokens, counts):
            tokens[key] += value or 0

    native = ""
    for driver, native_id in conn.execute(
        "SELECT json_extract(payload_json, '$.nativeThreadRef.driver'), json_extract(payload_json, '$.nativeThreadRef.nativeId')"
        f" FROM {T}provider_threads WHERE thread_id = ? ORDER BY first_run_ordinal",
        (thread_id,),
    ):
        if native_id:
            native = f"{DRIVERS.get(driver, driver)}:{native_id}"
            break

    delegated = [
        {"title": title or "", "model": model or "", "status": status or ""}
        for child, status, title, model in conn.execute(
            "SELECT child_thread_id, status, json_extract(payload_json, '$.title'), json_extract(payload_json, '$.model')"
            f" FROM {T}subagents WHERE thread_id = ? AND origin = 'app_owned' ORDER BY started_at",
            (thread_id,),
        )
        if child in with_runs
    ]
    turn_finals = [finals[run] for run in runs if run in finals]
    if not turn_finals and None in finals:
        turn_finals = [finals[None]]
    return {
        "cwd": payload.get("worktreePath") or "",
        "model": (payload.get("modelSelection") or {}).get("model") or "",
        "pr_urls": prs,
        "prompts": prompts,
        "turn_finals": turn_finals,
        "turns": len(runs),
        "tokens": tokens if any(tokens.values()) else {},
        "peak_context": peak,
        "delegated": delegated,
        "native_ref": native,
        "branch": payload.get("branch") or "",
        "created_by": created_by,
    }
