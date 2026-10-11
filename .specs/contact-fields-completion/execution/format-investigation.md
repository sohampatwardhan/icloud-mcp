# Format Investigation: Apple-only Contact Fields

A throwaway card, **ZZ MCP Fields Test**, was created on the user's iPhone (iOS 27.0.1) through iPhone Mirroring. Its stored vCard was then read from iCloud over CardDAV, with a read-only GET. An ETag comparison of the whole address book before and after showed exactly one new card, with 0 changed and 0 removed.

## Ringtone

| What the iPhone shows | Stored line |
|---|---|
| Ringtone: Sound: Arpeggio | `X-ACTIVITY-ALERT:type=call\,snd=system:Arpeggio` |

- `type=call` marks the ringtone. `snd=system:<name>` is a built-in sound, and its name is exactly what Contacts shows.
- The one real card in the snapshot with a custom ringtone has an identifier-like `snd=` value that is not a `system:` name. That form cannot be turned into a display name without device data.
- **Decision:** readable when `snd` uses the `system:` scheme; omitted otherwise.

## Text tone, lunar birthday and verification code

Pending. iPhone Mirroring does not accept scroll input from computer use, so these fields, which sit lower on the edit form, could not be reached. The user is adding them to the same throwaway card by hand.
