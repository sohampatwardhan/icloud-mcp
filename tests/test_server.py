"""End-to-end through the MCP protocol with the iCloud backends mocked out."""

import asyncio
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest
from fastmcp import Client

from icloud_mcp import mail as mail_module
from icloud_mcp import server
from icloud_mcp.auth import AuthenticationError
from icloud_mcp.config import config


class FakeIMAP:
    """Just enough of IMAPClient for the mail module."""

    def __init__(self, messages):
        self.messages = messages  # uid -> (flags, raw bytes)
        self.selected = None
        self.moved = []
        self.deleted = []
        self.flags_set = []

    def select_folder(self, folder, readonly=False):
        self.selected = folder

    def search(self, criteria, charset=None):
        self.last_criteria = criteria
        if criteria == ["UNSEEN"] or "UNSEEN" in criteria:
            return [uid for uid, (flags, _) in self.messages.items() if b"\\Seen" not in flags]
        return list(self.messages)

    def fetch(self, uids, fields):
        out = {}
        for uid in uids:
            if uid in self.messages:
                flags, raw = self.messages[uid]
                out[uid] = {b"FLAGS": flags, b"BODY[]": raw}
        return out

    def list_folders(self):
        return [
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\HasNoChildren", b"\\Sent"), b"/", "Sent Messages"),
            ((b"\\HasNoChildren", b"\\Trash"), b"/", "Deleted Messages"),
            ((b"\\HasNoChildren", b"\\Drafts"), b"/", "Drafts"),
        ]

    def find_special_folder(self, flag):
        return {b"\\Sent": "Sent Messages", b"\\Trash": "Deleted Messages", b"\\Drafts": "Drafts"}.get(flag)

    def has_capability(self, cap):
        return cap in {"MOVE", "UIDPLUS"}

    def move(self, uids, folder):
        self.moved.append((list(uids), folder))

    def delete_messages(self, uids):
        self.deleted.extend(uids)

    def uid_expunge(self, uids):
        pass

    def add_flags(self, uids, flags):
        self.flags_set.append(("add", list(uids), flags))

    def remove_flags(self, uids, flags):
        self.flags_set.append(("remove", list(uids), flags))

    def append(self, folder, raw, flags=None):
        self.appended = (folder, raw)

    def logout(self):
        pass


def _raw(subject, body, attach=False, seen=True):
    msg = MIMEMultipart("mixed")
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if attach:
        part = MIMEBase("image", "png")
        part.set_payload(b"\x89PNG fake")
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename="pic.png")
        msg.attach(part)
    msg["Subject"] = subject
    msg["From"] = "a@example.com"
    msg["To"] = "tester@icloud.com"
    msg["Message-ID"] = f"<{subject}@example.com>"
    flags = (b"\\Seen",) if seen else ()
    return flags, msg.as_bytes()


@pytest.fixture
def fake_imap(monkeypatch):
    fake = FakeIMAP({1: _raw("first", "hello one"), 2: _raw("second", "hello two", attach=True, seen=False)})
    monkeypatch.setattr(mail_module, "get_imap_client", lambda u, p: fake)
    monkeypatch.setattr(mail_module, "close_imap_client", lambda c: None)
    return fake


async def _call_raw(tool, arguments):
    async with Client(server.mcp) as client:
        return await client.call_tool(tool, arguments, raise_on_error=False)


def _call(name, **kwargs):
    async def run():
        async with Client(server.mcp) as client:
            return await client.call_tool(name, kwargs, raise_on_error=False)

    return asyncio.run(run())


def test_list_messages_newest_first(fake_imap):
    result = _call("email_list_messages", folder="INBOX", limit=10)
    items = result.structured_content["result"]
    assert [i["id"] for i in items] == ["2", "1"]
    assert items[0]["unread"] is True and items[0]["has_attachments"] is True
    assert items[0]["body_text"] == "hello two"


def test_unread_only(fake_imap):
    items = _call("email_list_messages", unread_only=True).structured_content["result"]
    assert [i["id"] for i in items] == ["2"]


