"""Read-only live check of Contacts lists (task 2.1).

Runs the real ``list_groups``, ``list_contacts(group_id=...)``, ``list_contacts(fields=
["groups"])`` and ``get_contact`` against the iCloud account in the local, git-ignored
``.env`` and prints aggregates only: counts, timings and byte sizes. No names, ids or
other contact values are printed or written.

Read-only by construction: every HTTP method except GET, HEAD, OPTIONS, PROPFIND and
REPORT is refused at ``requests.Session.request`` before anything is sent, and the guard
is self-checked before the first request.

The expected results are computed independently from the raw vCards (``X-ADDRESSBOOKSERVER-
KIND``, ``X-ADDRESSBOOKSERVER-MEMBER``, ``UID`` and ``FN`` lines read with regular
expressions), without ``build_card`` or the module's list helpers.

Usage, from the repository root:
    .venv/bin/python -I .specs/contacts-lists/execution/live_check.py [results.md]
"""

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


def compact(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


# --- independent expectation from raw vCards ---------------------------------------------------
session = C._get_carddav_session(*require_auth())
items = C._fetch_all_vcards(session, C._default_addressbook_url(session))
KIND = re.compile(r"(?im)^(?:[\w-]+\.)?X-ADDRESSBOOKSERVER-KIND(?:;[^:\r\n]*)?:\s*group\s*\r?$")
MEMBER = re.compile(r"(?im)^(?:[\w-]+\.)?X-ADDRESSBOOKSERVER-MEMBER(?:;[^:\r\n]*)?:([^\r\n]*?)\r?$")
UID = re.compile(r"(?im)^(?:[\w-]+\.)?UID(?:;[^:\r\n]*)?:([^\r\n]*?)\r?$")
FN = re.compile(r"(?im)^(?:[\w-]+\.)?FN(?:;[^:\r\n]*)?:([^\r\n]*?)\r?$")


def unescape(value: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


raw_groups: dict[str, tuple[str, list[str]]] = {}   # canonical url -> (name, member uid keys)
uid_people: dict[str, set[str]] = {}               # uid.casefold() -> canonical person urls
people_urls: set[str] = set()
escaped_uids = 0
for item in items:
    text = re.sub(r"\r?\n[ \t]", "", item["data"])
    url = canonical_url(item["url"])
    if KIND.search(text):
        fn = FN.search(text)
        refs = []
        for value in MEMBER.findall(text):
            value = unescape(value).strip()  # TEXT escapes (\, \n \") mean the same UID unescaped
            refs.append((value[9:] if value.lower().startswith("urn:uuid:") else value).casefold())
        raw_groups[url] = (unescape(fn.group(1)).strip() if fn else "", refs)
        continue
    people_urls.add(url)
    uid = UID.search(text)
    if uid and uid.group(1).strip():
        escaped_uids += "\\" in uid.group(1)
        uid_people.setdefault(unescape(uid.group(1)).strip().casefold(), set()).add(url)

expected_members = {url: set().union(*(uid_people.get(ref, set()) for ref in refs)) if refs else set()
                    for url, (_, refs) in raw_groups.items()}
expected_groups_of: dict[str, set[str]] = {}
for group_url, members in expected_members.items():
    for person in members:
        expected_groups_of.setdefault(person, set()).add(group_url)
all_refs = [ref for _, refs in raw_groups.values() for ref in refs]
dangling = sum(1 for ref in all_refs if ref not in uid_people)
shared_uids = sum(1 for urls in uid_people.values() if len(urls) > 1)

# --- 1. contacts_groups: names, ids, counts, order -----------------------------------------------
cold_started = time.perf_counter()
listing = C.list_groups()
cold = time.perf_counter() - cold_started
got = {canonical_url(g["id"]): g for g in listing["groups"]}
group_checks = {
    "ids": set(got) == set(raw_groups),
    "names": all(got[u]["name"] == raw_groups[u][0] for u in raw_groups if u in got),
    "counts": all(got[u]["member_count"] == len(expected_members[u]) for u in raw_groups if u in got),
    "total": listing["total"] == len(raw_groups) == len(listing["groups"]),
    "order": [g["id"] for g in listing["groups"]]
             == [g["id"] for g in sorted(listing["groups"], key=lambda g: (C.fold(g["name"]), g["id"]))],
}

# --- 2. every list's members, paged --------------------------------------------------------------
member_mismatches, member_calls, largest_member_page = 0, 0, 0
for group in listing["groups"]:
    seen, cursor = [], None
    while True:
        page = C.list_contacts(page_size=7, cursor=cursor, group_id=group["id"])
        member_calls += 1
        largest_member_page = max(largest_member_page, compact(page))
        seen.extend(canonical_url(c["id"]) for c in page["contacts"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    expected = expected_members[canonical_url(group["id"])]
    if set(seen) != expected or len(seen) != len(set(seen)) or page["total"] != len(expected):
        member_mismatches += 1

# --- 3. groups on every person -------------------------------------------------------------------
people_seen, groups_mismatches, unsorted, cursor = 0, 0, 0, None
while True:
    page = C.list_contacts(page_size=200, cursor=cursor, fields=["groups"])
    for entry in page["contacts"]:
        people_seen += 1
        ids = [canonical_url(g["id"]) for g in entry["groups"]]
        if set(ids) != expected_groups_of.get(canonical_url(entry["id"]), set()) or len(ids) != len(set(ids)):
            groups_mismatches += 1
        names = [(C.fold(g["name"]), g["id"]) for g in entry["groups"]]
        if names != sorted(names):
            unsorted += 1
    cursor = page["next_cursor"]
    if cursor is None:
        break
in_a_list = sum(1 for urls in expected_groups_of.values() if urls)
most_lists = max((len(urls) for urls in expected_groups_of.values()), default=0)

# --- 4. speed: 10 warm contacts_groups, 5 warm contacts_get on a person in a list ---------------


def timed(fn, *args, **kwargs):
    started = time.perf_counter()
    value = fn(*args, **kwargs)
    return time.perf_counter() - started, value


warm_groups = [timed(C.list_groups)[0] for _ in range(10)]
member = max(expected_groups_of, key=lambda u: (len(expected_groups_of[u]), u)) if expected_groups_of else None
warm_get, get_matches, get_bytes = [], True, 0
if member:
    for _ in range(5):
        elapsed, contact = timed(C.get_contact, member)
        warm_get.append(elapsed)
        get_matches &= {canonical_url(g["id"]) for g in contact["groups"]} == expected_groups_of[member]
        get_bytes = compact(contact["groups"])

# --- 5. how iCloud labels a list card's encoding (header only) ----------------------------------
content_type = ""
if raw_groups:
    response = C._get_carddav_session(*require_auth()).get(listing["groups"][0]["id"], timeout=C.TIMEOUT)
    content_type = response.headers.get("Content-Type", "")

checks = {
    "contacts_groups ids, names, counts, total and order match the independent parse": all(group_checks.values()),
    "every list's paged members match it": member_mismatches == 0,
    "every person's groups match it, sorted": groups_mismatches == 0 and unsorted == 0 and people_seen == len(people_urls),
    "contacts_get groups match it": bool(member) and get_matches,
    "every warm contacts_groups call under 0.5 s": max(warm_groups) < 0.5,
    "only read methods issued": set(methods) <= READ_METHODS,
}


def ms(values):
    return ", ".join(f"{v * 1000:.0f}" for v in values)


report = f"""# Live Results: Contacts Lists

Read-only run of [live_check.py](live_check.py) against the user's iCloud account. Aggregates only.

## Address book

- Raw download: {len(items)} cards, {len(raw_groups)} Contacts lists, {len(people_urls)} people.
- Member references on lists: {len(all_refs)}, of which {dangling} name no card and are skipped.
- People in at least one list: {in_a_list}; the most lists one person is in: {most_lists}.
- UIDs shared by more than one person: {shared_uids}. People whose stored UID contains vCard escapes (`\\,`, `\\n`, `\\"`): {escaped_uids}.
- A list card's `Content-Type` on GET: `{content_type or "none"}`.

## Correctness (R1.1–R1.4, R2.1–R2.3, R3.1–R3.4)

- `contacts_groups`: {", ".join(f"{name} {'match' if ok else 'MISMATCH'}" for name, ok in group_checks.items())}.
- Member pages (page size 7): {len(listing["groups"]) - member_mismatches} of {len(listing["groups"])} lists match exactly, in {member_calls} calls.
- `groups` on every person: {people_seen - groups_mismatches} of {people_seen} match, {unsorted} out of order.
- `contacts_get` on the person in the most lists: groups {"match" if get_matches else "MISMATCH"} in all {len(warm_get)} calls.

## Speed (R6.1)

Each call opens a fresh HTTP session, as the server does, and includes the change check.

| Call | Raw timings (ms) | Median | Max |
|---|---|---:|---:|
| First `contacts_groups` (discovery, sync token, full download) | {ms([cold])} | — | — |
| 10 warm `contacts_groups` | {ms(warm_groups)} | {statistics.median(warm_groups) * 1000:.0f} ms | {max(warm_groups) * 1000:.0f} ms |
| 5 warm `contacts_get` (GET plus change check) | {ms(warm_get)} | {statistics.median(warm_get) * 1000:.0f} ms | {max(warm_get) * 1000:.0f} ms |

## Size (R6.3)

- `contacts_groups`: {compact(listing):,} bytes of compact JSON (about {compact(listing) // 4:,} tokens).
- Largest member page (7 people, summaries): {largest_member_page:,} bytes.
- `groups` on the person in the most lists: {get_bytes:,} bytes.

## Checks

""" + "\n".join(f"- {'Pass' if ok else 'FAIL'}: {name}." for name, ok in checks.items()) + "\n"

results_path.write_text(report, encoding="utf-8")
print(report)
sys.exit(0 if all(checks.values()) else 1)
