# Device Check: Throwaway Contact Through the MCP Server

One throwaway contact was created and then updated through the real MCP server over stdio, with only the contacts tools enabled and no delete call. It was checked on the user's iPhone (iOS 27.0.1) through iPhone Mirroring. A whole-address-book ETag comparison after each write showed that only this card changed: 1 new card after the create, and 1 changed card, this one, after the update. Opening and cancelling the iPhone's Edit view left the card unchanged. The card is left in iCloud for the user to delete.

## After creation

| Field | Sent | Stored line | iPhone shows |
|---|---|---|---|
| Name parts | Dr. / ZZ MCP / Q / Test / Jr. | `N:Test;ZZ MCP;Q;Dr.;Jr.`, `FN:Dr. ZZ MCP Q Test Jr.` | "Dr. ZZ MCP Q Test Jr."; Edit lists each part in its own field |
| Phonetic names | zee-zee em-see-pee / kyoo / tehst | `X-PHONETIC-FIRST/MIDDLE/LAST-NAME` | Edit: under the matching name parts |
| Pronunciation names | zee zee / test | `X-PRONUNCIATION-FIRST/LAST-NAME` | Edit: "zee zee" after the phonetic first name, "test" after the phonetic last name |
| Nickname | Zed | `NICKNAME:Zed` | card title "Zed" |
| Maiden name | Example | `X-MAIDENNAME:Example` | subtitle "Example" |
| Organization, department, phonetic company | MCP Test Co / QA Lab / em-see-pee | `ORG:MCP Test Co;QA Lab`, `X-PHONETIC-ORG:em-see-pee` | listed as "MCP Test Co"; subtitle "em-see-pee", "Test Engineer - QA Lab" |
| Company flag | true | `X-ABSHOWAS:COMPANY` | building icon in search and in Edit |
| Related names | mother: Mary Example; Climbing buddy: Sam Example | `X-ABRELATEDNAMES` + `X-ABLabel` (`_$!<Mother>!$_`, custom) | "mother Mary Example", "Climbing buddy Sam Example" |
| Instant messages | Skype zz.mcp.test (work); Signal +15550100199 | `IMPP;TYPE=WORK;X-SERVICE-TYPE=Skype:skype:zz.mcp.test`, `IMPP;TYPE=OTHER;X-SERVICE-TYPE=Signal:x-apple:+15550100199` | "Skype zz.mcp.test", "Signal +15550100199" |

## After the update

| Change | Stored result | iPhone shows |
|---|---|---|
| Rename with only a display name ("Dr. ZZ MCP Q Renamed Jr.") | `N:Renamed;ZZ MCP;Q;Dr.;Jr.`; prefix, middle name and suffix kept | title "Dr. ZZ MCP Q Renamed Jr." |
| Relabel Sam to friend | `item3.X-ABLabel:_$!<Friend>!$_`; the old custom label removed | "friend Sam Example" |
| Remove the Signal account | the Signal `IMPP` line removed; Skype unchanged | only "Skype zz.mcp.test" |
| Clear the nickname | `NICKNAME` removed | the title is the full name instead of "Zed" |
| Change the department | `ORG:MCP Test Co;Field Ops` | "Test Engineer - Field Ops • MCP Test Co" |
| `is_company` false | `X-ABSHOWAS` removed | a person card (initials "ZR"); the company shown in the subtitle |
| Unchanged fields | identical lines | phonetic names, maiden name, mother, Skype and title as before |

The server's own read-back through `contacts_get` matched the intended values after both writes.
