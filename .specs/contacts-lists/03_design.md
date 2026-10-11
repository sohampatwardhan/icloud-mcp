# Design: Contacts Lists

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md) · [Design](03_design.md) · [Tasks](04_tasks.md) · [Execution](05_execution.md)
<!-- spec-nav:end -->

## Overview

Contacts lists become first-class in the contact tools, following discovery approach A. There are four new tools, and two existing ones gain additions:

- **`contacts_groups`** (new): reads every list.
- **`contacts_group_create`, `contacts_group_update` and `contacts_group_delete`** (new): change lists.
- **`contacts_list`**: an optional `group_id` returns one list's members.
- **Contact reads**: gain a read-only `groups` key.

The rejected alternatives were editing membership from `contacts_update`, which writes several cards in one call with no transaction, and treating lists as contacts.

**Naming:** the tools, parameters and keys say "group", because a `contacts_lists` tool beside the existing `contacts_list` invites mix-ups. Every description calls them "Contacts lists (groups)" so assistants map the user's word "list" to these tools.

All code lives in [`src/icloud_mcp/contacts.py`](../../src/icloud_mcp/contacts.py) and [`src/icloud_mcp/server.py`](../../src/icloud_mcp/server.py). [`src/icloud_mcp/contacts_cache.py`](../../src/icloud_mcp/contacts_cache.py) is unchanged: it stores whatever `build_card` returns.

## Architecture

```text
server.py                                   contacts.py
contacts_groups          (READ_ONLY)   ──▶  list_groups()
contacts_list(group_id)  (READ_ONLY)   ──▶  list_contacts(..., group_id)
contacts_get             (READ_ONLY)   ──▶  get_contact()              + groups from _book
contacts_group_create    (WRITE)       ──▶  create_group(name, members)
contacts_group_update    (DESTRUCTIVE) ──▶  update_group(group_id, name, add, remove)
contacts_group_delete    (DESTRUCTIVE) ──▶  delete_group(group_id)

reads:  _book(session, credentials) ──▶ _all_cards() ──▶ _cache.cards(...)   one change check
writes: _book (validate) ──▶ GET list card ──▶ text edit ──▶ PUT/DELETE with If-Match ──▶ _cache.mark_dirty
```

## Components and Interfaces

### Cached cards (`contacts.py`)

`CachedCard` gains two fields, both filled by `build_card`:

```python
uid: str = ""                    # the card's UID, which list members refer to; "" when absent
members: tuple[str, ...] = ()    # list cards only: member UIDs in stored order, "urn:uuid:" removed
```

- Member values are read from every `X-ADDRESSBOOKSERVER-MEMBER` property, with or without a group prefix. The `urn:uuid:` prefix is matched case-insensitively. A value without that prefix is kept as it is, so it can still be counted as unresolved.
- `listed` (people) is unchanged.

### The address-book index (`contacts.py`)

```python
def _all_cards(session, credentials) -> list[CachedCard]
    # What _people returns today before filtering: the cache path or, with CONTACTS_CACHE=false,
    # a full download. _people becomes [c for c in _all_cards(...) if c.listed].

@dataclass(frozen=True)
class _Book:
    people: dict[str, CachedCard]    # canonical_url -> person
    groups: dict[str, CachedCard]    # canonical_url -> list card
    by_uid: dict[str, CachedCard]    # uid.casefold() -> person (first card wins on a duplicate UID)

    def members(self, group: CachedCard) -> list[CachedCard]
        # People the list names, each once, in stored order; missing references are skipped (R1.4).
    def groups_of(self) -> dict[str, list[dict[str, str]]]
        # canonical person url -> [{"id", "name"}], sorted by (fold(name), id) (R3.1-R3.3).

def _book(session, credentials) -> _Book
```

UIDs are compared case-insensitively. Measured live, this resolves exactly the same 188 of 280 references as an exact comparison, and it means a reference stored in a different case is never added twice.

### Reading lists

```python
def list_groups() -> dict
    # {"groups": [{"id", "name", "member_count"}], "total": int}, sorted by (fold(name), id).
```

- There is no paging: 15 lists are about 3 KB (R1.1–R1.3, R1.5).
- `member_count` is `len(book.members(group))` (R1.4).

```python
def list_contacts(page_size=50, cursor=None, fields=None, group_id: str | None = None) -> dict
```

