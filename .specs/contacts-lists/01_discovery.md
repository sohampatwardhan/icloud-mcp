# Discovery: Contacts Lists

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md) · [Design](03_design.md) · [Tasks](04_tasks.md) · [Execution](05_execution.md)
<!-- spec-nav:end -->

## Problem and Outcome

Contacts on iPhone and Mac lets people sort contacts into lists ("Family", "Work", "Book club"). The MCP server reads every list card from iCloud but drops it: [`src/icloud_mcp/contacts.py`](../../src/icloud_mcp/contacts.py) marks list cards as not listed, so `contacts_list`, `contacts_search` and `contacts_upcoming_dates` skip them, and no tool can read or change a list. An assistant cannot answer "who is in my Family list?" or act on "add Sam to Work".

Measured read-only on the user's live iCloud account:

| Measure | Value |
|---|---|
| Address books | 1 |
| People / lists | 1,798 / 15 |
| How a list is stored | a vCard with `X-ADDRESSBOOKSERVER-KIND:group`, its name in `FN` and `N`, and one `X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:<UID>` line per member |
| Members per list | 0 to 84; 280 references in total; one list is empty |
| References that point at no card | 92 of 280 (no match on UID, UID ignoring case, or file name); Contacts does not show them |
| People whose `UID` equals their URL file name | 7 of 1,798, so a contact id cannot be turned into a member reference without reading the card |
| People in any list / in two or more | 133 / 53 |
| Lists inside lists, duplicate list names | none |
| Other properties on list cards | `PRODID`, `REV` (14 of 15), `UID`, and on one card `X-APPLE-OL-MAPPING-INFO` |
| Card format | CRLF line endings; `N` equal to `FN`; 12 of 15 in the order `VERSION, PRODID, N, FN, X-ADDRESSBOOKSERVER-KIND, members, REV, UID`; `UID` in upper or lower case |

**Outcome:** an assistant can see the user's lists and who is in each, see which lists a person belongs to, add and remove people, and create, rename and delete lists. Every change shows up in Contacts on the user's devices as if made there, and nothing else on any card changes.

## Users and Current Workaround

- **Users:** AI assistants acting on a person's iCloud contacts (Muse, Claude and any other MCP client), and the person who later sees those lists on their devices.
- **Current workaround:** none. The user edits lists by hand on a device.

## Scope and Non-Goals

**In scope:**

- **Read lists:** every list with its id, name and number of members, in name order.
- **Read members:** the people in one list, paged and projected exactly like `contacts_list`, through an optional list filter on that tool.
- **Membership on a person:** contact reads (`contacts_get`, and the `fields` option of list, search and upcoming dates) say which lists the person is in. This is read-only on the person.
- **Change membership:** add and remove people in a list, given contact ids.
- **Manage lists:** create a list (optionally with members), rename it, and delete it. Deleting a list never deletes or changes the people in it.
- **Faithful storage:** writes keep every other property of the list card, including member references that point at no card, and are conditional on the card's ETag so a concurrent edit on a device is not overwritten.
- **The same change-checked cache** as list and search, kept current after the server's own writes.

**Non-goals:**

- Cleaning up member references that point at no card. Contacts ignores them; removing them is the user's call and is not needed for any read.
- Lists inside lists, and smart lists (Mac-only saved searches, which are not stored in iCloud).
- Lists in other accounts (Gmail, Exchange). The server only talks to iCloud.
- Editing a person's lists from `contacts_update`. Membership is changed with the list tools, so one call writes one card.
- My Card, contact photos and duplicate merging: separate specs.

## Constraints and Success Measures

**Constraints:**

- **Generic client:** nothing Muse-specific.
- **One card per write:** every write changes exactly one list card, or creates or deletes one. Person cards are never written by this feature.
- **No breaking change:** existing tools keep their parameters and output; additions are optional parameters and new keys.
- **Live writes only on throwaway data,** after an explicit yes from the user, with every card's ETag compared before and after to prove nothing else changed.

**Success measures:**

