"""Read-only live check of contacts upcoming dates (task 2.1).

Runs the real ``upcoming_dates`` code against the iCloud account in the local,
git-ignored ``.env`` and prints aggregates only: timings, byte sizes and counts. No
names, dates, links or other contact values are printed or written.

Read-only by construction: every HTTP method except GET, HEAD, OPTIONS, PROPFIND and
REPORT is refused at ``requests.Session.request`` before anything is sent, and the guard
is self-checked before the first request.

The expected results are computed independently from the raw vCards (``BDAY`` and
``X-ABDATE`` lines read with regular expressions), without ``build_card`` or the
module's date helpers.

Usage, from the repository root:
    .venv/bin/python -I .specs/contacts-upcoming-dates/execution/live_check.py [results.md]
"""

import datetime
import json
import re
import statistics
import sys
import time
from pathlib import Path

import requests

READ_METHODS = {"GET", "HEAD", "OPTIONS", "PROPFIND", "REPORT"}
methods: list[str] = []
_original = requests.Session.request


def guarded(self, method, url, *args, **kwargs):
    if method.upper() not in READ_METHODS:
        raise RuntimeError(f"blocked {method}: this check is read-only")
    methods.append(method.upper())
    return _original(self, method, url, *args, **kwargs)


requests.Session.request = guarded
for blocked in ("put", "delete", "post", "patch"):
    try:
        getattr(requests.Session(), blocked)("https://contacts.icloud.com/never-sent")
    except RuntimeError:
        continue
    sys.exit(f"write guard failed for {blocked.upper()}; aborting before any request")
print("write guard: PUT/DELETE/POST/PATCH refused before any network call")

from icloud_mcp import contacts as C  # noqa: E402  (imported after the guard is installed)
from icloud_mcp.auth import require_auth  # noqa: E402
from icloud_mcp.contacts_cache import canonical_url  # noqa: E402

assert C.config.CONTACTS_CACHE, "run with the cache enabled (CONTACTS_CACHE unset or true)"
results_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("live-results.md")
today = C._today()


# --- independent expectation from raw vCards ---------------------------------------------------
session = C._get_carddav_session(*require_auth())
items = C._fetch_all_vcards(session, C._default_addressbook_url(session))
GROUP = re.compile(r"(?im)^X-ADDRESSBOOKSERVER-KIND:\s*group\s*$")
DATE_LINE = re.compile(r"(?im)^(?:[\w-]+\.)?(BDAY|X-ABDATE)((?:;[^:\r\n]*)?):([^\r\n]*)\r?$")  # vCards use CRLF
VALUE = re.compile(r"\s*(?:(\d{4})-?|--)(\d{2})-?(\d{2})")
stored: dict[str, list[tuple[int, int]]] = {}
groups = unreadable = 0
for item in items:
    text = re.sub(r"\r?\n[ \t]", "", item["data"])
    if GROUP.search(text):
        groups += 1
        continue
    dates = []
    for _prop, _params, value in DATE_LINE.findall(text):
        match = VALUE.match(value)
        if not match:
            unreadable += 1
            continue
        month, day = int(match.group(2)), int(match.group(3))
        try:
            datetime.date(2000, month, day)
        except ValueError:
            unreadable += 1
            continue
        dates.append((month, day))
    if dates:
        stored[canonical_url(item["url"])] = dates


def expected(start: datetime.date, days: int) -> set[str]:
    end = start + datetime.timedelta(days=days - 1)
    hits = set()
    for url, dates in stored.items():
        for month, day in dates:
            for year in {start.year, end.year}:
                leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
                placed = datetime.date(year, 2, 28) if (month, day) == (2, 29) and not leap else datetime.date(year, month, day)
                if start <= placed <= end:
                    hits.add(url)
    return hits


def traverse(start: datetime.date, days: int, page_size: int = 200):
    entries, cursor, largest = [], None, (0, 0)
    while True:
        page = C.upcoming_dates(start_date=start.isoformat(), days=days, page_size=page_size, cursor=cursor)
        entries.extend(page["contacts"])
        largest = max(largest, (len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()),
                                len(json.dumps(page, ensure_ascii=False).encode())))
        cursor = page["next_cursor"]
        if cursor is None:
            return entries, page["total"], largest


def timed(**kwargs):
    started = time.perf_counter()
    C.upcoming_dates(**kwargs)
    return time.perf_counter() - started


# --- 1. speed: cold, then 10 warm calls each for today and a week -------------------------------
cold = timed()
warm_today = [timed() for _ in range(10)]
warm_week = [timed(days=7) for _ in range(10)]

