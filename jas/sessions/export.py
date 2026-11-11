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


def collect(cfg: dict) -> list[SessionRecord]:
    """Records from every configured source; a failing source is one warning, never an error."""
    out = []
    for source in sources(cfg["sessions"]["sources"]):
        ok, reason = source.available()
        if not ok:
            warn(f"{source.name} source disabled: {reason}")
            continue
        try:
            out += list(source.records())
        except Exception as error:  # a broken store must never break sync
            warn(f"{source.name} source disabled: {error}")
    return out


def run(cfg: dict, root: Path, *, everything: bool = False, since: str = "", dry_run: bool = False) -> dict:
    """Export finished top-level sessions. Returns counts and the written paths."""
    state_file = state_path(root)
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        state = {}
    now = dt.datetime.now(dt.timezone.utc)
    idle = dt.timedelta(minutes=cfg["sessions"]["idle_minutes"])
    excluded = {p.lower() for p in cfg["sessions"]["exclude_projects"]}
    records = collect(cfg)
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
        if not everything and state.get(rec.id) == stamp and (root / rel).exists():
            result["unchanged"] += 1
            continue
        if not rec.subagent_count:
            rec.subagent_count = children.get(rec.id, 0)
        result["written"].append(rel)
        if not dry_run:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(digest.render(scrub.record(rec), cfg["box"], cfg["zone"]))
            state[rec.id] = stamp
    if not dry_run:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return result


def listing(root: Path, since: str = "", box: str = "", project: str = "") -> list[dict]:
    rows = []
    for path in sorted((root / "Sessions").glob("*/*/*.md")):
        meta, _ = brain.frontmatter(path.read_text(errors="replace"))
        date = str(meta.get("updated", ""))[:10]
        if (since and date < since) or (box and meta.get("box") != box) or (project and str(meta.get("project", "")).lower() != project.lower()):
            continue
        tokens = sum(int(n) for n in re.findall(r":\s*(\d+)", str(meta.get("tokens", ""))))
        rows.append({
            "date": date, "box": meta.get("box", ""), "project": meta.get("project", ""), "title": meta.get("title", ""),
            "turns": int(meta.get("turns") or 0), "tokens": tokens, "path": path.relative_to(root).as_posix(),
        })
    return sorted(rows, key=lambda r: (r["date"], r["path"]))
