"""`jas init`, `jas update` and `jas restore`: preflighted, idempotent box setup with a restore manifest."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jas import JasError, brain, config, paths, rules

ATTRIBUTION = {"commit": "", "pr": ""}


def load_installer():
    spec = importlib.util.spec_from_file_location("jas_install", paths.STACK / "scripts" / "install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load_installer()


class Transaction:
    """Records every change outside the brain, with backups, so `jas restore` can undo it."""

    def __init__(self) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.dir = paths.config_dir() / "backups" / f"{stamp}-{uuid.uuid4().hex[:8]}"
        paths.private_dir(self.dir.parent)
        self.dir.mkdir(mode=0o700)
        self.manifest = self.dir / "manifest.json"
        self.data = {"version": 1, "status": "applying", "entries": []}
        self.save()

    def save(self) -> None:
        paths.write_atomic(self.manifest, json.dumps(self.data, indent=2) + "\n")

    def record(self, entry: dict) -> None:
        self.data["entries"].append(entry)
        self.save()

    def mkdirs(self, path: Path) -> None:
        missing = []
        while not path.exists():
            missing.append(path)
            path = path.parent
        for folder in reversed(missing):
            self.record({"type": "mkdir", "path": str(folder)})
            folder.mkdir()

    def backup_path(self, path: Path) -> str:
        return str(self.dir / f"{len(self.data['entries'])}-{path.name}")

    def write(self, path: Path, text: str) -> None:
        self.mkdirs(path.parent)
        data = text.encode()
        backup = None
        if path.exists():
            backup = self.backup_path(path)
            paths.write_atomic(Path(backup), path.read_bytes())
        self.record({"type": "write", "path": str(path), "backup": backup, "sha256": hashlib.sha256(data).hexdigest()})
        paths.write_atomic(path, data)

    def link(self, path: Path, target: Path) -> None:
        self.mkdirs(path.parent)
        backup = self.backup_path(path) if os.path.lexists(path) else None
        self.record({"type": "link", "path": str(path), "target": str(target), "backup": backup})
        if backup:
            os.rename(path, backup)
        path.symlink_to(target)

    def skills(self, repo: Path, root: Path, replace: bool) -> None:
        self.mkdirs(root)
        manifest = installer.install(repo, root, replace)
        if manifest:
            self.record({"type": "skills", "manifest": str(manifest)})

    def finish(self) -> None:
        self.data["status"] = "applied"
        self.save()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class Change:
    step: str
    target: str
    state: str  # add, update, keep, conflict, skip
    detail: str = ""
    apply: Callable[[Transaction], None] | None = None
    in_brain: bool = False


def config_change(cfg: dict) -> Change:
    path = paths.config_file()
    raw = config.read_raw()
    if raw == cfg:
        return Change("config", paths.tilde(path), "keep")
    text = config.render(cfg)
    return Change("config", paths.tilde(path), "update" if raw else "add", f"box {cfg['box']}, zone {cfg['zone']}", lambda tx: tx.write(path, text))


def check_layers(repos: list[Path]) -> None:
    seen: dict[str, Path] = {}
    for repo in repos:
        try:
            names = json.loads((repo / "skills.json").read_text())["skills"]
        except (OSError, ValueError, KeyError):
            raise JasError(f"{repo} has no readable skills.json. A layer needs skills.json and skills/ like the stack.")
        for name in names:
            if name in seen:
                raise JasError(f"Skill {name!r} is in both {seen[name]} and {repo}. Rename one; layers never override each other.")
            seen[name] = repo


def skill_changes(repos: list[Path], replace: bool) -> list[Change]:
    check_layers(repos)
    out = []
    for root in paths.skill_roots():
        for repo in repos:
            try:
                rows = installer.plan(repo, root)
            except ValueError as error:
                raise JasError(f"{error} Fix {repo}/skills.json, then rerun.")
            adds = [t.name for t, _, s in rows if s == "add"]
            clashes = [t.name for t, _, s in rows if s == "replace"]
            label = f"{paths.tilde(root)} ← {repo.name}"
            if not adds and not clashes:
                out.append(Change("skills", label, "keep", f"{len(rows)} linked"))
                continue
            detail = f"{len(adds)} to link" + (f"; existing: {', '.join(clashes)}" if clashes else "")
            state = "update" if clashes and replace else "conflict" if clashes else "add"
            out.append(Change("skills", label, state, detail, lambda tx, repo=repo, root=root: tx.skills(repo, root, replace)))
    return out


def link_change(step: str, path: Path, target: Path, replace: bool) -> Change:
    label = paths.tilde(path)
    if path.is_symlink() and path.resolve() == target.resolve():
        return Change(step, label, "keep")
    if os.path.lexists(path) and not replace:
        return Change(step, label, "conflict", "exists; inspect it, then rerun with --replace to back it up")
    return Change(step, label, "update" if os.path.lexists(path) else "add", f"→ {paths.tilde(target)}", lambda tx: tx.link(path, target))


def block_change(path: Path, block: str) -> Change:
    label = paths.tilde(path)
    text = path.read_bytes().decode() if path.exists() else ""
    try:
        if rules.current(text, path) == block:
            return Change("rules", label, "keep")
        new = rules.with_block(text, block, path)
    except JasError as error:
        return Change("rules", label, "conflict", str(error))
    return Change("rules", label, "update" if path.exists() else "add", "managed block", lambda tx: tx.write(path, new))


def rules_changes(repos: list[Path]) -> list[Change]:
    block = rules.render(repos)
    canonical = paths.canonical_rules()
    out = [block_change(canonical, block)]
    for path in paths.linked_rules():
        if not os.path.lexists(path):
            out.append(Change("rules", paths.tilde(path), "add", f"→ {paths.tilde(canonical)}", lambda tx, p=path: tx.link(p, canonical)))
        elif path.is_symlink() and path.resolve() == canonical.resolve():
            out.append(Change("rules", paths.tilde(path), "keep", f"→ {paths.tilde(canonical)}"))
        elif not path.exists():
            out.append(Change("rules", paths.tilde(path), "conflict", "dangling link; remove it or point it at ~/.agents/AGENTS.md"))
        else:
            out.append(block_change(path, block))
    return out


def attribution_change() -> Change:
    path = paths.home() / ".claude" / "settings.json"
    label = paths.tilde(path)
    if not path.exists():
        return Change("attribution", label, "skip", "no Claude settings file")
    try:
        settings = json.loads(path.read_text())
    except ValueError as error:
        return Change("attribution", label, "conflict", f"invalid JSON ({error}); fix it, then rerun")
    if not isinstance(settings, dict) or "attribution" in settings:
        return Change("attribution", label, "keep")
    text = json.dumps(dict(settings, attribution=ATTRIBUTION), indent=2) + "\n"
    return Change("attribution", label, "update", "add empty commit/PR attribution", lambda tx: tx.write(path, text))


def brain_changes(cfg: dict, remote: str, new_brain: bool, import_dir: Path | None) -> list[Change]:
    root = config.brain_path(cfg)
    label = paths.tilde(root)
    box, zone = cfg["box"], cfg["zone"]
    out = []
    if root.exists() and any(root.iterdir()):
        found = brain.zone_of(root)
        if not brain.is_repo(root) or found is None:
            out.append(Change("brain", label, "conflict", "exists and is not a brain (a git repo with brain.json); --new-brain and --brain need an empty or missing "
                                                          "path. Pick another --brain-path, and use --import-notes to bring notes in"))
        elif found != zone:
            raise JasError(f"Brain {label} is zone {found!r}; this box is zone {zone!r}. Use --brain-path for a {zone} brain.")
        else:
            missing = brain.skeleton_missing(root, zone)
            if missing:
                def fill(tx):
                    brain.commit_paths(root, box, "brain: add skeleton files", brain.write_skeleton(root, zone))
                out.append(Change("brain", label, "update", "add " + ", ".join(sorted(missing)), fill, True))
            else:
                out.append(Change("brain", label, "keep", f"zone {zone}"))
            if remote and "origin" not in brain.git(root, "remote", check=False).stdout.split():
                out.append(Change("brain", label, "update", f"add remote origin {remote}", lambda tx: brain.git(root, "remote", "add", "origin", remote), True))
    elif remote:
        def clone(tx):
            root.parent.mkdir(parents=True, exist_ok=True)
            brain.git(root.parent, "clone", "-q", remote, str(root))
            found = brain.zone_of(root)
            if found is None and any(p.name != ".git" for p in root.iterdir()):
                raise JasError(f"{remote} has files but no brain.json, so it is not a brain. Remove {label} and pass --brain with a brain repo's URL")
            if found and found != zone:
                raise JasError(f"Cloned brain is zone {found!r}; this box is zone {zone!r}. Remove {label} and clone a {zone} brain.")
            brain.commit_paths(root, box, "brain: init", brain.write_skeleton(root, zone))
        out.append(Change("brain", label, "add", f"clone {remote}", clone, True))
    elif new_brain:
        out.append(Change("brain", label, "add", f"new {zone} brain", lambda tx: brain.create(root, zone, box), True))
    else:
        out.append(Change("brain", label, "skip", "no brain yet; pass --brain <git-url> or --new-brain"))
    if import_dir:
        if not import_dir.is_dir():
            raise JasError(f"--import-notes {import_dir} is not a folder. Pass the folder that holds Projects/.")
        copies, kept = brain.import_plan(import_dir, root)

        def run_import(tx):
            brain.commit_paths(root, box, "brain: import notes", brain.import_notes(import_dir, root))
        state = "add" if copies else "keep"
        out.append(Change("import", str(import_dir), state, f"{len(copies)} files to copy, {len(kept)} already present", run_import if copies else None, True))
    return out


def desired_config(args) -> dict:
    raw = config.read_raw() or {}
    cfg = config.merge(config.DEFAULTS, raw)
    cfg["box"] = args.box or raw.get("box") or config.default_box()
    cfg["zone"] = args.zone or raw.get("zone") or "personal"
    cfg["stack"] = str(paths.STACK)
    if args.brain_path:
        cfg["brain"]["path"] = args.brain_path
    if args.brain:
        cfg["brain"]["remote"] = args.brain
    for layer in args.layer or []:
        resolved = str(Path(layer).expanduser().resolve())
        if resolved not in cfg["layers"]:
            cfg["layers"].append(resolved)
    config.validate(cfg)
    return cfg


def plan(cfg: dict, replace: bool, brain_args: tuple | None) -> list[Change]:
    repos = [config.stack_path(cfg), *config.layer_paths(cfg)]
    changes = [config_change(cfg), *skill_changes(repos, replace)]
    changes.append(link_change("cli", paths.bin_dir() / "jas", config.stack_path(cfg) / "bin" / "jas", replace))
    changes += rules_changes(repos)
    changes.append(attribution_change())
    if brain_args is not None:
        changes += brain_changes(cfg, *brain_args)
    return changes


def table(changes: list[Change]) -> str:
    width = max(len(c.target) for c in changes)
    return "".join(f"{c.state:8} {c.step:11} {c.target:{width}}  {c.detail}".rstrip() + "\n" for c in changes)


def execute(changes: list[Change], apply: bool) -> int:
    """Print the plan; with `apply`, make the pending changes. Returns 0 when there was nothing to do."""
    print(table(changes), end="")
    conflicts = [c for c in changes if c.state == "conflict"]
    if conflicts:
        raise JasError(f"{len(conflicts)} conflict(s) above; nothing was changed. Resolve them, or rerun with --replace where offered.")
    pending = [c for c in changes if c.apply]
    if not pending:
        print("Nothing to do.")
        return 0
    if not apply:
        print("Preview only; nothing changed. Rerun with --apply to make these changes.")
        return 0
    pending.sort(key=lambda c: not c.in_brain)  # brain steps can fail on the network, so they run before the box is touched
    tx = None
    for change in pending:
        if not change.in_brain and tx is None:
            tx = Transaction()
        try:
            change.apply(tx)
        except (OSError, ValueError, JasError) as error:
            hint = f" Undo the completed steps with: jas restore {tx.manifest}" if tx else ""
            raise JasError(f"{change.step} {change.target} failed: {error}.{hint}")
    if tx:
        tx.finish()
        print(f"Restore manifest: {tx.manifest}")
    if str(paths.bin_dir()) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"{paths.tilde(paths.bin_dir())} is not on PATH. Add to your shell profile: export PATH=\"$HOME/.local/bin:$PATH\"")
    return 0


def run_init(args) -> int:
    cfg = desired_config(args)
    import_dir = Path(args.import_notes).expanduser().resolve() if args.import_notes else None
    changes = plan(cfg, args.replace, (args.brain or cfg["brain"]["remote"], args.new_brain, import_dir))
    execute(changes, args.apply)
    return 0


def pull(repo: Path) -> str:
    if not brain.is_repo(repo):
        return f"{repo} is not a git checkout; skipped pull"
    result = brain.git(repo, "pull", "--ff-only", "-q", timeout=60, check=False)
    if result.returncode:
        return f"pull failed in {repo}: {brain.reason(result)}; continuing with the current checkout"
    return ""


def run_update(args) -> int:
    cfg = config.load()
    if args.apply:
        for repo in [config.stack_path(cfg), *config.layer_paths(cfg)]:
            try:
                warning = pull(repo)
            except JasError as error:
                warning = str(error)
            if warning:
                print(f"warning: {warning}")
    cfg["stack"] = str(paths.STACK)
    execute(plan(cfg, args.replace, None), args.apply)
    return 0


def in_place(entry: dict) -> bool:
    """Whether the entry's change is in place (True) or never happened (False); raises when the path changed since."""
    path, backup = Path(entry.get("path", "")), entry.get("backup")
    if entry["type"] == "write":
        if path.is_file() and digest(path) == entry["sha256"]:
            return True
        if not os.path.lexists(path) if backup is None else path.is_file() and digest(path) == digest(Path(backup)):
            return False
        raise JasError(f"{path} changed since this transaction; nothing restored. Restore it by hand from {backup or 'nothing (it was new)'}.")
    if entry["type"] == "link":
        if path.is_symlink() and os.readlink(path) == entry["target"]:
            return True
        if not os.path.lexists(path) or (backup is not None and not os.path.lexists(backup)):
            return False
        raise JasError(f"{path} no longer links to {entry['target']}; nothing restored. Inspect it by hand.")
    if entry["type"] == "skills":
        skills = json.loads(Path(entry["manifest"]).read_text())
        for row in skills["entries"]:
            if not installer.points_to(Path(row["target"]), Path(row["source"])):
                raise JasError(f"Skill link {row['target']} changed; nothing restored. Inspect it by hand.")
    return True


def restore(manifest_path: Path) -> None:
    """Undo an applied transaction, or the recorded steps of one that stopped midway."""
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as error:
        raise JasError(f"Cannot read manifest {manifest_path}: {error}. Pass the path printed by `jas init --apply`.")
    if manifest.get("version") != 1 or manifest.get("status") not in ("applying", "applied"):
        raise JasError(f"{manifest_path} is not a jas transaction that can be undone (status {manifest.get('status')!r}).")
    entries = [(entry, in_place(entry)) for entry in manifest["entries"]]
    for entry, done in reversed(entries):
        path, backup = Path(entry.get("path", "")), entry.get("backup")
        if entry["type"] == "write" and done:
            if backup:
                paths.write_atomic(path, Path(backup).read_bytes())
            else:
                path.unlink()
        elif entry["type"] == "link":
            if done:
                path.unlink()
            if backup and os.path.lexists(backup):
                os.rename(backup, path)
        elif entry["type"] == "skills":
            installer.restore(Path(entry["manifest"]))
        elif entry["type"] == "mkdir":
            try:
                path.rmdir()
            except OSError:
                pass
    manifest["status"] = "restored"
    paths.write_atomic(manifest_path, json.dumps(manifest, indent=2) + "\n")
