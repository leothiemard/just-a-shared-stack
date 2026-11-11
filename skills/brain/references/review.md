# Brain review

A periodic pass that turns raw session digests and logs into lessons, trims hubs and proposes stack changes. One report per ISO week.

1. Find the last review: the newest file in `Reviews/` (`jas show Reviews/<file>`), or none. Its week sets `<since>`; with none, use `--since 4w`.
2. List the material: `jas sessions list --since <since>`, and `jas recall "<theme>" --kind log --since <since>` for project logs.
3. Cluster the digests by project and by theme (a tool, a failure, a workflow). Read at most about 30 digests in total, the most informative of each cluster. When subagents are available, hand each cluster to one with the paths to read and ask for recurring friction, wins and candidate lessons, each with the digest paths as evidence.
4. Extract what recurs: friction seen in two or more sessions, a fix that worked, a tool that keeps failing the same way, a manual step done again and again.
5. For each candidate lesson, `jas recall "<claim>" --kind lesson` first. Extend or correct an existing lesson instead of adding a near-copy; otherwise `jas lesson <topic> "<claim>"`.
6. Run `jas doctor` and trim every hub it lists as over budget: move detail into topic notes, keep the hub under 3,000 bytes.
7. List proposed stack changes (a new or changed skill, a global rule, a script) with the evidence for each. Leave them for the human; never edit the stack from a review.
8. Write `Reviews/<YYYY>-W<ww>.md`, one page: period covered, sessions read, lessons added or changed, hubs trimmed, proposed stack changes, open questions.
9. `jas sync`, then give the user the report path and the proposed stack changes.

Done when the report exists, every lesson in it is in `Lessons/`, and the sync succeeded.
