"""Tests for the contacts cache (spec: .specs/contacts-list-search-scale)."""

import pytest

from icloud_mcp import config as config_module

# --- task 1.1: configuration helpers --------------------------------------------------------


def test_cache_config_defaults():
    assert config_module._env_positive_int("CONTACTS_CACHE_MAX_ACCOUNTS", 8, environ={}) == 8
    assert config_module._env_positive_float("CONTACTS_CACHE_IDLE_SECONDS", 1800.0, environ={}) == 1800.0
    assert config_module.config.CONTACTS_CACHE in (True, False)
    assert config_module.config.CONTACTS_CACHE_MAX_ACCOUNTS >= 1
    assert config_module.config.CONTACTS_CACHE_IDLE_SECONDS > 0


def test_cache_config_overrides():
    env = {"CONTACTS_CACHE_MAX_ACCOUNTS": " 3 ", "CONTACTS_CACHE_IDLE_SECONDS": "90.5"}
    assert config_module._env_positive_int("CONTACTS_CACHE_MAX_ACCOUNTS", 8, environ=env) == 3
    assert config_module._env_positive_float("CONTACTS_CACHE_IDLE_SECONDS", 1800.0, environ=env) == 90.5
    assert config_module._env_positive_int("X", 8, environ={"X": ""}) == 8


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_cache_config_rejects_non_positive_or_invalid(value):
    with pytest.raises(ValueError, match="CONTACTS_CACHE_MAX_ACCOUNTS"):
        config_module._env_positive_int("CONTACTS_CACHE_MAX_ACCOUNTS", 8, environ={"CONTACTS_CACHE_MAX_ACCOUNTS": value})
    with pytest.raises(ValueError, match="CONTACTS_CACHE_IDLE_SECONDS"):
        config_module._env_positive_float("CONTACTS_CACHE_IDLE_SECONDS", 1800.0,
                                          environ={"CONTACTS_CACHE_IDLE_SECONDS": value})
