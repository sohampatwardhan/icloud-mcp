# Design: Upcoming Birthdays and Dates

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md) · [Design](03_design.md) · [Tasks](04_tasks.md) · [Execution](05_execution.md)
<!-- spec-nav:end -->

## Overview

A new read-only MCP tool, `contacts_upcoming_dates`, answers "whose birthday or significant date falls in these days?" from the same change-checked people that `contacts_list` and `contacts_search` use. This realizes discovery approach A (a dedicated tool over the cache). The rejected alternatives were a date filter on search, and instructions alone, which would cost about 180k tokens per question.

Each entry contains:
- the person's id and name;
- the matching occasions;
- every social profile as a link;
- the first phone and email.

Results are paged with the same cursor contract as list and search. Everything lives in [`src/icloud_mcp/contacts.py`](../../src/icloud_mcp/contacts.py) and [`src/icloud_mcp/server.py`](../../src/icloud_mcp/server.py); no new module or dependency is needed.

## Architecture

```text
contacts_upcoming_dates (server.py, READ_ONLY)
  └─ upcoming_dates(start_date, days, page_size, cursor, fields)      contacts.py
       1. validate: days, start_date, page_size, fields, cursor        (no request yet)
       2. _people(session, credentials)                                 change check via the cache
       3. per person: _occasions(contact, start, days)                  placed dates in the window
       4. sort by (soonest days_away, unnamed, folded name, url)
       5. _page(..., render=_date_entry)                                shared keyset paging
```

## Components and Interfaces

### Date placement (`contacts.py`)

```python
MAX_WINDOW_DAYS = 366

def _month_day(text: str) -> tuple[int | None, int, int] | None
    # "YYYY-MM-DD" or "YYYYMMDD" -> (year, month, day); "--MM-DD" or "--MMDD" -> (None, month,
    # day). Month and day are validated against date(2000 if year is None else year, month, day),
    # so a yearless
    # February 29 is accepted. Anything else, or an impossible date such as --02-30, -> None
    # (the date is skipped, R1.9).

def _place(month: int, day: int, year: int) -> datetime.date
    # The day the occasion falls in `year`: February 29 becomes February 28 when `year`
    # is not a leap year (R1.5).

def _occasions(contact: dict, start: datetime.date, days: int) -> list[dict]
    # Every birthday and significant date of `contact` falling in [start, start + days - 1],
    # sorted by date. Each is {"kind", "date", "days_away"} plus "years" when the original
    # year is stored. kind is "birthday", or the date's label as contact reads report it
    # ("anniversary", "other" or custom text), with "other" for an unlabelled date, the same
    # default `_set_dates` writes. Both calendar years a window touches are
    # tried, so a window across December 31 matches January dates (R1.6), and a 366-day
    # window can hold the same birthday twice.
```

- **`years`** is the age (for a birthday) or the number of years (for another date) reached on the placed day, which is `placed.year - original year`. It is included only when the original year is stored and the result is 0 or more (R2.3).
- **Dates without a year** come from `_read_date` as `--MM-DD`, which includes Apple's 1604 placeholder year and `X-APPLE-OMIT-YEAR`. They have no `years` and match by month and day (R1.4).

### Social links (`contacts.py`)

```python
def _social_links(contact: dict) -> list[dict[str, str]]
    # One {"service", "username", "url"} per stored social profile, in stored order.
```

- **A web address** (`http://` or `https://`, matched case-insensitively) is used as the link unchanged (R2.5).
- **An Apple-internal value** (`x-apple:`, matched case-insensitively) gets a link built from the username with the `_SOCIAL_SERVICES` template, for Snapchat, Instagram, Facebook, LinkedIn, Twitter and TikTok. The username has a leading `@` removed and is URL-quoted with no safe characters (unlike `_set_social_profiles`, which inserts it unquoted). Profiles of other services, without a username, or with any other stored value get `url: ""` (R2.6).
- **`social_profiles` in existing reads is unchanged**, because writes send the raw value back (R4.5).

### Entries and the public function (`contacts.py`)

