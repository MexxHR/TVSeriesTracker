"""Stage boundary for one Android-request monitored update before promotion."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import manual_promotion_guard as production_guard
import monitored
import update


MONITORED_WRITABLE = set(production_guard.PROTECTED[3:])


def gate_enabled(value: str) -> bool:
    """The repository variable is a kill switch, not a truthy string."""
    return value == "true"


def _validate_inputs(root: Path) -> dict:
    trusted = json.loads((root / production_guard.PROTECTED[2]).read_text(encoding="utf-8"))
    update.validate_registry(trusted)
    canonical = json.loads((root / production_guard.PROTECTED[0]).read_text(encoding="utf-8"))
    trusted_ids = {row["tmdbId"] for row in trusted["series"]}
    supplemental = {row["tmdbId"] for row in canonical["series"] if row["tmdbId"] not in trusted_ids}
    update.validate_dataset(canonical, trusted, canonical, allow_supplemental_ids=supplemental)
    update.audit_bytes(root / production_guard.PROTECTED[1])
    registry, _ = monitored.load_registry(root / production_guard.PROTECTED[3])
    monitored.validate_registry(registry)
    monitored.audit_bytes(root / production_guard.PROTECTED[4])
    return registry


def snapshot_stage(root: Path, snapshot: Path, tmdb_id: int) -> None:
    if tmdb_id <= 0 or production_guard.changed_paths(root):
        raise ValueError("Requested ID must be positive and checkout clean before monitored processing")
    _validate_inputs(root)
    for name in production_guard.PROTECTED:
        destination = snapshot / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((root / name).read_bytes())


def verify_stage(root: Path, snapshot: Path, tmdb_id: int, report: dict) -> tuple[str, bool]:
    if report.get("tmdbId") != tmdb_id or report.get("dryRun") is not False:
        raise ValueError("Monitored result identity or mode is invalid")
    state = report.get("pipelineState")
    if state not in monitored.STATES | {"ALREADY_TRUSTED"}:
        raise ValueError("Unknown monitored state")
    changed = production_guard.changed_paths(root)
    if changed - MONITORED_WRITABLE:
        raise ValueError("Unexpected monitored-stage files: " + ", ".join(sorted(changed - MONITORED_WRITABLE)))
    before = {name: (snapshot / name).read_bytes() for name in production_guard.PROTECTED}
    after = {name: (root / name).read_bytes() for name in production_guard.PROTECTED}
    if any(before[name] != after[name] for name in production_guard.PROTECTED[:3]):
        raise ValueError("Monitored stage changed production or trusted-source data")
    registry = _validate_inputs(root)
    old = json.loads(before[production_guard.PROTECTED[3]])
    old_by_id = {row["tmdbId"]: row for row in old["series"]}
    new_by_id = {row["tmdbId"]: row for row in registry["series"]}
    changed_ids = {identifier for identifier in old_by_id.keys() | new_by_id.keys()
                   if old_by_id.get(identifier) != new_by_id.get(identifier)}
    if changed_ids - {tmdb_id}:
        raise ValueError("Monitored stage changed another tmdbId")
    if state == "ALREADY_TRUSTED":
        if changed or before != after or report.get("registryChange") != "none":
            raise ValueError("Trusted-source request changed monitored staging")
        return state, False
    row = new_by_id.get(tmdb_id)
    if row is None or row.get("discoveryState") != state:
        raise ValueError("Monitored report differs from persisted requested row")
    for report_key, row_key in (("title", "title"), ("provider", "provider"),
                                ("candidateUrls", "candidateUrls"),
                                ("verifiedFacts", "verifiedFacts"),
                                ("sourceEvidence", "sourceEvidence"),
                                ("parserResult", "parserResult"),
                                ("validationResult", "validationResult")):
        if report.get(report_key) != row.get(row_key):
            raise ValueError("Monitored report evidence differs from persisted requested row")
    if state == "VERIFIED_FACTS" and (row.get("parserResult") != "facts"
                                      or row.get("validationResult") != "passed"
                                      or not row.get("eligibleSourceUrls")):
        raise ValueError("VERIFIED_FACTS lacks persisted parser eligibility")
    wrote = bool(changed_ids)
    if wrote:
        if changed != MONITORED_WRITABLE or report.get("registryChange") not in ("added", "updated"):
            raise ValueError("Monitored write must change registry and audit together")
        old_audit, new_audit = before[production_guard.PROTECTED[4]], after[production_guard.PROTECTED[4]]
        if not new_audit.startswith(old_audit):
            raise ValueError("Monitored audit history was rewritten")
        appended = new_audit[len(old_audit):]
        if appended.count(b"\n") != 1 or not appended.endswith(b"\n"):
            raise ValueError("Expected one monitored audit entry")
        entry = json.loads(appended)
        if (entry.get("tmdbId") != tmdb_id or entry.get("newState") != state
                or entry.get("oldState") != (old_by_id.get(tmdb_id) or {}).get("discoveryState")):
            raise ValueError("Monitored audit does not match requested update")
    elif changed or before != after or report.get("registryChange") != "none":
        raise ValueError("Unchanged monitored result rewrote repository state")
    return state, wrote


def main() -> None:
    command = sys.argv[1]
    if command == "gate" and len(sys.argv) == 3:
        print("true" if gate_enabled(sys.argv[2]) else "false")
        return
    if command == "snapshot" and len(sys.argv) == 4:
        snapshot_stage(Path.cwd(), Path(sys.argv[3]), production_guard.positive_id(sys.argv[2]))
        return
    if command == "verify-stage" and len(sys.argv) == 5:
        report = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
        state, changed = verify_stage(Path.cwd(), Path(sys.argv[3]),
                                      production_guard.positive_id(sys.argv[2]), report)
        print("state=" + state)
        print("monitored_changed=" + ("true" if changed else "false"))
        return
    raise SystemExit("Usage: automatic_promotion.py gate VALUE | snapshot ID DIR | verify-stage ID DIR REPORT")


if __name__ == "__main__":
    main()
