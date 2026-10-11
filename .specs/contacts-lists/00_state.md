# Spec State: Contacts Lists

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md) · [Design](03_design.md)
<!-- spec-nav:end -->

| Gate | Status | Evidence |
|---|---|---|
| Discovery | approved | 2026-10-11, approved by the user (full list editing; list tools, a list filter on `contacts_list` and lists on person reads; a new stacked branch and pull request) |
| Requirements | approved | 2026-10-11, 48 criteria across 7 requirements; covered by the user's approval of all gates for this feature |
| Design | approved | 2026-10-11, 12 correctness properties covering all 48 criteria; covered by the user's approval of all gates for this feature |
| Tasks | not_started | — |
| Audit | not_run | — |
| Execution | not_started | — |

## Change Control

- 2026-10-11: The spec was created on branch `feature/contacts-lists`, stacked on `feature/contacts-upcoming-dates`. The user approved all gates for this feature and asked for implementation; each artifact is still written and checked in phase order. Live writes still need the user's explicit yes at that checkpoint.
- 2026-10-11: During design, a second read-only probe measured the list card layout. Two criteria were clarified to match it, with no change in scope: R4.7 now allows the revision timestamp to change on a write, as devices do, and R6.5 names which malformed arguments are refused before any request. The discovery's size estimate was corrected to about 3 KB. The Requirements gate stays approved under the blanket approval.
