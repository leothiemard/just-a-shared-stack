"""The managed rules block inside the global instruction files."""

from __future__ import annotations

from pathlib import Path

from jass import JassError, lore

BEGIN = "<!-- jass:begin (managed by just-a-stack; edit rules/global.md instead) -->"
END = "<!-- jass:end -->"


def render(repos: list[Path]) -> str:
    """The block: `rules/global.md` of the stack then each layer, between the markers."""
    parts = [(repo / "rules" / "global.md").read_text().strip() for repo in repos if (repo / "rules" / "global.md").is_file()]
    return BEGIN + "\n" + "\n\n".join(parts) + "\n" + END


def span(text: str, where: Path) -> tuple[int, int] | None:
    """Where the block is: one begin then one end, each on its own line at column zero outside code fences; None when there is neither."""
    lines = text.split("\n")
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)
    found = [(i, line.rstrip()) for i, line in lore.outside_fences(lines) if line.rstrip() in (BEGIN, END)]
    if not found:
        return None
    if [marker for _, marker in found] != [BEGIN, END]:
        raise JassError(f"{where} has ambiguous jass markers. Leave exactly one begin marker line followed by one end marker line, then rerun.")
    (b, _), (e, _) = found
    return starts[b], starts[e] + len(END)


def current(text: str, where: Path) -> str | None:
    found = span(text, where)
    return text[found[0]:found[1]] if found else None


def with_block(text: str, block: str, where: Path) -> str:
    """Replace the block in place, or put it at the top; every byte outside it is kept."""
    found = span(text, where)
    if found:
        return text[:found[0]] + block + text[found[1]:]
    return block + "\n" + ("\n" + text if text else "")
