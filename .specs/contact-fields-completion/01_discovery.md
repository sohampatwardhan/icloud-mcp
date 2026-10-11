# Discovery: Contact Fields Completion

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md) · [Design](03_design.md) · [Tasks](04_tasks.md)
<!-- spec-nav:end -->

## Problem and Outcome

The contacts tools in [`src/icloud_mcp/contacts.py`](../../src/icloud_mcp/contacts.py) and [`src/icloud_mcp/server.py`](../../src/icloud_mcp/server.py) now read and write labelled phones and emails, structured addresses, birthdays, significant dates, websites and social profiles. That work is open as pull request sohampatwardhan/icloud-mcp#1, and changes to those fields round-trip without disturbing anything else. Several fields that iOS and macOS Contacts offer in their "Add Field to Card" menu are still not readable or editable:

- **Name parts.** Prefix, middle name and suffix are not returned, and renaming a contact re-splits the name naively ("last word is the family name"). The rename silently drops the existing middle name, prefix and suffix.
- **Other names.** Phonetic first, middle and last name, nickname and maiden name.
- **Organization details.** Department, phonetic company name and the "Company" checkbox that makes a card display as a company. Updating the organization today overwrites the whole `ORG` value, which wipes the department.
- **Related names** (mother, spouse, assistant and so on) and **instant messages** (Skype, Snapchat and app-based services).
- **Apple-only fields**: pronouns, ringtone and text tone, lunar birthday and verification code. Their storage formats are undocumented.

**Outcome:** an MCP client can read, create, update and clear every person field above, plus the readable Apple-only fields. Updating one field never alters another, and the iPhone shows what was written.

## Users and Current Workaround

- **Users:** AI assistants (Muse, Claude and any MCP client) acting on a person's iCloud contacts, and the person who later sees those contacts on their devices.
- **Current workaround:** none inside the tools. An assistant cannot see a nickname or a related person. It also has no way to rename a contact without losing middle names or prefixes, short of editing the contact by hand on a device.

## Scope and Non-Goals

**In scope (this spec):**

- Structured name parts (prefix, first, middle, last, suffix) on read and write, with renames that no longer lose parts.
- Phonetic first, middle and last name; nickname; maiden name.
- Department, phonetic organization and the company-card flag; updating the organization keeps the department.
- Related names with Apple labels (mother, father, spouse, partner, child, sibling, assistant, manager, friend and custom text).
- Instant messages with service, username and label, keeping Apple's app identifiers on unchanged entries.
- A guarantee, backed by tests, that pronouns, ringtone and text tone, lunar birthday, verification code and every other unrecognised property survive edits unchanged.
- Learning the real storage format of lunar birthday, ringtone and text tone, and verification code from a throwaway card. Each one that turns out readable is exposed read-only.

**Non-goals (separate specs or excluded):**

- Contacts lists (groups), contact photos, and `contacts_list` size limits and search speed. Each ships as its own spec.
- Reading or writing pronouns. All 36 stored values are opaque byte blobs with no recognisable structure.
- Writing ringtone, lunar birthday or verification code. This spec makes them readable at most.
- A raw vCard read or write tool.

## Constraints and Success Measures

**Constraints:**

- **Test data.** Real contacts are never updated or deleted during testing. Writes go only to throwaway test contacts, which the user deletes.
- **Breaking changes.** Output shape changes are acceptable, because this fork has agreed to make breaking changes cleanly. Keys stay identical between reads and writes.
- **Round-trips.** Every existing round-trip guarantee from pull request sohampatwardhan/icloud-mcp#1 stays intact.
- **Verification sources.** Results are checked against a read-only snapshot of the live iCloud address book (1,803 people) and on the user's iPhone through iPhone Mirroring. The test Mac's Contacts app is currently out of sync with iCloud, so it cannot serve as the reference.

**Success measures:**

| Measure | Target |
|---|---|
| Read-then-write-back of every field across the live snapshot | 0 contacts changed |
| Field-level edits replayed over the snapshot | only the edited property lines change |
| Rename with structured parts or a display name | middle name, prefix and suffix preserved unless explicitly changed |
| Throwaway contact created and updated through the MCP server | every new field displays as intended on the iPhone, before and after the update |
| Apple-only fields on cards that carry them (36 pronoun, 1 ringtone in the snapshot) | byte-identical after any edit |

