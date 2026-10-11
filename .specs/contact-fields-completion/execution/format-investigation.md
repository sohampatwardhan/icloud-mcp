# Format Investigation: Apple-only Contact Fields

A throwaway card, **ZZ MCP Fields Test**, was created on the user's iPhone (iOS 27.0.1) through iPhone Mirroring. Its stored vCard was then read from iCloud over CardDAV, with a read-only GET. An ETag comparison of the whole address book before and after showed exactly one new card, with 0 changed and 0 removed.

## Ringtone

| What the iPhone shows | Stored line |
|---|---|
| Ringtone: Sound: Arpeggio | `X-ACTIVITY-ALERT:type=call\,snd=system:Arpeggio` |

- `type=call` marks the ringtone. `snd=system:<name>` is a built-in sound, and its name is exactly what Contacts shows.
- The one real card in the snapshot with a custom ringtone has an identifier-like `snd=` value that is not a `system:` name. That form cannot be turned into a display name without device data.
- **Decision:** readable when `snd` uses the `system:` scheme; omitted otherwise.

## Second round: fields added on the iPhone by the user

The user added the remaining fields to the same card on the iPhone. A second whole-book ETag comparison against the snapshot taken before this task showed 1 new card, 0 changed and 0 removed.

| Field (as the iPhone names it) | Stored line | Decision |
|---|---|---|
| Text Tone: Antic | `X-ACTIVITY-ALERT:type=text\,snd=texttone:Antic` | readable: `type=text` marks the text tone, and `snd=texttone:<name>` gives the shown name |
| Birthday in another calendar (Hebrew chosen; the picker also offers Chinese, Hijri, Korean and Vietnamese) | `X-ALTBDAY;CALSCALE=hebrew:--0130` | readable: the calendar comes from `CALSCALE`, and the value is `--MMDD` with no year (presumably `YYYYMMDD` when a year is stored) |
| Pronunciation first name | `X-PRONUNCIATION-FIRST-NAME:zzz` | readable plain text; added to scope by the user |
| Pronunciation last name | `X-PRONUNCIATION-LAST-NAME:macap` | readable plain text; added to scope by the user |
| Verification code | no line in the vCard | not observed: the field could not be saved on the device, so it is not exposed |

The user also set a Gregorian birthday, January 11, stored as `BDAY;value=date:1604-01-11`. It uses Apple's no-year sentinel but has no `X-APPLE-OMIT-YEAR` parameter, which shows the two birthday fields are independent. The existing birthday reader already treats year 1604 as "no year" and returns `--01-11`.
