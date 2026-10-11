import base64
import json

import pytest
import vobject
from fake_carddav import BOOK, vcard
from fake_carddav import FakeCardDAV as CardDAVServer

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
    contacts._apply_fields(vcard, [{"value": "+15550101234"}, {"value": "+1 555 000 0000"}], None, None, None, None)
    tels = {t.value: t.params for t in vcard.tel_list}
    assert tels["+15550101234"]["TYPE"] == ["HOME", "VOICE"]  # same number reformatted, label kept
    assert tels["+1 555 000 0000"]["TYPE"] == ["CELL", "VOICE"]  # new number, default label mobile
    assert len(vcard.tel_list) == 2


def test_apply_notes_and_read_back(monkeypatch):
    monkeypatch.setattr(contacts.config, "HTML_MODE", "text")
    vcard = vobject.readOne(VCARD)
    data = contacts._vcard_to_dict(vcard, "https://p1-contacts.icloud.com/c.vcf")
    assert data["notes"] == "Met at conf\n2025"
    assert data["phones"] == [{"label": "home", "value": "+1 (555) 010-1234"},
                              {"label": "work", "value": "+1 555 010 9999"}]
    contacts._apply_fields(vcard, None, None, None, None, None, notes="plain")
    assert vcard.note.value == "plain"
    contacts._apply_fields(vcard, None, None, None, None, None, notes="")
    assert not hasattr(vcard, "note")  # "" clears the notes


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
        {"service": "instagram", "username": "martygdalton",
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
        {"service": "linkedin", "username": "martyd",
         "url": "https://linkedin.com/in/martyd"},
        {"service": "mastodon", "username": "",
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
    assert data["urls"] == [{"url": "https://jane.example.com", "label": "homepage"}]


def test_unchanged_phone_keeps_custom_label_group():
    vcard = vobject.readOne(VCARD_GROUPED)
    contacts._apply_fields(vcard, [{"value": "+15551112222"}], None, None, None, None)
    (tel,) = vcard.tel_list
    assert tel.group == "item1"
    assert _labels(vcard)["item1"] == "Gym buddy line"
    assert "item1.TEL" in vcard.serialize()


def test_removed_entries_drop_their_labels():
    vcard = vobject.readOne(VCARD_GROUPED)
    contacts._apply_fields(vcard, [{"value": "+1 555 333 4444"}], None, [], None, None, urls=[], social_profiles=[])
    serialized = vcard.serialize()
    assert "X-ABLABEL" not in serialized
    assert "X-ABADR" not in serialized


def test_unchanged_url_without_label_keeps_label():
    vcard = vobject.readOne(VCARD_GROUPED)
    contacts._apply_fields(vcard, None, None, None, None, None,
                           urls=[{"url": "https://jane.example.com"}])
    assert contacts._parse_urls(vcard) == [{"url": "https://jane.example.com", "label": "homepage"}]


def test_url_labels_written_apple_style():
    vcard = vobject.readOne(VCARD_GROUPED)
    contacts._apply_fields(vcard, None, None, None, None, None, urls=[
        {"url": "https://jane.example.com", "label": "Blog"},
        {"url": "https://work.example.com", "label": "work"},
        {"url": "https://home.example.com", "label": "homepage"},
    ])
    assert contacts._parse_urls(vcard) == [
        {"url": "https://jane.example.com", "label": "Blog"},
        {"url": "https://work.example.com", "label": "work"},
        {"url": "https://home.example.com", "label": "homepage"},
    ]
    labels = set(_labels(vcard).values())
    assert {"Blog", "_$!<Work>!$_", "_$!<HomePage>!$_", "Gym buddy line", "Twitter"} == labels
    reparsed = vobject.readOne(vcard.serialize())
    assert contacts._parse_urls(reparsed)[1]["label"] == "work"


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


VCARD_SOCIAL = """BEGIN:VCARD
VERSION:3.0
FN:Sam Lee
X-SOCIALPROFILE;type=Snapchat;x-user=samlee:https://snapchat.com/add/samlee
X-SOCIALPROFILE;type=linkedin:https://www.linkedin.com/in/sam-lee-42
X-SOCIALPROFILE;type=facebook;x-userid=100004:https://www.facebook.com/profile.php?id=100004
X-SOCIALPROFILE;type=WHATSAPP:x-apple:+15550101234
X-SOCIALPROFILE:https://twitter.com/samtweets
END:VCARD"""


def test_social_profiles_read_normalised():
    data = contacts._vcard_to_dict(vobject.readOne(VCARD_SOCIAL), "https://x/c.vcf")
    assert data["social_profiles"] == [
        {"service": "snapchat", "username": "samlee", "url": "https://snapchat.com/add/samlee"},
        {"service": "linkedin", "username": "sam-lee-42", "url": "https://www.linkedin.com/in/sam-lee-42"},
        {"service": "facebook", "username": "", "url": "https://www.facebook.com/profile.php?id=100004",
         "user_id": "100004"},
        {"service": "whatsapp", "username": "+15550101234", "url": "x-apple:+15550101234"},
        {"service": "twitter", "username": "samtweets", "url": "https://twitter.com/samtweets"},
    ]


def test_social_profiles_echo_is_verbatim():
    vcard = vobject.readOne(VCARD_SOCIAL)
    before = vcard.serialize()
    data = contacts._vcard_to_dict(vcard, "https://x/c.vcf")
    contacts._apply_fields(vcard, None, None, None, None, None, social_profiles=data["social_profiles"])
    assert vcard.serialize() == before


def test_social_profile_add_from_username_only():
    vcard = vobject.readOne(VCARD_SOCIAL)
    profiles = contacts._parse_social_profiles(vcard) + [
        {"service": "Instagram", "username": "@sam.lee"},
        {"service": "x", "username": "samx"},
        {"service": "Signal", "username": "+15550109999"},
    ]
    contacts._apply_fields(vcard, None, None, None, None, None, social_profiles=profiles)
    added = contacts._parse_social_profiles(vcard)[-3:]
    assert added == [
        {"service": "instagram", "username": "sam.lee", "url": "https://www.instagram.com/sam.lee/"},
        {"service": "twitter", "username": "samx", "url": "https://twitter.com/samx"},
        {"service": "signal", "username": "+15550109999", "url": "x-apple:+15550109999"},
    ]
    assert "X-SOCIALPROFILE;TYPE=Instagram;X-USER=sam.lee:https://www.instagram.com/sam.lee/" in vcard.serialize().replace("\r\n ", "")
    assert len(vcard.x_socialprofile_list) == 8


def test_social_profile_change_and_remove():
    vcard = vobject.readOne(VCARD_SOCIAL)
    profiles = contacts._parse_social_profiles(vcard)
    profiles[2]["url"] = "https://www.facebook.com/sam.lee.fb"  # new URL, same account id
    del profiles[0]  # drop Snapchat
    contacts._apply_fields(vcard, None, None, None, None, None, social_profiles=profiles)
    result = contacts._parse_social_profiles(vcard)
    assert [p["service"] for p in result] == ["linkedin", "facebook", "whatsapp", "twitter"]
    assert result[1] == {"service": "facebook", "username": "sam.lee.fb",
                         "url": "https://www.facebook.com/sam.lee.fb", "user_id": "100004"}
    assert "snapchat" not in vcard.serialize().lower()


def test_social_profile_needs_url_or_username():
    vcard = vobject.readOne(VCARD_SOCIAL)
    try:
        contacts._apply_fields(vcard, None, None, None, None, None, social_profiles=[{"service": "instagram"}])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_search_matches_social_username(monkeypatch):
    (url,) = seed(monkeypatch, VCARD_SOCIAL)
    assert found("@samlee") == [url]
    assert found("sam-lee-42") == [url]
    assert found("@nobody") == []


def test_removing_one_of_two_profiles_sharing_a_value():
    vcard = vobject.readOne("""BEGIN:VCARD
VERSION:3.0
FN:Pat
X-SOCIALPROFILE;type=signal:x-apple:+15550101234
X-SOCIALPROFILE;type=whatsapp:x-apple:+15550101234
END:VCARD""")
    contacts._apply_fields(vcard, None, None, None, None, None,
                           social_profiles=contacts._parse_social_profiles(vcard)[1:])
    serialized = vcard.serialize()
    assert "X-SOCIALPROFILE;TYPE=whatsapp:x-apple:+15550101234" in serialized
    assert "signal" not in serialized


VCARD_DATES = """BEGIN:VCARD
VERSION:3.0
FN:Alex Kim
BDAY;X-APPLE-OMIT-YEAR=1604:1604-02-29
item1.X-ABDATE;type=pref:2015-06-20
item1.X-ABLabel:_$!<Anniversary>!$_
item2.X-ABDATE;X-APPLE-OMIT-YEAR=1604:1604-09-01
item2.X-ABLabel:First day at work
END:VCARD"""


def _unfolded(vcard):
    return vcard.serialize().replace("\r\n ", "")


def test_dates_read():
    data = contacts._vcard_to_dict(vobject.readOne(VCARD_DATES), "https://x/c.vcf")
    assert data["birthday"] == "--02-29"
    assert data["dates"] == [
        {"label": "anniversary", "date": "2015-06-20"},
        {"label": "First day at work", "date": "--09-01"},
    ]
    assert contacts._vcard_to_dict(vobject.readOne(VCARD), "https://x/c.vcf")["dates"] == []


def test_dates_echo_is_verbatim():
    vcard = vobject.readOne(VCARD_DATES)
    before = vcard.serialize()
    contacts._apply_fields(vcard, None, None, None, None, None, dates=contacts._parse_dates(vcard))
    assert vcard.serialize() == before


def test_dates_add_apple_style():
    vcard = vobject.readOne(VCARD)
    contacts._apply_fields(vcard, None, None, None, None, None, dates=[
        {"label": "anniversary", "date": "2019-05-04"},
        {"label": "Adopted Rex", "date": "--11-15"},
        {"date": "2001-01-01"},
    ])
    assert contacts._parse_dates(vcard) == [
        {"label": "anniversary", "date": "2019-05-04"},
        {"label": "Adopted Rex", "date": "--11-15"},
        {"label": "other", "date": "2001-01-01"},
    ]
    serialized = _unfolded(vcard)
    assert "item1.X-ABDATE:2019-05-04" in serialized
    assert "item1.X-ABLABEL:_$!<Anniversary>!$_" in serialized
    assert "item2.X-ABDATE;X-APPLE-OMIT-YEAR=1604:1604-11-15" in serialized
    assert "item3.X-ABLABEL:_$!<Other>!$_" in serialized


def test_dates_change_and_clear():
    vcard = vobject.readOne(VCARD_DATES)
    dates = contacts._parse_dates(vcard)
    dates[0]["date"] = "2016-06-20"
    contacts._apply_fields(vcard, None, None, None, None, None, dates=dates)
    assert contacts._parse_dates(vcard) == [
        {"label": "anniversary", "date": "2016-06-20"},
        {"label": "First day at work", "date": "--09-01"},
    ]
    assert len(vcard.x_ablabel_list) == 2  # the old anniversary label went with its date
    contacts._apply_fields(vcard, None, None, None, None, None, dates=[])
    assert "X-ABDATE" not in vcard.serialize()
    assert "X-ABLABEL" not in vcard.serialize()


def test_invalid_dates_raise():
    for field, value in (("birthday", "2023-02-30"), ("birthday", "--13-01"), ("dates", [{"date": "June 5"}])):
        vcard = vobject.readOne(VCARD)
        try:
            contacts._apply_fields(vcard, None, None, None, None, None, **{field: value})
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {field}={value!r}")


def test_birthday_change_keeps_property_and_toggles_year():
    vcard = vobject.readOne(VCARD_DATES)
    contacts._apply_fields(vcard, None, None, None, None, None, birthday="1990-02-28")
    assert "BDAY:1990-02-28" in vcard.serialize()
    contacts._apply_fields(vcard, None, None, None, None, None, birthday="--02-29")
    assert "BDAY;X-APPLE-OMIT-YEAR=1604:1604-02-29" in vcard.serialize()
    contacts._apply_fields(vcard, None, None, None, None, None, birthday="")
    assert "BDAY" not in vcard.serialize()


def test_serialize_adds_missing_fn():
    number_only = vobject.readOne("BEGIN:VCARD\nVERSION:3.0\nN:;;;;\nTEL:+15550101234\nEND:VCARD")
    assert b"\r\nFN:\r\n" in contacts._serialize(number_only)
    named = vobject.readOne("BEGIN:VCARD\nVERSION:3.0\nN:Doe;Jane;Q;Dr.;\nEND:VCARD")
    assert b"FN:Dr. Jane Q Doe" in contacts._serialize(named)
    company = vobject.readOne("BEGIN:VCARD\nVERSION:3.0\nN:;;;;\nORG:Acme;Sales\nEND:VCARD")
    assert b"FN:Acme" in contacts._serialize(company)


def test_display_name_falls_back_like_contacts_app():
    def name(body):
        return contacts._vcard_to_dict(vobject.readOne(f"BEGIN:VCARD\nVERSION:3.0\n{body}\nEND:VCARD"), "u")["name"]
    assert name("FN:\nN:Doe;Jane;;;") == "Jane Doe"
    assert name("FN:\nN:Doe;Jane;Q;Dr.;PhD") == "Dr. Jane Q Doe PhD"
    assert name("FN:\nN:;;;;\nORG:Acme;Sales") == "Acme"
    assert name("FN:\nN:Doe;Jane;;;\nORG:Acme;\nX-ABShowAs:COMPANY") == "Acme"
    assert name("FN:\nN:Doe;Jane;;;\nORG:Acme;") == "Jane Doe"
    assert name("FN:Janie\nN:Doe;Jane;;;") == "Janie"
    assert name("N:;;;;\nTEL:+15550101234") == ""


def test_list_contacts_skips_groups(monkeypatch):
    group = "BEGIN:VCARD\nVERSION:3.0\nFN:Book club\nN:Book club;;;;\nX-ADDRESSBOOKSERVER-KIND:group\nX-ADDRESSBOOKSERVER-MEMBER:urn:uuid:1\nEND:VCARD"
    monkeypatch.setattr(contacts.config, "CONTACTS_CACHE", False)  # full-download path: no sync requests
    monkeypatch.setattr(contacts, "require_auth", lambda: ("a", "b"))
    monkeypatch.setattr(contacts, "_default_addressbook_url", lambda session: "https://p1-contacts.icloud.com/ab/")
    monkeypatch.setattr(contacts, "_fetch_all_vcards", lambda session, url: [
        {"url": "https://p1-contacts.icloud.com/ab/g.vcf", "data": group, "etag": ""},
        {"url": "https://p1-contacts.icloud.com/ab/c.vcf", "data": VCARD, "etag": ""},
    ])
    assert [c["name"] for c in contacts.list_contacts()["contacts"]] == ["Jane Doe"]


def test_preexisting_orphan_annotations_are_left_alone():
    vcard = vobject.readOne("""BEGIN:VCARD
VERSION:3.0
FN:Jo
item1.TEL:+15550101234
item1.X-ABLabel:Old line
item2.X-ABADR:us
item3.X-ABLabel:stray
END:VCARD""")
    contacts._apply_fields(vcard, [{"value": "+15550109999"}], None, None, None, None)
    serialized = vcard.serialize()
    assert "Old line" not in serialized  # orphaned by this update: removed
    assert "item2.X-ABADR:us" in serialized and "item3.X-ABLABEL:stray" in serialized


def test_unchanged_birthday_keeps_stored_form():
    vcard = vobject.readOne("BEGIN:VCARD\nVERSION:3.0\nFN:Jo\nBDAY;VALUE=date:1990-01-15\nEND:VCARD")
    before = vcard.serialize()
    contacts._apply_fields(vcard, None, None, None, None, None, birthday="1990-01-15")
    assert vcard.serialize() == before


VCARD_LABELS = """BEGIN:VCARD
VERSION:3.0
FN:Robin Park
TEL;type=CELL;type=VOICE;type=pref:+1 555 010 0001
TEL;type=IPHONE;type=CELL;type=VOICE:+1 555 010 0002
TEL;type=HOME;type=FAX:+1 555 010 0003
TEL;type=pref:+1 555 010 0004
item1.TEL;type=pref:+1 555 010 0005
item1.X-ABLabel:Gym
EMAIL;type=INTERNET;type=HOME;type=pref:robin@example.com
item2.EMAIL;type=INTERNET:robin@school.example.edu
item2.X-ABLabel:_$!<School>!$_
item3.ADR;type=HOME;type=pref:;;1 Main St\\nApt 2;Boston;MA;02110;United States
item3.X-ABADR:us
item3.X-APPLE-SUBLOCALITY:Back Bay
ADR;type=WORK:;;9 Dock Rd;London;;E1 6AN;United Kingdom
END:VCARD"""


def test_phone_email_labels_read():
    data = contacts._vcard_to_dict(vobject.readOne(VCARD_LABELS), "https://x/c.vcf")
    assert data["phones"] == [
        {"label": "mobile", "value": "+1 555 010 0001"},
        {"label": "iPhone", "value": "+1 555 010 0002"},
        {"label": "home fax", "value": "+1 555 010 0003"},
        {"label": "", "value": "+1 555 010 0004"},
        {"label": "Gym", "value": "+1 555 010 0005"},
    ]
    assert data["emails"] == [
        {"label": "home", "value": "robin@example.com"},
        {"label": "school", "value": "robin@school.example.edu"},
    ]


def test_addresses_read_structured():
    data = contacts._vcard_to_dict(vobject.readOne(VCARD_LABELS), "https://x/c.vcf")
    assert data["addresses"] == [
        {"label": "home", "street": "1 Main St\nApt 2", "city": "Boston", "region": "MA",
         "postal_code": "02110", "country": "United States", "country_code": "us",
         "formatted": "1 Main St Apt 2, Boston, MA, 02110, United States"},
        {"label": "work", "street": "9 Dock Rd", "city": "London", "region": "", "postal_code": "E1 6AN",
         "country": "United Kingdom", "country_code": "", "formatted": "9 Dock Rd, London, E1 6AN, United Kingdom"},
    ]


def test_labelled_and_address_echo_is_verbatim():
    vcard = vobject.readOne(VCARD_LABELS)
    before = vcard.serialize()
    data = contacts._vcard_to_dict(vcard, "https://x/c.vcf")
    contacts._apply_fields(vcard, data["phones"], data["emails"], data["addresses"], None, None)
    assert vcard.serialize() == before


def test_phone_labels_written_like_contacts():
    vcard = vobject.readOne(VCARD_LABELS)
    phones = contacts._parse_labelled(vcard, "tel", contacts._TEL_TYPE_LABELS)
    phones[0]["label"] = "work"          # relabel, keeps pref
    phones[4]["label"] = "Climbing gym"  # custom -> X-ABLabel
    phones.append({"value": "+1 555 010 0006"})                   # default mobile
    phones.append({"label": "Main", "value": "+1 555 010 0007"})  # case-insensitive built-in
    contacts._apply_fields(vcard, phones, None, None, None, None)
    out = _unfolded(vcard)
    assert "TEL;TYPE=WORK,VOICE,pref:+1 555 010 0001" in out
    assert "TEL;TYPE=CELL,VOICE:+1 555 010 0006" in out
    assert "TEL;TYPE=MAIN:+1 555 010 0007" in out
    assert "X-ABLABEL:Climbing gym" in out and "X-ABLABEL:Gym\r" not in out
    assert [p["label"] for p in contacts._parse_labelled(vcard, "tel", contacts._TEL_TYPE_LABELS)] == [
        "work", "iPhone", "home fax", "", "Climbing gym", "mobile", "main"]


def test_address_edit_and_add():
    vcard = vobject.readOne(VCARD_LABELS)
    addresses = contacts._parse_addresses(vcard)
    addresses[0]["street"] = "2 Main St"  # changed: new line, keeps label + country code
    addresses[1] = {"label": "school", "street": "1 College Rd", "city": "Oxford", "country": "United Kingdom",
                    "country_code": "GB"}
    addresses.append("77 Plain Text Ave")  # bare string -> street, default home
    contacts._apply_fields(vcard, None, None, addresses, None, None)
    result = contacts._parse_addresses(vcard)
    assert [(a["label"], a["street"], a["country_code"]) for a in result] == [
        ("home", "2 Main St", "us"), ("school", "1 College Rd", "gb"), ("home", "77 Plain Text Ave", "")]
    out = _unfolded(vcard)
    assert "ADR;TYPE=HOME:;;2 Main St;Boston;MA;02110;United States" in out
    assert "X-ABLABEL:_$!<School>!$_" in out
    assert "Back Bay" not in out  # sub-locality belonged to the old street address


def test_invalid_labelled_and_address_input():
    for kwargs in ({"phones": [{"label": "mobile"}]}, {"addresses": [{"label": "home"}]},
                   {"addresses": [{"street": "x", "country_code": "USA"}]}):
        vcard = vobject.readOne(VCARD_LABELS)
        try:
            contacts._apply_fields(vcard, kwargs.get("phones"), None, kwargs.get("addresses"), None, None)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {kwargs}")


def test_search_uses_labelled_values(monkeypatch):
    (url,) = seed(monkeypatch, VCARD_LABELS)
    assert found("555-010-0003") == [url]
    assert found("school.example") == [url]
    assert found("dock rd") == [url]


# --- contact-fields-completion: task 1.1 (single-value text fields, organization) -----------

class FakeCardDAV:
    """Records PUTs; serves one stored vCard for GET. No network."""

    def __init__(self, stored: str = "", etag: str = '"e1"'):
        self.stored, self.etag, self.puts = stored, etag, []

    def get(self, url, timeout=None):
        response = type("R", (), {})()
        response.text, response.headers = self.stored, {"ETag": self.etag} if self.etag else {}
        response.raise_for_status = lambda: None
        return response

    def put(self, url, data=None, headers=None, timeout=None):
        self.puts.append({"url": url, "data": data.decode(), "headers": headers})
        response = type("R", (), {})()
        response.raise_for_status = lambda: None
        return response


@pytest.fixture
def fake_carddav(monkeypatch):
    fake = FakeCardDAV()
    monkeypatch.setattr(contacts, "require_auth", lambda: ("a@example.com", "pw"))
    monkeypatch.setattr(contacts, "_get_carddav_session", lambda e, p: fake)
    monkeypatch.setattr(contacts, "_default_addressbook_url", lambda s: "https://p1-contacts.icloud.com/ab/")
    return fake


VCARD_TEXT_FIELDS = """BEGIN:VCARD
VERSION:3.0
FN:Akira Sato
N:Sato;Akira;;;
X-PHONETIC-FIRST-NAME:ah-KEE-rah
X-PHONETIC-LAST-NAME:SAH-toh
NICKNAME:Aki
X-MAIDENNAME:Tanaka
ORG:Acme;Research;Platform
X-PHONETIC-ORG:AK-mee
X-ADDRESSING-GRAMMAR;TYPE=pref:b3BhcXVlLWJsb2I=
END:VCARD"""

TEXT_FIELDS = ("phonetic_first_name", "phonetic_middle_name", "phonetic_last_name", "nickname",
               "maiden_name", "department", "phonetic_organization")


def test_text_fields_read_and_default_empty():
    data = contacts._vcard_to_dict(vobject.readOne(VCARD_TEXT_FIELDS), "u")
    assert {k: data[k] for k in TEXT_FIELDS} == {
        "phonetic_first_name": "ah-KEE-rah", "phonetic_middle_name": "", "phonetic_last_name": "SAH-toh",
        "nickname": "Aki", "maiden_name": "Tanaka", "department": "Research", "phonetic_organization": "AK-mee"}
    assert data["organization"] == "Acme"
    empty = contacts._vcard_to_dict(vobject.readOne(VCARD), "u")
    assert all(empty[k] == "" for k in TEXT_FIELDS)


def test_text_fields_set_clear_and_omit():
    vcard = vobject.readOne(VCARD_TEXT_FIELDS)
    contacts._apply_fields(vcard, phonetic_middle_name="mee", nickname="", maiden_name="Ito")
    data = contacts._vcard_to_dict(vcard, "u")
    assert (data["phonetic_middle_name"], data["nickname"], data["maiden_name"]) == ("mee", "", "Ito")
    assert "NICKNAME" not in vcard.serialize()
    assert data["phonetic_first_name"] == "ah-KEE-rah"  # omitted: unchanged


def test_text_fields_echo_is_verbatim():
    vcard = vobject.readOne(VCARD_TEXT_FIELDS)
    before = vcard.serialize()
    data = contacts._vcard_to_dict(vcard, "u")
    contacts._apply_fields(vcard, organization=data["organization"],
                           **{k: data[k] for k in TEXT_FIELDS})
    assert vcard.serialize() == before


def test_organization_update_keeps_department_and_extra_components():
    vcard = vobject.readOne(VCARD_TEXT_FIELDS)
    contacts._apply_fields(vcard, organization="Acme Corp")
    assert vcard.org.value == ["Acme Corp", "Research", "Platform"]
    contacts._apply_fields(vcard, department="Labs")
    assert vcard.org.value == ["Acme Corp", "Labs", "Platform"]
    contacts._apply_fields(vcard, department="")
    assert contacts._vcard_to_dict(vcard, "u")["department"] == ""
    assert contacts._vcard_to_dict(vcard, "u")["organization"] == "Acme Corp"


def test_organization_with_separators_round_trips():
    vcard = vobject.readOne(VCARD)
    contacts._apply_fields(vcard, organization="Smith; Jones, LLP", department="Tax")
    reread = vobject.readOne(vcard.serialize())
    data = contacts._vcard_to_dict(reread, "u")
    assert (data["organization"], data["department"]) == ("Smith; Jones, LLP", "Tax")


def test_clearing_whole_organization_removes_org():
    vcard = vobject.readOne("BEGIN:VCARD\nVERSION:3.0\nFN:X\nORG:Acme;Eng\nEND:VCARD")
    contacts._apply_fields(vcard, organization="", department="")
    assert "ORG" not in vcard.serialize()


def test_trailing_empty_org_component_survives_echo():
    vcard = vobject.readOne("BEGIN:VCARD\nVERSION:3.0\nFN:X\nORG:Acme;\nEND:VCARD")
    before = vcard.serialize()
    data = contacts._vcard_to_dict(vcard, "u")
    contacts._apply_fields(vcard, organization=data["organization"], department=data["department"])
    assert vcard.serialize() == before


def test_create_and_update_pass_new_text_fields(fake_carddav):
    contacts.create_contact("Akira Sato", nickname="Aki", department="Research")
    created = vobject.readOne(fake_carddav.puts[-1]["data"])
    assert created.nickname.value == "Aki" and created.org.value == ["", "Research"]
    fake_carddav.stored = VCARD_TEXT_FIELDS
    contacts.update_contact("https://p1-contacts.icloud.com/ab/c.vcf", maiden_name="")
    updated = vobject.readOne(fake_carddav.puts[-1]["data"])
    assert not hasattr(updated, "x_maidenname")
    assert updated.x_addressing_grammar.value == "b3BhcXVlLWJsb2I="  # Apple-only data untouched


def test_phonetic_organization_set_and_clear():
    vcard = vobject.readOne(VCARD)
    contacts._apply_fields(vcard, phonetic_organization="AK-mee")
    assert contacts._vcard_to_dict(vcard, "u")["phonetic_organization"] == "AK-mee"
    contacts._apply_fields(vcard, phonetic_organization="")
    assert "X-PHONETIC-ORG" not in vcard.serialize()


# --- contact-fields-completion: task 1.3 (structured name parts, loss-free renames) ---------

def _card(body: str):
    return vobject.readOne(f"BEGIN:VCARD\nVERSION:3.0\n{body}\nEND:VCARD")


def _parts(vcard):
    return contacts._parse_name_parts(vcard)


def test_name_parts_read():
    data = contacts._vcard_to_dict(_card("FN:Dr. Jane Q Doe PhD\nN:Doe;Jane;Q;Dr.;PhD"), "u")
    assert {k: data[k] for k in ("prefix", "first_name", "middle_name", "last_name", "suffix")} == {
        "prefix": "Dr.", "first_name": "Jane", "middle_name": "Q", "last_name": "Doe", "suffix": "PhD"}
    empty = contacts._vcard_to_dict(_card("FN:X"), "u")
    assert all(empty[k] == "" for k in ("prefix", "first_name", "middle_name", "last_name", "suffix"))


def test_rename_with_display_name_keeps_prefix_middle_suffix():
    vcard = _card("FN:Dr. Jane Q Doe PhD\nN:Doe;Jane;Q;Dr.;PhD")
    contacts._apply_fields(vcard, name="Dr. Janet Q Doe PhD")
    assert _parts(vcard) == {"prefix": "Dr.", "first_name": "Janet", "middle_name": "Q",
                             "last_name": "Doe", "suffix": "PhD"}
    assert vcard.fn.value == "Dr. Janet Q Doe PhD"


def test_rename_without_stored_parts_and_single_token():
    vcard = _card("FN:Jane Q Doe\nN:Doe;Jane;Q;;")
    contacts._apply_fields(vcard, name="Jane Smith")  # middle name not in the new text: kept anyway
    assert _parts(vcard)["middle_name"] == "Q" and _parts(vcard)["last_name"] == "Smith"
    vcard = _card("FN:Jane Doe\nN:Doe;Jane;;;")
    contacts._apply_fields(vcard, name="Cher")
    assert (_parts(vcard)["first_name"], _parts(vcard)["last_name"]) == ("Cher", "")


def test_rename_keeps_multi_word_family_name():
    vcard = _card("FN:Jose de la Cruz\nN:de la Cruz;Jose;;;")
    contacts._apply_fields(vcard, name="José de la Cruz")
    assert (_parts(vcard)["first_name"], _parts(vcard)["last_name"]) == ("José", "de la Cruz")


def test_structured_parts_set_clear_and_recompute_display_name():
    vcard = _card("FN:Janie\nN:Doe;Jane;Q;;")
    contacts._apply_fields(vcard, prefix="Dr.", middle_name="")
    assert _parts(vcard) == {"prefix": "Dr.", "first_name": "Jane", "middle_name": "",
                             "last_name": "Doe", "suffix": ""}
    assert vcard.fn.value == "Dr. Jane Doe"  # R1.5: recomputed from the parts


def test_name_and_parts_together():
    vcard = _card("FN:Jane Doe\nN:Doe;Jane;;;")
    contacts._apply_fields(vcard, name="Janie", last_name="Smith")
    assert vcard.fn.value == "Janie" and _parts(vcard)["last_name"] == "Smith"
    assert _parts(vcard)["first_name"] == "Janie"  # not given explicitly: split from name


def test_unprovided_multi_value_parts_untouched():
    vcard = _card("FN:John Doe\nN:Doe;John;A,B;Dr.;Jr.,PhD")
    contacts._apply_fields(vcard, first_name="Jon")
    serialized = vcard.serialize()
    assert "N:Doe;Jon;A,B;Dr.;Jr.,PhD" in serialized


@pytest.mark.parametrize("body", [
    "FN:\nN:Doe;Jane;;;",                                  # empty FN: read name comes from N
    "FN:\nN:;;;;\nORG:Acme;\nX-ABShowAs:COMPANY",           # company card
    "FN:John Doe\nN:Doe;John;A,B;Dr.;Jr.,PhD",              # multi-value parts
    "FN:Jose de la Cruz\nN:de la Cruz;Jose;;;",             # multi-word family name
])
def test_name_write_back_is_a_no_op(body):
    vcard = _card(body)
    before = vcard.serialize()
    data = contacts._vcard_to_dict(vcard, "u")
    contacts._apply_fields(vcard, name=data["name"],
                           **{k: data[k] for k in ("prefix", "first_name", "middle_name", "last_name", "suffix")})
    assert vcard.serialize() == before


def test_create_without_identity_fails_before_any_request(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network or auth touched")
    monkeypatch.setattr(contacts, "require_auth", no_network)
    monkeypatch.setattr(contacts.requests.Session, "request", no_network)
    with pytest.raises(ValueError, match="needs a name, a name part or an organization"):
        contacts.create_contact()
    with pytest.raises(ValueError):
        contacts.create_contact(name="  ", organization="")


def test_create_from_parts_or_organization_only(fake_carddav):
    contacts.create_contact(first_name="Ada", last_name="Lovelace")
    card = vobject.readOne(fake_carddav.puts[-1]["data"])
    assert card.fn.value == "Ada Lovelace" and card.n.value.family == "Lovelace"
    contacts.create_contact(organization="Acme")
    card = vobject.readOne(fake_carddav.puts[-1]["data"])
    assert card.fn.value == "Acme" and hasattr(card, "n")


def test_update_contact_rename_keeps_parts(fake_carddav):
    fake_carddav.stored = "BEGIN:VCARD\nVERSION:3.0\nFN:Dr. Jane Q Doe\nN:Doe;Jane;Q;Dr.;\nEND:VCARD"
    contacts.update_contact("https://p1-contacts.icloud.com/ab/c.vcf", name="Dr. Jane Q Smith")
    card = vobject.readOne(fake_carddav.puts[-1]["data"])
    assert (card.n.value.prefix, card.n.value.given, card.n.value.additional, card.n.value.family) == (
        "Dr.", "Jane", "Q", "Smith")


def test_name_write_back_ignores_stray_spaces_in_stored_parts():
    vcard = _card("FN:\nN:Doe ;Jane;;;")
    before = vcard.serialize()
    data = contacts._vcard_to_dict(vcard, "u")
    assert data["last_name"] == "Doe"
    contacts._apply_fields(vcard, name=data["name"],
                           **{k: data[k] for k in ("prefix", "first_name", "middle_name", "last_name", "suffix")})
    assert vcard.serialize() == before


# --- contact-fields-completion: task 1.4 (company-card flag) ------------------------------

def test_is_company_read():
    assert contacts._vcard_to_dict(_card("FN:Acme\nORG:Acme;\nX-ABShowAs:COMPANY"), "u")["is_company"] is True
    assert contacts._vcard_to_dict(_card("FN:Acme\nORG:Acme\nX-ABShowAs:company"), "u")["is_company"] is True
    assert contacts._vcard_to_dict(_card("FN:Jane"), "u")["is_company"] is False


def test_is_company_set_and_unset():
    vcard = _card("FN:Jane\nN:Doe;Jane;;;\nORG:Acme")
    contacts._apply_fields(vcard, is_company=True)
    assert contacts._vcard_to_dict(vcard, "u")["is_company"] is True
    assert "X-ABSHOWAS:COMPANY" in vcard.serialize().upper()
    contacts._apply_fields(vcard, is_company=False)
    assert "X-ABSHOWAS" not in vcard.serialize().upper()


def test_is_company_false_keeps_other_showas_values():
    vcard = _card("FN:Jane\nORG:Acme\nX-ABShowAs:PERSON")
    contacts._apply_fields(vcard, is_company=False)
    assert "X-ABSHOWAS:PERSON" in vcard.serialize().upper()


def test_is_company_needs_an_organization():
    vcard = _card("FN:Jane\nN:Doe;Jane;;;")
    before = vcard.serialize()
    with pytest.raises(ValueError, match="is_company needs an organization"):
        contacts._apply_fields(vcard, [{"value": "+15550101234"}], is_company=True)
    assert vcard.serialize() == before  # validated before anything changed
    with pytest.raises(ValueError):
        contacts._apply_fields(_card("FN:X\nORG:Acme"), organization="", is_company=True)
    ok = _card("FN:Jane")
    contacts._apply_fields(ok, organization="Acme", is_company=True)
    assert contacts._vcard_to_dict(ok, "u")["is_company"] is True


def test_is_company_echo_is_a_no_op():
    vcard = _card("FN:\nN:;;;;\nORG:Acme;\nX-ABShowAs:COMPANY")
    before = vcard.serialize()
    data = contacts._vcard_to_dict(vcard, "u")
    contacts._apply_fields(vcard, organization=data["organization"], is_company=data["is_company"])
    assert vcard.serialize() == before


# --- contact-fields-completion: task 1.5 (related names) ----------------------------------

VCARD_RELATED = """BEGIN:VCARD
VERSION:3.0
FN:Jo Doe
item1.X-ABRELATEDNAMES;type=pref:Mary Doe
item1.X-ABLabel:_$!<Mother>!$_
item2.X-ABRELATEDNAMES:Sam
item2.X-ABLabel:Climbing buddy
X-ABRELATEDNAMES:Pat
END:VCARD"""


def test_related_names_read():
    assert contacts._vcard_to_dict(vobject.readOne(VCARD_RELATED), "u")["related_names"] == [
        {"label": "mother", "name": "Mary Doe"},
        {"label": "Climbing buddy", "name": "Sam"},
        {"label": "", "name": "Pat"},
    ]
    assert contacts._vcard_to_dict(vobject.readOne(VCARD), "u")["related_names"] == []


def test_related_names_echo_is_verbatim():
    vcard = vobject.readOne(VCARD_RELATED)
    before = vcard.serialize()
    contacts._apply_fields(vcard, related_names=contacts._parse_related_names(vcard))
    assert vcard.serialize() == before


def test_related_names_add_remove_relabel():
    vcard = vobject.readOne(VCARD_RELATED)
    items = contacts._parse_related_names(vcard)
    items[0]["label"] = "Parent"          # relabel built-in -> built-in, keeps pref
    del items[1]                          # remove Sam and his custom label
    items.append({"name": "Alex", "label": "spouse"})
    items.append({"name": "Robin"})       # no label -> other
    contacts._apply_fields(vcard, related_names=items)
    assert contacts._parse_related_names(vcard) == [
        {"label": "parent", "name": "Mary Doe"},
        {"label": "", "name": "Pat"},
        {"label": "spouse", "name": "Alex"},
        {"label": "other", "name": "Robin"},
    ]
    out = _unfolded(vcard)
    assert "Climbing buddy" not in out and "Sam" not in out
    assert "X-ABLABEL:_$!<Parent>!$_" in out and "X-ABLABEL:_$!<Spouse>!$_" in out
    mary = next(p for p in vcard.x_abrelatednames_list if p.value == "Mary Doe")
    assert "pref" in [t.lower() for t in mary.params["TYPE"]]


def test_related_name_needs_a_name():
    vcard = vobject.readOne(VCARD_RELATED)
    before = vcard.serialize()
    with pytest.raises(ValueError, match="related name entry needs a name"):
        contacts._apply_fields(vcard, related_names=[{"label": "mother", "name": " "}])
    assert vcard.serialize() == before


def test_damaged_related_label_survives_echo():
    vcard = _card("FN:Jo\nitem1.X-ABRELATEDNAMES:Mary\nitem1.X-ABLabel:_$!<Mother>!$_X-SHARED-PHOTO-DISPLAY-PREF:ALWAYS_ASK")
    before = vcard.serialize()
    contacts._apply_fields(vcard, related_names=contacts._parse_related_names(vcard))
    assert vcard.serialize() == before


def test_new_related_name_never_reuses_a_label_less_group():
    vcard = _card("FN:Jo\nitem1.X-ABRELATEDNAMES:Mary\nitem1.X-ABLabel:_$!<Mother>!$_\nitem2.X-ABRELATEDNAMES:Kim")
    items = contacts._parse_related_names(vcard) + [{"name": "Lee", "label": "Coach"}]
    contacts._apply_fields(vcard, related_names=items)
    groups = [p.group for p in vcard.x_abrelatednames_list]
    assert len(set(groups)) == 3 and contacts._parse_related_names(vcard)[1] == {"label": "", "name": "Kim"}


# --- contact-fields-completion: task 1.6 (instant messages) -------------------------------

VCARD_IMPP = """BEGIN:VCARD
VERSION:3.0
FN:Jo Doe
IMPP;X-SERVICE-TYPE=Skype;type=HOME;type=pref:skype:jane.doe
item1.IMPP;X-SERVICE-TYPE=Snapchat;X-BUNDLEIDENTIFIERS=com.toyopagroup.picaboo;X-TEAMIDENTIFIER=424M5254LK;type=pref:x-apple:janesnaps
item1.X-ABLabel:Snapchat
IMPP:aim:jane%20aim
END:VCARD"""


def test_instant_messages_read():
    assert contacts._vcard_to_dict(vobject.readOne(VCARD_IMPP), "u")["instant_messages"] == [
        {"label": "home", "service": "Skype", "username": "jane.doe"},
        {"label": "Snapchat", "service": "Snapchat", "username": "janesnaps"},
        {"label": "", "service": "AIM", "username": "jane aim"},  # service from the URI scheme
    ]


def test_instant_messages_echo_is_verbatim_including_app_identifiers():
    vcard = vobject.readOne(VCARD_IMPP)
    before = vcard.serialize()
    contacts._apply_fields(vcard, instant_messages=contacts._parse_instant_messages(vcard))
    assert vcard.serialize() == before


def test_instant_messages_add_remove_relabel():
    vcard = vobject.readOne(VCARD_IMPP)
    items = contacts._parse_instant_messages(vcard)
    items[0]["label"] = "work"   # relabel Skype, keeps pref
    del items[2]                 # remove AIM
    items.append({"service": "skype", "username": "jane work"})
    items.append({"service": "Signal", "username": "+15550101234", "label": "Private"})
    contacts._apply_fields(vcard, instant_messages=items)
    assert contacts._parse_instant_messages(vcard) == [
        {"label": "work", "service": "Skype", "username": "jane.doe"},
        {"label": "Snapchat", "service": "Snapchat", "username": "janesnaps"},
        {"label": "other", "service": "Skype", "username": "jane work"},
        {"label": "Private", "service": "Signal", "username": "+15550101234"},
    ]
    out = _unfolded(vcard)
    assert "skype:jane%20work" in out and "x-apple:+15550101234" in out and "aim:" not in out
    assert "X-BUNDLEIDENTIFIERS=com.toyopagroup.picaboo" in out
    skype = next(p for p in vcard.impp_list if p.value == "skype:jane.doe")
    assert [t.upper() for t in skype.params["TYPE"]] == ["WORK", "PREF"]


@pytest.mark.parametrize("entry", [{"service": "Skype", "username": ""}, {"service": " ", "username": "x"}])
def test_instant_message_needs_service_and_username(entry):
    vcard = vobject.readOne(VCARD_IMPP)
    before = vcard.serialize()
    with pytest.raises(ValueError, match="instant message entry needs a service and a username"):
        contacts._apply_fields(vcard, instant_messages=[entry])
    assert vcard.serialize() == before


# --- contact-fields-completion: task 1.7 (readable Apple-only fields) ---------------------

# Lines recorded from the throwaway "ZZ MCP Fields Test" card (iOS 27.0.1); see
# .specs/contact-fields-completion/execution/format-investigation.md.
VCARD_APPLE_EXTRAS = """BEGIN:VCARD
VERSION:3.0
FN:ZZ MCP Fields Test
N:Test;ZZ MCP Fields;;;
BDAY;value=date:1604-01-11
X-ALTBDAY;CALSCALE=hebrew:--0130
X-ACTIVITY-ALERT:type=text\\,snd=texttone:Antic
X-ACTIVITY-ALERT:type=call\\,snd=system:Arpeggio
X-ADDRESSING-GRAMMAR;TYPE=pref:b3BhcXVlLWJsb2I=
END:VCARD"""


def test_apple_extras_read_from_recorded_values():
    data = contacts._vcard_to_dict(vobject.readOne(VCARD_APPLE_EXTRAS), "u")
    assert data["alternate_birthday"] == {"calendar": "hebrew", "year": None, "month": 1, "day": 30}
    assert (data["ringtone"], data["text_tone"]) == ("Arpeggio", "Antic")
    assert data["birthday"] == "--01-11"  # the Gregorian birthday stays independent


def test_alternate_birthday_with_year():
    data = contacts._vcard_to_dict(_card("FN:X\nX-ALTBDAY;CALSCALE=chinese:20240215"), "u")
    assert data["alternate_birthday"] == {"calendar": "chinese", "year": 2024, "month": 2, "day": 15}


@pytest.mark.parametrize("body", [
    "X-ALTBDAY:--0130",                                  # no calendar
    "X-ALTBDAY;CALSCALE=chinese:--13L01",                # unknown shape (e.g. a leap-month marker)
    "X-ACTIVITY-ALERT:type=call\\,snd=itunes:1234567890",  # purchased tone: identifier, not a name
    "X-ACTIVITY-ALERT:snd=system:Arpeggio",              # no type
])
def test_undecodable_apple_values_are_omitted(body):
    data = contacts._vcard_to_dict(_card(f"FN:X\n{body}"), "u")
    assert not {"alternate_birthday", "ringtone", "text_tone"} & set(data)


def test_absent_apple_extras_and_pronouns_never_read():
    data = contacts._vcard_to_dict(vobject.readOne(VCARD_APPLE_EXTRAS), "u")
    assert "b3BhcXVlLWJsb2I=" not in repr(data) and "pronouns" not in data
    plain = contacts._vcard_to_dict(vobject.readOne(VCARD), "u")
    assert not {"alternate_birthday", "ringtone", "text_tone", "verification_code"} & set(plain)


# --- contact-fields-completion: task 1.8 (pronunciation names) -----------------------------

def test_pronunciation_names_read_set_clear_and_echo():
    vcard = vobject.readOne(VCARD_APPLE_EXTRAS.replace(
        "END:VCARD", "X-PRONUNCIATION-FIRST-NAME:zzz\nX-PRONUNCIATION-LAST-NAME:macap\nEND:VCARD"))
    data = contacts._vcard_to_dict(vcard, "u")
    assert (data["pronunciation_first_name"], data["pronunciation_last_name"]) == ("zzz", "macap")
    before = vcard.serialize()
    contacts._apply_fields(vcard, pronunciation_first_name="zzz", pronunciation_last_name="macap")
    assert vcard.serialize() == before
    contacts._apply_fields(vcard, pronunciation_first_name="zee", pronunciation_last_name="")
    data = contacts._vcard_to_dict(vcard, "u")
    assert (data["pronunciation_first_name"], data["pronunciation_last_name"]) == ("zee", "")
    assert "X-PRONUNCIATION-LAST-NAME" not in vcard.serialize()
    assert contacts._vcard_to_dict(vobject.readOne(VCARD), "u")["pronunciation_first_name"] == ""


# --- contact-fields-completion: wave review fixes (stages 1-2) ------------------------------

def test_name_with_parts_still_fills_first_and_last(fake_carddav):
    contacts.create_contact("Jane Doe", middle_name="Q")
    card = vobject.readOne(fake_carddav.puts[-1]["data"])
    assert (card.n.value.given, card.n.value.additional, card.n.value.family) == ("Jane", "Q", "Doe")
    contacts.create_contact("Dr. Jane Doe Jr.", prefix="Dr.", suffix="Jr.")
    card = vobject.readOne(fake_carddav.puts[-1]["data"])
    assert (card.n.value.prefix, card.n.value.given, card.n.value.family, card.n.value.suffix) == (
        "Dr.", "Jane", "Doe", "Jr.")
    vcard = _card("FN:Jane Doe\nN:Doe;Jane;;;")
    contacts._apply_fields(vcard, name="Janet Roe", suffix="Jr.")
    assert _parts(vcard) == {"prefix": "", "first_name": "Janet", "middle_name": "",
                             "last_name": "Roe", "suffix": "Jr."}
    contacts._apply_fields(vcard, name="Jan Smith", last_name="Brown")  # a changed explicit part wins
    assert (_parts(vcard)["first_name"], _parts(vcard)["last_name"]) == ("Jan Smith", "Brown")


def test_duplicate_related_names_match_on_label_first():
    vcard = _card("FN:Jo\nitem1.X-ABRELATEDNAMES:Sam\nitem1.X-ABLabel:_$!<Spouse>!$_\n"
                  "item2.X-ABRELATEDNAMES;type=pref:Sam\nitem2.X-ABLabel:_$!<Friend>!$_")
    contacts._apply_fields(vcard, related_names=[{"name": "Sam", "label": "friend"}])
    (prop,) = vcard.x_abrelatednames_list
    assert prop.group == "item2" and "pref" in [t.lower() for t in prop.params["TYPE"]]


def test_duplicate_instant_messages_match_on_label_first():
    vcard = _card("FN:Jo\nitem1.IMPP;X-SERVICE-TYPE=Signal;X-FOO=a:x-apple:u\nitem1.X-ABLabel:One\n"
                  "item2.IMPP;X-SERVICE-TYPE=Signal;X-FOO=b:x-apple:u\nitem2.X-ABLabel:Two")
    contacts._apply_fields(vcard, instant_messages=[{"service": "Signal", "username": "u", "label": "Two"}])
    (prop,) = vcard.impp_list
    assert prop.group == "item2" and prop.params["X-FOO"] == ["b"]


def test_is_company_true_replaces_other_showas_value():
    vcard = _card("FN:Jo\nORG:Acme\nX-ABShowAs:PERSON")
    contacts._apply_fields(vcard, is_company=True)
    assert [str(p.value).upper() for p in vcard.contents["x-abshowas"]] == ["COMPANY"]


def test_sound_name_with_comma_is_not_truncated():
    data = contacts._vcard_to_dict(_card("FN:X\nX-ACTIVITY-ALERT:type=call\\,snd=system:Time\\, Up"), "u")
    assert data["ringtone"] == "Time, Up"


def test_whitespace_only_text_field_clears_it():
    vcard = vobject.readOne(VCARD_TEXT_FIELDS)
    contacts._apply_fields(vcard, nickname="   ", maiden_name="  Ito ")
    data = contacts._vcard_to_dict(vcard, "u")
    assert (data["nickname"], data["maiden_name"]) == ("", "Ito")
    assert "NICKNAME" not in vcard.serialize()


def test_echo_of_every_read_field_changes_nothing():
    for body in (VCARD, VCARD_TEXT_FIELDS, VCARD_RELATED, VCARD_IMPP, VCARD_APPLE_EXTRAS, VCARD_LABELS,
                 VCARD_GROUPED, VCARD_SOCIAL, VCARD_DATES,
                 "BEGIN:VCARD\nVERSION:3.0\nFN:Jo\nN:Doe;Jo;;;\nNOTE:<b>bold</b> note\nEND:VCARD"):
        vcard = vobject.readOne(body)
        before = vcard.serialize()
        data = contacts._vcard_to_dict(vcard, "u")
        writable = {k: v for k, v in data.items()
                    if k not in ("id", "url", "alternate_birthday", "ringtone", "text_tone")}
        positional = [writable.pop(k) for k in ("phones", "emails", "addresses", "organization", "title",
                                                 "notes", "birthday", "urls", "social_profiles", "dates")]
        contacts._apply_fields(vcard, *positional, **writable)
        assert vcard.serialize() == before, body.splitlines()[2]


def test_name_split_ignores_echoed_parts_and_strips_explicit_ones():
    vcard = _card("FN:Dr. Jane Q Doe Jr\nN:Doe;Jane;Q;Dr.;Jr")
    contacts._apply_fields(vcard, name="Janet Smith", prefix="Dr.", first_name="Jane", middle_name="Q",
                           last_name="Doe", suffix="Jr")  # echoed parts count as omitted
    assert (_parts(vcard)["first_name"], _parts(vcard)["last_name"]) == ("Janet", "Smith")
    vcard = _card("FN:Jane Doe\nN:Doe;Jane;;;")
    contacts._apply_fields(vcard, name="Jane van der Berg", last_name="van der Berg")
    assert (_parts(vcard)["first_name"], _parts(vcard)["last_name"]) == ("Jane", "van der Berg")
    vcard = _card("FN:Jane Doe\nN:Doe;Jane;;;")
    contacts._apply_fields(vcard, name="Mary Ann Smith Jones", first_name="Mary Ann")
    assert (_parts(vcard)["first_name"], _parts(vcard)["last_name"]) == ("Mary Ann", "Smith Jones")


@pytest.mark.parametrize("stored", ["<br>", "<p></p>", "<div> </div>"])
def test_empty_notes_clear_html_that_reads_as_empty(stored):
    vcard = _card(f"FN:X\nNOTE:{stored}")
    contacts._apply_fields(vcard, notes="")
    assert not hasattr(vcard, "note")


# --- contact-fields-completion: task 2.1 (search the new name fields) ---------------------

def test_search_matches_nickname_maiden_related_and_im(monkeypatch):
    (url,) = seed(monkeypatch,
                  "BEGIN:VCARD\nVERSION:3.0\nFN:Akira Sato\nN:Sato;Akira;;;\nNICKNAME:Aki\nX-MAIDENNAME:Tanaka\n"
                  "item1.X-ABRELATEDNAMES:Yumi Sato\nitem1.X-ABLabel:_$!<Spouse>!$_\n"
                  "IMPP;X-SERVICE-TYPE=Skype:skype:akira.s\nEND:VCARD")
    for query in ("aki", "TANAKA", "yumi", "akira.s"):
        assert found(query) == [url], query
    assert found("nobody-here") == []


# --- contact-fields-completion: task 2.2 (conditional PUT) --------------------------------

def test_update_sends_if_match_with_the_read_etag(fake_carddav):
    fake_carddav.stored, fake_carddav.etag = VCARD, '"etag-42"'
    contacts.update_contact("https://p1-contacts.icloud.com/ab/c.vcf", nickname="Jo")
    assert fake_carddav.puts[-1]["headers"]["If-Match"] == '"etag-42"'


def test_update_refuses_when_icloud_returns_no_etag(fake_carddav):
    fake_carddav.stored, fake_carddav.etag = VCARD, ""
    with pytest.raises(ValueError, match="no ETag"):
        contacts.update_contact("https://p1-contacts.icloud.com/ab/c.vcf", nickname="Jo")
    assert fake_carddav.puts == []


# --- contacts-list-search-scale: task 1.2 (folding and cached cards) -----------------------

@pytest.mark.parametrize("text, folded", [
    ("José García", "jose garcia"), ("Straße", "strasse"), ("Łukasz", "lukasz"), ("Øyvind", "oyvind"),
    ("Đorđe", "dorde"), ("İstanbul", "istanbul"), ("Æsa Œuvre", "aesa oeuvre"), ("Þór", "thor"),
])
def test_fold(text, folded):
    assert contacts.fold(text) == folded


def test_build_card_fields_and_search_text():
    card = contacts.build_card("https://p1-contacts.icloud.com/ab/c.vcf", '"e1"', VCARD_LABELS)
    assert card.url.endswith("c.vcf") and card.etag == '"e1"' and card.listed
    assert card.contact == contacts._vcard_to_dict(vobject.readOne(VCARD_LABELS), card.url)
    assert card.sort_key == (0, "robin park")
    assert "robin park" in card.name_text
    assert "robin@school.example.edu" in card.other_text and "boston" in card.other_text
    assert "15550100003" in card.phone_digits


def test_build_card_covers_every_searched_field():
    vcard = ("BEGIN:VCARD\nVERSION:3.0\nFN:Akira Satō\nN:Satō;Akira;;;\nNICKNAME:Aki\nX-MAIDENNAME:Tanaka\n"
             "X-PHONETIC-FIRST-NAME:ah-kee-rah\nX-PRONUNCIATION-LAST-NAME:sah-toh\nORG:Acmé;Research\n"
             "NOTE:likes tea\nEMAIL:aki@example.com\nitem1.X-ABRELATEDNAMES:Yumi\nitem1.X-ABLabel:_$!<Spouse>!$_\n"
             "IMPP;X-SERVICE-TYPE=Skype:skype:aki.s\n"
             "X-SOCIALPROFILE;type=instagram;x-user=aki_s:https://www.instagram.com/aki_s/\nEND:VCARD")
    card = contacts.build_card("https://p1-contacts.icloud.com/ab/a.vcf", None, vcard)
    for word in ("akira", "sato", "aki", "tanaka", "ah-kee-rah", "sah-toh"):
        assert word in card.name_text, word
    for word in ("acme", "research", "likes tea", "aki@example.com", "yumi", "aki.s", "aki_s", "instagram.com"):
        assert word in card.other_text, word


def test_build_card_listing_rules():
    group = "BEGIN:VCARD\nVERSION:3.0\nFN:Club\nN:Club;;;;\nX-ADDRESSBOOKSERVER-KIND:group\nEND:VCARD"
    assert contacts.build_card("u1", None, group).listed is False
    nameless = "BEGIN:VCARD\nVERSION:3.0\nN:;;;;\nNOTE:just a note\nEND:VCARD"
    card = contacts.build_card("u2", None, nameless)
    assert card.listed is True and card.sort_key == (1, "")  # every non-group card is a person
    assert contacts.build_card("u3", None, "not a vcard") is None


# --- paged listing (spec: .specs/contacts-list-search-scale, task 2.1) -------------------------

NAMELESS = "BEGIN:VCARD\nVERSION:3.0\nN:;;;;\nTEL:+1555010000{}\nEND:VCARD\n"
GROUP = "BEGIN:VCARD\nVERSION:3.0\nFN:Club\nN:Club;;;;\nX-ADDRESSBOOKSERVER-KIND:group\nEND:VCARD\n"


@pytest.fixture
def book(monkeypatch):
    """A fake iCloud address book behind the real list/search code path (cache included)."""
    server = CardDAVServer()
    server.discoveries = 0

    def discover(session):
        server.discoveries += 1
        return BOOK

    monkeypatch.setattr(contacts, "require_auth", lambda: ("jo@example.com", "app-pw"))
    monkeypatch.setattr(contacts, "_get_carddav_session", lambda email, password: server)
    monkeypatch.setattr(contacts, "_default_addressbook_url", discover)
    return server


def traverse(page_size=7, fields=None, between=None):
    seen, cursor, pages = [], None, []
    while True:
        page = contacts.list_contacts(page_size=page_size, cursor=cursor, fields=fields)
        seen.extend(page["contacts"])
        pages.append(page)
        cursor = page["next_cursor"]
        if between:
            between(len(pages))
        if cursor is None:
            return seen, pages


def test_list_traverses_everyone_once_in_folded_order(book):
    named = [f"Person {i:03d}" for i in range(110)] + [
        "Émile Adams", "Łukasz Nowak", "anna bell", "Øyvind Berg", "Zoë Quinn", "Fred Ames", "Ölaf Ek"]
    for i, name in enumerate(named):
        book.put_card(f"p{i:03d}", vcard(name))
    for i in range(3):
        book.put_card(f"nameless{i}", NAMELESS.format(i))
    book.put_card("club", GROUP)
    seen, pages = traverse(page_size=7)
    assert len(seen) == 120 and len({c["id"] for c in seen}) == 120
    assert all(len(p["contacts"]) == 7 for p in pages[:-1]) and pages[-1]["next_cursor"] is None
    assert {p["total"] for p in pages} == {120}
    got = [c["name"] for c in seen]
    assert got[:117] == sorted(named, key=contacts.fold) and got[117:] == ["", "", ""]
    assert got.index("anna bell") < got.index("Émile Adams") < got.index("Fred Ames") < got.index("Łukasz Nowak")
    assert got.index("Ölaf Ek") < got.index("Øyvind Berg") < got.index("Person 000")
    assert "Club" not in got


def test_adds_and_removes_between_pages_never_repeat(book):
    for i in range(30):
        book.put_card(f"p{i:02d}", vcard(f"Person {i:02d}"))

    def between(pages):
        if pages == 1:
            book.put_card("early", vcard("Aaron Early"))  # sorts before the cursor: not served
            book.put_card("late", vcard("Zach Late"))     # sorts after it: served
            book.delete_card("p02")                       # already served
            book.delete_card("p20")                       # not yet served

    seen, _ = traverse(page_size=5, between=between)
    ids = [c["id"] for c in seen]
    assert len(ids) == len(set(ids))
    names = {c["name"] for c in seen}
    assert "Zach Late" in names and "Aaron Early" not in names and "Person 20" not in names
    assert {f"Person {i:02d}" for i in range(30)} - {"Person 20"} <= names


def _b64(payload) -> str:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return base64.urlsafe_b64encode(raw).decode()


LIST_FP = contacts._fingerprint("list")
BAD_CURSORS = [
    "!!!not base64!!!",
    _b64(b"\xff\xfe\xfd"),
    _b64(b"not json"),
    _b64([0, 0, "a", "u"]),
    _b64({"v": 2, "q": LIST_FP, "after": [0, 0, "a", "u"]}),
    _b64({"v": True, "q": LIST_FP, "after": [0, 0, "a", "u"]}),
    _b64({"v": 1, "q": LIST_FP, "after": [0, 0, "a"]}),
    _b64({"v": 1, "q": LIST_FP, "after": ["0", 0, "a", "u"]}),
    _b64({"v": 1, "q": LIST_FP, "after": [1, 0, "a", "u"]}),   # search ranks are not list positions
    _b64({"v": 1, "q": LIST_FP, "after": [0, 2, "a", "u"]}),
    _b64({"v": 1, "q": LIST_FP, "after": [0, 0, None, "u"]}),
    _b64({"v": 1, "q": LIST_FP, "after": [0, 0, "a", "u"], "x": 1}),
    contacts._encode_cursor(contacts._fingerprint("search:jo"), (0, 0, "jo", BOOK + "j.vcf")),
]


@pytest.mark.parametrize("cursor", BAD_CURSORS)
def test_bad_or_foreign_cursor_asks_to_start_again(book, cursor):
    with pytest.raises(ValueError, match="start again without a cursor"):
        contacts.list_contacts(cursor=cursor)
    assert book.log == []  # rejected before any request


def test_page_size_and_fields_may_change_between_pages(book):
    names = [f"Person {i:02d}" for i in range(12)]
    for i, name in enumerate(names):
        book.put_card(f"p{i:02d}", vcard(name, "TEL:+15550100000\n"))
    first = contacts.list_contacts(page_size=3)
    second = contacts.list_contacts(page_size=10, cursor=first["next_cursor"], fields=["phones"])
    assert [c["name"] for c in first["contacts"]] == names[:3]
    assert len(second["contacts"]) == 9 and second["next_cursor"] is None
    assert all(set(c) == {"id", "phones"} for c in second["contacts"])
    assert [c["id"] for c in first["contacts"] + second["contacts"]] == [BOOK + f"p{i:02d}.vcf" for i in range(12)]


def test_field_projection(book):
    url = book.put_card("jane", VCARD)
    full = contacts._vcard_to_dict(vobject.readOne(VCARD), url)
    summary = contacts.list_contacts()["contacts"][0]
    assert summary == {"id": url, "name": "Jane Doe", "organization": "",
                       "phone": full["phones"][0]["value"], "email": "jane@example.com"}
    assert contacts.list_contacts(fields=[])["contacts"][0] == summary
    named = contacts.list_contacts(fields=["phones", "ringtone", "nickname", "phone", "phones"])["contacts"][0]
    assert named == {"id": url, "phones": full["phones"], "nickname": "", "phone": summary["phone"]}
    everything = contacts.list_contacts(fields=["all"])["contacts"][0]
    assert everything == full
    everything["phones"].clear()  # callers get copies; the cached card is untouched
    assert contacts.list_contacts(fields=["all"])["contacts"][0] == full
    with pytest.raises(ValueError, match="unknown field.*shoe_size.*valid fields: all, addresses"):
        contacts.list_contacts(fields=["shoe_size"])
    with pytest.raises(ValueError, match='"all" cannot be combined'):
        contacts.list_contacts(fields=["all", "name"])


def test_contact_fields_cover_every_key_a_contact_read_can_have():
    rich = vobject.readOne("BEGIN:VCARD\nVERSION:3.0\nFN:Ada Lovelace\nN:Lovelace;Ada;;;\n"
                           "X-ALTBDAY;CALSCALE=hebrew:--0130\n"
                           "X-ACTIVITY-ALERT:type=call\\,snd=system:Arpeggio\n"
                           "X-ACTIVITY-ALERT:type=text\\,snd=texttone:Note\nEND:VCARD\n")
    keys = set(contacts._vcard_to_dict(rich, BOOK + "a.vcf"))
    assert {"alternate_birthday", "ringtone", "text_tone"} <= keys
    assert keys <= contacts.CONTACT_FIELDS
    assert contacts.CONTACT_FIELDS - keys == {"phone", "email"}


@pytest.mark.parametrize("page_size", [0, 201, -1, True, 2.5])
def test_page_size_out_of_range_is_rejected_before_any_request(book, page_size):
    with pytest.raises(ValueError, match="page_size"):
        contacts.list_contacts(page_size=page_size)
    assert book.log == []


def test_cached_list_checks_for_changes_on_every_call(book):
    book.put_card("jane", VCARD)
    contacts.list_contacts()
    book.log.clear()
    book.put_card("john", vcard("John Roe"))
    assert [c["name"] for c in contacts.list_contacts()["contacts"]] == ["Jane Doe", "John Roe"]
    assert book.kinds() == ["sync_collection", "addressbook_multiget"]
    assert book.discoveries == 1


def test_kill_switch_downloads_everything_on_every_call(book, monkeypatch):
    book.put_card("jane", VCARD)
    contacts.list_contacts()  # cached first ...
    monkeypatch.setattr(contacts.config, "CONTACTS_CACHE", False)
    book.log.clear()
    contacts.list_contacts()
    contacts.list_contacts()
    assert book.kinds() == ["addressbook_query", "addressbook_query"]
    assert book.discoveries == 3
    assert not contacts._cache._entries  # ... and switching the cache off drops cached data


# --- the server's own writes (task 2.2) ---------------------------------------------------------

def _entry():
    (entry,) = contacts._cache._entries.values()
    return entry


def test_own_writes_show_up_even_when_the_change_report_lags(book):
    book.put_card("jane", VCARD)
    contacts.list_contacts()            # warm the cache
    book.report_writes = False          # iCloud's change report has not caught up yet

    created = contacts.create_contact(name="Grace Hopper", phones=[{"value": "+15550100001"}])
    listed = contacts.list_contacts()["contacts"]
    assert [c["name"] for c in listed] == ["Grace Hopper", "Jane Doe"]
    assert book.kinds()[-2:] == ["sync_collection", "addressbook_multiget"]

    contacts.update_contact(created["id"], nickname="Amazing Grace")
    entries = contacts.list_contacts(fields=["nickname"])["contacts"]
    assert {c["id"]: c["nickname"] for c in entries}[created["id"]] == "Amazing Grace"

    contacts.delete_contact(created["id"])
    assert [c["name"] for c in contacts.list_contacts()["contacts"]] == ["Jane Doe"]
    assert _entry().dirty == set()


def test_failed_write_leaves_the_cache_untouched(book):
    import requests

    book.put_card("jane", VCARD)
    contacts.list_contacts()
    book.errors["put"] = [500]
    with pytest.raises(requests.HTTPError):
        contacts.create_contact(name="Grace Hopper")
    book.errors["delete"] = [403]
    with pytest.raises(requests.HTTPError):
        contacts.delete_contact(BOOK + "jane.vcf")
    assert _entry().dirty == set()
    book.log.clear()
    assert [c["name"] for c in contacts.list_contacts()["contacts"]] == ["Jane Doe"]
    assert book.kinds() == ["sync_collection"]


# --- search (task 2.3) ---------------------------------------------------------------------------

def seed(monkeypatch, *texts):
    """Serve ``texts`` as the address book through the ``_people`` seam; returns their ids."""
    cards = [contacts.build_card(f"https://p1-contacts.icloud.com/ab/c{i}.vcf", None, text)
             for i, text in enumerate(texts)]
    monkeypatch.setattr(contacts, "_people", lambda session, credentials: [c for c in cards if c.listed])
    return [card.url for card in cards]


def found(query, **kwargs):
    return [c["id"] for c in contacts.search_contacts(query, **kwargs)["contacts"]]


def card_text(name, extra=""):
    return f"BEGIN:VCARD\nVERSION:3.0\nFN:{name}\nN:;{name};;;\n{extra}END:VCARD\n"


def test_search_ignores_accents_case_and_word_order(monkeypatch):
    jose, other = seed(monkeypatch, card_text("José García"), card_text("Josefina Ruiz"))
    assert found("jose garcia") == [jose]
    assert found("GARCIA jose") == [jose]
    assert found("JOSÉ") == [jose, other]


@pytest.mark.parametrize("query,name", [("strasse", "Anna Straße"), ("lukasz", "Łukasz Nowak"),
                                        ("oyvind", "Øyvind Berg"), ("dorde", "Đorđe Petrović"),
                                        ("istanbul", "İstanbul Office"), ("aeron", "Æron Moe")])
def test_search_folds_letters_nfkd_leaves_whole(monkeypatch, query, name):
    (url,) = seed(monkeypatch, card_text(name))
    assert found(query) == [url]


def test_search_ranks_name_prefix_then_name_words_then_other_fields(monkeypatch):
    org_only, name_word, prefix = seed(
        monkeypatch,
        card_text("Ann Lee", "ORG:Jose Industries\n"),
        card_text("Maria Jose"),
        card_text("Jose Alvarez"),
    )
    assert found("jose") == [prefix, name_word, org_only]
    page = contacts.search_contacts("jose", fields=["name"])
    assert [c["name"] for c in page["contacts"]] == ["Jose Alvarez", "Maria Jose", "Ann Lee"]
    assert page["total"] == 3 and page["next_cursor"] is None


@pytest.mark.parametrize("query", ["555 010 4242", "(555) 010-4242", "+1-555-010-4242", "0104242", "4242"])
def test_search_matches_phone_digits_whatever_the_format(monkeypatch, query):
    hit, _ = seed(monkeypatch, card_text("Pat Doe", "TEL:+1 (555) 010-4242\n"), card_text("Kim Roe", "TEL:+1 555 010 9999\n"))
    assert found(query) == [hit]


def test_punctuation_only_words_do_not_match_every_phone(monkeypatch):
    hyphenated, _plain, _phone_only = seed(
        monkeypatch,
        card_text("Mary Smith-Jones", "TEL:+15550100001\n"),
        card_text("Bob Smith", "TEL:+15550100002\n"),
        card_text("Zed Other", "TEL:+15550100003\n"),
    )
    assert found("smith - jones") == [hyphenated]
    assert found("-") == [hyphenated]  # matches the hyphen in the name, not the phones


def test_search_handles_at_usernames_and_rejects_empty_queries(monkeypatch):
    (url,) = seed(monkeypatch, card_text("Sam Lee", "EMAIL:sam@example.com\n"))
    assert found("@sam") == [url]
    for query in ("", "   ", "@", " @ @@ "):
        with pytest.raises(ValueError, match="query must not be empty"):
            contacts.search_contacts(query)


def test_search_rejects_empty_query_before_any_request(book):
    with pytest.raises(ValueError, match="query must not be empty"):
        contacts.search_contacts("  ")
    assert book.log == []


def test_search_pages_with_its_own_cursor(monkeypatch):
    urls = seed(monkeypatch, *(card_text(f"Robin {i:02d}") for i in range(7)))
    first = contacts.search_contacts("robin", page_size=3)
    assert first["total"] == 7 and len(first["contacts"]) == 3
    rest = contacts.search_contacts("  ROBIN ", page_size=10, cursor=first["next_cursor"])  # same words: same query
    assert [c["id"] for c in first["contacts"] + rest["contacts"]] == urls and rest["next_cursor"] is None
    with pytest.raises(ValueError, match="start again without a cursor"):
        contacts.search_contacts("rob", cursor=first["next_cursor"])
    with pytest.raises(ValueError, match="start again without a cursor"):
        contacts.list_contacts(cursor=first["next_cursor"])


def test_search_goes_through_the_cache(book):
    book.put_card("jane", VCARD)
    assert [c["name"] for c in contacts.search_contacts("jane")["contacts"]] == ["Jane Doe"]
    book.put_card("janet", vcard("Janet Roe"))
    book.log.clear()
    assert [c["name"] for c in contacts.search_contacts("jane")["contacts"]] == ["Jane Doe", "Janet Roe"]
    assert book.kinds() == ["sync_collection", "addressbook_multiget"]
