"""Build a small T3 Code store with the pinned projection tables, for tests."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import sqlite3
from pathlib import Path

T = "orchestration_v2_projection_"
SCHEMA = f"""
CREATE TABLE {T}threads (thread_id TEXT PRIMARY KEY, project_id TEXT, title TEXT, created_at TEXT, updated_at TEXT,
  archived_at TEXT, deleted_at TEXT, payload_json TEXT, provider_instance_id TEXT);
CREATE TABLE {T}runs (run_id TEXT PRIMARY KEY, thread_id TEXT, ordinal INTEGER, status TEXT, payload_json TEXT);
CREATE INDEX {T}runs_thread_ordinal_idx ON {T}runs (thread_id, ordinal);
CREATE TABLE {T}messages (message_id TEXT PRIMARY KEY, thread_id TEXT, run_id TEXT, role TEXT, created_at TEXT, payload_json TEXT);
CREATE INDEX {T}messages_thread_created_idx ON {T}messages (thread_id, created_at, message_id);
CREATE TABLE {T}provider_turns (provider_turn_id TEXT PRIMARY KEY, thread_id TEXT, payload_json TEXT);
CREATE TABLE {T}provider_threads (provider_thread_id TEXT PRIMARY KEY, thread_id TEXT, first_run_ordinal INTEGER, payload_json TEXT);
CREATE TABLE {T}subagents (subagent_id TEXT PRIMARY KEY, thread_id TEXT, child_thread_id TEXT, origin TEXT, status TEXT,
  started_at TEXT, payload_json TEXT);
