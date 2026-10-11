# Execution Ledger: Contact Fields Completion

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md) · [Design](03_design.md) · [Tasks](04_tasks.md) · [Execution](05_execution.md)
<!-- spec-nav:end -->

## Active Wave

| Task | Stage | Mode | Branch / worktree | State |
|---:|---:|---|---|---|
| 2.1–2.2 | 3 | dedicated review | `feature/birthday-social-profiles` / main checkout | `feature/birthday-social-profiles` / main checkout | dispatched |

| Task | Status | Commit / diff | Verification | Reviewer | Notes |
|---|---|---|---|---|---|
| 1.1 | verified | `056c35a` | 111 tests pass (9 new); ruff clean; snapshot no-op replay unchanged (0 contacts changed apart from 1 HTML note) | wave review (2 reviewers) + targeted re-review: pass after fixes | ORG edited in place |
| 1.2 | verified | spec commit | encodings recorded in [execution/format-investigation.md](execution/format-investigation.md) and Appendix A; two whole-book ETag diffs: 1 new card, 0 changed | user confirmed values | verification code could not be saved on the device; pronunciation names and the `alternate_birthday` rename were approved into scope |
| 1.3 | verified | `7498281` | 126 tests pass (15 new); ruff clean; snapshot: 1,803 name write-backs unchanged, 1,782 renames keep every part and touch only N/FN | wave review (2 reviewers) + targeted re-review: pass after fixes | the replay found stray spaces in a stored N part; reads now trim parts |
| 1.4 | verified | `79b06b6` | 131 tests pass (5 new); ruff clean; snapshot write-back of organization, department and is_company: 1,803 unchanged | wave review (2 reviewers) + targeted re-review: pass after fixes | — |
| 1.5 | verified | `86f4235` | 137 tests pass (6 new); ruff clean; snapshot: echo 1,803 unchanged, add adds exactly 2 lines on 1,803; earlier replays unchanged | wave review (2 reviewers) + targeted re-review: pass after fixes | `_next_group` gained reserved groups |
| 1.6 | verified | `b5e6a70` | 142 tests pass (5 new); ruff clean; snapshot: echo 1,803 unchanged (8 cards with IMPP), add adds 1 line on all | wave review (2 reviewers) + targeted re-review: pass after fixes | — |
| 1.7 | verified | `a93dc3f` | 149 tests pass (7 new) using the recorded throwaway values; ruff clean; snapshot: the one real purchased-tone ringtone is omitted | wave review (2 reviewers) + targeted re-review: pass after fixes | verification code not exposed (never observed) |
| 1.8 | verified | `a5ea541` | 150 tests pass (1 new); ruff clean | wave review (2 reviewers) + targeted re-review: pass after fixes | — |
| 2.1 | verified | `40f18ba` | 162 tests pass (1 new); ruff clean | controller self-check; stage 3 review pending | — |
| 2.2 | verified | `6936354` | 168 tests pass (6 new); ruff clean; fastmcp still 4.1.0 (the Context7 evidence holds) | dedicated review pending (public contract) | contract repair: the ETag tests live in [`tests/test_contacts.py`](../../tests/test_contacts.py), added to Files |
| 3.1 | not_started | — | — | — | — |
| 4.1 | not_started | — | — | — | — |
| 5.1 | not_started | — | — | — | — |

## Baseline

| Revision | Command | Exit | Pre-existing failures |
|---|---|---:|---|
| `a55261963ddf40cf768c712bcd0cab05d05e3d89` | `.venv/bin/pytest -q` (102 tests) and `.venv/bin/ruff check src tests` | 0 | none |

Work happens directly on `feature/birthday-social-profiles`, the branch behind pull request sohampatwardhan/icloud-mcp#1, at the user's request to commit there. The tree was clean at the base commit, and no separate worktree is used.

## Preflight

