# Requirements: Contact Fields Completion

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md) · [Design](03_design.md) · [Tasks](04_tasks.md)
<!-- spec-nav:end -->

## Introduction

This feature makes the remaining iOS and macOS person fields readable and editable through the iCloud MCP contacts tools. It covers name parts, phonetic names, nickname, maiden name, organization details, related names and instant messages. It also guarantees that Apple-only fields survive every edit, and exposes the readable ones once their storage format is confirmed.

**Terms used below:**

- **Contact read**: the contact object returned by `contacts_get`, `contacts_list` and `contacts_search`.
- **Create or update request**: a call to `contacts_create` or `contacts_update`.
- **Live snapshot**: a read-only copy of the user's live iCloud address book (1,803 people).
- **Throwaway contact**: a test card created only for verification, which the user deletes.
- **Apple-only fields**: pronouns, ringtone, text tone, lunar birthday and verification code.

**Assumptions:**

- Real contacts are never written during verification; only throwaway contacts are.
- The iPhone, reached through iPhone Mirroring or by the user, is the reference for what people see. The test Mac's Contacts app is out of sync with iCloud.

> [!IMPORTANT]
> Approval gate: approve these requirements before work begins on [03_design.md](03_design.md).

## Requirements

### Requirement 1: Structured name parts

**User Story:** As an assistant acting on a person's contacts, I want each part of a contact's name as a separate field, so that I can rename someone without losing their middle name, prefix or suffix.

#### Acceptance Criteria

1. **R1.1** WHEN a contact is read, THE Contact_Reader SHALL return `prefix`, `first_name`, `middle_name`, `last_name` and `suffix` as separate text values, each empty when the contact has none.
2. **R1.2** WHEN a create or update request provides a name part, THE Contact_Writer SHALL store that part with the provided value.
3. **R1.3** WHEN an update request provides only a display name, THE Contact_Writer SHALL keep the contact's existing prefix, middle name and suffix.
4. **R1.4** WHEN an update request provides only a display name that contains the contact's existing prefix, middle name or suffix, THE Contact_Writer SHALL exclude those parts from the stored first and last name.
5. **R1.5** WHEN an update request changes a name part without providing a display name, THE Contact_Writer SHALL set the display name to the non-empty parts joined in the order prefix, first, middle, last, suffix.
6. **R1.6** WHEN a create or update request provides a name part as an empty string, THE Contact_Writer SHALL clear that part.
7. **R1.7** IF a create request provides no display name, no name part and no organization, THEN THE Contact_Writer SHALL reject the request with an error that names the missing fields.

### Requirement 2: Phonetic names, nickname and maiden name

**User Story:** As an assistant, I want phonetic names, nicknames and maiden names, so that I can find, address and pronounce people the way the user knows them.

#### Acceptance Criteria

1. **R2.1** WHEN a contact is read, THE Contact_Reader SHALL return `phonetic_first_name`, `phonetic_middle_name`, `phonetic_last_name`, `nickname` and `maiden_name`, each empty when the contact has none.
2. **R2.2** WHEN a create or update request provides one of these fields with text, THE Contact_Writer SHALL store that text.
3. **R2.3** WHEN a create or update request provides one of these fields as an empty string, THE Contact_Writer SHALL remove that field from the contact.
4. **R2.4** WHEN a throwaway contact with phonetic names, a nickname and a maiden name is stored, THE Contacts_App SHALL display each value on the contact card.

### Requirement 3: Organization details

**User Story:** As an assistant, I want a contact's department, phonetic company name and company-card setting, so that I can keep work details complete without wiping any of them.

#### Acceptance Criteria

1. **R3.1** WHEN a contact is read, THE Contact_Reader SHALL return `department`, `phonetic_organization` and `is_company` alongside `organization`.
2. **R3.2** WHEN an update request provides an organization without a department, THE Contact_Writer SHALL keep the contact's existing department.
3. **R3.3** WHEN a create or update request provides a department, THE Contact_Writer SHALL store it as the contact's department, clearing it when the value is empty.
4. **R3.4** WHEN a create or update request provides a phonetic organization, THE Contact_Writer SHALL store it, clearing it when the value is empty.
5. **R3.5** WHEN a create or update request sets `is_company` to true, THE Contact_Writer SHALL mark the contact to display as a company.
6. **R3.6** WHEN an update request sets `is_company` to false, THE Contact_Writer SHALL mark the contact to display as a person.
7. **R3.7** IF a create or update request sets `is_company` to true for a contact that would have no organization, THEN THE Contact_Writer SHALL reject the request with an error asking for an organization.

### Requirement 4: Related names

**User Story:** As an assistant, I want a contact's related people with their relationship labels, so that I can answer questions such as "who is Sam's spouse?" and record new relationships.

