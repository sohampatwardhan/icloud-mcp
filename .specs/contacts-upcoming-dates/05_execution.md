# Execution Ledger: Upcoming Birthdays and Dates

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md) · [Design](03_design.md) · [Tasks](04_tasks.md) · [Execution](05_execution.md)
<!-- spec-nav:end -->

## Active Wave

| Task | Stage | Mode | Branch / worktree | State |
|---:|---:|---|---|---|
| 1.1 | 1 | sequential | `feature/contacts-upcoming-dates` / main checkout | dispatched |

| Task | Status | Commit / diff | Verification | Reviewer | Notes |
|---|---|---|---|---|---|
| 1.1 | not_started | — | — | — | — |
| 1.2 | not_started | — | — | — | — |
| 1.3 | not_started | — | — | — | — |
| 2.1 | not_started | — | — | — | — |
| 3.1 | not_started | — | — | — | — |

## Baseline

| Revision | Command | Exit | Pre-existing failures |
|---|---|---:|---|
| `99011e1a0679b36794c2d98a9b4f3ca8378246ba` | `.venv/bin/pytest -q` (269 passed) and `.venv/bin/ruff check src tests` | 0 | none |

Work happens on `feature/contacts-upcoming-dates`, stacked on `feature/contacts-list-search-scale` at the user's request, in the main checkout. The tree was clean at the base commit.

## Preflight

- **Depth:** medium. The feature adds a public read-only tool and changes one shared paging helper across more than three files; it touches no authentication, migration or dependency.
- **Review fan-out:** 1 reviewer at `balanced` tier with `medium` reasoning, and 2 repair rounds per defect.
- **Artifact digest** (SHA-256 of [01_discovery.md](01_discovery.md) through [04_tasks.md](04_tasks.md)): `906198538362745a6554274cadaea54847aae43e2f13bedfd69c566b5d882b1a`.
- **Hardening result:** 9 findings (2 P1, 7 P2, no P0), all within the approved requirements and applied to the design and tasks:
  - **Timezone:** `_today` goes through `calendar._zone(None)`, so an unknown zone gives the calendar tools' clear error, and it is tested under two zones 25 hours apart.
  - **Real vCards:** tests go through `build_card`, covering the 1604 placeholder year with February 29, the Anniversary label, custom labels and unlabelled dates (kind `other`).
  - **Compact dates** (`--0214`, `19850214`) are read like the dashed forms.
  - **`["all"]` entries** keep `phone` and `email`.
  - **End-date overflow:** a window ending after 9999-12-31 is rejected before any request.
  - **Links:** `x-apple:` and web addresses are matched case-insensitively; usernames lose a leading `@` and are quoted with no safe characters.
  - **More tests:** a protocol test for the tool description, and `contacts_list` and `contacts_search` added to the pinned schema digests.
  - **`years`** is left out only when the stored year is later than the placed year (a data error with no age reached). The occasion itself is still listed.
- **Targeted re-review: fail on two text issues, both fixed.** The link rule was narrowed back to exactly R2.6 (`x-apple:` values), which avoids building a wrong link from a stored value without a scheme. A false claim about `_set_social_profiles` was corrected. Year 0 is no longer treated as "no year".
- **Requirements** are unchanged apart from navigation. The Design and Tasks gates stay approved under delegated authority.
- **Hardened artifact digest:** `d54fb5073fc37b3cc03bc914b3e3e5739b7dfd96f89ad53d017f8bef00ede002`.

## Execution Timing

### Run Intervals
| Run ID | Started UTC | Stopped UTC | Elapsed Seconds | Outcome |
|---|---|---|---:|---|
| run-20261011T223737Z | 2026-10-11T22:37:37Z | pending | pending | active |

### Task Attempt Intervals
| Run ID | Stage/Wave | Task | Attempt | Started UTC | Stopped UTC | Elapsed Seconds | Outcome |
|---|---|---|---:|---|---|---:|---|

## Checkpoints

- None planned: nothing in this feature writes to iCloud.

## Integration Decision

- Status: pending
- Base: `feature/contacts-list-search-scale`
- Result: pending
- Post-integration verification: pending
