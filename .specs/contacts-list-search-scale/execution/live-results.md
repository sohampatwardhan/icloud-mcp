# Live Results: Contacts List Size and Search Speed

Read-only run of [live_check.py](live_check.py) against the user's iCloud account. Aggregates only.

## Speed (R4.1)

Each call opens a fresh HTTP session, as the server does, and includes the change check.

| Call | Raw timings (ms) | Median | Max |
|---|---|---:|---:|
| Cold list (discovery, sync token, full download) | 2292 | — | — |
| 10 warm `list_contacts` | 168, 177, 173, 173, 189, 165, 181, 211, 169, 195 | 175 ms | 211 ms |
| 10 warm `search_contacts` | 177, 172, 165, 176, 171, 177, 182, 149, 163, 158 | 171 ms | 182 ms |

- Cold call requests: 5 (PROPFIND, REPORT).
- Warm call requests: one REPORT each.

## Page size (R2.5)

- Full traversal: 36 default pages of up to 50 summaries, 1798 people.
- Largest full page: 9,999 bytes of compact JSON (10,503 bytes with spaces), against a 16,000-byte limit.

## Completeness (R1.6)

- Direct download: 1813 cards, 15 Contacts lists, 0 unparsable, 1798 people.
- Traversal: 1798 ids, 1798 distinct; 0 not in the download, 0 missing.

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

## Run history

- **Before the wave-review fixes:** every check passed (warm list max 195 ms, warm search max 207 ms; 1,800 people).
- **After them, first rerun:** 3 of 20 warm calls took over 0.5 s (max 757 ms), during a slow period in which the cold call also took 3.2 s. A follow-up diagnostic split warm-call time into 2–3 ms of processing and about 160 ms median for the iCloud round trip, and reusing one HTTP session saved only about 5 ms, so the session-reuse fallback was not adopted.
- **The run above**, on the final code, passed every check. The two throwaway test cards had been deleted by then, so the book holds 1,798 people.
