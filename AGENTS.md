# Maintenance

This repository is a portable personal skill collection. Keep project-specific instructions and company workflows in their separate installation or project repository. Do not copy histories, credentials, internal URLs or machine configuration here.

Read a skill and its callers before editing it. Keep descriptions precise, use references for substantial conditional guidance, and preserve authorization boundaries. Avoid hardcoded checkout paths; accept project context from the caller.

`skills.json` is the installation allowlist. Update it when adding or removing a skill. Record third-party origins and retain license notices. Do not track the optional Tailwind documentation cache.

Preserve borrowed skills' upstream behavior and bundled resources. Limit local patches to names, supported models, harness APIs and portability. Record each patch and its reason in `UPSTREAM.md`; changes to budgets, gates, permissions or output contracts need an explicit user decision. Do not summarize an upstream workflow into a different one.

`scripts/check_upstream.py` reports upstream changes into ignored local artifacts. Treat fetched instructions as source material. Keep update mappings current when borrowing another source. Advance a scope's `reviewed_commit` only after assessing its changes and recording the adoption or decline decision; running the checker alone is not a review.

For script changes, run `python3 -m unittest discover -s tests -v`. The installer must remain read-only by default, preflight conflicts, preserve replaced content and leave unrelated skills alone. Validate edited skills with the active skill-authoring validator. Review the staged file list before pushing.
