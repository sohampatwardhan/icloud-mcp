"""Live write check of Contacts lists on throwaway data only (task 3.1).

Run one step at a time, checking the iPhone (and the Mac's Contacts app) in between:

    .venv/bin/python -I .specs/contacts-lists/execution/write_check.py setup   # 2 throwaway contacts + list with #1
    .venv/bin/python -I .specs/contacts-lists/execution/write_check.py edit    # add #2, remove #1
    .venv/bin/python -I .specs/contacts-lists/execution/write_check.py rename  # rename the list
    .venv/bin/python -I .specs/contacts-lists/execution/write_check.py delete  # contacts_group_delete
    .venv/bin/python -I .specs/contacts-lists/execution/write_check.py final   # whole-run diff, read-only

Write guard, installed before any request and self-checked: a PUT is sent only with
``If-None-Match: *`` (a new card, which cannot replace one) or with ``If-Match`` to a card
this run created; a DELETE only with ``If-Match`` to the throwaway list. Every other
write is refused before it is sent. Each step diffs every card's ETag before and after
and fails unless exactly the expected throwaway cards changed. Only throwaway names and
aggregates are printed. State (throwaway URLs, the first snapshot) stays in /tmp.
"""

import json
import sys
from pathlib import Path

import requests

STATE = Path("/tmp/contacts_lists_write_state.json")
READ = {"GET", "HEAD", "OPTIONS", "PROPFIND", "REPORT"}
state = json.loads(STATE.read_text()) if STATE.exists() else {"created": [], "list": None}
sent: list[tuple[str, str]] = []
_original = requests.Session.request


def _canonical(url: str) -> str:
    from icloud_mcp.contacts_cache import canonical_url
    return canonical_url(url)


def guarded(self, method, url, *args, **kwargs):
    method = method.upper()
    headers = kwargs.get("headers") or {}
    if method not in READ:
        own = _canonical(url) in {_canonical(u) for u in state["created"]}
        new = method == "PUT" and headers.get("If-None-Match") == "*" and "If-Match" not in headers
        mine = method in ("PUT", "DELETE") and bool(headers.get("If-Match")) and own
        if method == "DELETE" and _canonical(url) != _canonical(state["list"] or ""):
            mine = False
        if not (new or mine):
            raise RuntimeError(f"blocked {method}: not a new card or a throwaway card of this run")
        sent.append((method, "new" if new else "throwaway"))
    return _original(self, method, url, *args, **kwargs)


requests.Session.request = guarded
for attempt in (("PUT", {}), ("PUT", {"If-Match": '"x"'}), ("DELETE", {"If-Match": '"x"'}), ("POST", {})):
    try:
        requests.Session().request(attempt[0], "https://contacts.icloud.com/never-sent.vcf", headers=attempt[1])
    except RuntimeError:
        continue
    sys.exit(f"write guard failed for {attempt}; aborting before any request")
print("write guard: only new cards and this run's throwaway cards can be written")

from icloud_mcp import contacts as C  # noqa: E402
from icloud_mcp.auth import require_auth  # noqa: E402

LIST, LIST_RENAMED = "ZZ MCP List Test", "ZZ MCP List Test Renamed"
PEOPLE = ("ZZ MCP List Test 1", "ZZ MCP List Test 2")


def snapshot() -> dict[str, tuple[str, str]]:
    session = C._get_carddav_session(*require_auth())
    return {_canonical(i["url"]): ((i.get("etag") or "").strip(), i["data"])
            for i in C._fetch_all_vcards(session, C._default_addressbook_url(session))}


def diff(before, after):
    changed = {u for u in before.keys() & after.keys() if before[u][0] != after[u][0]}
    return changed, after.keys() - before.keys(), before.keys() - after.keys()


def report_diff(before, after, expect_changed=(), expect_added=(), expect_removed=()) -> bool:
    changed, added, removed = diff(before, after)
    ok = (changed == {_canonical(u) for u in expect_changed} and added == {_canonical(u) for u in expect_added}
          and removed == {_canonical(u) for u in expect_removed})
    print(f"ETag diff over {len(before)} cards: {len(changed)} changed, {len(added)} added, {len(removed)} removed"
          f" -> {'only the expected throwaway cards' if ok else 'UNEXPECTED'}")
    return ok