- **Depth:** thorough. The work changes the public MCP tool contract and handles personal contact data.
- **Review fan-out:** 2 reviewers at `balanced` tier with `high` reasoning, and 2 repair rounds per defect.
- **Artifact digest** (SHA-256 of [01_discovery.md](01_discovery.md) through [04_tasks.md](04_tasks.md)): `3f9dd5193e2e2299bbdac46786bf339e61c8bbc51a71f9d73f5ef6ef583619af`.

- **Hardening result.** Two reviewers (gaps and code) returned 21 findings. Every certain fix that stays within the approved requirements was applied to the design and tasks:
  - write-backs equal to the read are no-ops for name and parts;
  - `N` and `ORG` are edited in place, and `ORG` components from index 2 on are kept;
  - validation runs before mutation and before any network request on create;
  - related names and instant messages use dedicated matching, keep `pref`, and reserve old group numbers;
  - the service is derived from the URI scheme when missing;
  - updates are refused when iCloud returns no ETag, and 412 errors are split between create and update;
  - the device check now covers `is_company` and `department`.
- **Uncertain findings** became verification steps in task 3.1: unescaped commas in text properties, IMPP without a service type, multi-component `ORG` and company cards with no organization.
- **Targeted re-review.** It rejected two edits, and both were reverted:
  - **Company-first display name.** This was an unrequested change to the read `name`. Reads keep the existing behaviour.
  - **Change-only `is_company` validation.** This would have deviated from R3.7. R3.7 is kept as written, because the snapshot has no company card without an organization, so R6.2 is unaffected.
- **Requirements** changed only in their navigation links. The Design and Tasks gates stay approved under delegated authority.
- **Hardened artifact digest:** `2fbe1a0f086d84dc04ceeb92ee0a2dbcea3004dfbc225dfc9b6ca7cd22f5ac59`.

## Execution Timing


### Task Board

```mermaid
kanban
  pending[Pending]
    t_kanban_1_2[⚪ 1.2: Record Apple-only encodings from a throwaway iPhone card]
    t_kanban_1_3[⚪ 1.3: Add structured name parts and loss-free renames]
    t_kanban_1_4[⚪ 1.4: Add the company-card flag]
    t_kanban_1_5[⚪ 1.5: Add related names with Apple labels]
    t_kanban_1_6[⚪ 1.6: Add instant-message accounts]
    t_kanban_1_7[⚪ 1.7: Read the Apple-only fields proven readable]
    t_kanban_2_1[⚪ 2.1: Search nicknames, maiden, related and IM names]
    t_kanban_2_2[⚪ 2.2: Expose the new fields as MCP tool parameters]
    t_kanban_3_1[⚪ 3.1: Replay the live snapshot for zero unintended changes]
    t_kanban_4_1[⚪ 4.1: Create and update a test contact check on the iPhone]
    t_kanban_5_1[⚪ 5.1: Update the README and pull request, then push]
  done[Done]
    t_kanban_1_1[🟢 1.1: Add single-value text fields and keyword parameters]
```
### Run Intervals
| Run ID | Started UTC | Stopped UTC | Elapsed Seconds | Outcome |
|---|---|---|---:|---|
| run-20261011T195853Z | 2026-10-11T19:58:53Z | 2026-10-11T20:16:19Z | 1046 | blocked |
| run-20261011T202231Z | 2026-10-11T20:22:31Z | pending | pending | active |

