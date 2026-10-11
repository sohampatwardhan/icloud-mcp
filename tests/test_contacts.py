import vobject

from icloud_mcp import contacts

VCARD = """BEGIN:VCARD
VERSION:3.0
FN:Jane Doe
N:Doe;Jane;;;
TEL;TYPE=HOME,VOICE:+1 (555) 010-1234
TEL;TYPE=WORK:+1 555 010 9999
EMAIL;TYPE=INTERNET,HOME:jane@example.com
NOTE:Met at <b>conf</b><br>2025
END:VCARD"""


def test_replace_entries_preserves_labels():
    vcard = vobject.readOne(VCARD)
    contacts._apply_fields(vcard, ["+15550101234", "+1 555 000 0000"], None, None, None, None)
    tels = {t.value: t.params for t in vcard.tel_list}
    assert tels["+15550101234"]["TYPE"] == ["HOME", "VOICE"]  # same number, label kept
    assert tels["+1 555 000 0000"]["TYPE"] == ["CELL"]  # new number, default label
    assert len(vcard.tel_list) == 2


def test_apply_notes_and_read_back(monkeypatch):
    monkeypatch.setattr(contacts.config, "HTML_MODE", "text")
    vcard = vobject.readOne(VCARD)
    data = contacts._vcard_to_dict(vcard, "https://p1-contacts.icloud.com/c.vcf")
    assert data["notes"] == "Met at conf\n2025"
    assert data["phones"] == ["+1 (555) 010-1234", "+1 555 010 9999"]
    contacts._apply_fields(vcard, None, None, None, None, None, notes="plain")
    assert vcard.note.value == "plain"
    contacts._apply_fields(vcard, None, None, None, None, None, notes="")
    assert vcard.note.value == ""


VCARD_RICH = """BEGIN:VCARD
VERSION:3.0
FN:Marty Dalton
N:Dalton;Marty;;;
BDAY;X-APPLE-OMIT-YEAR=1604:1604-06-09
item1.X-SOCIALPROFILE;type=Instagram;x-user=martygdalton:https://instagram.com/martygdalton
item1.X-ABLabel:Instagram
URL;type=Homepage:https://martydalton.example.com
END:VCARD"""

VCARD_BDAY_YEAR = """BEGIN:VCARD
VERSION:3.0
FN:Jane Doe
BDAY:1990-01-15
END:VCARD"""


def test_parse_birthday_with_year():
    vcard = vobject.readOne(VCARD_BDAY_YEAR)
    data = contacts._vcard_to_dict(vcard, "https://x/c.vcf")
    assert data["birthday"] == "1990-01-15"


def test_parse_birthday_omit_year():
    vcard = vobject.readOne(VCARD_RICH)
    data = contacts._vcard_to_dict(vcard, "https://x/c.vcf")
    assert data["birthday"] == "--06-09"


def test_parse_social_profiles_and_urls():
    vcard = vobject.readOne(VCARD_RICH)
    data = contacts._vcard_to_dict(vcard, "https://x/c.vcf")
    assert data["social_profiles"] == [
        {"service": "Instagram", "username": "martygdalton",
         "url": "https://instagram.com/martygdalton"}
    ]
    assert data["urls"] == [
        {"url": "https://martydalton.example.com", "label": "Homepage"}
    ]


def test_parse_missing_birthday_and_socials():
    vcard = vobject.readOne(VCARD)
    data = contacts._vcard_to_dict(vcard, "https://x/c.vcf")
    assert data["birthday"] == ""
    assert data["social_profiles"] == []
    assert data["urls"] == []


def test_set_birthday_round_trip():
    vcard = vobject.readOne(VCARD)
    contacts._apply_fields(vcard, None, None, None, None, None,
                           birthday="1988-12-03")
    assert contacts._parse_birthday(vcard) == "1988-12-03"
    assert "BDAY:1988-12-03" in vcard.serialize()


def test_set_birthday_no_year_uses_apple_convention():
    vcard = vobject.readOne(VCARD)
    contacts._apply_fields(vcard, None, None, None, None, None,
                           birthday="--04-22")
    assert contacts._parse_birthday(vcard) == "--04-22"
    serialized = vcard.serialize()
    assert "X-APPLE-OMIT-YEAR=1604" in serialized
    assert "1604-04-22" in serialized


