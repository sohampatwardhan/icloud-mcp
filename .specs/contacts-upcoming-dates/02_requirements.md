# Requirements: Upcoming Birthdays and Dates

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md)
<!-- spec-nav:end -->

## Introduction

This feature lets an assistant ask "whose birthday is it today?" (or this week) with one small call. The answer lists each person with the occasion, how far away it is, their age when known, every social profile as a link, and their first phone and email, so the user can send wishes straight away. It reads the same change-checked contact data as list and search.

**Terms used below:**

- **Person**: a contact that is not a Contacts list (group).
- **Significant date**: a date stored on a contact besides the birthday, such as an anniversary or a date with a custom label.
- **Occasion**: one birthday or significant date of a person, placed on the day it falls in a given year.
- **Window**: the consecutive days, starting with the start date, that a call asks about.
- **Upcoming-dates tool**: the new read-only tool this feature adds.
- **Warm**: the server has already answered a contacts call for the same account since it started.

**Assumptions:**

- Most stored birthdays have no year (1,639 of 1,655 on the user's account); Apple stores those with a placeholder year that contact reads already report as "no year".
- The server's configured default timezone is the user's timezone unless the caller passes a start date.
- Real contacts are never written during verification.

> [!IMPORTANT]
> Approval gate: approve these requirements before work begins on `03_design.md`. The user approved all gates for this feature on 2026-10-11.

## Requirements

### Requirement 1: Finding occasions in a window

**User Story:** As an assistant acting on a person's contacts, I want everyone with a birthday or significant date on a given day or in the next few days, so that I can remind the user without reading the whole address book.

#### Acceptance Criteria

1. **R1.1** WHEN the upcoming-dates tool is called with a start date and a window length, THE Upcoming_Dates SHALL return every person with a birthday or significant date falling on a day of that window.
2. **R1.2** WHEN no start date is given, THE Upcoming_Dates SHALL use today's date in the server's configured default timezone.
3. **R1.3** WHEN no window length is given, THE Upcoming_Dates SHALL cover the start date only.
4. **R1.4** WHEN a stored date has no year, THE Upcoming_Dates SHALL match it by month and day in every year the window touches.
5. **R1.5** WHEN a stored date is February 29 and a year the window touches is not a leap year, THE Upcoming_Dates SHALL place that occasion on February 28 of that year.
6. **R1.6** WHEN the window runs past December 31, THE Upcoming_Dates SHALL match occasions on the window's days in the following year.
7. **R1.7** THE Upcoming_Dates SHALL leave Contacts lists (groups) out of its results.
8. **R1.8** THE Upcoming_Dates SHALL order people by their soonest occasion in the window, then by display name ignoring case and accents.
9. **R1.9** IF a stored date cannot be read as a calendar date, THEN THE Upcoming_Dates SHALL leave that date out without failing the call.

### Requirement 2: What each entry contains

**User Story:** As a person wishing friends a happy birthday, I want each entry to show the occasion and every way to reach the person, so that I can message them in one tap.

#### Acceptance Criteria

1. **R2.1** THE Upcoming_Dates SHALL include each person's id and display name in their entry.
2. **R2.2** THE Upcoming_Dates SHALL list each of the person's occasions in the window with its kind or label, the date it falls on, and the number of days from the start date.
3. **R2.3** WHEN an occasion's original year is stored, THE Upcoming_Dates SHALL include the age or number of years reached on the day it falls.
4. **R2.4** THE Upcoming_Dates SHALL include every social profile of the person with its service and username.
5. **R2.5** WHEN a social profile is stored as a web address, THE Upcoming_Dates SHALL give that address as the profile's link.
6. **R2.6** IF a social profile is stored as an Apple-internal value instead of a web address, THEN THE Upcoming_Dates SHALL give it a link built from its username for services with a known profile address.
7. **R2.7** THE Upcoming_Dates SHALL include the person's first phone number and first email address, empty when there is none.
8. **R2.8** WHERE the request names other contact fields, THE Upcoming_Dates SHALL add those fields to each entry, accepting the same field names as the list tool.
9. **R2.9** WHEN the window is one day long on the user's address book, THE Upcoming_Dates SHALL produce at most 16,000 bytes of JSON per page.

### Requirement 3: Paging and validation

**User Story:** As an assistant, I want long windows to come back in bounded pages and bad input to be rejected clearly, so that a request for a month or a year never floods my context.

#### Acceptance Criteria

1. **R3.1** THE Upcoming_Dates SHALL return at most 50 people per page unless the request sets a page size.
2. **R3.2** IF a requested page size is below 1 or above 200, THEN THE Contact_Tools SHALL reject the request with a validation error.
3. **R3.3** WHEN more people remain after a page, THE Upcoming_Dates SHALL return a cursor that yields the next page.
4. **R3.4** WHEN the last page is returned, THE Upcoming_Dates SHALL return no cursor.
5. **R3.5** WHEN a page is returned, THE Upcoming_Dates SHALL include the total number of people with an occasion in the window.
6. **R3.6** IF a cursor is malformed or was issued for a different start date, window or tool, THEN THE Upcoming_Dates SHALL return an error telling the caller to start again from the first page.
7. **R3.7** IF a requested window is shorter than 1 day or longer than 366 days, THEN THE Contact_Tools SHALL reject the request with a validation error.
8. **R3.8** IF the start date is not a real calendar date written as YYYY-MM-DD, THEN THE Upcoming_Dates SHALL reject the request with a validation error before contacting iCloud.

### Requirement 4: Freshness, speed and compatibility

**User Story:** As a person whose assistant reads my contacts, I want birthday answers that match my phone and arrive quickly, without changing anything else, so that I can trust and keep using the existing tools.

#### Acceptance Criteria

1. **R4.1** WHEN the upcoming-dates tool is called, THE Upcoming_Dates SHALL answer from contact data checked against iCloud with the caller's credentials during that call.
2. **R4.2** WHILE an account is warm, WHEN the upcoming-dates tool is called for an address book of up to 1,800 people, THE Upcoming_Dates SHALL answer within 0.5 seconds of network and processing time.
3. **R4.3** IF the check against iCloud fails, THEN THE Upcoming_Dates SHALL return the error without returning any contact data.
4. **R4.4** THE Contact_Tools SHALL leave stored contacts unchanged on every call of the upcoming-dates tool.
5. **R4.5** THE Contact_Tools SHALL keep the parameters and response shapes of every existing contact tool unchanged.

### Requirement 5: Guidance for assistants

**User Story:** As a user setting up an assistant, I want the tool to explain itself and an example instruction to copy, so that the assistant shows today's birthdays with links without trial and error.

#### Acceptance Criteria

1. **R5.1** THE Contact_Tools SHALL describe the start date default, the window, the entry contents and the paging in the upcoming-dates tool's own description.
2. **R5.2** THE Project_Docs SHALL include an example assistant instruction for showing today's birthdays with each person's social links.
