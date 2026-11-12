#!/usr/bin/env python3
"""Preview, install or restore selected skill symlinks; standard library only."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import uuid

REPO = Path(__file__).resolve().parents[1]


def present(path: Path) -> bool:
    return os.path.lexists(path)


def points_to(path: Path, source: Path) -> bool:
    return path.is_symlink() and path.resolve() == source.resolve()


def plan(repo: Path, root: Path) -> list[tuple[Path, Path, str]]:
    if root.resolve().is_relative_to(repo.resolve()):
        raise ValueError("Install root must be outside the source repository.")
    names = json.loads((repo / "skills.json").read_text())["skills"]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate skill names in manifest.")
    rows = []
    for name in names:
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            raise ValueError(f"Invalid skill name: {name!r}")
        source = repo / "skills" / name
        if not (source / "SKILL.md").is_file():
            raise ValueError(f"Missing SKILL.md: {source}")
        target = root / name
        state = "linked" if points_to(target, source) else "replace" if present(target) else "add"
        rows.append((target, source, state))
    return rows


def restore(manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("version") != 1 or manifest.get("status") != "installed":
        raise ValueError("Manifest does not describe an installed transaction.")
    rows = manifest["entries"]
    # Preflight all rows before touching any of them.
    for row in rows:
        target, source = Path(row["target"]), Path(row["source"])
        if not points_to(target, source):
            raise ValueError(f"Installed link changed; refusing to restore: {target}")
        if row["backup"] and not present(Path(row["backup"])):
            raise ValueError(f"Missing backup: {row['backup']}")
    for row in reversed(rows):
        target = Path(row["target"])
        target.unlink()
        if row["backup"]:
            Path(row["backup"]).rename(target)
    manifest["status"] = "restored"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


def install(repo: Path, root: Path, replace: bool = False) -> Path | None:
    rows = plan(repo, root)
    pending = [row for row in rows if row[2] != "linked"]
    if not pending:
        return None
    if any(state == "replace" for _, _, state in pending) and not replace:
        raise ValueError("Existing skills conflict. Inspect the preview, then use --apply --replace.")
    root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = root.parent / "skill-backups" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    backup_root.mkdir(parents=True)
    manifest_path = backup_root / "manifest.json"
    manifest = {"version": 1, "status": "installing", "entries": []}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    moved: list[tuple[Path, Path]] = []
    linked: list[tuple[Path, Path]] = []
    try:
        for target, source, state in pending:
            backup = backup_root / target.name if state == "replace" else None
            if backup:
                target.rename(backup)
                moved.append((target, backup))
            target.symlink_to(source, target_is_directory=True)
            linked.append((target, source))
            manifest["entries"].append({"target": str(target), "source": str(source), "backup": str(backup) if backup else None})
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        manifest["status"] = "installed"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        return manifest_path
    except BaseException:
        for target, source in reversed(linked):
            if points_to(target, source):
                target.unlink()
        for target, backup in reversed(moved):
            if present(target):
                raise RuntimeError(f"Rollback target changed; recover backup manually: {backup}")
            backup.rename(target)
        manifest["status"] = "rolled-back"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        raise


def default_root() -> Path:
    sys.path.insert(0, str(REPO))
    from jas import paths

    return paths.skill_roots()[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="Install links after preflight.")
    action.add_argument("--check", action="store_true", help="Read-only; fail unless all links are current.")
    action.add_argument("--restore", type=Path, help="Undo an installation using its manifest.")
    parser.add_argument("--root", type=Path, help="skill root (default: ~/.agents/skills, honouring JAS_HOME)")
    parser.add_argument("--replace", action="store_true", help="Back up conflicting selected skills.")
    args = parser.parse_args()
    if args.replace and not args.apply:
        parser.error("--replace requires --apply")
    try:
        if args.restore:
            restore(args.restore.expanduser().absolute())
            print("Restored previous skills; repository retained.")
            return 0
        root = (args.root.expanduser() if args.root else default_root()).absolute()
        rows = plan(REPO, root)
        for target, _, state in rows:
            print(f"{state:7} {target}")
        if args.apply:
            manifest = install(REPO, root, args.replace)
            print(f"Restore manifest: {manifest}" if manifest else "All selected skills already linked.")
        elif args.check:
            return int(any(state != "linked" for _, _, state in rows))
        else:
            print("Preview only. Use --apply to install; --apply --replace to back up conflicts.")
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
