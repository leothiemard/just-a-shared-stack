"""Box config at `~/.config/jass/box.json`."""

from __future__ import annotations

import copy
import json
import re
import socket
from pathlib import Path

from jass import JassError, paths

BOX_NAME = re.compile(r"[a-z0-9-]+")

DEFAULTS = {
    "version": 1,
    "box": "",
    "zone": "personal",
    "stack": "",
    "layers": [],
    "lore": {"path": "~/lore", "remote": ""},
    # Sessions never exported: whole projects, titles matching a regular expression (case ignored),
    # and threads whose opening message starts with a given text (workers and probes that agents launch).
    "sessions": {
        "export": True, "sources": ["t3"], "t3_store": "", "idle_minutes": 30,
        "exclude_projects": [], "exclude_title_patterns": [], "exclude_prompt_prefixes": [],
    },
    "sync": {"max_age_minutes": 30},
    # `jass night` and `jass heavy`: memory per agent and held back for the system, and the cap of one heavy command
    # (null: a quarter of this box's memory; 0: no cap).
    "night": {"agent_mb": 1500, "reserve_mb": 2000, "heavy_mb": None},
    # `jass board`: Vercel Board tasks in `<channel>/<project>` run as T3 Code threads on this box. `projects` maps a
    # project key to its folder here; `base` is the ref a task's worktree starts from ('' for origin's default branch,
    # 'none' for no worktree); `worker` is the board profile ('' for worker-<box>); provider, model and options pick the
    # threads' model ('' for the T3 project's default).
    "board": {
        "channel": "build", "projects": {}, "worker": "", "workers": 1, "base": "",
        "provider": "", "model": "", "options": [],
        "lease_minutes": 10, "poll_seconds": 15, "settle_seconds": 45, "board_poll_seconds": 60, "answer_hours": 24,
    },
}


def default_box() -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", socket.gethostname().split(".")[0].lower()).strip("-")
    return name or "box"


def merge(base: dict, extra: dict) -> dict:
    """Deep-merge `extra` over `base`, keeping keys either side has."""
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def read_raw() -> dict | None:
    path = paths.config_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise JassError(f"Cannot read {path}: {error}. Fix the JSON or rerun `jass init --apply`.")
    if not isinstance(data, dict):
        raise JassError(f"{path} must hold a JSON object. Fix it or rerun `jass init --apply`.")
    return data


def load() -> dict:
    raw = read_raw()
    if raw is None:
        raise JassError(f"No box config at {paths.tilde(paths.config_file())}. Run `jass init` first.")
    cfg = merge(DEFAULTS, raw)
    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    if not BOX_NAME.fullmatch(str(cfg.get("box", ""))):
        raise JassError(f"Box name {cfg.get('box')!r} must match [a-z0-9-]+. Pass --box with a valid name.")
    if not str(cfg.get("zone", "")).strip():
        raise JassError("Zone is empty. Pass --zone, for example --zone personal.")


def render(cfg: dict) -> str:
    return json.dumps(cfg, indent=2) + "\n"


def lore_path(cfg: dict) -> Path:
    return paths.expand(cfg["lore"]["path"])


def stack_path(cfg: dict) -> Path:
    return paths.expand(cfg.get("stack") or paths.STACK)


def layer_paths(cfg: dict) -> list[Path]:
    return [paths.expand(layer) for layer in cfg.get("layers", [])]