### Task Attempt Intervals
| Run ID | Stage/Wave | Task | Attempt | Started UTC | Stopped UTC | Elapsed Seconds | Outcome |
|---|---|---|---:|---|---|---:|---|
| run-20261011T195853Z | Stage 1 | 1.1 | 1 | 2026-10-11T20:06:02Z | 2026-10-11T20:08:33Z | 151 | verified |
| run-20261011T195853Z | Stage 1 | 1.2 | 1 | 2026-10-11T20:09:21Z | 2026-10-11T20:16:19Z | 418 | blocked |
| run-20261011T202231Z | Stage 1 | 1.2 | 1 | 2026-10-11T20:22:31Z | 2026-10-11T20:25:00Z | 149 | verified |
| run-20261011T202231Z | Stage 2 | 1.3 | 1 | 2026-10-11T20:25:12Z | 2026-10-11T20:27:29Z | 137 | verified |
| run-20261011T202231Z | Stage 2 | 1.4 | 1 | 2026-10-11T20:27:31Z | 2026-10-11T20:28:24Z | 53 | verified |
| run-20261011T202231Z | Stage 2 | 1.5 | 1 | 2026-10-11T20:28:26Z | 2026-10-11T20:30:27Z | 121 | verified |
| run-20261011T202231Z | Stage 2 | 1.6 | 1 | 2026-10-11T20:30:29Z | 2026-10-11T20:31:38Z | 69 | verified |
| run-20261011T202231Z | Stage 2 | 1.7 | 1 | 2026-10-11T20:31:41Z | 2026-10-11T20:32:17Z | 36 | verified |
| run-20261011T202231Z | Stage 2 | 1.8 | 1 | 2026-10-11T20:32:19Z | 2026-10-11T20:32:54Z | 35 | verified |
| run-20261011T202231Z | Stage 2 | wave-review-fixes | 1 | 2026-10-11T20:40:54Z | 2026-10-11T20:46:58Z | 364 | verified |
| run-20261011T202231Z | Stage 3 | 2.1 | 1 | 2026-10-11T20:47:11Z | 2026-10-11T20:47:33Z | 22 | verified |
| run-20261011T202231Z | Stage 3 | 2.2 | 1 | 2026-10-11T20:47:35Z | 2026-10-11T20:49:32Z | 117 | verified |

## Wave Review: Stages 1–2

- **Reviewers.** Two independent reviewers at thorough depth (balanced tier, high reasoning) covered requirement compliance and correctness/quality across the accumulated diff of tasks 1.1–1.8.
  - **Verdicts:** requirements pass and quality pass from the first; requirements fail and quality pass from the second.
  - **The failure (P1):** a create or update giving `name` together with a name part left first and last name empty.
- **Repair round 1** (`b4beaf5`), seven fixes:
  - unprovided first and last name are split from `name`;
  - duplicate related names and IM accounts match on label first;
  - `is_company=true` replaces any other `X-ABShowAs` value;
  - alert parsing keeps sound names containing commas whole;
  - text fields are trimmed;
  - `_validate_fields` receives only the arguments it uses;
  - title and notes equal to the read are no-ops.
- **Targeted re-review: pass**, with three P2 edge cases.
- **Repair round 2** (`ffec83f`):
  - echoed name parts count as omitted;
  - an explicit multi-word first or last name is removed from the display name before the other part is split;
  - `notes=""` clears HTML that reads back as empty.
- **Live-snapshot replays after both rounds:**
  - **Write-back** of every field: 0 of 1,803 cards change, including the HTML note that previously changed.
  - **Renames:** 1,782 keep every part.
  - **Social, label, address and date replays:** unchanged.
- **Behaviour note for the tool description in task 2.2:** notes text equal to the cleaned read of a rich-text note is treated as an echo and keeps the stored HTML.

## Checkpoints

- Checkpoint 4 (live write to the user's iCloud account): pending. Execution stops here for the user's go-ahead.

## Integration Decision

- Status: pending
- Base: `feature/birthday-social-profiles`
- Result: pending
- Post-integration verification: pending

### Execution Gantt

```mermaid
gantt
    dateFormat YYYY-MM-DDTHH:mm:ss
    axisFormat %m-%d %H:%M
    section Execution Runs
    run-20261011T195853Z (blocked, 1046s) :crit, run_20261011T195853Z, 2026-10-11T19:58:53, 2026-10-11T20:16:19
    section Stage 1
    1.1 attempt 1 (verified, 151s) :done, b_1_1_attempt1, 2026-10-11T20:06:02, 2026-10-11T20:08:33
    1.2 attempt 1 (blocked, 418s) :crit, b_1_2_attempt1, 2026-10-11T20:09:21, 2026-10-11T20:16:19
```
