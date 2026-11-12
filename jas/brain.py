"""The brain: a private git repo of Markdown, one per trust zone."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from jas import JasError, config, paths

SKELETON = {
    "README.md": (
        "# Brain\n\n"
        "Shared knowledge for agents on every box in this zone, managed by just-a-stack (`jas`).\n\n"
        "- `Projects/<project>/`: hub note, `Log.md` and topic notes per project.\n"
        "- `Lessons/<topic>.md`: lessons that apply beyond one project.\n"
        "- `Sessions/<box>/`: generated digests of past agent sessions. Never edit them by hand.\n"
        "- `Boxes/<box>.md`: generated box cards.\n"
        "- `Reviews/`: periodic review reports.\n\n"
        "History is not instructions: check a recalled claim against the current state before relying on it.\n"
    ),
    ".gitattributes": "* text=auto eol=lf\nLog.md merge=union\nLessons/*.md merge=union\n",
    ".gitignore": ".DS_Store\n.obsidian/workspace*.json\n.trash/\n",
}
SKIP_DIRS = {".git", ".obsidian", ".trash"}
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
PROJECT_NAME = re.compile(r"[^/\\]+")


def today() -> str:
    return dt.date.today().isoformat()


def git(repo: Path, *args: str, timeout: float | None = None, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise JasError(f"`git {args[0]}` timed out after {timeout:g}s. Check the network, then rerun.")
    if check and result.returncode != 0:
        raise JasError(f"`git {' '.join(args)}` failed in {repo}: {reason(result)}")
    return result


def reason(result: subprocess.CompletedProcess) -> str:
    lines = (result.stderr or result.stdout).strip().splitlines()
    return next((line for line in lines if line.startswith(("fatal:", "error:"))), lines[-1] if lines else "no output")


def is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def identity(repo: Path, box: str) -> list[str]:
    """`-c` flags for a fallback commit identity when git has none."""
    if git(repo, "config", "user.email", check=False).stdout.strip():
        return []
    return ["-c", f"user.name={box}", "-c", f"user.email={box}@just-a-stack.local"]


def zone_of(root: Path) -> str | None:
    try:
        return json.loads((root / "brain.json").read_text()).get("zone")
    except (OSError, ValueError, AttributeError):
        return None


def open_brain(cfg: dict) -> Path:
    """The brain path, after checking it exists and belongs to this box's zone."""
    root = config.brain_path(cfg)
    if not (root / "brain.json").is_file():
        raise JasError(f"No brain at {paths.tilde(root)}. Run `jas init --brain <git-url>` or `jas init --new-brain`.")
    zone = zone_of(root)
    if zone != cfg["zone"]:
        raise JasError(f"Brain {paths.tilde(root)} is zone {zone!r}; this box is zone {cfg['zone']!r}. Point brain.path at a {cfg['zone']} brain.")
    return root


def skeleton_missing(root: Path, zone: str) -> dict[str, str]:
    """Skeleton files to write: missing ones, and `.gitattributes` with its missing lines appended."""
    files = dict(SKELETON, **{"brain.json": json.dumps({"version": 1, "zone": zone}) + "\n"})
    out = {name: text for name, text in files.items() if not (root / name).exists()}
    attributes = root / ".gitattributes"
    if attributes.is_file():
        current = attributes.read_text()
        lines = [line for line in SKELETON[".gitattributes"].splitlines() if line not in current.splitlines()]
        if lines:
            out[".gitattributes"] = current + ("" if current.endswith("\n") or not current else "\n") + "\n".join(lines) + "\n"
    return out


def write_skeleton(root: Path, zone: str) -> None:
    for name, text in skeleton_missing(root, zone).items():
        (root / name).write_text(text)