- **Without `group_id`:** exactly today's code path and the cursor fingerprint `"list"`, so earlier cursors and output are unchanged (R2.6).
- **With `group_id`:**
  1. `ensure_icloud_url(group_id, "group")` runs before any request (R6.5).
  2. After `_book`, the canonical URL must be in `book.groups`, else `ValueError("group not found: <id>; use contacts_groups for valid ids")` (R2.4).
  3. Results are `book.members(group)` sorted by the same `(0, *sort_key, url)` keys and paged by `_page`, so `total`, page size, cursors and `fields` behave the same (R2.1–R2.3).
  4. The fingerprint is `"group:" + canonical_url(group_id)`, so a cursor from another list or from the unfiltered listing raises the restart error (R2.5).

### A person's lists on reads

- **`CONTACT_FIELDS`** gains `"groups"`, so `fields=["groups"]` is valid for list, search and upcoming dates (R3.4).
- **`_project(card, fields, groups_of=None)`**: `"groups"` is a derived field like `phone`. `["all"]` adds it to the full record. Each call computes `groups_of` only when the fields include `"groups"` or `"all"`, and the summary is unchanged.
- **`_date_entry`** takes the same mapping.
- **`get_contact`** keeps its direct GET of the card. It then adds `contact["groups"] = book.groups_of().get(canonical_url(contact_id), [])` from `_book`, which costs one change check of about 0.15 s. A card that is not a person in the book gets `[]` (R3.1, R3.3).
- **Descriptions:** `contacts_get` names `groups` among its read-only keys, and `contacts_update` names it among the keys to leave out (R3.5).

### Editing a list card's text (`contacts.py`)

List cards are edited as text, never re-serialized, so every byte of every property the request does not touch stays identical. That includes unknown properties such as `X-APPLE-OL-MAPPING-INFO`, folded lines, line endings and missing references (R4.6, R4.7).

```python
def _logical_lines(text: str) -> tuple[list[str], str]
    # The card split into logical lines, each kept with its original folding, plus the newline
    # style ("\r\n" when the card has any, else "\n").
def _unfolded(line: str) -> str
def _prop_name(line: str) -> str          # upper-case name without a group prefix ("ITEM1.FN" -> "FN")
def _member_ref(line: str) -> str | None  # the UID of an X-ADDRESSBOOKSERVER-MEMBER line, else None
def _escape(value: str) -> str            # vCard 3.0 TEXT escaping of \ , ; (newlines are rejected earlier)
def _folded(line: str, newline: str) -> str  # RFC 6350 folding at 75 octets, never inside a UTF-8 character
def _rev(now: datetime.datetime) -> str   # "REV:YYYY-MM-DDTHH:MM:SSZ" in UTC, as iCloud stores it

def _edit_group(text: str, *, name: str | None, add: list[str], remove: list[str], now) -> tuple[str, dict]
    # Returns the new text and {"added", "removed", "unchanged"} as UID lists.
```

`_edit_group` makes these changes and no others:

- **Remove:** drops every member line whose UID equals a UID in `remove`, ignoring case.
- **Add:** inserts one `X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:<uid>` line per UID in `add` that is not already present. The new lines go after the last member line, or before `REV`/`UID`/`END:VCARD` when there is none.
- **Rename:** replaces the value of the `FN` line and the `N` line with `_escape(name)`, because iCloud stores `N` equal to `FN` on every list.
- **REV:** updates `REV` to now when there is any other change, and leaves the card untouched when there is none (R4.8).

```python
def _new_group_text(uid: str, name: str, member_uids: list[str], now) -> str
```

New cards copy the layout of 12 of the user's 15 lists, with CRLF line endings (R5.10):

```text
BEGIN:VCARD
VERSION:3.0
PRODID:-//icloud-mcp//Contacts lists//EN
N:<name>
FN:<name>
X-ADDRESSBOOKSERVER-KIND:group
X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:<member uid>
REV:<now>
UID:<UPPER-CASE UUID4>
END:VCARD
```

### Writing lists (`contacts.py`)

```python
def create_group(name: str, members: list[str] | None = None) -> dict
def update_group(group_id: str, name: str | None = None,
                 add: list[str] | None = None, remove: list[str] | None = None) -> dict
def delete_group(group_id: str) -> dict
```

**Shared checks, before any request (R6.5):**

- `name`, when given, is a string; after `strip()` it is non-empty, at most 255 characters, and free of control characters. An empty name gives `ValueError("group name must not be empty")` (R5.3).
- `group_id` and every member id pass `ensure_icloud_url`.
- `add` and `remove` are lists of strings; duplicates within one list collapse.
- An id in both `add` and `remove` raises (R4.5).
- `update_group` with no `name`, `add` or `remove` raises `ValueError("nothing to change: give name, add or remove")`.

