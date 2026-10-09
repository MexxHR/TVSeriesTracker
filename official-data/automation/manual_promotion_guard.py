"""Independent pre/post checks for the human-dispatched production promotion."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import re
import subprocess
import sys

import discovery
import monitored
import update


PROTECTED = (
    "official-data/official_series_data.json",
    "official-data/history/changes.jsonl",
    "official-data/sources.json",
    "official-data/monitored_series.json",
    "official-data/history/monitored_changes.jsonl",
)
WRITABLE = set(PROTECTED[:2])


def positive_id(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", value) or len(value) > 18:
        raise ValueError("tmdb_id must be a positive decimal integer")
    return int(value)


def changed_paths(root: Path) -> set[str]:
    def paths(*args: str) -> set[str]:
        raw = subprocess.check_output(["git", *args], cwd=root)
        return set(raw.decode("utf-8").strip("\0").split("\0")) - {""}
    return paths("diff", "--name-only", "--no-renames", "-z", "HEAD") | paths(
        "ls-files", "--others", "--exclude-standard", "-z")


def _inputs(root: Path, tmdb_id: int) -> tuple[dict, dict, dict]:
    trusted = json.loads((root / PROTECTED[2]).read_text(encoding="utf-8"))
    update.validate_registry(trusted)
    production = json.loads((root / PROTECTED[0]).read_text(encoding="utf-8"))
    update.validate_dataset(production, trusted, production,
                            allow_supplemental_ids={row["tmdbId"] for row in production["series"]
                                                    if row["tmdbId"] not in {item["tmdbId"] for item in trusted["series"]}})
    staging, _ = monitored.load_registry(root / PROTECTED[3])
    monitored.validate_registry(staging)
    row = next((item for item in staging["series"] if item["tmdbId"] == tmdb_id), None)
    if row is None:
        raise ValueError("Requested monitored record is absent")
    return trusted, production, row


def preflight(root: Path, snapshot: Path, tmdb_id: int) -> None:
    if changed_paths(root):
        raise ValueError("Repository must start clean")
    _inputs(root, tmdb_id)
    update.audit_bytes(root / PROTECTED[1])
    for relative in PROTECTED:
        source = root / relative
        data = source.read_bytes()
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def verify(root: Path, snapshot: Path, tmdb_id: int, result: dict) -> bool:
    before = {name: (snapshot / name).read_bytes() for name in PROTECTED}
    after = {name: (root / name).read_bytes() for name in PROTECTED}
    changed = changed_paths(root)
    if changed - WRITABLE:
        raise ValueError("Unexpected repository changes: " + ", ".join(sorted(changed - WRITABLE)))
    if any(before[name] != after[name] for name in PROTECTED[2:]):
        raise ValueError("Trusted or monitored input changed")
    if result.get("tmdbId") != tmdb_id or result.get("dryRun") is not False or result.get("fatal"):
        raise ValueError("Promotion report identity or mode is invalid")
    state = result.get("promotionState")
    if state == "NO_CHANGE":
        if result.get("wouldPublish") is not False or before != after or changed:
            raise ValueError("NO_CHANGE rewrote production data")
        return False
    if state != "PROMOTED":
        raise ValueError("Promotion was not approved: " + str(state))
    if result.get("wouldPublish") is not True or result.get("validationResult") != "passed":
        raise ValueError("PROMOTED report lacks successful validation")
    if changed != WRITABLE or any(before[name] == after[name] for name in WRITABLE):
        raise ValueError("PROMOTED must change only canonical JSON and production audit")

    trusted, current, monitored_row = _inputs(root, tmdb_id)
    old = json.loads(before[PROTECTED[0]])
    update.validate_dataset(current, trusted, old, allow_supplemental_ids={tmdb_id})
    old_by_id = {row["tmdbId"]: row for row in old["series"]}
    new_by_id = {row["tmdbId"]: row for row in current["series"]}
    if len(new_by_id) != len(current["series"]):
        raise ValueError("Duplicate canonical tmdbId")
    different = {identifier for identifier in old_by_id.keys() | new_by_id.keys()
                 if old_by_id.get(identifier) != new_by_id.get(identifier)}
    if different != {tmdb_id}:
        raise ValueError("Canonical change is not limited to requested tmdbId")
    previous_time = dt.datetime.fromisoformat(old["generatedAt"].replace("Z", "+00:00"))
    current_time = dt.datetime.fromisoformat(current["generatedAt"].replace("Z", "+00:00"))
    if current_time <= previous_time:
        raise ValueError("generatedAt did not advance")
    record = new_by_id[tmdb_id]
    if result.get("oldProductionRecord") != old_by_id.get(tmdb_id) or result.get("proposedProductionRecord") != record:
        raise ValueError("Promotion report differs from canonical change")
    domains = discovery.provider_specs()[monitored_row["provider"]]["officialDomains"]
    urls = result.get("sourceUrls")
    if not isinstance(urls, list) or not urls or not all(update.allowed(url, domains) for url in urls):
        raise ValueError("Promotion lacks official source URLs")
    if record["sourceUrl"] not in urls or not update.allowed(record["sourceUrl"], domains):
        raise ValueError("Canonical source is outside official provider domains")
    expected_fields = sorted(key for key in set(old_by_id.get(tmdb_id) or {}) | set(record)
                             if (old_by_id.get(tmdb_id) or {}).get(key) != record.get(key))
    if result.get("changedFields") != expected_fields:
        raise ValueError("Reported changed fields differ from canonical record")

    if tmdb_id == 111110:
        expected = {"title": "ONE PIECE", "nextSeasonNumber": 3, "status": "RENEWED",
                    "releaseDate": None, "releaseYear": 2027, "sourceName": "Netflix Tudum",
                    "sourceUrl": "https://www.netflix.com/tudum/articles/one-piece-renewed-season-3",
                    "announcementDate": None}
        if any(record.get(key) != value for key, value in expected.items()):
            raise ValueError("ONE PIECE first-promotion acceptance facts differ")

    old_audit, new_audit = before[PROTECTED[1]], after[PROTECTED[1]]
    if not new_audit.startswith(old_audit):
        raise ValueError("Production audit history was rewritten")
    appended = new_audit[len(old_audit):]
    if appended.count(b"\n") != 1 or not appended.endswith(b"\n"):
        raise ValueError("Expected exactly one appended audit entry")
    entry = json.loads(appended)
    if (entry.get("tmdbId") != tmdb_id or entry.get("old") != old_by_id.get(tmdb_id)
            or entry.get("new") != record or entry.get("source") != "monitored automatic promotion"
            or entry.get("sourceUrls") != urls or entry.get("changedFields") != expected_fields
            or entry.get("monitoredState") != "VERIFIED_FACTS"
            or entry.get("title") != record["title"]
            or entry.get("promotionReason") != "VERIFIED_FACTS passed independent production gate"):
        raise ValueError("Production audit does not match requested promotion")
    try:
        audit_time = dt.datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Production audit timestamp is invalid") from exc
    if audit_time != current_time:
        raise ValueError("Production audit timestamp differs from generatedAt")
    supporting = entry.get("supportingSourceUrls")
    if (entry.get("provider") != record["sourceName"]
            or entry.get("sourceUrl") != record["sourceUrl"]
            or not isinstance(entry.get("rule"), str) or not entry["rule"]
            or not isinstance(supporting, list)
            or not all(isinstance(url, str) and url in urls and update.allowed(url, domains)
                       for url in supporting)):
        raise ValueError("Production audit provider or supporting provenance is invalid")
    if any(key in json.dumps(entry).lower() for key in ('"token"', '"secret"', '"clientip"', '"ipaddress"')):
        raise ValueError("Sensitive field in production audit")
    return True


def main() -> None:
    command = sys.argv[1]
    if command == "validate-id" and len(sys.argv) == 3:
        print(positive_id(sys.argv[2]))
        return
    if command == "preflight" and len(sys.argv) == 4:
        preflight(Path.cwd(), Path(sys.argv[3]), positive_id(sys.argv[2]))
        print("Preflight validated; five protected files snapshotted outside checkout")
        return
    if command == "verify" and len(sys.argv) == 5:
        report = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
        print("commit=true" if verify(Path.cwd(), Path(sys.argv[3]), positive_id(sys.argv[2]), report)
              else "commit=false")
        return
    raise SystemExit("Usage: manual_promotion_guard.py validate-id ID | preflight ID SNAPSHOT | verify ID SNAPSHOT REPORT")


if __name__ == "__main__":
    main()
