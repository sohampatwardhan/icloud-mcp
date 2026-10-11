# Execution Ledger: Contacts List Size and Search Speed

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md) · [Design](03_design.md) · [Tasks](04_tasks.md) · [Execution](05_execution.md)
<!-- spec-nav:end -->

## Active Wave

| Task | Stage | Mode | Branch / worktree | State |
|---:|---:|---|---|---|
| 1.1 | 1 | sequential | `feature/contacts-list-search-scale` / main checkout | dispatched |

| Task | Status | Commit / diff | Verification | Reviewer | Notes |
|---|---|---|---|---|---|
| 1.1 | not_started | — | — | — | — |
| 1.2 | not_started | — | — | — | — |
| 1.3 | not_started | — | — | — | — |
| 2.1 | not_started | — | — | — | — |
| 2.2 | not_started | — | — | — | — |
| 2.3 | not_started | — | — | — | — |
| 2.4 | not_started | — | — | — | — |
| 3.1 | not_started | — | — | — | — |
| 4.1 | not_started | — | — | — | — |
| 5.1 | not_started | — | — | — | — |

## Baseline

| Revision | Command | Exit | Pre-existing failures |
|---|---|---:|---|
| `41bdbd5bf93f652b2b67967656bab05357dc6cd0` | `.venv/bin/pytest -q` (168 tests) and `.venv/bin/ruff check src tests` | 0 | none |

Work happens on `feature/contacts-list-search-scale`, a branch stacked on `feature/birthday-social-profiles` at the user's request, in the main checkout. The tree was clean at the base commit.

## Preflight

- **Depth:** thorough. The cache handles personal contact data across credentials, and the change alters the public list and search output.
- **Artifact digest** (SHA-256 of [01_discovery.md](01_discovery.md) through [04_tasks.md](04_tasks.md)): `85d0c57473bcd851dafc51f64213879752143896fc8f5406cc2e27e00595d846`.

- **Review fan-out:** 2 reviewers at `balanced` tier with `high` reasoning (one for gaps, one for code against the plan), and 2 repair rounds per defect.
- **Hardening result.** 33 findings, of which 3 were P0 and 9 P1. Applied to the design and tasks:
  - **Truncated syncs:** an in-207 507 on the collection triggers a rebuild, and the token never advances past a truncated reply.
  - **Atomic deltas:** the delta is staged and committed together with its token; an omitted href raises; a 404 in a multiget deletes.
  - **The server's own writes** mark the card dirty for a refetch instead of building a local copy.
  - **Cold loads** are committed only on success, so failed logins never evict or leak entries.
  - **Locking:** an explicit protocol, with a pending table and a single lock order.
  - **Cursors:** robust decoding, with `qnorm` used for both rank and fingerprint.
  - **Structure:** an injected-collaborator signature (no import cycle, existing test patches still work) and a `_people` seam with per-test cache clearing.
  - **Folding** of stroke letters (ł, ø, đ, …).
  - **Keys and staleness:** canonical URL keys, and rediscovery on a collection 404/410.
  - **Idle expiry:** an idle sweeper thread, so idle data is discarded even if no call arrives.
  - **Paging:** the paging property narrowed to adds and removes, with edits between pages documented as a limitation.
- **Targeted re-review: fail.** All five issues it raised were resolved by aligning with the approved wording rather than changing requirements:
  - the locking protocol was made consistent;
  - phone matching follows R3.5 literally, with any digit-bearing word matching but punctuation-only words not;
  - every non-group card is listed, per the requirements' definition of a person;
  - task 2.2 extends the shared fake server;
  - R4.1 is kept strict, so every warm call must finish under 0.5 s.
- **Requirements** are unchanged apart from navigation. The Design and Tasks gates stay approved under delegated authority.
- **Hardened artifact digest:** `6ce448b58a3f5ebf74c2f4b58253fb9c0fa9b7f070b7a4f501c9f1bc683df481`.

## Execution Timing

### Run Intervals
| Run ID | Started UTC | Stopped UTC | Elapsed Seconds | Outcome |
|---|---|---|---:|---|
| run-20261011T212026Z | 2026-10-11T21:20:26Z | pending | pending | active |

### Task Attempt Intervals
| Run ID | Stage/Wave | Task | Attempt | Started UTC | Stopped UTC | Elapsed Seconds | Outcome |
|---|---|---|---:|---|---|---:|---|

## Checkpoints

- Checkpoint 4 (live freshness test on a throwaway card): pending.

## Integration Decision

- Status: pending
- Base: `feature/birthday-social-profiles`
- Result: pending
- Post-integration verification: pending