**Checks against the index, before any write:**

- Every member id must be a person in `book.people`, else `ValueError("not a person in this address book: <id>")` (R4.4). A person with no UID cannot be referenced, and gets an error that says so.
- `group_id` must be in `book.groups`, else "group not found" (R5.8). The fetched card must also contain `X-ADDRESSBOOKSERVER-KIND:group`, so a person card is never edited or deleted through these tools.
- A create or rename to a name that, after `fold`, equals another list's name raises `ValueError("a group named … already exists")` (R5.4). For a rename the list itself is excluded, so changing only the case is allowed.

**`create_group`:**

1. Builds the card with `_new_group_text`.
2. Sends `PUT {book}{UID}.vcf` with `If-None-Match: *`.
3. Marks the URL dirty and returns `{"id", "name", "member_count"}` (R5.1, R5.2, R5.11).

**`update_group`** runs at most two attempts:

1. GET the list card and its ETag. A card with no ETag is refused, as `update_contact` does.
2. Apply `_edit_group`. If the text is unchanged, return without writing (R4.8).
3. PUT with `If-Match`. On 412, a request without `name` starts attempt 2 from step 1, applying the same deltas to the newer card (R4.9). On a second 412, or on any 412 when `name` was given, it raises `ValueError("the group changed on another device while it was being updated; nothing was overwritten, retry")` (R4.10, R5.9).
4. Marks the URL dirty and returns `{"id", "name", "member_count", "added", "removed", "unchanged"}`, with members reported as contact ids (R4.1–R4.3, R4.11, R5.5, R5.11).

**`delete_group`:**

1. GETs the card to confirm it is a list and to take its ETag (R5.8).
2. Sends `DELETE` with `If-Match`. A 412 raises the conflict error (R5.6, R5.9).
3. Marks the URL dirty and returns `{"status": "success", "message": "Group <name> deleted; its members were not changed"}`.

**Every write:**

- Only the one list card is sent. No person card is ever written (R5.7).
- An iCloud error is raised as it is, after at most the one conflict retry, with no further write (R6.4).
- `mark_dirty` makes the next read show the change (R6.2).

### Contact_Tools (`server.py`)

```python
GroupId = Annotated[str, Field(description="Group (Contacts list) id from contacts_groups.")]
MemberIds = Annotated[list[str] | None, Field(description="Contact ids (from contacts_list, contacts_search or contacts_get) ...")]

contacts_groups() -> dict                                                    # READ_ONLY
contacts_list(page_size, cursor, fields, group_id: GroupId | None = None)   # READ_ONLY, new last parameter
contacts_group_create(name: str, members: MemberIds = None) -> dict          # WRITE
contacts_group_update(group_id: GroupId, name: str | None = None,
                      add: MemberIds = None, remove: MemberIds = None) -> dict   # DESTRUCTIVE
contacts_group_delete(group_id: GroupId) -> dict                             # DESTRUCTIVE
```

- **Annotations** follow R6.6.
- **Descriptions** say that members are given and returned as contact ids (R7.1), and that deleting a group keeps the people in it (R7.2).
- **One update tool:** the requirements' "membership tool" and "update tool" are the same `contacts_group_update`. A rename and membership changes can be made in one call.
- **`_run`** needs no change: conflicts arrive as `ValueError`, and other HTTP errors keep the existing mapping.

### Project_Docs

The README gets one row per new tool, a note on `group_id` in `contacts_list`, and the `groups` key (R7.3).

## Data Models

**List summary:** `{"id": "https://p…-contacts.icloud.com/…/card/<uid>.vcf", "name": "Family", "member_count": 12}`

**`contacts_groups` response:** `{"groups": [summary, …], "total": 15}`

**`groups` on a person:** `[{"id": "<list id>", "name": "Family"}, …]`

**`contacts_group_update` response:** `{"id", "name", "member_count", "added": [contact id, …], "removed": [...], "unchanged": [...]}`

**Cursor with `group_id`:** the existing format, with `q` set to the fingerprint of `"group:" + canonical list URL`.

## Correctness Properties

### Property 1: Every list, counted as Contacts shows it
`list_groups` returns one summary per list card in the address book and none for people, sorted by folded name then id. `member_count` is the number of distinct people the list names, skipping missing references. An address book without lists gives `{"groups": [], "total": 0}`.
**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5**

