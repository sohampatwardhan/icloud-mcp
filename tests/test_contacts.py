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
    card = contacts._vcard_to_dict(vobject.readOne(VCARD_SOCIAL), "https://x/c.vcf")
    monkeypatch.setattr(contacts, "list_contacts", lambda: [card])
    assert contacts.search_contacts("@samlee") == [card]
    assert contacts.search_contacts("sam-lee-42") == [card]
    assert contacts.search_contacts("@nobody") == []


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
        {"label": "Anniversary", "date": "2015-06-20"},
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
        {"label": "Anniversary", "date": "2019-05-04"},
        {"label": "Adopted Rex", "date": "--11-15"},
        {"label": "Other", "date": "2001-01-01"},
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
        {"label": "Anniversary", "date": "2016-06-20"},
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
    monkeypatch.setattr(contacts, "require_auth", lambda: ("a", "b"))
    monkeypatch.setattr(contacts, "_default_addressbook_url", lambda session: "https://p1-contacts.icloud.com/ab/")
    monkeypatch.setattr(contacts, "_fetch_all_vcards", lambda session, url: [
        {"url": "https://p1-contacts.icloud.com/ab/g.vcf", "data": group, "etag": ""},
        {"url": "https://p1-contacts.icloud.com/ab/c.vcf", "data": VCARD, "etag": ""},
    ])
    assert [c["name"] for c in contacts.list_contacts()] == ["Jane Doe"]


def test_preexisting_orphan_annotations_are_left_alone():
    vcard = vobject.readOne("""BEGIN:VCARD
VERSION:3.0
FN:Jo
item1.TEL:+15550101234
item1.X-ABLabel:Old line
item2.X-ABADR:us
item3.X-ABLabel:stray
END:VCARD""")
    contacts._apply_fields(vcard, ["+15550109999"], None, None, None, None)
    serialized = vcard.serialize()
    assert "Old line" not in serialized  # orphaned by this update: removed
    assert "item2.X-ABADR:us" in serialized and "item3.X-ABLABEL:stray" in serialized


def test_unchanged_birthday_keeps_stored_form():
    vcard = vobject.readOne("BEGIN:VCARD\nVERSION:3.0\nFN:Jo\nBDAY;VALUE=date:1990-01-15\nEND:VCARD")
    before = vcard.serialize()
    contacts._apply_fields(vcard, None, None, None, None, None, birthday="1990-01-15")
    assert vcard.serialize() == before
