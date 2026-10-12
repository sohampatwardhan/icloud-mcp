# Design: Contact Fields Completion

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md) · [Design](03_design.md) · [Tasks](04_tasks.md) · [Execution](05_execution.md)
<!-- spec-nav:end -->

## Overview

Every new field is a typed key on the contact object returned by reads and an optional parameter of `contacts_create` and `contacts_update`, following approach A from discovery. Rejected alternatives: nested objects, and raw vCard access. The field logic lives in [`src/icloud_mcp/contacts.py`](../../src/icloud_mcp/contacts.py), beside the existing parsers and setters; [`src/icloud_mcp/server.py`](../../src/icloud_mcp/server.py) only declares parameters and passes them through.

Each field reuses one of three existing write patterns:

- **Single-value text fields** (name parts, phonetic names, nickname, maiden name, department, phonetic organization). An omitted field is left alone, an empty string clears it, and any other text sets it.
- **Labelled lists** (related names, instant messages). The list is replaced as a whole. Entries matching a stored entry are re-added verbatim. New or changed entries are written with Contacts' own label encoding, and labels orphaned by the update are cleaned up.
- **Untouched properties.** Everything the server does not model, including pronouns, ringtones and photos, is never removed or rewritten.

Apple-only reads (birthday in another calendar, ringtone and text tone, verification code) are gated on evidence. Each one is added only if a throwaway card proves its encoding readable.

> [!IMPORTANT]
> Approval gate: approve this design before work begins on [04_tasks.md](04_tasks.md).

## Architecture

No new modules or tools. The change touches five places:

| Component | Location | Change |
|---|---|---|
| Contact_Reader | `_vcard_to_dict` and new parsers in [`src/icloud_mcp/contacts.py`](../../src/icloud_mcp/contacts.py) | Adds the new keys to every contact read |
| Contact_Writer | `_apply_fields`, the new `_apply_name` and new setters in the same file | Applies new parameters; keeps existing round-trip guarantees |
| Contact_Search | `search_contacts` in the same file | Adds nickname, maiden name, related names and instant-message usernames to the matched text |
| Contact_Tools | `contacts_create` / `contacts_update` in [`src/icloud_mcp/server.py`](../../src/icloud_mcp/server.py) | New optional parameters; `name` becomes optional on create; HTTP 412 becomes a conflict error |
| Format_Investigation | a verification procedure, not shipped code | Reads a throwaway card's vCard and records Apple-only encodings in this design's appendix |

## Components and Interfaces

### Contact_Reader

`_vcard_to_dict(vcard, url) -> dict` gains these keys. Every key is always present, as an empty string or empty list when the contact has no value:

| Key | Type | Source |
|---|---|---|
| `prefix`, `first_name`, `middle_name`, `last_name`, `suffix` | `str` | `N` components, in vCard order family;given;additional;prefix;suffix. Multi-value components are joined with a space. |
| `phonetic_first_name`, `phonetic_middle_name`, `phonetic_last_name` | `str` | `X-PHONETIC-FIRST-NAME`, `X-PHONETIC-MIDDLE-NAME`, `X-PHONETIC-LAST-NAME` |
| `pronunciation_first_name`, `pronunciation_last_name` | `str` | `X-PRONUNCIATION-FIRST-NAME`, `X-PRONUNCIATION-LAST-NAME` (iOS 27; separate from the phonetic names) |
| `nickname` | `str` | first `NICKNAME` value |
| `maiden_name` | `str` | `X-MAIDENNAME` |
| `department` | `str` | second component of `ORG` |
| `phonetic_organization` | `str` | `X-PHONETIC-ORG` |
| `is_company` | `bool` | `X-ABShowAs` equals `COMPANY`, case-insensitive |
| `related_names` | `list[{"label", "name"}]` | `X-ABRELATEDNAMES` plus its group's `X-ABLabel` |
| `instant_messages` | `list[{"label", "service", "username"}]` | `IMPP`: `service` from `X-SERVICE-TYPE`; `username` is the value after its URI scheme, percent-decoded; `label` from `TYPE` or `X-ABLabel` |
| `alternate_birthday` (`{calendar, month, day, year}`), `ringtone`, `text_tone` | as recorded in Appendix A; `verification_code` was never observed and is not exposed | present only when the investigation confirms the format is readable and the card has a decodable value |

