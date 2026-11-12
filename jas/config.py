"""Box config at `~/.config/just-a-stack/box.json`."""

from __future__ import annotations

import copy
import json
import re
import socket
from pathlib import Path

from jas import JasError, paths

BOX_NAME = re.compile(r"[a-z0-9-]+")

DEFAULTS = {
    "version": 1,
    "box": "",
    "zone": "personal",
    "stack": "",
    "layers": [],
    "brain": {"path": "~/brain", "remote": ""},
    "sessions": {"export": True, "sources": ["t3"], "t3_store": "", "idle_minutes": 30, "exclude_projects": []},
    "sync": {"max_age_minutes": 30},
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
        raise JasError(f"Cannot read {path}: {error}. Fix the JSON or rerun `jas init --apply`.")
    if not isinstance(data, dict):
        raise JasError(f"{path} must hold a JSON object. Fix it or rerun `jas init --apply`.")
    return data


def load() -> dict:
    raw = read_raw()
    if raw is None:
        raise JasError(f"No box config at {paths.tilde(paths.config_file())}. Run `jas init` first.")
    cfg = merge(DEFAULTS, raw)
    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    if not BOX_NAME.fullmatch(str(cfg.get("box", ""))):
        raise JasError(f"Box name {cfg.get('box')!r} must match [a-z0-9-]+. Pass --box with a valid name.")
    if not str(cfg.get("zone", "")).strip():
        raise JasError("Zone is empty. Pass --zone, for example --zone personal.")


def render(cfg: dict) -> str:
    return json.dumps(cfg, indent=2) + "\n"


def brain_path(cfg: dict) -> Path:
    return paths.expand(cfg["brain"]["path"])


def stack_path(cfg: dict) -> Path:
    return paths.expand(cfg.get("stack") or paths.STACK)


def layer_paths(cfg: dict) -> list[Path]:
    return [paths.expand(layer) for layer in cfg.get("layers", [])]
