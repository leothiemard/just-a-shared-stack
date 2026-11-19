"""What each T3 Code thread is doing, read strictly read-only from `~/.t3/userdata/statev2.sqlite`.

T3's own status calls a thread `completed` both when it has finished and when its turn ended while a
delegate or a background command still works for it, so a watcher needs more than the run status:

- `working`   a run is going.
- `blocked`   a request waits for the owner (an approval, a question, a secret, a sign-in link).
- `delegated`  no run is going, but a child agent of this thread still is.
- `background` no run is going, but T3 lists a background command or task the thread still waits on.
- `failed`     the last run failed; `failure` is `memory`, `network`, `limit`, `auth` or `other`. It wins over
               `delegated` and `background`: what a killed run had started is usually dead with it.
- `stopped`    the last run was interrupted or cancelled, by a person or another agent.
- `stuck`      a run queued after the last one that ran, with nothing going. T3 holds a thread's queue
               after a failed run until a person writes in the app; a message from the CLI starts a new
               run instead, and a queued run older than the last one that ran is passed over.
- `ended`      the last run completed and nothing is pending.

The tables are internal to T3 Code. Every table and column read here is pinned and checked first,
as in `jass.sessions.t3`.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from pathlib import Path

from jass import JassError, paths
from jass.sessions import t3

T = t3.T
PINNED = {
    f"{T}threads": ("thread_id", "title", "updated_at", "archived_at", "deleted_at", "payload_json", "active_provider_thread_id"),
    f"{T}runs": ("run_id", "thread_id", "ordinal", "status", "requested_at", "completed_at"),
    f"{T}turn_items": ("thread_id", "run_id", "type", "status", "updated_at", "payload_json"),
    f"{T}subagents": ("thread_id", "child_thread_id", "status"),
    f"{T}provider_threads": ("provider_thread_id", "payload_json"),
    f"{T}runtime_requests": ("thread_id", "kind", "status"),
}
GOING = ("running", "starting", "preparing", "waiting")  # T3's own test for an active run
AUTH_WORDS = ("authenticat", "unauthorized", "not logged in", "log in again", "sign in again")
NETWORK_WORDS = ("reach the api", "eai_again", "enotfound", "econnreset", "econnrefused", "etimedout", "network", "dns", "socket", "fetch failed")


def store_path(cfg: dict) -> Path:
    configured = cfg.get("sessions", {}).get("t3_store", "")
    return paths.expand(configured) if configured else t3.default_store()


def drift(conn: sqlite3.Connection) -> str:
    problems = []
    for table, columns in PINNED.items():
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            problems.append(f"table {table} missing")
        problems += [f"column {table}.{c} missing" for c in columns if have and c not in have]
    return "; ".join(problems)


@contextlib.contextmanager
def opened(store: Path):
    if not store.is_file():
        raise JassError(f"No T3 Code store at {paths.tilde(store)}. Is T3 Code installed on this box?")
    try:
        conn = t3.connect(store)
        conn.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.Error as error:
        raise JassError(f"Cannot open {paths.tilde(store)}: {error}")
    try:
        problem = drift(conn)
        if problem:
            raise JassError(f"T3 Code's store changed shape ({problem}); `jass night` must be updated before it can read it.")
        yield conn
    finally:
        conn.close()


def failure_kind(failure: dict) -> str:
    """`memory`, `network`, `limit`, `auth` or `other`, from the `failure` T3 records on a failed run's error item."""
    kind, code, text = str(failure.get("class", "")), str(failure.get("code", "")).lower(), str(failure.get("message", "")).lower()
    if code == "oom_kill" or "out of memory" in text:
        return "memory"
    if kind == "usage_limit" or any(word in text for word in ("usage limit", "session limit", "rate limit")):
        return "limit"
    if re.search(r"(^|\D)40[13](\D|$)", f"{code} {text}") or any(word in text for word in AUTH_WORDS):
        return "auth"
    if kind == "transport_error" or any(word in text for word in NETWORK_WORDS):
        return "network"
    return "other"


