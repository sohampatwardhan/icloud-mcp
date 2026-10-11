# Discovery: Contacts List Size and Search Speed

<!-- spec-nav:start -->
**Spec navigation:** [State](00_state.md) · [Discovery](01_discovery.md) · [Requirements](02_requirements.md) · [Design](03_design.md) · [Tasks](04_tasks.md)
<!-- spec-nav:end -->

## Problem and Outcome

`contacts_list` and `contacts_search` in [`src/icloud_mcp/contacts.py`](../../src/icloud_mcp/contacts.py) were built for small address books, and they break down for an AI assistant working on a real one. Measured on the user's live iCloud account (1,799 people and 15 lists) after the contact-fields work:

| Measure | Today |
|---|---|
| `contacts_list` output, every person, full records | about 1,774 KiB (about 454k tokens) |
| The same with only id, name and organization | about 285 KiB (about 72k tokens); the 105-character URL ids dominate |
| Time per list or search call | about 2.1 s: CardDAV discovery (5 requests, 0.9 s) plus a full address-book download (1.56 MB, 1.25 s) |
| Local parsing of every card | 0.19 s |
| Search matching | case-insensitive substring only: "jose" misses "José", the words of "jane smith" must be adjacent, and results are unranked and unpaged |

**Outcome:** an assistant can page through any address book in small pieces, search it reliably by name, and get answers in well under a second, with results that always reflect the current iCloud data.

## Users and Current Workaround

- **Users:** AI assistants acting on a person's iCloud contacts (Muse, Claude and any other MCP client), whether the server runs locally over stdio or as a multi-user HTTP service.
- **Current workaround:** pass `limit` to `contacts_list`. That truncates in storage order, with no way to reach the next page, so an assistant either floods its context with everything or sees an arbitrary few hundred contacts.

## Scope and Non-Goals

**In scope:**

- **`contacts_list` paging.** Results are sorted by display name in pages, 50 by default, with an opaque cursor for the next page. Each entry is a compact summary: id, name, organization, first phone and first email. A `fields` option returns more fields or full records.
- **`contacts_search` results** use the same paging and summary shape. Matching gains:
  - accent- and case-insensitive comparison ("jose" finds "José");
  - every query word required, in any order;
  - ranking with display-name matches first.
- **A checked in-memory cache, per account.** It holds the discovered address-book URL and the parsed cards.
  - **Freshness:** every list or search call first asks iCloud what changed since the cache's sync token, using the caller's own credentials, and applies only changed and deleted cards.
  - **Fallback:** if iCloud rejects the token, it falls back to one full download.
  - **Own writes:** the server's create, update and delete calls keep the cache current.
  - **Limits:** the number of accounts held and their idle lifetime are capped.
- `contacts_get` remains the full-record view, and writes keep their current behaviour.

**Non-goals:**

- Typo-tolerant ("fuzzy") search.
- A cache stored on disk, and data that persists across server restarts.
- Server-side search through CardDAV `text-match`.
- Shorter contact ids.
- Contact photos and Contacts lists (groups), which are separate specs.

## Constraints and Success Measures

**Constraints:**

- **Generic client.** The server is a general CardDAV/MCP client with nothing host-specific in it.
- **Both modes.** It has to work in stdio mode (local, one account) and in stateless HTTP mode (many accounts, with credentials on each request).
- **Never stale, never shared.** Cached data must not be served without a fresh authenticated check against iCloud on the same call, and must never be visible to a caller with different credentials.
- **Test data.** Real contacts are never written during testing; verification uses read-only snapshots and throwaway cards only.
- **Breaking changes are acceptable.** `contacts_list` and `contacts_search` may change their output shape; the change will be noted in the pull request.

**Success measures:**