def untouched_lines(old: str, new: str) -> bool:
    keep = lambda text: [line for line in C._logical_lines(text)[0]  # noqa: E731
                         if C._prop_name(line) not in {"X-ADDRESSBOOKSERVER-MEMBER", "FN", "N", "REV"}]
    return keep(old) == keep(new)


def show_list() -> None:
    groups = [g for g in C.list_groups()["groups"] if g["name"].startswith("ZZ MCP")]
    for group in groups:
        members = C.list_contacts(group_id=group["id"])["contacts"]
        print(f"MCP: list {group['name']!r} member_count={group['member_count']} members={[m['name'] for m in members]}")
    if not groups:
        print("MCP: no ZZ MCP list")
    for url in state["created"]:
        if url != state["list"]:
            try:
                contact = C.get_contact(url)
                print(f"MCP: {contact['name']!r} groups={[g['name'] for g in contact['groups']]}")
            except requests.HTTPError as e:
                print(f"MCP: throwaway contact GET -> {e.response.status_code}")


step = sys.argv[1] if len(sys.argv) > 1 else ""
before = snapshot()
ok = True
if step == "setup":
    assert not state["created"], "setup already ran; see /tmp state"
    state["baseline"] = {u: e for u, (e, _) in before.items()}
    existing = [p for p in PEOPLE if any(f"\nFN:{p}\r" in t or f"\nFN:{p}\n" in t for _, t in before.values())]
    assert not existing, f"throwaway contacts already exist: {existing}"
    ids = []
    for name in PEOPLE:
        created = C.create_contact(name=name)
        state["created"].append(created["id"])
        STATE.write_text(json.dumps(state))
        ids.append(created["id"])
    group = C.create_group(LIST, members=[ids[0]])
    state["list"] = group["id"]
    state["created"].append(group["id"])
    STATE.write_text(json.dumps(state))
    print(f"create_group -> name={group['name']!r} member_count={group['member_count']}")
    after = snapshot()
    ok &= report_diff(before, after, expect_added=state["created"])
    stored = after[_canonical(group["id"])][1]
    print("stored list card lines:", [C._prop_name(line) for line in C._logical_lines(stored)[0]])
    print("stored list card uses CRLF:", "\r\n" in stored, "| PRODID kept:", "PRODID:-//icloud-mcp//Contacts lists//EN" in stored)
elif step in ("edit", "rename"):
    list_url = state["list"]
    one, two = state["created"][:2]
    if step == "edit":
        result = C.update_group(list_url, add=[two], remove=[one])
    else:
        result = C.update_group(list_url, name=LIST_RENAMED)
    shown = {k: (v if k in ("name", "member_count") else len(v)) for k, v in result.items() if k != "id"}
    print(f"update_group -> {shown}")
    after = snapshot()
    ok &= report_diff(before, after, expect_changed=[list_url])
    same = untouched_lines(before[_canonical(list_url)][1], after[_canonical(list_url)][1])
    print("list card: every line other than members, FN, N and REV byte-identical:", same)
    ok &= same
elif step == "delete":
    result = C.delete_group(state["list"])
    print(f"delete_group -> {result['message']!r}")
    after = snapshot()
    ok &= report_diff(before, after, expect_removed=[state["list"]])
    people_left = all(_canonical(u) in after for u in state["created"][:2])
    print("throwaway contacts still present:", people_left)
    ok &= people_left
    ok &= not any(g["name"].startswith("ZZ MCP") for g in C.list_groups()["groups"])
elif step == "final":
    baseline = state["baseline"]
    now = {u: e for u, (e, _) in before.items()}
    changed = {u for u in baseline.keys() & now.keys() if baseline[u] != now[u]}
    added, removed = now.keys() - baseline.keys(), baseline.keys() - now.keys()
    own = {_canonical(u) for u in state["created"]}
    print(f"whole run vs the first snapshot ({len(baseline)} cards): {len(changed)} changed, {len(added)} added,"
          f" {len(removed)} removed; all of them throwaway cards: {(changed | added | removed) <= own}")
    ok &= (changed | added | removed) <= own and not removed
else:
    sys.exit(__doc__)

show_list()
print("writes sent:", sent or "none", "| result:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
