"""CardDAV operations for iCloud contacts (direct HTTP/WebDAV, RFC 6352)."""

import base64
import bisect
import copy
import datetime
import functools
import hashlib
import json
import logging
import re
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import requests
import vobject
from defusedxml import ElementTree as ET
from requests.auth import HTTPBasicAuth

from .auth import require_auth
from .calendar import _zone
from .config import config
from .contacts_cache import ContactCache, canonical_url
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


def get_contact(contact_id: str) -> dict[str, Any]:
    """The stored card read directly, plus ``groups``: the person's Contacts lists
    (read-only here, ``[]`` for a list card), resolved after one change check."""
    contact_id = ensure_icloud_url(contact_id, "contact")
    email, password = require_auth()
    session = _get_carddav_session(email, password)
    response = session.get(contact_id, timeout=TIMEOUT)
    response.raise_for_status()
    contact = _vcard_to_dict(vobject.readOne(response.text), contact_id)
    contact["groups"] = _book(session, (email, password)).groups_of().get(canonical_url(contact_id), [])
    return contact


# Single-value text fields: contact key -> vCard property (Apple's X- names for the phonetic
# and maiden names). Pronunciation names (iOS 27) are separate fields from the phonetic
# ones. ``department`` is not here: it lives in the second ``ORG`` component.
_TEXT_FIELDS = {
    "phonetic_first_name": "x-phonetic-first-name",
    "phonetic_middle_name": "x-phonetic-middle-name",
    "phonetic_last_name": "x-phonetic-last-name",
    "pronunciation_first_name": "x-pronunciation-first-name",
    "pronunciation_last_name": "x-pronunciation-last-name",
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
    return (", ".join(str(v) for v in value) if isinstance(value, list) else str(value)).strip()


def _set_text_property(vcard: Any, prop_name: str, value: str | None) -> None:
    """Apply one single-value text field: ``None`` leaves it alone, ``""`` removes every
    instance, other text replaces it. Text equal to the stored value is a no-op so a
    read-then-write-back never rewrites the line."""
    if value is None:
        return
    value = value.strip()  # whitespace-only clears, like name parts and organization
    if value == _text_property(vcard, prop_name):
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
    4. With both, ``FN`` is ``name``, given parts follow rule 1, and first and last name
       not given explicitly are split from ``name`` as in rule 2, so ``name="Jane Doe",
       middle_name="Q"`` still stores Jane and Doe.
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
    for key, value in changed.items():
        setattr(n_value, _NAME_PARTS[key], value)
    if name is not None and not {"first_name", "last_name"} <= changed.keys():
        effective = {**current, **changed}
        tokens = name.split()
        tokens = _strip_tokens(tokens, effective["prefix"].split(), "start")
        tokens = _strip_tokens(tokens, effective["suffix"].split(), "end")
        tokens = _strip_tokens(tokens, effective["middle_name"].split(), "middle")
        stored_last, stored_first = current["last_name"].split(), current["first_name"].split()
        if "last_name" in changed:      # the other part is whatever remains of the name
            first, last = _strip_tokens(tokens, changed["last_name"].split(), "end"), []
        elif "first_name" in changed:
            first, last = [], _strip_tokens(tokens, changed["first_name"].split(), "start")
        elif len(stored_last) > 1 and len(tokens) > len(stored_last) and tokens[-len(stored_last):] == stored_last:
            first, last = tokens[:-len(stored_last)], stored_last
        elif len(stored_first) > 1 and len(tokens) > len(stored_first) and tokens[:len(stored_first)] == stored_first:
            first, last = stored_first, tokens[len(stored_first):]
        elif len(tokens) >= 2:
            first, last = tokens[:-1], tokens[-1:]
        else:
            first, last = tokens, []
        if "first_name" not in changed:
            n_value.given = " ".join(first)
        if "last_name" not in changed:
            n_value.family = " ".join(last)
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
    the person's name. ``True`` replaces any other ``X-ABShowAs`` value so the card holds
    exactly one; ``False`` removes only a ``COMPANY`` value, leaving any other value a
    client may have written; ``None`` or the current value is a no-op.
    """
    if is_company is None or bool(is_company) == _is_company(vcard):
        return
    for prop in list(vcard.contents.get("x-abshowas", [])):
        if is_company or str(prop.value or "").strip().upper() == "COMPANY":
            vcard.remove(prop)
    if is_company:
        vcard.add("x-abshowas").value = "COMPANY"


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

    An entry whose name exactly matches a stored one (preferring one with the same label
    when names repeat) and whose label is omitted or equal is re-added verbatim, so echoing a read changes nothing. Other entries are written
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
        same = [pair for pair in existing if pair[1]["name"].strip() == name]
        match = next((pair for pair in same if pair[1]["label"].lower() == label.lower()), None) or \
            next(iter(same), None)
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
    service, preferring one with the same label when accounts repeat. A match whose label is omitted or equal is re-added verbatim, which keeps
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
        same = [pair for pair in existing if pair[1]["service"].lower() == service.lower()
                and pair[1]["username"] == username]
        match = next((pair for pair in same if pair[1]["label"].lower() == label.lower()), None) or \
            next(iter(same), None)
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
    - ``ringtone`` / ``text_tone``: ``X-ACTIVITY-ALERT:type=call|text\\,snd=<scheme>:<name>``
      (stored with an escaped comma), when the sound scheme is ``system:`` or ``texttone:``.

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
        # vobject has already unescaped "\,"; split only on commas that start a key=, so a
        # sound name containing a comma ("Time, Up") stays whole.
        settings = dict(part.partition("=")[::2]
                        for part in re.split(r",(?=\s*[A-Za-z-]+=)", str(prop.value or "")))
        key = _ALERT_KEYS.get(settings.get("type", "").strip().lower())
        scheme, _, sound = settings.get("snd", "").partition(":")
        if key and sound.strip() and scheme.strip().lower() in _NAMED_SOUND_SCHEMES:
            extras.setdefault(key, sound.strip())
    return extras


def _set_notes(vcard: Any, notes: str | None) -> None:
    """Apply notes: ``""`` removes them, other text replaces them. Text equal to what a read
    returns (rich-text notes are read back cleaned, see ``clean_rich_text``) is a no-op, so
    echoing a read keeps the stored HTML instead of flattening it."""
    if notes is None:
        return
    stored = _text_property(vcard, "note")
    if not notes.strip():
        _set_text_property(vcard, "note", "")  # clears even HTML that reads back as empty
        return
    if notes.strip() == stored or notes == clean_rich_text(stored):
        return
    _set_text_property(vcard, "note", notes)


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
    pronunciation_first_name: str | None = None,
    pronunciation_last_name: str | None = None,
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
        "phonetic_last_name": phonetic_last_name, "pronunciation_first_name": pronunciation_first_name,
        "pronunciation_last_name": pronunciation_last_name, "nickname": nickname, "maiden_name": maiden_name,
        "phonetic_organization": phonetic_organization,
    }
    _validate_fields(vcard, organization=organization, is_company=is_company,
                     related_names=related_names, instant_messages=instant_messages)
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
    _set_text_property(vcard, "title", title)
    _set_notes(vcard, notes)
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
    # Re-fetch rather than cache the local copy: iCloud normalises what it stores.
    _cache.mark_dirty((email, password), contact_url)
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
    if not etag:
        raise ValueError("iCloud returned no ETag for this contact; refusing an unconditional overwrite")
    vcard = vobject.readOne(response.text)

    _apply_fields(vcard, phones, emails, addresses, organization, title, notes,
                  birthday, urls, social_profiles, dates, name=name, **fields)

    headers = {"Content-Type": "text/vcard; charset=utf-8", "If-Match": etag}
    response = session.put(
        contact_id, data=_serialize(vcard), headers=headers, timeout=TIMEOUT
    )
    response.raise_for_status()
    # Re-fetch rather than cache the local copy: iCloud normalises what it stores.
    _cache.mark_dirty((email, password), contact_id)
    return _vcard_to_dict(vcard, contact_id)


def delete_contact(contact_id: str) -> dict[str, str]:
    contact_id = ensure_icloud_url(contact_id, "contact")
    email, password = require_auth()
    session = _get_carddav_session(email, password)
    response = session.delete(contact_id, timeout=TIMEOUT)
    response.raise_for_status()
    # Re-check rather than drop locally: the next change check confirms the deletion.
    _cache.mark_dirty((email, password), contact_id)
    return {"status": "success", "message": f"Contact {contact_id} deleted"}


# Letters that NFKD does not decompose into a base letter plus a combining mark.
_FOLD_EXTRA = str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ħ": "h", "ð": "d", "þ": "th", "ı": "i",
                             "æ": "ae", "œ": "oe"})
_NAME_TEXT_KEYS = ("name", "nickname", "maiden_name", "prefix", "first_name", "middle_name", "last_name",
                   "suffix", "phonetic_first_name", "phonetic_middle_name", "phonetic_last_name",
                   "pronunciation_first_name", "pronunciation_last_name")


def fold(text: str) -> str:
    """Case- and accent-insensitive form of ``text`` for sorting and matching.

    NFKD splits accented letters into a base letter plus combining marks, which are
    dropped; ``casefold`` handles case (including ``ß`` -> ``ss``); a small table covers
    letters NFKD leaves whole (``ł``, ``ø``, ``đ``, ``æ``, …). Queries and stored text are
    folded the same way, so "jose" matches "José" and "lukasz" matches "Łukasz".
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.casefold().translate(_FOLD_EXTRA)


@dataclass(frozen=True)
class CachedCard:
    """One parsed contact as kept by the contacts cache.

    ``contact`` is exactly what ``contacts_get`` returns and is never mutated. The search
    fields are precomputed once per card version so list and search calls only compare
    strings: ``name_text`` (name-like fields, used for ranking) is kept apart from
    ``other_text`` (every other searched field).
    """

    url: str
    etag: str | None
    contact: dict[str, Any]
    listed: bool                 # every card that is not a Contacts list (group) is a person
    sort_key: tuple[int, str]    # (0 if named else 1, folded name): nameless people sort last
    name_text: str
    other_text: str
    phone_digits: tuple[str, ...]
    uid: str = ""                    # the card's UID, which list members refer to; "" when absent
    members: tuple[str, ...] = ()    # list cards only: member UIDs in stored order, "urn:uuid:" removed


_MEMBER_PROP = "X-ADDRESSBOOKSERVER-MEMBER"
_URN_UUID = "urn:uuid:"


def _member_uid(value: str) -> str:
    """The UID a list member value names; a value that is not ``urn:uuid:`` is kept as it is."""
    value = value.strip()
    return value[len(_URN_UUID):] if value.lower().startswith(_URN_UUID) else value


def build_card(url: str, etag: str | None, vcard_text: str) -> CachedCard | None:
    """Parse one stored vCard into a ``CachedCard``; ``None`` when it cannot be parsed.

    ``name_text`` covers the display name, nickname, maiden name, every name part and the
    phonetic and pronunciation names. ``other_text`` covers everything else search looked
    at before paging existed: organization, department, notes, emails, formatted
    addresses, related names, instant-message usernames and social usernames/URLs.
    ``uid`` and, on list cards, ``members`` (every member line, with or without a group
    prefix) let lists be resolved without another request.
    """
    try:
        vcard = vobject.readOne(vcard_text)
        contact = _vcard_to_dict(vcard, url)
        group = _is_group(vcard)
        uid_prop = getattr(vcard, "uid", None)
        uid = str(uid_prop.value or "").strip() if uid_prop is not None else ""
        members = tuple(ref for prop in vcard.contents.get(_MEMBER_PROP.lower(), [])
                        if (ref := _member_uid(str(prop.value or "")))) if group else ()
    except Exception as e:  # one odd card must not break every list and search
        logger.debug("Skipping unparsable vCard %s: %s", url, e)
        return None
    name = contact["name"]
    others = [
        contact["organization"], contact["department"], contact["notes"],
        *(e["value"] for e in contact["emails"]),
        *(a["formatted"] for a in contact["addresses"]),
        *(r["name"] for r in contact["related_names"]),
        *(m["username"] for m in contact["instant_messages"]),
        *(f"{p['username']} {p['url']}" for p in contact["social_profiles"]),
    ]
    return CachedCard(
        url=url,
        etag=etag,
        contact=contact,
        listed=not group,
        sort_key=(0 if name else 1, fold(name)),
        name_text=fold("\n".join(str(contact.get(key) or "") for key in _NAME_TEXT_KEYS)),
        other_text=fold("\n".join(str(value) for value in others if value)),
        phone_digits=tuple("".join(ch for ch in p["value"] if ch.isdigit()) for p in contact["phones"]),
        uid=uid,
        members=members,
    )


_cache = ContactCache(config.CONTACTS_CACHE_MAX_ACCOUNTS, config.CONTACTS_CACHE_IDLE_SECONDS,
                      build_card=build_card)


def _all_cards(session: requests.Session, credentials: tuple[str, str]) -> list[CachedCard]:
    """Every card (people and Contacts lists) in the default address book, as iCloud holds it now.

    With the cache on, this is one change check plus any changed cards; with
    ``CONTACTS_CACHE=false`` (read on every call) it is a full discovery and download, and
    any cached data is dropped. Errors propagate unchanged and return nothing.
    """
    if config.CONTACTS_CACHE:
        return _cache.cards(session, credentials, discover=_default_addressbook_url, fetch_all=_fetch_all_vcards)
    _cache.clear()
    url = _default_addressbook_url(session)
    built = (build_card(item["url"], (item.get("etag") or "").strip() or None, item["data"])
             for item in _fetch_all_vcards(session, url) if item.get("url"))
    return [card for card in built if card is not None]


def _people(session: requests.Session, credentials: tuple[str, str]) -> list[CachedCard]:
    """Every person (non-group card) in the default address book; see ``_all_cards``."""
    return [card for card in _all_cards(session, credentials) if card.listed]


@dataclass(frozen=True)
class _Book:
    """One snapshot of the address book with its lists resolved, keyed by canonical URL.

    Lists name members by UID, compared ignoring case (on the user's account this resolves
    the same references as an exact match). A reference to no card is skipped, as the
    iPhone does; people who share a UID are all named by it, so the answer never depends
    on cache order.
    """

    people: dict[str, CachedCard]
    groups: dict[str, CachedCard]
    by_uid: dict[str, tuple[CachedCard, ...]]

    def members(self, group: CachedCard) -> list[CachedCard]:
        """The people ``group`` names, each once, sorted by url."""
        found = {canonical_url(p.url): p for ref in group.members for p in self.by_uid.get(ref.casefold(), ())}
        return sorted(found.values(), key=lambda card: card.url)

    def groups_of(self) -> dict[str, list[dict[str, str]]]:
        """Canonical person URL -> that person's lists as ``{"id", "name"}``, by folded name then id."""
        mapping: dict[str, list[dict[str, str]]] = {}
        for group in self.groups.values():
            for member in self.members(group):
                mapping.setdefault(canonical_url(member.url), []).append({"id": group.url, "name": group.contact["name"]})
        for entries in mapping.values():
            entries.sort(key=lambda entry: (fold(entry["name"]), entry["id"]))
        return mapping


def _book(session: requests.Session, credentials: tuple[str, str]) -> _Book:
    """The address book with its lists, after one change check (see ``_all_cards``)."""
    people, groups, by_uid = {}, {}, {}
    for card in _all_cards(session, credentials):
        (people if card.listed else groups)[canonical_url(card.url)] = card
    for card in sorted(people.values(), key=lambda c: c.url):
        if card.uid:
            by_uid.setdefault(card.uid.casefold(), []).append(card)
    return _Book(people, groups, {uid: tuple(cards) for uid, cards in by_uid.items()})


def _readable(session: requests.Session, credentials: tuple[str, str],
              fields: list[str] | None) -> tuple[list[CachedCard], dict[str, list[dict[str, str]]] | None]:
    """The people for a list, search or dates call, and their lists when ``fields`` asks for
    them (``"groups"`` or ``"all"``); either way the call makes one change check."""
    if not fields or ("all" not in fields and "groups" not in fields):
        return _people(session, credentials), None
    book = _book(session, credentials)
    return list(book.people.values()), book.groups_of()


# --- Contacts lists (groups): editing a list card's text -----------------------------------------
#
# List cards are edited as text and never re-serialized: vobject would rewrite folding,
# parameter case and property order, and could drop what it does not model (Apple's
# X-APPLE-OL-MAPPING-INFO, member references to deleted people). Only member lines, the
# FN/N name lines and REV are ever changed; every other byte stays as iCloud stored it.

_LINE_BREAK = re.compile(r"(\r\n|\n)")
_FOLD = re.compile(r"(?:\r\n|\n)[ \t]")
_FOLD_OCTETS = 75
_INSERT_BEFORE = {"REV", "UID", "END"}


def _logical_lines(text: str) -> tuple[list[str], str]:
    """``text`` split into logical lines, each with its folding and line breaks, and the
    newline style (``"\\r\\n"`` when the card has any, else ``"\\n"``).

    ``"".join(lines) == text`` always holds. Only CRLF and LF end a line: ``str.splitlines``
    would also split on U+2028, U+0085 or a form feed inside a value.
    """
    pieces = _LINE_BREAK.split(text)
    physical = ["".join(pair) for pair in zip(pieces[::2], [*pieces[1::2], ""], strict=True) if "".join(pair)]
    lines: list[str] = []
    for line in physical:
        if lines and line[:1] in (" ", "\t"):
            lines[-1] += line
        else:
            lines.append(line)
    return lines, "\r\n" if "\r\n" in text else "\n"


def _line_end(line: str) -> str:
    return "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""


def _unfolded(line: str) -> str:
    """The logical line's content: folding removed, final line break dropped."""
    return _FOLD.sub("", line).rstrip("\r\n")


def _split_line(line: str) -> tuple[str, str]:
    """``(name and parameters, value)`` of a content line; a ``:`` inside a quoted
    parameter value does not end the name."""
    content = _unfolded(line)
    quoted = False
    for i, ch in enumerate(content):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            return content[:i], content[i + 1:]
    return content, ""


def _prop_name(line: str) -> str:
    """Upper-case property name without a group prefix (``"ITEM1.FN"`` -> ``"FN"``)."""
    head = _split_line(line)[0].split(";", 1)[0]
    return head.rsplit(".", 1)[-1].strip().upper()


def _member_ref(line: str) -> str | None:
    """The UID an ``X-ADDRESSBOOKSERVER-MEMBER`` line names, else ``None``."""
    if _prop_name(line) != _MEMBER_PROP:
        return None
    return _member_uid(_split_line(line)[1])


def _escape(value: str) -> str:
    """vCard 3.0 TEXT escaping of ``\\``, ``,`` and ``;`` (line breaks are rejected earlier)."""
    return value.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")


def _unescape(value: str) -> str:
    out, chars = [], iter(value)
    for ch in chars:
        if ch == "\\":
            nxt = next(chars, "")
            out.append("\n" if nxt in ("n", "N") else nxt)
        else:
            out.append(ch)
    return "".join(out)


def _components(value: str) -> list[str]:
    """A structured value split on unescaped ``;``, each component unescaped."""
    parts, current, chars = [], [], iter(value)
    for ch in chars:
        if ch == "\\":
            current.append(ch + next(chars, ""))
        elif ch == ";":
            parts.append(_unescape("".join(current)))
            current = []
        else:
            current.append(ch)
    parts.append(_unescape("".join(current)))
    return parts


def _folded(line: str, newline: str) -> str:
    """``line`` folded at 75 octets (RFC 6350), never inside a UTF-8 character, ending in
    ``newline``. Continuation lines start with one space, which counts toward the 75."""
    chunks, current, size = [], [], 0
    for ch in line:
        width = len(ch.encode("utf-8"))
        limit = _FOLD_OCTETS - (1 if chunks else 0)
        if size + width > limit:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(ch)
        size += width
    chunks.append("".join(current))
    return (newline + " ").join(chunks) + newline


def _rev(now: datetime.datetime) -> str:
    """``REV`` in UTC, in the form iCloud stores (``REV:2025-01-02T03:04:05Z``)."""
    return "REV:" + now.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _member_line(uid: str, newline: str) -> str:
    return _folded(f"{_MEMBER_PROP}:{_URN_UUID}{uid}", newline)


def _renamed(line: str, name: str, newline: str) -> str:
    """``line`` with its value set to ``name``; unchanged when it already reads ``name``."""
    head, value = _split_line(line)
    if _prop_name(line) == "FN":
        same = _unescape(value) == name
    else:  # N: iCloud stores the list name as the first component, the rest empty
        parts = _components(value)
        same = parts[0] == name and not any(parts[1:])
    if same:
        return line
    folded = _folded(f"{head}:{_escape(name)}", _line_end(line) or newline)
    return folded if _line_end(line) else folded.rstrip("\r\n")


def _edit_group(text: str, *, name: str | None, add: list[str], remove: list[str],
                now: datetime.datetime) -> tuple[str, dict[str, list[str]]]:
    """The list card ``text`` with members added and removed and, given ``name``, renamed.

    Returns the new text and ``{"added", "removed", "unchanged"}`` as UID lists, in the
    caller's spelling. UIDs match ignoring case. The only changes made: member lines naming
    a UID in ``remove`` are dropped; one line per UID in ``add`` not already a member goes
    after the last member line (else before ``REV``/``UID``/``END``) in the card's newline
    style; ``FN`` and ``N`` take ``name`` unless they already read it; ``REV`` (only when
    the card has one) is set to ``now`` when anything else changed. A request that changes
    nothing returns ``text`` itself.
    """
    lines, newline = _logical_lines(text)
    drop = {uid.casefold() for uid in remove}
    kept, dropped = [], set()
    for line in lines:
        ref = _member_ref(line)
        if ref is not None and ref.casefold() in drop:
            dropped.add(ref.casefold())
        else:
            kept.append(line)
    present = {ref.casefold() for line in kept if (ref := _member_ref(line)) is not None}
    added = []
    for uid in add:
        if uid.casefold() not in present:
            present.add(uid.casefold())
            added.append(uid)
    if added:
        member_at = [i for i, line in enumerate(kept) if _member_ref(line) is not None]
        before = [i for i, line in enumerate(kept) if _prop_name(line) in _INSERT_BEFORE]
        at = member_at[-1] + 1 if member_at else before[0] if before else len(kept)
        if at and not _line_end(kept[at - 1]):
            kept[at - 1] += newline
        kept[at:at] = [_member_line(uid, newline) for uid in added]
    if name is not None:
        kept = [_renamed(line, name, newline) if _prop_name(line) in ("FN", "N") else line for line in kept]
    if kept != lines:
        stamp = _rev(now).split(":", 1)[1]
        kept = [f"{_split_line(line)[0]}:{stamp}{_line_end(line)}" if _prop_name(line) == "REV" else line
                for line in kept]
    removed = [uid for uid in remove if uid.casefold() in dropped]
    unchanged = [uid for uid in add if uid not in added] + [uid for uid in remove if uid not in removed]
    return "".join(kept), {"added": added, "removed": removed, "unchanged": unchanged}


def _new_group_text(uid: str, name: str, member_uids: list[str], now: datetime.datetime) -> str:
    """A new list card in the layout 12 of 15 measured iCloud lists use, with CRLF."""
    newline = "\r\n"
    lines = ["BEGIN:VCARD", "VERSION:3.0", "PRODID:-//icloud-mcp//Contacts lists//EN",
             f"N:{_escape(name)}", f"FN:{_escape(name)}", "X-ADDRESSBOOKSERVER-KIND:group"]
    body = "".join(_folded(line, newline) for line in lines)
    body += "".join(_member_line(member, newline) for member in member_uids)
    return body + "".join(_folded(line, newline) for line in (_rev(now), f"UID:{uid}", "END:VCARD"))


def _is_group_text(text: str) -> bool:
    return any(_prop_name(line) == "X-ADDRESSBOOKSERVER-KIND" and _split_line(line)[1].strip().lower() == "group"
               for line in _logical_lines(text)[0])


# --- Contacts lists (groups): writing ------------------------------------------------------------
#
# Every write sends exactly one list card, conditional on its ETag (If-Match) or on its
# absence (If-None-Match: *); no person card is ever written. All arguments are checked
# before any request, and ids are checked against one snapshot of the book before any write.

MAX_GROUP_NAME = 255
_BAD_NAME_CATEGORIES = {"Cc", "Zl", "Zp"}  # controls and line/paragraph separators; Cf (emoji joiners) is fine
_GROUP_CONFLICT = "the group changed on another device while it was being {}; nothing was {}, retry"
_VCARD_HEADERS = {"Content-Type": "text/vcard; charset=utf-8"}


def _check_group_name(name: Any) -> str:
    if not isinstance(name, str):
        raise ValueError("group name must be text")
    name = name.strip()
    if not name:
        raise ValueError("group name must not be empty")
    if len(name) > MAX_GROUP_NAME:
        raise ValueError(f"group name must be at most {MAX_GROUP_NAME} characters")
    if any(unicodedata.category(ch) in _BAD_NAME_CATEGORIES for ch in name):
        raise ValueError("group name must not contain line breaks or control characters")
    return name


def _check_ids(ids: Any, what: str) -> list[str]:
    if ids is None:
        return []
    if not isinstance(ids, list) or not all(isinstance(contact_id, str) for contact_id in ids):
        raise ValueError(f"{what} must be a list of contact ids")
    return [ensure_icloud_url(contact_id, "contact") for contact_id in ids]


def _group(book: _Book, group_id: str) -> CachedCard:
    group = book.groups.get(canonical_url(group_id))
    if group is None:
        raise ValueError(f"group not found: {group_id}; use contacts_groups for valid ids")
    return group


def _resolve(book: _Book, ids: list[str]) -> dict[str, CachedCard]:
    """``uid.casefold()`` -> person for each id, first spelling kept; two spellings of one
    card, or two people sharing a UID, collapse to one entry."""
    resolved: dict[str, CachedCard] = {}
    for contact_id in ids:
        found = book.people.get(canonical_url(contact_id))
        if found is None:
            raise ValueError(f"not a person in this address book: {contact_id}")
        if not found.uid:
            raise ValueError(f"this contact has no UID, so a list cannot name it: {contact_id}")
        resolved.setdefault(found.uid.casefold(), found)
    return resolved


def _check_unique_name(book: _Book, name: str, exclude: str | None = None) -> None:
    for url, group in book.groups.items():
        if url != exclude and fold(group.contact["name"]) == fold(name):
            raise ValueError(f'a group named "{group.contact["name"]}" already exists')


def _fetch_group(session: requests.Session, group: CachedCard, group_id: str) -> tuple[str, str]:
    """The list card's stored text and ETag, read fresh; refused unless it is still a list.

    The bytes are decoded as UTF-8 (vCards over CardDAV are UTF-8), never by ``requests``'
    header guess, which is ISO-8859-1 without a charset and would double-encode every
    non-ASCII byte on the write back.
    """
    response = session.get(group.url, timeout=TIMEOUT)
    response.raise_for_status()
    etag = response.headers.get("ETag", "")
    if not etag:
        raise ValueError("iCloud returned no ETag for this group; refusing an unconditional write")
    text = response.content.decode("utf-8")
    if not _is_group_text(text):
        raise ValueError(f"group not found: {group_id}; use contacts_groups for valid ids")
    return text, etag


def _member_count(book: _Book, url: str, text: str) -> int:
    card = build_card(url, None, text)
    return len(book.members(card)) if card is not None else 0


def create_group(name: str, members: list[str] | None = None) -> dict[str, Any]:
    """Create a Contacts list, optionally with people in it, and return its summary.

    Returns ``{"id", "name", "member_count"}``. The name is trimmed and must be unique among
    lists ignoring case and accents; members are contact ids of people. The card is sent
    once with ``If-None-Match: *``, so an existing card is never replaced.
    """
    name = _check_group_name(name)
    member_ids = _check_ids(members, "members")
    email, password = require_auth()
    session, credentials = _get_carddav_session(email, password), (email, password)
    book = _book(session, credentials)
    _check_unique_name(book, name)
    resolved = _resolve(book, member_ids)
    uid = str(uuid.uuid4()).upper()
    url = f"{_default_addressbook_url(session)}{uid}.vcf"
    text = _new_group_text(uid, name, [card.uid for card in resolved.values()],
                           datetime.datetime.now(datetime.UTC))
    response = session.put(url, data=text.encode("utf-8"), headers={**_VCARD_HEADERS, "If-None-Match": "*"},
                           timeout=TIMEOUT)
    if response.status_code == 412:
        raise ValueError("a group with this id already exists; nothing was written, retry")
    response.raise_for_status()
    _cache.mark_dirty(credentials, url)
    return {"id": url, "name": name, "member_count": _member_count(book, url, text)}


def update_group(group_id: str, name: str | None = None, add: list[str] | None = None,
                 remove: list[str] | None = None) -> dict[str, Any]:
    """Rename a Contacts list and/or add and remove people, editing only that list card.

    Returns ``{"id", "name", "member_count", "added", "removed", "unchanged"}`` with people
    as contact ids; people already in the requested state are ``unchanged``, and a request
    that changes nothing sends nothing. Order: arguments are checked, then every id against
    the book, then the card is read fresh (it must still be a list) and written with
    ``If-Match``. A membership-only change that meets a conflict is applied once more to the
    newer card, which keeps what the other device changed; a rename is not retried, because
    it would overwrite a name chosen elsewhere. People's own cards are never written.
    """
    group_id = ensure_icloud_url(group_id, "group")
    if name is not None:
        name = _check_group_name(name)
    add_ids, remove_ids = _check_ids(add, "add"), _check_ids(remove, "remove")
    if name is None and not add_ids and not remove_ids:
        raise ValueError("nothing to change: give name, add or remove")
    email, password = require_auth()
    session, credentials = _get_carddav_session(email, password), (email, password)
    book = _book(session, credentials)
    group = _group(book, group_id)
    if name is not None:
        _check_unique_name(book, name, exclude=canonical_url(group.url))
    adds, removes = _resolve(book, add_ids), _resolve(book, remove_ids)
    both = adds.keys() & removes.keys()
    if both:
        raise ValueError(f"cannot both add and remove the same contact: {adds[next(iter(both))].url}")
    people = {**adds, **removes}
    for attempt in (1, 2):
        text, etag = _fetch_group(session, group, group_id)
        new_text, result = _edit_group(text, name=name, add=[card.uid for card in adds.values()],
                                       remove=[card.uid for card in removes.values()],
                                       now=datetime.datetime.now(datetime.UTC))
        if new_text == text:
            break
        response = session.put(group.url, data=new_text.encode("utf-8"),
                               headers={**_VCARD_HEADERS, "If-Match": etag}, timeout=TIMEOUT)
        if response.status_code == 412:
            if name is None and attempt == 1:
                continue
            raise ValueError(_GROUP_CONFLICT.format("updated", "overwritten"))
        response.raise_for_status()
        _cache.mark_dirty(credentials, group.url)
        break
    card = build_card(group.url, None, new_text)
    return {"id": group.url, "name": card.contact["name"] if card is not None else name or "",
            "member_count": _member_count(book, group.url, new_text),
            **{key: [people[uid.casefold()].url for uid in uids] for key, uids in result.items()}}


def delete_group(group_id: str) -> dict[str, str]:
    """Delete a Contacts list. Its people are not changed: only the list card is deleted,
    with ``If-Match``, after reading it fresh to confirm it is still a list."""
    group_id = ensure_icloud_url(group_id, "group")
    email, password = require_auth()
    session, credentials = _get_carddav_session(email, password), (email, password)
    group = _group(_book(session, credentials), group_id)
    text, etag = _fetch_group(session, group, group_id)
    response = session.delete(group.url, headers={"If-Match": etag}, timeout=TIMEOUT)
    if response.status_code == 412:
        raise ValueError(_GROUP_CONFLICT.format("deleted", "deleted"))
    response.raise_for_status()
    _cache.mark_dirty(credentials, group.url)
    card = build_card(group.url, None, text)
    return {"status": "success",
            "message": f"Group {card.contact['name'] if card is not None else group_id} deleted; "
                       "its members were not changed"}


# Every key a contact read can contain, plus the summary-only "phone" and "email" and the
# person's Contacts lists ("groups", read-only, resolved from the list cards).
CONTACT_FIELDS = frozenset({
    "id", "url", "name", "phones", "emails", "addresses", "organization", "department", "title", "notes",
    "birthday", "dates", "urls", "social_profiles", "is_company", "related_names", "instant_messages",
    *_NAME_PARTS, *_TEXT_FIELDS, "alternate_birthday", *_ALERT_KEYS.values(), "phone", "email", "groups",
})
SUMMARY_FIELDS = ("id", "name", "organization", "phone", "email")
MAX_PAGE_SIZE = 200
_CURSOR_ERROR = "cursor is invalid or belongs to a different query; start again without a cursor"

# A page position: (rank, 0 if named else 1, folded name, url). Every person has a distinct
# key because the url is unique, so "strictly after the last key served" never repeats one.
PageKey = tuple[int, int, str, str]


def _fingerprint(query: str) -> str:
    """Ties a cursor to its query ("list" or "search:" + normalised words)."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


def _encode_cursor(fingerprint: str, key: PageKey) -> str:
    payload = json.dumps({"v": 1, "q": fingerprint, "after": list(key)}, separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None, fingerprint: str, max_rank: int) -> PageKey | None:
    """The position after which the next page starts; ``None`` for the first page.

    Any cursor that does not decode to exactly what ``_encode_cursor`` wrote for this
    query raises ``ValueError`` asking the caller to start again, never ``TypeError``.
    """
    if not cursor:
        return None
    try:
        text = cursor.strip()
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or set(data) != {"v", "q", "after"}:
            raise ValueError
        version, query, after = data["v"], data["q"], data["after"]
        if type(version) is not int or version != 1 or query != fingerprint:
            raise ValueError
        if not isinstance(after, list) or len(after) != 4:
            raise ValueError
        rank, unnamed, name, url = after
        if type(rank) is not int or not 0 <= rank <= max_rank or type(unnamed) is not int or unnamed not in (0, 1):
            raise ValueError
        if not isinstance(name, str) or not isinstance(url, str):
            raise ValueError
    # binascii.Error and JSONDecodeError are ValueErrors; deeply nested JSON raises RecursionError.
    except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
        raise ValueError(_CURSOR_ERROR) from None
    return rank, unnamed, name, url


def _check_page_size(page_size: int) -> None:
    if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be a whole number from 1 to {MAX_PAGE_SIZE}")


def _check_fields(fields: list[str] | None) -> list[str] | None:
    """Validated projection: ``None`` for the summary, ``["all"]``, or named fields."""
    if not fields:
        return None
    names = list(dict.fromkeys(str(f).strip() for f in fields))
    if "all" in names and len(names) > 1:
        raise ValueError(f'"all" cannot be combined with other fields; valid fields: all, {", ".join(sorted(CONTACT_FIELDS))}')
    unknown = [n for n in names if n != "all" and n not in CONTACT_FIELDS]
    if unknown:
        raise ValueError(f"unknown field(s) {', '.join(unknown)}; valid fields: all, {', '.join(sorted(CONTACT_FIELDS))}")
    return names


def _project(card: CachedCard, fields: list[str] | None,
             groups_of: dict[str, list[dict[str, str]]] | None = None) -> dict[str, Any]:
    """One result entry: the summary, the full contact read, or ``id`` plus named fields.

    ``phone`` and ``email`` are the first value ("" when there is none). ``groups`` (the
    person's lists, from ``groups_of``) is added under ``["all"]`` and when named. A named
    field the card does not store (``ringtone`` on most cards) is left out. Values are
    copied, so callers cannot change the cached card.
    """
    contact = card.contact
    derived = {"phone": contact["phones"][0]["value"] if contact["phones"] else "",
               "email": contact["emails"][0]["value"] if contact["emails"] else ""}
    if groups_of is not None:
        derived["groups"] = copy.deepcopy(groups_of.get(canonical_url(card.url), []))
    if fields is None:
        return {"id": contact["id"], "name": contact["name"], "organization": contact["organization"],
                "phone": derived["phone"], "email": derived["email"]}
    if fields == ["all"]:
        full = copy.deepcopy(contact)
        if "groups" in derived:
            full["groups"] = derived["groups"]
        return full
    entry = {"id": contact["id"]}
    for name in fields:
        if name in derived:
            entry[name] = derived[name]
        elif name in contact:
            entry[name] = copy.deepcopy(contact[name])
    return entry


def _page(results: list[tuple[PageKey, Any]], page_size: int, after: PageKey | None,
          fingerprint: str, fields: list[str] | None,
          render: Callable[[Any, list[str] | None], dict[str, Any]] = _project) -> dict[str, Any]:
    """One page of ``results`` (sorted by key) starting strictly after ``after``.

    Returns ``{"contacts": [...], "total": <all matches>, "next_cursor": <str or None>}``.
    Keyset paging (rather than an offset) means a person added or removed between pages
    cannot make another person appear twice or be skipped. ``render(item, fields)`` builds
    each entry; list and search use ``_project`` on a ``CachedCard``.
    """
    keys = [key for key, _ in results]
    start = 0 if after is None else bisect.bisect_right(keys, after)
    chunk = results[start:start + page_size]
    more = start + page_size < len(results)
    return {
        "contacts": [render(item, fields) for _, item in chunk],
        "total": len(results),
        "next_cursor": _encode_cursor(fingerprint, chunk[-1][0]) if more and chunk else None,
    }


def list_contacts(page_size: int = 50, cursor: str | None = None, fields: list[str] | None = None,
                  group_id: str | None = None) -> dict[str, Any]:
    """One page of every person in the default address book, ordered by folded name.

    Returns ``{"contacts", "total", "next_cursor"}``. Entries are summaries (id, name,
    organization, first phone, first email) unless ``fields`` names others or is
    ``["all"]`` (the full ``contacts_get`` record). People without a name come last.
    ``group_id`` (a list id from ``list_groups``) limits the listing to that list's
    members, paged the same way.

    ``cursor`` is the opaque ``next_cursor`` of the previous page; it is tied to this
    listing (and to the list, given ``group_id``), so a search cursor (or a damaged one)
    raises ``ValueError`` asking to start again. ``page_size`` and ``fields`` may change
    between pages. Arguments are checked before any request is sent. Every call checks
    iCloud for changes first.
    """
    _check_page_size(page_size)
    fields = _check_fields(fields)
    group_key = canonical_url(ensure_icloud_url(group_id, "group")) if group_id is not None else None
    fingerprint = _fingerprint("list" if group_key is None else "group:" + group_key)
    after = _decode_cursor(cursor, fingerprint, max_rank=0)
    email, password = require_auth()
    session, credentials = _get_carddav_session(email, password), (email, password)
    groups_of = None
    if group_key is None:
        people, groups_of = _readable(session, credentials, fields)
    else:
        book = _book(session, credentials)
        group = book.groups.get(group_key)
        if group is None:
            raise ValueError(f"group not found: {group_id}; use contacts_groups for valid ids")
        people = book.members(group)
        if fields and ("all" in fields or "groups" in fields):
            groups_of = book.groups_of()
    results = sorted((((0, *card.sort_key, card.url), card) for card in people), key=lambda kv: kv[0])
    return _page(results, page_size, after, fingerprint, fields, render=functools.partial(_project, groups_of=groups_of))


def list_groups() -> dict[str, Any]:
    """Every Contacts list (group) in the default address book, by folded name then id.

    Returns ``{"groups": [{"id", "name", "member_count"}], "total"}``. ``member_count``
    counts the people a list names that exist, as the iPhone shows it: references to
    deleted people are skipped. Every call checks iCloud for changes first.
    """
    email, password = require_auth()
    book = _book(_get_carddav_session(email, password), (email, password))
    groups = [{"id": group.url, "name": group.contact["name"], "member_count": len(book.members(group))}
              for group in book.groups.values()]
    groups.sort(key=lambda entry: (fold(entry["name"]), entry["id"]))
    return {"groups": groups, "total": len(groups)}


def _query_words(query: str) -> list[str]:
    """Folded query words, each without a leading ``@``; raises when none are left."""
    words = [word.lstrip("@") for word in fold(query or "").split()]
    words = [word for word in words if word]
    if not words:
        raise ValueError("query must not be empty")
    return words


def _search_rank(card: CachedCard, words: list[str], qnorm: str, digit_words: dict[str, str]) -> int | None:
    """0 for a name starting with the query, 1 for every word in the name fields, 2 for any
    other match; ``None`` when some word matches nothing."""
    for word in words:
        if word in card.name_text or word in card.other_text:
            continue
        digits = digit_words.get(word)
        if digits and any(digits in phone for phone in card.phone_digits):
            continue
        return None
    if card.sort_key[1].startswith(qnorm):
        return 0
    return 1 if all(word in card.name_text for word in words) else 2


def search_contacts(query: str, page_size: int = 50, cursor: str | None = None,
                    fields: list[str] | None = None) -> dict[str, Any]:
    """One page of the people matching ``query``, best matches first.

    Returns ``{"contacts", "total", "next_cursor"}`` with the same paging, cursor and
    ``fields`` options as ``list_contacts``; a cursor only continues the same query.

    Matching ignores case and accents (``fold``). The query is split into words, a leading
    ``@`` on a word is ignored, and a person matches when *every* word is found, in any
    order, in at least one searched field: name, name parts, nickname, maiden name,
    phonetic and pronunciation names, organization, department, notes, emails, formatted
    addresses, related names, instant-message usernames and social profiles (username or
    URL). A word containing a digit also matches a phone number whose digits contain the
    word's digits, whatever the formatting ("555-0100" finds "+1 (555) 010-0100"'s digits).

    Ranking: people whose display name starts with the query come first, then people
    matched on name fields alone, then everyone else; ties are in name order. A query with
    no words left raises ``ValueError`` before any request is sent.
    """
    words = _query_words(query)
    qnorm = " ".join(words)
    _check_page_size(page_size)
    fields = _check_fields(fields)
    fingerprint = _fingerprint("search:" + qnorm)
    after = _decode_cursor(cursor, fingerprint, max_rank=2)
    digit_words = {word: digits for word in words if (digits := "".join(ch for ch in word if ch.isdigit()))}
    email, password = require_auth()
    people, groups_of = _readable(_get_carddav_session(email, password), (email, password), fields)
    results = []
    for card in people:
        rank = _search_rank(card, words, qnorm, digit_words)
        if rank is not None:
            results.append(((rank, *card.sort_key, card.url), card))
    results.sort(key=lambda kv: kv[0])
    return _page(results, page_size, after, fingerprint, fields, render=functools.partial(_project, groups_of=groups_of))


# --- Upcoming birthdays and significant dates ----------------------------------------------------

MAX_WINDOW_DAYS = 366  # a full year, so any window includes each yearly date at least once
_DATE_TEXT = re.compile(r"(?:(\d{4})-?|--)(\d{2})-?(\d{2})")


def _month_day(text: str) -> tuple[int | None, int, int] | None:
    """``(year, month, day)`` from a date as contact reads report it, or ``None``.

    Accepts ``YYYY-MM-DD`` and ``YYYYMMDD``, and the yearless ``--MM-DD`` and ``--MMDD``
    (``year`` is ``None``; Apple's 1604 placeholder year already arrives this way). The
    month and day are checked against a real calendar, using a leap year when there is no
    year so that a yearless February 29 is kept. Unreadable text and impossible dates
    return ``None``, so one bad stored date never fails a whole call.
    """
    match = _DATE_TEXT.fullmatch((text or "").strip())
    if not match:
        return None
    year = int(match.group(1)) if match.group(1) else None
    month, day = int(match.group(2)), int(match.group(3))
    try:
        datetime.date(2000 if year is None else year, month, day)
    except ValueError:
        return None
    return year, month, day


def _place(month: int, day: int, year: int) -> datetime.date:
    """The day a yearly date falls on in ``year``.

    February 29 falls on February 28 in common years, so leap-day birthdays still come up
    every year.
    """
    if (month, day) == (2, 29):
        try:
            return datetime.date(year, 2, 29)
        except ValueError:
            return datetime.date(year, 2, 28)
    return datetime.date(year, month, day)


def _occasions(contact: dict[str, Any], start: datetime.date, days: int) -> list[dict[str, Any]]:
    """The contact's birthday and significant dates that fall in ``days`` days from ``start``.

    Each occasion is ``{"kind", "date", "days_away"}``: ``kind`` is ``"birthday"`` or the
    date's label as contact reads report it (``"other"`` when unlabelled, the default new
    dates get), ``date`` is the ISO day it falls on and ``days_away`` counts from ``start``.
    ``"years"`` (the age or the number of years) is added when the original year is stored
    and has been reached. Dates match by month and day in each calendar year the window
    touches, so a window across December 31 finds January dates, and a 366-day window can
    hold the same date twice. Occasions are sorted by day, birthday first on a tie.
    """
    end = start + datetime.timedelta(days=days - 1)
    stored = [("birthday", contact.get("birthday") or "")]
    stored += [((d.get("label") or "").strip() or "other", d.get("date") or "") for d in contact.get("dates") or []]
    found = []
    for kind, text in stored:
        parsed = _month_day(text)
        if parsed is None:
            continue
        year, month, day = parsed
        for placed_year in sorted({start.year, end.year}):
            placed = _place(month, day, placed_year)
            if start <= placed <= end:
                occasion: dict[str, Any] = {"kind": kind, "date": placed.isoformat(), "days_away": (placed - start).days}
                if year is not None and placed_year >= year:
                    occasion["years"] = placed_year - year
                found.append(occasion)
    found.sort(key=lambda occasion: occasion["days_away"])
    return found


def _social_links(contact: dict[str, Any]) -> list[dict[str, str]]:
    """Every social profile as ``{"service", "username", "url"}`` with a link to open it.

    A stored web address is the link. A value stored the Apple way (``x-apple:username``,
    as iOS saves some profiles) gets a link built from the username for services with a
    known profile address; anything else gets ``""``. This is derived output only:
    ``social_profiles`` keeps the stored value, because updates send it back unchanged.
    """
    links = []
    for profile in contact.get("social_profiles") or []:
        service, username, stored = profile.get("service", ""), profile.get("username", ""), profile.get("url", "")
        url = ""
        handle = username.lstrip("@")
        if stored.lower().startswith(("http://", "https://")):
            url = stored
        elif stored.lower().startswith("x-apple:") and handle and service in _SOCIAL_SERVICES:
            url = _SOCIAL_SERVICES[service][1].format(quote(handle, safe=""))
        links.append({"service": service, "username": username, "url": url})
    return links


_START_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _today() -> datetime.date:
    """Today in the server's ``DEFAULT_TIMEZONE``: the default first day of a window.

    Uses the calendar tools' zone lookup, so an unknown zone raises the same clear
    ``ValueError``. Callers that know the user's own date should pass it instead.
    """
    return datetime.datetime.now(_zone(None)).date()


def _date_entry(item: tuple[CachedCard, list[dict[str, Any]]], fields: list[str] | None,
                groups_of: dict[str, list[dict[str, str]]] | None = None) -> dict[str, Any]:
    """One upcoming-dates entry: who, which occasions, and every way to reach them.

    The default is ``{"id", "name", "occasions", "social_links", "phone", "email"}``, with
    ``phone`` and ``email`` the first stored values (``""`` when none). Named ``fields`` are
    added with ``contacts_list``'s names and rules; ``["all"]`` is the full contact read
    (with ``groups``) plus the same four keys. Values are copies, so callers cannot change
    the cached card.
    """
    card, occasions = item
    contact = card.contact
    reach = {"social_links": _social_links(contact),
             "phone": contact["phones"][0]["value"] if contact["phones"] else "",
             "email": contact["emails"][0]["value"] if contact["emails"] else ""}
    if fields == ["all"]:
        return {**_project(card, fields, groups_of), "occasions": occasions, **reach}
    entry = {"id": contact["id"], "name": contact["name"], "occasions": occasions, **reach}
    if fields:
        extra = _project(card, fields, groups_of)
        entry.update({name: extra[name] for name in fields if name in extra and name not in entry})
    return entry


def upcoming_dates(start_date: str | None = None, days: int = 1, page_size: int = 50,
                   cursor: str | None = None, fields: list[str] | None = None) -> dict[str, Any]:
    """People with a birthday or significant date in the ``days`` days from ``start_date``.

    Returns ``{"contacts", "total", "next_cursor", "start_date", "end_date"}``. Each entry
    (see ``_date_entry``) lists the person's occasions in the window, every social profile
    with a link, and their first phone and email. People are ordered by their soonest
    occasion, then by name; Contacts lists are left out.

    ``start_date`` is ``YYYY-MM-DD`` and defaults to today in ``DEFAULT_TIMEZONE`` (a blank
    value counts as no value); the response echoes the window it used. ``days`` is 1 to
    366, so ``1`` is that day only and ``7`` a week. Paging, cursors and ``fields`` work as
    in ``list_contacts``; a cursor only continues the same start date and window. All
    arguments are checked before any request, and every call checks iCloud for changes
    first, so a failure raises and returns nothing.
    """
    if type(days) is not int or not 1 <= days <= MAX_WINDOW_DAYS:
        raise ValueError(f"days must be a whole number from 1 to {MAX_WINDOW_DAYS}")
    text = str(start_date or "").strip()
    if not text:
        start = _today()
    else:
        try:
            if not _START_DATE.fullmatch(text):
                raise ValueError
            start = datetime.date.fromisoformat(text)
        except ValueError:
            raise ValueError("start_date must be a real date written as YYYY-MM-DD") from None
    try:
        end = start + datetime.timedelta(days=days - 1)
    except OverflowError:
        raise ValueError("start_date and days must end on or before 9999-12-31") from None
    _check_page_size(page_size)
    fields = _check_fields(fields)
    fingerprint = _fingerprint(f"dates:{start.isoformat()}:{days}")
    after = _decode_cursor(cursor, fingerprint, max_rank=days - 1)
    email, password = require_auth()
    people, groups_of = _readable(_get_carddav_session(email, password), (email, password), fields)
    results = []
    for card in people:
        occasions = _occasions(card.contact, start, days)
        if occasions:
            results.append(((occasions[0]["days_away"], *card.sort_key, card.url), (card, occasions)))
    results.sort(key=lambda kv: kv[0])
    page = _page(results, page_size, after, fingerprint, fields,
                 render=functools.partial(_date_entry, groups_of=groups_of))
    return {**page, "start_date": start.isoformat(), "end_date": end.isoformat()}
