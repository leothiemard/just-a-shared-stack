# Brain review

A periodic pass that turns raw session digests and logs into lessons, trims hubs and proposes stack changes. One report per ISO week.

1. Find the last review: the newest file in `Reviews/` (`jas show Reviews/<file>`), or none. Its week sets `<since>`; with none, use `--since 4w`.
2. Start from the cost picture: `jas sessions stats --since <since>`, then `--by project`. Note the sessions over 200K peak context and the delegate-heavy ones (many subagents) by title and path (the digests carry `peak_context` and `subagents` in their frontmatter).
3. List the material: `jas sessions list --since <since>`, and `jas recall "<theme>" --kind log --since <since>` for project logs.
4. Cluster the digests by project and by theme (a tool, a failure, a workflow). Read at most about 30 digests in total, the most informative of each cluster. When subagents are available, hand each cluster to one with the paths to read and ask for recurring friction, wins and candidate lessons, each with the digest paths as evidence.
5. Extract what recurs: friction seen in two or more sessions, a fix that worked, a tool that keeps failing the same way, a manual step done again and again.
6. For each candidate lesson, `jas recall "<claim>" --kind lesson` first. Extend or correct an existing lesson instead of adding a near-copy; otherwise `jas lesson <topic> "<claim>"`.
7. Run `jas doctor` and trim every hub it lists as over budget: move detail into topic notes, keep the hub under 3,000 bytes.
8. List proposed stack changes (a new or changed skill, a global rule, a script) with the evidence for each. Leave them for the human; never edit the stack from a review.
9. Write `Reviews/<YYYY>-W<ww>.md`, one page: period covered, the stats table, long-context and delegate-heavy sessions by name, sessions read, lessons added or changed, hubs trimmed, proposed stack changes, open questions. For long threads, recommend handing off to a fresh thread once the state is in the hub and log, instead of continuing past 200K.
10. `jas sync`, then give the user the report path and the proposed stack changes.

Done when the report exists, every lesson in it is in `Lessons/`, and the sync succeeded.
