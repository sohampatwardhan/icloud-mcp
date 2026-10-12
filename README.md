# iCloud MCP Server

[Model Context Protocol](https://modelcontextprotocol.io) server for iCloud. Gives Claude (Desktop, Code, claude.ai connectors) and any other MCP client full access to an iCloud account:

- **Calendar** (CalDAV): list/search/create/update/delete events, recurring events (RRULE), alerts (VALARM), IANA timezones, all-day events, iTIP invitations and cancellations by email.
- **Contacts** (CardDAV): list/search/get/create/update/delete covering the iOS/macOS person fields:
  - name parts (prefix, first, middle, last, suffix; renames keep them), phonetic and pronunciation names, nickname, maiden name;
  - company, department, phonetic company and the company-card flag;
  - phones and emails with their labels (mobile/home/work/custom) and structured addresses;
  - birthdays, anniversaries and other dates, websites, social profiles, related names (mother, spouse, …) and instant-message accounts;
  - read-only: the birthday in another calendar, ringtone and text tone;
  - Contacts lists (groups): see every list and who is in it, see which lists a person is in, and create, rename, add or remove people, and delete a list without deleting its people.

  Entries you send back unchanged are kept exactly as stored, and Apple data the server does not model (pronouns, photos, ringtones) survives every edit.
- **Mail** (IMAP/SMTP): folders, listing, server-side search, full messages with attachments, attachment download, sending (HTML, attachments, threaded replies), drafts, move/delete/read flags.

Runs in two modes with the same code:

| Mode | Transport | Typical use | Credentials |
|------|-----------|-------------|-------------|
| **Local ("offline")** | stdio | Claude Desktop / Claude Code launch it as a subprocess on your machine | `.env` or environment variables |
| **Server** | Streamable HTTP, stateless | Docker / Cloud Run / any host, many users | Per-request headers or HTTP Basic auth, env fallback |

## Tools

| Tool | Description |
|------|-------------|
| `calendar_list_calendars` | Calendars with IDs; Reminders lists are flagged `read_only` |
| `calendar_list_events` | Events in a date range, recurring series expanded per occurrence, sorted by start |
| `calendar_search_events` | Text search over summary/description/location |
| `calendar_create_event` | Create an event: timezone, all-day, `rrule`, `reminders` (minutes before start or `"09:00"` time of day), attendees (invitations sent by email) |
| `calendar_update_event` | Partial update; change timezone/recurrence/alerts; re-sends invitations when attendees change |
| `calendar_delete_event` | Delete and email cancellations to attendees |
| `contacts_list` / `contacts_search` | People one page at a time (`page_size` 50 by default, up to 200) as `{contacts, total, next_cursor}`. Entries are summaries (id, name, organization, first phone and email) unless `fields` names other fields or is `["all"]`. Search needs every word to match, in any order and ignoring case and accents, across names, organization, notes, emails, addresses, related names, instant-message usernames, social profiles and phone numbers (words of 3 or more digits, in any format); name matches come first. Contacts lists are skipped, and `contacts_list` also skips cards with no name, phone or email. `contacts_list` takes an optional `group_id` (an id from `contacts_groups`) to page one list's members; omit it for everyone. `fields` can include `groups`, and `["all"]` includes it |
| `contacts_groups` | Contacts lists (groups) by name as `{groups: [{id, name, member_count}], total}`. `member_count` counts people the way the iPhone does, skipping references to people who were deleted |
| `contacts_group_create` | Create a list, optionally with people given as contact ids. Returns `{id, name, member_count}`. Only the new list is written |
| `contacts_group_update` | Rename a list and/or add and remove people, given as contact ids. Returns `{id, name, member_count, added, removed, unchanged}` with people as contact ids. Only the list changes; people's own cards are never edited. If another device changes the list at the same moment, membership changes are applied again to the newer list; a rename is not, and asks you to retry |
| `contacts_group_delete` | Delete a list. The people in it are kept |
| `contacts_upcoming_dates` | People with a birthday or significant date (anniversary or custom-labelled) in the `days` days from `start_date` (default: today in `DEFAULT_TIMEZONE`), soonest first. Each entry has the occasions (the date, days away, and age or years when the year is stored), every social profile as a link to open it, and the first phone and email. Paged like `contacts_list` |
| `contacts_get` | One person with every field: name and name parts, phonetic/pronunciation names, nickname, maiden name, labelled phones and emails, structured addresses, organization, department, company flag, title, notes, birthday, significant dates, urls, social profiles, related names, instant messages, and read-only alternate birthday, ringtone, text tone and `groups` (the lists they are in, as `{id, name}`) |
| `contacts_create` / `contacts_update` / `contacts_delete` | Write contacts: every read field except the read-only ones. `groups` is one of those: change it with `contacts_group_update`, not `contacts_update`. Updates and deletes are conditional on the contact being unchanged since they read it, and refuse a Contacts list id (use the `contacts_group_*` tools). Create and update return the contact shaped like `contacts_get`. |
| `email_list_folders` | Folders with IMAP flags |
| `email_list_messages` | Newest messages of a folder with readable `body_text`, `unread`, `has_attachments` |
| `email_search` | Server-side IMAP search: `query`, `sender`, `recipient`, `subject`, `body`, `since`, `before`, `unread_only` (AND) |
| `email_get_message` / `email_get_messages` | Full message(s): headers, `body_text`, optional raw `body_html`, attachment list |
| `email_get_attachment` | Download an attachment: save to a local directory (stdio) or return it inline |
| `email_send` | Send mail: multiple recipients, CC/BCC, HTML with auto plain-text alternative, local file attachments, threaded reply (`reply_to_message_id`); copy stored in Sent |
| `email_save_draft` | Same inputs as `email_send` but stores the message in Drafts for the user to review and send from any mail client |
| `email_move` / `email_delete` | Move between folders; delete to Trash (`Deleted Messages`) or permanently |
| `email_mark_read` / `email_mark_unread` | Toggle `\Seen` |

Every tool carries MCP annotations (`readOnlyHint`, `destructiveHint`) so clients can ask for confirmation before writes. Errors are returned as MCP tool errors with actionable messages (e.g. wrong password vs. missing credentials).

### Behaviour worth knowing

- **IDs**: calendar/contact/event IDs are full URLs, pass them back verbatim. Email IDs are IMAP UIDs and are only valid inside the folder they came from.
- **Contact pages**: pass `next_cursor` back as `cursor` until it is `null` to see everyone. A cursor only continues the listing or search that produced it; a stale or foreign one asks you to start again.
- **Contacts cache**: each account's address book is kept in memory (keyed by a hash of its credentials, never written to disk), and every list or search first asks iCloud what changed since the last call (a CardDAV sync report). Answers stay current, and a warm call takes about 0.2 s instead of about 2 s for a full download. `CONTACTS_CACHE=false` turns it off; `CONTACTS_CACHE_MAX_ACCOUNTS` and `CONTACTS_CACHE_IDLE_SECONDS` bound memory on multi-user servers. With the cache off, `contacts_get` and `contacts_update` also look up which lists the person is in, asking iCloud only for the few properties lists need rather than every whole card.
- **Times**: pass `YYYY-MM-DDTHH:MM:SS` plus an IANA `timezone` (default: `DEFAULT_TIMEZONE`, which falls back to the machine's zone, then UTC) or `YYYY-MM-DD` for all-day events. Returned times are ISO 8601 with offset.
- **Recurring events**: `calendar_list_events` returns one entry per occurrence; update/delete apply to the whole series.
- **Email bodies** are converted from HTML to readable text and truncated to `EMAIL_BODY_MAX_CHARS` (20 000) so newsletters do not flood the model context. `full_html=true` returns the raw HTML too.
- **Attachments on disk** (`save_dir`, `attachment_paths`) are enabled by default in stdio mode and disabled in HTTP mode. Override with `ICLOUD_MCP_LOCAL_FILES` and restrict to a folder with `ICLOUD_MCP_LOCAL_FILES_ROOT`.
- **Rich text** that users paste into event notes/locations or contact notes comes back from iCloud as HTML; it is converted to Markdown so links and lists survive (`ICLOUD_HTML_MODE=markdown`, default), rendered to plain text (`text`) or passed through (`raw`).
- **Tool groups** can be switched off per instance with `ICLOUD_ENABLED_CATEGORIES=calendar,contacts,email` (e.g. a read-only mail agent gets `email` only).
- **Only iCloud hosts** are ever contacted with the account's credentials: calendar, event and contact URLs on any other host are rejected, and mail headers are validated against injection.
- **Outbound recipients** can be restricted with `EMAIL_SEND_ALLOWLIST=@mycompany.com,partner@example.com`; it applies to `email_send`, `email_save_draft` and calendar invitations, so a prompt-injected agent cannot mail data to arbitrary addresses.

### Example assistant instruction

Assistants pick tools better with a short hint. For birthday reminders, an instruction like this works:

> When I ask whose birthday it is (today, tomorrow or this week), call `contacts_upcoming_dates` with `start_date` set to my local date, and `days` set to 1 for one day or 7 for a week. For each person, show their name, the date, and their age when `years` is given. Then show their Snapchat and Instagram links from `social_links` as tappable links; if they have neither, show their phone number. Only call `contacts_get` if I ask for more about someone.

> When I ask about a Contacts list, call `contacts_groups`, then `contacts_list` with that list's id as `group_id`. Add or remove people with `contacts_group_update`, using contact ids from `contacts_list` or `contacts_search`. Deleting a list with `contacts_group_delete` keeps the people.

## Requirements

- Python 3.11+ (tested on 3.12 and 3.14) or Docker
- An iCloud account and an **app-specific password**: <https://account.apple.com/account/manage> → Sign-In and Security → App-Specific Passwords. iCloud Mail additionally needs an active `@icloud.com` / `@me.com` / `@mac.com` address.

## Local mode (Claude Desktop / Claude Code)

### 1. Install

```bash
git clone https://github.com/mike-tih/icloud-mcp.git
cd icloud-mcp
uv venv && uv pip install -e .        # or: python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env                  # add ICLOUD_EMAIL / ICLOUD_APP_SPECIFIC_PASSWORD
```

Try it:

```bash
.venv/bin/icloud-mcp --help
```

### 2. Claude Code

```bash
claude mcp add icloud -- /absolute/path/to/icloud-mcp/.venv/bin/icloud-mcp
```

or, without a checkout at all:

```bash
claude mcp add icloud -e ICLOUD_EMAIL=you@icloud.com -e ICLOUD_APP_SPECIFIC_PASSWORD=xxxx-xxxx-xxxx-xxxx \
  -- uvx --from git+https://github.com/mike-tih/icloud-mcp icloud-mcp
```

### 3. Claude Desktop

Config file: macOS `~/Library/Application Support/Claude/claude_desktop_config.json`, Windows `%APPDATA%\Claude\claude_desktop_config.json`.

```json
{
  "mcpServers": {
    "icloud": {
      "command": "/absolute/path/to/icloud-mcp/.venv/bin/icloud-mcp",
      "env": {
        "ICLOUD_EMAIL": "you@icloud.com",
        "ICLOUD_APP_SPECIFIC_PASSWORD": "xxxx-xxxx-xxxx-xxxx",
        "DEFAULT_TIMEZONE": "Europe/Berlin"
      }
    }
  }
}
```

`python /absolute/path/to/icloud-mcp/run.py` works as the command too. Restart Claude Desktop completely after editing the file; the server shows up under the tools icon.

## Server mode (Streamable HTTP)

```bash
icloud-mcp --http                      # 0.0.0.0:8000/mcp, stateless
icloud-mcp --http --port 9000 --path /icloud --stateful
```

Or with Docker:

```bash
docker compose up -d                   # reads .env for optional fallback credentials
curl http://localhost:8000/health
```

The image runs `icloud-mcp --http` with `PORT` from the environment, so it works unchanged on Cloud Run, Fly.io, Railway etc. The server is **stateless**: every request carries its own credentials, no sessions are kept, and instances can be scaled horizontally.

### Authentication (per request)

Checked in order:

1. Headers `X-Apple-Email` and `X-Apple-App-Specific-Password`
2. `Authorization: Basic base64(email:app-specific-password)`
3. Environment `ICLOUD_EMAIL` / `ICLOUD_APP_SPECIFIC_PASSWORD` (single-user deployments)

### Protecting the endpoint

Set `MCP_AUTH_TOKEN` to a long random secret. Every MCP request must then carry it as `Authorization: Bearer <token>` or `X-MCP-Token: <token>` (`/health` stays open). Without a token the server logs a warning and **ignores the environment credentials over HTTP**, so an unprotected port can never hand out the operator's account; per-request credentials still work. Set `ICLOUD_MCP_ALLOW_ENV_CREDENTIALS=true` only if you really want an open endpoint bound to one account (e.g. on a private network). `docker-compose.yml` publishes the port on `127.0.0.1` only.

```bash
MCP_AUTH_TOKEN=$(openssl rand -hex 32) icloud-mcp --http
claude mcp add --transport http icloud https://mcp.example.com/mcp \
  -H "Authorization: Bearer <token>" \
  -H "X-Apple-Email: you@icloud.com" -H "X-Apple-App-Specific-Password: xxxx-xxxx-xxxx-xxxx"
```

Example with Claude Code against a remote server:

```bash
claude mcp add --transport http icloud https://mcp.example.com/mcp \
  -H "X-Apple-Email: you@icloud.com" -H "X-Apple-App-Specific-Password: xxxx-xxxx-xxxx-xxxx"
```

Always put the server behind HTTPS: app-specific passwords travel in headers.

## Configuration

All settings are environment variables (a `.env` file next to the checkout is loaded). See [`.env.example`](.env.example) for the full list. The important ones:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ICLOUD_EMAIL`, `ICLOUD_APP_SPECIFIC_PASSWORD` | – | Fallback credentials |
| `DEFAULT_TIMEZONE` | machine zone or `UTC` | Timezone for naive event times; set explicitly on servers |
| `EMAIL_BODY_MAX_CHARS` | `20000` | Body truncation |
| `EMAIL_MAX_ATTACHMENT_BYTES` | `20971520` | Outgoing attachment budget |
| `EMAIL_SEND_ALLOWLIST` | – | Allowed outbound addresses/domains (send, drafts, invitations) |
| `ICLOUD_MCP_LOCAL_FILES` | stdio: on, HTTP: off | Allow reading/writing attachments on the server's disk |
| `ICLOUD_MCP_LOCAL_FILES_ROOT` | – | Confine those files to a directory |
| `MCP_AUTH_TOKEN` | – | Shared secret required on HTTP requests |
| `ICLOUD_MCP_ALLOW_ENV_CREDENTIALS` | true with token, else false | Serve the env account over HTTP |
| `ICLOUD_ENABLED_CATEGORIES` | `calendar,contacts,email` | Tool groups to expose |
| `ICLOUD_HTML_MODE` | `markdown` | Rich text in event/contact fields: `markdown`, `text` or `raw` |
| `CONTACTS_CACHE` | `true` | Keep address books in memory and check iCloud for changes on each call; `false` downloads the whole book on every list or search |
| `CONTACTS_CACHE_MAX_ACCOUNTS` | `8` | Most accounts cached at once; the least recently used is dropped first |
| `CONTACTS_CACHE_IDLE_SECONDS` | `1800` | Drop an account's cached contacts after this long unused |
| `MCP_TRANSPORT`, `PORT`, `MCP_SERVER_HOST`, `MCP_SERVER_PATH` | stdio, `8000`, `0.0.0.0`, `/mcp` | HTTP transport |
| `LOG_LEVEL` | `INFO` | Logging (always to stderr, stdout is reserved for stdio) |

## Troubleshooting

- **"Authentication required"**: no credentials reached the server. Check the `env` block / headers.
- **"iCloud rejected the credentials" / HTTP 401**: wrong app-specific password, or the Apple ID password was used.
- **"IMAP login failed"** but calendar works: the Apple ID has no iCloud Mail address (Apple IDs created with a third-party email cannot use iCloud Mail).
- **Events land at the wrong time**: set `DEFAULT_TIMEZONE` or pass `timezone` explicitly.
- **HTTP 401 `unauthorized`** from the server itself: `MCP_AUTH_TOKEN` is set and the request did not carry it.
- **"Refusing to use ... URL on untrusted host"**: an ID was not one returned by the list tools; pass the URL verbatim.
- **Server logs**: everything goes to stderr; in Claude Desktop see Help → Show Logs.

## Development

```bash
uv pip install -e ".[dev]"
pytest              # unit tests + end-to-end through the MCP protocol with mocked IMAP
ruff check src tests
```

Project layout:

```
src/icloud_mcp/
├── server.py      # FastMCP app, tool definitions, CLI entrypoint
├── auth.py        # per-request credential resolution
├── config.py      # environment configuration and logging
├── calendar.py    # CalDAV operations, RRULE/timezone handling, iTIP mail
├── contacts.py    # CardDAV operations
├── mail.py        # IMAP/SMTP operations
├── mail_utils.py  # MIME parsing, HTML→text, attachments, special folders, header validation
├── html_render.py # HTML→Markdown for rich text in event/contact fields
└── urls.py        # iCloud host allow-list for calendar/contact URLs
```

## License

MIT, see [LICENSE](LICENSE).
