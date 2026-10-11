# Design: Contact Fields Completion

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md) · [Design](03_design.md) · [Tasks](04_tasks.md)
<!-- spec-nav:end -->

## Overview

Every new field is a typed key on the contact object returned by reads and an optional parameter of `contacts_create` and `contacts_update`, following approach A from discovery. Rejected alternatives: nested objects, and raw vCard access. The field logic lives in [`src/icloud_mcp/contacts.py`](../../src/icloud_mcp/contacts.py), beside the existing parsers and setters; [`src/icloud_mcp/server.py`](../../src/icloud_mcp/server.py) only declares parameters and passes them through.

Each field reuses one of three existing write patterns:

- **Single-value text fields** (name parts, phonetic names, nickname, maiden name, department, phonetic organization). An omitted field is left alone, an empty string clears it, and any other text sets it.
- **Labelled lists** (related names, instant messages). The list is replaced as a whole. Entries matching a stored entry are re-added verbatim. New or changed entries are written with Contacts' own label encoding, and labels orphaned by the update are cleaned up.
- **Untouched properties.** Everything the server does not model, including pronouns, ringtones and photos, is never removed or rewritten.

Apple-only reads (lunar birthday, ringtone and text tone, verification code) are gated on evidence. Each one is added only if a throwaway card proves its encoding readable.

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
| `nickname` | `str` | first `NICKNAME` value |
| `maiden_name` | `str` | `X-MAIDENNAME` |
| `department` | `str` | second component of `ORG` |
| `phonetic_organization` | `str` | `X-PHONETIC-ORG` |
| `is_company` | `bool` | `X-ABShowAs` equals `COMPANY`, case-insensitive |
| `related_names` | `list[{"label", "name"}]` | `X-ABRELATEDNAMES` plus its group's `X-ABLabel` |
| `instant_messages` | `list[{"label", "service", "username"}]` | `IMPP`: `service` from `X-SERVICE-TYPE`; `username` is the value after its URI scheme, percent-decoded; `label` from `TYPE` or `X-ABLabel` |
| `lunar_birthday`, `ringtone`, `text_tone`, `verification_code` | as recorded in Appendix A | present only when the investigation confirms the format is readable and the card has a decodable value |

New functions:

- `_parse_name_parts(vcard) -> dict[str, str]`
- `_parse_related_names(vcard) -> list[dict[str, str]]`
- `_parse_instant_messages(vcard) -> list[dict[str, str]]`
- `_parse_apple_extras(vcard) -> dict[str, Any]`. Returns only the confirmed, decodable fields. A value that fails to decode is skipped.

Pronouns (`X-ADDRESSING-GRAMMAR`) are never read.

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

Rename handling moves from `update_contact` and `create_contact` into `_apply_name(vcard, name, parts: dict[str, str | None]) -> None`. The rules are:

1. **Structured parts given.** Each provided part is set, and an empty string clears it. Unprovided parts keep their stored value.
2. **Only `name` given.** The text is tokenised on whitespace, and tokens that match the stored prefix, middle name or suffix are removed. A leading prefix match, a trailing suffix match and one contiguous middle-name match are each removed at most once. The remaining tokens become first name (all but the last) and last name (the last); a single token becomes the first name. Prefix, middle name and suffix are kept. `FN` is set to `name`.
3. **Parts changed and `name` not given.** `FN` is recomputed from the non-empty parts in the order prefix, first, middle, last, suffix.
4. **Both given.** `FN` is set to `name`, and the parts follow rule 1.

New helpers:

- `_set_text_property(vcard, prop_name, value: str | None) -> None` handles the single-value text fields.
- `_set_org(vcard, organization, department) -> None` keeps the stored department when only `organization` is given, and the other way round.
- `_set_company_flag(vcard, is_company: bool | None) -> None` adds or removes `X-ABShowAs:COMPANY`.
- `_set_related_names(vcard, items)` and `_set_instant_messages(vcard, items)` reuse `_set_typed_label` and the verbatim-match pattern of `_set_labelled`.

Validation runs before any mutation. A `ValueError` means the vCard is not modified and nothing is sent:

