"""Tests for the contacts cache (spec: .specs/contacts-list-search-scale)."""

import threading

import pytest
import requests
from fake_carddav import BOOK, FakeCardDAV, FakeResponse, vcard

from icloud_mcp import config as config_module
from icloud_mcp import contacts
from icloud_mcp.contacts_cache import ContactCache, canonical_url

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


# --- task 1.3: the change-checked cache -------------------------------------------------------

CREDS = ("jo@example.com", "app-pw-1")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_cache(**kw):
    kw.setdefault("max_accounts", 4)
    kw.setdefault("idle_seconds", 60.0)
    return ContactCache(kw.pop("max_accounts"), kw.pop("idle_seconds"), build_card=contacts.build_card,
                        sweep_interval=None, **kw)


def names(cards):
    return sorted(c.contact["name"] for c in cards)


def call(cache, server, creds=CREDS, discovered=None, book=BOOK):
    def discover(session):
        if discovered is not None:
            discovered.append(1)
        return book
    return cache.cards(server, creds, discover=discover, fetch_all=contacts._fetch_all_vcards)


@pytest.fixture
def server():
    s = FakeCardDAV()
    s.put_card("ada-lovelace")
    s.put_card("alan-turing")
    return s


def test_cold_load_takes_token_before_download(server):
    cache = make_cache()
    assert names(call(cache, server)) == ["Ada Lovelace", "Alan Turing"]
    assert server.kinds() == ["sync_token", "addressbook_query"]


def test_warm_call_without_changes_is_one_report(server):
    cache = make_cache()
    call(cache, server)
    server.log.clear()
    assert names(call(cache, server)) == ["Ada Lovelace", "Alan Turing"]
    assert server.kinds() == ["sync_collection"]


def test_changed_added_and_deleted_cards_are_applied(server):
    cache = make_cache()
    book = BOOK.replace("https://p1-contacts.icloud.com/", "https://P1-Contacts.iCloud.com:443/")
    call(cache, server, book=book)
    server.put_card("ada-lovelace", vcard("Ada King"))
    server.put_card("grace-hopper")
    server.delete_card("alan-turing")
    server.log.clear()
    cards = call(cache, server, book=book)
    assert names(cards) == ["Ada King", "Grace Hopper"]
    assert server.kinds() == ["sync_collection", "addressbook_multiget"]
    # Cards fetched by the delta keep the id a full download would give them.
    assert sorted(c.url for c in cards) == [book + "ada-lovelace.vcf", book + "grace-hopper.vcf"]
    assert all(c.contact["id"] == c.url for c in cards)


def test_unchanged_etag_needs_no_multiget(server):
    cache = make_cache()
    call(cache, server)
    server.changes.append((server.version, "/123/carddavhome/card/ada-lovelace.vcf"))  # reported, same etag
    server.version += 1
    server.log.clear()
    call(cache, server)
    assert server.kinds() == ["sync_collection"]


def test_many_changes_are_fetched_in_chunks(server):
    cache = make_cache()
    call(cache, server)
    for i in range(230):
        server.put_card(f"person-{i:03d}")
    server.log.clear()
    assert len(call(cache, server)) == 232
    assert server.kinds().count("addressbook_multiget") == 3


def test_groups_and_unparsable_changed_cards(server):
    cache = make_cache()
    call(cache, server)
    server.put_card("club", "BEGIN:VCARD\nVERSION:3.0\nFN:Club\nN:Club;;;;\nX-ADDRESSBOOKSERVER-KIND:group\nEND:VCARD\n")
    server.put_card("ada-lovelace", "garbage")
    cards = call(cache, server)
    assert names(cards) == ["Alan Turing", "Club"]
    assert [c.listed for c in cards if c.contact["name"] == "Club"] == [False]


