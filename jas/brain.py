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
FENCE = re.compile(r"^\s*(`{3,}(?!.*`)|~{3,})(.*)$")
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


IN_PROGRESS = (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"))


def unfinished(root: Path) -> str:
    """Why the brain's git state must not be committed over: an operation in progress or unmerged files. Empty when clean."""
    git_dir = Path(git(root, "rev-parse", "--absolute-git-dir").stdout.strip())
    for marker, operation in IN_PROGRESS:
        if (git_dir / marker).exists():
            return (f"A git {operation} is in progress in the brain. In {root}, finish it (fix the files, git add them, "
                    f"git {operation} --continue) or undo it (git {operation} --abort), then rerun.")
    unmerged = sorted(set(git(root, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()))
    if unmerged:
        return f"The brain has unmerged files: {', '.join(unmerged)}. In {root}, fix them and git add them, then rerun."
    return ""


def identity(repo: Path, box: str) -> list[str]:
    """`-c` flags for a fallback commit identity when git has none."""
    if git(repo, "config", "user.email", check=False).stdout.strip():
        return []
    return ["-c", f"user.name={box}", "-c", f"user.email={box}@just-a-stack.local"]


def parse_zone(text: str) -> str | None:
    """The zone in the text of a `brain.json`, or None when it is not one."""
    try:
        return json.loads(text).get("zone")
    except (ValueError, AttributeError):
        return None


def zone_of(root: Path) -> str | None:
    """The brain's zone; None when its `brain.json` is missing, invalid or leads outside it."""
    path = root / "brain.json"
    try:
        return parse_zone(path.read_text()) if inside(root, path) else None
    except OSError:
        return None


def inside(root: Path, path: Path) -> bool:
    """Whether `path`, with every symlink resolved, stays inside the brain.

    Decided by file identity, so another spelling of the brain's path (on a case-insensitive filesystem) counts as inside.
    Before the brain folder exists nothing can lead out through it, so its spelled path decides."""
    if not root.exists():
        return path.resolve().is_relative_to(root.resolve())
    probe = path.resolve()
    while not probe.exists():
        probe = probe.parent
    top = root.stat()
    return any(os.path.samestat(top, folder.stat()) for folder in (probe, *probe.parents))


def within(root: Path, path: Path) -> Path:
    """`path`, when it stays inside the brain; jas never reads or writes through a link that leads out."""
    if not inside(root, path):
        raise JasError(f"{path} leads outside the brain; jas does not read or write there. If a symlink leads out, replace it with a real file or folder.")
    return path


def open_brain(cfg: dict) -> Path:
    """The brain path, after checking it exists and belongs to this box's zone."""
    root = config.brain_path(cfg)
    zone = zone_of(root)
    if zone is None:
        raise JasError(f"No valid brain at {paths.tilde(root)} (a brain.json inside it that names a zone). "
                       f"Run `jas init --brain <git-url>` or `jas init --new-brain`.")
    if zone != cfg["zone"]:
        raise JasError(f"Brain {paths.tilde(root)} is zone {zone!r}; this box is zone {cfg['zone']!r}. Point brain.path at a {cfg['zone']} brain.")
    return root


def skeleton_missing(root: Path, zone: str) -> dict[str, str]:
    """Skeleton files to write: missing ones, and `.gitattributes` with its missing lines appended."""
    files = dict(SKELETON, **{"brain.json": json.dumps({"version": 1, "zone": zone}) + "\n"})
    out = {name: text for name, text in files.items() if not os.path.lexists(root / name)}
    attributes = root / ".gitattributes"
    if attributes.is_file():
        current = attributes.read_text()
        lines = [line for line in SKELETON[".gitattributes"].splitlines() if line not in current.splitlines()]
        if lines:
            out[".gitattributes"] = current + ("" if current.endswith("\n") or not current else "\n") + "\n".join(lines) + "\n"
    return out


def write_skeleton(root: Path, zone: str) -> list[str]:
    missing = skeleton_missing(root, zone)
    for name, text in missing.items():
        paths.write_atomic(within(root, root / name), text)
    return sorted(missing)


def create(root: Path, zone: str, box: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    commit_paths(root, box, "brain: init", write_skeleton(root, zone))


def commit_paths(root: Path, box: str, message: str, names: list[str]) -> bool:
    """Commit only `names`, so files jas did not write never ride along."""
    if not names:
        return False
    git(root, "add", "--", *names)
    if git(root, "diff", "--cached", "--quiet", "--", *names, check=False).returncode == 0:
        return False
    git(root, *identity(root, box), "commit", "-q", "-m", message, "--", *names)
    return True


def commit_all(root: Path, box: str, message: str) -> bool:
    git(root, "add", "-A")
    if not git(root, "status", "--porcelain").stdout.strip():
        return False
    git(root, *identity(root, box), "commit", "-q", "-m", message)
    return True


def import_plan(source: Path, root: Path) -> tuple[list[tuple[Path, Path]], list[Path]]:
    """Files to copy from `source` (its `Projects/` and loose top-level `.md`), and existing ones kept.

    Source files that lead outside `source` are left out. Any existing destination, a dangling link included, is kept;
    one that leads outside the brain stops the whole import."""
    pairs = [(f, root / f.name) for f in sorted(source.glob("*.md")) if f.is_file() and inside(source, f)]
    projects = source / "Projects"
    if projects.is_dir():
        for f in sorted(projects.rglob("*")):
            rel = f.relative_to(source)
            if f.is_file() and inside(source, f) and not any(part.startswith(".") for part in rel.parts):
                pairs.append((f, root / rel))
    copies = [(src, within(root, dest)) for src, dest in pairs if not os.path.lexists(dest)]
    kept = [dest for _, dest in pairs if os.path.lexists(dest)]
    return copies, kept


def import_notes(source: Path, root: Path) -> list[str]:
    """Copy the planned files; returns their paths relative to the brain."""
    copies, _ = import_plan(source, root)
    for src, dest in copies:
        dest.parent.mkdir(parents=True, exist_ok=True)
        paths.write_atomic(dest, src.read_bytes())
        shutil.copystat(src, dest)
    return [dest.relative_to(root).as_posix() for _, dest in copies]


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


def fenced(lines: list[str]) -> tuple[list[bool], str]:
    """Per line, whether it belongs to a code fence (delimiters included), and the fence still open at the end ("" when none).

    A fence closes only on a line of its own character, at least as long, with nothing but whitespace after it."""
    mask, fence = [], ""
    for line in lines:
        match = FENCE.match(line)
        mask.append(bool(fence or match))
        if fence:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not match.group(2).strip():
                fence = ""
        elif match:
            fence = match.group(1)
    return mask, fence


def outside_fences(lines: list[str]) -> list[tuple[int, str]]:
    """(line index, line) for each line outside code fences, fence lines excluded."""
    mask, _ = fenced(lines)
    return [(i, line) for i, line in enumerate(lines) if not mask[i]]


def headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, text) for each heading outside code fences."""
    matches = ((i, HEADING.match(line)) for i, line in outside_fences(lines))
    return [(i, len(m.group(1)), m.group(2)) for i, m in matches if m]


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
    folder = within(root, root / "Projects" / project)
    if not folder.is_dir():
        folder.mkdir(parents=True)
        date = today()
        paths.write_atomic(
            hub_path(root, project),
            f"---\nproject: {project}\ntags: [project, {project}]\ncreated: {date}\nupdated: {date}\n---\n"
            f"# {project}\n\n## Summary\n\n## Current state\n\n## Key decisions\n\n## Notes\n\n- [[Log]]\n"
        )
    return folder


def add_log(root: Path, project: str, title: str, body: str, box: str) -> Path:
    log = within(root, ensure_project(root, project) / "Log.md")
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
    paths.write_atomic(log, "\n".join(lines))
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
    path = within(root, root / "Lessons" / f"{slug}.md")
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
    paths.write_atomic(path, text.rstrip("\n") + "\n" + entry)
    return path, near


def clip_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def clip(text: str, limit: int, full: str) -> str:
    """At most `limit` bytes of `text`, with a visible notice when cut."""
    data = text.encode()
    if len(data) <= limit:
        return text
    cut = data[:limit].decode(errors="ignore")
    cut = cut[: cut.rfind("\n") + 1] or cut
    return cut + f"\n[truncated: {len(cut.encode())} of {len(data)} bytes shown; full text: {full}]\n"
