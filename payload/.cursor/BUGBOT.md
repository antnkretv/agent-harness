# Bugbot review rules

The review standard and weight table live in `.harness/REVIEW.md`, which `ah install` places and git
does not track; `REVIEW.md` adds the project's own review rules. Read both in full before producing
findings when they are present; without an installed kit read `REVIEW.md`. Also read `AGENTS.md`, the applicable root skill for each changed area, and Accepted ADRs
referenced by the repository.

For findings about multi-instance behavior, delivery semantics, partial failures, rolling changes,
or growth of data and load, also apply `.harness/review/architecture.md`.

Publish a finding only when it satisfies the weight table and the evidence contract of the review standard.
Do not publish preferences, optional refactorings, or issues already enforced by a mechanical gate
unless they cause a distinct behavior or contract failure.
