# Discovery: Upcoming Birthdays and Dates

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md)
<!-- spec-nav:end -->

## Problem and Outcome

The user wants their assistant (Muse) to answer "whose birthday is it today?" and show each person's Snapchat and Instagram links, so they can send wishes. Contact search in [`src/icloud_mcp/contacts.py`](../../src/icloud_mcp/contacts.py) does not match dates, so today the only way is to page through every person with their birthday and social profiles and filter in the assistant.

Measured read-only on the user's live iCloud account (1,798 people):

| Measure | Value |
|---|---|
| People with a birthday | 1,655, of which only 16 store the year |
| Birthdays per calendar day | median 4, maximum 12 |
| Birthdays in a 7-day window | median 31, maximum 48 |
| Birthday people with Snapchat or Instagram | 1,617; only 182 have a phone number |
| Snapchat / Instagram profiles stored as a web link | 1,592 of 1,597 / 595 of 608; the rest are `x-apple:username`, which is not tappable |
| Anniversaries and other significant dates, alternate-calendar birthdays | none stored |
| Cost of answering today by paging everyone | about 9 calls and about 180k tokens |

**Outcome:** one small, read-only call returns the people with a birthday or significant date on a given day or in a short window, each with tappable social links and their first phone and email, for about 1k tokens on a busy day.

## Users and Current Workaround

- **Users:** AI assistants acting on a person's iCloud contacts (Muse, Claude and any other MCP client), over stdio or HTTP.
- **Current workaround:** `contacts_list` with `fields=["name", "birthday", "social_profiles"]` across every page, filtered by the assistant. It is slow, costs about 180k tokens, and leaves the assistant to turn `x-apple:` values into links.

## Scope and Non-Goals

**In scope:**

- **A new read-only tool** that takes a start date (default: today in the server's `DEFAULT_TIMEZONE`) and a window length in days, and returns every person with a birthday or significant date (anniversary, other, or a custom label) in that window, soonest first.
- **Each entry** carries the person's id and name, each matching occasion (its kind or label, the date it falls on in the window, how many days away, and the age or number of years when the original year is known), every social profile as a tappable web link, and the first phone and email. A `fields` option adds other contact fields, as in `contacts_list`.
- **Links:** a profile stored as an `x-apple:` value gets a web link built from its username for services with a known profile URL (Snapchat, Instagram, Facebook, LinkedIn, Twitter, TikTok); others keep their username without a link.
- **Paging** with the same `page_size` and `cursor` contract as list and search, so a long window stays bounded.
- **The same change-checked cache** as list and search, so answers reflect the current iCloud data.
- **An assistant instruction** the user can give Muse, delivered with the pull request; the repository README gets a generic usage note only.

**Non-goals:**

- Alternate-calendar birthdays (Chinese, Hebrew, Hijri and others): they need calendar conversion and none are stored.
- Choosing which services to show: the server returns every profile; the assistant instruction picks Snapchat and Instagram.
- Reminders, notifications or scheduled messages; the assistant asks when the user asks.
- Changing `social_profiles` in existing tools: their raw `url` stays as stored, because writes send it back unchanged.

## Constraints and Success Measures

**Constraints:**

- **Generic client.** Nothing Muse-specific in the code; the instruction text lives outside the server.
- **Read-only.** The tool never writes; live verification is read-only.
- **No breaking change** to existing tools.

**Success measures:**

| Measure | Target |
|---|---|
| "Today" on the user's busiest day | at most about 4k tokens of output (12 people) |
| Warm call | under 0.5 s, like list and search |
| People returned for a day or window | exactly those whose stored birthday or date falls in it, checked against an independent computation over the raw vCards |
| Snapchat and Instagram profiles in results | every one carries a web link |

## Approaches Considered

| Approach | Benefits | Costs / risks | Reversibility | Decision |
|---|---|---|---|---|
| **A. A dedicated read-only tool over the cache** | Tiny output; explicit for assistants; reuses the change-checked cache, projection and paging; can say which occasion matched and how far away it is | One more tool in the surface | High: additive | **Chosen** |
| B. A date filter on `contacts_search` | No new tool | A word query and a date window combine awkwardly; results cannot say which date matched or how many days away; ranking no longer fits | Medium | Rejected |
| C. Instructions only: the assistant pages everything and filters | No code | About 9 calls and 180k tokens per question; `x-apple:` links stay untappable | High | Rejected: cost |

## Chosen Direction

Use approach A: a read-only tool that reads the same cached people as list and search, finds each birthday and significant date that falls in the requested window, and returns paged entries with links, phone and email by default.

- **Dates without a year** (iOS stores most birthdays this way) match on month and day; no age is given.
- **February 29** falls on February 28 in years that are not leap years.
- **Windows cross the year end** (December 30 for 7 days includes early January).
- **Ordering:** soonest first, then by name.

## Architecture and Flow Outline

The tool sits beside `contacts_list` and `contacts_search`:

1. The tool validates the start date, window and paging arguments, then checks iCloud for changes through the existing cache.
2. For each cached person, it computes the occurrences of their birthday and significant dates inside the window.
3. Matching people are sorted by the soonest occurrence and name, paged, and projected into entries with links, phone and email.

Changes stay inside [`src/icloud_mcp/contacts.py`](../../src/icloud_mcp/contacts.py) and the tool declarations in [`src/icloud_mcp/server.py`](../../src/icloud_mcp/server.py), plus tests and the README.

## Failure and Verification Strategy

**Failures:**

- An invalid start date, window or cursor raises a validation error before any request.
- iCloud errors surface exactly as in list and search, with no data returned.
- A stored date that cannot be read is skipped for that person rather than failing the call.

**Verification:**

1. **Unit tests** on fixed dates: no-year and with-year birthdays, the Apple sentinel year, February 29, a window across the year end, custom-labelled dates, groups excluded, `x-apple:` link building, paging and projection.
2. **Protocol tests** for the tool schema and the unchanged existing tools.
3. **A read-only live check:** today, a 7-day window and a full year compared with an independent count over the raw vCards; output size measured; every Snapchat and Instagram profile in results has a web link. Aggregates only.

## Open Decisions

| Decision | Owner | Resolved by |
|---|---|---|
| Tool and parameter names, the window's upper bound and the entry's exact keys | Claude | design |

## Approval

Status: **Approved on 2026-10-11.** The user chose a dedicated tool, full links plus phone and email in each entry, and a new stacked pull request, and approved all gates for this feature.