### Property 2: Members page like the address book
With `group_id`, `list_contacts` returns exactly `book.members(group)` in name order. `total` is their number, and paging, page size and `fields` behave as without it. Without `group_id`, the output equals the pre-feature output.
**Validates: Requirements 2.1, 2.2, 2.3, 2.6**

### Property 3: Unknown lists and foreign cursors are refused
A `group_id` that is not a list in the book raises "group not found". A cursor from another list, from the unfiltered listing or from search raises the restart error.
**Validates: Requirements 2.4, 2.5**

### Property 4: A person's lists
For every person, `groups` lists exactly the lists that name that person's UID (ignoring case), sorted by folded name, and is empty for a person in no list. This holds in `contacts_get`, in `["all"]` and when named in `fields`. The read-only status is stated in the `contacts_get` and `contacts_update` descriptions.
**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

### Property 5: Membership deltas
After a successful update, the list names every added person and none of the removed people. People already in the requested state are reported as unchanged. An id in both `add` and `remove`, or one that is not a person in the book, raises before any write.
**Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.11**

### Property 6: Byte-faithful edits
For any list card and any request, the new text equals the old text with only these changes: the named member lines removed, the new member lines inserted, the `FN` and `N` values replaced on a rename, and `REV` updated. Every other logical line, including its folding and newline style, is byte-identical. A request that changes nothing produces identical text and no PUT.
**Validates: Requirements 4.6, 4.7, 4.8**

### Property 7: Conflicts
A membership-only update that gets one 412 re-reads the card and applies the same deltas to the newer version, so a member added on a device stays. A second 412, or a 412 on a rename or a delete, raises the conflict error with no further request.
**Validates: Requirements 4.9, 4.10, 5.9**

### Property 8: Create, rename, delete
- **Create** sends one PUT with `If-None-Match: *` of a card in the iCloud list layout, with the given name and members.
- **Rename** changes `FN` and `N` only.
- **Delete** sends one conditional DELETE.
- **Refusals:** an empty name, a name another list already has (folded), or an id that is not a list card raises before any write.
- **Returns:** id, name and member count.
**Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.8, 5.10, 5.11**

### Property 9: Person cards are never written
Across every list tool, the only PUT or DELETE targets are list cards, or a new list URL under the address book.
**Validates: Requirements 5.7**

### Property 10: Fresh, fast and fail-closed
- **Freshness:** every list read and every write's validation goes through `_book`, so it runs the change check with the caller's credentials.
- **After a write:** `mark_dirty` makes the next read show the change.
- **On error:** an iCloud error raises with no further write.
- **Speed:** on the user's account, 10 consecutive warm `contacts_groups` calls each finish in under 0.5 s.
**Validates: Requirements 6.1, 6.2, 6.3, 6.4**

### Property 11: Validation and compatibility
- **Before any request:** malformed arguments raise.
- **Annotations:** `contacts_groups` is read-only, create is a write, and update and delete are destructive.
- **Schemas:** `contacts_create`, `contacts_delete`, `contacts_search` and `contacts_upcoming_dates` keep their digests.
- **Planned changes:** `contacts_list` changes only by the optional last parameter `group_id`, and `contacts_get` and `contacts_update` only by the description text about `groups`. Their new digests are pinned.
**Validates: Requirements 6.5, 6.6, 6.7**

### Property 12: Guidance
The descriptions say that members are contact ids and that deleting a group keeps its people. The README documents every list tool, `group_id` and the `groups` key.
**Validates: Requirements 7.1, 7.2, 7.3**

## Error Handling / Edge Cases

| Case | Behaviour |
|---|---|
| A missing reference on a list | Skipped in counts, members and `groups`; kept byte-identical on every edit |
| A member stored with a UID in another case | Treated as the same person; never added twice |
| A folded member, `FN` or unknown line | Kept with its folding; a renamed `FN`/`N` is re-folded at 75 octets |
| A list card without `REV` | No `REV` is added on edit; a new list gets one |
| A list card with LF instead of CRLF | New lines use the card's own newline style |
| A person without a UID | Cannot be added; the error names the contact id |
| A name with a comma, semicolon or backslash | Escaped in `FN` and `N`; read back unescaped |
| A name with a line break or other control character | Rejected before any request |
| A delete or update `group_id` that names a person | "group not found", and nothing is sent |
| `contacts_get` on a list card | Returns the card as today, with `groups: []` |
| `CONTACTS_CACHE=false` | `_book` does a full download, so `contacts_get` costs one more download; documented in the README |
| A create racing another create with the same UID | iCloud answers 412 to `If-None-Match`; mapped to "already exists" |

