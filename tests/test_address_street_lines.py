"""Tests for multi-line street addresses (street given as a list of lines)."""

import vobject

from icloud_mcp import contacts

from test_contacts import VCARD, _unfolded


def test_address_street_accepts_list_of_lines():
    vcard = vobject.readOne(VCARD)
    contacts._apply_fields(vcard, None, None, [{
        "label": "home",
        "street": ["12 Silbury Boulevard", "Central Milton Keynes"],
        "city": "Milton Keynes",
        "postal_code": "MK9 1AB",
        "country": "United Kingdom",
        "country_code": "GB",
    }], None, None)
    out = _unfolded(vcard)
    assert "12 Silbury Boulevard\\nCentral Milton Keynes" in out  # multi-line ADR street, escaped
    result = contacts._parse_addresses(vcard)
    assert result[0]["street"] == "12 Silbury Boulevard\nCentral Milton Keynes"
    assert result[0]["formatted"] == "12 Silbury Boulevard Central Milton Keynes, Milton Keynes, MK9 1AB, United Kingdom"
    # blank lines are dropped and a list round-trips verbatim
    contacts._apply_fields(vcard, None, None, [{
        "label": "home",
        "street": ["  12 Silbury Boulevard  ", "", "Central Milton Keynes"],
        "city": "Milton Keynes",
        "postal_code": "MK9 1AB",
        "country": "United Kingdom",
        "country_code": "GB",
    }], None, None)
    assert contacts._parse_addresses(vcard)[0]["street"] == "12 Silbury Boulevard\nCentral Milton Keynes"
