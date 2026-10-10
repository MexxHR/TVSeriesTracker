"""Read-only FX/Hulu qualification report for the V2.7.3 GitHub runner.

This wraps the existing bounded provider-audit surface and independently
reconstructs any positive parsed facts with the promotion replay helpers. It
never invokes monitored.process or any writer. Unknown registry TMDB identities
remain null in the report; a clearly marked temporary integer is used only by
the promotion validator because that API requires an integer identity.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import monitored
import promotion
import provider_audit
import update


PROTECTED_FILES = (
    "official-data/official_series_data.json",
    "official-data/history/changes.jsonl",
    "official-data/sources.json",
    "official-data/monitored_series.json",
    "official-data/history/monitored_changes.jsonl",
)
MIN_DISTINCT_PROVIDER_ANNOUNCEMENTS = 3
HISTORICAL_CASE_BASELINE = {
    "fx-lowdown-s2": (True, "complete"),
    "fx-mayans-final-s5": (False, "parser"),
    "fx-snowfall-final-s6": (True, "complete"),
    "hulu-deli-boys-s2": (True, "complete"),
    "hulu-difficult-people-s2": (False, "parser"),
    "hulu-handmaids-tale-s3": (False, "discovery"),
    "hulu-shrill-final-s3": (True, "complete"),
    "hulu-reasonable-doubt-cast": (True, "complete"),
}


def _hash_protected_files(root: Path = update.ROOT) -> dict[str, str]:
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in PROTECTED_FILES}


def protected_integrity(before: dict[str, str], after: dict[str, str]) -> dict:
    changed = [name for name in PROTECTED_FILES if before.get(name) != after.get(name)]
    return {"algorithm": "sha256", "before": before, "after": after,
            "unchanged": not changed, "changedFiles": changed}


def _status(row: dict) -> str:
    result = row.get("recommendation")
    if result == "QUALIFIED_ON_GITHUB_RUNNER":
        return result
    if result == "BLOCKED_FROM_GITHUB_RUNNER":
        return "PROVIDER_UNAVAILABLE"
    if result in ("PARSER_COMPATIBILITY_NEEDS_WORK", "AMBIGUITY_SAFETY_NEEDS_WORK"):
        return result
    if result == "FETCHABLE_BUT_DISCOVERY_NEEDS_WORK":
        return "DISCOVERY_NEEDS_WORK"
    return "NEEDS_MORE_RESEARCH"


def _fact_reconstruction(case: dict, case_index: int, domains: list[str], today: dt.date,
                         fetcher=provider_audit.fetch_url) -> dict:
    """Replay strict parser evidence through promotion's exact field validator."""
    url = case.get("discoveredFinalUrl") or case.get("discoveredUrl")
    if not url:
        return {"result": "not_applicable", "reason": "no_discovered_article"}
    fetch_result, raw = fetcher(url, domains)
    final_url = fetch_result.get("finalUrl") if isinstance(fetch_result, dict) else None
    if not raw or not final_url or fetch_result.get("status") != "fetched" or not update.allowed(final_url, domains):
        return {"result": "failed", "reason": "independent_refetch_failed",
                "fetchStatus": fetch_result.get("status") if isinstance(fetch_result, dict) else None,
                "failureCategory": fetch_result.get("failureCategory") if isinstance(fetch_result, dict) else None}

    try:
        page = update.parse_page(raw)
        text = provider_audit._QualificationText()
        text.feed(raw)
        body = text.text()[:25_000]
        publication = provider_audit.publication_date(raw)
        article = {"title": page.heading.strip() or page.title.strip(), "body": body,
                   "url": final_url, "sourceName": "Provider discovery qualification",
                   "publicationDate": publication}
        aliases = case.get("aliases") or [case["series"]]
        # promotion._validate_evidence requires a positive integer. This is
        # an isolated parser key; it is never reported as TMDB identity or persisted.
        synthetic_id = 900_000_000 + case_index
        entry = {"tmdbId": synthetic_id, "title": case["series"], "aliases": aliases,
                 "allowedDomains": domains}
        facts = update.detect(article, entry, today, strict_binding=True)
        if not facts:
            return {"result": "not_applicable", "reason": "strict_parser_returned_no_facts",
                    "tmdbId": None, "internalSyntheticKeyUsed": True}
        update.validate_facts(facts, entry)
        evidence = [{**{key: fact.get(key) for key in monitored.EVIDENCE_FIELDS},
                     "publicationDateSource": "structured_metadata" if fact.get("announcementDate") else "none"}
                    for fact in facts]
        row = {"tmdbId": synthetic_id, "title": case["series"], "provider": "qualification-only",
               "parserResult": "facts", "validationResult": "passed",
               "lastChecked": today.isoformat(), "verifiedFacts": {}, "sourceEvidence": evidence,
               "eligibleSourceUrls": sorted({fact["sourceUrl"] for fact in facts})}
        # Construct the parser's summary and require promotion's separate
        # evidence replay to match the same complete set of promotion fields.
        merged, _ = update.merge(None, facts, today)
        if merged is None:
            raise update.AutomationError("Facts could not be independently merged")
        if merged["status"] == "RENEWED" and merged.get("releaseDate"):
            merged["status"] = "RELEASE_DATE_CONFIRMED"
            date_fact = next((fact for fact in facts if fact["nextSeasonNumber"] == merged["nextSeasonNumber"] and
                              fact.get("releaseDate") == merged["releaseDate"] and fact.get("rule") == "explicit-premiere"), None)
            if date_fact:
                for key in ("sourceName", "sourceUrl", "announcementDate"):
                    merged[key] = date_fact[key]
        row["verifiedFacts"] = {key: merged.get(key) for key in monitored.FACT_FIELDS}
        rebuilt_facts = promotion._validate_evidence(row, domains)
        rebuilt = promotion._facts_summary(rebuilt_facts, row, today)
        fields = list(monitored.FACT_FIELDS)
        actual = {key: row["verifiedFacts"].get(key) for key in fields}
        reconstructed = {key: rebuilt.get(key) for key in fields}
        mismatches = [key for key in fields if actual[key] != reconstructed[key]]
        return {"result": "passed" if not mismatches else "mismatch", "tmdbId": None,
                "internalSyntheticKeyUsed": True, "fieldsCompared": fields,
                "mismatchedFields": mismatches, "verifiedFacts": actual,
                "reconstructedFacts": reconstructed,
                "sourceEvidence": [{key: item.get(key) for key in
                                    ("factType", "status", "nextSeasonNumber", "rule", "sourceName",
                                     "sourceUrl", "announcementDate", "evidenceText")}
                                   for item in evidence]}
    except (update.AutomationError, ValueError, KeyError, TypeError) as exc:
        return {"result": "failed", "reason": type(exc).__name__, "tmdbId": None,
                "internalSyntheticKeyUsed": True, "fieldsCompared": list(monitored.FACT_FIELDS),
                "validationResult": "failed"}


