"""Render one SessionRecord as a compact Markdown digest."""

from __future__ import annotations

import hashlib
import json
import re

from jass import lore
from jass.sessions import SessionRecord

MAX_BYTES = 6000
PROMPT_CHARS = 600
OUTCOME_CHARS = 2500
TURN_CHARS = 200
KEEP_PROMPTS = 3  # first and last, when there are more than twice this many
# (earlier turns, delegated lines, prompt chars), tried in order until the digest fits.
SHRINK = [(12, 12, PROMPT_CHARS), (6, 12, PROMPT_CHARS), (0, 6, PROMPT_CHARS), (0, 0, 300), (0, 0, 120)]


def relpath(rec: SessionRecord, box: str) -> str:
    project = re.sub(r"[^A-Za-z0-9._-]+", "-", rec.project).strip("-") or "no-project"
    date = rec.started.date().isoformat()
    sha = hashlib.sha1(rec.id.encode()).hexdigest()[:8]
    return f"Sessions/{box}/{date[:7]}/{date}-{project}-{sha}.md"


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def one_line(text: str, limit: int) -> str:
    return clip(" ".join((text or "").split()), limit)


def iso(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def frontmatter(rec: SessionRecord, box: str, zone: str) -> str:
    fields = [
        ("kind", "session"), ("id", rec.id), ("box", box), ("zone", zone), ("project", rec.project),
        ("title", rec.title), ("started", iso(rec.started)), ("updated", iso(rec.updated)),
        ("provider", rec.provider), ("model", rec.model), ("turns", turns(rec)), ("subagents", rec.subagent_count),
        ("prs", rec.pr_urls), ("tokens", rec.tokens), ("peak_context", rec.peak_context),
        ("t3_environment_id", rec.environment_id), ("native_ref", rec.native_ref),
        ("branch", rec.branch), ("created_by", rec.created_by),
    ]
    lines = ["---"]
    for key, value in fields:
        if key in ("peak_context", "t3_environment_id", "native_ref", "branch", "created_by") and not value:
            continue
        if isinstance(value, dict):
            value = "{" + ", ".join(f"{k}: {v}" for k, v in value.items()) + "}"
        elif isinstance(value, (str, list)):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {value}")
    return "\n".join(lines + ["---", ""])


def turns(rec: SessionRecord) -> int:
    return rec.turns or max(len(rec.prompts), len(rec.turn_finals))


def contained(text: str) -> str:
    """Assistant Markdown that cannot open sections of its own: headings become bold, a cut fence is closed."""
    lines = text.split("\n")
    mask, still_open = lore.fenced(lines)
    out = [line if inside else re.sub(r"^#{1,6}\s+(.*?)\s*#*\s*$", r"**\1**", line) for line, inside in zip(lines, mask)]
    return "\n".join(out + ([still_open] if still_open else []))


def asked(prompts: list[str], chars: int) -> list[str]:
    def item(text: str) -> str:
        return "- " + contained(clip(text, chars)).replace("\n", "\n  ")

    if len(prompts) <= 2 * KEEP_PROMPTS:
        return [item(p) for p in prompts]
    hidden = len(prompts) - 2 * KEEP_PROMPTS
    return [*(item(p) for p in prompts[:KEEP_PROMPTS]), f"- … {hidden} more …", *(item(p) for p in prompts[-KEEP_PROMPTS:])]


def render_with(rec: SessionRecord, box: str, zone: str, keep_turns: int, keep_delegated: int, prompt_chars: int) -> str:
    out = [frontmatter(rec, box, zone), f"# {one_line(rec.title, 200) or rec.id}", "", "## Asked", ""]
    out += asked(rec.prompts, prompt_chars) or ["_No prompt recorded._"]
    outcome = contained(clip(rec.turn_finals[-1], OUTCOME_CHARS)) if rec.turn_finals else "_No final assistant message._"
    out += ["", "## Outcome", "", outcome]
    earlier = rec.turn_finals[:-1]
    if earlier:
        kept = earlier[-keep_turns:] if keep_turns else []
        out += ["", "## Turns", ""]
        if len(earlier) > len(kept):
            out.append(f"- … {len(earlier) - len(kept)} earlier turns …")
        first = len(earlier) - len(kept) + 1
        out += [f"{first + n}. {one_line(text, TURN_CHARS)}" for n, text in enumerate(kept)]
    if rec.delegated:
        kept = rec.delegated[:keep_delegated]
        out += ["", "## Delegated", ""]
        out += [f"- {one_line(d.get('title', ''), 120)} · {d.get('model', '')} · {d.get('status', '')}" for d in kept]
        if len(rec.delegated) > len(kept):
            out.append(f"- … {len(rec.delegated) - len(kept)} more …")
    return "\n".join(out) + "\n"


def render(rec: SessionRecord, box: str, zone: str) -> str:
    for limits in SHRINK:
        text = render_with(rec, box, zone, *limits)
        if len(text.encode()) <= MAX_BYTES:
            return text
    return lore.clip(text, MAX_BYTES - 120, "the source session")