| Measure | Target |
|---|---|
| Warm list or search call, 1,799 people | under 0.5 s end to end |
| Default page | under 4k tokens |
| A change made on another device | reflected in the next call: 0 stale results across the edit test |
| Wrong password over HTTP with a warm cache | 401 error, no cached data returned |
| "jose" with a contact named "José" | found, and ranked above organization-only matches |
| Full output of `contacts_list` across all pages | every person exactly once |

## Approaches Considered

| Approach | Benefits | Costs / risks | Reversibility | Decision |
|---|---|---|---|---|
| **A. Checked in-memory cache + paged summaries + improved local search** | About 0.15 s per warm call (one `sync-collection` request, measured at 0.14 s and 178 bytes with no changes); never stale; small pages; better recall | Cache code with memory caps; the incremental-sync path must handle changes, deletions and expired tokens correctly | High: the cache can be disabled and fall back to today's full fetch | **Chosen** |
| B. Time-based cache without checking | Simplest and fastest | Returns stale contacts after an edit on another device; in HTTP mode it could serve data without re-checking credentials | High | Rejected: freshness and credential risk |
| C. iCloud-side search (`addressbook-query` with `text-match`) | No cache needed for search | A `contains` match for "test" on `FN` returned 0 results although a matching card exists; iCloud also ignores partial `address-data` requests and always sends the full 1.56 MB | — | Rejected on live evidence |
| D. On-disk cache | Survives restarts | Leaves personal data on disk; needs invalidation and encryption | Low | Rejected: privacy |

## Chosen Direction

Use approach A.

- **The cache.** One in-process cache entry per account holds the address-book URL, the sync token and the parsed cards keyed by URL, together with their ETags.
- **Each read call:**
  1. Run `sync-collection` with the stored token, authenticating with the caller's credentials.
  2. Fetch the bodies of changed cards and drop deleted ones.
  3. Serve the request from memory.
- **Without a usable token** (first use, or a token iCloud rejects), do one full download.
- **Writes** made through the server update or evict their own entry.
- **Paging and summaries** are computed from the cache. Search folds accents and case, requires every word, ranks name matches first, and pages the results.

## Architecture and Flow Outline

All changes stay inside [`src/icloud_mcp/contacts.py`](../../src/icloud_mcp/contacts.py), a new small cache module, and the tool declarations in [`src/icloud_mcp/server.py`](../../src/icloud_mcp/server.py). Writes keep their current request flow.

1. **Read call:** cache lookup by credentials → incremental sync (`sync-collection` with the token; 0 or more GETs, or a full REPORT as fallback) → sort, filter, project → page.
2. **Write call:** the existing GET / PUT or DELETE flow, then the cache entry for that card is updated or evicted.

## Failure and Verification Strategy

**Failures:**

- **Sync rejected.** If iCloud rejects the sync token (a `valid-sync-token` error or HTTP 403/409), the cache does one full download and continues.
- **Authentication failure.** Any authentication failure on the sync request surfaces as today's 401/403 tool error, and the cached data is not used.
- **Unknown cursor.** A cursor that is malformed or belongs to a different query returns a clear error asking the caller to restart from the first page.

**Verification:**

1. **Unit tests** with a fake CardDAV server covering the initial sync, a changed card, a deleted card, an expired token, credential isolation, cursor paging and search folding.
2. **Read-only live timing runs** on the user's account, cold and warm.
3. **The cache picks up real changes.** One throwaway card is edited on the iPhone by the user or through iPhone Mirroring. The next list and search calls must reflect the edit, and must drop the card after the user deletes it.

## Open Decisions

| Decision | Owner | Resolved by |
|---|---|---|
| Exact cache caps (accounts held, idle lifetime) and page-size limits | Claude | design, from measured memory per account (about 1.6 MB raw) |
| What iCloud's `sync-collection` returns for changed and deleted cards with an older token | Claude | read-only probes during design, using a token taken before the user edits a throwaway card |

## Approval

Status: **Approved on 2026-10-11** by the user, with the code to land on a new branch `feature/contacts-list-search-scale` stacked on `feature/birthday-social-profiles`, and its own pull request.