| Measure | Target |
|---|---|
| Reading all lists | one call, warm under 0.5 s, about 3 KB for 15 lists (each id is a URL of about 110 characters) |
| Member counts and member pages | match Contacts on the iPhone, which hides references to missing cards |
| A membership change, rename, create or delete | visible on the iPhone, with no other card's ETag changed and the list card's other properties byte-identical |
| Existing tools | unchanged behaviour; existing tests pass |

## Approaches Considered

| Approach | Benefits | Costs / risks | Reversibility | Decision |
|---|---|---|---|---|
| **A. List tools, a list filter on `contacts_list`, and a read-only list field on person reads** | Each write touches one list card; member pages reuse the existing paging, cursor and projection; a person's lists come from the cache with no extra download | Four new tools and one optional parameter | High: additive | **Chosen** (user choice) |
| B. Edit membership from the person (`contacts_update(lists=[...])`) | Matches the iPhone's per-person view | One call writes several list cards with no transaction: a failure halfway leaves some lists changed; mixes person and list edits in one tool | Medium | Rejected |
| C. Treat list cards as contacts in the existing tools | No new tools | Assistants would see "Family" as a person; member editing would need raw member references; easy to delete a list by mistake | Low | Rejected |
| D. Read-only lists | Smallest change, no write risk | Does not meet the goal of matching what Contacts can do | High | Rejected (user chose full editing) |

## Chosen Direction

Use approach A.

- **Ids:** a list is identified by the URL of its card, like a contact. Members are given and returned as contact ids; the server translates between a contact id and the card's `UID`, which is what the list stores.
- **Membership changes are deltas:** add and remove name the people to change, so the server never has to rewrite an 84-member list from the caller's copy, and references to missing cards stay untouched.
- **Retry on conflict:** a membership change that loses an ETag race is re-read and applied once more, since adding or removing the same people again gives the same result; a rename that conflicts is reported instead.
- **Counts follow the iPhone:** member counts and member pages include only references that point at a person in the address book.

## Architecture and Flow Outline

1. **Cache:** each cached card also keeps its `UID`, and each list card keeps its name and member references, so reading lists, members and a person's lists needs only the existing change check.
2. **Reads:** the lists tool and the list filter on `contacts_list` resolve member references to cached people; person reads add the lists the person is in.
3. **Writes:** create sends a new list card with `If-None-Match: *`; rename and membership changes read the list card, change only the name or member lines, and send it back with `If-Match`; delete sends a `DELETE` with `If-Match`. Each write marks the card for the cache's next change check, as person writes do today.

Changes stay inside [`src/icloud_mcp/contacts.py`](../../src/icloud_mcp/contacts.py), [`src/icloud_mcp/contacts_cache.py`](../../src/icloud_mcp/contacts_cache.py) if needed, and the tool declarations in [`src/icloud_mcp/server.py`](../../src/icloud_mcp/server.py), plus tests and the README.

## Failure and Verification Strategy

**Failures:**

- An unknown list id, a contact id that is not a person in the address book, or a card that is not a list raises a clear error before any write.
- Adding someone already in the list or removing someone not in it changes nothing and says so; an empty change sends no request.
- iCloud errors surface unchanged; a write that iCloud rejects leaves the cache marked for re-checking.

**Verification:**

1. **Unit tests** against real Apple list vCards: parsing, member resolution with missing references, the UID-to-id translation, byte-faithful edits, create, rename, delete, conflict retry, and validation before any request.
2. **Protocol tests** for the new tool schemas, the optional list filter, and the unchanged parts of existing tools.
3. **A read-only live check:** list names, member counts and member pages compared with an independent parse of the raw vCards; timings and sizes. Aggregates only.
4. **A live write check on throwaway data,** after the user's explicit yes: create a list, add and remove throwaway contacts, rename it and delete it, checking each step on the iPhone and comparing every card's ETag before and after.

## Open Decisions

| Decision | Owner | Resolved by |
|---|---|---|
| Tool, parameter and field names (working names use "group", because a `contacts_lists` tool beside `contacts_list` invites mix-ups) | Claude | design |
| Whether `contacts_get` reads lists through the cache or with its own request | Claude | design |

## Approval

Status: **Approved on 2026-10-11.** The user chose full list editing (members, create, rename, delete), list tools with a list filter on `contacts_list` and lists shown on person reads, and approved all gates for this feature on a new branch and pull request stacked on #3.
