"""Replay contact edits in memory over a snapshot of a real iCloud address book.

Never writes to iCloud. ``--fetch`` downloads a fresh snapshot with every HTTP method other
than GET/HEAD/OPTIONS/PROPFIND/REPORT blocked, and it checks that the block works before the
first request. Keep the snapshot outside the repository: it holds personal data. The replay
prints, and optionally writes, aggregate counts only, never contact values.

Usage (from the repository root, inside the project virtualenv):
    python -I .specs/contact-fields-completion/execution/replay_snapshot.py --fetch /tmp/snapshot.vcf
    python -I .specs/contact-fields-completion/execution/replay_snapshot.py /tmp/snapshot.vcf --results out.md
"""

import argparse
import collections
import copy
import re
import sys
from pathlib import Path

import requests
import vobject

from icloud_mcp import contacts as C

READ_METHODS = {"GET", "HEAD", "OPTIONS", "PROPFIND", "REPORT"}
APPLE_ONLY = ("X-ADDRESSING-GRAMMAR", "X-ACTIVITY-ALERT", "X-ALTBDAY")
READ_ONLY_KEYS = {"id", "url", "alternate_birthday", "ringtone", "text_tone"}
POSITIONAL = ("phones", "emails", "addresses", "organization", "title", "notes", "birthday", "urls",
              "social_profiles", "dates")
TEXT_PROPS = ("FN", "NICKNAME", "NOTE", "TITLE", "X-MAIDENNAME", "X-PHONETIC-FIRST-NAME",
              "X-PHONETIC-MIDDLE-NAME", "X-PHONETIC-LAST-NAME", "X-PRONUNCIATION-FIRST-NAME",
              "X-PRONUNCIATION-LAST-NAME", "X-PHONETIC-ORG")


def fetch(path: Path) -> None:
    original = requests.Session.request

    def guarded(self, method, url, *args, **kwargs):
        if method.upper() not in READ_METHODS:
            raise RuntimeError(f"blocked {method}: this harness is read-only")
        return original(self, method, url, *args, **kwargs)

    requests.Session.request = guarded
    for method in ("put", "delete", "post"):
        try:
            getattr(requests.Session(), method)("https://contacts.icloud.com/never-sent")
        except RuntimeError:
            continue
        sys.exit(f"write guard failed for {method.upper()}; aborting")
    from icloud_mcp.auth import require_auth

    session = C._get_carddav_session(*require_auth())
    items = C._fetch_all_vcards(session, C._default_addressbook_url(session))
    path.write_text("\n".join(item["data"].strip() for item in items), encoding="utf-8")
    print(f"fetched {len(items)} vCards (writes blocked)")


def lines(card) -> collections.Counter:
    text = C._serialize(copy.deepcopy(card)).decode().replace("\r\n ", "")
    return collections.Counter(line for line in text.split("\r\n") if line)


def prop_name(line: str) -> str:
    return line.split(":", 1)[0].split(";", 1)[0].split(".", 1)[-1].upper()


def apply(card, **fields):
    edited = copy.deepcopy(card)
    positional = [fields.pop(key, None) for key in POSITIONAL]
    C._apply_fields(edited, *positional, **fields)
    return edited


def write_back(data: dict) -> dict:
    return {k: v for k, v in data.items() if k not in READ_ONLY_KEYS}


