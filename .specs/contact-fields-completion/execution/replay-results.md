# Snapshot Replay Results

Results from an in-memory replay over a fresh read-only snapshot of the user's live iCloud address book, using the code at `a37d922` and the harness [replay_snapshot.py](replay_snapshot.py). No contact was written. The table records counts only; the snapshot itself is kept outside the repository.

| Measure | Count |
|---|---:|
| IMPP without X-SERVICE-TYPE | 0 |
| ORG with 3+ components | 0 |
| add instant message: only its own lines | 1799 |
| add related name: only its own lines | 1799 |
| cards with Apple-only lines | 38 |
| clear nickname: only its own lines | 13 |
| company cards with empty organization | 0 |
| lists (skipped) | 15 |
| people | 1799 |
| rename (display name only): only its own lines | 1798 |
| set department: only its own lines | 1799 |
| set maiden name: only its own lines | 1799 |
| set middle name: only its own lines | 1799 |
| set nickname: only its own lines | 1799 |
| set phonetic first name: only its own lines | 1799 |
| set phonetic organization: only its own lines | 1799 |
| set prefix: only its own lines | 1799 |
| set pronunciation first name: only its own lines | 1799 |
| toggle is_company: only its own lines | 46 |
| unescaped commas in single-value text lines | 0 |
| write-back: unchanged | 1799 |

Each "only its own lines" row means the edit changed only that field's vCard lines, plus labels in the entry's own group, and that every pronoun, ringtone and other-calendar-birthday line stayed byte-identical. The rename count is 1 lower than the people count because one card has no name. The company-flag toggle runs only on cards that have an organization.