def test_set_birthday_invalid_raises():
    vcard = vobject.readOne(VCARD)
    try:
        contacts._apply_fields(vcard, None, None, None, None, None,
                               birthday="not-a-date")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_set_social_profiles_and_urls_round_trip():
    vcard = vobject.readOne(VCARD_RICH)
    contacts._apply_fields(
        vcard, None, None, None, None, None,
        urls=[{"url": "https://example.com"}],
        social_profiles=[
            {"service": "LinkedIn", "username": "martyd",
             "url": "https://linkedin.com/in/martyd"},
            {"service": "Mastodon", "username": "",
             "url": "https://mastodon.social/@martyd"},
        ],
    )
    data = contacts._vcard_to_dict(vcard, "https://x/c.vcf")
    assert data["urls"] == [{"url": "https://example.com", "label": ""}]
    assert data["social_profiles"] == [
        {"service": "LinkedIn", "username": "martyd",
         "url": "https://linkedin.com/in/martyd"},
        {"service": "Mastodon", "username": "",
         "url": "https://mastodon.social/@martyd"},
    ]
    # old Instagram entry was replaced, not duplicated
    assert "instagram.com" not in vcard.serialize()


VCARD_GROUPED = """BEGIN:VCARD
VERSION:3.0
FN:Jane Doe
N:Doe;Jane;;;
item1.TEL;type=pref:+1 555 111 2222
item1.X-ABLabel:Gym buddy line
TEL;type=CELL;type=VOICE:+1 555 333 4444
item2.URL;type=pref:https://jane.example.com
item2.X-ABLabel:_$!<HomePage>!$_
item3.X-SOCIALPROFILE;type=twitter;x-user=jane:https://twitter.com/jane
item3.X-ABLabel:Twitter
item4.ADR;type=HOME:;;1 Main St;Boston;MA;02110;USA
item4.X-ABADR:us
END:VCARD"""


def _labels(vcard):
    return {child.group: child.value for child in vcard.getChildren() if child.name == "X-ABLABEL"}


def test_url_label_read_from_ablabel_not_pref():
    vcard = vobject.readOne(VCARD_GROUPED)
    data = contacts._vcard_to_dict(vcard, "https://x/c.vcf")
    assert data["urls"] == [{"url": "https://jane.example.com", "label": "HomePage"}]


def test_unchanged_phone_keeps_custom_label_group():
    vcard = vobject.readOne(VCARD_GROUPED)
    contacts._apply_fields(vcard, ["+15551112222"], None, None, None, None)
    (tel,) = vcard.tel_list
    assert tel.group == "item1"
    assert _labels(vcard)["item1"] == "Gym buddy line"
    assert "item1.TEL" in vcard.serialize()


def test_removed_entries_drop_their_labels():
    vcard = vobject.readOne(VCARD_GROUPED)
    contacts._apply_fields(vcard, ["+1 555 333 4444"], None, [], None, None, urls=[], social_profiles=[])
    serialized = vcard.serialize()
    assert "X-ABLABEL" not in serialized
    assert "X-ABADR" not in serialized


def test_unchanged_url_without_label_keeps_label():
    vcard = vobject.readOne(VCARD_GROUPED)
    contacts._apply_fields(vcard, None, None, None, None, None,
                           urls=[{"url": "https://jane.example.com"}])
    assert contacts._parse_urls(vcard) == [{"url": "https://jane.example.com", "label": "HomePage"}]


def test_url_labels_written_apple_style():
    vcard = vobject.readOne(VCARD_GROUPED)
    contacts._apply_fields(vcard, None, None, None, None, None, urls=[
        {"url": "https://jane.example.com", "label": "Blog"},
        {"url": "https://work.example.com", "label": "work"},
        {"url": "https://home.example.com", "label": "homepage"},
    ])
    assert contacts._parse_urls(vcard) == [
        {"url": "https://jane.example.com", "label": "Blog"},
        {"url": "https://work.example.com", "label": "Work"},
        {"url": "https://home.example.com", "label": "HomePage"},
    ]
    labels = set(_labels(vcard).values())
    assert {"Blog", "_$!<Work>!$_", "_$!<HomePage>!$_", "Gym buddy line", "Twitter"} == labels
    reparsed = vobject.readOne(vcard.serialize())
    assert contacts._parse_urls(reparsed)[1]["label"] == "Work"


def test_unchanged_social_profile_keeps_group():
    vcard = vobject.readOne(VCARD_GROUPED)
    contacts._apply_fields(vcard, None, None, None, None, None, social_profiles=[
        {"service": "twitter", "username": "jane", "url": "https://twitter.com/jane"},
    ])
    (prop,) = vcard.x_socialprofile_list
    assert prop.group == "item3"
    assert _labels(vcard)["item3"] == "Twitter"


def test_echoing_read_values_back_changes_nothing():
    vcard = vobject.readOne(VCARD_GROUPED.replace("x-user=jane", "x-user=jane;x-userid=12345"))
    before = vcard.serialize()
    data = contacts._vcard_to_dict(vcard, "https://x/c.vcf")
    contacts._apply_fields(vcard, data["phones"], data["emails"], None, None, None,
                           urls=data["urls"], social_profiles=data["social_profiles"])
    assert vcard.serialize() == before