def test_get_message_lists_attachments(fake_imap):
    data = _call("email_get_message", message_id="2").structured_content
    assert data["attachments"][0]["name"] == "pic.png"
    assert data["message_id_header"] == "<second@example.com>"


def test_get_attachment_inline_image(fake_imap, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_FILES", False)
    result = _call("email_get_attachment", message_id="2", attachment="pic.png")
    types = [type(c).__name__ for c in result.content]
    assert "ImageContent" in types
    assert any("pic.png" in getattr(c, "text", "") for c in result.content)


def test_get_attachment_save_dir(fake_imap, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_FILES", True)
    monkeypatch.setattr(config, "LOCAL_FILES_ROOT", None)
    result = _call("email_get_attachment", message_id="2", attachment="1", save_dir=str(tmp_path))
    assert (tmp_path / "pic.png").read_bytes() == b"\x89PNG fake"
    assert str(tmp_path / "pic.png") in result.content[0].text


def test_get_attachment_denied_when_local_files_off(fake_imap, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_FILES", False)
    result = _call("email_get_attachment", message_id="2", attachment="1", save_dir=str(tmp_path))
    assert result.is_error and "disabled" in result.content[0].text


def test_search_requires_a_filter(fake_imap):
    result = _call("email_search")
    assert result.is_error and "at least one filter" in result.content[0].text.lower()


def test_search_builds_and_criteria(fake_imap):
    _call("email_search", sender="a@example.com", subject="first", since="2025-01-01")
    crit = fake_imap.last_criteria
    assert crit[:4] == ["FROM", "a@example.com", "SUBJECT", "first"]
    assert crit[4] == "SINCE"


def test_delete_moves_to_trash(fake_imap):
    result = _call("email_delete", message_id="1").structured_content
    assert fake_imap.moved == [([1], "Deleted Messages")]
    assert "Deleted Messages" in result["message"]


def test_mark_unread(fake_imap):
    _call("email_mark_unread", message_id="1")
    assert fake_imap.flags_set == [("remove", [1], ["\\Seen"])]


def test_missing_message_is_tool_error(fake_imap):
    result = _call("email_get_message", message_id="99")
    assert result.is_error and "not found" in result.content[0].text


def test_auth_error_is_tool_error(monkeypatch):
    def boom():
        raise AuthenticationError("no creds")

    monkeypatch.setattr(mail_module, "require_auth", boom)
    result = _call("email_list_folders")
    assert result.is_error and "Authentication required" in result.content[0].text


def test_send_message(fake_imap, monkeypatch):
    sent = {}

    class FakeSMTP:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send_message(self, msg, from_addr, to_addrs):
            sent["msg"] = msg
            sent["to"] = to_addrs

    monkeypatch.setattr(mail_module, "get_smtp_client", lambda u, p: FakeSMTP())
    result = _call(
        "email_send",
        to="x@example.com, y@example.com",
        subject="Hi",
        body="<p>Hello</p>",
        html=True,
        bcc="z@example.com",
        reply_to_message_id="1",
    ).structured_content
    assert sent["to"] == ["x@example.com", "y@example.com", "z@example.com"]
    assert sent["msg"]["In-Reply-To"] == "<first@example.com>"
    assert "Bcc" not in sent["msg"]
    assert result["saved_to_folder"] == "Sent Messages"
    assert fake_imap.appended[0] == "Sent Messages"


def test_instructions_and_annotations():
    async def run():
        async with Client(server.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
            return tools, client.instructions

    tools, instructions = asyncio.run(run())
    assert "iCloud MCP server" in (instructions or "")
    assert tools["email_delete"].annotations.destructive_hint is True
    assert tools["calendar_list_events"].annotations.read_only_hint is True
    assert "context" not in tools["calendar_list_events"].input_schema.get("properties", {})


def test_save_draft(fake_imap):
    result = _call(
        "email_save_draft", subject="Draft subj", body="text", to="x@example.com", reply_to_message_id="1"
    ).structured_content
    assert result["folder"] == "Drafts" and result["to"] == ["x@example.com"]
    folder, raw = fake_imap.appended
    assert folder == "Drafts"
    assert b"In-Reply-To: <first@example.com>" in raw


def test_send_rejects_header_injection(fake_imap, monkeypatch):
    called = []
    monkeypatch.setattr(mail_module, "get_smtp_client", lambda u, p: called.append(1))
    result = _call("email_send", to="x@example.com\r\nBcc: v@example.com", subject="s", body="b")
    assert result.is_error and "line breaks" in result.content[0].text
    result = _call("email_send", to="x@example.com", subject="s\nX: y", body="b")
    assert result.is_error
    assert not called


def test_foreign_event_url_is_rejected():
    result = _call("calendar_delete_event", event_id="https://evil.example/e.ics")
    assert result.is_error and "untrusted host" in result.content[0].text


# --- contact-fields-completion: task 2.2 (tool surface) -----------------------------------

NEW_CONTACT_KEYS = {
    "prefix", "first_name", "middle_name", "last_name", "suffix", "phonetic_first_name",
    "phonetic_middle_name", "phonetic_last_name", "pronunciation_first_name", "pronunciation_last_name",
    "nickname", "maiden_name", "department", "phonetic_organization", "is_company", "related_names",
    "instant_messages",
}
APPLE_ONLY = {"pronouns", "ringtone", "text_tone", "alternate_birthday", "lunar_birthday", "verification_code"}


def _contact_tools():
    async def run():
        async with Client(server.mcp) as client:
            return {t.name: t for t in await client.list_tools()}
    return asyncio.run(run())


def test_contact_tools_expose_new_fields_but_no_apple_only_writes():
    tools = _contact_tools()
    for name in ("contacts_create", "contacts_update"):
        props = set(tools[name].input_schema["properties"])
        assert NEW_CONTACT_KEYS <= props, NEW_CONTACT_KEYS - props
        assert not APPLE_ONLY & props
    assert "name" not in tools["contacts_create"].input_schema.get("required", [])


def test_contacts_update_rejects_apple_only_argument():
    result = _call("contacts_update", contact_id="https://p1-contacts.icloud.com/ab/c.vcf", pronouns="she/her")
    assert result.is_error and "pronouns" in result.content[0].text


def test_contacts_create_over_mcp_writes_new_fields(monkeypatch):
    from icloud_mcp import contacts as contacts_module

    puts = []

    class Session:
        def put(self, url, data=None, headers=None, timeout=None):
            puts.append(data.decode())
            return type("R", (), {"raise_for_status": lambda self: None})()

    monkeypatch.setattr(contacts_module, "require_auth", lambda: ("a@example.com", "pw"))
    monkeypatch.setattr(contacts_module, "_get_carddav_session", lambda e, p: Session())
    monkeypatch.setattr(contacts_module, "_default_addressbook_url", lambda s: "https://p1-contacts.icloud.com/ab/")
    result = _call("contacts_create", first_name="Ada", last_name="Lovelace", nickname="Countess",
                   department="Analytical Engines", organization="Babbage & Co", is_company=False,
                   related_names=[{"name": "Lord Byron", "label": "father"}],
                   instant_messages=[{"service": "Skype", "username": "ada.l"}])
    assert not result.is_error, result.content
    data = result.structured_content
    assert (data["first_name"], data["nickname"], data["department"]) == ("Ada", "Countess", "Analytical Engines")
    assert data["related_names"] == [{"label": "father", "name": "Lord Byron"}]
    assert data["instant_messages"] == [{"label": "other", "service": "Skype", "username": "ada.l"}]
    assert "FN:Ada Lovelace" in puts[-1]


def test_412_from_icloud_reaches_the_client_through_the_tools(monkeypatch):
    import requests

    from icloud_mcp import contacts as contacts_module

    response = requests.Response()
    response.status_code = 412

    class Session:
        def get(self, url, timeout=None):
            text = "BEGIN:VCARD\nVERSION:3.0\nFN:Jo\nN:;Jo;;;\nEND:VCARD"
            r = type("R", (), {"text": text, "content": text.encode(),
                               "headers": {"ETag": '"e1"'}, "raise_for_status": lambda self: None})()
            return r

        def delete(self, url, headers=None, timeout=None):
            return self.put(url)

        def put(self, url, data=None, headers=None, timeout=None):
            r = type("R", (), {})()
            def fail():
                raise requests.HTTPError(response=response)
            r.raise_for_status = fail
            return r

    monkeypatch.setattr(contacts_module, "require_auth", lambda: ("a@example.com", "pw"))
    monkeypatch.setattr(contacts_module, "_get_carddav_session", lambda e, p: Session())
    monkeypatch.setattr(contacts_module, "_default_addressbook_url", lambda s: "https://p1-contacts.icloud.com/ab/")
    monkeypatch.setattr(contacts_module, "_lists_of", lambda *args: [])
    updated = _call("contacts_update", contact_id="https://p1-contacts.icloud.com/ab/c.vcf", nickname="J")
    assert updated.is_error and "nothing was overwritten" in updated.content[0].text
    created = asyncio.run(_call_raw("contacts_create", {"name": "Jo"}))  # "name" clashes with _call
    assert created.is_error and "already exists" in created.content[0].text
    deleted = _call("contacts_delete", contact_id="https://p1-contacts.icloud.com/ab/c.vcf")
    assert deleted.is_error and "nothing was deleted" in deleted.content[0].text


# --- contacts paging (spec: .specs/contacts-list-search-scale, task 2.4) -----------------------

# Digests of the input schema, output schema and description of the contact tools this
# feature must leave alone (requirements 5.1 and 5.2), taken before it changed server.py.
# If one of these tools is changed on purpose later, update its digest.
UNCHANGED_CONTACT_TOOLS = {
    # Re-pinned by the pre-merge review of the contacts PRs: contacts_create and contacts_update
    # return groups, contacts_update and contacts_delete refuse list ids, contacts_list skips
    # cards with no name, phone or email, and only phone-like search words match phone numbers.
    # Re-pinned 2026-10-12: addresses street accepts a list of street lines (multi-line ADR).
    "contacts_create": "a369a50bac1b95b7",
    "contacts_delete": "501b0abbb7218125",
    "contacts_search": "6b4a58ea847dbaec",
    "contacts_list": "3cd8b0209b3be326",
    "contacts_update": "5fac2dd0c39cce3b",
    # Re-pinned by .specs/contacts-lists: the contacts_get description names the read-only groups key.
    "contacts_get": "15e99b84c830bf36",
    # Pinned before the Contacts list tools were added (spec: .specs/contacts-lists).
    "contacts_upcoming_dates": "4061525bbb4f6c91",
}


def _schema_digest(tool):
    import hashlib
    import json

    blob = json.dumps({"input": tool.input_schema, "output": tool.output_schema, "description": tool.description},
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def test_other_contact_tools_keep_their_schemas():
    tools = _contact_tools()
    assert {name: _schema_digest(tools[name]) for name in UNCHANGED_CONTACT_TOOLS} == UNCHANGED_CONTACT_TOOLS


def test_list_and_search_take_page_size_cursor_and_fields_but_no_limit():
    tools = _contact_tools()
    for name in ("contacts_list", "contacts_search"):
        props = tools[name].input_schema["properties"]
        assert "limit" not in props
        assert (props["page_size"]["minimum"], props["page_size"]["maximum"], props["page_size"]["default"]) == (1, 200, 50)
        assert {"cursor", "fields"} <= set(props)
    assert set(tools["contacts_list"].input_schema["properties"]) == {"page_size", "cursor", "fields", "group_id"}
    assert tools["contacts_search"].input_schema["required"] == ["query"]


def test_bad_paging_arguments_are_rejected_before_the_call(monkeypatch):
    from icloud_mcp import contacts as contacts_module

    calls = []
    monkeypatch.setattr(contacts_module, "list_contacts", lambda *args: calls.append(args))
    for arguments in ({"page_size": 0}, {"page_size": 201}, {"limit": 5}):
        assert asyncio.run(_call_raw("contacts_list", arguments)).is_error, arguments
    assert calls == []


def test_paged_list_and_search_through_the_protocol(monkeypatch):
    from icloud_mcp import contacts as contacts_module

    cards = [contacts_module.build_card(f"https://p1-contacts.icloud.com/ab/{i}.vcf", None,
                                        f"BEGIN:VCARD\nVERSION:3.0\nFN:{name}\nN:;{name};;;\nTEL:+1555010000{i}\nEND:VCARD\n")
             for i, name in enumerate(["José García", "Ana Ruiz", "Bo Chen"])]
    monkeypatch.setattr(contacts_module, "require_auth", lambda: ("a@example.com", "pw"))
    monkeypatch.setattr(contacts_module, "_all_cards", lambda session, credentials: cards)

    first = _call("contacts_list", page_size=2)
    assert not first.is_error, first.content
    assert set(first.structured_content) == {"contacts", "total", "next_cursor"}
    assert [c["name"] for c in first.structured_content["contacts"]] == ["Ana Ruiz", "Bo Chen"]
    assert set(first.structured_content["contacts"][0]) == {"id", "name", "organization", "phone", "email"}
    rest = _call("contacts_list", page_size=2, cursor=first.structured_content["next_cursor"])
    assert [c["name"] for c in rest.structured_content["contacts"]] == ["José García"]
    assert rest.structured_content["total"] == 3 and rest.structured_content["next_cursor"] is None

    found = _call("contacts_search", query="garcia JOSE", fields=["all"])
    assert not found.is_error, found.content
    (contact,) = found.structured_content["contacts"]
    assert contact == {**cards[0].contact, "groups": []}
    bad = _call("contacts_search", query="ana", cursor=first.structured_content["next_cursor"])
    assert bad.is_error and "start again without a cursor" in bad.content[0].text
    empty = _call("contacts_search", query="  ")
    assert empty.is_error and "query must not be empty" in empty.content[0].text



# --- contacts_upcoming_dates (spec: .specs/contacts-upcoming-dates, task 1.3) ------------------

def test_upcoming_dates_tool_schema_and_description():
    tool = _contact_tools()["contacts_upcoming_dates"]
    props = tool.input_schema["properties"]
    assert set(props) == {"start_date", "days", "page_size", "cursor", "fields"}
    assert (props["days"]["minimum"], props["days"]["maximum"], props["days"]["default"]) == (1, 366, 1)
    assert (props["page_size"]["minimum"], props["page_size"]["maximum"], props["page_size"]["default"]) == (1, 200, 50)
    assert tool.input_schema.get("required", []) == []
    for word in ("start_date", "days", "occasions", "social_links", "next_cursor"):
        assert word in tool.description, word
    assert "defaults to today" in tool.description and "days defaults to 1" in tool.description
    assert tool.annotations.read_only_hint is True


def test_upcoming_dates_out_of_range_arguments_are_rejected_before_the_call(monkeypatch):
    from icloud_mcp import contacts as contacts_module

    calls = []
    monkeypatch.setattr(contacts_module, "upcoming_dates", lambda *args: calls.append(args))
    for arguments in ({"days": 0}, {"days": 367}, {"page_size": 201}, {"limit": 5}):
        assert asyncio.run(_call_raw("contacts_upcoming_dates", arguments)).is_error, arguments
    assert calls == []


def test_upcoming_dates_through_the_protocol(monkeypatch):
    import datetime

    from icloud_mcp import contacts as contacts_module

    texts = ["BEGIN:VCARD\nVERSION:3.0\nFN:Sam Lee\nN:Lee;Sam;;;\nBDAY:--10-12\n"
             "X-SOCIALPROFILE;type=Snapchat;x-user=sam.lee:x-apple:sam.lee\nEND:VCARD\n",
             "BEGIN:VCARD\nVERSION:3.0\nFN:Ana Ruiz\nN:Ruiz;Ana;;;\nBDAY:1990-10-11\nTEL:+15550100000\nEND:VCARD\n"]
    cards = [contacts_module.build_card(f"https://p1-contacts.icloud.com/ab/{i}.vcf", None, t) for i, t in enumerate(texts)]
    monkeypatch.setattr(contacts_module, "require_auth", lambda: ("a@example.com", "pw"))
    monkeypatch.setattr(contacts_module, "_all_cards", lambda session, credentials: cards)
    monkeypatch.setattr(contacts_module, "_today", lambda: datetime.date(2026, 10, 11))

    result = _call("contacts_upcoming_dates", days=7)
    assert not result.is_error, result.content
    data = result.structured_content
    assert (data["start_date"], data["end_date"], data["total"], data["next_cursor"]) == ("2026-10-11", "2026-10-17", 2, None)
    ana, sam = data["contacts"]
    assert ana["occasions"] == [{"kind": "birthday", "date": "2026-10-11", "days_away": 0, "years": 36}]
    assert ana["phone"] == "+15550100000"
    assert sam["social_links"] == [{"service": "snapchat", "username": "sam.lee", "url": "https://snapchat.com/add/sam.lee"}]
    bad = _call("contacts_upcoming_dates", start_date="11/10/2026")
    assert bad.is_error and "start_date must be a real date" in bad.content[0].text


# --- Contacts lists (spec: .specs/contacts-lists, task 1.4) ------------------------------------

GROUP_TOOLS = ("contacts_groups", "contacts_group_create", "contacts_group_update", "contacts_group_delete")


def test_group_tools_schemas_annotations_and_descriptions():
    tools = _contact_tools()
    props = {name: tools[name].input_schema.get("properties", {}) for name in GROUP_TOOLS}
    assert props["contacts_groups"] == {}
    assert set(props["contacts_group_create"]) == {"name", "members"}
    assert set(props["contacts_group_update"]) == {"group_id", "name", "add", "remove"}
    assert set(props["contacts_group_delete"]) == {"group_id"}
    assert tools["contacts_group_create"].input_schema["required"] == ["name"]
    assert tools["contacts_group_update"].input_schema["required"] == ["group_id"]
    hints = {name: (tools[name].annotations.read_only_hint, tools[name].annotations.destructive_hint)
             for name in GROUP_TOOLS}
    assert hints == {"contacts_groups": (True, False), "contacts_group_create": (False, False),
                     "contacts_group_update": (False, True), "contacts_group_delete": (False, True)}
    for name in GROUP_TOOLS:
        assert "Contacts list" in tools[name].description and "(group" in tools[name].description, name
    for name in ("contacts_group_create", "contacts_group_update"):
        assert "contact ids" in tools[name].description, name
    for name, param in (("contacts_group_create", "members"), ("contacts_group_update", "add"),
                        ("contacts_group_update", "remove")):
        assert "contact ids" in props[name][param]["description"], param
    assert "people in it are kept" in tools["contacts_group_delete"].description
    assert "groups" in tools["contacts_get"].description and "read-only" in tools["contacts_get"].description
    assert "contacts_group_update" in tools["contacts_get"].description
    assert "text_tone, groups)" in tools["contacts_update"].description
    assert "group_id" in tools["contacts_list"].description


@pytest.fixture
def lists_server(monkeypatch):
    from fake_carddav import BOOK, vcard
    from fake_carddav import FakeCardDAV as CardDAVServer

    from icloud_mcp import contacts as contacts_module

    book = CardDAVServer()
    monkeypatch.setattr(contacts_module, "require_auth", lambda: ("jo@example.com", "app-pw"))
    monkeypatch.setattr(contacts_module, "_get_carddav_session", lambda email, password: book)
    monkeypatch.setattr(contacts_module, "_default_addressbook_url", lambda session: BOOK)
    ids = {"ann": book.put_card("ann", vcard("Ann Ames", "UID:AAA-1\n")),
           "bob": book.put_card("bob", vcard("Bob Brown", "UID:bbb-2\n")),
           "fam": book.put_card("fam", "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Family\r\nFN:Family\r\n"
                                       "X-ADDRESSBOOKSERVER-KIND:group\r\n"
                                       "X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:AAA-1\r\nUID:FAM\r\nEND:VCARD\r\n")}
    return book, ids


def test_group_tools_through_the_protocol(lists_server):
    book, ids = lists_server
    listed = _call("contacts_groups")
    assert not listed.is_error, listed.content
    assert listed.structured_content == {"groups": [{"id": ids["fam"], "name": "Family", "member_count": 1}], "total": 1}
    members = _call("contacts_list", group_id=ids["fam"], fields=["groups"])
    assert members.structured_content["contacts"] == [{"id": ids["ann"], "groups": [{"id": ids["fam"], "name": "Family"}]}]
    assert _call("contacts_get", contact_id=ids["ann"]).structured_content["groups"] == [{"id": ids["fam"], "name": "Family"}]

    created = asyncio.run(_call_raw("contacts_group_create", {"name": "Gym", "members": [ids["bob"]]}))
    assert not created.is_error, created.content
    gym = created.structured_content
    assert (gym["name"], gym["member_count"]) == ("Gym", 1)
    updated = asyncio.run(_call_raw("contacts_group_update", {"group_id": gym["id"], "name": "Gym crew",
                                                               "add": [ids["ann"]], "remove": [ids["bob"]]}))
    assert not updated.is_error, updated.content
    assert updated.structured_content == {"id": gym["id"], "name": "Gym crew", "member_count": 1,
                                          "added": [ids["ann"]], "removed": [ids["bob"]], "unchanged": []}
    deleted = _call("contacts_group_delete", group_id=gym["id"])
    assert not deleted.is_error and "members were not changed" in deleted.structured_content["message"]
    assert [method for method, _, _ in book.writes] == ["PUT", "PUT", "DELETE"]
    assert {url for _, url, _ in book.writes} == {gym["id"]}


def test_group_tool_errors_through_the_protocol(lists_server):
    book, ids = lists_server
    cases = [
        ("contacts_group_update", {"group_id": ids["ann"], "add": [ids["bob"]]}, "group not found"),
        ("contacts_group_update", {"group_id": ids["fam"]}, "nothing to change"),
        ("contacts_group_update", {"group_id": ids["fam"], "add": [ids["bob"]], "remove": [ids["bob"]]},
         "cannot both add and remove"),
        ("contacts_group_create", {"name": " "}, "must not be empty"),
        ("contacts_group_create", {"name": "family"}, "already exists"),
        ("contacts_group_delete", {"group_id": "https://evil.example.com/x.vcf"}, "untrusted host"),
        ("contacts_list", {"group_id": ids["bob"]}, "use contacts_groups for valid ids"),
    ]
    for tool, arguments, message in cases:
        result = asyncio.run(_call_raw(tool, arguments))
        assert result.is_error and message in result.content[0].text, (tool, arguments, result.content)
    book.before["put"] = [lambda: book.put_card("fam", book.cards[next(p for p in book.cards if p.endswith("fam.vcf"))][1])]
    renamed = asyncio.run(_call_raw("contacts_group_update", {"group_id": ids["fam"], "name": "Fam"}))
    assert renamed.is_error and "changed on another device" in renamed.content[0].text
    assert [method for method, _, _ in book.writes] == ["PUT"]


# --- optional proxy tunnel at startup ---------------------------------------------------------

def _start_stdio(monkeypatch):
    from icloud_mcp import server

    runs = []
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: runs.append(kwargs))
    monkeypatch.setattr(server.config, "LOCAL_FILES", None)
    server.main([])
    return runs


def test_main_starts_without_the_proxy_tunnel_module(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "icloud_mcp.proxy_tunnel", None)  # import fails as if absent
    assert _start_stdio(monkeypatch) == [{"transport": "stdio", "show_banner": False}]


def test_main_installs_the_proxy_tunnel_when_present(monkeypatch):
    import sys
    import types

    installed = []
    module = types.ModuleType("icloud_mcp.proxy_tunnel")
    module.install = lambda: installed.append(True)
    monkeypatch.setitem(sys.modules, "icloud_mcp.proxy_tunnel", module)
    assert _start_stdio(monkeypatch) and installed == [True]
