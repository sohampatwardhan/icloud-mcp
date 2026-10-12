"""Tests for multi-line street addresses (street given as a list of lines)."""

import vobject

from icloud_mcp import contacts

from test_contacts import VCARD, _unfolded


def test_address_street_accepts_list_of_lines():
    vcard = vobject.readOne(VCARD)
    contacts._apply_fields(vcard, None, None, [{
        "label": "home",
        "street": ["11 Trelissick Way", "Westcroft"],
        "city": "Milton Keynes",
        "postal_code": "MK4 4JW",
        "country": "United Kingdom",
        "country_code": "GB",
    }], None, None)
    out = _unfolded(vcard)
    assert "11 Trelissick Way\\nWestcroft" in out  # multi-line ADR street, escaped
    result = contacts._parse_addresses(vcard)
    assert result[0]["street"] == "11 Trelissick Way\nWestcroft"
    assert result[0]["formatted"] == "11 Trelissick Way Westcroft, Milton Keynes, MK4 4JW, United Kingdom"
    # blank lines are dropped and a list round-trips verbatim
    contacts._apply_fields(vcard, None, None, [{
        "label": "home",
        "street": ["  11 Trelissick Way  ", "", "Westcroft"],
        "city": "Milton Keynes",
        "postal_code": "MK4 4JW",
        "country": "United Kingdom",
        "country_code": "GB",
    }], None, None)
    assert contacts._parse_addresses(vcard)[0]["street"] == "11 Trelissick Way\nWestcroft"
