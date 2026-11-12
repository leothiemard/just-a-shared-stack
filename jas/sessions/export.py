"""Write digests of finished sessions into the brain, and list them."""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

from jas import brain, paths
from jas.sessions import SessionRecord, digest, scrub, sources

def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def state_path(root: Path) -> Path:
    return paths.cache_dir() / f"sessions-{paths.brain_key(root)}.json"


def load_state(root: Path) -> dict:
    """`{"horizon": iso or None, "sessions": {id: updated}}`; anything else reads as empty."""
    try:
        state = json.loads(state_path(root).read_text())
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict) or not isinstance(state.get("sessions"), dict):
        return {"horizon": None, "sessions": {}}
    return state


def collect(cfg: dict, changed_after: dt.datetime | None) -> tuple[list[SessionRecord], bool]:
    """Records from every configured source, and whether every source was read.

    A failing source is one warning, never an error."""
    out, complete = [], True
    for source in sources(cfg["sessions"]):
        ok, reason = source.available()
        if not ok:
            warn(f"{source.name} source disabled: {reason}")
            complete = False
            continue
        try:
            out += list(source.records(changed_after))
        except Exception as error:  # a broken store must never break sync
            warn(f"{source.name} source disabled: {error}")
            complete = False
    return out, complete


def run(cfg: dict, root: Path, *, everything: bool = False, since: str = "", dry_run: bool = False) -> dict:
    """Export finished top-level sessions. Returns counts and the written paths.

    Sessions updated before the last run's horizon (its time minus the idle window)
    were final then, so incremental runs ask sources only for newer ones."""
    state = load_state(root)
    seen = state["sessions"]
    now = dt.datetime.now(dt.timezone.utc)
    idle = dt.timedelta(minutes=cfg["sessions"]["idle_minutes"])
    excluded = {p.lower() for p in cfg["sessions"]["exclude_projects"]}
    horizon = None if everything or not state.get("horizon") else dt.datetime.fromisoformat(state["horizon"])
    records, complete = collect(cfg, horizon)
    children: dict[str, int] = {}
    for rec in records:
        if rec.parent_id:
            children[rec.parent_id] = children.get(rec.parent_id, 0) + 1
    result = {"written": [], "unchanged": 0, "skipped": 0}
    for rec in records:
        stamp = digest.iso(rec.updated)
        if rec.parent_id or rec.project.lower() in excluded or now - rec.updated < idle or (since and rec.updated.date().isoformat() < since):
            result["skipped"] += 1
            continue
        rel = digest.relpath(rec, cfg["box"])
        if not everything and seen.get(rec.id) == stamp and (root / rel).exists():
            result["unchanged"] += 1
            continue
        if not rec.subagent_count:
            rec.subagent_count = children.get(rec.id, 0)
        result["written"].append(rel)
        if not dry_run:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(digest.render(scrub.record(rec), cfg["box"], cfg["zone"]))
            seen[rec.id] = stamp
    if not dry_run:
        if complete and not since:
            state["horizon"] = (now - idle).isoformat()
        state_path(root).parent.mkdir(parents=True, exist_ok=True)
        state_path(root).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return result


def numbers(text: str) -> dict[str, int]:
    return {k: int(v) for k, v in re.findall(r"(\w+):\s*(\d+)", text)}


def listing(root: Path, since: str = "", box: str = "", project: str = "") -> list[dict]:
    rows = []
    for path in sorted((root / "Sessions").glob("*/*/*.md")):
        meta, _ = brain.frontmatter(path.read_text(errors="replace"))
        date = str(meta.get("updated", ""))[:10]
        if (since and date < since) or (box and meta.get("box") != box) or (project and str(meta.get("project", "")).lower() != project.lower()):
            continue
        tokens = numbers(str(meta.get("tokens", "")))
        rows.append({
            "date": date, "box": meta.get("box", ""), "project": meta.get("project", ""), "title": meta.get("title", ""),
            "turns": int(meta.get("turns") or 0), "tokens": sum(tokens.values()), "path": path.relative_to(root).as_posix(),
            "input": tokens.get("input", 0), "cached": tokens.get("cached", 0), "output": tokens.get("output", 0),
            "subagents": int(meta.get("subagents") or 0), "peak_context": int(meta.get("peak_context") or 0),
        })
    return sorted(rows, key=lambda r: (r["date"], r["path"]))

