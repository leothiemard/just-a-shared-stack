# Notes conventions

The lore is also an Obsidian vault. Keep its notes readable by a person.

## Layout per project

- `Projects/<project>/<project>.md`: the hub. Always the entry point. Short summary, stack and tools, where the code lives, current state, key decisions, and a `[[wikilink]]` to every topic note.
- `Projects/<project>/Log.md`: dated entries, newest first. Write them with `jass log`, never by hand-editing the top of the file.
- `Projects/<project>/<Topic>.md`: one subject per note (a migration, the deploy setup, an auth quirk).
- A sub-folder only when one theme needs several notes; give it its own hub `<Theme>/<Theme>.md`.

## Writing

- New notes start with frontmatter:

  ```yaml
  ---
  project: <project>
  tags: [project, <project>]
  created: YYYY-MM-DD
  updated: YYYY-MM-DD
  ---
  ```

  A hub may add `aliases: [other-name]` when its repository goes by another folder or remote name on some box; `jass` then resolves those names to this project.

- Bump `updated` when you change a note.
- Absolute dates only (`2026-11-12`), never "today" or "last week".
- Link notes with `[[wikilinks]]`; link each new topic note from the hub.
- Notes look forward: current state, open items, decisions with their reason, and gotchas. Keep history only when an agent would repeat a costly mistake without it; git keeps the rest.
- Replace a hub's state when it changes. Never append dated status bullets to a hub.
- Update a note instead of adding a near-copy. Fix or remove a claim that turned out wrong.
- Keep the hub under 3,000 bytes: `jass doctor` lists hubs over budget. Move detail into topic notes and leave a one-line pointer.
- Text the human wrote stays as written. Propose a rewrite or deletion to the user before you make it.
- Never write secrets, tokens or private credentials into a note.
