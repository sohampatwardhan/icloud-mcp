"""In-memory CardDAV server for tests: an address book with sync tokens.

Supports the requests the contacts code makes (PROPFIND sync-token, REPORT
addressbook-query / sync-collection / addressbook-multiget, GET, PUT, DELETE) with a
request log and switches for the failure modes the cache must survive: rejected or
truncated sync reports, error statuses, multiget responses that omit cards, and a change
report that lags behind the server's own writes.
"""

import itertools
import re
from urllib.parse import unquote, urljoin, urlsplit
from xml.sax.saxutils import escape

import requests

BOOK = "https://p1-contacts.icloud.com/123/carddavhome/card/"
BOOK_PATH = urlsplit(BOOK).path


def vcard(name: str, extra: str = "") -> str:
    family, _, given = name.rpartition(" ")
    return f"BEGIN:VCARD\nVERSION:3.0\nFN:{name}\nN:{family};{given};;;\n{extra}END:VCARD\n"


class FakeResponse:
    def __init__(self, status: int, body: str = "", headers: dict | None = None):
        self.status_code, self.text, self.headers = status, body, headers or {}
        self.content = body.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)


class FakeCardDAV:
    """Acts as a ``requests.Session`` for one address book."""

    def __init__(self):
        self.cards: dict[str, tuple[str, str]] = {}   # path -> (etag, vcard text)
        self.version = 0
        self.changes: list[tuple[int, str]] = []        # (version, path) of every change
        self.log: list[tuple[str, str]] = []            # (method, kind)
        self.errors: dict[str, list[int | tuple[int, str]]] = {}  # kind -> queued statuses
        self.truncate_next_sync = False
        self.omit_from_multiget: set[str] = set()
        self.report_writes = True                       # False: own writes missing from sync
        self.writes: list[tuple[str, str, dict]] = []   # (method, url, headers) of every PUT/DELETE
        self.before: dict[str, list] = {}               # kind -> queued callables run before handling
        self._headers: dict = {}
        self._etags = itertools.count(1)

    # -- store helpers used by tests ---------------------------------------------------
    def put_card(self, name: str, text: str | None = None, *, report: bool = True) -> str:
        path = f"{BOOK_PATH}{name}.vcf"
        self.cards[path] = (f'"etag-{next(self._etags)}"', text or vcard(name.replace("-", " ").title()))
        self._changed(path, report)
        return BOOK + f"{name}.vcf"

    def delete_card(self, name: str, *, report: bool = True) -> None:
        path = f"{BOOK_PATH}{name}.vcf"
        self.cards.pop(path, None)
        self._changed(path, report)

    def _changed(self, path: str, report: bool) -> None:
        self.version += 1
        if report:
            self.changes.append((self.version, path))

    def kinds(self) -> list[str]:
        return [kind for _, kind in self.log]

    # -- requests.Session interface ----------------------------------------------------
    def request(self, method, url, data=None, headers=None, timeout=None):
        body = data.decode() if isinstance(data, bytes) else (data or "")
        kind = self._kind(method, body)
        self.log.append((method.upper(), kind))
        self._headers = dict(headers or {})
        if kind in ("put", "delete"):
            self.writes.append((method.upper(), url, self._headers))
        hooks = self.before.get(kind)
        if hooks:
            hooks.pop(0)()
        queued = self.errors.get(kind)
        if queued:
            status = queued.pop(0)
            text = ""
            if isinstance(status, tuple):
                status, text = status
            return FakeResponse(status, text)
        return getattr(self, f"_do_{kind}")(url, body)

    def get(self, url, timeout=None, **kw):
        return self.request("GET", url)

    def put(self, url, data=None, headers=None, timeout=None):
        return self.request("PUT", url, data=data, headers=headers)

    def delete(self, url, timeout=None, headers=None, **kw):
        return self.request("DELETE", url, headers=headers)

    @staticmethod
    def _kind(method, body):
        method = method.upper()
        if method in ("GET", "PUT", "DELETE"):
            return method.lower()
        if method == "PROPFIND" and "sync-token" in body:
            return "sync_token"
        for kind in ("sync-collection", "addressbook-multiget", "addressbook-query"):
            if kind in body:
                return kind.replace("-", "_")
        raise AssertionError(f"unexpected {method} request")

    # -- handlers ------------------------------------------------------------------------
    def _token(self, version=None):
        return f"https://fake/sync/{self.version if version is None else version}"

    def _do_sync_token(self, url, body):
        return FakeResponse(207, f'<d:multistatus xmlns:d="DAV:"><d:response><d:href>{BOOK_PATH}</d:href>'
                                 f'<d:propstat><d:prop><d:sync-token>{self._token()}</d:sync-token></d:prop>'
                                 '<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>')

    def _card_response(self, path, with_data):
        etag, text = self.cards[path]
        data = f"<card:address-data>{escape(text)}</card:address-data>" if with_data else ""
        return (f"<d:response><d:href>{path}</d:href><d:propstat><d:prop><d:getetag>{escape(etag)}</d:getetag>"
                f"{data}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")

    def _multistatus(self, inner, token=None):
        tail = f"<d:sync-token>{token}</d:sync-token>" if token else ""
        return FakeResponse(207, '<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
                                 f"{inner}{tail}</d:multistatus>")

    def _do_addressbook_query(self, url, body):
        return self._multistatus("".join(self._card_response(p, True) for p in self.cards))

    def _do_sync_collection(self, url, body):
        match = re.search(r"<d:sync-token>https://fake/sync/(\d+)</d:sync-token>", body)
        if not match or int(match.group(1)) > self.version:
            return FakeResponse(412, '<d:error xmlns:d="DAV:"><d:valid-sync-token/></d:error>')
        since = int(match.group(1))
        changed = list(dict.fromkeys(path for version, path in self.changes if version > since))
        parts = []
        for path in changed:
            if path in self.cards:
                parts.append(self._card_response(path, False))
            else:
                parts.append(f"<d:response><d:href>{path}</d:href><d:status>HTTP/1.1 404 Not Found</d:status></d:response>")
        if self.truncate_next_sync:
            self.truncate_next_sync = False
            parts = parts[:1] + [f"<d:response><d:href>{BOOK_PATH}</d:href>"
                                 "<d:status>HTTP/1.1 507 Insufficient Storage</d:status></d:response>"]
        parts.append(f"<d:response><d:href>{BOOK_PATH}</d:href><d:propstat><d:prop/>"
                     "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
        return self._multistatus("".join(parts), token=self._token())

    def _do_addressbook_multiget(self, url, body):
        parts = []
        for href in re.findall(r"<d:href>([^<]+)</d:href>", body):
            path = unquote(urlsplit(href).path)
            if path in self.omit_from_multiget:
                continue
            if path in self.cards:
                parts.append(self._card_response(path, True))
            else:
                parts.append(f"<d:response><d:href>{path}</d:href><d:status>HTTP/1.1 404 Not Found</d:status></d:response>")
        return self._multistatus("".join(parts))

    def _do_get(self, url, body):
        path = urlsplit(url).path
        if path not in self.cards:
            return FakeResponse(404)
        etag, text = self.cards[path]
        return FakeResponse(200, text, {"ETag": etag})

    def _precondition_failed(self, path):
        """Like iCloud: If-Match must name the stored ETag; If-None-Match: * needs no card."""
        match = self._headers.get("If-Match")
        if match is not None and (path not in self.cards or self.cards[path][0] != match):
            return True
        return self._headers.get("If-None-Match") == "*" and path in self.cards

    def _do_put(self, url, body):
        path = urlsplit(url).path
        if self._precondition_failed(path):
            return FakeResponse(412)
        # Like iCloud, store a normalised copy, so a client's own serialisation is not the stored card.
        self.cards[path] = (f'"etag-{next(self._etags)}"', body.replace("PRODID:", "PRODID:server-"))
        self._changed(path, self.report_writes)
        return FakeResponse(201, "", {"ETag": self.cards[path][0]})

    def _do_delete(self, url, body):
        path = urlsplit(url).path
        if path not in self.cards:
            return FakeResponse(404)
        if self._precondition_failed(path):
            return FakeResponse(412)
        del self.cards[path]
        self._changed(path, self.report_writes)
        return FakeResponse(204)


def absolute(path_or_url: str) -> str:
    return urljoin(BOOK, path_or_url)
