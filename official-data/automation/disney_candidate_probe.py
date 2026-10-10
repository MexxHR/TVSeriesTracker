"""Read-only diagnostics for one Disney+ candidate on the GitHub runner.

All discovery, parsing, validation, and evidence replay are delegated to the
existing production modules. This module only records their outputs.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import discovery
import monitored
import promotion
import update


TMDB_ID = 138503
REQUESTED_TITLE = "Your Friendly Neighborhood Spider-Man"
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


def protected_hashes(root: Path = ROOT) -> dict[str, str]:
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in PROTECTED}


def probe(metadata: dict | None = None, *, processor=monitored.process,
          discoverer=discovery.discover) -> dict:
    # The normal monitored path uses these exact metadata and routing helpers.
    meta = metadata if metadata is not None else discovery.tmdb_metadata(
        TMDB_ID, discovery.local_tmdb_token())
    if meta.get("tmdbId") != TMDB_ID:
        raise ValueError("TMDB metadata identity mismatch")
    provider, signals, reason = discovery.route_provider(meta, discovery.provider_specs())
    report = {
        "schemaVersion": 1,
        "reportType": "disney_plus_candidate_read_only_preview",
        "candidate": {"tmdbId": TMDB_ID, "requestedTitle": REQUESTED_TITLE},
        "metadata": meta,
        "routing": {"provider": provider, "routeSignals": signals, "reason": reason},
        "discoveryDiagnostics": None,
        "monitoredPreview": None,
        "proposedMonitoredRecord": None,
        "independentReconstruction": {"result": "not_applicable"},
        "recommendedForLiveE2E": False,
    }
    if provider != "DISNEY_PLUS":
        report["routing"]["factualProcessingStopped"] = True
        return report

    discovery_result = {}

    def capture_discovery(actual_meta, trusted):
        result = discoverer(actual_meta, trusted)
        discovery_result.update(result)
        return result

    result = processor(TMDB_ID, dry_run=True, metadata=meta,
                       discoverer=capture_discovery)
    if result.get("tmdbId") != TMDB_ID or result.get("dryRun") is not True or not isinstance(result.get("pipelineState"), str):
        raise ValueError("Invalid monitored dry-run result")
    report["discoveryDiagnostics"] = discovery_result
    report["monitoredPreview"] = result
    if result.get("provider") not in (None, provider):
        raise ValueError("Monitored provider differs from TMDB routing")

    row = result.get("proposedRecord")
    if row is None and result.get("registryChange") == "none":
        registry, _ = monitored.load_registry(monitored.MONITORED)
        row = next((item for item in registry["series"]
                    if item["tmdbId"] == TMDB_ID), None)
    report["proposedMonitoredRecord"] = row
    if result.get("pipelineState") == "VERIFIED_FACTS" and result.get("validationResult") == "passed":
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
    lines = ["### Disney+ candidate 138503 read-only probe", "",
             f"TMDB title: {meta.get('title')}",
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
              f"Protected files unchanged: {'yes' if integrity.get('unchanged') else 'no'}",
              "", "See the JSON artifact for metadata, discovery and source diagnostics."]
    return lines


def main() -> int:
    before = protected_hashes()
    report = {"schemaVersion": 1, "reportType": "disney_plus_candidate_read_only_preview",
              "candidate": {"tmdbId": TMDB_ID, "requestedTitle": REQUESTED_TITLE}}
    error_type = None
    try:
        report = probe()
    except Exception as exc:
        # Preserve a useful artifact, but fail the workflow. Never serialize
        # exception messages, which could contain server or request details.
        error_type = type(exc).__name__
        report["probeErrorType"] = error_type
    after = protected_hashes()
    unchanged = before == after
    report["protectedFileIntegrity"] = {
        "beforeSha256": before, "afterSha256": after, "unchanged": unchanged}
    output = Path(os.environ["RUNNER_TEMP"]) / "disney-plus-candidate-preview.json"
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
    raise SystemExit(main())