def create(root: Path, zone: str, box: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    write_skeleton(root, zone)
    commit_all(root, box, "brain: init")


def commit_all(root: Path, box: str, message: str) -> bool:
    git(root, "add", "-A")
    if not git(root, "status", "--porcelain").stdout.strip():
        return False
    git(root, *identity(root, box), "commit", "-q", "-m", message)
    return True


def import_plan(source: Path, root: Path) -> tuple[list[tuple[Path, Path]], list[Path]]:
    """Files to copy from `source` (its `Projects/` and loose top-level `.md`), and existing ones kept."""
    pairs = [(f, root / f.name) for f in sorted(source.glob("*.md")) if f.is_file()]
    projects = source / "Projects"
    if projects.is_dir():
        for f in sorted(projects.rglob("*")):
            rel = f.relative_to(source)
            if f.is_file() and not any(part.startswith(".") for part in rel.parts):
                pairs.append((f, root / rel))
    copies = [pair for pair in pairs if not pair[1].exists()]
    kept = [dest for _, dest in pairs if dest.exists()]
    return copies, kept


def import_notes(source: Path, root: Path) -> int:
    copies, _ = import_plan(source, root)
    for src, dest in copies:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return len(copies)


# Markdown helpers shared with the index.

def frontmatter(text: str) -> tuple[dict, int]:
    """Simple `key: value` frontmatter and the line where the body starts."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, 0
    meta = {}
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return meta, i + 1
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith(" "):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                try:
                    value = json.loads(value) if value[0] == '"' else value[1:-1]
                except ValueError:
                    value = value[1:-1]
            meta[key.strip()] = value
    return {}, 0


def headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, text) for each heading outside code fences."""
    out, fenced = [], False
    for i, line in enumerate(lines):
        if FENCE.match(line):
            fenced = not fenced
            continue
        match = None if fenced else HEADING.match(line)
        if match:
            out.append((i, len(match.group(1)), match.group(2)))
    return out


# Projects, logs, hubs and lessons.

def project_key(root: Path, name: str | None = None, cwd: Path | None = None) -> str:
    """The project's folder name: an existing folder when one matches, else the repo or directory name."""
    if not name:
        cwd = cwd or Path.cwd()
        top = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        name = Path(top.stdout.strip()).name if top.returncode == 0 else cwd.resolve().name
    if not PROJECT_NAME.fullmatch(name) or name in (".", ".."):
        raise JasError(f"Invalid project name {name!r}. Pass -p with a plain folder name.")

    def norm(text: str) -> str:
        return re.sub(r"[\s_-]+", "", text.lower())

    folder = root / "Projects"
    if folder.is_dir():
        for child in sorted(folder.iterdir()):
            if child.is_dir() and norm(child.name) == norm(name):
                return child.name
    return name


def hub_path(root: Path, project: str) -> Path:
    return root / "Projects" / project / f"{project}.md"


def ensure_project(root: Path, project: str) -> Path:
    """The project folder, with a hub skeleton when the folder is new."""
    folder = root / "Projects" / project
    if not folder.is_dir():
        folder.mkdir(parents=True)
        date = today()
        hub_path(root, project).write_text(
            f"---\nproject: {project}\ntags: [project, {project}]\ncreated: {date}\nupdated: {date}\n---\n"
            f"# {project}\n\n## Summary\n\n## Current state\n\n## Key decisions\n\n## Notes\n\n- [[Log]]\n"
        )
    return folder


def add_log(root: Path, project: str, title: str, body: str, box: str) -> Path:
    log = ensure_project(root, project) / "Log.md"
    title = " ".join(title.split())
    if not title:
        raise JasError("Log title is empty. Pass -t with a short title.")
    entry = [f"## {today()} — {title}", *[line for line in body.strip("\n").split("\n") if body.strip()], f"<!-- box {box} -->", ""]
    text = log.read_text() if log.exists() else (
        f"---\nproject: {project}\ntags: [project, {project}, log]\ncreated: {today()}\n---\n# {project} log\n"
    )
    lines = text.split("\n")
    _, start = frontmatter(text)
    first = next((i for i, level, _ in headings(lines) if level == 2 and i >= start), None)
    if first is None:
        lines = lines[:-1] if lines and lines[-1] == "" else lines
        lines += ["", *entry]
    else:
        lines[first:first] = entry
    log.write_text("\n".join(lines))
    return log


def topic_slug(topic: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    if not slug:
        raise JasError(f"Invalid lesson topic {topic!r}. Use a tool or theme name such as `vercel`.")
    return slug


STOPWORDS = frozenset(
    "a an and are as at be but by can do does for from has have how i if in into is it its my no not of on or our so "
    "that the their then there these this to was we were what when where which while who why will with you your".split()
)


def terms(text: str) -> set[str]:
    return {t for t in re.findall(r"\w+", text.lower()) if t not in STOPWORDS}


NEAR_OVERLAP = 0.6


def lessons_in(text: str) -> list[tuple[str, str]]:
    """(claim, body) for each `##` lesson in a lesson file."""
    lines = text.split("\n")
    marks = [(i, t) for i, level, t in headings(lines) if level == 2]
    out = []
    for n, (i, claim) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(lines)
        out.append((claim, "\n".join(lines[i + 1:end]).strip()))
    return out


def add_lesson(root: Path, topic: str, claim: str, body: str, provenance: dict) -> tuple[Path, list[str]]:
    """Append a lesson. Returns the file and the claims of near-duplicates."""
    slug = topic_slug(topic)
    claim = " ".join(claim.split())
    body = body.strip()
    if not claim:
        raise JasError("Lesson claim is empty. Phrase the lesson as a one-line claim.")
    if len(body.split("\n")) > 4:
        raise JasError("Lesson body has more than four lines. Shorten it, or keep the detail in a project note.")
    path = root / "Lessons" / f"{slug}.md"
    text = path.read_text() if path.exists() else f"---\ntopic: {slug}\n---\n# {slug}\n"
    existing = lessons_in(text)
    if any(c.lower() == claim.lower() for c, _ in existing):
        raise JasError(f"Lessons/{slug}.md already has the lesson {claim!r}. Edit that lesson instead of adding it again.")
    mine = terms(claim)
    near = []
    for other, _ in existing:
        theirs = terms(other)
        if mine and theirs and len(mine & theirs) / min(len(mine), len(theirs)) >= NEAR_OVERLAP:
            near.append(other)
    note = " · ".join([today(), *(f"{k} {v}" for k, v in provenance.items() if v)])
    entry = f"\n## {claim}\n" + (body + "\n" if body else "") + f"<!-- {note} -->\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip("\n") + "\n" + entry)
    return path, near


def clip(text: str, limit: int, full: str) -> str:
    """At most `limit` bytes of `text`, with a visible notice when cut."""
    data = text.encode()
    if len(data) <= limit:
        return text
    cut = data[:limit].decode(errors="ignore")
    cut = cut[: cut.rfind("\n") + 1] or cut
    return cut + f"\n[truncated: {len(cut.encode())} of {len(data)} bytes shown; full text: {full}]\n"