def _case_result(provider: str, provider_row: dict, case_row: dict, case_config: dict,
                 case_index: int, today: dt.date, *, github_runner: bool,
                 fetcher=provider_audit.fetch_url) -> dict:
    discovered = bool(case_row.get("discoveredFromRoot"))
    known = case_row.get("knownArticleDiagnostic") or {}
    content = case_row.get("content") or {}
    expected_ok = bool(case_row.get("passed"))
    kind = case_row.get("kind", "positive")
    domains = provider_row.get("officialDomains", [])
    reconstruction = (_fact_reconstruction(case_row, case_index, domains, today, fetcher)
                      if content.get("parserFactCount", 0) else
                      {"result": "not_applicable", "reason": "no_qualifying_parsed_facts"})
    validation = ("passed" if reconstruction["result"] == "passed" or
                  (expected_ok and kind == "negative" and not content.get("parserFactCount", 0)) else
                  "failed" if reconstruction["result"] in ("failed", "mismatch") else "not_applicable")
    case_passed = (expected_ok and validation == "passed" and
                   (not content.get("parserFactCount", 0) or reconstruction["result"] == "passed"))
    http_rows = case_row.get("http", [])
    attempted = (list(case_row.get("discoveryRoots", [])) +
                 [item.get("url") for item in http_rows if item.get("url")] +
                 ([case_row.get("articleUrl")] if case_row.get("articleUrl") else []))
    rejected = [{"url": item.get("url"), "reason": item.get("failureCategory")}
                for item in http_rows if item.get("failureCategory")]
    current_stage = ("complete" if case_passed else "discovery" if not discovered else
                     "fetch" if not known.get("fetchable") and case_row.get("failureCategory") else
                     "identity" if case_row.get("failureCategory") == "ARTICLE_IDENTITY_MISMATCH" else
                     "complete" if kind == "negative" and expected_ok and not content.get("parserFactCount", 0) else
                     "parser" if not case_row.get("parserCompatible") else
                     "expectation" if not expected_ok else
                     "independent_reconstruction" if reconstruction["result"] in ("failed", "mismatch") else "complete")
    if kind in ("negative", "ambiguity") and case_passed:
        classification = "NEGATIVE_SAFETY_CASE_PASSED"
    elif expected_ok and reconstruction["result"] == "mismatch":
        classification = "INDEPENDENT_RECONSTRUCTION_MISMATCH"
    elif expected_ok and reconstruction["result"] == "failed":
        classification = "INDEPENDENT_RECONSTRUCTION_FAILED"
    elif expected_ok and reconstruction["result"] in ("passed", "not_applicable"):
        classification = "OFFICIAL_EVIDENCE_CASE_PASSED"
    else:
        classification = case_row.get("failureCategory") or "QUALIFICATION_CASE_FAILED"
    return {
        "caseId": case_row.get("caseId"), "tmdbId": case_config.get("tmdbId"),
        "title": case_row.get("series"), "provider": provider,
        "passed": case_passed, "kind": kind, "articleUrl": case_row.get("articleUrl"),
        "rootScope": case_row.get("rootScope", "provider"),
        "qualificationOnly": True, "routeSignals": [], "productionRouteResult": "not_assessed",
        "routingEvidence": "provider_audit_registry_case; no TMDB identity asserted",
        "officialUrlsAttempted": sorted(set(attempted)),
        "discovery": {"result": "candidate_found" if discovered else "no_relevant_official_url",
                      "discoveredFromRoot": discovered, "qualificationCandidateCount": 1 if discovered else 0,
                      "candidateUrls": [case_row.get("discoveredUrl")] if discovered else [],
                      "rejectedCandidateUrls": rejected, "provenance": case_row.get("discoveryProvenance"),
                      "productionParserEligibility": "not_assessed"},
        "fetch": {"discoveryFetches": http_rows, "knownArticleDiagnostic": known},
        "qualificationTitleMarkerResult": ("rejected" if case_row.get("failureCategory") == "ARTICLE_IDENTITY_MISMATCH" else
                                            "passed" if expected_ok else "not_confirmed"),
        "productionIdentityResult": "not_assessed",
        "configuredSourceStructure": case_row.get("sourceStructure", "announcement"),
        "productionStructureQualification": "not_assessed",
        "parsedSourceCount": 1 if discovered and content.get("extractable") else 0,
        "productionParsedSourceCount": "not_applicable",
        "parserResult": {"factCount": content.get("parserFactCount", 0), "facts": content.get("facts", []),
                         "signalsDiagnosticOnly": content.get("signals", [])},
        "verifiedFacts": reconstruction.get("verifiedFacts") if reconstruction.get("result") == "passed" else None,
        "sourceEvidence": reconstruction.get("sourceEvidence", []),
        "productionMonitoredState": "not_created",
        "validationResult": validation,
        "independentReconstruction": reconstruction,
        "expectationResult": {"passed": expected_ok, "failureCategory": case_row.get("failureCategory")},
        "baselineResult": {"source": "prior accepted V2.7.1.1 qualification state supplied for this task",
                           "recorded": case_row.get("caseId") in HISTORICAL_CASE_BASELINE,
                           "passed": HISTORICAL_CASE_BASELINE.get(case_row.get("caseId"), (None, None))[0],
                           "earliestFailureStage": HISTORICAL_CASE_BASELINE.get(case_row.get("caseId"), (None, None))[1]},
        "earliestFailureStage": (None if case_passed else
                                  "independent_reconstruction" if expected_ok and content.get("parserFactCount", 0) and
                                  reconstruction["result"] != "passed" else
                                  None if current_stage == "complete" else current_stage),
        "classification": classification,
    }