## Testing Strategy

| Level | What | Criteria |
|---|---|---|
| Unit: card text | `_logical_lines`/`_unfolded` on CRLF, LF and folded cards; `_member_ref` with group prefixes and case; `_escape`; `_folded` on long and multi-byte names; `_edit_group` on the five real layouts measured live (no members, unknown property, no `REV`, kind before `PRODID`), checking that every untouched logical line is byte-identical; `_new_group_text` against the iCloud layout | R4.6–R4.8, R5.10 |
| Unit: index | `build_card` fills `uid` and `members`; `_Book.members` skips missing references and duplicates; case-insensitive UIDs; `groups_of` ordering | R1.4, R3.1–R3.3 |
| Unit: reads (`_all_cards` seam) | `list_groups` ordering, counts and empty book; `list_contacts(group_id=…)` members, total, paging, fields, unknown group, foreign cursors; output without `group_id` unchanged; `fields=["groups"]` and `["all"]` in list, search and upcoming dates; `get_contact` adds `groups` | R1.1–R1.5, R2.1–R2.6, R3.1–R3.4 |
| Unit: writes (fake session recording requests) | Add, remove, mixed, no-op (no PUT), unknown and overlapping ids (no PUT), rename, duplicate name, case-only rename, create with and without members, delete; a person id given as `group_id` (no write); 412 once then success for membership; 412 twice; 412 on rename and delete; malformed arguments sending no request; only list URLs ever written | R4.1–R4.11, R5.1–R5.9, R5.11, R6.4, R6.5 |
| MCP protocol | New tool schemas and annotations; the `group_id` parameter; descriptions mention contact ids and that deletion keeps people; pinned digests (four unchanged, three re-pinned) | R3.5, R6.6, R6.7, R7.1, R7.2 |
| Live, read-only | `contacts_groups` names and counts against an independent parse of the raw vCards; every list's member pages against the same parse; 10 warm calls timed; output bytes; only read methods | R1.1–R1.4, R2.1–R2.3, R6.1, R6.3 |
| Live write (throwaway data, after the user's explicit yes) | Create a list with one throwaway contact; add a second, remove the first; rename; check on the iPhone; delete; ETags of every other card unchanged before and after | R4.1, R4.2, R5.1, R5.5–R5.7, R5.10, R6.2 |
| Docs review | README rows and notes | R7.3 |

## Cross-Cutting Risk Gates

| Gate | Failure mode | Verification | Decision |
|---|---|---|---|
| Security / authorization | A caller-supplied URL receives the user's credentials, or a tool writes a person card | Every id passes `ensure_icloud_url` and must be a card of the cached book of the right kind before any write; tests assert the written URLs | Applies; Properties 3, 8 and 9 |
| Data integrity | An edit drops a member, a missing reference or an Apple property; a race overwrites a device edit | Text-level edits with byte-identity tests; `If-Match` on every write; a delta retry only for membership | Applies; Properties 6 and 7 |
| Privacy | Contact values in logs or check output | No new logging of values; the live checks print aggregates only | Applies |
| Performance | `contacts_get` gets slower | One change check (about 0.15 s) when warm; measured in the live check | Applies; accepted for correct `groups` |
| Accessibility | — | — | Not applicable: no user interface |
| Observability | — | — | Not applicable beyond the existing debug logs |
| Migration / rollout | Clients break on changed schemas | Only additive changes; digests pinned | Applies |
| Rollback | A bad list edit on the user's account | Live writes only on throwaway lists and contacts; reverting the commits removes the tools | Applies |

## Current Technology Evidence

Not applicable: no new or changed library behaviour is used.

- **List card format:** iCloud's format (`X-ADDRESSBOOKSERVER-KIND:group`, `X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:…`, `N` equal to `FN`, CRLF) was measured read-only on the user's account on 2026-10-11 and is recorded in [01_discovery.md](01_discovery.md).
- **Protocol:** conditional writes (`If-Match`, `If-None-Match`) are the ones `create_contact` and `update_contact` already use against iCloud.
- **Libraries:** `vobject` is only used, as now, to parse cards in `build_card`, and FastMCP parameter declarations follow the existing tools.

## Dependency Security Evidence

No new or upgraded dependency is selected or required. The feature uses the already-installed `fastmcp`, `requests`, `vobject` and `defusedxml`, plus the Python standard library (`datetime`, `uuid`, `re`).
