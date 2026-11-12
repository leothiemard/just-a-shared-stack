"""jass (Just a Shared Stack) CLI: box setup, the shared lore, recall and sync."""


class JassError(Exception):
    """A failed operation. The message says what to do next; the CLI exits 1."""