def run(*, fetcher=provider_audit.fetch_url, github_runner: bool | None = None,
        today: dt.date | None = None, root: Path = update.ROOT) -> dict:
    """Run FX/Hulu qualification and prove protected bytes stayed unchanged."""
    before = _hash_protected_files(root)
    today = today or dt.datetime.now(dt.timezone.utc).date()
    registry = provider_audit.load_registry()
    registry["providers"] = [row for row in registry["providers"] if row.get("provider") in {"FX", "HULU"}]
    if {row.get("provider") for row in registry["providers"]} != {"FX", "HULU"}:
        raise ValueError("Qualification registry must contain exactly FX and HULU")
    is_github = provider_audit._is_github_actions(github_runner)
    report = provider_audit.qualify(registry, fetcher=fetcher, github_actions=is_github, today=today)
    source_by_provider = {row["provider"]: row for row in registry["providers"]}
    providers = {}
    ordinal = 1
    for row in report["providers"]:
        original = source_by_provider[row["provider"]]
        configs = {item.get("caseId"): item for item in original.get("tests", [])}
        cases = [_case_result(row["provider"], row, case, configs.get(case.get("caseId"), {}), ordinal,
                              today, github_runner=is_github, fetcher=fetcher)
                 for case in row.get("tests", [])]
        ordinal += len(cases)
        configured_announcements = {case.get("articleUrl") for case in original.get("tests", [])
                                    if case.get("kind", "positive") == "positive" and
                                    case.get("rootScope", "provider") == "provider" and
                                    case.get("sourceStructure", "announcement") == "announcement"}
        positive_announcements = {case.get("articleUrl")
                                  for case in cases if case["passed"] and case.get("kind") == "positive" and
                                  case.get("rootScope", "provider") == "provider" and
                                  case.get("configuredSourceStructure", "announcement") == "announcement"}
        passed = sum(case["passed"] for case in cases)
        threshold_possible = len(configured_announcements) >= MIN_DISTINCT_PROVIDER_ANNOUNCEMENTS
        ready = bool(is_github and row.get("qualificationReady") and threshold_possible and passed == len(cases))
        state = "QUALIFIED_ON_GITHUB_RUNNER" if ready else _status(row)
        if not ready and row.get("recommendation") == "QUALIFIED_ON_GITHUB_RUNNER":
            state = "NEEDS_MORE_RESEARCH"
        providers[row["provider"]] = {
            "qualificationState": state, "passedCases": passed, "totalCases": len(cases),
            "baseline": {"qualificationState": "PARSER_COMPATIBILITY_NEEDS_WORK",
                         "passedCases": 2 if row["provider"] == "FX" else 3,
                         "totalCases": 3 if row["provider"] == "FX" else 5,
                         "source": "prior qualification results supplied in the task; not rerun in this workflow"},
            "minimumDistinctProviderAnnouncements": MIN_DISTINCT_PROVIDER_ANNOUNCEMENTS,
            "configuredProviderRootAnnouncements": len(configured_announcements),
            "distinctPassingProviderAnnouncements": len(positive_announcements),
            "thresholdPossibleFromConfiguredCaseSet": threshold_possible,
            "cases": cases,
        }
    after = _hash_protected_files(root)
    integrity = protected_integrity(before, after)
    result = {"schemaVersion": 1, "reportType": "fx_hulu_provider_qualification",
              "generatedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
              "readOnly": True, "qualificationOnly": True,
              "productionActivationReady": {"FX": False, "HULU": False},
              "identityLimitations": "The audit registry has no TMDB IDs; tmdbId is null and no production route or monitored VERIFIED_FACTS state is asserted.",
              "runner": {"githubActions": is_github}, "providers": providers,
              "summary": {"fxReadyForProductionActivation": False,
                          "huluReadyForProductionActivation": False,
                          "activationDecision": "requires a separate later decision after reviewing real GitHub artifact",
                          "remainingGaps": _remaining_gaps(providers)},
              "protectedFileIntegrity": integrity}
    if not integrity["unchanged"]:
        raise RuntimeError("Protected Official Data bytes changed during qualification")
    return result