# Per-field edits: a function building the edit from the read, and the vCard properties the
# edit may touch (labels and Apple annotations in the entry's own group included).
GROUP_ANNOTATIONS = {"X-ABLABEL", "X-ABADR", "X-APPLE-SUBLOCALITY", "X-APPLE-SUBADMINISTRATIVEAREA"}
EDITS = {
    "rename (display name only)": (lambda d: {"name": (d["name"] + " Zz").strip()} if d["name"] else None, {"N", "FN"}),
    "set prefix": (lambda d: {"prefix": "Zz."}, {"N", "FN"}),
    "set middle name": (lambda d: {"middle_name": "Zz"}, {"N", "FN"}),
    "set phonetic first name": (lambda d: {"phonetic_first_name": "zz"}, {"X-PHONETIC-FIRST-NAME"}),
    "set pronunciation first name": (lambda d: {"pronunciation_first_name": "zz"}, {"X-PRONUNCIATION-FIRST-NAME"}),
    "set nickname": (lambda d: {"nickname": "Zz"}, {"NICKNAME"}),
    "clear nickname": (lambda d: {"nickname": ""} if d["nickname"] else None, {"NICKNAME"}),
    "set maiden name": (lambda d: {"maiden_name": "Zz"}, {"X-MAIDENNAME"}),
    "set department": (lambda d: {"department": "Zz"}, {"ORG"}),
    "set phonetic organization": (lambda d: {"phonetic_organization": "zz"}, {"X-PHONETIC-ORG"}),
    "toggle is_company": (lambda d: {"is_company": not d["is_company"]} if d["organization"] else None, {"X-ABSHOWAS"}),
    "add related name": (lambda d: {"related_names": d["related_names"] + [{"name": "Zz", "label": "friend"}]},
                         {"X-ABRELATEDNAMES"} | GROUP_ANNOTATIONS),
    "add instant message": (lambda d: {"instant_messages": d["instant_messages"] + [{"service": "Skype", "username": "zz"}]},
                            {"IMPP"} | GROUP_ANNOTATIONS),
}


def unescaped_comma_count(raw: str) -> int:
    count = 0
    for line in raw.replace("\r\n ", "").replace("\n ", "").splitlines():
        name, sep, value = line.partition(":")
        if sep and name.split(";")[0].split(".")[-1].upper() in TEXT_PROPS and re.search(r"(?<!\\),", value):
            count += 1
    return count


def replay(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    cards = [vobject.readOne(chunk) for chunk in re.findall(r"BEGIN:VCARD.*?END:VCARD", raw, re.S)]
    people = [card for card in cards if not C._is_group(card)]
    stats = collections.Counter()
    for card in people:
        data = C._vcard_to_dict(card, "u")
        apple = {line for line in lines(card) if prop_name(line) in APPLE_ONLY}
        stats["company cards with empty organization"] += data["is_company"] and not data["organization"]
        stats["IMPP without X-SERVICE-TYPE"] += sum("X-SERVICE-TYPE" not in p.params for p in card.contents.get("impp", []))
        org = getattr(card, "org", None)
        stats["ORG with 3+ components"] += bool(org is not None and isinstance(org.value, list) and len(org.value) >= 3)
        try:
            edited = apply(card, **write_back(data))
            stats["write-back: unchanged" if lines(edited) == lines(card) else "write-back: CHANGED"] += 1
        except Exception as exc:  # noqa: BLE001 - every failure is a finding to count
            stats[f"write-back: FAILED ({type(exc).__name__})"] += 1
        for label, (build, allowed) in EDITS.items():
            edit = build(data)
            if edit is None:
                continue
            try:
                edited = apply(card, **edit)
            except Exception as exc:  # noqa: BLE001
                stats[f"{label}: FAILED ({type(exc).__name__})"] += 1
                continue
            touched = {prop_name(line) for line in (lines(edited) - lines(card)) + (lines(card) - lines(edited))}
            kept_apple = apple <= set(lines(edited))
            ok = touched and touched <= allowed and kept_apple
            stats[f"{label}: {'only its own lines' if ok else 'OTHER CHANGES'}"] += 1
    stats["people"] = len(people)
    stats["lists (skipped)"] = len(cards) - len(people)
    stats["unescaped commas in single-value text lines"] = unescaped_comma_count(raw)
    stats["cards with Apple-only lines"] = sum(
        1 for card in people if any(prop_name(line) in APPLE_ONLY for line in lines(card)))
    return dict(sorted(stats.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--fetch", action="store_true", help="download a fresh read-only snapshot first")
    parser.add_argument("--results", type=Path, help="write aggregate counts as Markdown")
    args = parser.parse_args()
    if args.fetch:
        fetch(args.snapshot)
    results = replay(args.snapshot)
    for key, value in results.items():
        print(f"{value:6}  {key}")
    if args.results:
        rows = "\n".join(f"| {key} | {value} |" for key, value in results.items())
        args.results.write_text(f"| Measure | Count |\n|---|---:|\n{rows}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