```python
def _today() -> datetime.date
    # Today in config.DEFAULT_TIMEZONE, through calendar._zone(None), so an unknown zone raises
    # the same ValueError as the calendar tools; a seam tests replace (R1.2).

def _date_entry(item: tuple[CachedCard, list[dict]], fields: list[str] | None) -> dict
    # Default: {"id", "name", "occasions", "social_links", "phone", "email"} (R2.1-R2.7).
    # Named fields: the default entry plus each named field, with _project's rules (R2.8).
    # ["all"]: the full contact read plus "occasions", "social_links", "phone" and "email".

def upcoming_dates(start_date: str | None = None, days: int = 1, page_size: int = 50,
                   cursor: str | None = None, fields: list[str] | None = None) -> dict
    # {"contacts": [...], "total": int, "next_cursor": str | None,
    #  "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}
```

- **Validation, before any request:**
  - `days` must be a whole number from 1 to 366 (R3.7).
  - `start_date` must match `^\d{4}-\d{2}-\d{2}$` and be a real date, and the window must end on or before 9999-12-31 (R3.8; the end-date check prevents an overflow after the iCloud request).
  - Page size and fields are checked by the existing `_check_page_size` and `_check_fields` (R3.2, R2.8).
  - The cursor is checked by `_decode_cursor` with `max_rank = days - 1` and the fingerprint `"dates:" + start.isoformat() + ":" + str(days)` (R3.6).
- **Results:**
  - Each person with at least one occasion becomes `((first days_away, *sort_key, url), (card, occasions))`.
  - Results are sorted on that key (R1.8) and paged by `_page` (R3.1, R3.3–R3.5).
  - The response echoes the resolved `start_date` and `end_date`, so a caller relying on the default can see which day it got.
- **Groups** are excluded by `_people` (R1.7). Freshness and errors come from `_people` unchanged (R4.1, R4.3).

### Shared paging change (`contacts.py`)

```python
def _page(results, page_size, after, fingerprint, fields, render=_project) -> dict
```

`render(item, fields)` builds each entry. List and search keep the default. Their behaviour and output are byte-for-byte unchanged, and the existing tests cover them.

### Contact_Tools (`server.py`)

```python
contacts_upcoming_dates(
    start_date: str | None = None,          # "First day, YYYY-MM-DD. Default: today in the server's timezone; pass the user's local date when known."
    days: int = 1 (Field ge=1, le=366),     # "Days to cover from start_date: 1 = that day, 7 = a week."
    page_size: PageSize = 50, cursor: Cursor = None,
    fields: DateFields = None,              # "Extra contact fields to add to each entry, or ["all"]."
) -> dict
```

- The tool is read-only (`READ_ONLY` annotations). Its description states the default start date, the window, the entry keys, which links are given and how paging works (R5.1).
- `PageSize` and `Cursor` are reused. A separate `DateFields` annotation describes this tool's own default entry, so the descriptions of list and search stay byte-identical (R4.5).

### Project_Docs

The README gets a tools-table row and an "Example assistant instruction" section. Both are generic and name no specific assistant (R5.2). The Muse-specific instruction is delivered with the pull request.

## Data Models

**Entry** (default projection):

```json
{"id": "https://…/card/….vcf", "name": "Sam Lee",
 "occasions": [{"kind": "birthday", "date": "2026-10-11", "days_away": 0, "years": 30}],
 "social_links": [{"service": "snapchat", "username": "samlee", "url": "https://snapchat.com/add/samlee"},
                  {"service": "instagram", "username": "sam.lee", "url": "https://www.instagram.com/sam.lee/"}],
 "phone": "+1 555 010 0000", "email": ""}
```

**Response:** `{"contacts": [entry, …], "total": 4, "next_cursor": null, "start_date": "2026-10-11", "end_date": "2026-10-11"}`.

**Cursor:** the existing format, `{"v": 1, "q": fingerprint, "after": [days_away, unnamed, folded name, url]}`.

## Correctness Properties

### Property 1: Window membership and placement
A person is returned exactly when at least one of their stored birthdays or significant dates, placed in a calendar year the window touches, falls on a day from `start_date` to `start_date + days - 1`. Placement works as follows:
- dates without a year match by month and day;
- February 29 falls on February 28 in common years;
- a window across December 31 tries both years.