| Condition | Error |
|---|---|
| Related name with empty `name` | `related name entry needs a name: <entry>` |
| Instant message with empty `service` or `username` | `instant message entry needs a service and a username: <entry>` |
| `is_company=true` with no resulting organization | `is_company needs an organization` |
| Create with no `name`, no part and no `organization` | `contact needs a name, a name part or an organization` |

`create_contact(name: str | None = None, …, **new_fields)` and `update_contact(contact_id, …, **new_fields)` pass the new keywords straight to `_apply_fields`.

### Contact_Search

`search_contacts` adds `nickname`, `maiden_name`, every `related_names[].name` and every `instant_messages[].username` to the text it matches against, with the existing case-insensitive substring match.

### Contact_Tools

`contacts_create` and `contacts_update` declare each new key as an optional parameter, using `Annotated[..., Field(description=...)]` like the existing parameters. `name` becomes `str | None = None` on `contacts_create`. No parameter exists for pronouns, ringtone, text tone, lunar birthday or verification code. FastMCP rejects unknown arguments, so such a call fails validation.

`_run` maps `requests.HTTPError` with status 412 to `ToolError("contact changed on another device since it was read; fetch it again with contacts_get and retry")`.

### Format_Investigation

1. A second throwaway card named `ZZ MCP Fields Test` is created on the iPhone through iPhone Mirroring. It gets a lunar birthday, a non-default ringtone and text tone, and a verification code.
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
| `is_company` `true` / `false` | `X-ABShowAs:COMPANY` present / absent |

## Sequence / Flows

`contacts_update` keeps its existing flow:

1. GET the vCard with its ETag.
2. Validate the new inputs.
3. Run `_apply_fields`, which applies the name, single-value fields, organization, company flag and lists.
4. Remove labels orphaned by this update, sparing leftovers that were already orphaned.
5. Run `_serialize`.
6. PUT with `If-Match`.
7. A 412 response becomes the conflict error.

`contacts_create` runs the same field steps on a new vCard and PUTs with `If-None-Match: *`.

## Correctness Properties

### Property 1: Name parts read and partial write
Every read returns all five name parts. A request sets exactly the parts it provides, clears parts given as empty strings, and leaves every other part unchanged.
**Validates: Requirements 1.1, 1.2, 1.6, 8.2**

### Property 2: Display-name rename keeps parts
For any stored contact and any new display name, an update giving only `name` leaves prefix, middle name and suffix unchanged. The stored first and last name never contain a token removed as a matching prefix, middle name or suffix.
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
`is_company` reads true exactly when the card is marked to display as a company. Setting it true or false adds or removes that mark, and setting it true without an organization fails before any write.
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
Reads never include pronoun data. The create and update tool schemas contain no parameter for pronouns, ringtone, text tone, lunar birthday or verification code, and calls naming one are rejected.
**Validates: Requirements 6.4, 6.5**

### Property 11: Evidence-gated Apple-only reads
Each of `lunar_birthday`, `ringtone`/`text_tone` and `verification_code` appears in reads only when two things hold:
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
When the stored ETag changed since the read, the update returns the conflict error and the stored card is unchanged.
**Validates: Requirements 8.6**

## Error Handling / Edge Cases

- **Validation errors** (R1.7, R3.7, R4.5, R5.4) are raised as `ValueError` before mutation. `_run` already turns them into MCP tool errors with the message text.
- **Conflicts** (R8.6): HTTP 412 is mapped to an explicit conflict error that tells the client to re-read.
- **Name tokenising edge cases.**
  - A display name that is only the stored prefix, or only the stored suffix, leaves first and last name empty and keeps `FN` as given.
  - Multi-word middle names are matched as one contiguous token sequence.
  - Matching is case-sensitive, so "Jr" and "jr." are treated as different tokens.
- **Damaged stored lines.** An `X-ABLabel` value already damaged in iCloud, such as a label line merged with another property, is returned verbatim as a custom label, and an unchanged echo re-stores it as is.
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

This appendix starts empty. It is filled in during execution with each field's observed vCard encoding, what the iPhone shows, and the readable or not-readable decision, and the design is updated in place when it is.
