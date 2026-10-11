# Requirements: Contacts Lists

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md) · [Design](03_design.md) · [Tasks](04_tasks.md) · [Execution](05_execution.md)
<!-- spec-nav:end -->

## Introduction

This feature gives assistants the Contacts lists that people keep on iPhone and Mac ("Family", "Work"). An assistant can see every list and how many people are in it, page through a list's members, see which lists a person is in, add and remove people, and create, rename and delete lists. Every change is stored the way Contacts stores it, so it appears on the user's devices, and nothing else on any card changes.

**Terms used below:**

- **Person**: a contact that is not a list.
- **List**: a Contacts list (a group), stored in iCloud as its own card that names its members.
- **Member**: a person whom a list names.
- **Missing reference**: a member entry on a list that points at no card in the address book, usually left behind when a person was deleted. Contacts does not show these.
- **List tools**: the new tools this feature adds for reading and changing lists.
- **Listing tool**: the existing `contacts_list` tool.
- **Full contact read**: the record `contacts_get` returns.
- **Warm**: the server has already answered a contacts call for the same account since it started.

**Assumptions:**

- The default iCloud address book is the only one in scope, as for every existing contact tool.
- A list's members are people in that same address book; lists inside lists do not occur.
- Real contacts and real lists are never written during verification.

> [!IMPORTANT]
> Approval gate: approve these requirements before work begins on [03_design.md](03_design.md). The user approved all gates for this feature on 2026-10-11.

## Requirements

### Requirement 1: Seeing every list

**User Story:** As an assistant acting on a person's contacts, I want every list with its name and size, so that I can answer "what lists do I have?" and pick the right one.

#### Acceptance Criteria

1. **R1.1** WHEN the list-reading tool is called, THE List_Tools SHALL return every list in the default address book.
2. **R1.2** THE List_Tools SHALL give each list its id, its name and its member count.
3. **R1.3** THE List_Tools SHALL order lists by name, ignoring case and accents.
4. **R1.4** THE List_Tools SHALL count only members that are people in the address book, leaving missing references out of every count.
5. **R1.5** WHEN the address book has no lists, THE List_Tools SHALL return an empty result without an error.

### Requirement 2: Members of one list

**User Story:** As an assistant, I want the people in one list in the same pages as the rest of the address book, so that I can answer "who is in my Family list?" without reading everyone.

#### Acceptance Criteria

1. **R2.1** WHERE the listing tool is given a list id, THE Contact_Tools SHALL return only the people who are members of that list.
2. **R2.2** WHERE the listing tool is given a list id, THE Contact_Tools SHALL apply the same ordering, page size limits, cursors and field options as an unfiltered listing.
3. **R2.3** WHERE the listing tool is given a list id, THE Contact_Tools SHALL report as the total the number of members that are people in the address book.
4. **R2.4** IF the given list id does not identify a list in the address book, THEN THE Contact_Tools SHALL reject the request with an error saying the list was not found.
5. **R2.5** IF a cursor issued for another list or for the unfiltered listing is passed with a list id, THEN THE Contact_Tools SHALL return an error telling the caller to start again from the first page.
6. **R2.6** WHEN the listing tool is called without a list id, THE Contact_Tools SHALL return the same people, order and entries as before this feature.

### Requirement 3: A person's lists

**User Story:** As an assistant, I want to see which lists a person is in, so that I can answer "is Sam in my Work list?" from the person's record.

#### Acceptance Criteria

1. **R3.1** THE Contact_Tools SHALL include in each full contact read the lists the person is a member of, each with its id and name.
2. **R3.2** THE Contact_Tools SHALL order a person's lists by name, ignoring case and accents.
3. **R3.3** WHEN a person is a member of no list, THE Contact_Tools SHALL return an empty set of lists for that person.
4. **R3.4** WHERE a request's field option names the person's lists, THE Contact_Tools SHALL add them to each entry of the listing, search and upcoming-dates tools.
5. **R3.5** THE Contact_Tools SHALL state in the full-read tool's description that a person's lists are read-only there and are changed with the list tools.

### Requirement 4: Adding and removing people

**User Story:** As an assistant, I want to add people to a list and remove them, so that I can act on "add Sam to Work" or "take Jo off the party list".

#### Acceptance Criteria