New functions:

- `_parse_name_parts(vcard) -> dict[str, str]`
- `_parse_related_names(vcard) -> list[dict[str, str]]`
- `_parse_instant_messages(vcard) -> list[dict[str, str]]`
- `_parse_apple_extras(vcard) -> dict[str, Any]`. Returns only the confirmed, decodable fields. A value that fails to decode is skipped.

Pronouns (`X-ADDRESSING-GRAMMAR`) are never read.

Reading details:
- **Value types.** vobject returns `N` and `ORG` components as `str` or `list[str]`. Multi-value parts are read joined with a space.
- **Instant messages.** The `IMPP` value is split on its first colon only, and the username is percent-decoded with `urllib.parse.unquote`. When `X-SERVICE-TYPE` is absent, `service` is derived from the URI scheme using the reverse of the service table (`skype:` → `Skype`), and is empty only for `x-apple:` values. Labels come from `_entry_label(vcard, prop, _HOME_WORK_OTHER)`, so `TYPE=HOME` reads as `home`.
- **Company cards.** `_display_name` is unchanged. On a company card with a non-empty `FN`, the read `name` stays that `FN` even after rule 3 recomputes it from person parts, while the Contacts app shows the organization. The snapshot has 10 such cards.

### Contact_Writer

The existing positional parameters of `_apply_fields(vcard, phones, emails, addresses, organization, title, notes, birthday, urls, social_profiles, dates)` stay. New keyword-only parameters are added, all defaulting to `None`, meaning "leave unchanged":

```python
def _apply_fields(vcard, phones=None, emails=None, addresses=None, organization=None, title=None,
                  notes=None, birthday=None, urls=None, social_profiles=None, dates=None, *,
                  name=None, prefix=None, first_name=None, middle_name=None, last_name=None, suffix=None,
                  phonetic_first_name=None, phonetic_middle_name=None, phonetic_last_name=None,
                  nickname=None, maiden_name=None, department=None, phonetic_organization=None,
                  is_company=None, related_names=None, instant_messages=None) -> None
```

Rename handling moves from `update_contact` and `create_contact` into `_apply_name(vcard, name, parts: dict[str, str | None]) -> None`. Before any rule runs, values equal to what a read returns are treated as omitted: a `name` equal to the current `_display_name`, and a part equal to its `_parse_name_parts` value. A read-then-write-back therefore leaves `N` and `FN` untouched. The rules are:

1. **Structured parts given.** Each provided part is set on the existing `N` value in place, as plain text, and an empty string clears it. Unprovided parts, including multi-value ones, keep their stored value untouched. `N` is created only when it is absent and a part is being written; create always adds one, which may be empty.
2. **Only `name` given.** The text is tokenised on whitespace, and tokens that match the stored prefix, middle name or suffix are removed. A leading prefix match, a trailing suffix match and one contiguous middle-name match are each removed at most once. The remaining tokens become first name (all but the last) and last name (the last); a single token becomes the first name. Prefix, middle name and suffix are kept. `FN` is set to `name`.
3. **Parts changed and `name` not given.** `FN` is recomputed from the non-empty parts in the order prefix, first, middle, last, suffix.
4. **Both given.** `FN` is set to `name`, and given parts follow rule 1. A first or last name that is not given explicitly is split from `name` as in rule 2, so `name="Jane Doe", middle_name="Q"` still stores Jane and Doe; the wave review found that the original rule left them empty.

New helpers:

- `_set_text_property(vcard, prop_name, value: str | None) -> None` handles the single-value text fields and `TITLE`. Values are trimmed, so whitespace-only clears a field, and a value equal to the stored one is a no-op.
- `_set_notes(vcard, notes)` treats notes equal to the read value as a no-op. That read value is the stored text after rich-text cleanup, so an echo keeps the stored HTML. `""` removes the notes.
- `_set_org(vcard, organization, department) -> None` replaces only `ORG` components 0 and 1. Any component from index 2 on is kept, trailing empty components are dropped, and `ORG` is removed when every component is empty. The value is always assigned as a list, because a plain string is split into single characters on serialisation.
- `_set_company_flag(vcard, is_company: bool | None) -> None` adds `X-ABShowAs:COMPANY`, or removes it only when its value is `COMPANY`. A value equal to the current read is a no-op.
- `_set_related_names(vcard, items)` and `_set_instant_messages(vcard, items)` are dedicated setters; they do not reuse `_normalized`.
  - **Matching.** Related names match a stored entry on exact stripped name. Instant messages match on (service, decoded username).
  - **Kept verbatim** (the stored property object re-added) when the label is omitted or equal.
  - **Changed.** The label is rewritten through `_set_typed_label`, passing `pref` when the stored entry had it.
  - **Validation and defaults** apply only to entries that match no stored entry.
  - **Group numbers.** Groups of removed properties stay reserved for this update, so new groups never reuse them (`_next_group(vcard, reserved)`).

`_validate_fields(vcard, **fields)` runs as the first step of `_apply_fields` and computes the resulting organization without mutating anything. In `create_contact`, the identity check runs before `require_auth` and before any CardDAV discovery request. A `ValueError` means the vCard is not modified and nothing is sent:

| Condition | Error |
|---|---|
| Related name with empty `name` | `related name entry needs a name: <entry>` |
| Instant message with empty `service` or `username` | `instant message entry needs a service and a username: <entry>` |
| `is_company=true` with no resulting organization | `is_company needs an organization` |
| Update whose GET returned no ETag | `iCloud returned no ETag for this contact; refusing an unconditional overwrite` |
| Create with no `name`, no part and no `organization` | `contact needs a name, a name part or an organization` |

`create_contact(name: str | None = None, …, **new_fields)` and `update_contact(contact_id, …, **new_fields)` pass the new keywords straight to `_apply_fields`. The tool functions pass the new fields by keyword.

### Contact_Search

`search_contacts` adds `nickname`, `maiden_name`, every `related_names[].name` and every `instant_messages[].username` to the text it matches against, with the existing case-insensitive substring match.

### Contact_Tools

`contacts_create` and `contacts_update` declare each new key as an optional parameter, using `Annotated[..., Field(description=...)]` like the existing parameters. `name` becomes `str | None = None` on `contacts_create`. No parameter exists for pronouns, ringtone, text tone, birthday in another calendar or verification code. FastMCP rejects unknown arguments, so such a call fails validation.

`_run` maps `requests.HTTPError` with status 412 by operation. For `update_contact` it raises `ToolError("contact changed on another device since it was read; fetch it again with contacts_get and retry")`. For `create_contact` it raises `ToolError("a contact with this id already exists")`. The new parameters are declared once as `Annotated` aliases shared by both tools.

### Format_Investigation

1. A second throwaway card named `ZZ MCP Fields Test` is created on the iPhone through iPhone Mirroring, which the user has allowed for testing. It gets a birthday in another calendar, a non-default ringtone and text tone, a verification code, and pronunciation names.
2. Its vCard is fetched over CardDAV with a GET, and each encoding is recorded in Appendix A, together with what the iPhone shows for it.
3. A field counts as readable only when the shown value can be derived from the stored value without device-only data.
4. The card is left for the user to delete.

## Data Models

### New labels

