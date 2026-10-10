"""Read-only diagnostics for a Disney+ candidate on the GitHub runner.

All discovery, parsing, validation, and evidence replay are delegated to the
existing production modules. This module only records their outputs.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import discovery
import monitored
import promotion
import update


ROOT = Path(__file__).resolve().parents[2]
PROTECTED = (
    "official-data/official_series_data.json",
    "official-data/history/changes.jsonl",
    "official-data/sources.json",
    "official-data/monitored_series.json",
    "official-data/history/monitored_changes.jsonl",
)
FACT_FIELDS = ("status", "nextSeasonNumber", "releaseDate", "releaseYear",
               "sourceName", "sourceUrl", "announcementDate")


def parse_tmdb_id(raw: str) -> int:
    """Accept only a bounded positive ASCII decimal ID from workflow input."""
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,18}", raw):
        raise ValueError("TMDB ID must be 1-18 ASCII decimal digits")
    value = int(raw)
    if value == 0:
        raise ValueError("TMDB ID must be positive")
    return value


def protected_hashes(root: Path = ROOT) -> dict[str, str]:
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in PROTECTED}


def probe(tmdb_id: int, metadata: dict | None = None, *, processor=monitored.process,
          discoverer=discovery.discover) -> dict:
    if not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool) or tmdb_id <= 0:
        raise ValueError("TMDB ID must be positive")
    report = {
        "schemaVersion": 1,
        "reportType": "disney_plus_candidate_read_only_preview",
        "candidate": {"tmdbId": tmdb_id},
        "metadata": None,
        "routing": {"provider": None, "routeSignals": [], "reason": None},
        "discoveryDiagnostics": None,
        "monitoredPreview": None,
        "proposedMonitoredRecord": None,
        "independentReconstruction": {"result": "not_applicable"},
        "recommendedForLiveE2E": False,
    }
    # The normal monitored path uses these exact metadata and routing helpers.
    try:
        meta = metadata if metadata is not None else discovery.tmdb_metadata(
            tmdb_id, discovery.local_tmdb_token())
    except (OSError, UnicodeError, ValueError, update.AutomationError) as exc:
        report["routing"].update(reason="metadata_unavailable", factualProcessingStopped=True)
        report["metadataErrorType"] = type(exc).__name__
        report["monitoredPreview"] = {
            "tmdbId": tmdb_id, "pipelineState": "METADATA_UNAVAILABLE",
            "discoveryResult": "not_run", "parserResult": "skipped",
            "validationResult": "not_applicable", "dryRun": True,
        }
        return report
    if not isinstance(meta, dict) or meta.get("tmdbId") != tmdb_id:
        raise ValueError("TMDB metadata identity mismatch")
    provider, signals, reason = discovery.route_provider(meta, discovery.provider_specs())
    report["metadata"] = meta
    report["routing"] = {"provider": provider, "routeSignals": signals, "reason": reason}
    if provider != "DISNEY_PLUS":
        report["routing"]["factualProcessingStopped"] = True
        return report

    discovery_result = {}

    def capture_discovery(actual_meta, trusted):
        result = discoverer(actual_meta, trusted)
        discovery_result.update(result)
        return result

    result = processor(tmdb_id, dry_run=True, metadata=meta,
                       discoverer=capture_discovery)
    if result.get("tmdbId") != tmdb_id or result.get("dryRun") is not True or not isinstance(result.get("pipelineState"), str):
        raise ValueError("Invalid monitored dry-run result")
    report["discoveryDiagnostics"] = discovery_result
    report["monitoredPreview"] = result
    if result.get("provider") not in (None, provider):
        raise ValueError("Monitored provider differs from TMDB routing")

    row = result.get("proposedRecord")
    if row is None and result.get("registryChange") == "none":
        registry, _ = monitored.load_registry(monitored.MONITORED)
        row = next((item for item in registry["series"]
                    if item["tmdbId"] == tmdb_id), None)
    report["proposedMonitoredRecord"] = row
    if result.get("pipelineState") == "VERIFIED_FACTS":
        if result.get("parserResult") != "facts" or result.get("validationResult") != "passed":
            raise ValueError("Invalid verified-facts dry-run result")
        if row is None:
            raise ValueError("Verified preview has no monitored record for evidence replay")
        try:
            domains = discovery.provider_specs()[provider]["officialDomains"]
            facts = promotion._validate_evidence(row, domains)
            rebuilt = promotion._facts_summary(
                facts, row, dt.date.fromisoformat(row["lastChecked"]))
            report["independentReconstruction"] = {
                "result": "passed",
                "facts": {field: rebuilt.get(field) for field in FACT_FIELDS},
            }
            report["recommendedForLiveE2E"] = True
        except (update.AutomationError, ValueError) as exc:
            report["independentReconstruction"] = {
                "result": "failed", "errorType": type(exc).__name__,
                "mismatch": getattr(exc, "diagnostic", None),
            }
    return report


def summary_lines(report: dict) -> list[str]:
    meta = report.get("metadata") or {}
    routing = report.get("routing") or {}
    result = report.get("monitoredPreview") or {}
    facts = result.get("verifiedFacts") or {}
    reconstruction = report.get("independentReconstruction") or {}
    integrity = report.get("protectedFileIntegrity") or {}
    tmdb_id = (report.get("candidate") or {}).get("tmdbId")
    lines = [f"### Disney+ candidate {tmdb_id} read-only probe", "",
             f"TMDB title: {meta.get('title')}",
             f"TMDB ID: {tmdb_id}",
             f"Provider: {routing.get('provider')}",
             f"Route signals: {', '.join(routing.get('routeSignals') or []) or 'none'}",
             f"Discovery result: {result.get('discoveryResult', 'not_run')}",
             f"Eligible candidates: {result.get('eligibleCandidateCount', 0)}",
             f"Parsed sources: {result.get('parsedSourceCount', 0)}",
             f"Pipeline state: {result.get('pipelineState', 'not_run')}",
             f"Parser result: {result.get('parserResult', 'not_run')}",
             f"Validation result: {result.get('validationResult', 'not_run')}"]
    if result.get("pipelineState") == "VERIFIED_FACTS":
        lines += [f"Status: {facts.get('status')}",
                  f"Season: {facts.get('nextSeasonNumber')}",
                  f"Release date: {facts.get('releaseDate')}",
                  f"Release year: {facts.get('releaseYear')}",
                  f"Source: {facts.get('sourceUrl')}"]
    lines += [f"Independent reconstruction: {reconstruction.get('result', 'not_applicable')}",
              f"Recommended for live E2E: {'yes' if report.get('recommendedForLiveE2E') else 'no'}",
              f"Protected files unchanged: {'yes' if integrity.get('unchanged') else 'no'}",
              "", "See the JSON artifact for metadata, discovery and source diagnostics."]
    return lines


def main(raw_tmdb_id: str) -> int:
    tmdb_id = parse_tmdb_id(raw_tmdb_id)
    before = protected_hashes()
    report = {"schemaVersion": 1, "reportType": "disney_plus_candidate_read_only_preview",
              "candidate": {"tmdbId": tmdb_id}}
    error_type = None
    try:
        report = probe(tmdb_id)
    except Exception as exc:
        # Preserve a useful artifact, but fail the workflow. Never serialize
        # exception messages, which could contain server or request details.
        error_type = type(exc).__name__
        report["probeErrorType"] = error_type
    after = protected_hashes()
    unchanged = before == after
    report["protectedFileIntegrity"] = {
        "beforeSha256": before, "afterSha256": after, "unchanged": unchanged}
    output = Path(os.environ["RUNNER_TEMP"]) / f"disney-plus-candidate-{tmdb_id}-preview.json"
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    token = discovery.local_tmdb_token()
    if token and token in rendered:
        raise ValueError("Probe report contains TMDB token")
    output.write_text(rendered, encoding="utf-8")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
        summary.write("\n".join(summary_lines(report)) + "\n")
    if not unchanged or error_type or report.get("independentReconstruction", {}).get("result") == "failed":
        raise SystemExit("Disney+ candidate probe integrity failure; inspect JSON artifact")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--validate-id"]:
        try:
            print(parse_tmdb_id(os.environ.get("RAW_TMDB_ID", "")))
        except ValueError:
            raise SystemExit("Invalid TMDB ID: enter a positive decimal integer") from None
    elif len(sys.argv) == 1:
        raise SystemExit(main(os.environ.get("TMDB_ID", "")))
    else:
        raise SystemExit("Unexpected command-line arguments")
