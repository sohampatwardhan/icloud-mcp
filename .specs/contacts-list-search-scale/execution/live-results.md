# Live Results: Contacts List Size and Search Speed

Read-only run of [live_check.py](live_check.py) against the user's iCloud account. Aggregates only.

## Speed (R4.1)

Each call opens a fresh HTTP session, as the server does, and includes the change check.

| Call | Raw timings (ms) | Median | Max |
|---|---|---:|---:|
| Cold list (discovery, sync token, full download) | 2661 | — | — |
| 10 warm `list_contacts` | 151, 150, 159, 182, 169, 161, 195, 154, 193, 176 | 165 ms | 195 ms |
| 10 warm `search_contacts` | 169, 184, 174, 154, 207, 167, 176, 201, 165, 185 | 175 ms | 207 ms |

- Cold call requests: 5 (PROPFIND, REPORT).
- Warm call requests: one REPORT each.

## Page size (R2.5)

- Full traversal: 36 default pages of up to 50 summaries, 1800 people.
- Largest full page: 9,999 bytes of compact JSON (10,503 bytes with spaces), against a 16,000-byte limit.

## Completeness (R1.6)

- Direct download: 1815 cards, 15 Contacts lists, 0 unparsable, 1800 people.
- Traversal: 1800 ids, 1800 distinct; 0 not in the download, 0 missing.

## Accent-folded search (R3.1)

- People whose name has letters that fold to ASCII: 12; probed: 12.
- single word: 12 of 12 found.
- full name, reversed: 12 of 12 found.

## Checks

- Pass: every warm call under 0.5 s.
- Pass: each warm call is one REPORT.
- Pass: largest default page at most 16,000 bytes.
- Pass: traversal equals direct download.
- Pass: every person exactly once.
- Pass: every accent probe finds its target.
