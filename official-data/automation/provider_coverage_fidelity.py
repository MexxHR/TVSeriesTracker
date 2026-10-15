"""Audit-only comparison of frozen provider evidence with current replayed facts.

This module never fetches sources or writes data. It accepts proof summaries
from the coverage harness and fails closed when either historical or current
facts cannot be independently tied to a replayed lifecycle assertion.
"""
from __future__ import annotations

import copy
import datetime as dt
import re
from urllib.parse import unquote, urlsplit, urlunsplit

import discovery
import update

FACT_FIELDS = ("status", "nextSeasonNumber", "releaseDate", "releaseYear",
               "sourceName", "sourceUrl", "announcementDate")
_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_AMBIGUITY_FLAGS = ("ambiguous", "ambiguousTitle", "ambiguousSeason", "multipleTitles",
                    "multipleSeasons", "identityAmbiguous", "titleAmbiguous", "seasonAmbiguous")
_UNCERTAIN_FAILURES = {"SOURCE_INACCESSIBLE", "NETWORK_INCONCLUSIVE", "LOCAL_INCONCLUSIVE",
                       "CORPUS_INVALID", "UNEXPECTED_EXCEPTION"}


def _day(value):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _date_string(value):
    return isinstance(value, str) and _day(value) is not None and len(value) == 10


def _fields(fact):
    return {key: fact.get(key) for key in FACT_FIELDS} if isinstance(fact, dict) else None


def _provider_domains(provider):
    if isinstance(provider, dict):
        domains = provider.get("officialDomains")
    else:
        domains = discovery.provider_specs().get(provider, {}).get("officialDomains")
    return domains if isinstance(domains, list) else []


def _first_party(url, domains):
    return (isinstance(url, str) and url.startswith("https://") and
            update.allowed(url, domains))


def _pct_unreserved(path):
    def replace(match):
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else "%" + match.group(1).upper()
    return re.sub(r"%([0-9a-fA-F]{2})", replace, path)


def _canonical_url(url):
    """Normalize only safe URL presentation differences for audit comparison."""
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password:
            return None
        host = parts.hostname.lower()
        if parts.port not in (None, 443):
            return None
        path = _pct_unreserved(parts.path).rstrip("/") or "/"
        kept = []
        for item in parts.query.split("&") if parts.query else []:
            key = item.partition("=")[0]
            decoded_key = unquote(key).lower()
            if decoded_key.startswith("utm_") or decoded_key in _TRACKING_KEYS:
                continue
            kept.append(item)
        return urlunsplit(("https", host, path, "&".join(kept), ""))
    except (TypeError, ValueError):
        return None


def _fact_shape(fact, domains, today):
    if not isinstance(fact, dict):
        return False
    if (fact.get("status") not in {"RENEWED", "RELEASE_DATE_CONFIRMED", "FINAL_SEASON", "CANCELED"}
            or not isinstance(fact.get("nextSeasonNumber"), int)
            or isinstance(fact.get("nextSeasonNumber"), bool) or fact["nextSeasonNumber"] <= 0
            or not isinstance(fact.get("sourceName"), str) or not fact["sourceName"].strip()
            or not _first_party(fact.get("sourceUrl"), domains)):
        return False
    announcement = fact.get("announcementDate")
    if not _date_string(announcement) or _day(announcement) > today:
        return False
    release_date = fact.get("releaseDate")
    release_year = fact.get("releaseYear")
    if release_date is not None and (not _date_string(release_date) or _day(release_date) < dt.date(1900, 1, 1)):
        return False
    if release_year is not None and (not isinstance(release_year, int) or isinstance(release_year, bool)
                                     or release_year < 1900 or release_year > 9999):
        return False
    if release_date and release_year != _day(release_date).year:
        return False
    if fact["status"] == "RELEASE_DATE_CONFIRMED" and release_date is None:
        return False
    return True


def _ambiguous(value):
    return isinstance(value, dict) and any(value.get(flag) is True for flag in _AMBIGUITY_FLAGS)


def _proof_ambiguous(proof):
    if not isinstance(proof, dict) or _ambiguous(proof) or _ambiguous(proof.get("providerIdentity")):
        return True
    for key in ("sourceEvidence", "replayedFacts"):
        records = proof.get(key)
        if isinstance(records, list) and any(_ambiguous(item) for item in records):
            return True
    return False


