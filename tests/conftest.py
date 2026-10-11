import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

os.environ.setdefault("ICLOUD_EMAIL", "tester@icloud.com")
os.environ.setdefault("ICLOUD_APP_SPECIFIC_PASSWORD", "aaaa-bbbb-cccc-dddd")
os.environ.setdefault("DEFAULT_TIMEZONE", "Europe/Berlin")


@pytest.fixture(autouse=True)
def _clear_contacts_cache():
    """Each test starts and ends with an empty contacts cache, so no cached address book
    leaks between tests (the cache is module-level, like the server's)."""
    from icloud_mcp import contacts

    contacts._cache.clear()
    yield
    contacts._cache.clear()
