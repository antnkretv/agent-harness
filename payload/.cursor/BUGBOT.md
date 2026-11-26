# Bugbot review rules

`REVIEW.md` is the canonical review policy for this repository. Read it in full before producing
findings. Also read `AGENTS.md`, the applicable root skill for each changed area, and Accepted ADRs
referenced by the repository.

For findings about multi-instance behavior, delivery semantics, partial failures, rolling changes,
or growth of data and load, also apply `.harness/review/architecture.md`.

Publish a finding only when it satisfies the weight table and the evidence contract in `REVIEW.md`.
Do not publish preferences, optional refactorings, or issues already enforced by a mechanical gate
unless they cause a distinct behavior or contract failure.