Related-name labels are written as Apple built-in tokens when the label case-insensitively matches one of: `mother`, `father`, `parent`, `brother`, `sister`, `child`, `friend`, `spouse`, `partner`, `assistant`, `manager` or `other`. Any other label is written as custom text. Reads return built-in labels in lower case and custom labels verbatim, matching the existing `_decode_label` behaviour. A new related name without a label gets `other`.

Instant-message services are written with these display names and URI schemes:

| Service | `X-SERVICE-TYPE` | URI scheme |
|---|---|---|
| aim, icq | `AIM`, `ICQ` | `aim:` |
| jabber, googletalk, facebook | `Jabber`, `GoogleTalk`, `Facebook` | `xmpp:` |
| msn | `MSN` | `msnim:` |
| yahoo | `Yahoo` | `ymsgr:` |
| skype | `Skype` | `skype:` |
| any other service | the given text | `x-apple:` |

Instant-message labels map `home`, `work` and `other` onto `TYPE`. Any other label is written as an `X-ABLabel`. A new entry without a label gets `other`.

### Field lifecycle

| Request value | Effect |
|---|---|
| key omitted / `None` | stored value unchanged |
| `""` (text) or `[]` (list) | stored value removed |
| text or non-empty list | stored value replaced; list entries matching a stored entry kept verbatim |
| `is_company` `true` / `false` | `X-ABShowAs:COMPANY` present / absent; a value equal to the current read changes nothing |
| `name` or a name part equal to the current read | nothing changes |

## Sequence / Flows

`contacts_update` keeps its existing flow:

1. GET the vCard with its ETag.
2. Validate the new inputs.
3. Run `_apply_fields`, which applies the name, single-value fields, organization, company flag and lists.
4. Remove labels orphaned by this update, sparing leftovers that were already orphaned.
5. Run `_serialize`.
6. PUT with `If-Match`, refusing the update when the GET returned no ETag.
7. A 412 response becomes the conflict error.

`contacts_create` runs the same field steps on a new vCard and PUTs with `If-None-Match: *`.

## Correctness Properties

### Property 1: Name parts read and partial write
Every read returns all five name parts. A request sets exactly the parts it provides, clears parts given as empty strings, and leaves every other part unchanged.
**Validates: Requirements 1.1, 1.2, 1.6, 8.2**

### Property 2: Display-name rename keeps parts
For any stored contact and any new display name, an update giving only `name` leaves prefix, middle name and suffix unchanged. A `name` or part equal to the current read leaves `N` and `FN` byte-identical. The stored first and last name never contain a token removed as a matching prefix, middle name or suffix.
**Validates: Requirements 1.3, 1.4**

### Property 3: Display name follows parts
After an update that changes a name part without `name`, the display name equals the non-empty parts joined in the order prefix, first, middle, last, suffix.
**Validates: Requirements 1.5**

### Property 4: Created contacts are identifiable
A create request with no display name, name part or organization fails before any request is sent to iCloud.
**Validates: Requirements 1.7**

### Property 5: Single-value text fields
For each of the phonetic names, nickname, maiden name, department and phonetic organization, the following hold:
- reads return the stored text, or empty when the contact has none;
- text sets the field and an empty string removes it;
- omitting the field leaves it unchanged;
- the read key equals the parameter name.
**Validates: Requirements 2.1, 2.2, 2.3, 3.1, 3.3, 3.4, 8.1, 8.2**

### Property 6: Organization keeps department
Updating `organization` alone leaves the stored department unchanged.
**Validates: Requirements 3.2**

### Property 7: Company flag
`is_company` reads true exactly when the card is marked to display as a company. Setting it true or false adds or removes that mark, and setting it true without a resulting organization fails before any write.
**Validates: Requirements 3.1, 3.5, 3.6, 3.7**

### Property 8: Labelled lists
For related names and instant messages, the following hold:
- reads return the documented entry shapes;
- a provided list replaces the stored list;
- an entry sent back unchanged is re-stored byte-identically, including Apple app identifiers;
- a new related name without a label is labelled `other`;
- an entry missing its required text fails before any write.
**Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 5.1, 5.2, 5.3, 5.4**

