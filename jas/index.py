"""SQLite FTS5 index of the brain, ranked recall, and `show`."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

from jas import JasError, brain, paths

SCHEMA = "1"
KIND_WEIGHT = {"lesson": 1.5, "hub": 1.3, "note": 1.2, "review": 1.1, "log": 1.0, "box": 0.9, "session": 0.8, "other": 1.0}
KINDS = tuple(KIND_WEIGHT)
HALF_LIFE_DAYS = 180
RECENCY_FLOOR = 0.6
SNIPPET_CHARS = 240
COMMENT = re.compile(r"<!--.*?-->", re.S)
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
COLUMNS = "path, heading, trail, line, kind, project, box, date, body"


def fts5_available() -> bool:
    with contextlib.closing(sqlite3.connect(":memory:")) as conn:
        try:
            conn.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
            return True
        except sqlite3.OperationalError:
            return False


def db_path(root: Path) -> Path:
    return paths.cache_dir() / f"index-{paths.brain_key(root)}.sqlite"


def connect(root: Path) -> sqlite3.Connection:
    if not fts5_available():
        raise JasError("This Python's SQLite lacks FTS5. Use a Python build whose sqlite3 includes FTS5 (python.org or Homebrew builds do).")
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    if not row or row[0] != SCHEMA:
        conn.executescript(
            "DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS chunks;"
            "CREATE TABLE files (path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER);"
            "CREATE VIRTUAL TABLE chunks USING fts5(heading, body, path UNINDEXED, trail UNINDEXED, line UNINDEXED,"
            " kind UNINDEXED, project UNINDEXED, box UNINDEXED, date UNINDEXED, tokenize = 'porter unicode61');"
        )
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (SCHEMA,))
        conn.commit()
    refresh(conn, root)
    return conn


def markdown_files(root: Path) -> dict[str, Path]:
    found = {}
    for path in root.rglob("*.md"):
        rel = path.relative_to(root)
        if path.is_file() and not any(part in brain.SKIP_DIRS for part in rel.parts):
            found[rel.as_posix()] = path
    return found


def refresh(conn: sqlite3.Connection, root: Path) -> None:
    """Reindex files whose (mtime_ns, size) changed; drop deleted ones."""
    known = {path: (mtime, size) for path, mtime, size in conn.execute("SELECT path, mtime_ns, size FROM files")}
    current = markdown_files(root)
    for rel in set(known) - set(current):
        conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
        conn.execute("DELETE FROM files WHERE path = ?", (rel,))
    for rel, path in current.items():
        stat = path.stat()
        if known.get(rel) == (stat.st_mtime_ns, stat.st_size):
            continue
        conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
        for chunk in chunks(rel, path.read_text(errors="replace")):
            conn.execute(f"INSERT INTO chunks ({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [chunk[c] for c in COLUMNS.split(", ")])
        conn.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (rel, stat.st_mtime_ns, stat.st_size))
    conn.commit()


def file_meta(rel: str, meta: dict) -> dict:
    parts = rel.split("/")
    top = parts[0] if len(parts) > 1 else ""
    kind, project, box = "other", "", ""
    if top == "Projects" and len(parts) >= 3:
        project = parts[1]
        if len(parts) == 3 and parts[2].lower() == f"{project}.md".lower():
            kind = "hub"
        elif len(parts) == 3 and parts[2] == "Log.md":
            kind = "log"
        else:
            kind = "note"
    elif top in ("Lessons", "Reviews"):
        kind = top[:-1].lower()
    elif top == "Sessions":
        kind, box = "session", parts[1]
    elif top == "Boxes":
        kind, box = "box", Path(parts[-1]).stem
    project = meta.get("project") or project
    box = meta.get("box") or box
    date_text = next((str(meta[k]) for k in ("updated", "date", "started", "created") if meta.get(k)), "")
    found = DATE.search(date_text) or DATE.search(parts[-1])
    return {"path": rel, "kind": kind, "project": project, "box": box, "date": found.group(0) if found else ""}


def provenance(body: str, base: dict) -> dict:
    """Date, box and project from a log or lesson trailer comment, over the file defaults."""
    out = dict(base)
    for comment in re.findall(r"<!--(.*?)-->", body, re.S):
        date = DATE.match(comment.strip())
        box = re.search(r"\bbox ([\w.-]+)", comment)
        project = re.search(r"\bproject ([^\s·]+)", comment)
        if date:
            out["date"] = date.group(0)
        if box:
            out["box"] = box.group(1)
        if project:
            out["project"] = project.group(1)
    return out


def chunks(rel: str, text: str) -> list[dict]:
    meta, start = brain.frontmatter(text)
    base = file_meta(rel, meta)
    lines = text.split("\n")
    marks = [(i, level, t) for i, level, t in brain.headings(lines) if level <= 3 and i >= start]
    sections = [(start, 0, str(meta.get("title", "")))] + marks
    out, trail = [], {}
    for n, (i, level, heading) in enumerate(sections):
        end = sections[n + 1][0] if n + 1 < len(sections) else len(lines)
        raw = "\n".join(lines[i + (1 if level else 0):end])
        body = COMMENT.sub("", raw).strip()
        if level:
            trail = {k: v for k, v in trail.items() if k < level}
            trail[level] = heading
        if not body and level < 2:
            continue
        row = provenance(raw, base)
        if base["kind"] == "log" and DATE.match(heading):
            row["date"] = heading[:10]
        row.update(heading=heading, trail=" › ".join(trail[k] for k in sorted(trail)) if level else "", line=i, body=body)
        out.append(row)
    return out


def parse_since(text: str) -> str:
    match = re.fullmatch(r"(\d+)([dwmy])", text)
    if match:
        days = int(match.group(1)) * {"d": 1, "w": 7, "m": 30, "y": 365}[match.group(2)]
        return (dt.date.today() - dt.timedelta(days=days)).isoformat()
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError:
        raise JasError(f"Cannot read --since {text!r}. Use a span like 5w, 10d, 3m, 1y or a date like 2026-10-01.")


def recency(date: str) -> float:
    if not date:
        return 1.0
    age = (dt.date.today() - dt.date.fromisoformat(date)).days
    return max(RECENCY_FLOOR, 0.5 ** (max(age, 0) / HALF_LIFE_DAYS))


def search(conn: sqlite3.Connection, query: str, project: str = "", box: str = "", kinds: list[str] | None = None, since: str = "") -> list[dict]:
    words = sorted(brain.terms(query))
    if not words:
        return []
    where, args = ["chunks MATCH ?"], [" OR ".join(f'"{w}"' for w in words)]
    if kinds:
        where.append(f"kind IN ({', '.join('?' * len(kinds))})")
        args += kinds
    if project:
        where.append("lower(project) = lower(?)")
        args.append(project)
    if box:
        where.append("box = ?")
        args.append(box)
    if since:
        where.append("date != '' AND date >= ?")
        args.append(since)
    sql = f"SELECT {COLUMNS}, bm25(chunks, 4.0, 1.0) FROM chunks WHERE {' AND '.join(where)}"
    hits = []
    for row in conn.execute(sql, args):
        hit = dict(zip(COLUMNS.split(", "), row[:-1]))
        hit["score"] = -row[-1] * KIND_WEIGHT.get(hit["kind"], 1.0) * recency(hit["date"])
        hit["terms"] = words
        hits.append(hit)
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits


def snippet(hit: dict) -> str:
    text = " ".join(hit["body"].split())
    if len(text) <= SNIPPET_CHARS:
        return text
    lower = text.lower()
    found = [p for p in (lower.find(w[:5]) for w in hit["terms"]) if p >= 0]
    start = max(0, min(found) - 60) if found else 0
    piece = text[start:start + SNIPPET_CHARS - 2]
    return ("…" if start else "") + piece + "…"


def hit_lines(n: int, hit: dict) -> str:
    label = f"{hit['path']} § {hit['heading']}" if hit["heading"] else hit["path"]
    meta = [hit["kind"]]
    if hit["project"] and hit["kind"] != "hub":
        meta.append(hit["project"])
    if hit["box"]:
        meta.append(hit["box"])
    if hit["date"] and hit["date"] not in hit["heading"]:
        meta.append(hit["date"])
    return f"[{n}] {label}   {' · '.join(meta)}\n    {snippet(hit)}\n"


def refs_path(root: Path) -> Path:
    return paths.cache_dir() / f"recall-{paths.brain_key(root)}.json"


def recall(conn: sqlite3.Connection, root: Path, query: str, *, limit: int, budget: int, status: str, as_json: bool, **filters) -> str:
    hits = search(conn, query, **filters)
    if not hits:
        return json.dumps({"total": 0, "hits": []}) + "\n" if as_json else f"No hits for {query!r}.\n"
    shown, body = [], ""
    for hit in hits[:limit]:
        line = hit_lines(len(shown) + 1, hit)
        if shown and len((body + line).encode()) / 4 > budget:
            break
        shown.append(hit)
        body += line
    refs_path(root).write_text(json.dumps([{"path": h["path"], "line": h["line"], "heading": h["heading"]} for h in shown]))
    if as_json:
        keys = ("path", "heading", "trail", "kind", "project", "box", "date")
        rows = [dict({k: h[k] for k in keys}, ref=n, line=h["line"] + 1, score=round(h["score"], 3), snippet=snippet(h)) for n, h in enumerate(shown, 1)]
        return json.dumps({"total": len(hits), "hits": rows}, indent=2) + "\n"
    footer = "Expand with: jas show 1\n"
    tokens = (len(body.encode()) + len(footer) + 80) // 4
    return f"{len(shown)} of {len(hits)} hits · ~{tokens} tokens · {status}\n{body}{footer}"


def section(lines: list[str], start: int) -> str:
    """The heading at `start` and everything up to the next heading of the same or higher level."""
    marks = brain.headings(lines)
    level = next((lv for i, lv, _ in marks if i == start), 0)
    end = next((i for i, lv, _ in marks if i > start and (level == 0 or lv <= level)), len(lines))
    return "\n".join(lines[start:end]).rstrip() + "\n"


def show(root: Path, ref: str, max_bytes: int) -> str:
    heading, line = "", None
    if ref.isdigit():
        try:
            saved = json.loads(refs_path(root).read_text())
            entry = saved[int(ref) - 1]
        except (OSError, ValueError, IndexError):
            raise JasError(f"No hit {ref} from the last recall. Run `jas recall <query>` first.")
        rel, line = entry["path"], entry["line"]
    else:
        rel, _, heading = ref.partition("#")
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise JasError(f"No file {rel!r} in the brain. Use a path from `jas recall` output.")
    text = path.read_text(errors="replace")
    lines = text.split("\n")
    if heading:
        line = next((i for i, _, t in brain.headings(lines) if t.lower() == heading.strip().lower()), None)
        if line is None:
            raise JasError(f"No heading {heading!r} in {rel}. Check the spelling, or show the whole file with `jas show {rel}`.")
    out = text if line is None else section(lines, line)
    full = f"jas show {ref} --max-bytes {len(out.encode())}"
    return f"{rel}\n\n" + brain.clip(out, max_bytes, full)
