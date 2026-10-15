"""Read-only, production-path provider coverage audit.

Corpus expectations are independently curated first-party facts. The harness
does not feed known announcement URLs to generic discovery; it measures those
URLs separately as source-access diagnostics. All monitored processing is dry
run and isolated from the trusted production registry.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import socket
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import discovery
import monitored
import promotion
import update

ROOT = update.ROOT
CORPUS = ROOT / "official-data" / "audit" / "provider_coverage_corpus.json"
PROTECTED = (
    "official-data/official_series_data.json",
    "official-data/history/changes.jsonl",
    "official-data/sources.json",
    "official-data/monitored_series.json",
    "official-data/history/monitored_changes.jsonl",
)
FROZEN_MODULES = (
    "official-data/automation/update.py", "official-data/automation/discovery.py",
    "official-data/automation/monitored.py", "official-data/automation/promotion.py",
    "official-data/automation/hbo_production.py", "official-data/automation/max_production.py",
    "official-data/automation/hulu_discovery.py", "official-data/discovery_providers.json",
)
PROVIDERS = ("NETFLIX", "PARAMOUNT", "APPLE", "AMAZON", "AMC", "DISNEY_PLUS", "HULU", "HBO", "MAX", "FX")
PRODUCTION_PROVIDERS = frozenset(PROVIDERS[:-1])
FACT_FIELDS = ("status", "nextSeasonNumber", "releaseDate", "releaseYear", "sourceName", "sourceUrl", "announcementDate")
FAILURE_CLASSES = ("ROUTING_REJECTED", "SOURCE_INACCESSIBLE", "CORPUS_INVALID", "NETWORK_INCONCLUSIVE",
                   "DISCOVERY_MISSED", "IDENTITY_REJECTED", "OTHER_SAFE_FALSE_NEGATIVE",
                   "SEASON_BINDING_FAILED", "STATUS_BINDING_FAILED", "REGIONAL_DATE_POLICY_BLOCKED",
                   "RECONSTRUCTION_MISMATCH", "FALSE_POSITIVE", "UNEXPECTED_EXCEPTION")
MAX_PROVIDER_REQUESTS = 120
MAX_DIAGNOSTIC_REQUESTS = MAX_PROVIDER_REQUESTS // 2
MAX_GENERIC_REQUESTS = MAX_PROVIDER_REQUESTS - MAX_DIAGNOSTIC_REQUESTS


class CorpusError(ValueError):
    pass


def _first_party(url: object, domains: list[str]) -> bool:
    return isinstance(url, str) and update.allowed(url, domains) and urlsplit(url).scheme == "https"


def _canonical(url: str) -> str:
    p = urlsplit(url)
    query = urlencode(sorted(parse_qsl(p.query, keep_blank_values=True)))
    return urlunsplit((p.scheme.lower(), (p.hostname or "").lower(), p.path.rstrip("/") or "/", query, ""))


def validate_corpus(data: object) -> dict:
    """Validate audit-only corpus structure and semantic URL uniqueness."""
    if not isinstance(data, dict) or data.get("schemaVersion") != 1 or not isinstance(data.get("corpusVersion"), str):
        raise CorpusError("invalid corpus schema/version")
    providers = data.get("providers")
    if not isinstance(providers, list):
        raise CorpusError("providers must be an array")
    ids: set[str] = set()
    provider_ids: set[str] = set()
    urls: set[tuple[str, str, str]] = set()
    for provider in providers:
        if not isinstance(provider, dict):
            raise CorpusError("provider entry must be an object")
        name, domains, cases = provider.get("provider"), provider.get("officialDomains"), provider.get("cases")
        if name not in PROVIDERS or name in provider_ids or not isinstance(domains, list) or not domains or not isinstance(cases, list):
            raise CorpusError("unknown/duplicate provider or invalid domains/cases")
        provider_ids.add(name)
        if provider.get("productionEnabled") is not (name in PRODUCTION_PROVIDERS):
            raise CorpusError(f"productionEnabled mismatch for {name}")
        if any(not isinstance(d, str) or not re.fullmatch(r"(?:[a-z0-9-]+\.)*[a-z0-9-]+", d) for d in domains):
            raise CorpusError(f"invalid allowlist for {name}")
        if name in PRODUCTION_PROVIDERS:
            configured = discovery.provider_specs().get(name, {}).get("officialDomains", [])
            if not configured or not set(domains).issubset(set(configured)):
                raise CorpusError(f"corpus allowlist broadens the production provider boundary: {name}")
        elif name == "FX" and not set(domains).issubset({"fxnetworks.com"}):
            raise CorpusError("FX control corpus exceeds the existing FX first-party boundary")
        for case in cases:
            if not isinstance(case, dict):
                raise CorpusError(f"invalid case in {name}")
            case_id, kind = case.get("caseId"), case.get("kind")
            if not isinstance(case_id, str) or not case_id.strip() or case_id in ids:
                raise CorpusError(f"duplicate or missing caseId: {case_id!r}")
            ids.add(case_id)
            if kind not in ("positive", "negative", "manual_review"):
                raise CorpusError(f"invalid case kind: {case_id}")
            if case.get("goldReviewStatus") not in ("VERIFIED", "MANUAL_REVIEW"):
                raise CorpusError(f"invalid goldReviewStatus: {case_id}")
            if not isinstance(case.get("manuallyCurated"), bool) or not isinstance(case.get("manuallySeeded"), bool):
                raise CorpusError(f"missing curation/seed flags: {case_id}")
            provenance = case.get("curationProvenance")
            if isinstance(provenance, dict):
                if (not isinstance(provenance.get("method"), str) or not provenance["method"].strip()
                        or provenance.get("parserOutputUsedAsGroundTruth") is not False):
                    raise CorpusError(f"invalid independent curation provenance: {case_id}")
            elif not isinstance(provenance, str) or not provenance.strip():
                raise CorpusError(f"missing curation provenance: {case_id}")
            seeded_mode = case.get("seededMode")
            if seeded_mode is not None and seeded_mode not in ("none", "canonical_url", "manual"):
                raise CorpusError(f"invalid seededMode: {case_id}")
            if seeded_mode in ("canonical_url", "manual") and not case["manuallySeeded"]:
                raise CorpusError(f"seededMode contradicts manuallySeeded: {case_id}")
            if kind == "manual_review" or case.get("goldReviewStatus") == "MANUAL_REVIEW":
                continue
            url = case.get("canonicalUrl")
            if not _first_party(url, domains):
                raise CorpusError(f"non-first-party or invalid canonicalUrl: {case_id}")
            semantic = case.get("semanticCaseClass", "lifecycle" if kind == "positive" else "negative")
            key = (name, str(semantic), _canonical(url))
            if key in urls:
                raise CorpusError(f"duplicate canonical URL within semantic class: {case_id}")
            urls.add(key)
            if not isinstance(case.get("independentEvidenceUrl"), str) or not _first_party(case["independentEvidenceUrl"], domains):
                raise CorpusError(f"missing independent first-party evidence URL: {case_id}")
            if not isinstance(case.get("independentRationale"), str) or len(case["independentRationale"].strip()) < 12:
                raise CorpusError(f"missing independent rationale: {case_id}")
            if kind == "positive":
                if case.get("goldReviewStatus") != "VERIFIED":
                    raise CorpusError(f"positive case is not reviewed: {case_id}")
                if not isinstance(provenance, dict) or provenance.get("parserOutputUsedAsGroundTruth") is not False:
                    raise CorpusError(f"positive gold needs explicit parser-independent provenance: {case_id}")
                if not isinstance(case.get("tmdbId"), int) or isinstance(case.get("tmdbId"), bool) or case["tmdbId"] <= 0:
                    raise CorpusError(f"positive case needs a positive TMDB identity: {case_id}")
                if not isinstance(case.get("title"), str) or not case["title"].strip():
                    raise CorpusError(f"positive case needs a title: {case_id}")
                aliases = case.get("aliases")
                if not isinstance(aliases, list) or not aliases or any(not isinstance(a, str) or not a.strip() for a in aliases):
                    raise CorpusError(f"positive case needs identity aliases: {case_id}")
                facts = case.get("expectedFacts")
                if not isinstance(facts, dict) or set(facts) != set(FACT_FIELDS):
                    raise CorpusError(f"positive case requires all seven expected facts: {case_id}")
                known = case.get("expectedFactKnownFields", list(FACT_FIELDS))
                if (not isinstance(known, list) or not known or len(set(known)) != len(known)
                        or any(field not in FACT_FIELDS for field in known)):
                    raise CorpusError(f"invalid expectedFactKnownFields: {case_id}")
                if not {"status", "nextSeasonNumber"}.issubset(known):
                    raise CorpusError(f"gold facts must independently establish status and season: {case_id}")
                if facts.get("releaseDate") is not None and "releaseDate" not in known:
                    raise CorpusError(f"an asserted release date must be a known gold field: {case_id}")
                if facts.get("status") not in ("RENEWED", "RELEASE_DATE_CONFIRMED", "FINAL_SEASON", "CANCELED"):
                    raise CorpusError(f"invalid expected lifecycle status: {case_id}")
                season = facts.get("nextSeasonNumber")
                if not isinstance(season, int) or isinstance(season, bool) or season <= 0:
                    raise CorpusError(f"positive expected facts need explicit season: {case_id}")
                if facts.get("sourceUrl") != url or not _first_party(facts.get("sourceUrl"), domains):
                    raise CorpusError(f"expected sourceUrl must be the first-party canonical URL: {case_id}")
                if not isinstance(facts.get("sourceName"), str) or not facts["sourceName"].strip():
                    raise CorpusError(f"missing expected sourceName: {case_id}")
                if case.get("releaseDateScope") not in ("global", "regional", "none"):
                    raise CorpusError(f"invalid releaseDateScope: {case_id}")
            elif case.get("expectedFacts") is not None:
                raise CorpusError(f"negative case cannot contain expected lifecycle facts: {case_id}")
    if provider_ids != set(PROVIDERS):
        raise CorpusError(f"provider set mismatch; missing={sorted(set(PROVIDERS)-provider_ids)}")
    return data


def load_corpus(path: Path = CORPUS) -> dict:
    return validate_corpus(json.loads(path.read_text(encoding="utf-8")))


def protected_hashes() -> dict[str, str]:
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in PROTECTED}


def frozen_hashes() -> dict[str, str]:
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FROZEN_MODULES}


_CREDENTIAL_PATTERNS = (re.compile(r"(?im)\b(?:TMDB_API_TOKEN|API_KEY|ACCESS_TOKEN|CLIENT_SECRET|PASSWORD)\s*[:=]\s*['\"]?([A-Za-z0-9_./+=-]{20,})"),
                        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{24,}"),
                        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"))


def credential_scan_texts(contents: list[str]) -> int:
    return sum(len(pattern.findall(content)) for content in contents for pattern in _CREDENTIAL_PATTERNS)


def credential_scan(paths: list[Path]) -> int:
    matches = 0
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        matches += credential_scan_texts([content])
    return matches


def classify_exception(exc: BaseException, *, repeated: bool = False) -> str:
    if isinstance(exc, HTTPError):
        return "SOURCE_INACCESSIBLE" if exc.code in (401, 403, 429) or exc.code >= 500 else "CORPUS_INVALID" if exc.code == 404 else "SOURCE_INACCESSIBLE"
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "SOURCE_INACCESSIBLE" if repeated else "NETWORK_INCONCLUSIVE"
        return "SOURCE_INACCESSIBLE" if repeated else "NETWORK_INCONCLUSIVE"
    if isinstance(exc, update.ProviderUnavailable):
        status = re.search(r"\bHTTP\s+(\d{3})\b", str(exc), re.I)
        if status and status.group(1) == "404":
            return "CORPUS_INVALID"
        if status and status.group(1) in ("401", "403", "429"):
            return "SOURCE_INACCESSIBLE"
        if status and (status.group(1) == "408" or int(status.group(1)) >= 500):
            return "SOURCE_INACCESSIBLE"
        return "NETWORK_INCONCLUSIVE"
    if isinstance(exc, update.AutomationError):
        message = str(exc).casefold()
        if "redirect" in message:
            return "REDIRECT_REJECTED"
        if "outside" in message or "allowlist" in message:
            return "OFFICIAL_DOMAIN_REJECTED"
        if "too large" in message:
            return "RESPONSE_TOO_LARGE"
        return "VALIDATION_FAILURE"
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError, OSError)):
        return "SOURCE_INACCESSIBLE" if repeated else "NETWORK_INCONCLUSIVE"
    return "UNEXPECTED_EXCEPTION"


class BoundedFetcher:
    """Per-provider request budget and deduplicating transport wrapper."""
    def __init__(self, provider: str, *, transport=None, max_requests: int = MAX_PROVIDER_REQUESTS, budget=None):
        self.provider = provider
        self.transport = transport
        self.max_requests = max_requests
        self.budget = budget if budget is not None else {"count": 0, "limit": max_requests, "truncated": False}
        self.cache: dict[tuple[str, tuple[str, ...]], tuple[str, str]] = {}
        self.requests: list[dict] = []
        self.timeouts = 0
        self.truncated = False

    def __call__(self, url: str, domains: list[str], headers: dict | None = None):
        key = (url, tuple(sorted(domains)))
        if key in self.cache:
            return self.cache[key]
        if self.budget["count"] >= self.budget["limit"]:
            self.truncated = True
            self.budget["truncated"] = True
            raise update.ProviderUnavailable("provider coverage request budget exhausted")
        if not update.allowed(url, domains):
            raise update.AutomationError("URL outside existing provider allowlist")
        self.requests.append({"url": _public_url(url), "attempt": 1})
        self.budget["count"] += 1
        transport = self.transport
        if transport is None:
            if headers is not None or "api.themoviedb.org" in domains:
                transport = update.fetch
            elif self.provider == "HBO":
                import hbo_production
                transport = hbo_production.fetch
            elif self.provider == "MAX":
                import max_production
                transport = max_production.fetch
            else:
                transport = update.fetch
        try:
            result = transport(url, domains, headers) if headers is not None else transport(url, domains)
        except (TimeoutError, socket.timeout):
            self.timeouts += 1
            raise
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                self.timeouts += 1
            raise
        raw, final = result
        if not update.allowed(final, domains):
            raise update.AutomationError("redirect outside existing provider allowlist")
        self.cache[key] = (raw, final)
        return raw, final


def _public_url(url: str | None) -> str | None:
    if not isinstance(url, str):
        return None
    p = urlsplit(url)
    if p.scheme not in ("http", "https"):
        return None
    protected_names = {"token", "access_token", "api_key", "apikey", "key", "auth", "password", "secret", "client_secret"}
    query = urlencode([(key, "[REDACTED]" if key.casefold() in protected_names else value)
                       for key, value in parse_qsl(p.query, keep_blank_values=True)])
    return urlunsplit((p.scheme, p.netloc, p.path, query, ""))


def _public_facts(facts: dict | None) -> dict | None:
    if not isinstance(facts, dict):
        return facts
    output = dict(facts)
    if isinstance(output.get("sourceUrl"), str):
        output["sourceUrl"] = _public_url(output["sourceUrl"])
    return output


def _fresh_metadata(tmdb_id: int, token: str, fetcher) -> dict:
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?language=en-US"
    raw, _ = fetcher(url, ["api.themoviedb.org"], {"Authorization": f"Bearer {token}", "Accept": "application/json"})
    data = json.loads(raw)
    if data.get("id") != tmdb_id or not isinstance(data.get("name"), str) or not data["name"].strip():
        raise update.AutomationError("fresh TMDB identity mismatch")
    return {"tmdbId": data["id"], "title": data["name"], "originalName": data.get("original_name"),
            "networks": data.get("networks") or [], "productionCompanies": data.get("production_companies") or [],
            "homepage": data.get("homepage"), "originCountry": data.get("origin_country") or []}


def _compare_facts(actual: dict, expected: dict) -> list[str]:
    return [field for field in FACT_FIELDS if actual.get(field) != expected.get(field)]


@contextlib.contextmanager
def _isolated_trusted_registry():
    """Run monitored processing as an unregistered identity in an audit-only sandbox."""
    original_registry = update.REGISTRY
    original_validate = update.validate_registry
    with tempfile.TemporaryDirectory(prefix="provider-coverage-") as temp:
        monitored_path = Path(temp) / "monitored.json"
        monitored_path.write_text('{"schemaVersion":1,"series":[]}\n', encoding="utf-8")
        audit_path = Path(temp) / "monitored-audit.jsonl"
        # The production validator intentionally freezes the trusted registry
        # at 12 records. In this isolated audit only, an empty registry means
        # the case is explicitly hypothetical/unregistered; all nonempty
        # registries still go through the frozen production validator.
        def validate_audit_registry(registry):
            if isinstance(registry, dict) and registry.get("series") == []:
                return None
            return original_validate(registry)
        update.REGISTRY = Path(temp) / "trusted.json"
        update.REGISTRY.write_text('{"schemaVersion":1,"series":[]}\n', encoding="utf-8")
        update.validate_registry = validate_audit_registry
        try:
            yield monitored_path, audit_path
        finally:
            update.REGISTRY = original_registry
            update.validate_registry = original_validate


def _run_pipeline(provider_id: str, case: dict, metadata: dict, *, fetcher: BoundedFetcher,
                  specs: dict, direct_diagnostic: bool, today: dt.date,
                  direct_url: str | None = None) -> dict:
    """Persist a monitored candidate in a temporary registry, then replay it."""
    if direct_diagnostic:
        url = direct_url or case["canonicalUrl"]
        direct_report = {"provider": provider_id, "result": "candidate_found", "candidateUrls": [url],
                         "candidates": [{"candidateUrl": url, "factualParserEligible": True,
                                         "identityValidated": True, "providerRoutingConfidence": "NETWORK",
                                         "discoveryMethod": "direct_url_diagnostic", "knownUrlUsedAsSeed": True}]}
        discoverer = lambda _metadata, _registry: direct_report
    else:
        discoverer = lambda md, registry: discovery.discover(md, registry, fetcher=fetcher, specs=specs)
    with _isolated_trusted_registry() as (registry_path, audit_path):
        try:
            state = monitored.process(case["tmdbId"], dry_run=False, metadata=metadata,
                                      discoverer=discoverer, fetcher=fetcher,
                                      registry_path=registry_path, audit_path=audit_path, today=today)
            registry, _ = monitored.load_registry(registry_path)
            row = next((item for item in registry["series"] if item["tmdbId"] == case["tmdbId"]), None)
        except Exception as exc:
            return {"pipelineState": "UNEXPECTED_EXCEPTION", "parserResult": None,
                    "validationResult": None, "persisted": False, "verifiedFacts": {},
                    "reconstructionPassed": False, "reconstructedFacts": None,
                    "errorType": type(exc).__name__}
    result = {"pipelineState": state.get("pipelineState"), "parserResult": state.get("parserResult"),
              "validationResult": state.get("validationResult"), "persisted": isinstance(row, dict),
              "verifiedFacts": {}, "reconstructionPassed": False, "reconstructedFacts": None,
              "errorType": None}
    if result["pipelineState"] != "VERIFIED_FACTS":
        return result
    if not isinstance(row, dict):
        result["errorType"] = "PERSISTED_RECORD_MISSING"
        return result
    result["verifiedFacts"] = {key: (row.get("verifiedFacts") or {}).get(key) for key in FACT_FIELDS}
    try:
        facts = promotion._validate_evidence(row, specs[provider_id]["candidateDomains"])
        summary = promotion._facts_summary(facts, row, today)
    except (update.AutomationError, ValueError, KeyError, TypeError) as exc:
        result["errorType"] = type(exc).__name__
        return result
    result["reconstructionPassed"] = True
    result["reconstructedFacts"] = {key: summary.get(key) for key in FACT_FIELDS}
    return result


def _case_result_internal(provider: dict, case: dict, *, token: str, specs: dict, transport=None,
                 provider_fetcher: BoundedFetcher | None = None,
                 diagnostic_fetcher: BoundedFetcher | None = None, today: dt.date | None = None) -> dict:
    provider_id, domains = provider["provider"], provider["officialDomains"]
    result = {"caseId": case["caseId"], "provider": provider_id, "title": case.get("title"),
              "goldReviewStatus": case.get("goldReviewStatus"),
              "kind": case["kind"], "expectedFacts": _public_facts(case.get("expectedFacts")),
              "expectedFactKnownFields": case.get("expectedFactKnownFields", list(FACT_FIELDS)),
              "canonicalUrl": _public_url(case.get("canonicalUrl")),
              "manuallySeeded": case["manuallySeeded"], "genericDiscovery": False,
              "identityAccepted": False, "lifecycleParsed": False, "reconstructionPassed": False,
              "covered": False, "primaryFailure": None, "secondaryFailures": [],
              "routeValid": None, "sourceAccessible": None, "rawNetworkLabels": [], "httpStatus": None}
    if case["kind"] == "manual_review" or case.get("goldReviewStatus") == "MANUAL_REVIEW":
        result.update(primaryFailure="MANUAL_REVIEW", denominatorEligible=False)
        return result
    fetcher = provider_fetcher or BoundedFetcher(provider_id, transport=transport)
    direct_fetcher = diagnostic_fetcher or BoundedFetcher(provider_id, transport=transport)
    result["denominatorEligible"] = case["kind"] == "positive" and not case["manuallySeeded"]
    if provider_id == "FX":
        result.update(primaryFailure="INACTIVE_CONTROL_ONLY", denominatorEligible=False, routeValid=False)
        return result
    if not token:
        result.update(primaryFailure="LOCAL_INCONCLUSIVE", denominatorEligible=False)
        return result
    try:
        metadata = _fresh_metadata(case["tmdbId"], token, fetcher)
    except (update.AutomationError, ValueError, OSError, json.JSONDecodeError, URLError, TimeoutError) as exc:
        result.update(primaryFailure="LOCAL_INCONCLUSIVE", denominatorEligible=False,
                      metadataFailure=classify_exception(exc))
        return result
    result["freshMetadataFetched"] = True
    route, signals, _ = discovery.route_provider(metadata, specs)
    result["routeProvider"] = route
    result["rawNetworkLabels"] = [item.get("name") if isinstance(item, dict) else item
                                   for item in metadata.get("networks") or []]
    result["routeSignals"] = signals
    result["routeValid"] = route == provider_id
    if not result["routeValid"]:
        result["primaryFailure"] = "ROUTING_REJECTED"
        return result

    direct_before = len(direct_fetcher.requests)
    try:
        _, direct_final = direct_fetcher(case["canonicalUrl"], specs.get(provider_id, {}).get("candidateDomains", domains))
        result["sourceAccessible"] = True
        result["directUrlDiagnostic"] = {"fetchable": True, "httpStatus": 200,
                                         "finalUrl": _public_url(direct_final),
                                         "requestCount": len(direct_fetcher.requests) - direct_before}
        result["httpStatus"] = 200
    except (update.AutomationError, OSError, URLError, TimeoutError) as exc:
        failure = classify_exception(exc, repeated=isinstance(exc, (URLError, TimeoutError, OSError)))
        status_match = re.search(r"HTTP\s+(\d{3})", str(exc), re.I)
        http_status = getattr(exc, "code", None) or (int(status_match.group(1)) if status_match else None)
        result["sourceAccessible"] = False
        result["directUrlDiagnostic"] = {"fetchable": False, "failure": failure,
                                         "httpStatus": http_status,
                                         "requestCount": len(direct_fetcher.requests) - direct_before}
        result["httpStatus"] = result["directUrlDiagnostic"]["httpStatus"]
        if failure == "CORPUS_INVALID":
            result.update(primaryFailure="CORPUS_INVALID", denominatorEligible=False)
            return result
        if failure == "NETWORK_INCONCLUSIVE":
            result["denominatorEligible"] = False
    if not result["sourceAccessible"]:
        result["primaryFailure"] = result["directUrlDiagnostic"].get("failure", "SOURCE_INACCESSIBLE")
        return result

    current_date = today or dt.datetime.now(dt.timezone.utc).date()
    direct = result["directUrlDiagnostic"]
    direct_result = _run_pipeline(provider_id, case, metadata, fetcher=direct_fetcher,
                                  specs=specs, direct_diagnostic=True, today=current_date,
                                  direct_url=direct_final)
    direct["parserState"] = direct_result["pipelineState"]
    direct["persistedRecord"] = direct_result["persisted"]
    if direct_result["pipelineState"] == "UNEXPECTED_EXCEPTION":
        result["directUnexpectedException"] = direct_result.get("errorType") or "unknown"
    result["falsePositiveDenominatorEligible"] = case["kind"] == "negative" and direct_result["pipelineState"] in (
        "VERIFIED_FACTS", "NO_VERIFIED_FACTS")
    direct["identityAccepted"] = direct_result["pipelineState"] not in ("VALIDATION_FAILED", "DISCOVERY_INSUFFICIENT")
    direct["parserProducedFacts"] = direct_result["pipelineState"] == "VERIFIED_FACTS"
    direct["reconstructionPassed"] = direct_result["reconstructionPassed"]
    direct["reconstructedFacts"] = direct_result["reconstructedFacts"]
    known = result["expectedFactKnownFields"] or []
    expected = case.get("expectedFacts") or {}
    if direct_result["pipelineState"] == "VERIFIED_FACTS" and direct_result["reconstructionPassed"]:
        direct_mismatch = [field for field in known if direct_result["reconstructedFacts"].get(field) != expected.get(field)]
        direct["goldMismatchFields"] = direct_mismatch
        if direct_mismatch:
            direct["failure"] = _gold_failure(direct_mismatch, case)
        direct["parserCompatible"] = case["kind"] == "positive" and not direct_mismatch
        direct["falsePositive"] = case["kind"] == "negative"
    else:
        direct["goldMismatchFields"] = []
        direct["parserCompatible"] = case["kind"] == "negative" and direct_result["pipelineState"] == "NO_VERIFIED_FACTS"
        direct["falsePositive"] = False
        if direct_result.get("errorType"):
            direct["failure"] = "RECONSTRUCTION_MISMATCH"
            result["secondaryFailures"].append("DIRECT_RECONSTRUCTION_MISMATCH")
    if case["kind"] == "negative":
        result["falsePositive"] = bool(direct.get("falsePositive"))
        if result["falsePositive"]:
            result["secondaryFailures"].append("DIRECT_FALSE_POSITIVE")

    try:
        generic = discovery.discover(metadata, {"schemaVersion": 1, "series": []}, fetcher=fetcher, specs=specs)
    except (update.AutomationError, update.ProviderUnavailable, ValueError, OSError, URLError, TimeoutError) as exc:
        failure = classify_exception(exc)
        result["primaryFailure"] = "SOURCE_INACCESSIBLE" if failure in ("SOURCE_INACCESSIBLE", "NETWORK_INCONCLUSIVE") else "DISCOVERY_MISSED"
        result["secondaryFailures"].append(failure)
        return result
    candidates = generic.get("candidates") or []
    result["candidateCount"] = len(candidates)
    result["discoveryResult"] = generic.get("result")
    canonical_targets = {_canonical(case["canonicalUrl"]), _canonical(direct.get("finalUrl") or case["canonicalUrl"])}
    matched = [candidate for candidate in candidates if isinstance(candidate.get("candidateUrl"), str)
               and _canonical(candidate["candidateUrl"]) in canonical_targets]
    candidate = next((item for item in matched if update.allowed(item["candidateUrl"], specs[provider_id]["candidateDomains"])), None)
    result["genericDiscovery"] = bool(candidate and generic.get("result") == "candidate_found" and not case["manuallySeeded"])
    result["discoveredCandidateUrl"] = _public_url(candidate.get("candidateUrl")) if candidate else None
    result["discoveryMethod"] = candidate.get("discoveryMethod") if candidate else None
    result["discoveryProvenance"] = candidate.get("discoveryProvenance") if candidate else None
    result["discoveryRoot"] = _public_url(candidate.get("discoveryRoot")) if candidate else None
    if not candidate:
        if case["kind"] == "negative":
            result["primaryFailure"] = "FALSE_POSITIVE" if result.get("falsePositive") else None
        else:
            result["primaryFailure"] = "DISCOVERY_MISSED"
        return result
    result["identityAccepted"] = bool(result["genericDiscovery"] and candidate.get("identityValidated") is True
                                       and candidate.get("providerRoutingConfidence") == "NETWORK")
    if not result["identityAccepted"]:
        result["primaryFailure"] = "IDENTITY_REJECTED"
        return result
    if candidate.get("factualParserEligible") is not True:
        result["primaryFailure"] = "OTHER_SAFE_FALSE_NEGATIVE"
        return result

    outcome = _run_pipeline(provider_id, case, metadata, fetcher=fetcher,
                            specs=specs, direct_diagnostic=False, today=current_date)
    result["pipelineState"] = outcome["pipelineState"]
    result["parserResult"] = outcome["parserResult"]
    result["persistedRecord"] = outcome["persisted"]
    if case["kind"] == "negative" and outcome["pipelineState"] == "NO_VERIFIED_FACTS":
        result["genericFalsePositive"] = False
        if result.get("falsePositive"):
            result["primaryFailure"] = "FALSE_POSITIVE"
        return result
    if outcome["pipelineState"] != "VERIFIED_FACTS":
        result["primaryFailure"] = {"NO_VERIFIED_FACTS": "OTHER_SAFE_FALSE_NEGATIVE",
                                     "PROVIDER_UNAVAILABLE": "SOURCE_INACCESSIBLE",
                                     "VALIDATION_FAILED": "IDENTITY_REJECTED",
                                     "UNEXPECTED_EXCEPTION": "UNEXPECTED_EXCEPTION"}.get(
                                         outcome["pipelineState"], "OTHER_SAFE_FALSE_NEGATIVE")
        if outcome["pipelineState"] == "UNEXPECTED_EXCEPTION":
            result["unexpectedExceptionType"] = outcome.get("errorType")
        return result
    result["lifecycleParsed"] = True
    result["actualFacts"] = outcome["verifiedFacts"]
    if not outcome["reconstructionPassed"]:
        result["primaryFailure"] = "RECONSTRUCTION_MISMATCH"
        return result
    result["reconstructionPassed"] = True
    result["reconstructedFacts"] = outcome["reconstructedFacts"]
    result["announcementDateGroundTruth"] = "known" if "announcementDate" in known else "unknown; inspected at runner"
    mismatches = [field for field in known if result["reconstructedFacts"].get(field) != expected.get(field)]
    result["goldMismatchFields"] = mismatches
    if case["kind"] == "negative":
        result["genericFalsePositive"] = bool(outcome["verifiedFacts"].get("status"))
        result["falsePositive"] = result.get("falsePositive") or result["genericFalsePositive"]
        if result["genericFalsePositive"]:
            result["secondaryFailures"].append("GENERIC_FALSE_POSITIVE")
        if result["falsePositive"] and result["primaryFailure"] is None:
            result["primaryFailure"] = "FALSE_POSITIVE"
    else:
        result["covered"] = not mismatches and not case["manuallySeeded"]
        if mismatches:
            result["primaryFailure"] = _gold_failure(mismatches, case)
        elif case["manuallySeeded"]:
            result["primaryFailure"] = "OTHER_SAFE_FALSE_NEGATIVE"
    result["requestCountTruncated"] = fetcher.truncated or direct_fetcher.truncated
    result["timeoutCount"] = fetcher.timeouts + direct_fetcher.timeouts
    return result


def _gold_failure(mismatches: list[str], case: dict) -> str:
    if "nextSeasonNumber" in mismatches:
        return "SEASON_BINDING_FAILED"
    if "status" in mismatches:
        return "STATUS_BINDING_FAILED"
    if case.get("releaseDateScope") == "regional" and any(field in mismatches for field in ("releaseDate", "releaseYear")):
        return "REGIONAL_DATE_POLICY_BLOCKED"
    return "RECONSTRUCTION_MISMATCH"


def _case_aliases(row: dict, case: dict) -> dict:
    expected = case.get("expectedFacts") or {}
    return {**row,
            "tmdbId": case.get("tmdbId"), "seriesTitle": case.get("title"),
            "route": row.get("routeProvider"), "lifecycleType": expected.get("status"),
            "expectedStatus": expected.get("status"), "expectedNextSeasonNumber": expected.get("nextSeasonNumber"),
            "expectedReleaseDate": expected.get("releaseDate"), "expectedReleaseYear": expected.get("releaseYear"),
            "canonicalFirstPartyUrl": _public_url(case.get("canonicalUrl")),
            "sourceName": expected.get("sourceName"), "announcementDate": expected.get("announcementDate"),
            "discoveredGenerically": row.get("genericDiscovery", False), "parsedFact": row.get("actualFacts"),
            "reconstructionMismatches": row.get("goldMismatchFields", []), "notes": case.get("notes")}


def _case_result(provider: dict, case: dict, **kwargs) -> dict:
    return _case_aliases(_case_result_internal(provider, case, **kwargs), case)


def _pct(n: int, d: int):
    return round(100 * n / d, 2) if d else None


def _provider_rollup(provider: dict, cases: list[dict], *, truncated: bool = False) -> dict:
    positive = [c for c in cases if c["kind"] == "positive" and c.get("goldReviewStatus") != "MANUAL_REVIEW"]
    measured = [c for c in positive if c.get("denominatorEligible")]
    routed = [c for c in measured if c.get("routeValid") is True]
    accessible = [c for c in routed if c.get("sourceAccessible") is True]
    discovered = [c for c in accessible if c.get("genericDiscovery")]
    identified = [c for c in discovered if c.get("identityAccepted")]
    parsed = [c for c in identified if c.get("lifecycleParsed")]
    reconstructed = [c for c in parsed if c.get("reconstructionPassed")]
    covered = [c for c in reconstructed if c.get("covered")]
    negatives = [c for c in cases if c["kind"] == "negative" and c.get("falsePositiveDenominatorEligible")]
    false_positives = [c for c in negatives if c.get("falsePositive")]
    counts = {
        "goldEligible": len(measured),
        "routeValid": len(routed),
        "sourceAccessible": len(accessible),
        "discovered": len(discovered),
        "identityAccepted": len(identified),
        "lifecycleParsed": len(parsed),
        "reconstructionPassed": len(reconstructed),
        "covered": len(covered),
    }
    unknown_gold = any(c.get("expectedFactKnownFields", list(FACT_FIELDS)) != list(FACT_FIELDS) for c in positive)
    has_access_limits = any(c.get("primaryFailure") in ("SOURCE_INACCESSIBLE", "NETWORK_INCONCLUSIVE") for c in cases)
    confidence = "LOW" if len(positive) < 8 or unknown_gold or truncated else "MEDIUM" if len(positive) < 12 or has_access_limits else "HIGH"
    failures = {}
    for c in positive:
        f = c.get("primaryFailure")
        if f and f not in ("MANUAL_REVIEW", "INACTIVE_CONTROL_ONLY", "LOCAL_INCONCLUSIVE"):
            failures[f] = failures.get(f, 0) + 1
    lifecycle_types = {}
    for status in ("RENEWED", "RELEASE_DATE_CONFIRMED", "FINAL_SEASON", "CANCELED"):
        cohort = [c for c in measured if (c.get("expectedFacts") or {}).get("status") == status]
        cohort_covered = sum(bool(c.get("covered")) for c in cohort)
        lifecycle_types[status] = {"eligible": len(cohort), "covered": cohort_covered,
                                   "coveragePercent": _pct(cohort_covered, len(cohort)),
                                   "sampleCount": len(cohort),
                                   "sampleStatus": "INSUFFICIENT_SAMPLE" if len(cohort) < 8 else "MEASURED"}
    direct_compatible = sum(c.get("directUrlDiagnostic", {}).get("parserCompatible") is True for c in measured)
    corpus_size = sum(c["kind"] == "positive" and c.get("goldReviewStatus") == "VERIFIED" for c in cases)
    corpus_small = corpus_size < 8
    rating = ("LOW_CONFIDENCE" if corpus_small else "EXCELLENT" if _pct(len(covered), len(measured)) is not None and _pct(len(covered), len(measured)) >= 80
              else "GOOD" if _pct(len(covered), len(measured)) is not None and _pct(len(covered), len(measured)) >= 60
              else "NEEDS_HARDENING" if _pct(len(covered), len(measured)) is not None and _pct(len(covered), len(measured)) >= 40
              else "POOR")
    return {"provider": provider["provider"], "productionEnabled": provider["productionEnabled"],
            "corpusCount": corpus_size, "corpusSize": corpus_size,
            "eligibleGoldCases": len(measured), "accessiblePositiveCases": len(accessible),
            "manualReviewCount": sum(c["primaryFailure"] == "MANUAL_REVIEW" for c in cases),
            "negativeCount": len(negatives), "falsePositives": len(false_positives),
            "falsePositiveRate": _pct(len(false_positives), len(negatives)),
            "counts": counts, "funnel": [counts[k] for k in ("goldEligible", "routeValid", "sourceAccessible", "discovered", "identityAccepted", "lifecycleParsed", "reconstructionPassed", "covered")],
            "candidateCount": sum(c.get("candidateCount", 0) for c in cases),
            "measuredPositiveCases": len(measured),
            "discoveryPercent": _pct(len(discovered), len(measured)),
            "discoveryCoveragePercent": _pct(len(discovered), len(measured)),
            "rawE2EPercent": _pct(len(covered), len(measured)),
            "rawE2ECoveragePercent": _pct(len(covered), len(measured)),
            "accessibleE2EPercent": _pct(len([c for c in accessible if c.get("covered")]), len(accessible)),
            "accessibleE2ECoveragePercent": _pct(len([c for c in accessible if c.get("covered")]), len(accessible)),
            "directUrlPercent": _pct(direct_compatible, len(measured)),
            "directUrlParserCoveragePercent": _pct(direct_compatible, len(measured)),
            "directParserCompatibleCases": direct_compatible,
            "genericDiscoveryCases": len(discovered),
            "knownUrlOnlyCases": sum(c.get("directUrlDiagnostic", {}).get("fetchable") is True and not c.get("genericDiscovery") for c in measured),
            "manuallySeededCases": sum(c.get("manuallySeeded") is True for c in cases),
            "confidence": confidence, "corpusTooSmall": corpus_small,
            "rating": rating,
            "lifecycleTypeBreakdown": lifecycle_types,
            "failureBreakdown": failures, "positiveFailureBreakdown": failures,
            "negativeSafetyBreakdown": {"tested": len(negatives), "falsePositives": len(false_positives),
                                        "inaccessibleOrUnmeasured": sum(c["kind"] == "negative" and not c.get("falsePositiveDenominatorEligible") for c in cases)}}


def _roi(rows: list[dict]) -> list[dict]:
    candidates = []
    for row in rows:
        if not row["productionEnabled"] or row["provider"] == "FX":
            continue
        if row.get("falsePositives", 0) > 0:
            candidates.append({"provider": row["provider"], "accessibleUncovered": 0,
                               "dominantFailure": "FALSE_POSITIVE", "failureConcentration": 1.0,
                               "score": 1000 + row["falsePositives"], "severity": "HIGH",
                               "confidence": row.get("confidence"),
                               "formula": "safety-priority before coverage ROI; observed false positive count"})
            continue
        if row.get("confidence") == "LOW" or row.get("corpusTooSmall"):
            continue
        c, fail = row["counts"], row["positiveFailureBreakdown"]
        accessible_gaps = max(0, c["sourceAccessible"] - c["covered"])
        if not accessible_gaps:
            continue
        measured = sum(fail.values())
        if not row.get("measuredPositiveCases"):
            continue
        dominant, amount = max(fail.items(), key=lambda item: (item[1], item[0]), default=(None, 0))
        concentration = amount / measured if measured else 0
        # Transparent prioritization: accessible uncovered cases multiplied by
        # the share in the single largest failure stage. No score if unmeasured.
        score = round(accessible_gaps * concentration, 3) if measured else 0
        candidates.append({"provider": row["provider"], "accessibleUncovered": accessible_gaps,
                           "dominantFailure": dominant, "failureConcentration": round(concentration, 3),
                           "score": score, "severity": "COVERAGE", "confidence": row.get("confidence"),
                           "formula": "accessible_uncovered * (dominant_failure / measured_failures)"})
    return sorted(candidates, key=lambda x: (0 if x["severity"] == "HIGH" else 1, -x["score"], -x["accessibleUncovered"], x["provider"]))


def _aggregate_coverage(rows: list[dict]) -> dict:
    positive = sum(r["counts"]["goldEligible"] for r in rows)
    measured = sum(r.get("measuredPositiveCases", 0) for r in rows)
    accessible = sum(r["counts"]["sourceAccessible"] for r in rows)
    covered = sum(r["counts"]["covered"] for r in rows)
    measurable_percentages = [r["rawE2EPercent"] for r in rows if r["rawE2EPercent"] is not None]
    return {"providers": len(rows), "positiveCases": positive, "measuredPositiveCases": measured,
            "accessiblePositiveCases": accessible, "covered": covered,
            "weightedRawE2EPercent": _pct(covered, measured),
            "weightedAccessibleE2EPercent": _pct(covered, accessible),
            "macroProviderCoveragePercent": round(sum(measurable_percentages) / len(measurable_percentages), 2)
            if measurable_percentages else None}


def _failure_roi_analysis(rows: list[dict]) -> list[dict]:
    cases = [(row["provider"], case) for row in rows for case in row["cases"]]
    excluded = {"MANUAL_REVIEW", "INACTIVE_CONTROL_ONLY", "LOCAL_INCONCLUSIVE", None}
    classified = [(provider, case, case.get("primaryFailure")) for provider, case in cases
                  if (case.get("kind") == "positive" and case.get("denominatorEligible")
                      and case.get("primaryFailure") not in excluded)
                  or (case.get("kind") == "negative" and case.get("falsePositive") is True)]
    denominator = len(classified)
    output = []
    for failure in FAILURE_CLASSES:
        affected = [(provider, case) for provider, case, kind in classified if kind == failure]
        providers = sorted({provider for provider, _ in affected})
        output.append({"failureClass": failure, "affectedProviders": providers,
                       "affectedCases": len(affected),
                       "percentageOfAllFailures": _pct(len(affected), denominator),
                       "severity": "HIGH" if failure == "FALSE_POSITIVE" and affected else "OBSERVED" if affected else "NONE"})
    return sorted(output, key=lambda item: (0 if item["failureClass"] == "FALSE_POSITIVE" and item["affectedCases"] else 1,
                                            -item["affectedCases"], item["failureClass"]))


def _wbd_access_probe() -> dict:
    """One known WBD URL diagnostic, reported separately from HBO/MAX results."""
    url = "https://press.wbd.com/us/media-release/hbo-0/hbo-original-white-lotus-season-4-begins-filming-france"
    try:
        _, final = update.fetch(url, ["press.wbd.com"])
        return {"url": _public_url(url), "status": "FETCHED", "finalUrl": _public_url(final),
                "bypassAttempted": False, "productionRouteUsed": False}
    except (update.AutomationError, OSError, URLError, TimeoutError) as exc:
        reason = str(exc)
        match = re.search(r"HTTP\s+(\d{3})", reason, re.I)
        status = f"HTTP_{match.group(1)}" if match else classify_exception(exc)
        return {"url": _public_url(url), "status": status, "failureType": type(exc).__name__,
                "bypassAttempted": False, "productionRouteUsed": False}


def _fx_control_audit() -> dict:
    """Run the existing bounded FX source-strategy control; never activates FX."""
    try:
        import fx_source_strategy_audit
        report = fx_source_strategy_audit.run()
        return {"status": "MEASURED", "productionActivationRecommended": False,
                "conclusion": report.get("recommendedQualificationStrategy", {}).get("conclusion"),
                "fetchBudget": report.get("fetchBudget"),
                "providerRootAssessment": report.get("providerRootAssessment"),
                "genericDiscoveryAssessment": report.get("genericDiscoveryAssessment"),
                "productionActivationRecommendedFromSource": report.get("productionActivationRecommended"),
                "protectedFileIntegrity": report.get("protectedFileIntegrity")}
    except Exception as exc:
        return {"status": "LOCAL_INCONCLUSIVE", "productionActivationRecommended": False,
                "errorType": type(exc).__name__}


def build_report(corpus: dict, *, token: str | None = None, transport=None,
                 now: dt.datetime | None = None, execution_mode: str = "local") -> dict:
    # Unit-test host environment is not evidence that a coverage audit was run.
    if execution_mode not in ("local", "runner-confirmation"):
        raise ValueError("unsupported audit execution mode")
    is_actions = os.environ.get("GITHUB_ACTIONS", "").casefold() == "true"
    runner_execution = execution_mode == "runner-confirmation"
    if runner_execution and not is_actions:
        raise ValueError("runner-confirmation requires a GitHub Actions execution environment")
    validate_corpus(corpus)
    start = now or dt.datetime.now(dt.timezone.utc)
    protected_before = protected_hashes()
    frozen_before = frozen_hashes()
    specs = discovery.provider_specs()
    wbd_probe = _wbd_access_probe()
    fx_control = _fx_control_audit()
    provider_reports = []
    for provider in corpus["providers"]:
        generic_budget = {"count": 0, "limit": MAX_GENERIC_REQUESTS, "truncated": False}
        diagnostic_budget = {"count": 0, "limit": MAX_DIAGNOSTIC_REQUESTS, "truncated": False}
        provider_fetcher = BoundedFetcher(provider["provider"], transport=transport, budget=generic_budget)
        diagnostic_fetcher = BoundedFetcher(provider["provider"], transport=transport, budget=diagnostic_budget)
        case_rows = []
        for case in provider["cases"]:
            try:
                row = _case_result(provider, case, token=token or "", specs=specs, transport=transport,
                                   provider_fetcher=provider_fetcher, diagnostic_fetcher=diagnostic_fetcher)
            except Exception as exc:
                row = {"caseId": case["caseId"], "provider": provider["provider"], "title": case.get("title"),
                       "kind": case["kind"], "goldReviewStatus": case.get("goldReviewStatus"),
                       "expectedFacts": _public_facts(case.get("expectedFacts")),
                       "expectedFactKnownFields": case.get("expectedFactKnownFields", list(FACT_FIELDS)),
                       "canonicalUrl": _public_url(case.get("canonicalUrl")), "manuallySeeded": case.get("manuallySeeded", False),
                       "rawNetworkLabels": [], "httpStatus": None, "secondaryFailures": [],
                       "denominatorEligible": False, "sourceAccessible": None, "routeValid": None,
                       "genericDiscovery": False, "identityAccepted": False, "lifecycleParsed": False,
                       "reconstructionPassed": False, "covered": False, "primaryFailure": "UNEXPECTED_EXCEPTION",
                       "unexpectedExceptionType": type(exc).__name__}
                row = _case_aliases(row, case)
            case_rows.append(row)
        total_count = generic_budget["count"] + diagnostic_budget["count"]
        total_truncated = generic_budget["truncated"] or diagnostic_budget["truncated"]
        provider_reports.append({**_provider_rollup(provider, case_rows, truncated=total_truncated), "cases": case_rows,
                                 "requestCount": total_count, "requestBudgets": {"generic": generic_budget["count"],
                                                    "genericLimit": generic_budget["limit"],
                                                    "diagnostic": diagnostic_budget["count"],
                                                    "diagnosticLimit": diagnostic_budget["limit"],
                                                    "totalLimit": MAX_PROVIDER_REQUESTS},
                                 "truncated": total_truncated,
                                 "timeoutCount": provider_fetcher.timeouts + diagnostic_fetcher.timeouts,
                                 "requests": provider_fetcher.requests + diagnostic_fetcher.requests})
        provider_reports[-1]["measuredPositiveCases"] = sum(c["kind"] == "positive" and c.get("denominatorEligible") for c in case_rows)
    protected_after = protected_hashes()
    frozen_after = frozen_hashes()
    mutation = [p for p in PROTECTED if protected_before[p] != protected_after[p]]
    frozen_changed = [p for p in FROZEN_MODULES if frozen_before[p] != frozen_after[p]]
    credential_paths = [ROOT / "official-data/automation/provider_coverage_audit.py",
                                          ROOT / "official-data/automation/test_provider_coverage_audit.py",
                                          ROOT / ".github/workflows/provider-coverage-audit.yml", CORPUS]
    credential_matches = credential_scan([path for path in credential_paths if path.is_file()])
    eligible = [r for r in provider_reports if r["productionEnabled"]]
    all_cases = [c for r in provider_reports for c in r["cases"]]
    production_metrics = _aggregate_coverage(eligible)
    all_metrics = _aggregate_coverage(provider_reports)
    negatives = [c for c in all_cases if c["kind"] == "negative" and c.get("falsePositiveDenominatorEligible")]
    corpus_negatives = [c for c in all_cases if c["kind"] == "negative" and c.get("goldReviewStatus") == "VERIFIED"]
    false_positives = sum(bool(c.get("falsePositive")) for c in negatives)
    corpus_invalid_count = sum(c.get("primaryFailure") == "CORPUS_INVALID" for c in all_cases)
    unexpected_count = sum(c.get("primaryFailure") == "UNEXPECTED_EXCEPTION" or bool(c.get("directUnexpectedException")) for c in all_cases)
    quality_valid = (not mutation and not frozen_changed and credential_matches == 0 and
                     not corpus_invalid_count and not unexpected_count and bool(corpus_negatives))
    runner_metadata_measured = any(c.get("freshMetadataFetched") for c in all_cases)
    ranking = _roi(provider_reports) if quality_valid else []
    measured_providers = [r for r in eligible if r["rawE2EPercent"] is not None]
    losses = {
        "sourceAccess": [(r["provider"], sum(c.get("primaryFailure") == "SOURCE_INACCESSIBLE" for c in r["cases"])) for r in eligible],
        "discovery": [(r["provider"], r["counts"]["sourceAccessible"] - r["counts"]["discovered"]) for r in eligible],
        "parser": [(r["provider"], (r["counts"]["discovered"] - r["counts"]["identityAccepted"])
                    + (r["counts"]["identityAccepted"] - r["counts"]["lifecycleParsed"])) for r in eligible],
    }
    def top_loss(name):
        values = [(provider, amount) for provider, amount in losses[name] if amount > 0]
        return max(values, key=lambda x: (x[1], x[0])) if values else None
    top_false_positive = max((r for r in provider_reports if r["falsePositives"] > 0),
                             key=lambda r: (r["falsePositiveRate"], r["falsePositives"], r["provider"]), default=None)
    recommendations = []
    for item in ranking[:3]:
        failure = item["dominantFailure"]
        target_type = ("HIGH_SEVERITY_SAFETY_REVIEW" if failure == "FALSE_POSITIVE" else
                       "ARCHITECTURAL_OR_SOURCE_LIMITATION" if failure == "SOURCE_INACCESSIBLE" else "QUICK_WIN_CANDIDATE")
        recommendations.append({**item,
            "targetType": target_type,
            "problem": failure or "unclassified measured failures",
            "measuredImpact": f"{item['accessibleUncovered']} accessible positive cases uncovered",
            "risk": "False positive requires safety review before any coverage optimization" if failure == "FALSE_POSITIVE" else
                    "Availability or source policy requires review" if failure == "SOURCE_INACCESSIBLE" else "Preserve strict identity and reconstruction gates",
            "maximumPotentialGainCases": item["accessibleUncovered"],
            "recommendedAction": "Review false-positive evidence and parser binding before any production change" if failure == "FALSE_POSITIVE" else
                                "Review bounded source availability evidence" if failure == "SOURCE_INACCESSIBLE" else "Investigate the measured failing stage before changing production behavior"})
    diagnostic_status = "INCONCLUSIVE" if not token or not runner_metadata_measured else "MEASURED"
    authority_confirmed = bool(runner_execution and is_actions and quality_valid
                               and token and runner_metadata_measured)
    if not quality_valid:
        final_status = "AUDIT_INVALID"
    elif authority_confirmed:
        final_status = "GITHUB_RUNNER_CONFIRMATION"
    else:
        final_status = "AWAITING_GITHUB_RUNNER_CONFIRMATION"
    quality_gate = {"schemaValid": True, "noDuplicateCaseIds": True, "firstPartyPositiveCases": True,
                    "independentGoldFacts": True, "directUrlSeparated": True,
                    "manualReviewExcluded": True, "providerSemanticsChanged": False,
                    "falsePositiveCorpusPresent": bool(corpus_negatives),
                    "credentialMatches": credential_matches, "corpusInvalidCases": corpus_invalid_count,
                    "unexpectedExceptionCases": unexpected_count, "valid": quality_valid}
    return {"schemaVersion": 1, "auditVersion": "2.7.14", "reportType": "provider_coverage_audit", "readOnly": True,
            "productionMutation": bool(mutation), "protectedFilesUnchanged": not mutation,
            "protectedChangedFiles": mutation, "frozenModulesUnchanged": not frozen_changed,
            "frozenChangedModules": frozen_changed, "localResultScope": "LOCAL_DIAGNOSTIC_ONLY",
            "resultScope": ("RUNNER_CONFIRMATION" if authority_confirmed else
                            "RUNNER_INCONCLUSIVE" if runner_execution else "LOCAL_DIAGNOSTIC_ONLY"),
            "executionMode": execution_mode, "diagnosticStatus": diagnostic_status,
            "runnerAuthorityConfirmed": authority_confirmed,
            "finalCoverageStatus": final_status, "status": final_status,
            "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "environment": {"python": sys.version, "pythonVersion": platform.python_version(),
                            "runnerOS": platform.platform(), "startedAt": start.isoformat(timespec="seconds"),
                            "endedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                            "githubActions": os.environ.get("GITHUB_ACTIONS", "").casefold() == "true"},
            "corpus": {"schemaVersion": corpus["schemaVersion"], "corpusVersion": corpus["corpusVersion"],
                       "curatedAt": corpus.get("curatedAt"), "curationMethod": corpus.get("curationMethod"),
                       "selectionMethodology": corpus.get("selectionMethodology"),
                       "selectionBias": "PURPOSIVE_MANUALLY_CURATED_REFERENCE_SAMPLE",
                       "independenceStatus": "INDEPENDENT_GOLD_EXPECTATIONS" if all(c.get("manuallyCurated") for c in all_cases if c["kind"] == "positive") else "CORPUS_INDEPENDENCE_LIMITATION",
                       "positiveCases": sum(c["kind"] == "positive" and c.get("goldReviewStatus") == "VERIFIED" for c in all_cases),
                       "negativeCases": len(corpus_negatives), "measuredNegativeCases": len(negatives),
                       "manualReviewCases": sum(c["kind"] == "manual_review" or c.get("goldReviewStatus") == "MANUAL_REVIEW" for c in all_cases)},
            "overall": {"productionOnly": production_metrics,
                        "allAudited": {**all_metrics, "negativeCases": len(negatives),
                                       "falsePositives": false_positives,
                                       "falsePositiveRate": _pct(false_positives, len(negatives))}},
            "metrics": {"PRODUCTION_ONLY": production_metrics,
                       "ALL_AUDITED": {**all_metrics, "negativeCases": len(negatives),
                           "falsePositives": false_positives,
                           "falsePositiveRate": _pct(false_positives, len(negatives))}},
            "rankings": ({"bestCoverage": max(measured_providers, key=lambda r: (r["rawE2EPercent"], r["provider"]), default=None),
                          "lowestCoverage": min(measured_providers, key=lambda r: (r["rawE2EPercent"], r["provider"]), default=None),
                          "highestDiscoveryLoss": top_loss("discovery"), "highestParserLoss": top_loss("parser"),
                          "highestSourceAccessLoss": top_loss("sourceAccess"),
                          "highestFalsePositiveRisk": top_false_positive,
                          "bestV2715QuickWin": next((item for item in recommendations if item["targetType"] == "QUICK_WIN_CANDIDATE"), None)}
                         if quality_valid else {"bestCoverage": None, "lowestCoverage": None,
                                                "highestDiscoveryLoss": None, "highestParserLoss": None,
                                                "highestSourceAccessLoss": None, "highestFalsePositiveRisk": None,
                                                "bestV2715QuickWin": None}),
            "providers": provider_reports, "roiRanking": ranking, "recommendations": recommendations,
            "failureRoiAnalysis": {"PRODUCTION_ONLY": _failure_roi_analysis(eligible),
                                   "ALL_AUDITED": _failure_roi_analysis(provider_reports)},
            "sourceDiagnostics": {"wbdKnownArticleProbe": wbd_probe, "fxSourceStrategyControl": fx_control},
            "auditQuality": quality_gate, "qualityGate": quality_gate}


def render_markdown(report: dict) -> str:
    rows = report["providers"]
    ranking = report.get("rankings", {})
    name = lambda row: row.get("provider") if isinstance(row, dict) else "N/A"
    lines = ["# Provider Coverage Audit", "", f"**Final coverage status:** `{report['finalCoverageStatus']}`",
             "", f"Diagnostic status: `{report.get('diagnosticStatus', 'N/A')}`; execution mode: `{report.get('executionMode', 'local')}`; result scope: `{report.get('resultScope', 'LOCAL_DIAGNOSTIC_ONLY')}`.",
             "", "## Provider overview", "", "| Provider | Corpus | Accessible | Discovered | Covered | Discovery % | Raw E2E % | Accessible E2E % | Direct URL % | False positives | Confidence | Rating |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    fmt = lambda x: "N/A" if x is None else f"{x:.2f}%"
    for r in rows:
        c = r["counts"]
        rating = "CORPUS_TOO_SMALL" if r["corpusTooSmall"] else "MEASURED"
        lines.append(f"| {r['provider']} | {r['corpusCount']} | {c['sourceAccessible']} | {c['discovered']} | {c['covered']} | {fmt(r['discoveryPercent'])} | {fmt(r['rawE2EPercent'])} | {fmt(r['accessibleE2EPercent'])} | {fmt(r['directUrlPercent'])} | {r['falsePositives']} | {r['confidence']} | {rating} |")
    lines += ["", "## Cross-provider rankings", "",
              f"BEST COVERAGE: {name(ranking.get('bestCoverage'))}",
              f"LOWEST COVERAGE: {name(ranking.get('lowestCoverage'))}",
              f"HIGHEST DISCOVERY LOSS: {ranking.get('highestDiscoveryLoss') or 'N/A'}",
              f"HIGHEST PARSER LOSS: {ranking.get('highestParserLoss') or 'N/A'}",
              f"HIGHEST SOURCE ACCESS LOSS: {ranking.get('highestSourceAccessLoss') or 'N/A'}",
              f"HIGHEST FALSE-POSITIVE RISK: {name(ranking.get('highestFalsePositiveRisk'))}",
              f"BEST V2.7.15 QUICK WIN: {name(ranking.get('bestV2715QuickWin'))}",
              "", "## Provider funnels", ""]
    for r in rows:
        lines.append(f"**{r['provider']}**: " + " â†’ ".join(str(n) for n in r["funnel"]) + f"  ")
        lines.append("Failures: " + (", ".join(f"{k}={v}" for k, v in sorted(r["failureBreakdown"].items())) or "none measured") + ".")
    lines += ["", "## ROI ranking", "", "Safety priority: any observed false positive ranks first for high-severity safety review. Other providers rank by accessible uncovered cases Ã— dominant positive-failure share among measured positive failures.", ""]
    ranking = report["roiRanking"]
    if ranking:
        lines.extend(f"{i}. {r['provider']}: score {r['score']} ({r['dominantFailure']}, {r['accessibleUncovered']} accessible uncovered)" for i, r in enumerate(ranking[:3], 1))
    else:
        lines.append("No measured production quick win can be ranked from these data.")
    lines += ["", "## Recommended V2.7.15 investigation targets", ""]
    if report.get("recommendations"):
        for i, rec in enumerate(report["recommendations"], 1):
            lines += [f"{i}. **{rec['provider']} â€” {rec['targetType']}**",
                      f"   Problem: {rec['problem']}. Measured impact: {rec['measuredImpact']}.",
                      f"   Risk: {rec['risk']}. Maximum possible gain: {rec['maximumPotentialGainCases']} cases.",
                      f"   Next action: {rec['recommendedAction']}."]
    else:
        lines.append("No measured recommendation is available yet.")
    lines += ["", "## Scope and quality", "", f"Corpus independence: `{report['corpus']['independenceStatus']}`. Manual review cases: {report['corpus']['manualReviewCases']}.", f"Protected files unchanged: `{report['protectedFilesUnchanged']}`. Local scope: `{report['localResultScope']}`.", "", "Local results do not establish GitHub runner access or final provider coverage."]
    return "\n".join(lines) + "\n"


def write_report(report: dict, output_dir: Path) -> tuple[Path, Path, Path]:
    # Final workflow safety checks can invalidate a previously measured report.
    if not report.get("qualityGate", report.get("auditQuality", {})).get("valid", False):
        report["runnerAuthorityConfirmed"] = False
        if report.get("resultScope") == "RUNNER_CONFIRMATION":
            report["resultScope"] = "RUNNER_INCONCLUSIVE"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "provider-coverage-audit.json"
    md_path = output_dir / "provider-coverage-audit.md"
    jsonl_path = output_dir / "provider-coverage-cases.jsonl"
    json_text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    md_text = render_markdown(report)
    case_lines = [json.dumps({"provider": provider["provider"], **case}, ensure_ascii=False, sort_keys=True)
                  for provider in report["providers"] for case in provider["cases"]]
    jsonl_text = "\n".join(case_lines) + ("\n" if case_lines else "")
    output_matches = credential_scan_texts([json_text, md_text, jsonl_text])
    if output_matches:
        gate = report.get("qualityGate", report.get("auditQuality", {}))
        gate["credentialMatches"] = gate.get("credentialMatches", 0) + output_matches
        gate["valid"] = False
        report["auditQuality"] = report["qualityGate"] = gate
        report["finalCoverageStatus"] = report["status"] = "AUDIT_INVALID"
        report["runnerAuthorityConfirmed"] = False
        if report.get("resultScope") == "RUNNER_CONFIRMATION":
            report["resultScope"] = "RUNNER_INCONCLUSIVE"
        report["rankings"] = {key: None for key in report.get("rankings", {})}
        report["roiRanking"] = []
        report["recommendations"] = []
        json_text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        md_text = render_markdown(report)
        case_lines = [json.dumps({"provider": provider["provider"], **case}, ensure_ascii=False, sort_keys=True)
                      for provider in report["providers"] for case in provider["cases"]]
        jsonl_text = "\n".join(case_lines) + ("\n" if case_lines else "")
        # Suppress suspicious output bytes while retaining a clear invalid report.
        for pattern in _CREDENTIAL_PATTERNS:
            json_text = pattern.sub(lambda match: "[REDACTED]", json_text)
            md_text = pattern.sub(lambda match: "[REDACTED]", md_text)
            jsonl_text = pattern.sub(lambda match: "[REDACTED]", jsonl_text)
    json_path.write_text(json_text, encoding="utf-8")
    md_path.write_text(md_text, encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(jsonl_text)
    return json_path, md_path, jsonl_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--validate-corpus", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--execution-mode", choices=("local", "runner-confirmation"), default="local",
                        help="Explicit audit authority mode; tests and local calls default to local")
    args = parser.parse_args(argv)
    try:
        corpus = load_corpus(args.corpus)
        if args.validate_corpus:
            print(json.dumps({"valid": True, "providers": len(corpus["providers"]),
                              "cases": sum(len(p["cases"]) for p in corpus["providers"])}))
            return 0
        output_dir = args.output_dir
        if output_dir is None:
            raise ValueError("--output-dir is required for an audit run")
        token = discovery.local_tmdb_token()
        report = build_report(corpus, token=token, execution_mode=args.execution_mode)
        write_report(report, output_dir)
        print(render_markdown(report), end="")
        return 0 if report["auditQuality"]["valid"] else 2
    except (CorpusError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Provider coverage audit error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
