"""CardDAV operations for iCloud contacts (direct HTTP/WebDAV, RFC 6352)."""

import datetime
import logging
import re
import uuid
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import requests
import vobject
from defusedxml import ElementTree as ET
from requests.auth import HTTPBasicAuth

from .auth import require_auth
from .config import config
from .mail_utils import clean_rich_text
from .urls import ensure_icloud_url

logger = logging.getLogger(__name__)

NS = {"d": "DAV:", "card": "urn:ietf:params:xml:ns:carddav"}
TIMEOUT = 30


def _get_carddav_session(email: str, password: str) -> requests.Session:
    session = requests.Session()
    session.auth = HTTPBasicAuth(email, password)
    session.headers.update(
        {"Content-Type": "text/xml; charset=utf-8", "User-Agent": "iCloud-MCP/0.2"}
    )
    return session


def _discover_principal(session: requests.Session, base_url: str) -> str:
    body = """<?xml version="1.0" encoding="UTF-8"?>
    <d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>"""
    response = session.request(
        "PROPFIND", base_url, data=body, headers={"Depth": "0"}, timeout=TIMEOUT
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    elem = root.find(".//d:current-user-principal/d:href", NS)
    if elem is not None and elem.text:
        return urljoin(base_url, elem.text)
    raise ValueError("Could not discover CardDAV principal URL")


def _discover_addressbook_home(session: requests.Session, principal_url: str) -> str:
    body = """<?xml version="1.0" encoding="UTF-8"?>
    <d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
        <d:prop><card:addressbook-home-set/></d:prop>
    </d:propfind>"""
    response = session.request(
        "PROPFIND", principal_url, data=body, headers={"Depth": "0"}, timeout=TIMEOUT
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    elem = root.find(".//card:addressbook-home-set/d:href", NS)
    if elem is not None and elem.text:
        return urljoin(principal_url, elem.text)
    raise ValueError("Could not discover addressbook home URL")


def _list_addressbooks(session: requests.Session, home_url: str) -> list[dict[str, str]]:
    body = """<?xml version="1.0" encoding="UTF-8"?>
    <d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
        <d:prop><d:displayname/><d:resourcetype/><card:addressbook-description/></d:prop>
    </d:propfind>"""
    response = session.request(
        "PROPFIND", home_url, data=body, headers={"Depth": "1"}, timeout=TIMEOUT
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)

    addressbooks = []
    for resp in root.findall(".//d:response", NS):
        href = resp.find("d:href", NS)
        rtype = resp.find(".//d:resourcetype", NS)
        if rtype is None or rtype.find("card:addressbook", NS) is None:
            continue
        name = resp.find(".//d:displayname", NS)
        addressbooks.append(
            {
                "url": urljoin(home_url, href.text) if href is not None and href.text else "",
                "name": name.text if name is not None and name.text else "Unnamed",
            }
        )
    return addressbooks


def _default_addressbook_url(session: requests.Session) -> str:
    principal_url = _discover_principal(session, config.CARDDAV_SERVER)
    home_url = _discover_addressbook_home(session, principal_url)
    addressbooks = _list_addressbooks(session, home_url)
    if not addressbooks:
        raise ValueError("No addressbooks found for this account")
    url = addressbooks[0]["url"]
    return url if url.endswith("/") else url + "/"


def _fetch_all_vcards(session: requests.Session, addressbook_url: str) -> list[dict[str, Any]]:
    body = """<?xml version="1.0" encoding="UTF-8"?>
    <card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
        <d:prop><d:getetag/><card:address-data/></d:prop>
    </card:addressbook-query>"""
    response = session.request(
        "REPORT", addressbook_url, data=body, headers={"Depth": "1"}, timeout=TIMEOUT
    )
    response.raise_for_status()

    vcards = []
    root = ET.fromstring(response.content)
    for resp in root.findall(".//d:response", NS):
        href = resp.find("d:href", NS)
        data = resp.find(".//card:address-data", NS)
        etag = resp.find(".//d:getetag", NS)
        if data is not None and data.text:
            vcards.append(
                {
                    "url": urljoin(addressbook_url, href.text) if href is not None and href.text else "",
                    "data": data.text,
                    "etag": etag.text if etag is not None else "",
                }
            )
    return vcards


def _parse_labelled(vcard: Any, prop: str, type_labels: list[tuple[set[str], str]]) -> list[dict[str, str]]:
    """Phones or emails as ``{"label", "value"}``; label is "" when the entry has none."""
    return [
        {"label": _entry_label(vcard, item, type_labels), "value": str(item.value)}
        for item in getattr(vcard, f"{prop}_list", []) or [] if getattr(item, "value", None)
    ]


_ADDRESS_FIELDS = (("street", "street"), ("city", "city"), ("region", "region"),
                   ("postal_code", "code"), ("country", "country"))


def _group_value(vcard: Any, group: str | None, name: str) -> str:
    if not group:
        return ""
    return next((str(child.value) for child in vcard.getChildren()
                 if child.group == group and child.name == name and child.value), "")


def _address_dict(vcard: Any, adr: Any) -> dict[str, str]:
    value = adr.value
    address = {"label": _entry_label(vcard, adr, _HOME_WORK_OTHER)}
    for key, attr in _ADDRESS_FIELDS:
        part = getattr(value, attr, "")
        address[key] = "\n".join(part) if isinstance(part, list) else str(part or "")
    country_code = _group_value(vcard, adr.group, "X-ABADR")
    address["country_code"] = country_code.lower() if re.fullmatch(r"[A-Za-z]{2}", country_code) else ""
    address["formatted"] = _format_address(adr)
    return address


def _parse_addresses(vcard: Any) -> list[dict[str, str]]:
    """Postal addresses as label, street (may contain newlines), city, region,
    postal_code, country, country_code (ISO, from X-ABADR) and a one-line ``formatted``."""
    addresses = []
    for adr in getattr(vcard, "adr_list", []) or []:
        try:
            address = _address_dict(vcard, adr)
        except Exception:
            continue
        if address["formatted"]:
            addresses.append(address)
    return addresses


def _format_address(adr: Any) -> str:
    value = getattr(adr, "value", None)
    if value is None:
        return ""
    parts = [
        getattr(value, "street", ""),
        getattr(value, "city", ""),
        getattr(value, "region", ""),
        getattr(value, "code", ""),
        getattr(value, "country", ""),
    ]
    joined = ", ".join(str(p).replace("\n", " ").strip() for p in parts if p)
    return joined or str(value).replace("\n", " ").strip()


# Apple marks birthdays without a year using X-APPLE-OMIT-YEAR and a sentinel
# year (1604, sometimes 1600) in the BDAY value.
_OMIT_YEAR_SENTINELS = {"1600", "1604"}


def _read_date(prop: Any) -> str:
    """A BDAY/X-ABDATE value as ``YYYY-MM-DD``, or ``--MM-DD`` when the year is omitted."""
    if prop is None or not prop.value:
        return ""
    raw = str(prop.value).strip()
    omit_year = "X-APPLE-OMIT-YEAR" in (getattr(prop, "params", {}) or {})
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", raw)
    if m and (omit_year or m.group(1) in _OMIT_YEAR_SENTINELS):
        return f"--{m.group(2)}-{m.group(3)}"
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else raw


def _write_date(prop: Any, text: str, field: str) -> None:
    """Store ``YYYY-MM-DD`` or ``--MM-DD`` the way Apple Contacts does
    (no year: sentinel year 1604 plus ``X-APPLE-OMIT-YEAR=1604``)."""
    text = text.strip()
    no_year = re.match(r"^--(\d{2})-(\d{2})$", text)
    value = f"1604-{no_year.group(1)}-{no_year.group(2)}" if no_year else text
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be a real date as YYYY-MM-DD or --MM-DD, got {text!r}") from None
    prop.value = value
    prop.params.pop("X-APPLE-OMIT-YEAR", None)
    if no_year:
        prop.params["X-APPLE-OMIT-YEAR"] = ["1604"]


def _parse_birthday(vcard: Any) -> str:
    """Return the BDAY value as ``YYYY-MM-DD``, or ``--MM-DD`` when the year
    is unknown/omitted. Returns ``""`` when no birthday is set."""
    return _read_date(getattr(vcard, "bday", None))


def _parse_dates(vcard: Any) -> list[dict[str, str]]:
    """Other significant dates (X-ABDATE): anniversaries and custom-labelled dates."""
    return [
        {"label": _entry_label(vcard, prop), "date": _read_date(prop)}
        for prop in getattr(vcard, "x_abdate_list", []) or [] if prop.value
    ]


# Apple attaches labels to a property through a shared group: ``item1.URL:...`` plus
# ``item1.X-ABLabel:_$!<HomePage>!$_``. Built-in labels use the ``_$!<...>!$_`` wrapper,
# custom labels are plain text. Properties listed here only annotate their group.
_GROUP_ANNOTATIONS = {"X-ABLABEL", "X-ABADR", "X-APPLE-SUBLOCALITY", "X-APPLE-SUBADMINISTRATIVEAREA"}
_BUILTIN_URL_LABELS = {"homepage": "HomePage", "home": "Home", "work": "Work", "other": "Other"}
_BUILTIN_DATE_LABELS = {"anniversary": "Anniversary", "other": "Other"}
_BUILTIN_SCHOOL_LABEL = {"school": "School"}
_GENERIC_TYPES = {"PREF", "INTERNET", "VOICE"}
# Built-in label tokens whose display name is not simply the lower-cased token.
_BUILTIN_DISPLAY = {"HomeFAX": "home fax", "WorkFAX": "work fax", "OtherFAX": "other fax", "iPhone": "iPhone"}

# Labels Contacts encodes as TYPE parameters instead of X-ABLabel: the TYPE set each
# label is written as, and (in priority order) the TYPEs that make a stored entry read as it.
_TEL_TYPES = {
    "mobile": ["CELL", "VOICE"], "iPhone": ["IPHONE", "CELL", "VOICE"], "home": ["HOME", "VOICE"],
    "work": ["WORK", "VOICE"], "main": ["MAIN"], "home fax": ["HOME", "FAX"], "work fax": ["WORK", "FAX"],
    "other fax": ["OTHER", "FAX"], "pager": ["PAGER"], "other": ["OTHER", "VOICE"],
}
_TEL_TYPE_LABELS = [
    ({"IPHONE"}, "iPhone"), ({"HOME", "FAX"}, "home fax"), ({"WORK", "FAX"}, "work fax"), ({"FAX"}, "other fax"),
    ({"PAGER"}, "pager"), ({"MAIN"}, "main"), ({"CELL"}, "mobile"), ({"HOME"}, "home"), ({"WORK"}, "work"),
    ({"OTHER"}, "other"),
]
_EMAIL_TYPES = {"home": ["INTERNET", "HOME"], "work": ["INTERNET", "WORK"], "other": ["INTERNET", "OTHER"]}
_ADR_TYPES = {"home": ["HOME"], "work": ["WORK"], "other": ["OTHER"]}
_HOME_WORK_OTHER = [({"HOME"}, "home"), ({"WORK"}, "work"), ({"OTHER"}, "other")]


def _decode_label(raw: str) -> str:
    """Display name of an X-ABLabel: built-in ``_$!<Mobile>!$_`` tokens as Contacts shows
    them (``mobile``), custom labels verbatim."""
    m = re.match(r"^_\$!<(.*)>!\$_$", raw)
    if not m:
        return raw
    return _BUILTIN_DISPLAY.get(m.group(1), m.group(1).lower())


def _group_label(vcard: Any, group: str | None) -> str:
    if not group:
        return ""
    for child in vcard.getChildren():
        if child.group == group and child.name == "X-ABLABEL" and child.value:
            return _decode_label(str(child.value))
    return ""


def _entry_label(vcard: Any, prop: Any, type_labels: list[tuple[set[str], str]] | None = None) -> str:
    """Label of a multi-value property: its X-ABLabel, else derived from TYPE (via
    ``type_labels`` when given, else the first specific TYPE)."""
    label = _group_label(vcard, prop.group)
    if label:
        return label
    types = (getattr(prop, "params", {}) or {}).get("TYPE") or []
    if type_labels is not None:
        upper = {str(t).upper() for t in types}
        return next((name for needed, name in type_labels if needed <= upper), "")
    return next((str(t) for t in types if str(t).upper() not in _GENERIC_TYPES), "")


def _next_group(vcard: Any, reserved: Any = ()) -> str:
    """A fresh ``itemN`` group name. ``reserved`` holds groups of properties removed for
    re-adding later in the same update, so a new entry never takes one of them."""
    groups = [child.group for child in vcard.getChildren() if child.group] + list(reserved)
    used = [int(m.group(1)) for group in groups if (m := re.match(r"^item(\d+)$", group, re.I))]
    return f"item{max(used, default=0) + 1}"


def _set_label(vcard: Any, entry: Any, label: str, builtins: dict[str, str]) -> None:
    builtin = builtins.get(label.lower())
    entry.group = _next_group(vcard)
    vcard.add("x-ablabel", group=entry.group).value = f"_$!<{builtin}>!$_" if builtin else label
    if label.lower() in ("home", "work"):
        entry.params["TYPE"] = [label.upper()]


def _orphan_groups(vcard: Any) -> set[str]:
    """Groups holding only annotations (labels), i.e. whose labelled property is gone."""
    groups = {child.group for child in vcard.getChildren() if child.group}
    live = {child.group for child in vcard.getChildren()
            if child.group and child.name not in _GROUP_ANNOTATIONS}
    return groups - live


def _set_typed_label(
    vcard: Any, entry: Any, label: str, types: dict[str, list[str]], tokens: dict[str, str],
    base_types: list[str], pref: bool = False, reserved: Any = (),
) -> None:
    """Write a label the way Contacts does: as TYPE parameters when it has one
    (``mobile`` -> CELL,VOICE), else as an X-ABLabel in a group (built-in token or text).
    ``reserved`` is passed to ``_next_group``."""
    by_lower = {name.lower(): value for name, value in types.items()}
    entry.params.pop("TYPE", None)
    type_values = by_lower.get(label.lower())
    if type_values is None:
        type_values = list(base_types)
        entry.group = _next_group(vcard, reserved)
        token = tokens.get(label.lower())
        vcard.add("x-ablabel", group=entry.group).value = f"_$!<{token}>!$_" if token else label
    if pref:
        type_values = [*type_values, "pref"]
    if type_values:
        entry.params["TYPE"] = list(type_values)


def _remove_orphan_groups(vcard: Any, keep: set[str] = frozenset()) -> None:
    """Drop group annotations whose labelled property was removed. Groups in ``keep``
    were already orphaned when the card was read (iCloud has such lines) and stay."""
    orphans = _orphan_groups(vcard) - keep
    for child in list(vcard.getChildren()):
        if child.group in orphans:
            vcard.remove(child)


# Known services: TYPE as Apple Contacts writes it, and its profile URL template.
# Profiles on other services are stored the way Contacts does it: ``x-apple:<username>``.
_SOCIAL_SERVICES = {
    "facebook": ("facebook", "https://www.facebook.com/{}"),
    "instagram": ("Instagram", "https://www.instagram.com/{}/"),
    "snapchat": ("Snapchat", "https://snapchat.com/add/{}"),
    "linkedin": ("linkedin", "https://www.linkedin.com/in/{}"),
    "twitter": ("twitter", "https://twitter.com/{}"),
    "tiktok": ("TikTok", "https://www.tiktok.com/@{}"),
}
_SOCIAL_ALIASES = {
    "x": "twitter", "fb": "facebook", "ig": "instagram",
    "facebook.com": "facebook", "fb.com": "facebook", "instagram.com": "instagram",
    "snapchat.com": "snapchat", "linkedin.com": "linkedin", "twitter.com": "twitter",
    "x.com": "twitter", "tiktok.com": "tiktok",
}


def _social_service(name: str, url: str = "") -> str:
    """Canonical lower-case service name; inferred from the URL host when ``name`` is empty."""
    key = name.strip().lower().removeprefix("www.")
    if not key:
        key = urlsplit(url).netloc.lower().removeprefix("www.")
        return _SOCIAL_ALIASES.get(key, "")
    return _SOCIAL_ALIASES.get(key, key)


def _username_from_url(service: str, url: str) -> str:
    if url.lower().startswith("x-apple:"):
        return url[len("x-apple:"):]
    if service not in _SOCIAL_SERVICES:
        return ""
    segments = [seg for seg in urlsplit(url).path.split("/") if seg]
    if service in ("linkedin", "snapchat") and len(segments) >= 2 and segments[0] in ("in", "pub", "add"):
        return segments[1]
    if segments and segments[0] not in ("profile.php", "in", "pub", "add"):
        return segments[0].lstrip("@")
    return ""


def _social_profile(prop: Any) -> dict[str, str]:
    params = getattr(prop, "params", {}) or {}
    url = str(prop.value or "")
    service = _social_service(str((params.get("TYPE") or [""])[0]), url)
    profile = {
        "service": service,
        "username": str((params.get("X-USER") or [""])[0]) or _username_from_url(service, url),
        "url": url,
    }
    if params.get("X-USERID"):
        profile["user_id"] = str(params["X-USERID"][0])
    return profile


def _parse_social_profiles(vcard: Any) -> list[dict[str, str]]:
    """Parse X-SOCIALPROFILE properties into service, username, url (and user_id when set)."""
    return [_social_profile(prop) for prop in getattr(vcard, "x_socialprofile_list", []) or []]


def _parse_urls(vcard: Any) -> list[dict[str, str]]:
    """Parse URL properties (website links with optional labels)."""
    return [
        {"url": str(prop.value or ""), "label": _entry_label(vcard, prop)}
        for prop in getattr(vcard, "url_list", []) or []
    ]


def _set_birthday(vcard: Any, birthday: str) -> None:
    """Set or replace the BDAY property (``YYYY-MM-DD``, or ``--MM-DD`` without a year);
    an empty string clears it."""
    if not birthday:
        if hasattr(vcard, "bday"):
            vcard.remove(vcard.bday)
        return
    if hasattr(vcard, "bday") and _read_date(vcard.bday) == birthday.strip():
        return  # unchanged: keep the stored form (VALUE=date, Apple's no-year marker)
    prop = vcard.bday if hasattr(vcard, "bday") else vcard.add("bday")
    _write_date(prop, birthday, "birthday")


def _set_dates(vcard: Any, dates: list[dict[str, str]]) -> None:
    """Replace X-ABDATE properties.

    Each entry is ``{"label": ..., "date": ...}`` (the shape ``_parse_dates`` returns);
    label is Anniversary, Other or custom text (default Other). An unchanged date
    without a label (or with its current label) is kept as is.
    """
    existing = [(prop, _entry_label(vcard, prop), _read_date(prop))
                for prop in getattr(vcard, "x_abdate_list", []) or []]
    for prop, _, _ in existing:
        vcard.remove(prop)
    for item in dates:
        label = str(item.get("label") or "").strip()
        date = str(item.get("date") or "").strip()
        match = next((entry for entry in existing if entry[2] == date
                      and label.lower() in ("", entry[1].lower())), None)
        if match:
            existing.remove(match)
            vcard.add(match[0])
            continue
        prop = vcard.add("x-abdate")
        _write_date(prop, date, "date")
        _set_label(vcard, prop, label or "Other", _BUILTIN_DATE_LABELS)


def _set_urls(vcard: Any, urls: list[dict[str, str]]) -> None:
    """Replace URL (website) properties.

    Each entry is ``{"url": ..., "label": ...}`` (the shape ``_parse_urls`` returns).
    An unchanged URL without a ``label`` (or with its current label) is kept as is.
    """
    existing = {
        str(prop.value): (dict(prop.params), prop.group, _entry_label(vcard, prop))
        for prop in getattr(vcard, "url_list", []) or [] if prop.value
    }
    for prop in list(getattr(vcard, "url_list", []) or []):
        vcard.remove(prop)
    for item in urls:
        url = str(item.get("url") or "").strip()
        label = str(item.get("label") or "").strip()
        if not url:
            continue
        prop = vcard.add("url")
        prop.value = url
        if url in existing and label.lower() in ("", existing[url][2].lower()):
            params, group, _ = existing[url]
            prop.params.update(params)
            prop.group = group
        elif label:
            _set_label(vcard, prop, label, _BUILTIN_URL_LABELS)


def _match_social_profile(
    existing: list[tuple[Any, dict[str, str]]], service: str, username: str, url: str
) -> tuple[Any, dict[str, str]] | None:
    """Existing profile a new entry refers to. Several services can share one value
    (Signal and WhatsApp on ``x-apple:+phone``), so url+service wins over url alone."""
    rules = []
    if url:
        rules.append(lambda old: old["url"] == url and service in ("", old["service"]))
        rules.append(lambda old: old["url"] == url)
    if service and username:
        rules.append(lambda old: old["service"] == service and old["username"].lower() == username.lower())
    for rule in rules:
        for pair in existing:
            if rule(pair[1]):
                return pair
    return None


def _set_social_profiles(vcard: Any, profiles: list[dict[str, str]]) -> None:
    """Replace X-SOCIALPROFILE properties.

    Each profile is ``{"service", "username", "url"}`` (the shape reads return); a
    service plus username is enough, the URL is then built the way Contacts builds it.
    A profile matching an existing one (same url, or same service and username) that
    changes nothing is kept verbatim; a changed one keeps its other parameters
    (X-USERID, display name, label group).
    """
    existing = [(prop, _social_profile(prop)) for prop in getattr(vcard, "x_socialprofile_list", []) or []]
    for prop, _ in existing:
        vcard.remove(prop)
    for profile in profiles:
        raw_service = str(profile.get("service") or "").strip()
        url = str(profile.get("url") or "").strip()
        service = _social_service(raw_service, url)
        username = str(profile.get("username") or "").strip().lstrip("@")
        user_id = str(profile.get("user_id") or "").strip()
        if not url and not username:
            raise ValueError(f"social profile needs a url or a username: {profile!r}")

        match = _match_social_profile(existing, service, username, url)
        old: dict[str, str] = {}
        if match:
            existing.remove(match)
            old_prop, old = match
            if all(not new or new == old.get(key, "") for key, new in
                   (("service", service), ("username", username), ("url", url), ("user_id", user_id))):
                vcard.add(old_prop)
                continue

        if not url:
            template = _SOCIAL_SERVICES.get(service, ("", ""))[1]
            url = template.format(username) if template else f"x-apple:{username}"
        prop = vcard.add("x-socialprofile", group=match[0].group if match else None)
        prop.value = url
        if match:
            prop.params.update(dict(match[0].params))
        if service and service != old.get("service"):
            prop.params["TYPE"] = [_SOCIAL_SERVICES.get(service, (raw_service, ""))[0]]
        if username:
            prop.params["X-USER"] = [username]
        if user_id:
            prop.params["X-USERID"] = [user_id]


def _vcard_to_dict(vcard: Any, url: str) -> dict[str, Any]:
    def text(attr: str) -> str:
        prop = getattr(vcard, attr, None)
        return str(prop.value) if prop is not None and prop.value else ""

    contact: dict[str, Any] = {
        "id": url,
        "url": url,
        "name": _display_name(vcard),
        "phones": [],
        "emails": [],
        "addresses": [],
        "organization": "",
        "title": text("title"),
        "notes": clean_rich_text(text("note")),
        "birthday": _parse_birthday(vcard),
        "dates": _parse_dates(vcard),
        "urls": _parse_urls(vcard),
        "social_profiles": _parse_social_profiles(vcard),
    }
    contact.update(_parse_name_parts(vcard))
    contact["is_company"] = _is_company(vcard)
    contact["related_names"] = _parse_related_names(vcard)
    contact["instant_messages"] = _parse_instant_messages(vcard)
    contact.update(_parse_apple_extras(vcard))
    contact["organization"], contact["department"] = _org_components(vcard)[:2]
    for key, prop_name in _TEXT_FIELDS.items():
        contact[key] = _text_property(vcard, prop_name)
    contact["phones"] = _parse_labelled(vcard, "tel", _TEL_TYPE_LABELS)
    contact["emails"] = _parse_labelled(vcard, "email", _HOME_WORK_OTHER)
    contact["addresses"] = _parse_addresses(vcard)
    return contact


def list_contacts(limit: int | None = None) -> list[dict[str, Any]]:
    email, password = require_auth()
    session = _get_carddav_session(email, password)
    addressbook_url = _default_addressbook_url(session)

    result = []
    for item in _fetch_all_vcards(session, addressbook_url):
        if limit and len(result) >= limit:
            break
        try:
            vcard = vobject.readOne(item["data"])
        except Exception as e:
            logger.debug("Skipping unparsable vCard %s: %s", item["url"], e)
            continue
        if _is_group(vcard):
            continue
        contact = _vcard_to_dict(vcard, item["url"])
        if contact["name"] or contact["phones"] or contact["emails"]:
            result.append(contact)
    return result


def get_contact(contact_id: str) -> dict[str, Any]:
    contact_id = ensure_icloud_url(contact_id, "contact")
    email, password = require_auth()
    session = _get_carddav_session(email, password)
    response = session.get(contact_id, timeout=TIMEOUT)
    response.raise_for_status()
    return _vcard_to_dict(vobject.readOne(response.text), contact_id)


# Single-value text fields: contact key -> vCard property (Apple's X- names for the phonetic
# and maiden names). ``department`` is not here: it lives in the second ``ORG`` component.
_TEXT_FIELDS = {
    "phonetic_first_name": "x-phonetic-first-name",
    "phonetic_middle_name": "x-phonetic-middle-name",
    "phonetic_last_name": "x-phonetic-last-name",
    "nickname": "nickname",
    "maiden_name": "x-maidenname",
    "phonetic_organization": "x-phonetic-org",
}


def _text_property(vcard: Any, prop_name: str) -> str:
    """First value of a single-value text property, or "" when the card has none."""
    props = vcard.contents.get(prop_name, [])
    if not props or props[0].value is None:
        return ""
    value = props[0].value
    return ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)


def _set_text_property(vcard: Any, prop_name: str, value: str | None) -> None:
    """Apply one single-value text field: ``None`` leaves it alone, ``""`` removes every
    instance, other text replaces it. Text equal to the stored value is a no-op so a
    read-then-write-back never rewrites the line."""
    if value is None or value == _text_property(vcard, prop_name):
        return
    for prop in list(vcard.contents.get(prop_name, [])):
        vcard.remove(prop)
    if value:
        vcard.add(prop_name).value = value


def _org_components(vcard: Any) -> list[str]:
    """``ORG`` as a list of at least two components (organization, department, …)."""
    org = getattr(vcard, "org", None)
    if org is None or org.value is None:
        components: list[str] = []
    elif isinstance(org.value, list):
        components = [str(v) for v in org.value]
    else:
        components = [str(org.value)]
    return components + [""] * (2 - len(components))


def _set_org(vcard: Any, organization: str | None, department: str | None) -> None:
    """Edit the organization (``ORG`` component 0) and department (component 1) in place.

    Whichever one is not provided keeps its stored value, and components from index 2 on
    (sub-units some clients write) are never touched: replacing the whole value is what used
    to wipe the department on every organization update. The value is always assigned as a
    list because vobject splits a plain string into one component per character.
    """
    if organization is None and department is None:
        return
    current = _org_components(vcard)
    updated = list(current)
    if organization is not None:
        updated[0] = organization
    if department is not None:
        updated[1] = department
    if updated == current:
        return
    if not any(part.strip() for part in updated):
        if hasattr(vcard, "org"):
            vcard.remove(vcard.org)
        return
    stored = getattr(vcard, "org", None)
    stored_len = len(stored.value) if stored is not None and isinstance(stored.value, list) else 1
    while len(updated) > max(stored_len, 1) and not updated[-1]:
        updated.pop()  # keep the stored component count; drop only padding this function added
    (stored if stored is not None else vcard.add("org")).value = updated


# Contact keys for the structured name, mapped to vobject ``Name`` attributes.
_NAME_PARTS = {"prefix": "prefix", "first_name": "given", "middle_name": "additional",
               "last_name": "family", "suffix": "suffix"}


def _parse_name_parts(vcard: Any) -> dict[str, str]:
    """The five ``N`` components; multi-value components are joined with a space."""
    n = getattr(vcard, "n", None)
    parts = {}
    for key, attr in _NAME_PARTS.items():
        value = getattr(n.value, attr, "") if n is not None else ""
        text = " ".join(str(v) for v in value) if isinstance(value, list) else str(value or "")
        parts[key] = text.strip()  # some stored N parts carry stray spaces, e.g. "Doe ;Jane"
    return parts


def _joined_parts(n_value: Any) -> str:
    pieces = [getattr(n_value, attr) for attr in ("prefix", "given", "additional", "family", "suffix")]
    return " ".join(" ".join(p) if isinstance(p, list) else str(p) for p in pieces if p).strip()


def _strip_tokens(tokens: list[str], known: list[str], where: str) -> list[str]:
    """Remove one occurrence of ``known`` (a stored name part) from ``tokens``."""
    k = len(known)
    if not k or len(tokens) <= k:
        return tokens
    if where == "start":
        return tokens[k:] if tokens[:k] == known else tokens
    if where == "end":
        return tokens[:-k] if tokens[-k:] == known else tokens
    for i in range(1, len(tokens) - k):  # interior only: never the first or last token
        if tokens[i:i + k] == known:
            return tokens[:i] + tokens[i + k:]
    return tokens


def _apply_name(vcard: Any, name: str | None, parts: dict[str, str | None]) -> None:
    """Apply a display name and/or structured name parts without losing stored parts.

    Values equal to what a read returns (``name`` equal to ``_display_name``, a part equal
    to ``_parse_name_parts``) are treated as omitted, so a read-then-write-back never
    rewrites ``N`` or ``FN``. Then:

    1. Given parts are set in place on ``N`` (``""`` clears one); other parts, including
       multi-value ones, are left exactly as stored.
    2. With only ``name``, it becomes ``FN`` and is split into first and last name after
       removing tokens that match the stored prefix, suffix and middle name, which are
       kept. A stored multi-word first or last name that the text still contains is
       kept intact. The old behaviour replaced the whole ``N``, wiping prefix, middle
       name and suffix on every rename.
    3. With changed parts and no ``name``, ``FN`` is recomputed from the parts.
    4. With both, ``FN`` is ``name`` and the parts follow rule 1.
    """
    current = _parse_name_parts(vcard)
    if name is not None and (not name.strip() or name.strip() == _display_name(vcard)):
        name = None
    changed = {k: v.strip() for k, v in parts.items() if v is not None and v.strip() != current[k]}
    if name is None and not changed:
        return
    if not hasattr(vcard, "n"):
        vcard.add("n").value = vobject.vcard.Name()
    n_value = vcard.n.value
    if changed:
        for key, value in changed.items():
            setattr(n_value, _NAME_PARTS[key], value)
    else:
        tokens = name.split()
        tokens = _strip_tokens(tokens, current["prefix"].split(), "start")
        tokens = _strip_tokens(tokens, current["suffix"].split(), "end")
        tokens = _strip_tokens(tokens, current["middle_name"].split(), "middle")
        stored_last, stored_first = current["last_name"].split(), current["first_name"].split()
        if len(stored_last) > 1 and len(tokens) > len(stored_last) and tokens[-len(stored_last):] == stored_last:
            first, last = tokens[:-len(stored_last)], stored_last
        elif len(stored_first) > 1 and len(tokens) > len(stored_first) and tokens[:len(stored_first)] == stored_first:
            first, last = stored_first, tokens[len(stored_first):]
        elif len(tokens) >= 2:
            first, last = tokens[:-1], tokens[-1:]
        else:
            first, last = tokens, []
        n_value.given, n_value.family = " ".join(first), " ".join(last)
    full_name = name.strip() if name is not None else _joined_parts(n_value)
    if hasattr(vcard, "fn"):
        vcard.fn.value = full_name
    else:
        vcard.add("fn").value = full_name


def _is_company(vcard: Any) -> bool:
    """Whether the card displays as a company (Apple's ``X-ABShowAs:COMPANY``)."""
    return any(str(prop.value or "").strip().upper() == "COMPANY"
               for prop in vcard.contents.get("x-abshowas", []))


def _set_company_flag(vcard: Any, is_company: bool | None) -> None:
    """Mark the card to display as a company (``True``) or a person (``False``).

    Contacts shows a card marked ``X-ABShowAs:COMPANY`` under its organization instead of
    the person's name. ``False`` removes only a ``COMPANY`` value, leaving any other
    ``X-ABShowAs`` value a client may have written; ``None`` or the current value is a
    no-op.
    """
    if is_company is None or bool(is_company) == _is_company(vcard):
        return
    if is_company:
        vcard.add("x-abshowas").value = "COMPANY"
        return
    for prop in list(vcard.contents.get("x-abshowas", [])):
        if str(prop.value or "").strip().upper() == "COMPANY":
            vcard.remove(prop)


# Apple's built-in relationship labels (written as ``_$!<Token>!$_``); anything else is
# stored as custom label text.
_BUILTIN_RELATED_LABELS = {
    label: label.capitalize()
    for label in ("mother", "father", "parent", "brother", "sister", "child", "friend",
                  "spouse", "partner", "assistant", "manager", "other")
}


def _parse_related_names(vcard: Any) -> list[dict[str, str]]:
    """Related people (X-ABRELATEDNAMES) as ``{"label", "name"}``. Built-in labels read as
    Contacts shows them (``mother``); custom or damaged label text is returned verbatim."""
    return [
        {"label": _entry_label(vcard, prop, []), "name": str(prop.value or "")}
        for prop in vcard.contents.get("x-abrelatednames", [])
    ]


def _set_related_names(vcard: Any, items: list[dict[str, str]]) -> None:
    """Replace the related names with ``items`` (the shape reads return).

    An entry whose name exactly matches a stored one and whose label is omitted or equal
    is re-added verbatim, so echoing a read changes nothing. Other entries are written
    with Contacts' label encoding (a new entry without a label gets ``other``); a
    relabelled entry keeps ``pref``. Groups of removed entries stay reserved so new
    labels never collide with an entry re-added later in the list.
    """
    existing = [(prop, {"label": _entry_label(vcard, prop, []), "name": str(prop.value or "")})
                for prop in vcard.contents.get("x-abrelatednames", [])]
    reserved = {prop.group for prop, _ in existing if prop.group}
    for prop, _ in existing:
        vcard.remove(prop)
    for raw in items:
        item = _as_item(raw, "name")
        name = str(item.get("name") or "").strip()
        label = str(item.get("label") or "").strip()
        match = next((pair for pair in existing if pair[1]["name"].strip() == name), None)
        if match:
            existing.remove(match)
            if label.lower() in ("", match[1]["label"].lower()):
                vcard.add(match[0])
                continue
        prop = vcard.add("x-abrelatednames")
        prop.value = name
        _set_typed_label(vcard, prop, label or "other", {}, _BUILTIN_RELATED_LABELS, [],
                         pref=bool(match) and _is_pref(match[0]), reserved=reserved)


# Instant-message services Contacts knows: lower-case key -> (X-SERVICE-TYPE, URI scheme).
# Any other service is stored as Contacts stores it, with an ``x-apple:`` value.
_IM_SERVICES = {
    "aim": ("AIM", "aim"), "icq": ("ICQ", "aim"), "jabber": ("Jabber", "xmpp"),
    "googletalk": ("GoogleTalk", "xmpp"), "facebook": ("Facebook", "xmpp"),
    "msn": ("MSN", "msnim"), "yahoo": ("Yahoo", "ymsgr"), "skype": ("Skype", "skype"),
}
_IM_SCHEME_SERVICES = {"aim": "AIM", "xmpp": "Jabber", "msnim": "MSN", "ymsgr": "Yahoo", "skype": "Skype"}
_IM_TYPES = {"home": ["HOME"], "work": ["WORK"], "other": ["OTHER"]}


def _impp_entry(vcard: Any, prop: Any) -> dict[str, str]:
    value = str(prop.value or "")
    scheme, sep, rest = value.partition(":")
    if sep and re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*", scheme):
        username = unquote(rest)
    else:
        scheme, username = "", value
    service = str((prop.params.get("X-SERVICE-TYPE") or [""])[0] or "")
    if not service:
        service = _IM_SCHEME_SERVICES.get(scheme.lower(), "")
    return {"label": _entry_label(vcard, prop, _HOME_WORK_OTHER), "service": service, "username": username}


def _parse_instant_messages(vcard: Any) -> list[dict[str, str]]:
    """Instant-message accounts (IMPP) as ``{"label", "service", "username"}``.

    ``service`` comes from ``X-SERVICE-TYPE``, or from the URI scheme when that parameter
    is missing (``skype:`` -> Skype); ``username`` is the percent-decoded text after the
    first colon.
    """
    return [_impp_entry(vcard, prop) for prop in vcard.contents.get("impp", [])]


def _set_instant_messages(vcard: Any, items: list[dict[str, str]]) -> None:
    """Replace the instant-message accounts with ``items`` (the shape reads return).

    Entries are matched to stored ones on (service, username), case-insensitively for the
    service. A match whose label is omitted or equal is re-added verbatim, which keeps
    Apple's ``X-BUNDLEIDENTIFIERS``/``X-TEAMIDENTIFIER`` app links that a rewrite could not
    reproduce; a relabelled match keeps its value and other parameters. New entries use
    the service's URI scheme (``x-apple:`` for unknown services) and default to ``other``.
    """
    existing = [(prop, _impp_entry(vcard, prop)) for prop in vcard.contents.get("impp", [])]
    reserved = {prop.group for prop, _ in existing if prop.group}
    for prop, _ in existing:
        vcard.remove(prop)
    for raw in items:
        item = dict(raw)
        service = str(item.get("service") or "").strip()
        username = str(item.get("username") or "").strip()
        label = str(item.get("label") or "").strip()
        match = next((pair for pair in existing if pair[1]["service"].lower() == service.lower()
                      and pair[1]["username"] == username), None)
        if match:
            existing.remove(match)
            if label.lower() in ("", match[1]["label"].lower()):
                vcard.add(match[0])
                continue
        prop = vcard.add("impp")
        if match:
            prop.value = match[0].value
            prop.params.update({k: v for k, v in match[0].params.items() if k != "TYPE"})
        else:
            known = _IM_SERVICES.get(service.lower().replace(" ", ""))
            display, scheme = known if known else (service, "x-apple")
            prop.value = f"{scheme}:{quote(username, safe='@.+-_')}"
            prop.params["X-SERVICE-TYPE"] = [display]
        _set_typed_label(vcard, prop, label or "other", _IM_TYPES, {}, [],
                         pref=bool(match) and _is_pref(match[0]), reserved=reserved)


# Sound schemes whose value is the name Contacts shows. Other forms (purchased tones are
# stored as store identifiers) cannot be turned into a name without device data.
_NAMED_SOUND_SCHEMES = {"system", "texttone"}
_ALERT_KEYS = {"call": "ringtone", "text": "text_tone"}


def _parse_apple_extras(vcard: Any) -> dict[str, Any]:
    """Read-only Apple fields whose storage format was confirmed on a real device.

    Encodings were recorded from a throwaway iOS 27 card (see the spec's format
    investigation) and only those exact shapes are decoded; anything else is left out
    rather than guessed:

    - ``alternate_birthday``: ``X-ALTBDAY;CALSCALE=<calendar>:--MMDD`` or ``YYYYMMDD``,
      the birthday in another calendar (Chinese, Hebrew, Hijri, Korean, Vietnamese), as
      ``{"calendar", "year", "month", "day"}`` with ``year`` ``None`` when not stored.
    - ``ringtone`` / ``text_tone``: ``X-ACTIVITY-ALERT:type=call|text,snd=<scheme>:<name>``.

    Pronouns (``X-ADDRESSING-GRAMMAR``) are opaque and never read; the verification code
    is not stored in the vCard at all.
    """
    extras: dict[str, Any] = {}
    for prop in vcard.contents.get("x-altbday", []):
        calendar = str((prop.params.get("CALSCALE") or [""])[0] or "").strip().lower()
        m = re.fullmatch(r"(?:(\d{4})|--)(\d{2})(\d{2})", str(prop.value or "").strip())
        if calendar and m:
            extras["alternate_birthday"] = {"calendar": calendar,
                                            "year": int(m.group(1)) if m.group(1) else None,
                                            "month": int(m.group(2)), "day": int(m.group(3))}
            break
    for prop in vcard.contents.get("x-activity-alert", []):
        settings = dict(part.partition("=")[::2]
                        for part in str(prop.value or "").replace("\\,", ",").split(","))
        key = _ALERT_KEYS.get(settings.get("type", "").strip().lower())
        scheme, _, sound = settings.get("snd", "").partition(":")
        if key and sound.strip() and scheme.strip().lower() in _NAMED_SOUND_SCHEMES:
            extras.setdefault(key, sound.strip())
    return extras


def _validate_fields(vcard: Any, **fields: Any) -> None:
    """Reject invalid input before anything on the card changes.

    Runs first in ``_apply_fields`` and never mutates the vCard, so a ``ValueError`` means
    nothing was modified and nothing will be sent. Field-specific checks live here.
    """
    if fields.get("is_company"):
        organization = fields.get("organization")
        resulting = organization if organization is not None else _org_components(vcard)[0]
        if not resulting.strip():
            raise ValueError("is_company needs an organization")
    stored_related = {str(p.value or "").strip() for p in vcard.contents.get("x-abrelatednames", [])}
    for raw in fields.get("related_names") or []:
        item = _as_item(raw, "name")
        if not str(item.get("name") or "").strip() and "" not in stored_related:
            raise ValueError(f"related name entry needs a name: {raw!r}")
    stored_im = {(e["service"].lower(), e["username"]) for e in _parse_instant_messages(vcard)}
    for raw in fields.get("instant_messages") or []:
        service = str(raw.get("service") or "").strip()
        username = str(raw.get("username") or "").strip()
        if (not service or not username) and (service.lower(), username) not in stored_im:
            raise ValueError(f"instant message entry needs a service and a username: {raw!r}")


def _apply_fields(
    vcard: Any,
    phones: list[dict[str, str]] | None = None,
    emails: list[dict[str, str]] | None = None,
    addresses: list[dict[str, str]] | None = None,
    organization: str | None = None,
    title: str | None = None,
    notes: str | None = None,
    birthday: str | None = None,
    urls: list[dict[str, str]] | None = None,
    social_profiles: list[dict[str, str]] | None = None,
    dates: list[dict[str, str]] | None = None,
    *,
    name: str | None = None,
    prefix: str | None = None,
    first_name: str | None = None,
    middle_name: str | None = None,
    last_name: str | None = None,
    suffix: str | None = None,
    phonetic_first_name: str | None = None,
    phonetic_middle_name: str | None = None,
    phonetic_last_name: str | None = None,
    nickname: str | None = None,
    maiden_name: str | None = None,
    department: str | None = None,
    phonetic_organization: str | None = None,
    is_company: bool | None = None,
    related_names: list[dict[str, str]] | None = None,
    instant_messages: list[dict[str, str]] | None = None,
) -> None:
    """Apply partial updates. ``None`` leaves a field unchanged everywhere, ``""`` clears a
    text field; list fields replace existing values, and entries whose value is unchanged
    keep their original parameters (e.g. TYPE=HOME, custom iOS labels)."""
    text_fields = {
        "phonetic_first_name": phonetic_first_name, "phonetic_middle_name": phonetic_middle_name,
        "phonetic_last_name": phonetic_last_name, "nickname": nickname, "maiden_name": maiden_name,
        "phonetic_organization": phonetic_organization,
    }
    _validate_fields(vcard, organization=organization, department=department, is_company=is_company,
                     related_names=related_names, instant_messages=instant_messages, **text_fields)
    already_orphaned = _orphan_groups(vcard)
    _apply_name(vcard, name, {"prefix": prefix, "first_name": first_name, "middle_name": middle_name,
                              "last_name": last_name, "suffix": suffix})
    if phones is not None:
        _set_labelled(vcard, "tel", phones, _TEL_TYPE_LABELS, _TEL_TYPES, ["VOICE"], default_label="mobile")
    if emails is not None:
        _set_labelled(vcard, "email", emails, _HOME_WORK_OTHER, _EMAIL_TYPES, ["INTERNET"], default_label="")
    if addresses is not None:
        _set_addresses(vcard, addresses)
    _set_org(vcard, organization, department)
    for key, value in text_fields.items():
        _set_text_property(vcard, _TEXT_FIELDS[key], value)
    _set_company_flag(vcard, is_company)
    if related_names is not None:
        _set_related_names(vcard, related_names)
    if instant_messages is not None:
        _set_instant_messages(vcard, instant_messages)
    if title is not None:
        if hasattr(vcard, "title"):
            vcard.title.value = title
        else:
            vcard.add("title").value = title
    if notes is not None:
        if hasattr(vcard, "note"):
            vcard.note.value = notes
        else:
            vcard.add("note").value = notes
    if birthday is not None:
        _set_birthday(vcard, birthday)
    if urls is not None:
        _set_urls(vcard, urls)
    if social_profiles is not None:
        _set_social_profiles(vcard, social_profiles)
    if dates is not None:
        _set_dates(vcard, dates)
    _remove_orphan_groups(vcard, keep=already_orphaned)


def _normalized(value: str) -> str:
    return "".join(ch for ch in str(value).lower() if ch not in " -()\u00a0")


def _as_item(item: Any, key: str) -> dict[str, str]:
    """Accept a bare string where a ``{key: ...}`` object is expected."""
    return {key: item} if isinstance(item, str) else dict(item)


def _is_pref(prop: Any) -> bool:
    return any(str(t).upper() == "PREF" for t in (prop.params.get("TYPE") or []))


def _set_labelled(
    vcard: Any, prop: str, items: list[dict[str, str]], type_labels: list[tuple[set[str], str]],
    types: dict[str, list[str]], base_types: list[str], default_label: str,
) -> None:
    """Replace phones or emails given as ``{"label", "value"}``.

    An entry whose value is unchanged and whose label is omitted or unchanged is kept
    verbatim (parameters, group and X-ABLabel). Otherwise the label is written the way
    Contacts writes it; a new entry without a label gets ``default_label``.
    """
    existing = [(item, _entry_label(vcard, item, type_labels))
                for item in getattr(vcard, f"{prop}_list", []) or [] if item.value]
    for item, _ in existing:
        vcard.remove(item)
    for raw in items:
        item = _as_item(raw, "value")
        value = str(item.get("value") or "").strip()
        label = str(item.get("label") or "").strip()
        if not value:
            raise ValueError(f"{prop} entry needs a value: {raw!r}")
        match = next((pair for pair in existing if _normalized(pair[0].value) == _normalized(value)), None)
        if match:
            existing.remove(match)
            if match[0].value == value and label.lower() in ("", match[1].lower()):
                vcard.add(match[0])
                continue
        entry = vcard.add(prop)
        entry.value = value
        pref = bool(match) and _is_pref(match[0])
        if match and not label:
            entry.params.update(dict(match[0].params))  # formatting change only: keep label
            entry.group = match[0].group
        elif label or default_label:
            _set_typed_label(vcard, entry, label or default_label, types, _BUILTIN_SCHOOL_LABEL, base_types, pref)
        else:
            entry.params["TYPE"] = list(base_types)


def _set_addresses(vcard: Any, addresses: list[dict[str, str]]) -> None:
    """Replace postal addresses given as ``{"label", "street", "city", "region",
    "postal_code", "country", "country_code"}`` (a bare string is taken as the street).

    An address whose parts are unchanged and whose label/country_code are omitted or
    unchanged is kept verbatim (including Apple's X-ABADR and sub-locality lines).
    New or changed addresses default to the ``home`` label like Contacts does.
    """
    existing = [(adr, _address_dict(vcard, adr)) for adr in getattr(vcard, "adr_list", []) or []]
    for adr, _ in existing:
        vcard.remove(adr)
    for raw in addresses:
        item = _as_item(raw, "street")
        parts = {key: str(item.get(key) or "").strip() for key, _ in _ADDRESS_FIELDS}
        label = str(item.get("label") or "").strip()
        country_code = str(item.get("country_code") or "").strip().lower()
        if not any(parts.values()):
            raise ValueError(f"address needs at least one of street, city, region, postal_code, country: {raw!r}")
        if country_code and not re.fullmatch(r"[a-z]{2}", country_code):
            raise ValueError(f"country_code must be a two-letter ISO code, got {country_code!r}")
        match = next((pair for pair in existing
                      if all(pair[1][key].strip() == parts[key] for key, _ in _ADDRESS_FIELDS)), None)
        if match:
            existing.remove(match)
            old = match[1]
            if label.lower() in ("", old["label"].lower()) and country_code in ("", old["country_code"]):
                vcard.add(match[0])  # its group keeps the X-ABADR / sub-locality lines
                continue
        adr = vcard.add("adr")
        adr.value = vobject.vcard.Address(**{attr: parts[key] for key, attr in _ADDRESS_FIELDS})
        _set_typed_label(vcard, adr, label or "home", _ADR_TYPES, _BUILTIN_SCHOOL_LABEL, [],
                         bool(match) and _is_pref(match[0]))
        if country_code:
            if not adr.group:
                adr.group = _next_group(vcard)
            vcard.add("x-abadr", group=adr.group).value = country_code


def _display_name(vcard: Any) -> str:
    """The name Contacts shows: FN, else the structured name, else the organization.
    iCloud often stores an empty FN and leaves the display name to the client;
    company cards (X-ABShowAs:COMPANY) show the organization first."""
    fn = getattr(vcard, "fn", None)
    if fn is not None and str(fn.value or "").strip():
        return str(fn.value).strip()
    org = getattr(vcard, "org", None)
    org_name = ""
    if org is not None and org.value:
        org_name = str(org.value[0] if isinstance(org.value, list) else org.value).strip()
    show_as = getattr(vcard, "x_abshowas", None)
    if org_name and show_as is not None and str(show_as.value).upper() == "COMPANY":
        return org_name
    n = getattr(vcard, "n", None)
    if n is not None:
        parts = [n.value.prefix, n.value.given, n.value.additional, n.value.family, n.value.suffix]
        name = " ".join(" ".join(p) if isinstance(p, list) else str(p) for p in parts if p).strip()
        if name:
            return name
    return org_name


def _is_group(vcard: Any) -> bool:
    """iCloud stores Contacts lists as vCards with X-ADDRESSBOOKSERVER-KIND:group."""
    kind = getattr(vcard, "x_addressbookserver_kind", None)
    return kind is not None and str(kind.value).lower() == "group"


def _serialize(vcard: Any) -> bytes:
    """vCard bytes for a PUT. vCard 3.0 requires FN, which number-only cards lack;
    add one from the structured name or organization (empty when there is neither)."""
    if not hasattr(vcard, "fn"):
        vcard.add("fn").value = _display_name(vcard)
    return vcard.serialize().encode("utf-8")


def create_contact(
    name: str | None = None,
    phones: list[dict[str, str]] | None = None,
    emails: list[dict[str, str]] | None = None,
    addresses: list[dict[str, str]] | None = None,
    organization: str | None = None,
    title: str | None = None,
    notes: str | None = None,
    birthday: str | None = None,
    urls: list[dict[str, str]] | None = None,
    social_profiles: list[dict[str, str]] | None = None,
    dates: list[dict[str, str]] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Create a contact in the default address book and return it as a contact read.

    ``fields`` takes the keyword-only fields of ``_apply_fields`` (department, phonetic
    names, nickname, maiden name, name parts, …). At least one of ``name``, a name part or
    ``organization`` is required; that is checked before any request is sent. Raises
    ``ValueError`` for invalid field input and ``requests.HTTPError`` when iCloud rejects
    the request.
    """
    identity = [name, organization, *(fields.get(key) for key in _NAME_PARTS)]
    if not any(value and str(value).strip() for value in identity):
        raise ValueError("contact needs a name, a name part or an organization")
    email, password = require_auth()
    session = _get_carddav_session(email, password)
    addressbook_url = _default_addressbook_url(session)

    vcard = vobject.vCard()
    vcard.add("n").value = vobject.vcard.Name()
    unique_id = str(uuid.uuid4())
    vcard.add("uid").value = unique_id
    _apply_fields(vcard, phones, emails, addresses, organization, title, notes,
                  birthday, urls, social_profiles, dates, name=name, **fields)

    contact_url = f"{addressbook_url}{unique_id}.vcf"
    response = session.put(
        contact_url,
        data=_serialize(vcard),
        headers={"Content-Type": "text/vcard; charset=utf-8", "If-None-Match": "*"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return _vcard_to_dict(vcard, contact_url)


def update_contact(
    contact_id: str,
    name: str | None = None,
    phones: list[dict[str, str]] | None = None,
    emails: list[dict[str, str]] | None = None,
    addresses: list[dict[str, str]] | None = None,
    organization: str | None = None,
    title: str | None = None,
    notes: str | None = None,
    birthday: str | None = None,
    urls: list[dict[str, str]] | None = None,
    social_profiles: list[dict[str, str]] | None = None,
    dates: list[dict[str, str]] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Update a contact so that only the provided fields change, and return the result.

    ``fields`` takes the keyword-only fields of ``_apply_fields`` (department, phonetic
    names, nickname, maiden name, …). The PUT is conditional on the ETag read here, so an
    edit made elsewhere in between is not overwritten. Raises ``ValueError`` for invalid
    field input and ``requests.HTTPError`` when iCloud rejects the request (412 on conflict).
    """
    contact_id = ensure_icloud_url(contact_id, "contact")
    email, password = require_auth()
    session = _get_carddav_session(email, password)

    response = session.get(contact_id, timeout=TIMEOUT)
    response.raise_for_status()
    etag = response.headers.get("ETag", "")
    vcard = vobject.readOne(response.text)

    _apply_fields(vcard, phones, emails, addresses, organization, title, notes,
                  birthday, urls, social_profiles, dates, name=name, **fields)

    headers = {"Content-Type": "text/vcard; charset=utf-8"}
    if etag:
        headers["If-Match"] = etag
    response = session.put(
        contact_id, data=_serialize(vcard), headers=headers, timeout=TIMEOUT
    )
    response.raise_for_status()
    return _vcard_to_dict(vcard, contact_id)


def delete_contact(contact_id: str) -> dict[str, str]:
    contact_id = ensure_icloud_url(contact_id, "contact")
    email, password = require_auth()
    session = _get_carddav_session(email, password)
    response = session.delete(contact_id, timeout=TIMEOUT)
    response.raise_for_status()
    return {"status": "success", "message": f"Contact {contact_id} deleted"}


def search_contacts(query: str, limit: int | None = None) -> list[dict[str, Any]]:
    needle = query.lower().strip().lstrip("@") or query.lower().strip()
    digits = "".join(ch for ch in needle if ch.isdigit())
    matches = []
    for contact in list_contacts():
        haystack = [
            contact.get("name", ""),
            contact.get("organization", ""),
            contact.get("notes", ""),
            *(e["value"] for e in contact.get("emails", [])),
            *(a["formatted"] for a in contact.get("addresses", [])),
            *(f"{p['username']} {p['url']}" for p in contact.get("social_profiles", [])),
        ]
        phone_hit = bool(digits) and any(
            digits in "".join(ch for ch in phone["value"] if ch.isdigit()) for phone in contact.get("phones", [])
        )
        if phone_hit or any(needle in value.lower() for value in haystack if value):
            matches.append(contact)
            if limit and len(matches) >= limit:
                break
    return matches