@pytest.mark.parametrize("status", [412, 409, 507, (403, "<d:error><d:valid-sync-token/></d:error>"), 404, 410])
def test_rejected_or_stale_token_rebuilds_once(server, status):
    cache = make_cache()
    call(cache, server)
    server.put_card("grace-hopper")
    server.errors["sync_collection"] = [status]
    server.log.clear()
    discovered = []
    assert names(call(cache, server, discovered=discovered)) == ["Ada Lovelace", "Alan Turing", "Grace Hopper"]
    assert server.kinds() == ["sync_collection", "sync_token", "addressbook_query"]
    assert discovered == [1]  # rediscovered


def test_truncated_report_inside_207_rebuilds(server):
    cache = make_cache()
    call(cache, server)
    server.put_card("grace-hopper")
    server.put_card("katherine-johnson")
    server.truncate_next_sync = True
    server.log.clear()
    assert len(call(cache, server)) == 4
    assert "addressbook_query" in server.kinds()


@pytest.mark.parametrize("status", [401, 403, 500])
def test_failed_check_raises_and_keeps_entry(server, status):
    cache = make_cache()
    call(cache, server)
    server.put_card("grace-hopper")
    server.errors["sync_collection"] = [status]
    with pytest.raises(requests.HTTPError):
        call(cache, server)
    server.log.clear()
    assert names(call(cache, server)) == ["Ada Lovelace", "Alan Turing", "Grace Hopper"]  # replays the same token
    assert server.kinds() == ["sync_collection", "addressbook_multiget"]


def test_omitted_href_reloads_and_failed_chunk_commits_nothing(server):
    cache = make_cache()
    call(cache, server)
    server.put_card("ada-lovelace", vcard("Ada King"))
    server.omit_from_multiget = {"/123/carddavhome/card/ada-lovelace.vcf"}
    server.log.clear()
    assert "Ada King" in names(call(cache, server))  # the delta cannot complete: reload instead
    assert server.kinds() == ["sync_collection", "addressbook_multiget", "sync_token", "addressbook_query"]
    server.omit_from_multiget = set()
    for i in range(150):
        server.put_card(f"p-{i:03d}")
    original, calls = server._do_addressbook_multiget, []

    def second_chunk_fails(url, body):
        calls.append(1)
        return original(url, body) if len(calls) == 1 else FakeResponse(500)
    server._do_addressbook_multiget = second_chunk_fails
    with pytest.raises(requests.HTTPError):
        call(cache, server)
    server._do_addressbook_multiget = original
    cards = call(cache, server)   # nothing was committed, so the same token replays and converges
    assert len(cards) == 152 and "Ada King" in names(cards)


def test_report_without_token_raises(server):
    cache = make_cache()
    call(cache, server)
    original = server._do_sync_collection
    server._do_sync_collection = lambda url, body: server._multistatus("")
    with pytest.raises(ValueError, match="sync token"):
        call(cache, server)
    server._do_sync_collection = original


def test_cold_failure_inserts_nothing_and_bad_logins_do_not_evict(server):
    cache = make_cache(max_accounts=2)
    call(cache, server)
    for i in range(20):
        bad = FakeCardDAV()
        bad.errors["sync_token"] = [401]
        with pytest.raises(requests.HTTPError):
            call(cache, bad, creds=("jo@example.com", f"wrong-{i}"))
    server.log.clear()
    call(cache, server)
    assert server.kinds() == ["sync_collection"]  # still warm


def test_credentials_are_isolated(server):
    cache = make_cache()
    call(cache, server)
    other = FakeCardDAV()
    other.put_card("someone-else")
    assert names(call(cache, other, creds=("jo@example.com", "another-password"))) == ["Someone Else"]
    assert names(call(cache, server)) == ["Ada Lovelace", "Alan Turing"]