Field usage in the live snapshot, for prioritisation:

| Field | Cards |
|---|---:|
| pronouns | 36 |
| company flag | 20 |
| nickname | 13 |
| instant messages | 8 |
| middle name | 5 |
| ringtone | 1 |
| related names | 1 |
| prefix | 1 |
| department, phonetic names, maiden name, lunar birthday, verification code | 0 |

## Approaches Considered

| Approach | Benefits | Costs / risks | Reversibility | Decision |
|---|---|---|---|---|
| **A. Flat typed fields with identical read and write keys** (`first_name`, `middle_name`, `nickname`, `department`, `is_company`, `related_names: [{label, name}]`, `instant_messages: [{label, service, username}]`) | Matches every field already shipped. One obvious parameter per field for assistants. Reuses the existing preserve-unchanged and label machinery. | `contacts_create` and `contacts_update` grow to about 25 parameters. | High: parameters can be regrouped later. | **Chosen** |
| B. Nested objects (`name: {prefix, given, …}`, `organization: {name, department, …}`) | Tidier grouping. | Breaks the `name` and `organization` strings that already shipped. Nested schemas are harder for assistants to fill in correctly. | Medium | Rejected |
| C. Raw vCard property access (a tool that reads and edits vCard lines) | One tool covers every present and future field. | Assistants must know vCard and Apple encodings. A malformed write can corrupt a card on every device. Moves correctness off the server. | Low once clients depend on it | Rejected as the primary approach |

## Chosen Direction

Use approach A. Each new field gets a typed key on contact reads and the same key as an optional `contacts_create`/`contacts_update` parameter. Omitting a key leaves the field alone, and an empty value clears it. List fields (related names, instant messages) replace the whole list. As with existing fields, entries sent back unchanged are kept verbatim, including Apple parameters such as app identifiers and labels.

The display `name` stays. When only `name` is given, it replaces the display name and maps onto first and last name. Middle name, prefix and suffix are kept unless the new name contradicts them or the structured parameters change them. Structured name parameters always win over the split.

Apple-only fields follow an evidence-first rule. A throwaway card carries each field, set through the iPhone's own interface by iPhone Mirroring or by the user. Its vCard is then read over CardDAV, and only formats that are clearly readable are exposed, read-only. Pronouns remain preserved but unexposed.

## Architecture and Flow Outline

All changes stay inside the existing two modules; no new tools or modules are introduced.

- **Read path:** CardDAV GET or REPORT → vCard parse → contact dictionary, which gains the new keys.
- **Write path:** `contacts_create` / `contacts_update` parameters → the field-apply step on the parsed vCard → orphan-label cleanup that spares pre-existing leftovers → PUT with `If-Match`.
- **Preservation:** properties the server does not model are never touched, so Apple-only fields and future Apple properties keep surviving edits.

## Failure and Verification Strategy

**Failure handling:**

- Invalid input (an unknown service with no username, an empty related name, an undecodable Apple value) is rejected with an actionable error before anything is written.
- Unreadable Apple-only values are omitted from reads rather than guessed.
- The `If-Match` ETag on updates prevents overwriting a concurrent edit made on a device.

**Verification, in order:**

1. Unit tests for each field's read, write, clear and unchanged-echo behaviour.
2. In-memory replays over the live iCloud snapshot. A read-then-write-back must change 0 cards, and field edits must touch only their own lines.
3. A throwaway contact created and then updated through the real MCP server over stdio, with the contacts tools only and no delete calls. It is checked on the iPhone after creation and again after the update; the post-update check was missed in the previous round.
4. A second throwaway card, given lunar birthday, ringtone, text tone and verification code on the iPhone, read over CardDAV to settle those formats.

## Open Decisions

| Decision | Owner | Resolved by |
|---|---|---|
| Exact vCard encodings of lunar birthday, ringtone and text tone, and verification code | Claude, with iPhone access | the second throwaway card during execution; requirements for these fields are conditional on the formats proving readable |
| Exact label vocabulary for related names and instant messages | Claude | Apple's built-in label tokens observed on the throwaway card; custom text is always allowed |
| Who deletes the throwaway cards | the user | the cards are left in place and the user is told to delete them |

## Approval

Status: **Approved on 2026-10-11**. The user approved this direction and pre-approved all spec gates for this feature.
