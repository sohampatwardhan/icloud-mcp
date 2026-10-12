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
            r = type("R", (), {"text": "BEGIN:VCARD\nVERSION:3.0\nFN:Jo\nN:;Jo;;;\nEND:VCARD",
                               "headers": {"ETag": '"e1"'}, "raise_for_status": lambda self: None})()
            return r

        def put(self, url, data=None, headers=None, timeout=None):
            r = type("R", (), {})()
            def fail():
                raise requests.HTTPError(response=response)
            r.raise_for_status = fail
            return r

    monkeypatch.setattr(contacts_module, "require_auth", lambda: ("a@example.com", "pw"))
    monkeypatch.setattr(contacts_module, "_get_carddav_session", lambda e, p: Session())
    monkeypatch.setattr(contacts_module, "_default_addressbook_url", lambda s: "https://p1-contacts.icloud.com/ab/")
    updated = _call("contacts_update", contact_id="https://p1-contacts.icloud.com/ab/c.vcf", nickname="J")
    assert updated.is_error and "nothing was overwritten" in updated.content[0].text
    created = asyncio.run(_call_raw("contacts_create", {"name": "Jo"}))  # "name" clashes with _call
    assert created.is_error and "already exists" in created.content[0].text
