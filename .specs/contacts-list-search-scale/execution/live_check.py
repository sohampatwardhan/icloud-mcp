"""Read-only live check of paged contacts list and search (task 3.1).

Runs the real ``list_contacts`` / ``search_contacts`` code against the iCloud account in
the local, git-ignored ``.env`` and prints aggregates only: timings, byte sizes and
counts. No names, numbers or other contact values are printed or written.

Read-only by construction: every HTTP method except GET, HEAD, OPTIONS, PROPFIND and
REPORT is refused at ``requests.Session.request`` before anything is sent, and the guard
is self-checked before the first request.

Usage, from the repository root:
    .venv/bin/python -I .specs/contacts-list-search-scale/execution/live_check.py [results.md]
"""

import hashlib
import json
import re
import statistics
import sys
import time
from pathlib import Path

import requests
import vobject

READ_METHODS = {"GET", "HEAD", "OPTIONS", "PROPFIND", "REPORT"}
traffic: list[tuple[str, int]] = []
_original = requests.Session.request


def guarded(self, method, url, *args, **kwargs):
    if method.upper() not in READ_METHODS:
        raise RuntimeError(f"blocked {method}: this check is read-only")
    response = _original(self, method, url, *args, **kwargs)
    traffic.append((method.upper(), len(response.content)))
    return response


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
WARM_QUERIES = ["a", "e", "jo", "an", "1", "ma", "@", "li", "55", "o"]  # broad: worst-case ranking work
results_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("live-results.md")


def timed(fn, *args, **kwargs):
    before = len(traffic)
    started = time.perf_counter()
    value = fn(*args, **kwargs)
    return value, time.perf_counter() - started, [m for m, _ in traffic[before:]]


def page_bytes(page) -> tuple[int, int]:
    compact = json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()
    spaced = json.dumps(page, ensure_ascii=False).encode()
    return len(compact), len(spaced)


# 1. Cold call, then 10 warm list calls and 10 warm searches, each with a fresh session.
_, cold_s, cold_methods = timed(C.list_contacts)
warm_list = [timed(C.list_contacts) for _ in range(10)]
warm_search = [timed(C.search_contacts, q if q != "@" else "@a") for q in WARM_QUERIES]
warm_list_s = [t for _, t, _ in warm_list]
warm_search_s = [t for _, t, _ in warm_search]
warm_requests = {tuple(m) for _, _, m in warm_list + warm_search}

# 2. Full traversal with default pages.
ids, pages, largest, cursor = [], 0, (0, 0), None
while True:
    page = C.list_contacts(cursor=cursor)
    pages += 1
    ids.extend(c["id"] for c in page["contacts"])
    if len(page["contacts"]) == 50:
        largest = max(largest, page_bytes(page))
    total, cursor = page["total"], page["next_cursor"]
    if cursor is None:
        break

# 3. Independent full download: every non-group card, without build_card.
session = C._get_carddav_session(*require_auth())
items = C._fetch_all_vcards(session, C._default_addressbook_url(session))
group = re.compile(r"(?im)^X-ADDRESSBOOKSERVER-KIND:\s*group\s*$")
direct, unparsable, groups, names = set(), 0, 0, {}
for item in items:
    text = re.sub(r"\r?\n[ \t]", "", item["data"])
    if group.search(text):
        groups += 1
        continue
    try:
        card = vobject.readOne(item["data"])
    except Exception:
        unparsable += 1
        continue
    key = canonical_url(item["url"])
    direct.add(key)
    names[key] = C._display_name(card)
traversed = [canonical_url(i) for i in ids]

# 4. Accent probes: people whose display name has letters that fold to plain ASCII.
def probe_words(name):
    return [w for w in name.split() if not w.isascii() and C.fold(w).isascii() and C.fold(w).isalpha()]


targets = sorted((k for k, n in names.items() if probe_words(n)), key=lambda k: hashlib.sha256(k.encode()).hexdigest())
probes = {"single word": [0, 0], "full name, reversed": [0, 0]}


def search_ids(query):
    found, cur = set(), None
    while True:
        page = C.search_contacts(query, page_size=200, cursor=cur, fields=[])
        found.update(canonical_url(c["id"]) for c in page["contacts"])
        cur = page["next_cursor"]
        if cur is None:
            return found


for key in targets[:25]:
    word = C.fold(probe_words(names[key])[0])
    reversed_name = " ".join(reversed(C.fold(names[key]).split()))
    for label, query in (("single word", word), ("full name, reversed", reversed_name)):
        probes[label][1] += 1
        probes[label][0] += key in search_ids(query)

checks = {
    "every warm call under 0.5 s": max(warm_list_s + warm_search_s) < 0.5,
    "each warm call is one REPORT": warm_requests == {("REPORT",)},
    "largest default page at most 16,000 bytes": max(largest) <= 16_000,
    "traversal equals direct download": set(traversed) == direct and len(traversed) == len(direct) == total,
    "every person exactly once": len(traversed) == len(set(traversed)),
    "every accent probe finds its target": all(f == n for f, n in probes.values()) and probes["single word"][1] > 0,
}


def ms(values):
    return ", ".join(f"{v * 1000:.0f}" for v in values)


report = f"""# Live Results: Contacts List Size and Search Speed

Read-only run of [live_check.py](live_check.py) against the user's iCloud account. Aggregates only.

## Speed (R4.1)

Each call opens a fresh HTTP session, as the server does, and includes the change check.

| Call | Raw timings (ms) | Median | Max |
|---|---|---:|---:|
| Cold list (discovery, sync token, full download) | {ms([cold_s])} | — | — |
| 10 warm `list_contacts` | {ms(warm_list_s)} | {statistics.median(warm_list_s) * 1000:.0f} ms | {max(warm_list_s) * 1000:.0f} ms |
| 10 warm `search_contacts` | {ms(warm_search_s)} | {statistics.median(warm_search_s) * 1000:.0f} ms | {max(warm_search_s) * 1000:.0f} ms |

- Cold call requests: {len(cold_methods)} ({", ".join(sorted(set(cold_methods)))}).
- Warm call requests: {"one REPORT each" if warm_requests == {("REPORT",)} else sorted(warm_requests)}.

## Page size (R2.5)

- Full traversal: {pages} default pages of up to 50 summaries, {total} people.
- Largest full page: {largest[0]:,} bytes of compact JSON ({largest[1]:,} bytes with spaces), against a 16,000-byte limit.

## Completeness (R1.6)

- Direct download: {len(items)} cards, {groups} Contacts lists, {unparsable} unparsable, {len(direct)} people.
- Traversal: {len(traversed)} ids, {len(set(traversed))} distinct; {len(set(traversed) - direct)} not in the download, {len(direct - set(traversed))} missing.

## Accent-folded search (R3.1)

- People whose name has letters that fold to ASCII: {len(targets)}; probed: {min(len(targets), 25)}.
""" + "\n".join(f"- {label}: {f} of {n} found." for label, (f, n) in probes.items()) + """

## Checks

""" + "\n".join(f"- {'Pass' if ok else 'FAIL'}: {name}." for name, ok in checks.items()) + "\n"

results_path.write_text(report, encoding="utf-8")
print(report)
sys.exit(0 if all(checks.values()) else 1)