1. **R4.1** WHEN the membership tool is called with people to add, THE List_Tools SHALL make each of them a member of the list.
2. **R4.2** WHEN the membership tool is called with people to remove, THE List_Tools SHALL remove each of them from the list.
3. **R4.3** WHEN a person to add is already a member, or a person to remove is not a member, THE List_Tools SHALL leave that person's membership as it is and report the person as unchanged.
4. **R4.4** IF a contact id to add or remove does not identify a person in the address book, THEN THE List_Tools SHALL reject the request without changing the list.
5. **R4.5** IF one request both adds and removes the same person, THEN THE List_Tools SHALL reject the request without changing the list.
6. **R4.6** THE List_Tools SHALL keep every member entry that a request does not name, including missing references.
7. **R4.7** THE List_Tools SHALL keep every property of the list card that the request does not change, apart from the card's revision timestamp.
8. **R4.8** WHEN a request would leave the list exactly as stored, THE List_Tools SHALL send no write to iCloud.
9. **R4.9** IF the list changes in iCloud between the read and the write of a membership change, THEN THE List_Tools SHALL apply the requested change to the newer version, keeping the other change.
10. **R4.10** IF the list changes in iCloud again before that second write, THEN THE List_Tools SHALL return a conflict error without writing.
11. **R4.11** WHEN a membership change completes, THE List_Tools SHALL return the list's id, name and member count with the people added, removed and unchanged.

### Requirement 5: Creating, renaming and deleting lists

**User Story:** As an assistant, I want to create, rename and delete lists, so that I can organise the user's contacts the way Contacts on iPhone can.

#### Acceptance Criteria

1. **R5.1** WHEN the create tool is called with a name, THE List_Tools SHALL create a list with that name.
2. **R5.2** WHERE people are given to the create tool, THE List_Tools SHALL create the list with them as its members.
3. **R5.3** IF the requested name is empty after trimming spaces, THEN THE List_Tools SHALL reject the request without writing.
4. **R5.4** IF another list already has the requested name, ignoring case and accents, THEN THE List_Tools SHALL reject the create or rename without writing.
5. **R5.5** WHEN the update tool is given a new name, THE List_Tools SHALL rename the list and keep its members.
6. **R5.6** WHEN the delete tool is called with a list id, THE List_Tools SHALL delete that list.
7. **R5.7** THE List_Tools SHALL leave every person card unchanged when a list is created, changed or deleted.
8. **R5.8** IF the id given to the update or delete tool identifies a card that is not a list, THEN THE List_Tools SHALL reject the request without changing any card.
9. **R5.9** IF a list changes in iCloud between the read and the write of a rename or a delete, THEN THE List_Tools SHALL return a conflict error without writing.
10. **R5.10** THE List_Tools SHALL store a new list with the same card properties as a list created in Contacts on iPhone, so that it appears on the user's devices.
11. **R5.11** WHEN a list is created or renamed, THE List_Tools SHALL return its id, name and member count.

### Requirement 6: Freshness, safety and compatibility

**User Story:** As a person whose assistant manages my lists, I want answers that match my phone and edits that touch only what I asked for, so that I can trust the assistant with my address book.

#### Acceptance Criteria

1. **R6.1** WHEN a list tool or a list-filtered listing is called, THE List_Tools SHALL answer from data checked against iCloud with the caller's credentials during that call.
2. **R6.2** WHEN a list write succeeds, THE List_Tools SHALL show the change in the next read for the same account.
3. **R6.3** WHILE an account is warm, WHEN the list-reading tool is called for an address book of up to 1,800 people and 15 lists, THE List_Tools SHALL answer within 0.5 seconds of network and processing time.
4. **R6.4** IF iCloud returns an error, THEN THE List_Tools SHALL return that error without sending any further write.
5. **R6.5** IF an argument is malformed (a wrong type, an empty name, an id that is not an iCloud address, or an update with nothing to change), THEN THE List_Tools SHALL reject the request before sending any request to iCloud.
6. **R6.6** THE List_Tools SHALL mark the list-reading tool as read-only, the create tool as a write, and the update and delete tools as destructive.
7. **R6.7** THE Contact_Tools SHALL keep the parameters and responses of every existing contact tool unchanged, apart from the optional list id on the listing tool and the lists of a person in contact reads.

### Requirement 7: Guidance for assistants

**User Story:** As a user setting up an assistant, I want the tools to explain themselves, so that the assistant uses lists correctly without trial and error.

#### Acceptance Criteria

1. **R7.1** THE List_Tools SHALL state in their descriptions that list members are given and returned as contact ids.
2. **R7.2** THE List_Tools SHALL state in the delete tool's description that deleting a list keeps the people in it.
3. **R7.3** THE Project_Docs SHALL describe every list tool and the list filter on the listing tool.