A default window covers the start date only.
**Validates: Requirements 1.1, 1.3, 1.4, 1.5, 1.6**

### Property 2: Default start date
Without `start_date`, the window starts on today's date in `DEFAULT_TIMEZONE`, and the response's `start_date` shows that date.
**Validates: Requirements 1.2**

### Property 3: Exclusions
Contacts lists never appear. A stored date that is not a real calendar date is skipped, and the call still succeeds.
**Validates: Requirements 1.7, 1.9**

### Property 4: Ordering
Entries are ordered by their soonest occasion's `days_away`, then by display name (folded, nameless last), then by id.
**Validates: Requirements 1.8**

### Property 5: Occasion details
Each entry carries the id and name. Its occasions are those in the window, each with its kind or label, its placed date and its `days_away`. `years` is present when the original year is stored, and equals the placed year minus that year. A stored year later than the placed year is a data error with no age reached, so `years` is left out in that one case.
**Validates: Requirements 2.1, 2.2, 2.3**

### Property 6: Links
`social_links` has one item per stored profile, with its service and username:
- a stored web address (`http://` or `https://`, any case) is the item's `url`;
- an `x-apple:` value (any case) gets the template URL when the profile has a username and its service has a known template;
- otherwise `url` is empty.
**Validates: Requirements 2.4, 2.5, 2.6**

### Property 7: Contact channels and extra fields
`phone` and `email` are the first stored values, or `""` when there are none, in every projection including `["all"]`. Named `fields` are added with the list tool's names and rules, and unknown names raise the same error.
**Validates: Requirements 2.7, 2.8**

### Property 8: Small pages
On the user's account, every one-day window's first page serializes to at most 16,000 bytes.
**Validates: Requirements 2.9**

### Property 9: Paging
Pages hold at most `page_size` people (default 50). `next_cursor` is set exactly when more remain, and `total` counts every person in the window. A cursor from another start date, window or tool raises the "start again" error.
**Validates: Requirements 3.1, 3.3, 3.4, 3.5, 3.6**

### Property 10: Validation before any request
The following are rejected before any request is sent:
- a page size outside 1–200;
- a window outside 1–366 days;
- a start date that is not a real `YYYY-MM-DD` date;
- a window that would end after 9999-12-31.

The bounds are also declared in the tool schema.
**Validates: Requirements 3.2, 3.7, 3.8**

### Property 11: Fresh data, no data on failure
Every call obtains its people through `_people`, so it runs the change check with the caller's credentials. Any failure raises and returns no entries.
**Validates: Requirements 4.1, 4.3**

### Property 12: Warm calls are fast
On the user's account, each of 10 consecutive warm calls (today and a 7-day window) finishes in under 0.5 s, including the change check.
**Validates: Requirements 4.2**

### Property 13: Read-only and compatible
The tool issues only GET, PROPFIND and REPORT requests. The schemas and descriptions of the six existing contact tools keep their pre-feature digests.
**Validates: Requirements 4.4, 4.5**

### Property 14: Guidance
The tool description covers the default start date, the window, the entry keys and paging. The README contains a generic example instruction for showing today's birthdays with social links.
**Validates: Requirements 5.1, 5.2**

## Error Handling / Edge Cases

| Case | Behaviour |
|---|---|
| `days` outside 1–366, or not an integer | `ValueError("days must be a whole number from 1 to 366")`; also rejected by the schema |
| Bad `start_date` (`2026-13-01`, `11/10/2026`, `20261011`) | `ValueError("start_date must be a real date written as YYYY-MM-DD")` |
| Bad cursor, or a cursor for a different window | The existing restart error |
| `DEFAULT_TIMEZONE` names an unknown zone and `start_date` is omitted | The calendar tools' `ValueError("Unknown timezone …")`, raised before any request |
| A window ending after 9999-12-31 | `ValueError("start_date and days must end on or before 9999-12-31")`, before any request |
| A stored date that cannot be read (`BDAY:sometime`, `--02-30`) | That date is skipped |
| A compact stored date (`--0214`, `19850214`) | Read like the dashed forms |
| A significant date with no label | Reported with kind `other` |
| A birthday stored with a later year than the placed year | The occasion is kept without `years` |
| A 366-day window | One person can have two occasions for the same date (the start day and the same day a year later) |
| iCloud errors | Raised by `_people`, mapped by the server's `_run` as for list and search |

