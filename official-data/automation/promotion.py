"""Fail-closed promotion gate from monitored evidence to production Official Data."""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

import discovery
import monitored
import update


def _answer(tmdb_id: int, state: str, *, title=None, monitored_state=None,
            reason: str, dry_run: bool, promotion_eligible: bool = False,
            old=None, proposed=None, changed_fields=None, source_urls=None,
            validation_result="not_applicable", fatal=False) -> dict:
    return {"tmdbId": tmdb_id, "title": title, "monitoredState": monitored_state,
            "promotionEligible": promotion_eligible, "promotionState": state,
            "promotionReason": reason, "oldProductionRecord": old,
            "proposedProductionRecord": proposed, "changedFields": changed_fields or [],
            "sourceUrls": source_urls or [], "validationResult": validation_result,
            "dryRun": dry_run, "wouldPublish": bool(proposed is not None and changed_fields),
            "fatal": fatal}


def _validate_evidence(row: dict, domains: list[str]) -> list[dict]:
    """Re-run the strict parser over persisted fact excerpts and check provenance."""
    if (row.get("parserResult") != "facts" or row.get("validationResult") != "passed" or
            not isinstance(row.get("eligibleSourceUrls"), list) or not row["eligibleSourceUrls"]):
        raise update.AutomationError("Persisted parser eligibility or validation proof is missing")
    eligible = set(row["eligibleSourceUrls"])
    evidence = row.get("sourceEvidence")
    if not isinstance(evidence, list) or not evidence:
        raise update.AutomationError("Verified monitored record has no source evidence")
    entry = {"tmdbId": row["tmdbId"], "title": row["title"], "aliases": [row["title"]],
             "allowedDomains": domains}
    facts = []
    for item in evidence:
        if not isinstance(item, dict):
            raise update.AutomationError("Malformed source evidence")
        url = item.get("sourceUrl")
        text = item.get("evidenceText")
        if (not isinstance(url, str) or url not in eligible or not update.allowed(url, domains) or
                not isinstance(text, str) or not text.strip() or len(text) > 200):
            raise update.AutomationError("Evidence lacks eligible official source provenance")
        if item.get("announcementDate") and item.get("publicationDateSource") != "structured_metadata":
            raise update.AutomationError("announcementDate lacks structured publication metadata provenance")
        if item.get("publicationDateSource") not in (None, "none", "structured_metadata"):
            raise update.AutomationError("Invalid publication-date provenance")
        if item.get("factType") not in ("LIFECYCLE", "RELEASE_DATE", "RELEASE_YEAR"):
            raise update.AutomationError("Evidence lacks a recognized fact type")
        article = {"title": "", "body": text, "url": url,
                   "sourceName": item.get("sourceName"),
                   "publicationDate": item.get("announcementDate")}
        parsed = update.detect(article, entry, dt.date.fromisoformat(row["lastChecked"]),
                               extended_final=True, strict_binding=True)
        match = next((fact for fact in parsed if fact.get("factType") == item.get("factType") and
                      fact.get("rule") == item.get("rule") and
                      fact.get("nextSeasonNumber") == item.get("nextSeasonNumber") and
                      fact.get("status") == item.get("status") and
                      fact.get("releaseDate") == item.get("releaseDate") and
                      fact.get("releaseYear") == item.get("releaseYear")), None)
        if match is None:
            raise update.AutomationError("Persisted fact does not match strict parser evidence")
        facts.append(match)
    update.validate_facts(facts, entry)
    for season in {fact["nextSeasonNumber"] for fact in facts}:
        lifecycle_states = {fact["status"] for fact in facts
                            if fact["nextSeasonNumber"] == season and
                            fact.get("rule") not in ("explicit-premiere", "explicit-release-year")}
        if "CANCELED" in lifecycle_states and len(lifecycle_states) > 1:
            raise update.AutomationError(f"Conflicting lifecycle evidence for {row['title']} season {season}")
    return facts


