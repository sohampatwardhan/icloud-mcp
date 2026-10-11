# Requirements: Contacts List Size and Search Speed

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md) · [Design](03_design.md) · [Tasks](04_tasks.md)
<!-- spec-nav:end -->

## Introduction

This feature makes `contacts_list` and `contacts_search` usable on real address books. Results come back in small pages of compact summaries, search matches names the way people type them, and warm calls answer in under half a second. Every result must still reflect the current iCloud data.

**Terms used below:**

- **Person**: a contact that is not a Contacts list (group).
- **Display name**: the `name` value a contact read returns.
- **Page**: one response of the list or search tool.
- **Cursor**: an opaque value returned with a page that requests the next page.
- **Warm**: the server has already answered a list or search call for the same account since it started.
- **Change check**: the step in which the server asks iCloud what changed since its last call for that account.

**Assumptions:**

- iCloud's CardDAV service keeps offering incremental change reports (verified on 2026-10-11: a check with the current token took 0.14 s, and an invalid token is answered with HTTP 412).
- Real contacts are never written during verification; only throwaway cards are.

> [!IMPORTANT]
> Approval gate: approve these requirements before work begins on [03_design.md](03_design.md).

## Requirements

### Requirement 1: Paged listing

**User Story:** As an assistant acting on a person's contacts, I want the address book in small, ordered pages, so that I can see everyone without flooding my context.

#### Acceptance Criteria

1. **R1.1** WHEN `contacts_list` is called without a cursor, THE Contact_List SHALL return the first page of people sorted by display name, ignoring case and accents.
2. **R1.2** THE Contact_List SHALL return at most 50 people per page unless the request sets a page size.
3. **R1.3** IF a requested page size is below 1 or above 200, THEN THE Contact_Tools SHALL reject the request with a validation error.
4. **R1.4** WHEN more people remain after a page, THE Contact_List SHALL return a cursor that yields the next page.
5. **R1.5** WHEN the last page is returned, THE Contact_List SHALL return no cursor.
6. **R1.6** WHEN every page is requested in order and no contact changes in between, THE Contact_List SHALL have returned every person exactly once.
7. **R1.7** IF contacts are added or removed between page requests, THEN THE Contact_List SHALL still return no person twice across those pages.
8. **R1.8** IF a cursor is malformed or was issued for a different query, THEN THE Contact_List SHALL return an error telling the caller to start again from the first page.
9. **R1.9** WHEN a page is returned, THE Contact_List SHALL include the total number of people the query matches.

### Requirement 2: Summaries and field selection

**User Story:** As an assistant, I want compact entries by default and the fields I need on request, so that each page stays small without extra calls.

#### Acceptance Criteria

1. **R2.1** THE Contact_List SHALL return each person by default as a summary of `id`, `name`, `organization`, first phone and first email.
2. **R2.2** WHERE the request names fields, THE Contact_List SHALL return each person's `id` plus the named fields.
3. **R2.3** WHERE the request asks for all fields, THE Contact_List SHALL return each person exactly as `contacts_get` returns them.
4. **R2.4** IF the request names an unknown field, THEN THE Contact_Tools SHALL reject it with an error listing the valid field names.
5. **R2.5** WHEN a default page of 50 summaries is returned for the user's address book, THE Contact_List SHALL produce at most 16,000 bytes of JSON, which is about 4,000 tokens.

### Requirement 3: Search that matches how people type names

**User Story:** As an assistant, I want search to find people regardless of accents, word order and case, with the best matches first, so that "jose garcia" finds "José García".

#### Acceptance Criteria

1. **R3.1** WHEN a search query is given, THE Contact_Search SHALL match text ignoring case and accents.
2. **R3.2** WHEN a query contains several words, THE Contact_Search SHALL return only people for whom every word matches at least one searched field.
3. **R3.3** THE Contact_Search SHALL rank people whose display name matches the query ahead of people matched only through other fields.
4. **R3.4** THE Contact_Search SHALL return results with the same paging, cursor, total count and field options as the list.
5. **R3.5** WHEN a query contains digits, THE Contact_Search SHALL match phone numbers by their digits, ignoring formatting.
6. **R3.6** THE Contact_Search SHALL search every field it searches today, plus the same fields in their accent-folded form.
7. **R3.7** IF a query is empty after trimming, THEN THE Contact_Tools SHALL reject it with a validation error.

### Requirement 4: Fast answers that are never stale

**User Story:** As a person whose assistant reads my contacts, I want quick answers that always match what my phone shows, so that the assistant never works from outdated or someone else's data.

#### Acceptance Criteria

1. **R4.1** WHILE an account is warm, WHEN a list or search call is made for an address book of up to 1,800 people, THE Contact_Cache SHALL answer within 0.5 seconds of network and processing time.
2. **R4.2** WHEN a list or search call is made, THE Contact_Cache SHALL run a change check against iCloud with the caller's credentials before answering.
3. **R4.3** WHEN a contact was changed, added or deleted on another device since the previous call, THE Contact_Cache SHALL reflect that change in the answer.
4. **R4.4** IF the change check fails authentication, THEN THE Contact_Cache SHALL return the authentication error without returning any contact data.
5. **R4.5** IF iCloud rejects the stored change token, THEN THE Contact_Cache SHALL rebuild the account's data with one full download and answer from the rebuilt data.
6. **R4.6** IF the change check fails for any other reason, THEN THE Contact_Cache SHALL return the error without returning any contact data.
7. **R4.7** WHEN the server creates, updates or deletes a contact, THE Contact_Cache SHALL reflect that write in the next list or search call.
8. **R4.8** THE Contact_Cache SHALL keep each account's data separate, so a caller only receives data fetched under the account they authenticate as.
9. **R4.9** THE Contact_Cache SHALL hold data for no more than a configured number of accounts.
10. **R4.10** WHEN an account has had no list or search call for a configured idle time, THE Contact_Cache SHALL discard that account's data.
11. **R4.11** WHERE the cache is disabled by configuration, THE Contact_Cache SHALL fetch the full address book on every list or search call.

### Requirement 5: Compatibility

**User Story:** As an assistant developer, I want everything outside list and search to keep working as before, so that existing clients only need to adapt to the new list and search output.

#### Acceptance Criteria

1. **R5.1** THE Contact_Tools SHALL keep `contacts_get` returning the full contact record.
2. **R5.2** THE Contact_Tools SHALL keep the request and response shapes of `contacts_create`, `contacts_update` and `contacts_delete` unchanged.
3. **R5.3** THE Contact_Tools SHALL leave stored contacts unchanged on every list or search call.
