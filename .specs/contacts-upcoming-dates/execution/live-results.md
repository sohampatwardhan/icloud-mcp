# Live Results: Upcoming Birthdays and Dates

Read-only run of [live_check.py](live_check.py) against the user's iCloud account. Aggregates only.

## Speed (R4.2)

Each call opens a fresh HTTP session, as the server does, and includes the change check.

| Call | Raw timings (ms) | Median | Max |
|---|---|---:|---:|
| Cold call (discovery, sync token, full download) | 2174 | — | — |
| 10 warm calls, today | 178, 162, 179, 172, 159, 164, 154, 187, 155, 188 | 168 ms | 188 ms |
| 10 warm calls, 7 days | 189, 150, 154, 188, 149, 165, 220, 163, 165, 182 | 165 ms | 220 ms |

## Correctness (R1.1)

The windows below run the real `upcoming_dates` on one live snapshot taken with a single change check. An earlier run that sent one iCloud request per window got HTTP 503 from iCloud partway through. iCloud then answered a fresh call and a 40-call burst normally, so the error was transient on iCloud's side.

- Raw download: 1813 cards, 15 Contacts lists, 1655 people with a readable birthday or significant date, 0 unreadable date values.
- All 366 one-day windows from today: 366 of 366 match the independent computation exactly (same people, each once, matching total, a single page).
- Busiest single day: 12 people.
- 7-day window: 35 returned, 35 distinct, total 35, 35 expected.
- 366-day window: 1655 returned, 1655 distinct, total 1655, 1655 expected.

## Size (R2.9)

- Today's answer: 2,817 bytes of compact JSON (about 704 tokens).
- Largest one-day page over the year: 5,413 bytes compact (5,747 with spaces), against a 16,000-byte limit.

## Links (R2.5, R2.6)

- Snapchat links in the full-year results: 1596 https, 0 other.
- Instagram links in the full-year results: 586 https, 0 other.

## Checks

- Pass: every warm call under 0.5 s.
- Pass: every one-day window matches the independent computation.
- Pass: the 7-day and 366-day windows match it.
- Pass: largest one-day page at most 16,000 bytes.
- Pass: every Snapchat and Instagram link is https.
- Pass: only read methods issued.
