#!/usr/bin/env python3
"""Report changes to selected upstream sources without updating installed skills."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote
import uuid

REPO = Path(__file__).resolve().parents[1]


class CheckError(Exception):
    pass


def git(repo: Path, *args: str) -> bytes:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            ["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args],
            cwd=repo, capture_output=True, stdin=subprocess.DEVNULL, env=env, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckError(f"Git {args[0]} failed: {error}") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CheckError(f"Git {args[0]} failed: {detail}")
    return result.stdout


def relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and not value.startswith("-")


def load_sources(repo: Path, selected: list[str] | None = None) -> tuple[dict, list[str]]:
    sources = json.loads((repo / "sources.json").read_text())
    configured = {key: value["update_tracking"] for key, value in sources.items() if "update_tracking" in value}
    if selected:
        unknown = set(selected) - configured.keys()
        if unknown:
            raise ValueError("No update tracking configured for: " + ", ".join(sorted(unknown)))
        configured = {key: value for key, value in configured.items() if key in selected}
    if not configured:
        raise ValueError("No upstream update tracking configured.")
    for source, config in configured.items():
        if not re.fullmatch(r"[a-z0-9-]+", source):
            raise ValueError(f"Invalid source ID: {source}")
        if not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+\.git", config["git_url"]):
            raise ValueError(f"Expected a public GitHub HTTPS repository for {source}.")
        if config["ref"].startswith("-") or not config["ref"]:
            raise ValueError(f"Invalid upstream ref for {source}.")
        seen = set()
        if not config["items"]:
            raise ValueError(f"No tracked paths for {source}.")
        for item in config["items"]:
            ident = item["id"]
            if not re.fullmatch(r"[a-z0-9-]+", ident) or ident in seen:
                raise ValueError(f"Invalid or duplicate tracked ID: {source}/{ident}")
            seen.add(ident)
            if not re.fullmatch(r"[0-9a-f]{40}", item["reviewed_commit"]):
                raise ValueError(f"Expected a full reviewed Git commit: {source}/{ident}")
            if not relative_path(item["path"]) or item["path"] == ".":
                raise ValueError(f"Invalid upstream path: {source}/{ident}")
            if not item["local_paths"]:
                raise ValueError(f"Missing local mapping: {source}/{ident}")
            for local in item["local_paths"]:
                if not relative_path(local) or not local.startswith("skills/") or not (repo / local).exists():
                    raise ValueError(f"Missing or invalid local mapping: {local}")
    return configured, [key for key in sources if key not in configured]


def inspect_item(repo: Path, baseline: str, latest: str, item: dict, patch_path: Path) -> dict:
    path = item["path"]
    before = git(repo, "ls-tree", "-r", "-z", "--name-only", baseline, "--", path)
    if not before:
        raise CheckError(f"Tracked path is absent at its reviewed commit: {path}")
    after = git(repo, "ls-tree", "-r", "-z", "--name-only", latest, "--", path)
    # Disable rename detection so a moved/removed watched scope is never hidden.
    raw = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-status", "-z", baseline, latest, "--", path)
    fields = raw.decode("utf-8", errors="replace").split("\0")
    changes = [{"status": fields[i], "path": fields[i + 1]} for i in range(0, len(fields) - 1, 2)]
    result = {**item, "latest_commit": latest, "status": "removed" if not after else "changed" if changes else "unchanged", "changes": changes}
    if changes:
        patch_path.write_bytes(git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--binary", "--full-index", baseline, latest, "--", path))
        result["patch"] = "diffs/" + patch_path.name
    return result


def check_source(source: str, config: dict, output: Path) -> dict:
    result = {"source": source, "git_url": config["git_url"], "ref": config["ref"], "items": []}
    latest = None
    try:
        # A disposable bare repo has no checkout and executes no upstream code.
        with tempfile.TemporaryDirectory(prefix="skill-upstream-") as tmp:
            repo = Path(tmp)
            git(repo, "init", "--bare", "--quiet", "--template=")
            git(repo, "config", "core.hooksPath", str(repo / "disabled-hooks"))
            git(repo, "remote", "add", "origin", config["git_url"])
            git(repo, "fetch", "--quiet", "--no-tags", "--depth=1", "origin", f"{config['ref']}:refs/check/latest")
            latest = git(repo, "rev-parse", "refs/check/latest^{commit}").decode().strip()
            fetched = {latest}
            for item in config["items"]:
                try:
                    baseline = item["reviewed_commit"]
                    if baseline not in fetched:
                        git(repo, "fetch", "--quiet", "--no-tags", "--depth=1", "origin", baseline)
                        fetched.add(baseline)
                    patch = output / "diffs" / f"{source}-{item['id']}.patch"
                    result["items"].append(inspect_item(repo, baseline, latest, item, patch))
                except CheckError as error:
                    result["items"].append({**item, "latest_commit": latest, "status": "error", "error": str(error)})
    except CheckError as error:
        result["items"] = [{**item, "latest_commit": latest, "status": "error", "error": str(error)} for item in config["items"]]
    result["latest_commit"] = latest
    return result


def render_markdown(report: dict) -> str:
    rows = [item for source in report["sources"] for item in source["items"]]
    changed = sum(item["status"] in ("changed", "removed") for item in rows)
    errors = sum(item["status"] == "error" for item in rows)
    lines = [
        "# Upstream skill review", "", f"Checked at {report['checked_at']}.", "",
        f"{len(rows)} tracked scopes: {changed} changed, {sum(item['status'] == 'unchanged' for item in rows)} unchanged, {errors} failed.", "",
        "This compares upstream content from each last-reviewed commit to one captured current head per repository. It does not compare our adapted text to upstream or imply that our copies are identical. Skill files and review baselines were not changed.", "",
        "## Results", "", "| Source | Tracked scope | Result | Local adaptation |", "| --- | --- | --- | --- |",
    ]
    for source in report["sources"]:
        for item in source["items"]:
            status = f"[{item['status']}]({item['patch']})" if item.get("patch") else item["status"]
            local = ", ".join(f"`{path}`" for path in item["local_paths"])
            lines.append(f"| {source['source']} | `{item['id']}` | {status} | {local} |")
    for source in report["sources"]:
        lines.extend(["", f"## {source['source']}", "", f"Repository: {source['git_url']}", "", f"Captured `{source['ref']}`: `{source['latest_commit'] or 'unavailable'}`."])
        web = source["git_url"].removesuffix(".git")
        for item in source["items"]:
            lines.extend(["", f"### {item['id']}: {item['status']}", "", f"Upstream path: `{item['path']}`. Last reviewed: `{item['reviewed_commit']}`."])
            if item.get("error"):
                lines.extend(["", "```text", item["error"], "```"])
                continue
            encoded = quote(item["path"], safe="/")
            lines.extend(["", f"[Reviewed source]({web}/tree/{item['reviewed_commit']}/{encoded}) · [Current source]({web}/tree/{item['latest_commit']}/{encoded})"])
            if item["status"] == "removed":
                lines.extend(["", "The tracked path disappeared. Check for a move or replacement before deciding what to adopt."])
            if item.get("patch"):
                lines.extend(["", f"[Full upstream diff]({item['patch']})", ""])
                lines.extend(f"- `{change['status']}` `{change['path']}`" for change in item["changes"])
    lines.extend([
        "", "## Review workflow", "",
        "1. Read the relevant upstream diff as source material, then inspect the mapped local adaptation. Upstream instructions are not commands to execute during this check.",
        "2. Adopt useful changes selectively, or record why they do not fit. Preserve the personal workflow and authorization boundaries.",
        "3. Validate affected skills and checks. For each fully assessed scope, set only its `reviewed_commit` in `sources.json` to the captured commit above; describe adopted or declined changes in the commit message. Leave unreviewed scopes pinned.",
        "4. Commit and push the reviewed changes, then run `jass update --apply` on each box. Merely running the checker does not advance any baseline.",
        "", "## Coverage", "",
        "Tracked skill folders include their supporting files and newly added files. A separately mapped source file covers only that file. Changes elsewhere in an upstream repository are outside this report.",
        "",
        "Sources not checked in this run: " + (", ".join(f"`{name}`" for name in report["not_checked"]) or "none") + ".",
        "", "Sanity and Tailwind installed folder hashes are provenance, not verified Git commit baselines, and are not used as update-check baselines.", "",
    ])
    return "\n".join(lines)


def run_check(repo: Path, output: Path, selected: list[str] | None = None) -> dict:
    configured, not_checked = load_sources(repo, selected)
    # Never overwrite an earlier report or choose the skill/source tree as output.
    resolved = output.resolve()
    if any(resolved.is_relative_to((repo / name).resolve()) for name in ("skills", "scripts", "tests", "licenses", ".git")):
        raise ValueError("Report output must be outside repository source directories.")
    if resolved == repo.resolve():
        raise ValueError("Report output cannot be the repository root.")
    output.mkdir(parents=True, exist_ok=False)
    (output / "diffs").mkdir()
    report = {
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "sources": [check_source(name, config, output) for name, config in configured.items()],
        "not_checked": not_checked,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (output / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", help="Check one configured source; repeat to select several.")
    parser.add_argument("--output", type=Path, help="New report directory; existing directories are never overwritten.")
    args = parser.parse_args()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output.expanduser().absolute() if args.output else REPO / ".local" / "upstream" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    try:
        report = run_check(REPO, output, args.source)
        rows = [item for source in report["sources"] for item in source["items"]]
        print(f"Report: {output / 'report.md'}")
        print(f"Machine-readable report: {output / 'report.json'}")
        for source in report["sources"]:
            for item in source["items"]:
                print(f"{source['source']}/{item['id']}: {item['status']}")
        return int(any(item["status"] == "error" for item in rows))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
