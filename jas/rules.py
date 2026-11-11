"""The managed rules block inside the global instruction files."""

from __future__ import annotations

from pathlib import Path

from jas import JasError

BEGIN = "<!-- jas:begin (managed by just-a-stack; edit rules/global.md instead) -->"
END = "<!-- jas:end -->"


def render(repos: list[Path]) -> str:
    """The block: `rules/global.md` of the stack then each layer, between the markers."""
    parts = [(repo / "rules" / "global.md").read_text().strip() for repo in repos if (repo / "rules" / "global.md").is_file()]
    return BEGIN + "\n" + "\n\n".join(parts) + "\n" + END


def span(text: str, where: Path) -> tuple[int, int] | None:
    start = text.find(BEGIN)
    if start < 0:
        if END in text:
            raise JasError(f"{where} has `{END}` without its begin marker. Remove the stray marker, then rerun.")
        return None
    end = text.find(END, start)
    if end < 0 or text.find(BEGIN, start + 1) >= 0:
        raise JasError(f"{where} has unbalanced jas markers. Leave exactly one begin and one end marker, then rerun.")
    return start, end + len(END)


def current(text: str, where: Path) -> str | None:
    found = span(text, where)
    return text[found[0]:found[1]] if found else None


def with_block(text: str, block: str, where: Path) -> str:
    """Replace the block in place, or put it at the top; every byte outside it is kept."""
    found = span(text, where)
    if found:
        return text[:found[0]] + block + text[found[1]:]
    return block + "\n" + ("\n" + text if text else "")