### Property 9: Unmodelled properties survive
For every contact in the live snapshot, two replays hold:
- writing back every field as read changes no stored line;
- editing a single field changes only that field's lines plus its own group labels.

Every property the tools do not model stays byte-identical in both.
**Validates: Requirements 6.1, 6.2, 6.3**

### Property 10: Apple-only fields stay out of the write surface
Reads never include pronoun data. The create and update tool schemas contain no parameter for pronouns, ringtone, text tone, birthday in another calendar or verification code, and calls naming one are rejected.
**Validates: Requirements 6.4, 6.5**

### Property 11: Evidence-gated Apple-only reads
Each of `alternate_birthday` and `ringtone`/`text_tone` appears in reads only when two things hold:
- Appendix A records its encoding as readable;
- the stored value decodes.

A value that does not decode is omitted.
**Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5**

### Property 12: Search covers the new name fields
A query that is a case-insensitive substring of a contact's nickname, maiden name, a related name or an instant-message username returns that contact.
**Validates: Requirements 8.3**

### Property 13: What the iPhone shows matches what was written
For a throwaway contact created and then updated through the MCP server over stdio, the iPhone shows each new writable field with its intended value and label, both after creation and after the update.
**Validates: Requirements 2.4, 4.6, 5.5, 8.4, 8.5**

### Property 14: Conflicting edits are not overwritten
When the stored ETag changed since the read, the update returns the conflict error and the stored card is unchanged. An update whose GET returned no ETag is refused, never sent unconditionally.
**Validates: Requirements 8.6**

## Error Handling / Edge Cases

- **Validation errors** (R1.7, R3.7, R4.5, R5.4) are raised as `ValueError` before mutation. `_run` already turns them into MCP tool errors with the message text.
- **Conflicts** (R8.6): HTTP 412 is mapped to an explicit conflict error that tells the client to re-read.
- **Name tokenising edge cases.**
  - A display name that is only the stored prefix, or only the stored suffix, leaves first and last name empty and keeps `FN` as given.
  - Multi-word middle names are matched as one contiguous token sequence.
  - Matching is case-sensitive, so "Jr" and "jr." are treated as different tokens.
- **Damaged stored lines.** An `X-ABLabel` value already damaged in iCloud, such as a label line merged with another property, is returned verbatim as a custom label, and an unchanged echo re-stores it as is.
- **Company card without an organization.** R3.7 rejects `is_company=true` for any request whose result has no organization, including a write-back of such a card. The live snapshot has no such card (0 of 20 company cards), so R6.2 still holds on real data.
- **Unescaped commas.** vobject keeps only the text before an unescaped comma in a single-value text property, such as `NICKNAME:Jo,Johnny`. Apple escapes commas, so the snapshot replay checks how often this happens before deciding whether it matters.
- **Multiple `NICKNAME` values.** Only the first is read. Writing a nickname replaces all of them, and an empty string removes them all.
- **Apple-only decode failures** (R7.5): the field is omitted. This never fails the read.

## Testing Strategy

| Level | What | Criteria |
|---|---|---|
| Unit ([`tests/test_contacts.py`](../../tests/test_contacts.py)) | Each property 1–8 and 12 on hand-written vCards. A schema test lists the tool parameters and asserts the Apple-only names are absent and rejected. A 412 test uses a stubbed session. | R1.x, R2.1–2.3, R3.x, R4.1–4.5, R5.1–5.4, R6.4, R6.5, R8.1–8.3, R8.6 |
| Snapshot replay (in memory, read-only snapshot) | Write-back of every field must leave 0 cards changed. Edits of each new field must touch only their own lines. Pronoun (36) and ringtone (1) lines must stay byte-identical. | R6.1–6.3 |
| Format investigation | `ZZ MCP Fields Test` vCard read; encodings recorded in Appendix A; decoders unit-tested on the recorded values | R7.1–7.5 |
| Device (iPhone Mirroring) | `ZZ MCP Test` created through the MCP server over stdio with every new writable field, screenshot-checked; then updated and checked again | R2.4, R4.6, R5.5, R8.4, R8.5 |