def classify(runs: list[tuple], children: int, background: list[str], requests: list[str], failure: dict) -> tuple[str, str]:
    """(state, detail) from a thread's runs as (ordinal, status), oldest first, and what is pending around them."""
    if requests:
        return "blocked", "waits for the owner: " + ", ".join(sorted(set(r.replace("_", " ").replace("-", " ") for r in requests)))
    if any(status in GOING for _, status in runs):
        return "working", ""
    settled = [(ordinal, status) for ordinal, status in runs if status != "queued"]
    ordinal, status = settled[-1] if settled else (0, "")
    if status == "failed":
        message = " ".join(str(failure.get("message", "")).split())[:120]
        return "failed", f"run {ordinal} failed" + (f": {message}" if message else "")
    if children:
        return "delegated", f"{children} child agent{'s' if children > 1 else ''} still working"
    if background:
        return "background", "waits on " + ", ".join(sorted(set(background)))
    if status in ("interrupted", "cancelled"):
        return "stopped", f"run {ordinal} was {status}"
    if runs and runs[-1][1] == "queued" and runs[-1][0] > ordinal:
        return "stuck", f"run {runs[-1][0]} is queued with nothing running ahead of it"
    if not settled:
        return "ended", "no runs yet"
    return "ended", f"run {ordinal} {status}"


def loads(text) -> dict:
    try:
        value = json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def snapshot(store: Path, only: list[str] | None = None) -> list[dict]:
    """One dict per top-level thread that is not archived or deleted: id, title, state, detail, failure, reset, since, run."""
    out = []
    with opened(store) as conn:
        rows = conn.execute(
            f"SELECT thread_id, title, active_provider_thread_id, json_extract(payload_json, '$.lineage.parentThreadId') FROM {T}threads"
            f" WHERE deleted_at IS NULL AND archived_at IS NULL"
        ).fetchall()
        for thread_id, title, provider_thread, parent in rows:
            if parent or (only is not None and thread_id not in only):
                continue
            runs = conn.execute(f"SELECT ordinal, status, requested_at, completed_at FROM {T}runs WHERE thread_id = ? ORDER BY ordinal", (thread_id,)).fetchall()
            (children,) = conn.execute(f"SELECT count(*) FROM {T}subagents WHERE thread_id = ? AND status IN {GOING}", (thread_id,)).fetchone()
            requests = [kind for kind, in conn.execute(f"SELECT kind FROM {T}runtime_requests WHERE thread_id = ? AND status = 'pending'", (thread_id,))]
            requests += ["secret" for _ in conn.execute(
                f"SELECT 1 FROM {T}turn_items WHERE thread_id = ? AND type = 'secret_request' AND json_extract(payload_json, '$.secretStatus') = 'pending'", (thread_id,))]
            pending = conn.execute(f"SELECT payload_json FROM {T}provider_threads WHERE provider_thread_id = ?", (provider_thread,)).fetchone()
            tasks = loads(pending[0]).get("pendingBackgroundTasks") if pending else None
            background = [str(task.get("kind") or "task").replace("_", " ") for task in tasks or [] if isinstance(task, dict)]
            failure = {}
            last = [run for run in runs if run[1] != "queued"]
            if last and last[-1][1] == "failed":
                found = conn.execute(
                    f"SELECT json_extract(payload_json, '$.failure') FROM {T}turn_items WHERE thread_id = ? AND type = 'error' AND status = 'failed'"
                    f" ORDER BY updated_at DESC LIMIT 1", (thread_id,)).fetchone()
                failure = loads(found[0]) if found else {}
            state, detail = classify([(run[0], run[1]) for run in runs], children, background, requests, failure)
            since = (last[-1][3] or last[-1][2]) if last else ""
            out.append({"id": thread_id, "title": title or "", "state": state, "detail": detail, "since": since or "", "run": runs[-1][0] if runs else 0,
                        "failure": failure_kind(failure) if state == "failed" else "", "reset": str(failure.get("resetAt") or "") if state == "failed" else ""})
    return out


def going(store: Path) -> int:
    """How many agents have a run going now, children included: each is its own process."""
    with opened(store) as conn:
        return conn.execute(f"SELECT count(DISTINCT thread_id) FROM {T}runs WHERE status IN {GOING}").fetchone()[0]


def pick(threads: list[dict], wanted: list[str]) -> list[dict]:
    """The threads named by id, id prefix or a piece of the title; each name must match exactly one."""
    chosen = []
    for name in wanted:
        exact = [t for t in threads if t["id"] == name]
        found = exact or [t for t in threads if t["id"].startswith(name) or name.lower() in t["title"].lower()]
        if len(found) != 1:
            names = "; ".join(f"{t['title']} ({t['id'][:24]})" for t in found[:6])
            raise JassError(f"`{name}` matches {len(found)} threads" + (f": {names}" if found else "") + ". Use a longer piece of the title or the thread id.")
        if found[0] not in chosen:
            chosen.append(found[0])
    return chosen