def _remaining_gaps(providers: dict) -> list[str]:
    gaps = []
    for name, item in providers.items():
        if item["qualificationState"] != "QUALIFIED_ON_GITHUB_RUNNER":
            gaps.append(f"{name}: {item['qualificationState']} ({item['passedCases']}/{item['totalCases']}); production activation is not eligible.")
        if not item["thresholdPossibleFromConfiguredCaseSet"]:
            gaps.append(f"{name}: configured cases contain only {item['distinctPassingProviderAnnouncements']} qualifying provider-root announcements; policy requires {item['minimumDistinctProviderAnnouncements']}.")
        for case in item["cases"]:
            if case["earliestFailureStage"]:
                gaps.append(f"{name}/{case['caseId']}: earliest failing stage is {case['earliestFailureStage']} ({case['classification']}).")
    return gaps


def render_summary(report: dict) -> str:
    lines = ["### FX + Hulu Provider Qualification", ""]
    for name in ("FX", "HULU"):
        provider = report["providers"][name]
        lines += [f"#### {name}", "", "| Case | Discovery | Parser | Validation | Independent reconstruction | Result |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for case in provider["cases"]:
            lines.append(f"| {case['caseId']} | {case['discovery']['result']} | {case['parserResult']['factCount']} facts | "
                         f"{case['validationResult']} | {case['independentReconstruction']['result']} | {case['classification']} |")
        lines += ["", f"**Qualification: {provider['passedCases']}/{provider['totalCases']} — {provider['qualificationState']}**", ""]
    lines += ["#### Remaining gaps", ""]
    lines.extend(f"- {item}" for item in report["summary"]["remainingGaps"] or ["None observed; production activation still requires a separate decision."])
    lines += ["", "FX and Hulu are not activated by this workflow. A local fixture result cannot establish activation readiness."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output).resolve()
    try:
        output.relative_to(update.ROOT.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("Qualification artifact output must be outside the repository")
    report = run()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(render_summary(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
