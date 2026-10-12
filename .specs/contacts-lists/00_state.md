# Spec State: Contacts Lists

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md) · [Design](03_design.md) · [Tasks](04_tasks.md) · [Execution](05_execution.md)
<!-- spec-nav:end -->

| Gate | Status | Evidence |
|---|---|---|
| Discovery | approved | 2026-10-11, approved by the user (full list editing; list tools, a list filter on `contacts_list` and lists on person reads; a new stacked branch and pull request) |
| Requirements | approved | 2026-10-11, 48 criteria across 7 requirements; covered by the user's approval of all gates for this feature |
| Design | approved | 2026-10-11, 12 correctness properties covering all 48 criteria; covered by the user's approval of all gates for this feature |
| Tasks | approved | 2026-10-11, 7 tasks across 7 stages covering all 48 criteria, with one checkpoint before the live write test; covered by the user's approval of all gates for this feature |
| Audit | not_run | — |
| Execution | complete | all 7 tasks verified; [sohampatwardhan/icloud-mcp#4](https://github.com/sohampatwardhan/icloud-mcp/pull/4) opened |

## Change Control

- 2026-10-11: The spec was created on branch `feature/contacts-lists`, stacked on `feature/contacts-upcoming-dates`. The user approved all gates for this feature and asked for implementation; each artifact is still written and checked in phase order. Live writes still need the user's explicit yes at that checkpoint.
- 2026-10-11: During design, a second read-only probe measured the list card layout. Two criteria were clarified to match it, with no change in scope: R4.7 now allows the revision timestamp to change on a write, as devices do, and R6.5 names which malformed arguments are refused before any request. The discovery's size estimate was corrected to about 3 KB. The Requirements gate stays approved under the blanket approval.
- 2026-10-11: Plan hardening before execution (thorough, 2 reviewers) changed the design and tasks with no change in scope. Fixes:
  - `["all"]` carries `groups` with one change check per call;
  - people sharing a UID all get the list;
  - member ids are de-duplicated and overlap-checked by resolved UID;
  - a rename to the current name sends no write;
  - names with line or paragraph separators are rejected;
  - list-card checks run on every attempt;
  - a 412 on create raises "already exists".

  R2.6 is read together with R3.1 and R6.7: under `fields=["all"]`, entries gain `groups`. The Design and Tasks gates stay approved under the blanket approval.
- 2026-10-12: Execution completed. The live write test passed on throwaway data, with one note: My Card's ETag changed once during the membership edit and the tool did not write that card. Requirements, design and tasks are unchanged since plan hardening.