def test_cap_evicts_least_recently_used():
    cache = make_cache(max_accounts=2)
    servers = [FakeCardDAV() for _ in range(3)]
    for i, s in enumerate(servers):
        s.put_card(f"person-{i}")
        call(cache, s, creds=(f"u{i}@example.com", "pw"))
    servers[0].log.clear()
    call(cache, servers[0], creds=("u0@example.com", "pw"))
    assert servers[0].kinds() == ["sync_token", "addressbook_query"]  # was evicted, cold again


def test_idle_entries_are_swept(server):
    clock = Clock()
    cache = make_cache(idle_seconds=60.0, clock=clock)
    call(cache, server)
    clock.now += 61
    cache.sweep()
    server.log.clear()
    call(cache, server)
    assert server.kinds() == ["sync_token", "addressbook_query"]


def test_background_sweeper_discards_idle_data(server):
    clock = Clock()
    cache = ContactCache(4, 60.0, build_card=contacts.build_card, clock=clock, sweep_interval=0.01)
    call(cache, server)
    clock.now += 61
    for _ in range(200):
        if not cache._entries:
            break
        threading.Event().wait(0.01)
    assert not cache._entries
    cache.close()


def test_concurrent_cold_calls_download_once(server):
    cache = make_cache()
    gate = threading.Event()
    original = server._do_addressbook_query

    def slow(url, body):
        gate.wait(2)
        return original(url, body)
    server._do_addressbook_query = slow
    results = []
    threads = [threading.Thread(target=lambda: results.append(names(call(cache, server)))) for _ in range(2)]
    for t in threads:
        t.start()
    threading.Event().wait(0.1)
    gate.set()
    for t in threads:
        t.join(5)
    assert results == [["Ada Lovelace", "Alan Turing"]] * 2
    assert server.kinds().count("addressbook_query") == 1


def test_mark_dirty_refetches_when_report_lags(server):
    cache = make_cache()
    call(cache, server)
    server.report_writes = False
    server.put(BOOK + "ada-lovelace.vcf", data=vcard("Ada King").encode())
    server.delete(BOOK + "alan-turing.vcf")
    cache.mark_dirty(CREDS, "https://P1-CONTACTS.icloud.com:443/123/carddavhome/card/ada-lovelace.vcf")
    cache.mark_dirty(CREDS, BOOK + "alan-turing.vcf")
    cards = call(cache, server)
    assert names(cards) == ["Ada King"]
    assert "PRODID:server-" not in cards[0].contact.get("notes", "")  # card is iCloud's copy, built from the store


def test_mark_dirty_without_entry_is_ignored():
    make_cache().mark_dirty(CREDS, BOOK + "x.vcf")


def test_canonical_url():
    assert canonical_url("HTTPS://P1-Contacts.iCloud.com:443/a/b.vcf") == "https://p1-contacts.icloud.com/a/b.vcf"
    assert canonical_url("https://h:8443/a") == "https://h:8443/a"
    assert canonical_url("https://h/a%40b%2Dc.vcf") == canonical_url("https://h/a@b-c.vcf") == "https://h/a@b-c.vcf"
    assert canonical_url("https://h/a%20b.vcf") == canonical_url("https://h/a b.vcf") == "https://h/a%20b.vcf"


def test_only_read_methods_are_issued(server):
    cache = make_cache()
    call(cache, server)
    server.put_card("grace-hopper")
    server.delete_card("alan-turing")
    call(cache, server)
    assert {method for method, _ in server.log} <= {"GET", "PROPFIND", "REPORT"}


def test_clear_drops_everything(server):
    cache = make_cache()
    call(cache, server)
    cache.clear()
    server.log.clear()
    call(cache, server)
    assert server.kinds() == ["sync_token", "addressbook_query"]


# --- wave-review hardening ------------------------------------------------------------------------