## Testing Strategy

| Level | What | Criteria |
|---|---|---|
| Unit: placement | `_month_day` on full, compact, yearless, impossible and junk values; `_place` for February 29 in 2027 and 2028; `_occasions` for a one-day window, a 7-day window across December 31, and a 366-day window starting on a stored date (the duplicate case); `years` with and without a year, and with a later stored year. Also through real vCards built with `build_card`: `BDAY;X-APPLE-OMIT-YEAR=1604:1604-02-29`, an `_$!<Anniversary>!$_` date, a custom label and an unlabelled `X-ABDATE` | R1.1, R1.3–R1.6, R1.9, R2.2, R2.3 |
| Unit: links | https kept, any case; `x-apple:` (any case) Snapchat and Instagram built; unknown service, missing username and a schemeless stored value give `""`; `@`-prefixed and `/`-containing usernames quoted | R2.4–R2.6 |
| Unit: tool function (`_people` and `_today` seams) | `_today` under `Pacific/Kiritimati` and `Pacific/Pago_Pago` (25 hours apart, so the dates always differ) and an unknown zone; default start date; ordering; groups excluded; default entry keys; named fields and `["all"]` (including `phone` and `email`); unknown field error; paging across 3 pages with total and cursor; a cursor from another window or from the list tool rejected; bad days, start date, an end after 9999-12-31 and page size raising before any request; a failing `_people` returning nothing | R1.2, R1.7, R1.8, R2.1, R2.7, R2.8, R3.1–R3.8, R4.1, R4.3 |
| MCP protocol | The schema (bounds, defaults, no `limit`); the description naming `start_date`, `days`, `occasions`, `social_links` and `next_cursor`; the response through the protocol; the six existing contact tools' schema digests unchanged (`contacts_list` and `contacts_search` are added to the pinned set) | R3.2, R3.7, R4.5, R5.1 |
| Live, read-only | 10 warm calls each for today and a 7-day window, timed; all 366 one-day windows, plus a 7-day and a 366-day window, compared with an independent computation over raw vCards; largest one-day page in bytes; every Snapchat and Instagram link in results is `https`; only read methods issued | R1.1, R2.9, R4.2, R4.4 |
| Docs review | README row and example instruction | R5.2 |

## Cross-Cutting Risk Gates

| Gate | Failure mode | Verification | Decision |
|---|---|---|---|
| Security / authorization | Data served without checking the caller's credentials | Goes through `_people` (change check per call); read-only methods only | Applies; covered by Properties 11 and 13 |
| Privacy | Contact values written to logs | No new logging; the live check prints aggregates only | Applies; same rules as list and search |
| Performance | Date parsing over 1,800 people on every call | About 1,700 short strings parsed per call (milliseconds); measured live (Property 12) | Applies; precomputing into `CachedCard` is deferred unless measurements need it |
| Accessibility | — | — | Not applicable: no user interface |
| Observability | — | — | Not applicable beyond existing debug logs |
| Migration / rollout | Clients confused by a new tool | Additive tool; existing schemas pinned by digest | Applies |
| Rollback | — | Revert the feature commits; nothing persisted | Applies; trivial |

## Current Technology Evidence

Not applicable because the design relies only on Python's standard library (`datetime` for date arithmetic and leap years; `zoneinfo`, already used by [`src/icloud_mcp/calendar.py`](../../src/icloud_mcp/calendar.py)) and on the FastMCP 4.1.0 parameter validation this codebase already uses and tests for `page_size` (`Annotated[int, Field(ge=…, le=…)]`). No new or changed library behaviour is used.

## Dependency Security Evidence

No new or upgraded dependency is selected or required: the feature uses the already-installed `fastmcp`, `requests` and `vobject`, plus the Python standard library (`datetime`, `zoneinfo`, `urllib.parse`).
