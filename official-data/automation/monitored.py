"""Phase 2A: eligible official discovery to an isolated monitored registry.

No function here writes the trusted registry, canonical dataset or production audit.
"""
from __future__ import annotations

import datetime as dt
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re

import discovery
import update

MONITORED = update.ROOT / "official-data" / "monitored_series.json"
MONITORED_AUDIT = update.ROOT / "official-data" / "history" / "monitored_changes.jsonl"
STATES = {"VERIFIED_FACTS", "NO_VERIFIED_FACTS", "NOT_PARSER_ELIGIBLE", "PROVIDER_UNAVAILABLE",
          "DISCOVERY_INSUFFICIENT", "VALIDATION_FAILED", "UNSUPPORTED_PROVIDER", "METADATA_UNAVAILABLE"}
FACT_FIELDS = ("status", "nextSeasonNumber", "releaseDate", "releaseYear", "sourceName", "sourceUrl", "announcementDate")
EVIDENCE_FIELDS = ("status", "nextSeasonNumber", "releaseDate", "releaseYear", "sourceName", "sourceUrl", "announcementDate", "rule", "factType", "evidenceText")


def publication_date_from_html(raw: str) -> str | None:
    """Use structured publication metadata only, never the first body date."""
    class Dates(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "meta" and attrs.get("property", attrs.get("name", "")).casefold() in (
                    "article:published_time", "datepublished", "date_published", "pubdate"):
                self.values.append(attrs.get("content", ""))
            elif tag == "time" and attrs.get("datetime"):
                self.values.append(attrs["datetime"])

    dates = Dates()
    dates.feed(raw)
    valid = set()
    for value in dates.values:
        try:
            valid.add(dt.date.fromisoformat(value[:10]).isoformat())
        except (TypeError, ValueError):
            continue
    return next(iter(valid)) if len(valid) == 1 else None


def scoped_article(article: dict, aliases: list[str]) -> dict:
    """Keep only body clauses that name this show at a word boundary.

    The shared parser still performs factual extraction. This narrows its
    input for newly discovered sources, where neighboring show coverage can
    otherwise contaminate a generic index or editorial page.
    """
    punctuation = str.maketrans({"\u2018": "'", "\u2019": "'"})
    patterns = [re.compile(r"(?<![\w])" + re.escape(alias.translate(punctuation)) + r"(?![\w])", re.I)
                for alias in aliases]
    def belongs(clause: str) -> bool:
        comparable = clause.translate(punctuation)
        for alias, pattern in zip(aliases, patterns):
            for match in pattern.finditer(comparable):
                if (not alias.casefold().startswith("the ") and
                        re.search(r"\bthe\s+$", clause[:match.start()], re.I)):
                    continue
                return True
        return False
    chunks = re.split(r"[\n.!?]+", article["body"])
    return {**article, "body": "\n".join(chunk for chunk in chunks if belongs(chunk))}


def official_article_heading_identity(page: update.Page, aliases: list[str]) -> bool:
    return discovery.announcement_article_identity(page, aliases)


def load_registry(path: Path = MONITORED) -> tuple[dict, bytes]:
    raw = path.read_bytes() if path.exists() else b'{"schemaVersion":1,"series":[]}\n'
    try:
        data = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise update.AutomationError("Invalid monitored registry JSON") from exc
    validate_registry(data)
    return data, raw


def validate_registry(data: dict) -> None:
    if not isinstance(data, dict) or data.get("schemaVersion") != 1 or not isinstance(data.get("series"), list):
        raise update.AutomationError("Invalid monitored registry schema")
    ids = []
    specs = discovery.provider_specs()
    for row in data["series"]:
        if not isinstance(row, dict):
            raise update.AutomationError("Invalid monitored record")
        identifier = row.get("tmdbId")
        provider = row.get("provider")
        if (not isinstance(identifier, int) or isinstance(identifier, bool) or identifier <= 0 or
                not isinstance(row.get("title"), str) or not row["title"].strip() or
                row.get("discoveryState") not in STATES or
                provider is not None and provider not in specs):
            raise update.AutomationError("Invalid monitored identity/state")
        ids.append(identifier)
        urls = row.get("candidateUrls")
        evidence = row.get("sourceEvidence")
        facts = row.get("verifiedFacts")
        if not isinstance(urls, list) or not isinstance(evidence, list) or not isinstance(facts, dict):
            raise update.AutomationError("Invalid monitored evidence shape")
        domains = specs[provider]["officialDomains"] if provider else []
        if any(not isinstance(url, str) or not update.allowed(url, domains) for url in urls):
            raise update.AutomationError("Untrusted monitored candidate URL")
        if any(not isinstance(item, dict) or not update.allowed(item.get("sourceUrl", ""), domains)
               or item.get("status") not in update.STATUSES or
               not isinstance(item.get("nextSeasonNumber"), int) or item["nextSeasonNumber"] <= 0
               for item in evidence):
            raise update.AutomationError("Invalid monitored source evidence")
        if any("evidenceText" in item and (not isinstance(item["evidenceText"], str) or len(item["evidenceText"]) > 200)
               for item in evidence):
            raise update.AutomationError("Invalid monitored evidence excerpt")
        if any("factType" in item and item["factType"] not in ("LIFECYCLE", "RELEASE_DATE", "RELEASE_YEAR")
               for item in evidence):
            raise update.AutomationError("Invalid monitored fact type")
        if any("publicationDateSource" in item and item["publicationDateSource"] not in ("structured_metadata", "none")
               for item in evidence):
            raise update.AutomationError("Invalid publication-date provenance")
        # New rows persist the parser gate and the exact identity-checked
        # candidate URLs so promotion can verify where each fact came from.
        # Older rows remain readable; promotion itself fails closed when this
        # proof is absent.
        if "parserResult" in row and row["parserResult"] not in ("facts", "no_facts", "failed", "skipped", "unavailable"):
            raise update.AutomationError("Invalid monitored parser result")
        if "validationResult" in row and row["validationResult"] not in ("passed", "failed", "not_applicable"):
            raise update.AutomationError("Invalid monitored validation result")
        eligible_urls = row.get("eligibleSourceUrls")
        if eligible_urls is not None and (not isinstance(eligible_urls, list) or
                any(not isinstance(url, str) or not update.allowed(url, domains) for url in eligible_urls)):
            raise update.AutomationError("Invalid eligible source URLs")
        if row["discoveryState"] == "VERIFIED_FACTS":
            if (not evidence or facts.get("status") not in update.STATUSES or
                    not isinstance(facts.get("nextSeasonNumber"), int) or facts["nextSeasonNumber"] <= 0 or
                    not facts.get("sourceName") or not update.allowed(facts.get("sourceUrl", ""), domains)):
                raise update.AutomationError("Verified monitored record lacks official evidence")
            if eligible_urls is not None and any(item.get("sourceUrl") not in eligible_urls for item in evidence):
                raise update.AutomationError("Evidence source was not parser-eligible")
        elif facts or evidence:
            raise update.AutomationError("Unverified monitored record contains facts")
        for item in evidence + ([facts] if facts else []):
            if ((item.get("status") == "RELEASE_DATE_CONFIRMED" and not item.get("releaseDate")) or
                    (item.get("status") == "CANCELED" and item.get("releaseDate"))):
                raise update.AutomationError("Inconsistent monitored lifecycle status")
            for key in ("releaseDate", "announcementDate"):
                if item.get(key) is not None:
                    try:
                        dt.date.fromisoformat(item[key])
                    except (TypeError, ValueError) as exc:
                        raise update.AutomationError("Invalid monitored fact date") from exc
            if item.get("releaseDate") and item.get("releaseYear") != int(item["releaseDate"][:4]):
                raise update.AutomationError("Monitored release year mismatch")
        for date_key in ("lastChecked",):
            try:
                dt.date.fromisoformat(row[date_key])
            except (KeyError, TypeError, ValueError) as exc:
                raise update.AutomationError("Invalid monitored check date") from exc
    if ids != sorted(set(ids)):
        raise update.AutomationError("Duplicate or unsorted monitored TMDB IDs")


def audit_bytes(path: Path = MONITORED_AUDIT) -> bytes:
    raw = path.read_bytes() if path.exists() else b""
    try:
        for line in raw.decode("utf-8").splitlines():
            if line.strip() and not isinstance(json.loads(line), dict):
                raise ValueError("Audit entry is not an object")
    except (UnicodeError, ValueError) as exc:
        raise update.AutomationError("Invalid monitored audit") from exc
    return raw


def publish(registry: dict, old_registry: bytes, old_audit: bytes, audit_existed: bool,
            audit_row: dict, registry_path: Path = MONITORED, audit_path: Path = MONITORED_AUDIT) -> None:
    """Stage both files; restore both if replacement or verification fails."""
    new_registry = (json.dumps(registry, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    new_audit = old_audit + (json.dumps(audit_row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    staged_registry = staged_audit = None
    replaced = False
    committed = False
    try:
        staged_registry = update.staged_file(registry_path, new_registry)
        staged_audit = update.staged_file(audit_path, new_audit)
        os.replace(staged_registry, registry_path)
        staged_registry = None
        replaced = True
        os.replace(staged_audit, audit_path)
        staged_audit = None
        if registry_path.read_bytes() != new_registry or audit_path.read_bytes() != new_audit:
            raise update.AutomationError("Monitored write verification failed")
        validate_registry(json.loads(registry_path.read_text(encoding="utf-8")))
        audit_bytes(audit_path)
        committed = True
    finally:
        if replaced and not committed:
            rollback = update.staged_file(registry_path, old_registry)
            os.replace(rollback, registry_path)
            if audit_existed:
                rollback_audit = update.staged_file(audit_path, old_audit)
                os.replace(rollback_audit, audit_path)
            elif audit_path.exists():
                audit_path.unlink()
        for staged in (staged_registry, staged_audit):
            if staged is not None:
                staged.unlink(missing_ok=True)


def process(tmdb_id: int, dry_run: bool = True, *, metadata: dict | None = None,
            discoverer=None, fetcher=update.fetch, today: dt.date | None = None,
            now: dt.datetime | None = None, registry_path: Path = MONITORED,
            audit_path: Path = MONITORED_AUDIT) -> dict:
    if not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool) or tmdb_id <= 0:
        raise ValueError("TMDB ID must be positive")
    now = now or dt.datetime.now(dt.timezone.utc)
    today = today or now.date()
    trusted = json.loads(update.REGISTRY.read_text(encoding="utf-8"))
    update.validate_registry(trusted)
    if any(item["tmdbId"] == tmdb_id for item in trusted["series"]):
        return {"tmdbId": tmdb_id, "pipelineState": "ALREADY_TRUSTED", "dryRun": dry_run,
                "discoveryResult": "trusted_registry", "parserResult": "skipped", "validationResult": "not_applicable",
                "verifiedFacts": {}, "sourceEvidence": [], "candidateUrls": [],
                "registryChange": "none", "eligibleCandidateCount": 0, "parsedSourceCount": 0}
    registry, old_registry = load_registry(registry_path)
    old_audit = audit_bytes(audit_path)
    audit_existed = audit_path.exists()
    try:
        meta = metadata if metadata is not None else discovery.tmdb_metadata(tmdb_id, discovery.local_tmdb_token())
    except (OSError, UnicodeError, ValueError, update.AutomationError):
        return {"tmdbId": tmdb_id, "pipelineState": "METADATA_UNAVAILABLE", "dryRun": dry_run,
                "discoveryResult": "not_run", "parserResult": "skipped", "validationResult": "not_applicable",
                "verifiedFacts": {}, "sourceEvidence": [], "candidateUrls": [],
                "registryChange": "none", "eligibleCandidateCount": 0, "parsedSourceCount": 0}
    if not isinstance(meta, dict) or meta.get("tmdbId") != tmdb_id:
        raise update.AutomationError("TMDB metadata identity mismatch")
    report = (discoverer or discovery.discover)(meta, trusted)
    provider = report.get("provider")
    candidates = report.get("candidates") or []
    eligible = [c for c in candidates if report.get("result") == "candidate_found" and c.get("factualParserEligible") is True]
    # This is the only entrance to the factual parser. SUPPORTED, identity and
    # official-domain signals cannot substitute for this explicit gate.
    state = "DISCOVERY_INSUFFICIENT"
    if report.get("result") == "provider_unavailable":
        state = "PROVIDER_UNAVAILABLE"
    elif report.get("reason") == "unknown_provider":
        state = "UNSUPPORTED_PROVIDER"
    elif report.get("result") == "candidate_found" and not eligible:
        state = "NOT_PARSER_ELIGIBLE"
    facts = []
    parsed = 0
    eligible_source_urls: set[str] = set()
    source_checks = []
    validation_reason = None
    merged = None
    source_evidence = []
    if eligible:
        spec = discovery.provider_specs()[provider]
        domains = spec["candidateDomains"]
        entry = {"tmdbId": tmdb_id, "title": meta["title"], "provider": provider,
                 "aliases": discovery.metadata_names(meta), "allowedDomains": domains}
        try:
            for candidate in eligible:
                url = candidate["candidateUrl"]
                candidate_allowed = update.allowed(url, domains)
                check = {"candidateUrl": url, "candidateDomainAllowed": candidate_allowed,
                         "parserEligible": candidate.get("factualParserEligible") is True}
                source_checks.append(check)
                if not candidate_allowed:
                    raise update.AutomationError("Eligible candidate outside allowlist")
                raw, final = fetcher(url, domains)
                final_allowed = update.allowed(final, domains)
                check.update(finalUrl=final, redirectDomainAllowed=final_allowed)
                if not final_allowed:
                    raise update.AutomationError("Eligible candidate redirect outside allowlist")
                eligible_source_urls.update((url, final))
                if provider in ("NETFLIX", "APPLE", "AMC", "DISNEY_PLUS"):
                    # The page may have changed since discovery. Recheck the
                    # visible heading, including the title boundary, before
                    # treating any sentence as evidence for this series.
                    page = update.parse_page(raw)
                    if provider in ("AMC", "DISNEY_PLUS"):
                        identity_matches = official_article_heading_identity(page, entry["aliases"])
                    else:
                        heading = discovery.normalize(page.heading.strip() or page.title.strip())
                        identity_matches = any(heading == discovery.normalize(name) or
                                               heading.startswith(discovery.normalize(name) + " ")
                                               for name in entry["aliases"])
                    check["headlineBoundToMetadataTitle"] = identity_matches
                    if not identity_matches:
                        raise update.AutomationError("Discovered page identity changed")
                article = update.ADAPTERS[provider].parse(raw, final)
                article["publicationDate"] = publication_date_from_html(raw)
                parsed += 1
                facts.extend(update.detect(scoped_article(article, entry["aliases"]), entry, today,
                                         extended_final=True, strict_binding=True))
            update.validate_facts(facts, entry)
            facts = update.canonical_fact_order(facts)
            if facts:
                for season in {f["nextSeasonNumber"] for f in facts}:
                    states = {f["status"] for f in facts if f["nextSeasonNumber"] == season and f["rule"] != "explicit-premiere"}
                    if "CANCELED" in states and len(states) > 1:
                        raise update.AutomationError("Conflicting lifecycle evidence")
                merged, _ = update.merge(None, facts, today)
                if merged is None:
                    raise update.AutomationError("Facts could not be merged")
                # A dated premiere outranks a renewal for the same season;
                # explicit final/cancellation meaning remains sticky.
                if merged["status"] == "RENEWED" and merged.get("releaseDate"):
                    merged["status"] = "RELEASE_DATE_CONFIRMED"
                    date_fact = next(f for f in facts if f["nextSeasonNumber"] == merged["nextSeasonNumber"] and
                                     f.get("releaseDate") == merged["releaseDate"] and f["rule"] == "explicit-premiere")
                    for key in ("sourceName", "sourceUrl", "announcementDate"):
                        merged[key] = date_fact[key]
                source_evidence = [{**{key: fact.get(key) for key in EVIDENCE_FIELDS},
                                    "publicationDateSource": "structured_metadata" if fact.get("announcementDate") else "none"}
                                   for fact in facts]
                source_evidence.sort(key=lambda f: (f["nextSeasonNumber"], f["status"], f["sourceUrl"], f["rule"]))
                state = "VERIFIED_FACTS"
            else:
                state = "NO_VERIFIED_FACTS"
        except update.ProviderUnavailable:
            state = "PROVIDER_UNAVAILABLE"
            facts = []
            parsed = 0
        except (update.AutomationError, ValueError, KeyError) as exc:
            state = "VALIDATION_FAILED"
            validation_reason = type(exc).__name__
            facts = []
    if state == "VALIDATION_FAILED":
        return {"tmdbId": tmdb_id, "title": meta["title"], "provider": provider,
                "discoveryResult": report.get("result"), "eligibleCandidateCount": len(eligible),
                "parsedSourceCount": parsed, "pipelineState": state, "parserResult": "failed",
                "validationResult": "failed", "validationReason": validation_reason,
                "verifiedFacts": {}, "sourceEvidence": [], "candidateUrls": report.get("candidateUrls") or [],
                "dryRun": dry_run, "registryChange": "none", "routeSignals": report.get("routeSignals", []),
                "candidateDiagnostics": [{key: candidate.get(key) for key in (
                    "candidateUrl", "factualParserEligible", "evidenceLevel", "discoveryMethod", "discoveryProvenance", "discoveryRoot", "sourceAuthority", "identityMatchedName", "requestedUrl", "finalUrl", "redirectWithinOfficialBoundary")
                    if key in candidate} for candidate in candidates],
                "sourceChecks": source_checks,
                "selectedOfficialUrl": source_checks[0].get("finalUrl") if source_checks else None}
    verified = {key: merged.get(key) for key in FACT_FIELDS} if state == "VERIFIED_FACTS" else {}
    parser_result = "facts" if state == "VERIFIED_FACTS" else "no_facts" if state == "NO_VERIFIED_FACTS" else \
                    "failed" if state == "VALIDATION_FAILED" else "skipped" if not eligible else "unavailable"
    validation_result = "passed" if state == "VERIFIED_FACTS" else "failed" if state == "VALIDATION_FAILED" else "not_applicable"
    eligible_urls = sorted(eligible_source_urls)
    row = {"tmdbId": tmdb_id, "title": meta["title"], "provider": provider,
           "discoveryState": state, "candidateUrls": sorted(set(report.get("candidateUrls") or [])),
           "verifiedFacts": verified, "sourceEvidence": source_evidence if state == "VERIFIED_FACTS" else [],
           "parserResult": parser_result, "validationResult": validation_result,
           "eligibleSourceUrls": eligible_urls,
           "lastChecked": today.isoformat()}
    current = next((item for item in registry["series"] if item["tmdbId"] == tmdb_id), None)
    comparable = lambda item: {key: value for key, value in item.items() if key != "lastChecked"}
    changed = current is None or comparable(current) != comparable(row)
    if not changed:
        row = current
    proposed = {"schemaVersion": 1, "series": sorted([item for item in registry["series"] if item["tmdbId"] != tmdb_id] + [row], key=lambda item: item["tmdbId"])}
    validate_registry(proposed)
    action = ("would_add" if current is None else "would_update") if changed and dry_run else (
        "added" if current is None else "updated") if changed else "none"
    result = {"tmdbId": tmdb_id, "title": meta["title"], "provider": provider,
              "discoveryResult": report.get("result"), "eligibleCandidateCount": len(eligible),
              "parsedSourceCount": parsed, "pipelineState": state, "verifiedFacts": verified,
              "sourceEvidence": row["sourceEvidence"], "candidateUrls": row["candidateUrls"],
              "parserResult": parser_result,
              "validationResult": validation_result,
              "dryRun": dry_run, "registryChange": action,
              "routeSignals": report.get("routeSignals", []),
              "candidateDiagnostics": [{key: candidate.get(key) for key in (
                  "candidateUrl", "factualParserEligible", "evidenceLevel", "discoveryMethod", "discoveryProvenance", "discoveryRoot", "sourceAuthority", "identityMatchedName", "requestedUrl", "finalUrl", "redirectWithinOfficialBoundary")
                  if key in candidate} for candidate in candidates],
              "sourceChecks": source_checks,
              "selectedOfficialUrl": source_checks[0].get("finalUrl") if source_checks else None,
              "eligibleSourceUrls": sorted(eligible_source_urls),
              "oldRecord": current if dry_run and changed else None,
              "proposedRecord": row if dry_run and changed else None}
    if validation_reason:
        result["validationReason"] = validation_reason
    if not dry_run and changed:
        changed_fields = sorted(key for key in row if current is None or current.get(key) != row[key])
        audit_row = {"timestamp": now.isoformat(timespec="seconds"), "tmdbId": tmdb_id,
                     "oldState": current["discoveryState"] if current else None, "newState": state,
                     "changedFields": changed_fields, "sourceUrls": sorted({f["sourceUrl"] for f in row["sourceEvidence"]})}
        publish(proposed, old_registry, old_audit, audit_existed, audit_row, registry_path, audit_path)
    return result