def test_mark_dirty_during_a_first_load_is_kept(server):
    cache = make_cache()
    loading, release = threading.Event(), threading.Event()
    original = server._do_addressbook_query

    def slow(url, body):
        loading.set()
        release.wait(2)
        return original(url, body)
    server._do_addressbook_query = slow
    cold = threading.Thread(target=lambda: call(cache, server))
    cold.start()
    assert loading.wait(2)
    marker = threading.Thread(target=lambda: cache.mark_dirty(CREDS, BOOK + "grace-hopper.vcf"))
    marker.start()                       # waits for the first load to finish, then records the write
    release.set()
    cold.join(5)
    marker.join(5)
    (entry,) = cache._entries.values()
    assert entry.dirty == {BOOK + "grace-hopper.vcf"}


def test_mark_dirty_outside_the_cached_book_is_ignored(server):
    cache = make_cache()
    call(cache, server)
    cache.mark_dirty(CREDS, "https://p1-contacts.icloud.com/123/carddavhome/other-book/x.vcf")
    cache.mark_dirty(CREDS, "https://p2-contacts.icloud.com/123/carddavhome/card/x.vcf")
    (entry,) = cache._entries.values()
    assert entry.dirty == set()
    server.log.clear()
    call(cache, server)
    assert server.kinds() == ["sync_collection"]


def test_entry_evicted_while_waiting_is_not_used(server):
    cache = make_cache()
    call(cache, server)
    (key, old), = cache._entries.items()
    old.lock.acquire()                   # another call is busy with this account ...
    results = []
    waiter = threading.Thread(target=lambda: results.append(names(call(cache, server))))
    waiter.start()
    threading.Event().wait(0.1)
    with cache._map_lock:
        del cache._entries[key]          # ... and the entry is evicted meanwhile
    server.log.clear()
    old.lock.release()
    waiter.join(5)
    assert results == [["Ada Lovelace", "Alan Turing"]]
    assert server.kinds() == ["sync_token", "addressbook_query"]  # started over with a fresh entry
    assert cache._entries[key] is not old


def test_card_that_fails_conversion_is_skipped_not_fatal(server, monkeypatch):
    real = contacts._vcard_to_dict

    def flaky(vcard, url):
        if url.endswith("alan-turing.vcf"):
            raise AttributeError("odd card")
        return real(vcard, url)
    monkeypatch.setattr(contacts, "_vcard_to_dict", flaky)
    assert names(call(make_cache(), server)) == ["Ada Lovelace"]


def test_card_gone_per_property_is_dropped_not_retried_forever(server):
    """A dirty card that iCloud reports missing inside its propstat (not at response level)."""
    cache = make_cache()
    call(cache, server)
    server.delete_card("ada-lovelace", report=False)        # the change report lags
    cache.mark_dirty(CREDS, BOOK + "ada-lovelace.vcf")
    original = server._do_addressbook_multiget

    def propstat_404(url, body):
        return FakeResponse(207, '<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
                                 '<d:response><d:href>/123/carddavhome/card/ada-lovelace.vcf</d:href><d:propstat>'
                                 '<d:prop><card:address-data/></d:prop><d:status>HTTP/1.1 404 Not Found</d:status>'
                                 '</d:propstat></d:response></d:multistatus>')
    server._do_addressbook_multiget = propstat_404
    assert names(call(cache, server)) == ["Alan Turing"]
    server._do_addressbook_multiget = original
    server.log.clear()
    assert names(call(cache, server)) == ["Alan Turing"]
    assert server.kinds() == ["sync_collection"]


def test_load_without_a_sync_token_warns(server, caplog):
    cache = make_cache()
    server._do_sync_token = lambda url, body: FakeResponse(207, '<d:multistatus xmlns:d="DAV:"/>')
    with caplog.at_level("WARNING", logger="icloud_mcp.contacts_cache"):
        call(cache, server)
    assert "no sync token" in caplog.text


def test_addressbook_url_is_known_only_after_a_load(server):
    cache = make_cache()
    assert cache.addressbook_url(CREDS) is None
    call(cache, server)
    assert cache.addressbook_url(CREDS) == BOOK and cache.addressbook_url(("x@example.com", "pw")) is None
