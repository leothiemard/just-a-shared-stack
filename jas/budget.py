"""Measure always-loaded context: global instruction files, skill descriptions, plugins, MCP servers."""

from __future__ import annotations

import json
import re
from pathlib import Path

from jas import paths

GLOBAL_FILE_MAX = 2000
SKILL_DESCRIPTION_MAX = 500
TOTAL_MAX = 20000


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def description_bytes(skill_md: Path) -> int:
    """Bytes of the `name` and `description` frontmatter values, including folded YAML blocks."""
    try:
        lines = skill_md.read_text(errors="replace").split("\n")
    except OSError:
        return 0
    if not lines or lines[0].strip() != "---":
        return 0
    total, current = 0, None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key = re.match(r"([A-Za-z_-]+):\s*(.*)$", line)
        if key:
            current = key.group(1) if key.group(1) in ("name", "description") else None
            value = key.group(2).strip()
            if current and value not in (">", "|", ">-", "|-"):
                total += len(value.strip("\"'").encode())
        elif current and line.startswith((" ", "\t")):
            total += len(line.strip().encode()) + 1
    return total


def skills_in(folder: Path) -> list[tuple[str, int]]:
    if not folder.is_dir():
        return []
    return [(d.name, description_bytes(d / "SKILL.md")) for d in sorted(folder.iterdir()) if (d / "SKILL.md").is_file()]


def plugins(home: Path) -> list[dict]:
    enabled = read_json(home / ".claude" / "settings.json").get("enabledPlugins") or {}
    installed = read_json(home / ".claude" / "plugins" / "installed_plugins.json").get("plugins") or {}
    out = []
    for plugin_id, on in sorted(enabled.items()):
        if not on:
            continue
        entries = installed.get(plugin_id) or []
        root = next((Path(e["installPath"]) for e in entries if isinstance(e, dict) and e.get("installPath")), None)
        skills = skills_in(root / "skills") if root else []
        out.append({"name": plugin_id, "skills": len(skills), "bytes": sum(b for _, b in skills), "readable": bool(root and root.is_dir())})
    return out


def mcp_servers(home: Path) -> dict[str, list[str]]:
    claude = sorted((read_json(home / ".claude.json").get("mcpServers") or {}).keys())
    try:
        toml = (home / ".codex" / "config.toml").read_text()
    except OSError:
        toml = ""
    codex = sorted({m.group(2) for m in re.finditer(r'(?m)^\[mcp_servers\.("?)([^\]".]+)\1\]', toml)})
    return {"claude": claude, "codex": codex}


def measure() -> dict:
    home = paths.home()
    items, warnings, seen = [], [], set()
    for path in [paths.canonical_rules(), *paths.linked_rules()]:
        if not path.is_file() or path.resolve() in seen:
            continue
        seen.add(path.resolve())
        size = path.stat().st_size
        items.append({"group": "global file", "name": paths.tilde(path), "count": 1, "bytes": size})
        if size > GLOBAL_FILE_MAX:
            warnings.append(f"{paths.tilde(path)} is {size} bytes (limit {GLOBAL_FILE_MAX}); move detail into skills or notes")
    for root in paths.skill_roots():
        skills = skills_in(root)
        items.append({"group": "skill root", "name": paths.tilde(root), "count": len(skills), "bytes": sum(b for _, b in skills)})
        for name, size in skills:
            warning = f"skill {name} description is {size} bytes (limit {SKILL_DESCRIPTION_MAX})"
            if size > SKILL_DESCRIPTION_MAX and warning not in warnings:
                warnings.append(warning)
    for plugin in plugins(home):
        items.append({"group": "plugin", "name": plugin["name"], "count": plugin["skills"], "bytes": plugin["bytes"]})
    total = sum(item["bytes"] for item in items)
    if total > TOTAL_MAX:
        warnings.append(f"always-loaded total is {total} bytes (limit {TOTAL_MAX})")
    for item in items:
        item["tokens"] = item["bytes"] // 4
    return {"items": items, "total": total, "tokens": total // 4, "mcp": mcp_servers(home), "warnings": warnings}


def table(report: dict) -> str:
    rows = [f"{'bytes':>7} {'tokens':>7} {'count':>5}  item"]
    rows += [f"{i['bytes']:>7} {i['tokens']:>7} {i['count']:>5}  {i['group']}: {i['name']}" for i in report["items"]]
    rows.append(f"{report['total']:>7} {report['tokens']:>7} {'':>5}  total always loaded")
    for harness, names in report["mcp"].items():
        rows.append(f"{'':>7} {'':>7} {len(names):>5}  {harness} MCP servers" + (f": {', '.join(names)}" if names else ""))
    rows += [f"warning: {w}" for w in report["warnings"]]
    return "\n".join(rows) + "\n"