def _identity_context_mismatch_proven(current):
    comparison = current.get("identityContextComparison")
    if not isinstance(comparison, dict) or comparison.get("different") is not True:
        return False
    legacy = comparison.get("legacy")
    production = comparison.get("productionEquivalent")
    if not isinstance(legacy, dict) or not isinstance(production, dict) or legacy == production:
        return False
    observed_legacy_failure = any(legacy.get(key) is False for key in
                                  ("identityAccepted", "genericDiscovery", "routeValid",
                                   "lifecycleParsed", "reconstructionPassed"))
    production_pass = all(production.get(key) is True for key in
                          ("identityAccepted", "genericDiscovery", "routeValid",
                           "lifecycleParsed", "reconstructionPassed"))
    return observed_legacy_failure and production_pass


def _network_uncertain(current):
    if current.get("pipelineState") == "PROVIDER_UNAVAILABLE":
        return True
    if str(current.get("primaryFailure", "")).upper() in _UNCERTAIN_FAILURES:
        return True
    metadata_failure = current.get("metadataFailure")
    if isinstance(metadata_failure, dict):
        metadata_failure = " ".join(str(value) for value in metadata_failure.values())
    if isinstance(metadata_failure, str) and "NETWORK" in metadata_failure.upper():
        return True
    secondary = current.get("secondaryFailures")
    if isinstance(secondary, list):
        for failure in secondary:
            if isinstance(failure, dict):
                failure = failure.get("code") or failure.get("failure") or failure.get("category") or ""
            if str(failure).upper() in _UNCERTAIN_FAILURES:
                return True
    return False


def _replayed_lifecycle(proof, fact, tmdb_id, provider, domains, today):
    """Require a positive lifecycle replay that exactly anchors the summary."""
    if not isinstance(proof, dict) or proof.get("passed") is not True or _proof_ambiguous(proof):
        return False
    compared = proof.get("factsCompared")
    if (proof.get("provider") != provider or proof.get("tmdbId") != tmdb_id
            or proof.get("identityAccepted") is not True or proof.get("routeValidated") is not True
            or not isinstance(compared, list) or any(not isinstance(item, str) for item in compared)
            or set(compared) != set(FACT_FIELDS)):
        return False
    persisted = proof.get("verifiedFacts")
    reconstructed = proof.get("reconstructedFacts")
    replayed = proof.get("replayedFacts")
    if not isinstance(persisted, dict) or not isinstance(reconstructed, dict) or not isinstance(replayed, list):
        return False
    if _fields(persisted) != _fields(fact) or _fields(reconstructed) != _fields(fact):
        return False
    if not _fact_shape(fact, domains, today):
        return False
    identity = proof.get("providerIdentity")
    if not isinstance(identity, dict) or identity.get("provider") != provider or identity.get("tmdbId") != tmdb_id:
        return False
    identity_title = identity.get("title")
    if not isinstance(identity_title, str) or not identity_title.strip():
        return False
    source_evidence = proof.get("sourceEvidence")
    if not isinstance(source_evidence, list) or not source_evidence:
        return False
    if not any(isinstance(record, dict) and
               record.get("sourceUrl") == fact["sourceUrl"] and
               record.get("sourceName") == fact["sourceName"] and
               record.get("announcementDate") == fact["announcementDate"] and
               _first_party(record.get("sourceUrl"), domains) for record in source_evidence):
        return False
    lifecycle_match = False
    release_match = fact.get("status") != "RELEASE_DATE_CONFIRMED"
    for item in replayed:
        if not isinstance(item, dict):
            continue
        common_identity = (item.get("tmdbId") == tmdb_id and
                           item.get("nextSeasonNumber") == fact["nextSeasonNumber"] and
                           item.get("title") == identity_title and
                           _first_party(item.get("sourceUrl"), domains))
        if item.get("factType") == "LIFECYCLE" and common_identity:
            if (item.get("status") == fact["status"] and
                    item.get("sourceUrl") == fact["sourceUrl"] and
                    item.get("sourceName") == fact["sourceName"] and
                    item.get("announcementDate") == fact["announcementDate"]):
                lifecycle_match = True
            elif (fact.get("status") == "RELEASE_DATE_CONFIRMED" and
                  item.get("status") == "RENEWED"):
                # The production merge can promote a renewal plus a separately
                # replayed premiere into RELEASE_DATE_CONFIRMED. Keep both
                # claims bound to the same series and season.
                lifecycle_match = True
        if (fact.get("status") == "RELEASE_DATE_CONFIRMED" and
                item.get("factType") == "RELEASE_DATE" and common_identity and
                item.get("releaseDate") == fact.get("releaseDate") and
                item.get("releaseYear") == fact.get("releaseYear") and
                item.get("sourceUrl") == fact["sourceUrl"] and
                item.get("sourceName") == fact["sourceName"] and
                item.get("announcementDate") == fact["announcementDate"]):
            release_match = True
    return lifecycle_match and release_match