CREATE TABLE projection_projects (project_id TEXT PRIMARY KEY, title TEXT, workspace_root TEXT, deleted_at TEXT);
CREATE TABLE projection_thread_pull_requests (thread_id TEXT, url TEXT);
"""
NOW = dt.datetime.now(dt.timezone.utc)


def stamp(minutes_ago: float) -> str:
    return (NOW - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Store:
    """A fixture store. Times are minutes before now; the newest message lands at `updated`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.counter = 0
        with self.db() as conn:
            conn.executescript(SCHEMA)
        (path.parent / "environment-id").write_text("env-fixture\n")

    @contextlib.contextmanager
    def db(self):
        conn = sqlite3.connect(str(self.path))
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def next_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter}"

    def project(self, project_id: str, title: str, root: str, deleted: bool = False) -> None:
        with self.db() as conn:
            conn.execute("INSERT INTO projection_projects VALUES (?, ?, ?, ?)", (project_id, title, root, stamp(1) if deleted else None))

    def thread(self, thread_id: str, *, project: str = "p-app", title: str = "", parent: str = "", relationship: str = "subagent",
               created_by: str = "user", age: float = 600, updated: float = 120, deleted: bool = False, prs=(), branch: str = "",
               model: str = "model-a") -> None:
        payload = {
            "createdBy": created_by, "branch": branch or None, "worktreePath": None,
            "modelSelection": {"model": model}, "pullRequests": [{"url": u} for u in prs],
            "lineage": {"parentThreadId": parent or None, "relationshipToParent": relationship if parent else None},
        }
        with self.db() as conn:
            conn.execute(
                f"INSERT INTO {T}threads VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 'claudeAgent')",
                (thread_id, project, title or f"Thread {thread_id}", stamp(age), stamp(updated), stamp(updated) if deleted else None, json.dumps(payload)),
            )

    def touch(self, thread_id: str, updated: float) -> None:
        with self.db() as conn:
            conn.execute(f"UPDATE {T}threads SET updated_at = ? WHERE thread_id = ?", (stamp(updated), thread_id))

    def message(self, thread_id: str, role: str, text: str, run_id: str | None, at: float, **extra) -> None:
        payload = dict({"text": text, "createdBy": "user" if role == "user" else "agent"}, **extra)
        with self.db() as conn:
            conn.execute(f"INSERT INTO {T}messages VALUES (?, ?, ?, ?, ?, ?)", (self.next_id("m"), thread_id, run_id, role, stamp(at), json.dumps(payload)))

    def turn(self, thread_id: str, prompt: str | None, answer: str, *, at: float = 200, status: str = "completed",
             tokens=(100, 50, 10), used: int = 0, **prompt_extra) -> str:
        """One run: an optional user prompt, a progress message and the final answer."""
        with self.db() as conn:
            (ordinal,) = conn.execute(f"SELECT count(*) FROM {T}runs WHERE thread_id = ?", (thread_id,)).fetchone()
            run_id = self.next_id("run")
            conn.execute(f"INSERT INTO {T}runs VALUES (?, ?, ?, ?, '{{}}')", (run_id, thread_id, ordinal + 1, status))
            usage = {"turnTokenUsage": dict(zip(("inputTokens", "cachedInputTokens", "outputTokens"), tokens))}
            if used:
                usage["tokenUsage"] = {"usedTokens": used}
            conn.execute(f"INSERT INTO {T}provider_turns VALUES (?, ?, ?)", (self.next_id("pt"), thread_id, json.dumps(usage)))
        if prompt is not None:
            self.message(thread_id, "user", prompt, run_id, at + 0.03, **prompt_extra)
        self.message(thread_id, "assistant", "Working on it.", run_id, at + 0.02)
        self.message(thread_id, "assistant", answer, run_id, at + 0.01)
        return run_id

    def native(self, thread_id: str, driver: str, native_id: str) -> None:
        with self.db() as conn:
            conn.execute(f"INSERT INTO {T}provider_threads VALUES (?, ?, 1, ?)",
                         (self.next_id("pth"), thread_id, json.dumps({"nativeThreadRef": {"driver": driver, "nativeId": native_id}})))

    def subagent(self, parent: str, child: str, origin: str, status: str = "completed", title: str = "", model: str = "model-b") -> None:
        with self.db() as conn:
            conn.execute(f"INSERT INTO {T}subagents VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (self.next_id("sa"), parent, child, origin, status, stamp(300), json.dumps({"title": title, "model": model})))

    def pull_request(self, thread_id: str, url: str) -> None:
        with self.db() as conn:
            conn.execute("INSERT INTO projection_thread_pull_requests VALUES (?, ?)", (thread_id, url))

    def drop_column(self, table: str, column: str) -> None:
        with self.db() as conn:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def standard(path: Path) -> Store:
    """The fixture the tests share: one thread of each kind the exporter must handle."""
    s = Store(path)
    s.project("p-app", "Example App", "/work/example-app")
    s.project("p-scratch", "No project", "/home/someone/.t3/scratch")
    s.project("p-gone", "Old thing", "/work/old-thing", deleted=True)

    s.thread("top", title="Fix the deploy hook", prs=["https://example.invalid/pr/1"], branch="fix/deploy-hook")
    s.pull_request("top", "https://example.invalid/pr/2")
    s.native("top", "claudeAgent", "native-session-1")
    s.turn("top", "Fix the deploy hook please", "Looked at the hook; the env is missing.", at=400, used=150_000)
    s.turn("top", "Child result: looks fine", "Child finished.", at=300, delegatedCompletion={"taskId": "t1"})
    s.message("top", "user", "PR #1 checks passed", None, 250, notification={"kind": "pr"})
    s.turn("top", "Ship it", "Fixed the deploy hook and opened PR 1.", at=200, used=230_000)

    s.thread("app-child", parent="top", created_by="agent", title="Review the hook fix")
    s.turn("app-child", "Review this diff", "Looks fine.", at=350, created_by="agent", senderThreadId="top")
    s.subagent("top", "app-child", "app_owned", title="Review the hook fix", model="model-b")
    s.thread("native-child", parent="top", created_by="agent", title="Explore the repo")
    s.message("native-child", "user", "Explore the repo", None, 340, createdBy="agent")
    s.subagent("top", "native-child", "provider_native", title="Explore the repo")
    s.thread("grandchild", parent="app-child", created_by="agent", title="Nested helper")
    s.subagent("app-child", "grandchild", "provider_native")

    s.thread("agent-launched", created_by="agent", title="Launched by an agent")
    s.turn("agent-launched", "Brief from another thread: rename the event", "Renamed it.", at=500, created_by="agent", senderThreadId="top")
    s.turn("agent-launched", "Also check the archive", "Archive checked.", at=450)

    s.thread("deleted", deleted=True)
    s.turn("deleted", "Deleted work", "Gone.", at=500)
    s.thread("active", updated=5)
    s.turn("active", "Still going", "Partial answer.", at=6)
    s.thread("failed-only")
    s.turn("failed-only", "Try this", "Error.", at=500, status="failed")

    s.thread("secret", title="Set up the API client")
    s.turn("secret", "Use API_KEY=sk-livesecretvalue1234567890 for the client", "Configured the client.", at=500)

    s.thread("long", title="Long refactor", age=2000)
    for n in range(20):
        prompt = f"Prompt number {n}" if n % 2 == 0 else None
        s.turn("long", prompt, f"Answer for turn {n}", at=1900 - n * 10)

    s.thread("imported", title="Imported from v1", age=50000, updated=40000)
    s.message("imported", "user", "Old question", None, 40010)
    s.message("imported", "assistant", "Old progress", None, 40009)
    s.message("imported", "assistant", "Old answer", None, 40008)
    s.message("imported", "user", "Follow-up", None, 40005)
    s.message("imported", "assistant", "Final old answer", None, 40001)
    s.thread("empty-import", title="Imported without answers", updated=40000)
    s.message("empty-import", "user", "Nobody answered", None, 40010)

    s.thread("scratchy", project="p-scratch", title="Quick scratch question")
    s.turn("scratchy", "What is a monad", "A monoid in the category of endofunctors.", at=500)
    return s
