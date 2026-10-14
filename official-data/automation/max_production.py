"""MAX-only production wiring for the qualified Max Press Spain source.

The module uses generic first-party index discovery and the frozen MAX Spanish
parser. Qualification cases, known URLs and qualification output are not read.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import discovery
import hbo_max_discovery as generic
import hbo_max_qualification as qualified
import max_qualification_parser
import update
import wbd_hbo_max_source_audit as identity_audit

HOST = "press-spain.max.com"
SOURCE = "Max Press Spain"
MAX_FETCHES = 120


def strict_network(metadata: dict) -> bool:
    """Only exact Max/HBO Max network labels route to MAX."""
    labels = [item.get("name") if isinstance(item, dict) else item
              for item in metadata.get("networks") or []]
    allowed = {"max", "hbo max"}
    return bool(labels) and all(isinstance(label, str) and label.strip().casefold() in allowed
                                for label in labels)


def identity_context(metadata: dict) -> dict:
    if not strict_network(metadata):
        raise update.AutomationError("MAX requires strict unambiguous network identity")
    return {key: metadata.get(key) for key in ("tmdbId", "title", "originalName", "networks")}


def aliases(metadata: dict) -> list[str]:
    return discovery.metadata_names(metadata)


def validate_context(row: dict) -> list[str]:
    context = row.get("providerIdentity")
    if (discovery.provider_specs().get("MAX", {}).get("productionActivated") is not True
            or row.get("provider") != "MAX" or not isinstance(context, dict)
            or not strict_network(context) or context.get("tmdbId") != row.get("tmdbId")
            or context.get("title") != row.get("title")):
        raise update.AutomationError("Missing or inconsistent MAX network proof")
    return aliases(context)


def canonical(url: str) -> str | None:
    return generic.canonical(url)


def fetch(url: str, domains: list[str]) -> tuple[str, str]:
    if domains != [HOST] or not canonical(url):
        raise update.AutomationError("MAX source policy rejected URL")
    result, raw = qualified.fetch(url, domains)
    final = result.get("finalUrl") or url
    if not raw or result.get("status") != "fetched":
        raise update.ProviderUnavailable(result.get("failureCategory") or "MAX source unavailable")
    if not canonical(final):
        raise update.AutomationError("MAX redirect rejected")
    return raw, final


def parse(raw: str, url: str) -> dict:
    if not canonical(url) or not urlsplit(url).path.startswith("/post/"):
        raise update.AutomationError("MAX requires an exact first-party article")
    article = qualified.article(raw, url)
    if (article.get("sourceName") != SOURCE or article.get("language") != "es"
            or article.get("territory") != "ES"):
        raise update.AutomationError("MAX article language/source policy failed")
    return article


def original_brand(article: dict, entry: dict) -> list[str]:
    return qualified.original_brand(article, {**entry, "qualificationRoute": "MAX"})


def validate_article(raw: str, requested: str, final: str, entry: dict) -> dict:
    if not canonical(requested) or not canonical(final) or not urlsplit(final).path.startswith("/post/"):
        raise update.AutomationError("MAX article URL rejected")
    identity = identity_audit.identity(raw, requested, final, {"aliases": entry["aliases"]}, [HOST])
    article = parse(raw, final)
    brands = original_brand(article, entry)
    if not identity["acceptedForAudit"] or brands != ["MAX"]:
        raise update.AutomationError("MAX article title/canonical/provider identity rejected")
    return identity


def detect(article: dict, entry: dict, today, **kwargs) -> list[dict]:
    # Independent replay receives a bounded excerpt without language fields.
    # Promotion validates the persisted MAX network context before invoking
    # this detector; the fixed source policy supplies Spanish/ES defaults.
    if (article.get("sourceName") != SOURCE or not canonical(article.get("url", ""))
            or not urlsplit(article.get("url", "")).path.startswith("/post/")
            or article.get("language", "es") != "es" or article.get("territory", "ES") != "ES"):
        raise update.AutomationError("MAX evidence provenance rejected")
    article = {**article, "language": "es", "territory": "ES"}
    return max_qualification_parser.detect(article, entry, today, **kwargs)


def regional_dates(article: dict, entry: dict) -> list[dict]:
    scoped, parser_entry = max_qualification_parser.scoped(article, entry)
    return max_qualification_parser.regional_dates(scoped, parser_entry)


def discover(metadata: dict, spec: dict, report: dict, fetcher) -> dict:
    if not strict_network(metadata) or spec.get("productionActivated") is not True:
        report.update(result="insufficient_evidence", reason="ambiguous_provider")
        return report
    if spec.get("candidateDomains") != [HOST] or spec.get("officialDomains") != [HOST]:
        raise update.AutomationError("Invalid MAX source-domain policy")

    names = aliases(metadata)
    entry = {"tmdbId": metadata["tmdbId"], "title": metadata["title"], "aliases": names}
    count = 0
    cache = {}
    unavailable = False
    transport = fetch if fetcher is update.fetch else fetcher

    def get(url):
        nonlocal count, unavailable
        if url in cache:
            return cache[url]
        if count >= MAX_FETCHES or not canonical(url):
            return {"failureCategory": "MAX_BUDGET_OR_SOURCE_REJECTED"}, None
        count += 1
        report["attemptedOfficialUrls"].append(url)
        try:
            raw, final = transport(url, [HOST])
            if not canonical(final):
                raise update.AutomationError("MAX redirect rejected")
            update.parse_page(raw)
            result = ({"status": "fetched", "finalUrl": final}, raw)
        except (update.AutomationError, ValueError, OSError) as exc:
            unavailable = unavailable or isinstance(exc, update.ProviderUnavailable)
            result = ({"failureCategory": discovery.safe_failure(exc)}, None)
        cache[url] = result
        return result

    articles, diagnostic = generic.crawl(get, names)
    selected, _ = generic.candidates(articles, names, "MAX")
    for candidate in selected:
        requested = candidate["url"]
        result, raw = get(requested)
        if not raw:
            continue
        final = result["finalUrl"]
        try:
            validate_article(raw, requested, final, entry)
        except (update.AutomationError, ValueError):
            report["rejectedCandidates"].append(
                {"url": requested, "reasons": ["MAX_article_identity_or_brand_rejected"]})
            continue
        report["candidateUrls"].append(final)
        path = candidate.get("path") or []
        report["candidates"].append({
            "candidateUrl": final, "candidateDomain": HOST, "factualParserEligible": True,
            "identityValidated": True, "evidenceLevel": "STRONG", "providerRoutingConfidence": "NETWORK",
            "sourceIdentityConfidence": "CONFIRMED", "pageType": "official_article",
            "releaseStructure": True, "discoveryMethod": "DISCOVERED_FROM_FIRST_PARTY_INDEX",
            "discoveryProvenance": "max_first_party_index", "discoveryRoot": path[0] if path else None,
            "discoverySource": path, "knownUrlUsedAsSeed": False,
            "sourceAuthority": "official_provider_announcement", "requestedUrl": requested,
            "finalUrl": final, "redirectWithinOfficialBoundary": True, "reasons": []})
    report["discoveryDiagnostics"] = diagnostic
    report["fetchCount"] = count
    if report["candidates"]:
        report.update(result="candidate_found", evidenceLevel="STRONG")
    elif unavailable:
        report.update(result="provider_unavailable", reason="official_provider_access_failed")
    else:
        report["reason"] = "no_valid_official_candidate"
    return report
