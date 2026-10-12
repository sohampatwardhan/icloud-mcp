# Live Results: Contacts Lists

Read-only run of [live_check.py](live_check.py) against the user's iCloud account. Aggregates only.

**First run (2026-10-12):** 3 of 15 list counts and 2 of 1,798 people's `groups` disagreed with the independent parse. A read-only diagnosis, printing booleans only, traced this to 2 people whose long UIDs hold vCard TEXT escapes (`\,`, `\n`, `\"`). Their member lines escape the same UID slightly differently (`"` is left unescaped). Unescaped, both sides name the same UID. The tool's reads already matched them (vobject unescapes both), so the parse in the check was wrong. The write path was not right, though:
- `_member_ref` compared the escaped text, so removing such a person would have found no line;
- a new member line would have carried a raw line break.

Both were fixed test-first (`test_escaped_uids_match_their_member_lines`), and the check now unescapes both values. The rerun below passes. The discovery figure of 92 references to deleted people becomes 88, because those 4 references were the escaped UIDs.

## Address book

- Raw download: 1813 cards, 15 Contacts lists, 1798 people.
- Member references on lists: 280, of which 88 name no card and are skipped.
- People in at least one list: 135; the most lists one person is in: 3.
- UIDs shared by more than one person: 0. People whose stored UID contains vCard escapes (`\,`, `\n`, `\"`): 2.
- A list card's `Content-Type` on GET: `text/plain; charset=UTF-8`.

## Correctness (R1.1–R1.4, R2.1–R2.3, R3.1–R3.4)

- `contacts_groups`: ids match, names match, counts match, total match, order match.
- Member pages (page size 7): 15 of 15 lists match exactly, in 36 calls.
- `groups` on every person: 1798 of 1798 match, 0 out of order.
- `contacts_get` on the person in the most lists: groups match in all 5 calls.

## Speed (R6.1)

Each call opens a fresh HTTP session, as the server does, and includes the change check.

| Call | Raw timings (ms) | Median | Max |
|---|---|---:|---:|
| First `contacts_groups` (discovery, sync token, full download) | 2101 | — | — |
| 10 warm `contacts_groups` | 180, 177, 166, 172, 188, 173, 237, 176, 170, 168 | 175 ms | 237 ms |
| 5 warm `contacts_get` (GET plus change check) | 354, 364, 367, 431, 394 | 367 ms | 431 ms |

## Size (R6.3)

- `contacts_groups`: 2,425 bytes of compact JSON (about 606 tokens).
- Largest member page (7 people, summaries): 1,739 bytes.
- `groups` on the person in the most lists: 471 bytes.

## Checks

- Pass: contacts_groups ids, names, counts, total and order match the independent parse.
- Pass: every list's paged members match it.
- Pass: every person's groups match it, sorted.
- Pass: contacts_get groups match it.
- Pass: every warm contacts_groups call under 0.5 s.
- Pass: only read methods issued.