def _facts_summary(facts: list[dict], row: dict, today: dt.date) -> dict:
    # Same-season multi-source enrichment follows the existing production
    # merge. Keep the Phase 2A status conversion for a renewed season with a
    # separately verified premiere date.
    candidate, _ = update.merge(None, facts, today)
    if candidate is None:
        raise update.AutomationError("Evidence could not be merged")
    if candidate["status"] == "RENEWED" and candidate.get("releaseDate"):
        candidate["status"] = "RELEASE_DATE_CONFIRMED"
        date_fact = next((fact for fact in facts if fact["nextSeasonNumber"] == candidate["nextSeasonNumber"] and
                          fact.get("releaseDate") == candidate["releaseDate"] and
                          fact.get("rule") == "explicit-premiere"), None)
        if date_fact:
            for key in ("sourceName", "sourceUrl", "announcementDate"):
                candidate[key] = date_fact[key]
    fields = monitored.FACT_FIELDS
    expected = {key: candidate.get(key) for key in fields}
    actual = row.get("verifiedFacts")
    if not isinstance(actual, dict) or any(actual.get(key) != expected.get(key) for key in fields):
        raise update.AutomationError("verifiedFacts do not match the independently rebuilt evidence")
    return candidate


def promote(tmdb_id: int, dry_run: bool = False, *, now: dt.datetime | None = None,
            production_data: Path = update.DATA, production_audit: Path = update.AUDIT,
            production_registry: Path = update.REGISTRY,
            monitored_registry: Path = monitored.MONITORED) -> dict:
    """Promote one monitored record after independent validation.

    The caller controls whether this is a dry-run. All decisions, merges,
    diffs and validation happen before any production write.
    """
    if not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool) or tmdb_id <= 0:
        raise ValueError("TMDB ID must be positive")
    now = now or dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    today = now.date()
    try:
        trusted = json.loads(production_registry.read_text(encoding="utf-8"))
        update.validate_registry(trusted)
        raw = production_data.read_bytes()
        original = json.loads(raw)
        update.validate_dataset(original, trusted, original)
        audit_exists = production_audit.exists()
        old_audit = update.audit_bytes(production_audit)
        staging, _ = monitored.load_registry(monitored_registry)
        monitored.validate_registry(staging)
    except (OSError, UnicodeError, ValueError, update.AutomationError) as exc:
        return _answer(tmdb_id, "VALIDATION_FAILED", reason=f"Production or monitored input invalid: {exc}",
                       dry_run=dry_run, validation_result="failed", fatal=True)

    row = next((item for item in staging["series"] if item["tmdbId"] == tmdb_id), None)
    if row is None:
        return _answer(tmdb_id, "NOT_FOUND", reason="No monitored record for this TMDB ID",
                       dry_run=dry_run)
    title = row.get("title")
    state = row.get("discoveryState")
    if any(item["tmdbId"] == tmdb_id for item in trusted["series"]):
        return _answer(tmdb_id, "TRUSTED_SOURCE_PRECEDENCE", title=title, monitored_state=state,
                       reason="The ID is already covered by the trusted production source registry",
                       dry_run=dry_run)
    if state != "VERIFIED_FACTS":
        return _answer(tmdb_id, "NOT_VERIFIED", title=title, monitored_state=state,
                       reason="Only VERIFIED_FACTS may enter the production gate",
                       dry_run=dry_run)

    try:
        if not isinstance(title, str) or not title.strip() or title != " ".join(title.split()):
            raise update.AutomationError("Title must be non-empty normalized text")
        specs = discovery.provider_specs()
        provider = row.get("provider")
        if provider not in specs:
            raise update.AutomationError("Unsupported or missing official provider")
        domains = specs[provider]["officialDomains"]
        facts = _validate_evidence(row, domains)
        candidate = _facts_summary(facts, row, today)
        old_map = {item["tmdbId"]: item for item in original["series"]}
        old = old_map.get(tmdb_id)
        merged, merge_evidence = update.merge(old, facts, today)
        if merged is None:
            return _answer(tmdb_id, "NO_CHANGE", title=title, monitored_state=state,
                           reason="Production merge produced no factual change", dry_run=dry_run,
                           promotion_eligible=True, old=old, proposed=old,
                           validation_result="passed")
        if old and not merge_evidence:
            return _answer(tmdb_id, "NO_CHANGE", title=title, monitored_state=state,
                           reason="The verified facts are already represented in production",
                           dry_run=dry_run, promotion_eligible=True, old=old, proposed=old,
                           validation_result="passed")
        if merged["tmdbId"] != tmdb_id or merged["title"] != title:
            raise update.AutomationError("Promotion merge changed series identity")
        if old and merged["nextSeasonNumber"] == old["nextSeasonNumber"] and old["status"] == "RELEASE_DATE_CONFIRMED" and merged["status"] != "RELEASE_DATE_CONFIRMED":
            raise update.AutomationError("Same-season merge downgraded confirmed premiere status")
        candidate_by_id = {item["tmdbId"]: item for item in copy.deepcopy(original["series"])}
        candidate_by_id[tmdb_id] = merged
        changed_ids = [identifier for identifier in set(old_map) | set(candidate_by_id)
                       if old_map.get(identifier) != candidate_by_id.get(identifier)]
        if changed_ids != [tmdb_id] and set(changed_ids) != {tmdb_id}:
            raise update.AutomationError("Promotion changed an unrelated production record")
        if len(changed_ids) > 1:
            raise update.AutomationError("Promotion changed more than the requested TMDB ID")
        if not changed_ids:
            return _answer(tmdb_id, "NO_CHANGE", title=title, monitored_state=state,
                           reason="The verified facts are already represented in production",
                           dry_run=dry_run, promotion_eligible=True, old=old, proposed=old,
                           validation_result="passed")
        proposed = copy.deepcopy(original)
        proposed["series"] = sorted(candidate_by_id.values(), key=lambda item: item["tmdbId"])
        previous_generated = dt.datetime.fromisoformat(original["generatedAt"].replace("Z", "+00:00"))
        if now <= previous_generated:
            raise update.AutomationError("generatedAt must advance for a factual change")
        proposed["generatedAt"] = now.isoformat().replace("+00:00", "Z")
        # The existing validator recognizes trusted bootstrap IDs. Supplemental
        # IDs are accepted only as provider-domain-validated canonical records.
        update.validate_dataset(proposed, trusted, original,
                                allow_supplemental_ids={tmdb_id})
        if len(changed_ids) > 4:
            raise update.AutomationError("Mass-change guard: promotion exceeds four series")
        changed_fields = sorted(key for key in set(old or {}) | set(merged)
                                if (old or {}).get(key) != merged.get(key))
        evidence_urls = sorted({fact["sourceUrl"] for fact in facts
                                if fact["nextSeasonNumber"] == candidate["nextSeasonNumber"]})
        timestamp = now.isoformat(timespec="seconds")
        audit_row = {"timestamp": timestamp, "tmdbId": tmdb_id, "title": title,
                     "old": old, "new": merged, "changedFields": changed_fields,
                     "source": "monitored automatic promotion", "sourceUrls": evidence_urls,
                     "monitoredState": state,
                     "promotionReason": "VERIFIED_FACTS passed independent production gate",
                     **(merge_evidence or {})}
        # The audit's explicit source marker must remain unambiguous even when
        # merge evidence includes a provider/source URL of its own.
        audit_row["source"] = "monitored automatic promotion"
        result = _answer(tmdb_id, "PROMOTABLE" if dry_run else "PROMOTED", title=title,
                         monitored_state=state, reason="Independent validation and production merge passed",
                         dry_run=dry_run, promotion_eligible=True, old=old, proposed=merged,
                         changed_fields=changed_fields, source_urls=evidence_urls,
                         validation_result="passed")
        if not dry_run:
            update.publish(proposed, trusted, [audit_row], raw, old_audit, audit_exists,
                           data_path=production_data, audit_path=production_audit,
                           previous_data=original, allow_supplemental_ids={tmdb_id})
        return result
    except (update.AutomationError, OSError, ValueError) as exc:
        state_name = "CONFLICT" if isinstance(exc, update.AutomationError) and "Conflicting" in str(exc) else "VALIDATION_FAILED"
        return _answer(tmdb_id, state_name, title=title, monitored_state=state,
                       reason=str(exc), dry_run=dry_run, promotion_eligible=False,
                       validation_result="failed", fatal=True)
