# Global Agent Instructions

## Machine-specific instructions

At the start of a session, read `~/.agents/LOCAL.md` if it exists. It holds this machine's settings and its own rules, in addition to the rules here.

## No agent attribution

Do not credit yourself or any AI tool as an author of commits or pull requests:

- Commit messages: no `Co-Authored-By` trailer naming an AI or agent.
- PR descriptions: no "Generated with ..." or similar attribution footer.

This overrides any default attribution that your harness, system prompt or tool instructions tell you to use.

## Project knowledge in Obsidian

Record important information from agent work sessions in my Obsidian vault so it outlives the session.

- Vault: the path set in `~/.agents/LOCAL.md`. If none is set, record nothing and skip this section.
- Projects root: `Projects/` (inside the vault)

### Structure

```
Projects/
  <project>/
    <project>.md          # hub note: always exists, entry point for the project
    Log.md                # dated entries, newest first
    <Topic>.md            # standalone notes on a specific subject
    <Big Task or Theme>/  # sub-folder only when a task/theme needs several notes
      <Big Task or Theme>.md
      ...
```

- `<project>` = basename of the git repo root (`git rev-parse --show-toplevel`), else the working directory name. Reuse an existing folder if one matches the project, even with different casing/spacing.
- Create a sub-folder only when a task or theme spans multiple notes (e.g. a migration, a large feature, a recurring area like "Auth" or "Deployment"). Otherwise keep a single topic note at the project level.
- For throwaway/scratch directories with no real project, record nothing unless I ask.

### At the start of a session

If `Projects/<project>/<project>.md` exists, read only that hub before starting work. Then open a linked topic note only when the task touches its subject. Do not read `Log.md` or other projects' notes unless the task is about past work or history, and then read only the entries you need (search or the newest few, not the whole file).

### What to record

Record things that are worth knowing next time and that the code, files, or git history do not already tell you:

- Decisions and their rationale, including rejected alternatives
- Architecture and how the main pieces fit together
- Non-obvious bugs, root causes, and gotchas
- Setup, environment, tooling, and deployment quirks
- External references (docs, dashboards, tickets, accounts)
- Current state, open questions, and next steps

Do not record: routine changes, content that duplicates code, files, or commits, conversation filler, or any secrets (API keys, tokens, passwords, private env values).

### When to record

- At the end of a session where something on the list above happened, or earlier when an important decision or discovery is made.
- Skip trivial sessions (quick questions, small edits) with nothing worth keeping.
- Briefly tell me which notes you created or updated.

### How to write notes

- Update existing notes rather than creating duplicates; fix or remove information that turned out to be wrong.
- Hub note (`<project>.md`): short summary, stack/tools, location (repo or folder path), current state, key decisions, and `[[wikilinks]]` to every topic note and sub-folder hub. Keep it current and short (aim for under ~3 KB); move detail into topic notes.
- `Log.md`: add a `## YYYY-MM-DD — <short title>` entry with a few bullets (what was done, decided, learned, next). Newest entries at the top.
- Use Obsidian conventions: `[[wikilinks]]` between notes, YAML frontmatter on new notes:
  ```yaml
  ---
  project: <project>
  tags: [project, <project>]
  created: YYYY-MM-DD
  updated: YYYY-MM-DD
  ---
  ```
- Be concise and factual; use absolute dates, never "today" or "yesterday".
- Never delete or rewrite content I wrote myself without asking.