The existing suite, 102 tests at the start of this feature, must keep passing, and `ruff check src tests` must stay clean.

## Cross-Cutting Risk Gates

| Gate | Applies | Failure mode | Verification and decision |
|---|---|---|---|
| Data integrity | yes | An edit corrupts or drops stored fields on real contacts | Snapshot replay must show 0 unintended changes before merge. Real contacts are never written in tests. |
| Security / authorization | yes, unchanged | New parameters reach hosts other than iCloud | Existing `ensure_icloud_url` and the iCloud-only host rule are untouched. No new network calls. |
| Privacy | yes | Pronoun data or other personal values leak into reads or logs | Pronouns are never read. No new logging of field values. Investigation output records encodings with throwaway values only. |
| Compatibility / migration | yes | Clients relying on `name` being required on create | `name` stays accepted. This fork accepts breaking changes cleanly, and the change is noted in the pull request. |
| Performance | low | Larger contact objects | About 15 new keys per contact. List size and search speed are a separate spec. |
| Observability | not applicable | No new runtime paths beyond existing tool calls | — |
| Accessibility | not applicable | No user interface | — |
| Rollout / rollback | yes | A bad release corrupts cards on devices | Shipped through pull request sohampatwardhan/icloud-mcp#1's branch. Rollback reverts the commits; existing cards are unaffected because unmodelled properties are never rewritten. |

## Current Technology Evidence

FastMCP's argument validation was checked in Context7 (`/websites/gofastmcp`) and confirmed against the installed fastmcp 4.1.0. Decision: declare create and update parameters only for writable fields, and let FastMCP's own argument validation turn away any Apple-only field name.

| Technology | Context7 identity/source | Exact selected version | Current-doc question | Decision |
|---|---|---|---|---|
| FastMCP | `/websites/gofastmcp` | 4.1.0, the installed version, behaviour confirmed by listing the live tool schema | Are unknown tool arguments rejected, and how are optional and `list[dict]` parameters validated? | Unknown arguments are rejected (`additionalProperties: false`), so Apple-only write parameters are simply not declared. The default lenient validation accepts `"true"` for `is_company`. |

## Dependency Security Evidence

No new or upgraded dependency is selected or required by this feature. It relies only on the already-installed `vobject`, `requests` and `fastmcp`, and changes no manifest or lock file.

## Appendix A: Apple-only field encodings

The evidence for this appendix is recorded in [execution/format-investigation.md](execution/format-investigation.md).

| Field | Stored property | iPhone shows | Decision |
|---|---|---|---|
| Ringtone | `X-ACTIVITY-ALERT:type=call\,snd=system:<name>` | "Sound: <name>" | readable when `snd` uses `system:`; other `snd` forms, such as purchased-tone identifiers, are omitted |
| Text tone | `X-ACTIVITY-ALERT:type=text\,snd=texttone:<name>` | "<name>" | readable when `snd` uses `texttone:` or `system:`; other forms are omitted |
| Birthday in another calendar | `X-ALTBDAY;CALSCALE=<chinese\|hebrew\|…>:--MMDD` (or `YYYYMMDD`) | the date in that calendar | readable as calendar, month number and day, plus the year when stored; any other value shape is omitted. It is independent of `BDAY`: the user set both on the test card. |
| Verification code | none: the field could not be saved on the device | — | not observed, so not exposed (R7.4's condition is not met) |
| Pronunciation first / last name | `X-PRONUNCIATION-FIRST-NAME`, `X-PRONUNCIATION-LAST-NAME` | the text | readable plain text, added to scope by the user (R2.1) |