#### Acceptance Criteria

1. **R4.1** WHEN a contact is read, THE Contact_Reader SHALL return `related_names` as a list of entries with `label` and `name`, using the label text the Contacts app shows for built-in labels.
2. **R4.2** WHEN an update request provides `related_names`, THE Contact_Writer SHALL replace the contact's related names with the provided list.
3. **R4.3** WHEN a related-name entry is sent back unchanged, THE Contact_Writer SHALL keep its stored form byte-identical.
4. **R4.4** WHEN a new related-name entry has no label, THE Contact_Writer SHALL store it with the label `other`.
5. **R4.5** IF a related-name entry has an empty name, THEN THE Contact_Writer SHALL reject the request with an error identifying the entry.
6. **R4.6** WHEN a throwaway contact has a related name with a built-in label and one with a custom label, THE Contacts_App SHALL display both names with their labels.

### Requirement 5: Instant messages

**User Story:** As an assistant, I want a contact's instant-messaging accounts with their service, so that I can tell the user how to reach someone on Skype or another messaging app.

#### Acceptance Criteria

1. **R5.1** WHEN a contact is read, THE Contact_Reader SHALL return `instant_messages` as a list of entries with `label`, `service` and `username`.
2. **R5.2** WHEN an update request provides `instant_messages`, THE Contact_Writer SHALL replace the contact's instant-message accounts with the provided list.
3. **R5.3** WHEN an instant-message entry is sent back unchanged, THE Contact_Writer SHALL keep its stored form byte-identical, including Apple's app identifiers.
4. **R5.4** IF an instant-message entry has an empty service or an empty username, THEN THE Contact_Writer SHALL reject the request with an error identifying the entry.
5. **R5.5** WHEN a throwaway contact has instant-message accounts, THE Contacts_App SHALL display each account's service and username.

### Requirement 6: Preservation of everything else

**User Story:** As a person whose contacts an assistant edits, I want every field the tools do not model to survive edits, so that pronouns, ringtones and other Apple data are never lost.

#### Acceptance Criteria

1. **R6.1** THE Contact_Writer SHALL keep every stored property that the tools do not model byte-identical through any create or update request.
2. **R6.2** WHEN a read-then-write-back of every field is replayed over the live snapshot, THE Contact_Writer SHALL change zero contacts.
3. **R6.3** WHEN an edit of a single field is replayed over the live snapshot, THE Contact_Writer SHALL change only the stored lines of that field.
4. **R6.4** THE Contact_Reader SHALL omit pronoun data from contact reads.
5. **R6.5** THE Contact_Tools SHALL offer no create or update parameter for pronouns, ringtone, text tone, lunar birthday or verification code.

### Requirement 7: Readable Apple-only fields

**User Story:** As an assistant, I want the Apple-only fields whose format is readable, so that I can tell the user a contact's lunar birthday or assigned ringtone.

#### Acceptance Criteria

1. **R7.1** WHEN the format investigation runs, THE Format_Investigation SHALL record the stored encoding of lunar birthday, ringtone, text tone and verification code taken from a throwaway contact that was given those fields on the iPhone.
2. **R7.2** WHERE the lunar birthday encoding is confirmed readable, THE Contact_Reader SHALL return `lunar_birthday` with its calendar, month and day, and its year when one is stored.
3. **R7.3** WHERE the ringtone encoding is confirmed readable, THE Contact_Reader SHALL return `ringtone` and `text_tone` as the sound names the Contacts app shows.
4. **R7.4** WHERE the verification code encoding is confirmed readable, THE Contact_Reader SHALL return `verification_code` as the value the Contacts app shows.
5. **R7.5** IF a stored Apple-only value cannot be decoded, THEN THE Contact_Reader SHALL omit that field from the contact read.

### Requirement 8: Consistent tool contract

**User Story:** As an assistant developer, I want every new field to behave like the fields already shipped, so that clients need no special cases.

#### Acceptance Criteria

1. **R8.1** THE Contact_Tools SHALL use the same key for each new field in contact reads and in create or update parameters.
2. **R8.2** WHEN an update request omits a field, THE Contact_Writer SHALL leave that field unchanged.
3. **R8.3** WHEN a search query matches a contact's nickname, maiden name, related name or instant-message username, THE Contact_Search SHALL return that contact.
4. **R8.4** WHEN a throwaway contact carrying every new writable field is created through the MCP server, THE Contacts_App SHALL display each field with its intended value and label.
5. **R8.5** WHEN that throwaway contact is then updated through the MCP server, THE Contacts_App SHALL display the changed values and the unchanged fields as before.
6. **R8.6** IF a contact changed on another device after it was read for an update, THEN THE Contact_Writer SHALL return a conflict error without overwriting the other change.