# --- 2. every one-day window for a year, with the default page size ------------------------------
# One change check, then the real filtering, paging and projection on that live snapshot: a
# first run that made one iCloud request per window was answered with HTTP 503 partway through.
snapshot = C._people(C._get_carddav_session(*require_auth()), require_auth())
live_people = C._people
C._people = lambda session, credentials: snapshot
day_mismatches, largest_day, busiest, today_bytes = 0, (0, 0), 0, 0
for offset in range(366):
    start = today + datetime.timedelta(days=offset)
    page = C.upcoming_dates(start_date=start.isoformat())
    ids = [canonical_url(c["id"]) for c in page["contacts"]]
    size = (len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()),
            len(json.dumps(page, ensure_ascii=False).encode()))
    largest_day = max(largest_day, size)
    busiest = max(busiest, page["total"])
    if offset == 0:
        today_bytes = size[0]
    if set(ids) != expected(start, 1) or len(ids) != len(set(ids)) or page["total"] != len(ids) or page["next_cursor"]:
        day_mismatches += 1

# --- 3. a week and a full year, paged ------------------------------------------------------------
windows = {}
for days in (7, 366):
    entries, total, _ = traverse(today, days)
    ids = [canonical_url(c["id"]) for c in entries]
    windows[days] = {"returned": len(ids), "distinct": len(set(ids)), "total": total, "expected": len(expected(today, days)),
                     "match": set(ids) == expected(today, days) and len(ids) == len(set(ids)) == total}
    if days == 366:
        year_entries = entries

C._people = live_people

# --- 4. links ------------------------------------------------------------------------------------
link_kinds = {"snapchat": [0, 0], "instagram": [0, 0]}  # [https, other]
for entry in year_entries:
    for link in entry["social_links"]:
        if link["service"] in link_kinds:
            link_kinds[link["service"]][0 if link["url"].startswith("https://") else 1] += 1

checks = {
    "every warm call under 0.5 s": max(warm_today + warm_week) < 0.5,
    "every one-day window matches the independent computation": day_mismatches == 0,
    "the 7-day and 366-day windows match it": all(w["match"] for w in windows.values()),
    "largest one-day page at most 16,000 bytes": max(largest_day) <= 16_000,
    "every Snapchat and Instagram link is https": all(other == 0 for _, other in link_kinds.values()),
    "only read methods issued": set(methods) <= READ_METHODS,
}


def ms(values):
    return ", ".join(f"{v * 1000:.0f}" for v in values)


report = f"""# Live Results: Upcoming Birthdays and Dates

Read-only run of [live_check.py](live_check.py) against the user's iCloud account. Aggregates only.

## Speed (R4.2)

Each call opens a fresh HTTP session, as the server does, and includes the change check.

| Call | Raw timings (ms) | Median | Max |
|---|---|---:|---:|
| Cold call (discovery, sync token, full download) | {ms([cold])} | — | — |
| 10 warm calls, today | {ms(warm_today)} | {statistics.median(warm_today) * 1000:.0f} ms | {max(warm_today) * 1000:.0f} ms |
| 10 warm calls, 7 days | {ms(warm_week)} | {statistics.median(warm_week) * 1000:.0f} ms | {max(warm_week) * 1000:.0f} ms |

## Correctness (R1.1)

The windows below run the real `upcoming_dates` on one live snapshot taken with a single change check. An earlier run that sent one iCloud request per window got HTTP 503 from iCloud partway through. iCloud then answered a fresh call and a 40-call burst normally, so the error was transient on iCloud's side.

- Raw download: {len(items)} cards, {groups} Contacts lists, {len(stored)} people with a readable birthday or significant date, {unreadable} unreadable date values.
- All 366 one-day windows from today: {366 - day_mismatches} of 366 match the independent computation exactly (same people, each once, matching total, a single page).
- Busiest single day: {busiest} people.
- 7-day window: {windows[7]['returned']} returned, {windows[7]['distinct']} distinct, total {windows[7]['total']}, {windows[7]['expected']} expected.
- 366-day window: {windows[366]['returned']} returned, {windows[366]['distinct']} distinct, total {windows[366]['total']}, {windows[366]['expected']} expected.

## Size (R2.9)

- Today's answer: {today_bytes:,} bytes of compact JSON (about {today_bytes // 4:,} tokens).
- Largest one-day page over the year: {largest_day[0]:,} bytes compact ({largest_day[1]:,} with spaces), against a 16,000-byte limit.

## Links (R2.5, R2.6)

- Snapchat links in the full-year results: {link_kinds['snapchat'][0]} https, {link_kinds['snapchat'][1]} other.
- Instagram links in the full-year results: {link_kinds['instagram'][0]} https, {link_kinds['instagram'][1]} other.

## Checks

""" + "\n".join(f"- {'Pass' if ok else 'FAIL'}: {name}." for name, ok in checks.items()) + "\n"

results_path.write_text(report, encoding="utf-8")
print(report)
sys.exit(0 if all(checks.values()) else 1)