def _valid_proof(proof, fact, case, provider, domains, today):
    return _replayed_lifecycle(proof, fact, case.get("tmdbId"), provider, domains, today)


def _same_lifecycle(left, right):
    return (left["status"] == right["status"] and
            left["nextSeasonNumber"] == right["nextSeasonNumber"] and
            left.get("releaseDate") == right.get("releaseDate") and
            left.get("releaseYear") == right.get("releaseYear"))


def _same_except_url(left, right):
    return all(left.get(key) == right.get(key) for key in FACT_FIELDS if key != "sourceUrl")


def _temporal_relation(gold, current, gold_proof, current_proof):
    if _same_lifecycle(gold, current):
        return "SAME"
    if current["nextSeasonNumber"] > gold["nextSeasonNumber"]:
        gold_date = _day(gold["announcementDate"])
        current_date = _day(current["announcementDate"])
        # Season progression is stronger than recency; still require a newer,
        # independently replayed publication to avoid retroactive claims.
        if gold_date and current_date and current_date > gold_date:
            return "SUPERSEDED"
        return "INCOMPARABLE"
    if current["nextSeasonNumber"] < gold["nextSeasonNumber"]:
        return "INCOMPARABLE"
    # Same-season status transitions require an explicit production merge
    # decision proof. The current contract does not expose one, so fail closed.
    return "INCONCLUSIVE"


def _evidence_relation(gold, current):
    exact = _fields(gold) == _fields(current)
    if exact:
        return "EXACT"
    gold_url, current_url = _canonical_url(gold["sourceUrl"]), _canonical_url(current["sourceUrl"])
    same_values = _same_lifecycle(gold, current)
    same_canonical = bool(gold_url and gold_url == current_url)
    if same_canonical:
        # A canonical article cannot acquire a new publication date through
        # URL normalization; that provenance disagreement is always a
        # conflict, even when the current fact names a later season.
        if gold["announcementDate"] != current["announcementDate"]:
            return "CONFLICTING"
        if gold.get("sourceName") != current.get("sourceName"):
            return "CONFLICTING"
        if same_values and _same_except_url(gold, current):
            return "CANONICAL_EQUIVALENT"
    if same_values:
        return "FIRST_PARTY_EQUIVALENT"
    if current["nextSeasonNumber"] > gold["nextSeasonNumber"]:
        return "DIFFERENT_VALID_SUPERSEDING"
    if current["nextSeasonNumber"] == gold["nextSeasonNumber"]:
        return "CONFLICTING"
    return "INCONCLUSIVE"


def _reason(legacy, finding, temporal, evidence, current_covered):
    codes = []
    if current_covered:
        codes.append("CURRENT_GENERIC_LIFECYCLE_REPLAY_VALID")
    if finding == "IDENTITY_CONTEXT_MISMATCH":
        codes.append("LEGACY_IDENTITY_CONTEXT_DIFFERS")
    elif finding == "HISTORICAL_GOLD_SUPERSEDED":
        codes.append("LATER_SEASON_VALID_LIFECYCLE_EVIDENCE")
    elif finding == "EQUIVALENT_EVIDENCE":
        codes.append("SAME_LIFECYCLE_FIRST_PARTY_EVIDENCE")
    elif finding == "REAL_DISCOVERY_FAILURE":
        codes.append("GENERIC_DISCOVERY_MISSING")
    elif finding == "REAL_PARSER_FAILURE":
        codes.append("GENERIC_LIFECYCLE_PARSE_MISSING")
    elif finding == "REAL_RECONSTRUCTION_FAILURE":
        codes.append("CURRENT_RECONSTRUCTION_PROOF_INVALID")
    elif finding == "INCONCLUSIVE":
        codes.append("INSUFFICIENT_OR_CONFLICTING_PROOF")
    if temporal == "SUPERSEDED":
        codes.append("TEMPORAL_SUPERSESSION_PROVEN")
    if evidence == "CONFLICTING":
        codes.append("CANONICAL_EVIDENCE_METADATA_CONFLICT")
    return codes or ["NO_FIDELITY_EXCEPTION"]


