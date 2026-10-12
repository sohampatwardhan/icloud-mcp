# Write Check: Throwaway Contacts List

Run on 2026-10-12 with the user's explicit yes at the task 3.1 checkpoint, plus a separate yes to delete the list with `contacts_group_delete`. The harness is [write_check.py](write_check.py). Its write guard is installed and self-checked before any request. It sends a PUT only with `If-None-Match: *` (a new card) or with `If-Match` to a card this run created, and a DELETE only to the throwaway list. Every step diffs every card's ETag.

**Status: complete.** The user chose to continue after the step 2 pause, with the iPhone left untouched during the remaining writes. Steps 3 and 4 ran at 03:38Z. The only non-throwaway card differing from the baseline is My Card, and it did not change again during the rename or the delete. See "Unexplained change" below.

## Steps

| Step | What the tools did | ETag diff | iPhone (iPhone Mirroring) | Mac Contacts app (`osascript`) |
|---|---|---|---|---|
| 1. Setup (03:21:59Z) | `create_contact` ×2 ("ZZ MCP List Test 1" and "2"), then `create_group("ZZ MCP List Test", members=[#1])`. 3 PUTs, all `If-None-Match: *` | 1,813 cards: 0 changed, 3 added (the throwaway cards), 0 removed | The list shows "ZZ MCP List Test 1", with My Card at the top. The Lists screen shows the list with count 1 | No ZZ MCP contacts or list, even after 2+ minutes |
| 2. Edit (03:30:12Z) | `update_group(add=[#2], remove=[#1])`: 1 PUT with `If-Match` to the list. Reported added 1, removed 1, `member_count` 1 | 1,816 cards: **2 changed** (the list, and the user's My Card), 0 added, 0 removed | The list shows "ZZ MCP List Test 2", with My Card at the top. The Lists screen shows count 1 | Not checked again (stale, see below) |
| 3. Rename (03:38Z) | `update_group(name="ZZ MCP List Test Renamed")`: 1 PUT with `If-Match` to the list. Reported `member_count` 1, added 0, removed 0 | 1,816 cards: 1 changed (the list), 0 added, 0 removed | Not checked (the iPhone was left untouched during the write) | Not checked |
| 4. Delete (03:38Z) | `delete_group`: 1 DELETE with `If-Match` to the list. Reported "Group ZZ MCP List Test Renamed deleted; its members were not changed" | 1,816 cards: 0 changed, 0 added, 1 removed (the list) | Not checked | Not checked |
| 5. Whole run | Read-only, no writes sent | vs the first snapshot (1,813 cards): 1 changed (My Card), 2 added (the throwaway contacts), 0 removed | | |

List card after each step: CRLF line endings, in the order `BEGIN, VERSION, PRODID, N, FN, X-ADDRESSBOOKSERVER-KIND, X-ADDRESSBOOKSERVER-MEMBER, REV, UID, END`. iCloud stored the card as sent (`PRODID:-//icloud-mcp//Contacts lists//EN` kept). In step 2, every line other than the member lines, `FN`, `N` and `REV` stayed byte-identical.

## My Card in the list view is iOS display only

The user's screenshot of "ZZ MCP List Test" showed two rows: My Card and "ZZ MCP List Test 1". Read-only checks show My Card is not a member:
- The list card has exactly one `X-ADDRESSBOOKSERVER-MEMBER` line. After step 2 it names throwaway contact 2's UID (before it, contact 1's).
- The server's me-card property identifies the user's own card, and its UID is not on the list.
- `contacts_groups` reports `member_count` 1, and `contacts_list(group_id=…)` returns only the throwaway contact.
- On the iPhone, the Lists screen counts "ZZ MCP List Test" as 1. An existing list with count 1 ("MK Prep") also shows My Card above its one member, so iOS shows My Card at the top of every list.
- All 16 list counts on the iPhone's Lists screen equal `contacts_groups` exactly. This includes the throwaway list and lists with references to deleted people.

## Unexplained change to My Card

- **The diff:** the step 2 whole-book ETag diff showed the user's own card (My Card) changing, besides the throwaway list.
- **This process did not write it:** the guard logged exactly one write in step 2, a PUT with `If-Match` to the throwaway list. A PUT to an existing card that this run did not create is refused before it is sent. `update_group` writes only the list card's URL.
- **What changed and what didn't:**
  - The card's `REV` is unchanged (2026-09-17) and its `PRODID` is Apple's (iOS 27.0.1).
  - It contains no member lines.
  - Its ETag changed exactly once in the run (first snapshot to now). It then held steady over a 2-minute read-only watch (8 GETs at 15 s intervals, 03:31–03:33Z), even while lists were opened on the iPhone.
- **Possible causes, not proven:**
  - iOS or iCloud resaved My Card. During the iPhone check at about 03:28:41Z, a mis-scaled click opened My Card in view mode, without Edit, about 80 s before the change. The card shows iOS-managed sections ("Shared Name and Photo", "Siri found in …").
  - iCloud touched My Card in reaction to the list edit.
  - Another client wrote it.
- **Not recoverable:** the card's text before the change was not kept (the snapshot stores ETags only), so what changed cannot be shown.

## After the pause

The user chose to continue, with the iPhone left untouched during the writes. The rename and the delete each changed exactly the throwaway list card, and the list card's other lines stayed byte-identical through the rename. A read-only check right after step 5 found exactly one non-throwaway card differing from the baseline, and it is My Card. So My Card did not change again during the rename or the delete.

The two throwaway contacts are left for the user to delete. After the delete, `contacts_list` shows each of them in no list.

## Mac Contacts app

The Mac's Contacts app has 16 groups and 2,077 people (iCloud has 16 lists and 1,800 people). It still showed neither throwaway contact nor the list more than 2 minutes after step 1. A count-only comparison found:
- 15 group names in common;
- 1 group only on the Mac;
- 1 list only in iCloud (the throwaway list);
- 4 of the 15 common lists with different member counts.

The Mac copy is stale, so it cannot verify writes. The iPhone is the reference.
