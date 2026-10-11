"""In-memory, change-checked cache of iCloud address books, one entry per set of credentials.

Downloading a whole address book costs about 1.6 MB and 1.25 s on iCloud, while asking what
changed since a CardDAV sync token (RFC 6578 ``sync-collection``) costs about 0.14 s. So
every read call checks with the caller's own credentials before answering. Only changed
cards are fetched (``addressbook-multiget``), and the answer always matches what iCloud
holds at that moment. Nothing is written to disk.

Guarantees:

- **Fresh.** Each ``cards()`` call runs a change check with the caller's credentials and
  answers only after it succeeds. Failures raise and return no data.
- **Isolated.** Entries are keyed by an HMAC of email and password under a per-process
  secret, so a caller can only reach data loaded with the same credentials.
- **Atomic.** A delta is applied to a staged copy and committed together with its new
  token, so a partial failure leaves the entry exactly as it was. A failed first load
  inserts nothing, so failed logins cannot evict valid accounts.
- **Bounded.** At most ``max_accounts`` entries are kept, and idle entries are discarded
  by every call and by a background sweeper.

The module imports nothing from ``contacts``: discovery, the full download and card
building are injected, so tests can patch them on the ``contacts`` module.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import requests
from defusedxml import ElementTree as ET

logger = logging.getLogger(__name__)

NS = {"d": "DAV:", "card": "urn:ietf:params:xml:ns:carddav"}
TIMEOUT = 30
MULTIGET_CHUNK = 100
_DEFAULT_PORTS = {("https", 443), ("http", 80)}


class _Rebuild(Exception):
    """The stored token or address-book URL is no longer usable; reload from scratch."""


def canonical_url(url: str) -> str:
    """Cache key for a card URL: scheme and host lower-cased, default port dropped,
    percent-encoding normalised (so ``%40`` and ``@`` name the same card).

    The write path builds URLs by string concatenation and callers pass ids back
    verbatim, while sync responses yield joined hrefs; this makes all of them agree.
    """
    parts = urlsplit(url)
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    port = parts.port
    netloc = host if port is None or (scheme, port) in _DEFAULT_PORTS else f"{host}:{port}"
    path = quote(unquote(parts.path), safe="/:@!$&'()*+,;=-._~")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _status_code(element: Any) -> int | None:
    if element is None or not element.text:
        return None
    match = re.search(r"\s(\d{3})(?:\s|$)", " " + element.text.strip())
    return int(match.group(1)) if match else None


def _text(element: Any) -> str | None:
    if element is None or not element.text:
        return None
    return element.text.strip() or None


@dataclass(eq=False)
class _Entry:
    lock: threading.Lock = field(default_factory=threading.Lock)
    committed: bool = False
    addressbook_url: str = ""
    token: str | None = None
    cards: dict[str, Any] = field(default_factory=dict)
    dirty: set[str] = field(default_factory=set)
    last_used: float = 0.0


class ContactCache:
    """Per-credential address-book cache. See the module docstring for its guarantees.

    Locking: ``_map_lock`` guards ``_entries`` (committed, least recently used first) and
    ``_pending`` (first loads in progress) and is never held across I/O. Each entry's own
    lock serialises that account's network work. A thread holding an entry lock may take
    the map lock only to commit or abandon a first load. Holders of the map lock never
    block on an entry lock: eviction and expiry use ``acquire(False)`` and skip busy
    entries.
    """

    def __init__(self, max_accounts: int, idle_seconds: float, *,
                 build_card: Callable[[str, str | None, str], Any | None],
                 clock: Callable[[], float] = time.monotonic, sweep_interval: float | None = 60.0):
        self._max_accounts = max_accounts
        self._idle_seconds = idle_seconds
        self._build_card = build_card
        self._clock = clock
        self._sweep_interval = sweep_interval
        self._secret = secrets.token_bytes(32)
        self._map_lock = threading.Lock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._pending: dict[str, _Entry] = {}
        self._sweeper: threading.Thread | None = None
        self._stop = threading.Event()

    # -- public API --------------------------------------------------------------------------
    def cards(self, session: requests.Session, credentials: tuple[str, str], *,
              discover: Callable[[requests.Session], str],
              fetch_all: Callable[[requests.Session, str], list[dict]]) -> list[Any]:
        """Return the account's current cards after checking iCloud for changes.

        ``discover(session)`` returns the address-book URL, and
        ``fetch_all(session, url)`` returns every card as ``{"url", "data", "etag"}``.

        Raises ``requests.HTTPError`` (including 401/403) or another exception unchanged,
        and ``ValueError`` when iCloud's reply is incomplete. On any error nothing is
        returned and the entry's stored state is unchanged.
        """
        key = self._key(credentials)
        self.sweep()
        while True:
            with self._map_lock:
                entry = self._entries.get(key)
                if entry is not None:
                    self._entries.move_to_end(key)
                else:
                    entry = self._pending.setdefault(key, _Entry())
            with entry.lock:
                if not entry.committed and not self._is_current(key, entry):
                    continue  # a failed first load abandoned this entry; start over
                try:
                    if not entry.committed or entry.token is None:
                        self._load(entry, session, discover, fetch_all, "cold")
                    else:
                        try:
                            self._sync(entry, session)
                        except _Rebuild:
                            self._load(entry, session, discover, fetch_all, "rebuild")
                except BaseException:
                    if not entry.committed:
                        with self._map_lock:
                            if self._pending.get(key) is entry:
                                del self._pending[key]
                    raise
                entry.last_used = self._clock()
                if not entry.committed:
                    self._commit(key, entry)
                return list(entry.cards.values())

    def mark_dirty(self, credentials: tuple[str, str], url: str) -> None:
        """Make the next ``cards()`` call re-fetch ``url`` from iCloud.

        Called after the server's own create, update or delete, so the change is visible
        even if iCloud's change report lags. The card is re-fetched rather than built from
        the local copy, so the cache always holds iCloud's version.
        """
        with self._map_lock:
            entry = self._entries.get(self._key(credentials))
        if entry is None:
            return
        with entry.lock:
            entry.dirty.add(canonical_url(url))

    def sweep(self) -> None:
        """Discard entries idle for longer than ``idle_seconds``, skipping busy ones."""
        now = self._clock()
        with self._map_lock:
            for key, entry in list(self._entries.items()):
                if now - entry.last_used <= self._idle_seconds or not entry.lock.acquire(False):
                    continue
                try:
                    del self._entries[key]
                finally:
                    entry.lock.release()

    def clear(self) -> None:
        """Drop every entry (used by tests and when the cache is reconfigured)."""
        with self._map_lock:
            self._entries.clear()
            self._pending.clear()

    def close(self) -> None:
        """Stop the background sweeper, if one is running."""
        self._stop.set()

    # -- internals ---------------------------------------------------------------------------
    def _key(self, credentials: tuple[str, str]) -> str:
        email, password = credentials
        material = f"{email.strip().lower()}\0{password}".encode()
        return hmac.new(self._secret, material, hashlib.sha256).hexdigest()

    def _is_current(self, key: str, entry: _Entry) -> bool:
        with self._map_lock:
            return self._pending.get(key) is entry or self._entries.get(key) is entry

    def _commit(self, key: str, entry: _Entry) -> None:
        with self._map_lock:
            self._pending.pop(key, None)
            self._entries[key] = entry
            entry.committed = True
            for old_key, old in list(self._entries.items()):
                if len(self._entries) <= self._max_accounts:
                    break
                if old is entry or not old.lock.acquire(False):
                    continue
                try:
                    del self._entries[old_key]
                finally:
                    old.lock.release()
        self._ensure_sweeper()

    def _ensure_sweeper(self) -> None:
        if not self._sweep_interval or (self._sweeper and self._sweeper.is_alive()):
            return
        with self._map_lock:
            if self._sweeper and self._sweeper.is_alive():
                return

            def run() -> None:
                while not self._stop.wait(self._sweep_interval):
                    self.sweep()
            self._sweeper = threading.Thread(target=run, name="contacts-cache-sweeper", daemon=True)
            self._sweeper.start()

    def _load(self, entry: _Entry, session, discover, fetch_all, path: str) -> None:
        """Rediscover, take the sync token, then download every card; commit all at once."""
        url = discover(session)
        token = self._sync_token(session, url)  # before the download: later changes get re-reported
        cards = {}
        for item in fetch_all(session, url):
            if item.get("url"):
                card = self._build_card(item["url"], (item.get("etag") or "").strip() or None, item["data"])
                if card is not None:
                    cards[canonical_url(item["url"])] = card
        entry.addressbook_url, entry.token, entry.cards, entry.dirty = url, token, cards, set()
        logger.debug("contacts cache %s load: %d cards", path, len(cards))

    def _sync_token(self, session, url: str) -> str | None:
        body = ('<?xml version="1.0" encoding="UTF-8"?><d:propfind xmlns:d="DAV:">'
                "<d:prop><d:sync-token/></d:prop></d:propfind>")
        response = session.request("PROPFIND", url, data=body, headers={"Depth": "0"}, timeout=TIMEOUT)
        response.raise_for_status()
        element = ET.fromstring(response.content).find(".//d:sync-token", NS)
        return element.text.strip() if element is not None and element.text else None

    def _sync(self, entry: _Entry, session) -> None:
        """Apply changes since ``entry.token``; raises ``_Rebuild`` when that is impossible."""
        body = ('<?xml version="1.0" encoding="UTF-8"?><d:sync-collection xmlns:d="DAV:">'
                f"<d:sync-token>{escape(entry.token or '')}</d:sync-token><d:sync-level>1</d:sync-level>"
                "<d:prop><d:getetag/></d:prop></d:sync-collection>")
        response = session.request("REPORT", entry.addressbook_url, data=body,
                                   headers={"Depth": "1"}, timeout=TIMEOUT)
        status = response.status_code
        if status in (404, 409, 410, 412, 507) or (status == 403 and "valid-sync-token" in response.text):
            raise _Rebuild()
        response.raise_for_status()
        root = ET.fromstring(response.content)
        collection = urlsplit(entry.addressbook_url).path.rstrip("/")
        to_fetch, deleted = set(entry.dirty), set()
        for resp in root.findall("d:response", NS):
            href = resp.find("d:href", NS)
            if href is None or not href.text:
                continue
            url = urljoin(entry.addressbook_url, href.text.strip())
            code = _status_code(resp.find("d:status", NS))
            if urlsplit(url).path.rstrip("/") == collection:
                if code == 507:
                    raise _Rebuild()  # truncated report: the delta is incomplete
                continue
            key = canonical_url(url)
            if code in (404, 410):
                deleted.add(key)
                continue
            etag = resp.find(".//d:getetag", NS)
            cached = entry.cards.get(key)
            if cached is None or cached.etag != _text(etag):
                to_fetch.add(key)
        token = root.find("d:sync-token", NS)
        if token is None or not token.text:
            raise ValueError("iCloud returned no sync token with its change report; retry the call")
        to_fetch -= deleted
        staged = {k: v for k, v in entry.cards.items() if k not in deleted}
        for key, result in self._multiget(session, entry.addressbook_url, sorted(to_fetch)).items():
            if result is None:
                staged.pop(key, None)
                continue
            card = self._build_card(key, result[0], result[1])
            if card is None:
                logger.debug("contacts cache: dropping unparsable card %s", key)
                staged.pop(key, None)
            else:
                staged[key] = card
        entry.cards, entry.token, entry.dirty = staged, token.text.strip(), set()
        logger.debug("contacts cache warm: %d changed, %d deleted", len(to_fetch), len(deleted))

    def _multiget(self, session, book_url: str, keys: list[str]) -> dict[str, tuple[str | None, str] | None]:
        """Fetch ``keys``; maps each to ``(etag, vcard)`` or ``None`` when iCloud says 404.

        Raises ``ValueError`` if a requested card is missing from the reply, so an
        incomplete delta is never committed.
        """
        results: dict[str, tuple[str | None, str] | None] = {}
        for start in range(0, len(keys), MULTIGET_CHUNK):
            chunk = keys[start:start + MULTIGET_CHUNK]
            hrefs = "".join(f"<d:href>{escape(urlsplit(k).path)}</d:href>" for k in chunk)
            body = ('<?xml version="1.0" encoding="UTF-8"?><card:addressbook-multiget xmlns:d="DAV:" '
                    'xmlns:card="urn:ietf:params:xml:ns:carddav"><d:prop><d:getetag/><card:address-data/>'
                    f"</d:prop>{hrefs}</card:addressbook-multiget>")
            response = session.request("REPORT", book_url, data=body, headers={"Depth": "1"}, timeout=TIMEOUT)
            response.raise_for_status()
            for resp in ET.fromstring(response.content).findall("d:response", NS):
                href = resp.find("d:href", NS)
                if href is None or not href.text:
                    continue
                key = canonical_url(urljoin(book_url, href.text.strip()))
                if _status_code(resp.find("d:status", NS)) in (404, 410):
                    results[key] = None
                    continue
                data = resp.find(".//card:address-data", NS)
                etag = resp.find(".//d:getetag", NS)
                if data is not None and data.text:
                    results[key] = (_text(etag), data.text)
            missing = [k for k in chunk if k not in results]
            if missing:
                raise ValueError(f"iCloud did not return {len(missing)} changed contact(s); retry the call")
        return results