def evaluate(case, legacy, current, *, provider, identity_context_mode, today):
    """Return additive fidelity fields while preserving the historical audit row.

    ``case`` contains immutable corpus gold plus identity context. ``legacy`` is
    the existing audit result. ``current`` is a production-equivalent audit
    row carrying generic and direct-URL replay proof summaries.
    """
    output = copy.deepcopy(legacy) if isinstance(legacy, dict) else {}
    gold = copy.deepcopy(case.get("expectedFacts")) if isinstance(case, dict) else None
    fields = {
        "historicalGold": gold,
        "currentAuthoritativeFact": None,
        "historicalExactMatch": False,
        "currentCoverage": "INCONCLUSIVE",
        "temporalRelation": "INCONCLUSIVE",
        "evidenceRelation": "INCONCLUSIVE",
        "identityContextMode": identity_context_mode,
        "auditFidelityFinding": "INCONCLUSIVE",
        "reasonCodes": [],
    }
    date = _day(today)
    domains = _provider_domains(provider)
    if not isinstance(case, dict) or not isinstance(current, dict) or date is None or not domains:
        fields["reasonCodes"] = ["INVALID_EVALUATION_INPUT"]
        fields["evaluationProvenance"] = {"provider": provider, "today": str(today), "proofValidated": False}
        output.update(fields)
        return output
    if case.get("kind") != "positive" or case.get("goldReviewStatus") != "VERIFIED":
        fields["currentCoverage"] = "NOT_COVERED"
        fields["auditFidelityFinding"] = "INCONCLUSIVE"
        fields["reasonCodes"] = ["NON_POSITIVE_OR_UNREVIEWED_CASE"]
        fields["evaluationProvenance"] = {
            "provider": provider, "tmdbId": case.get("tmdbId"), "today": date.isoformat(),
            "identityContextMode": identity_context_mode, "genericDiscoveryRequired": True,
            "historicalProofValidated": False, "currentProofValidated": False,
            "currentGenericDiscovery": False, "officialDomains": list(domains),
        }
        output.update(fields)
        return output
    if _network_uncertain(current):
        fields["currentCoverage"] = "INCONCLUSIVE"
        fields["auditFidelityFinding"] = "INCONCLUSIVE"
        fields["reasonCodes"] = ["NETWORK_OR_ENVIRONMENT_UNCERTAIN"]
        fields["evaluationProvenance"] = {
            "provider": provider, "tmdbId": case.get("tmdbId"), "today": date.isoformat(),
            "identityContextMode": identity_context_mode, "genericDiscoveryRequired": True,
            "historicalProofValidated": False, "currentProofValidated": False,
            "currentGenericDiscovery": current.get("genericDiscovery") is True or
                                       current.get("discoveredGenerically") is True,
            "officialDomains": list(domains),
        }
        output.update(fields)
        return output

    known = case.get("expectedFactKnownFields", list(FACT_FIELDS))
    direct = current.get("directUrlDiagnostic")
    historical_fact = copy.deepcopy(direct.get("actualFacts")) if isinstance(direct, dict) else None
    historical_proof = direct.get("reconstructionProof") if isinstance(direct, dict) else None
    current_fact = copy.deepcopy(current.get("actualFacts"))
    current_proof = current.get("reconstructionProof")
    historical_valid = (isinstance(gold, dict) and isinstance(known, list) and
                        all(isinstance(item, str) for item in known) and set(known) == set(FACT_FIELDS) and
                        _fact_shape(gold, domains, date) and
                        _valid_proof(historical_proof, historical_fact, case, provider, domains, date) and
                        _fields(historical_fact) == _fields(gold))
    current_valid = (_valid_proof(current_proof, current_fact, case, provider, domains, date) and
                     isinstance(current.get("reconstructedFacts"), dict) and
                     _fields(current.get("reconstructedFacts")) == _fields(current_fact))
    regional_date_violation = (case.get("releaseDateScope") == "regional" and
                               isinstance(current_fact, dict) and
                               (current_fact.get("releaseDate") is not None or
                                current_fact.get("releaseYear") is not None))
    if regional_date_violation:
        current_valid = False
    generic = current.get("genericDiscovery") is True or current.get("discoveredGenerically") is True
    current_pipeline_pass = bool(generic and current.get("routeValid") is True and
                                 current.get("identityAccepted") is True and
                                 current.get("lifecycleParsed") is True and
                                 current.get("reconstructionPassed") is True and current_valid)
    current_exact_gold = bool(current_pipeline_pass and isinstance(gold, dict) and
                              _fact_shape(gold, domains, date) and _fields(current_fact) == _fields(gold))
    temporal = evidence = "INCONCLUSIVE"
    finding = "INCONCLUSIVE"
    current_covered = False
    if historical_valid and current_pipeline_pass:
        temporal = _temporal_relation(gold, current_fact, historical_proof, current_proof)
        evidence = _evidence_relation(gold, current_fact)
        current_covered = (evidence in ("EXACT", "CANONICAL_EQUIVALENT", "FIRST_PARTY_EQUIVALENT") or
                           (temporal == "SUPERSEDED" and evidence == "DIFFERENT_VALID_SUPERSEDING"))
        if evidence == "CONFLICTING" or temporal == "INCOMPARABLE":
            fields["currentCoverage"] = "NOT_COVERED"
        elif current_covered:
            fields["currentCoverage"] = "COVERED"
        else:
            fields["currentCoverage"] = "INCONCLUSIVE"
    elif current_exact_gold:
        # A valid generic replay that exactly reproduces every curated field
        # can establish current coverage even when the historical direct-URL
        # proof is unavailable.
        temporal, evidence = "SAME", "EXACT"
        current_covered = True
        fields["currentCoverage"] = "COVERED"
    elif not generic or current.get("primaryFailure") in ("DISCOVERY_MISSED", "GENERIC_DISCOVERY_MISSED"):
        fields["currentCoverage"] = "NOT_COVERED"
    elif generic and current.get("lifecycleParsed") is False:
        fields["currentCoverage"] = "NOT_COVERED"
    else:
        fields["currentCoverage"] = "INCONCLUSIVE"

    # This metric measures equality with the frozen seven-field gold, not
    # whether the separate historical direct-URL diagnostic was reachable.
    fields["historicalExactMatch"] = current_exact_gold
    if current_covered:
        fields["currentAuthoritativeFact"] = copy.deepcopy(current_fact)

    if not generic and current.get("primaryFailure") in ("DISCOVERY_MISSED", "GENERIC_DISCOVERY_MISSED"):
        finding = "REAL_DISCOVERY_FAILURE"
    elif generic and current.get("lifecycleParsed") is not True:
        finding = "REAL_PARSER_FAILURE"
    elif generic and not current_valid:
        finding = "REAL_RECONSTRUCTION_FAILURE"
    elif current_covered and historical_valid:
        if temporal == "SUPERSEDED" and evidence == "DIFFERENT_VALID_SUPERSEDING":
            finding = "HISTORICAL_GOLD_SUPERSEDED"
        elif evidence in ("CANONICAL_EQUIVALENT", "FIRST_PARTY_EQUIVALENT"):
            finding = "EQUIVALENT_EVIDENCE"
        elif fields["historicalExactMatch"]:
            finding = "NONE"
        if _identity_context_mismatch_proven(current):
            finding = "IDENTITY_CONTEXT_MISMATCH"
    elif current_covered and not historical_valid:
        finding = "NONE" if current_exact_gold else "INCONCLUSIVE"
        if _identity_context_mismatch_proven(current):
            finding = "IDENTITY_CONTEXT_MISMATCH"

    fields["temporalRelation"] = temporal
    fields["evidenceRelation"] = evidence
    fields["auditFidelityFinding"] = finding
    fields["reasonCodes"] = _reason(legacy, finding, temporal, evidence, current_covered)
    fields["evaluationProvenance"] = {
        "provider": provider,
        "tmdbId": case.get("tmdbId"),
        "today": date.isoformat(),
        "identityContextMode": identity_context_mode,
        "identityContextComparison": copy.deepcopy(current.get("identityContextComparison")),
        "genericDiscoveryRequired": True,
        "historicalProofValidated": bool(historical_valid),
        "currentProofValidated": bool(current_valid),
        "currentGenericDiscovery": bool(generic),
        "officialDomains": list(domains),
    }
    output.update(fields)
    return output
