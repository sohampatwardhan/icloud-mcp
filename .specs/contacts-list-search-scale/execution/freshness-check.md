# Freshness Check: an iPhone Edit and Deletion Reach the Next Call (R4.3)

Run on 2026-10-11 with the user's go-ahead at checkpoint 4. One MCP server process (the real FastMCP tools, called in process) stayed up for the whole test, so every answer below came from the same warm cache with no restart. The process refused every HTTP method except GET, HEAD, OPTIONS, PROPFIND and REPORT, and self-checked that guard before its first request. Only the two throwaway cards were touched.

## Steps and results

| Step | What happened | Result |
|---|---|---|
| 1. Warm | `contacts_search("zz mcp")` cold-loaded the book (discovery, sync token, full download, 2.2 s). A second call was one `sync-collection` (233 ms). All 1,815 ETags snapshotted. | Two throwaway cards returned: "ZZ MCP Fields Test" and "Dr. ZZ MCP Q Renamed Jr." |
| 2. Edit on the iPhone | Through iPhone Mirroring, the middle name of "Dr. ZZ MCP Q Renamed Jr." was changed from "Q" to "Fresh" and saved. A separate read-only GET confirmed iCloud had the new `N:` value before the next call. | iOS also cleared that card's phonetic middle name ("kyoo") on its own, as it does when a name part changes. |
| 3. First call after the edit | `contacts_search("zz mcp")` on the same server: one `sync-collection` plus one `addressbook-multiget` (the warm delta path, no rediscovery), 381 ms. | Returned "Dr. ZZ MCP Fresh Renamed Jr." Whole-book ETag diff: 1 card changed (the throwaway card), 0 added, 0 removed. |
| 4. Deletion by the user | The user deleted both throwaway cards on the iPhone. Read-only GETs confirmed 404 for both before the next call. | — |
| 5. First calls after the deletion | A full `contacts_list` traversal (9 pages of 200, one `sync-collection` each), then `contacts_search("zz mcp")`. | 1,798 people listed (1,800 before), 0 throwaway cards; search total 0. Whole-book ETag diff: 0 changed, 0 added, 2 removed, both of them the throwaway cards. |

## Outcome

- The edited value was returned by the first call after the edit.
- The deleted cards were absent from the first calls after the deletion.
- The ETag comparisons show no other card changed at any step.
- No throwaway cards remain in the account.
