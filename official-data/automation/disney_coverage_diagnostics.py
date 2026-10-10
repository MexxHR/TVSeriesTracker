"""Read-only, bounded Disney+ discovery coverage diagnostics.

The existing monitored/production discovery and parser are always run first.
The deeper sitemap pass is a separate diagnostic observation and its URLs are
never supplied to monitored processing or used as factual evidence.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import discovery
import monitored
import promotion
import update


ROOT = Path(__file__).resolve().parents[2]
CASES = (103540, 138503, 138502)
PROTECTED = (
    "official-data/official_series_data.json",
    "official-data/history/changes.jsonl",
    "official-data/sources.json",
    "official-data/monitored_series.json",
    "official-data/history/monitored_changes.jsonl",
)
FACT_FIELDS = ("status", "nextSeasonNumber", "releaseDate", "releaseYear",
               "sourceName", "sourceUrl", "announcementDate")
DIAGNOSTIC_LIMITS = {
    "maxSitemapBytes": 2_000_000,
    "maxSitemapChildren": 16,
    "maxEntriesPerSitemap": 10_000,
    "maxRelevantUrlsRecorded": 200,
    "maxArticleFetches": 12,
    "maxTotalFetches": 21,
}
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def protected_hashes(root: Path = ROOT) -> dict[str, str]:
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in PROTECTED}


def safe_http_failure(exc: Exception) -> str:
    match = re.search(r"\bHTTP (\d{3})\b", str(exc))
    if match:
        return f"HTTP {match.group(1)}"
    if "too large" in str(exc).lower():
        return "response_too_large"
    return type(exc).__name__


def _metadata_projection(meta: dict) -> dict:
    return {key: meta.get(key) for key in
            ("tmdbId", "title", "originalName", "networks", "productionCompanies")}


def _safe_url(url: str, domains: list[str]) -> bool:
    return isinstance(url, str) and update.allowed(url, domains)


def _parse_sitemap(raw: str) -> tuple[ET.Element | None, str]:
    if len(raw.encode("utf-8")) > DIAGNOSTIC_LIMITS["maxSitemapBytes"]:
        return None, "sitemap_too_large"
    if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        return None, "doctype_or_entity_rejected"
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None, "xml_parse_failed"
    if root.tag not in (SITEMAP_NS + "urlset", SITEMAP_NS + "sitemapindex"):
        return root, "unsupported_sitemap_root"
    return root, "parseable"


def _production_sitemap_surface(captures: list[dict], names: list[str]) -> list[dict]:
    """Describe the production XML boundary using its current limits/parser."""
    surfaces = []
    domains = discovery.provider_specs()["DISNEY_PLUS"]["candidateDomains"]
    for capture in captures:
        url = capture.get("finalUrl") or capture.get("requestedUrl") or ""
        raw = capture.get("raw")
        if not urlparse(url).path.casefold().endswith(".xml"):
            continue
        item = {"url": url, "fetchResult": capture.get("fetchResult"),
                "retrieved": capture.get("fetchResult") == "success",
                "finalDomain": capture.get("finalDomain"),
                "finalDomainApproved": capture.get("domainApproved", False),
                "productionParseResult": "not_attempted", "relevantEntriesExamined": 0,
                "candidateUrlsSelected": 0}
        if capture.get("fetchResult") != "success" or not isinstance(raw, str):
            surfaces.append(item)
            continue
        if len(raw.encode("utf-8")) > discovery.MAX_SITEMAP_BYTES:
            item["productionParseResult"] = "sitemap_too_large"
        elif "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
            item["productionParseResult"] = "doctype_or_entity_rejected"
        else:
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                item["productionParseResult"] = "xml_parse_failed"
            else:
                if root.tag == SITEMAP_NS + "sitemapindex":
                    entries = root.findall(SITEMAP_NS + "sitemap")
                    item["totalEntries"] = len(entries)
                    item["entriesExamined"] = 0 if len(entries) > discovery.MAX_SITEMAP_CHILDREN else len(entries)
                    item["productionParseResult"] = ("child_sitemap_limit_exceeded" if
                        len(entries) > discovery.MAX_SITEMAP_CHILDREN else "parseable")
                elif root.tag == SITEMAP_NS + "urlset":
                    entries = root.findall(SITEMAP_NS + "url")
                    item["totalEntries"] = len(entries)
                    if len(entries) > discovery.MAX_SITEMAP_URLS:
                        item["entriesExamined"] = 0
                        item["productionParseResult"] = "url_entry_limit_exceeded"
                    else:
                        _, article_urls = discovery._disney_sitemap_links(raw, domains, names)
                        item["entriesExamined"] = len(entries)
                        item["productionParseResult"] = "parseable"
                        item["candidateUrlsSelected"] = len(article_urls)
                else:
                    item["productionParseResult"] = "unsupported_sitemap_root"
        surfaces.append(item)
    return surfaces


def _slug_match(url: str, names: list[str]) -> dict:
    path = urlparse(url).path.casefold()
    slug = path.rstrip("/").rsplit("/", 1)[-1]
    normalized = discovery.normalize(slug.replace("-", " "))
    matches = [name for name in names if discovery._title_path_match(url, [name])]
    exact_matches = [name for name in names if
                     re.search(rf"(?<![a-z0-9]){re.escape(discovery.normalize(name))}(?![a-z0-9])", normalized)]
    return {"slug": slug, "normalizedSlug": normalized,
            "matchedNames": matches, "titleSlugMatch": bool(matches),
            "exactTitlePhraseMatch": bool(exact_matches),
            "exactMatchedNames": exact_matches}


def _capture_production(meta: dict, trusted: dict, fetcher, captures: list[dict]) -> dict:
    specs = discovery.provider_specs()
    spec = specs["DISNEY_PLUS"]
    domains = spec["candidateDomains"]

    def capture(url: str, allowed_domains: list[str], headers=None):
        if not _safe_url(url, allowed_domains):
            raise update.AutomationError("Source outside allowlist")
        try:
            raw, final = fetcher(url, allowed_domains, headers)
            captures.append({"stage": "production_discovery", "requestedUrl": url, "finalUrl": final,
                             "finalDomain": urlparse(final).hostname,
                             "domainApproved": _safe_url(final, allowed_domains),
                             "fetchResult": "success", "raw": raw})
            return raw, final
        except Exception as exc:
            captures.append({"stage": "production_discovery", "requestedUrl": url, "finalUrl": None,
                             "finalDomain": None, "domainApproved": False,
                             "fetchResult": safe_http_failure(exc)})
            raise

    result = discovery.discover(meta, trusted, fetcher=capture, specs=specs)
    return result


def _production_discoverer(captures: list[dict], fetcher, result_holder: dict):
    def run(meta: dict, trusted: dict) -> dict:
        result = _capture_production(meta, trusted, fetcher, captures)
        result_holder.update(result)
        return result
    return run


def _deep_sitemap_inspection(meta: dict, captures: list[dict], fetcher) -> dict:
    specs = discovery.provider_specs()
    spec = specs["DISNEY_PLUS"]
    domains = spec["candidateDomains"]
    names = discovery.metadata_names(meta)
    seeds = list(dict.fromkeys(spec["seeds"]))[:discovery.MAX_SEEDS]
    report = {"status": "not_run", "attemptedOfficialUrls": seeds,
              "fetches": [], "sitemaps": [], "relevantUrls": [], "filteredUrls": [],
              "limits": dict(DIAGNOSTIC_LIMITS),
              "productionBounds": {
                  "maxSeeds": discovery.MAX_SEEDS,
                  "maxSitemapBytes": discovery.MAX_SITEMAP_BYTES,
                  "maxSitemapChildren": discovery.MAX_SITEMAP_CHILDREN,
                  "maxEntriesPerSitemap": discovery.MAX_SITEMAP_URLS,
                  "maxArticleUrls": discovery.MAX_SITEMAP_ARTICLES,
                  "fetchBudget": 1 + discovery.MAX_SITEMAP_CHILDREN + discovery.MAX_SITEMAP_ARTICLES,
              }}
    queue = [(url, None, 0, 0) for url in seeds]
    seen: set[str] = set()
    sitemap_children = 0
    skipped_child_sitemaps = 0
    article_fetches = 0
    article_urls_queued = 0
    skipped_article_urls = 0
    relevant_url_count = 0
    filtered_url_count = 0
    seen_relevant_urls: set[str] = set()
    diagnostic_article_checks = []
    stop_reason = None
    while queue and len(seen) < DIAGNOSTIC_LIMITS["maxTotalFetches"]:
        url, parent, depth, child_index = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        record = {"url": url, "parentUrl": parent, "depth": depth,
                  "sitemapChildIndex": child_index,
                  "firstPartyApproved": _safe_url(url, domains)}
        if not record["firstPartyApproved"]:
            record["fetchResult"] = "rejected_unapproved_url"
            report["fetches"].append(record)
            continue
        try:
            raw, final = fetcher(url, domains)
            record.update(finalUrl=final, finalDomain=urlparse(final).hostname,
                          finalDomainApproved=_safe_url(final, domains), fetchResult="success")
        except (update.AutomationError, OSError, UnicodeError, ValueError, TimeoutError) as exc:
            record.update(finalUrl=None, finalDomain=None, finalDomainApproved=False,
                          fetchResult=safe_http_failure(exc))
            report["fetches"].append(record)
            continue
        report["fetches"].append(record)
        if not record["finalDomainApproved"]:
            record["fetchResult"] = "redirect_outside_allowlist"
            continue
        is_xml = urlparse(final).path.casefold().endswith(".xml")
        if not is_xml:
            article_fetches += 1
            try:
                page = update.parse_page(raw)
                heading = page.heading.strip() or page.title.strip()
                discovery_identity = (discovery.branded_index_identity(page, names, "DISNEY_PLUS") or
                                      discovery.article_identity(page, names) or
                                      discovery.announcement_article_identity(page, names))
                parser_identity = monitored.official_article_heading_identity(page, names)
                structure = discovery._announcement_structure(final, page, "DISNEY_PLUS")
                diagnostic_article_checks.append({"url": final,
                    "diagnosticOnlyObservation": not any(c.get("requestedUrl") == url
                        for c in captures if c.get("stage") == "production_discovery"),
                    "requestedTitle": meta.get("title"), "headline": heading,
                    "normalizedHeadline": discovery.normalize(heading),
                    "seriesIdentityConfirmed": discovery_identity,
                    "discoveryIdentityConfirmed": discovery_identity,
                    "parserHeadlineIdentityConfirmed": parser_identity,
                    "articleStructureConfirmed": structure,
                    "diagnosticParserRun": False,
                    "passedToMonitoredInput": False})
            except (update.AutomationError, ValueError) as exc:
                diagnostic_article_checks.append({"url": final,
                    "diagnosticOnlyObservation": True,
                    "identityCheckResult": "page_parse_failed",
                    "structureCheckResult": "not_applicable",
                    "errorType": type(exc).__name__,
                    "diagnosticParserRun": False,
                    "passedToMonitoredInput": False})
            continue
        root, parse_result = _parse_sitemap(raw)
        sitemap_record = {"url": final, "parentUrl": parent,
                          "parseResult": parse_result, "entriesExamined": 0,
                          "relevantUrls": [], "truncated": False}
        report["sitemaps"].append(sitemap_record)
        if root is None or parse_result != "parseable":
            continue
        if root.tag == SITEMAP_NS + "sitemapindex":
            children = root.findall(SITEMAP_NS + "sitemap")
            sitemap_record["entriesExamined"] = min(len(children), DIAGNOSTIC_LIMITS["maxSitemapChildren"])
            sitemap_record["truncated"] = len(children) > DIAGNOSTIC_LIMITS["maxSitemapChildren"]
            for child_index, node in enumerate(children[:DIAGNOSTIC_LIMITS["maxSitemapChildren"]], start=1):
                loc = node.findtext(SITEMAP_NS + "loc")
                pending_urls = {row[0] for row in queue}
                if loc and _safe_url(loc, domains) and urlparse(loc).path.casefold().endswith(".xml"):
                    if (sitemap_children < DIAGNOSTIC_LIMITS["maxSitemapChildren"] and
                            loc not in seen and loc not in pending_urls):
                        queue.append((loc, final, depth + 1, child_index))
                        sitemap_children += 1
                    elif loc not in seen and loc not in pending_urls:
                        skipped_child_sitemaps += 1
            continue
        entries = root.findall(SITEMAP_NS + "url")
        ceiling = DIAGNOSTIC_LIMITS["maxEntriesPerSitemap"]
        sitemap_record["entriesExamined"] = min(len(entries), ceiling)
        sitemap_record["truncated"] = len(entries) > ceiling
        relevant_index = 0
        production_matching_index = 0
        seen_relevant_urls: set[str] = set()
        seen_production_urls: set[str] = set()
        for index, node in enumerate(entries[:ceiling], start=1):
            loc = node.findtext(SITEMAP_NS + "loc")
            if not loc or not _safe_url(loc, domains):
                continue
            match = _slug_match(loc, names)
            production_article_link = discovery._disney_article_link(loc, domains, names)
            if production_article_link and loc not in seen_production_urls:
                production_matching_index += 1
                seen_production_urls.add(loc)
            # Only inspect Disney Press news articles; no external or generic URLs.
            if match["titleSlugMatch"] and not urlparse(loc).path.casefold().startswith("/news/"):
                filtered_url_count += 1
                if len(report["filteredUrls"]) < DIAGNOSTIC_LIMITS["maxRelevantUrlsRecorded"]:
                    report["filteredUrls"].append({"url": loc, "entryIndex": index,
                        "parentUrl": final, "foundBy": "diagnostic_only_sitemap_scan",
                        "productionArticleLinkMatch": False,
                        "productionSitemapEntryExamined": index <= discovery.MAX_SITEMAP_URLS,
                        "excludedBeforeArticleFetch": True,
                        "exclusionReason": "production_article_path_filter",
                        **match})
            if urlparse(loc).path.casefold().startswith("/news/") and match["titleSlugMatch"]:
                is_new_relevant_url = loc not in seen_relevant_urls
                if is_new_relevant_url:
                    relevant_index += 1
                    seen_relevant_urls.add(loc)
                    relevant_url_count += 1
                found = {"url": loc, "parentUrl": final, "entryIndex": index,
                         "matchingUrlIndex": relevant_index,
                         "productionMatchingUrlIndex": production_matching_index if production_article_link else None,
                         "depth": depth, "sitemapChildIndex": child_index,
                         "productionArticleLinkMatch": production_article_link,
                         "foundBy": "diagnostic_only_sitemap_scan", **match,
                         "productionConsidered": any(c.get("requestedUrl") == loc for c in captures),
                         "fetchedForProduction": any(c.get("requestedUrl") == loc and c.get("fetchResult") == "success" for c in captures)}
                if is_new_relevant_url:
                    if len(sitemap_record["relevantUrls"]) < DIAGNOSTIC_LIMITS["maxRelevantUrlsRecorded"]:
                        sitemap_record["relevantUrls"].append(found)
                if is_new_relevant_url:
                    if len(report["relevantUrls"]) < DIAGNOSTIC_LIMITS["maxRelevantUrlsRecorded"]:
                        report["relevantUrls"].append(found)
                if is_new_relevant_url:
                    pending_urls = {row[0] for row in queue}
                    if article_urls_queued < DIAGNOSTIC_LIMITS["maxArticleFetches"] and \
                            loc not in seen and loc not in pending_urls:
                        queue.append((loc, final, depth + 1, 0))
                        article_urls_queued += 1
                    else:
                        skipped_article_urls += 1
        if relevant_url_count > discovery.MAX_SITEMAP_ARTICLES:
            report["matchingUrlsBeyondProductionCutoff"] = True
    if queue and len(seen) >= DIAGNOSTIC_LIMITS["maxTotalFetches"]:
        stop_reason = "max_total_fetches_reached"
    parse_results = [item["parseResult"] for item in report["sitemaps"]]
    if not parse_results:
        report["status"] = "sitemap_unavailable"
    elif not any(value == "parseable" for value in parse_results):
        report["status"] = "sitemap_unparseable"
    elif stop_reason or any(item["truncated"] for item in report["sitemaps"]):
        report["status"] = "bounded_partial_scan"
    else:
        report["status"] = "completed"
    report["stopReason"] = stop_reason
    report["pendingFetchCount"] = len(queue)
    report["parseFailures"] = [item for item in report["sitemaps"]
                               if item["parseResult"] != "parseable"]
    report["articleChecks"] = diagnostic_article_checks
    report["skippedRelevantArticleUrls"] = skipped_article_urls
    report["skippedChildSitemaps"] = skipped_child_sitemaps
    report["articleFetchesPerformed"] = article_fetches
    report["relevantUrlCount"] = relevant_url_count
    report["relevantUrlsTruncated"] = relevant_url_count > len(report["relevantUrls"])
    report["filteredUrlCount"] = filtered_url_count
    report["filteredUrlsTruncated"] = filtered_url_count > len(report["filteredUrls"])
    # Mark production-path inventory and source URL stage distinctions.
    production_captures = [item for item in captures if item.get("stage") == "production_discovery"]
    production_requested = {item.get("requestedUrl") for item in production_captures}
    production_ok = {item.get("requestedUrl") for item in production_captures if item.get("fetchResult") == "success"}
    for item in report["relevantUrls"]:
        item["productionConsidered"] = item["url"] in production_requested
        item["fetchedForProduction"] = item["url"] in production_ok
        item["diagnosticOnlyObservation"] = not item["productionConsidered"]
        item["positionOutsideProductionArticleLimit"] = (
            (item["productionArticleLinkMatch"] and
             item["productionMatchingUrlIndex"] > discovery.MAX_SITEMAP_ARTICLES) or
            item["entryIndex"] > discovery.MAX_SITEMAP_URLS or
            item["sitemapChildIndex"] > discovery.MAX_SITEMAP_CHILDREN)
        if item["fetchedForProduction"]:
            item["stage"] = "fetched_by_production"
        elif item["productionConsidered"]:
            item["stage"] = "production_fetch_failed_or_not_completed"
        else:
            item["stage"] = "diagnostic_only_url_not_selected_by_production"
    report["matchingUrlsBeyondProductionCutoff"] = any(
        item["positionOutsideProductionArticleLimit"] for item in report["relevantUrls"])
    return report


def _captured_page_checks(meta: dict, captures: list[dict], production: dict) -> tuple[list[dict], list[dict], list[dict]]:
    names = discovery.metadata_names(meta)
    identities, structures, url_stages = [], [], []
    candidates = {item.get("candidateUrl"): item for item in production.get("candidates", [])}
    rejected = {item.get("url"): item for item in production.get("rejectedCandidates", [])}
    for capture in captures:
        if capture.get("stage") != "production_discovery":
            continue
        url = capture.get("finalUrl") or capture.get("requestedUrl")
        stage = {"url": url, "requestedUrl": capture.get("requestedUrl"),
                 "foundBy": "production_discovery", "productionConsidered": True,
                 "excludedBeforeFetch": False, "fetched": capture.get("fetchResult") == "success",
                 "fetchResult": capture.get("fetchResult"), "finalDomain": capture.get("finalDomain"),
                 "finalDomainApproved": capture.get("domainApproved", False)}
        reasonrow = rejected.get(url) or rejected.get(capture.get("requestedUrl"))
        if reasonrow:
            stage["rejectionReasons"] = reasonrow.get("reasons", [])
        url_stages.append(stage)
        if capture.get("fetchResult") != "success" or not isinstance(capture.get("raw"), str):
            continue
        if not urlparse(url).path.casefold().startswith("/news/"):
            continue
        try:
            page = update.parse_page(capture["raw"])
            heading = page.heading.strip() or page.title.strip()
            discovery_identity = (discovery.branded_index_identity(page, names, "DISNEY_PLUS") or
                                  discovery.article_identity(page, names) or
                                  discovery.announcement_article_identity(page, names))
            parser_identity = monitored.official_article_heading_identity(page, names)
            structure = discovery._announcement_structure(url, page, "DISNEY_PLUS")
            candidate = candidates.get(url) or candidates.get(capture.get("requestedUrl")) or {}
            reasons = candidate.get("reasons") or (reasonrow or {}).get("reasons", [])
            identities.append({"url": url, "requestedTitle": meta.get("title"),
                               "requestedOriginalTitle": meta.get("originalName"),
                               "normalizedTitle": discovery.normalize(meta.get("title", "")),
                               "normalizedOriginalTitle": discovery.normalize(meta.get("originalName") or ""),
                               "headline": heading, "normalizedHeadline": discovery.normalize(heading),
                               "slug": urlparse(url).path.rstrip("/").rsplit("/", 1)[-1],
                               "identityMatchedName": candidate.get("identityMatchedName"),
                               "seriesIdentityConfirmed": discovery_identity,
                               "discoveryIdentityConfirmed": discovery_identity,
                               "parserHeadlineIdentityConfirmed": parser_identity,
                               "titlePhraseMatch": any(re.search(
                                   rf"(?<![a-z0-9]){re.escape(discovery.normalize(name))}(?![a-z0-9])",
                                   discovery.normalize(heading)) for name in names),
                               "announcementHeadlineCue": any(term in discovery.normalize(heading) for term in
                                   ("season", "renewed", "renewal", "final season", "premieres", "premiere", "returns", "returning", "debut")),
                               "normalizationNotes": "ASCII casefold; NFKD diacritics; curly apostrophes normalized; punctuation collapsed",
                               "rejectionReasons": [reason for reason in reasons if "identity" in reason]})
            structures.append({"url": url, "articleStructureConfirmed": structure,
                               "headlineBodyStructure": {"headlinePresent": bool(heading),
                                                         "bodyLength": len(discovery.normalize("".join(page.parts[:300]))),
                                                         "announcementTermPresent": structure,
                                                         "announcementTermsMatched": [term for term in
                                                             ("season", "renewed", "premiere", "premieres", "final")
                                                             if term in discovery.normalize("".join(page.parts[:300]))]},
                               "sourceAuthority": candidate.get("sourceAuthority", "official_provider_announcement"),
                               "evidenceLevel": candidate.get("evidenceLevel", "INSUFFICIENT"),
                               "factualParserEligible": candidate.get("factualParserEligible", False),
                               "rejectionReasons": [reason for reason in reasons if "structure" in reason]})
        except (update.AutomationError, ValueError) as exc:
            identities.append({"url": url, "checkResult": "page_parse_failed",
                               "errorType": type(exc).__name__})
    return identities, structures, url_stages


def classify_case(case: dict) -> str:
    routing = case.get("routing") or {}
    if routing.get("provider") != "DISNEY_PLUS":
        return "ROUTING_NOT_DISNEY_PLUS"
    pipeline = case.get("pipeline") or {}
    state = pipeline.get("pipelineState")
    if state == "VERIFIED_FACTS":
        reconstruction = case.get("independentReconstruction", {}).get("result")
        if reconstruction == "failed":
            return "INDEPENDENT_RECONSTRUCTION_MISMATCH"
        return "PRODUCTION_PATH_VERIFIED"
    if state == "NO_VERIFIED_FACTS":
        return "PARSER_NO_FACTS"
    if state == "VALIDATION_FAILED":
        return "VALIDATION_FAILED"
    if state == "DISCOVERY_INSUFFICIENT":
        sitemap_surfaces = (case.get("productionDiscovery") or {}).get("sitemaps") or []
        if any(item.get("fetchResult") != "success" for item in sitemap_surfaces):
            return "OFFICIAL_FETCH_FAILED"
        if any(item.get("productionParseResult") not in ("parseable", "not_attempted")
               for item in sitemap_surfaces):
            return "PRODUCTION_SITEMAP_LIMIT_OR_PARSE_FAILURE"
        ident = case.get("identityChecks", [])
        structure = case.get("structureChecks", [])
        if any(x.get("seriesIdentityConfirmed") is False for x in ident):
            return "IDENTITY_BINDING_REJECTED"
        if any(x.get("articleStructureConfirmed") is False for x in structure):
            return "ARTICLE_STRUCTURE_REJECTED"
        diagnostic = case.get("diagnosticDiscovery") or {}
        diagnostic_checks = diagnostic.get("articleChecks") or []
        if diagnostic.get("relevantUrls"):
            if any(item.get("positionOutsideProductionArticleLimit") for item in diagnostic["relevantUrls"]):
                return "RELEVANT_URL_OUTSIDE_PRODUCTION_BOUNDS"
            if any(item.get("productionConsidered") is False and
                   item.get("productionArticleLinkMatch") is False
                   for item in diagnostic["relevantUrls"]):
                return "URL_FILTERED_BEFORE_FETCH"
            if any(item.get("productionConsidered") is False for item in diagnostic["relevantUrls"]):
                return "URL_PRESENT_NOT_SELECTED_BY_PRODUCTION"
            return "DISCOVERY_INSUFFICIENT_UNCLASSIFIED"
        if diagnostic.get("filteredUrlCount", 0):
            return "URL_FILTERED_BEFORE_FETCH"
        if any(x.get("diagnosticOnlyObservation") and
               x.get("seriesIdentityConfirmed") is False for x in diagnostic_checks):
            return "DIAGNOSTIC_ONLY_IDENTITY_NOT_CONFIRMED"
        if any(x.get("diagnosticOnlyObservation") and
               x.get("articleStructureConfirmed") is False for x in diagnostic_checks):
            return "DIAGNOSTIC_ONLY_STRUCTURE_NOT_CONFIRMED"
        if diagnostic.get("status") == "sitemap_unparseable":
            return "OFFICIAL_SITEMAP_UNPARSEABLE"
        if diagnostic.get("status") == "sitemap_unavailable":
            return "OFFICIAL_SITEMAP_UNAVAILABLE"
        if diagnostic.get("status") == "bounded_partial_scan":
            return "DISCOVERY_INSUFFICIENT_UNCLASSIFIED"
        return "NO_RELEVANT_OFFICIAL_URL"
    if state == "PROVIDER_UNAVAILABLE":
        return "OFFICIAL_FETCH_FAILED"
    return "DISCOVERY_INSUFFICIENT_UNCLASSIFIED"


def diagnose_case(tmdb_id: int, metadata: dict | None = None, *,
                  metadata_fetcher=discovery.tmdb_metadata,
                  fetcher=update.fetch, processor=monitored.process) -> dict:
    if not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool) or tmdb_id <= 0:
        raise ValueError("TMDB ID must be positive")
    case = {"tmdbId": tmdb_id, "routing": {}, "productionDiscovery": {},
            "diagnosticDiscovery": {}, "identityChecks": [], "structureChecks": [],
            "urlStages": [], "parser": {"result": "not_run"},
            "validation": {"result": "not_applicable"},
            "independentReconstruction": {"result": "not_applicable"},
            "observations": []}
    try:
        meta = metadata if metadata is not None else metadata_fetcher(tmdb_id, discovery.local_tmdb_token())
    except (OSError, UnicodeError, ValueError, update.AutomationError) as exc:
        case["routing"] = {"provider": None, "reason": "metadata_unavailable",
                           "factualProcessingStopped": True}
        case["metadataErrorType"] = type(exc).__name__
        case["classification"] = "METADATA_UNAVAILABLE"
        return case
    if not isinstance(meta, dict) or meta.get("tmdbId") != tmdb_id:
        raise ValueError("TMDB metadata identity mismatch")
    case["title"] = meta.get("title")
    case["metadata"] = _metadata_projection(meta)
    provider, signals, reason = discovery.route_provider(meta, discovery.provider_specs())
    case["routing"] = {"provider": provider, "routeSignals": signals, "reason": reason}
    if provider != "DISNEY_PLUS":
        case["routing"]["factualProcessingStopped"] = True
        case["classification"] = "ROUTING_NOT_DISNEY_PLUS"
        return case

    captures: list[dict] = []
    production_result: dict = {}
    # The processor receives only the original production discoverer. Diagnostic
    # sitemap discoveries are performed after this call and never enter it.
    def parser_fetch(url: str, domains: list[str], headers=None):
        try:
            raw, final = fetcher(url, domains, headers)
            captures.append({"stage": "factual_parser", "requestedUrl": url,
                             "finalUrl": final, "finalDomain": urlparse(final).hostname,
                             "domainApproved": _safe_url(final, domains),
                             "fetchResult": "success"})
            return raw, final
        except Exception as exc:
            captures.append({"stage": "factual_parser", "requestedUrl": url,
                             "finalUrl": None, "finalDomain": None,
                             "domainApproved": False, "fetchResult": safe_http_failure(exc)})
            raise

    pipeline = processor(tmdb_id, dry_run=True, metadata=meta,
                         discoverer=_production_discoverer(captures, fetcher, production_result),
                         fetcher=parser_fetch)
    if pipeline.get("tmdbId") != tmdb_id or pipeline.get("dryRun") is not True:
        raise ValueError("Invalid monitored dry-run result")
    case["pipeline"] = pipeline
    case["productionDiscovery"] = {
        "result": pipeline.get("discoveryResult"),
        "attemptedOfficialUrls": production_result.get("attemptedOfficialUrls", []),
        "candidateUrls": pipeline.get("candidateUrls", []),
        "candidates": production_result.get("candidates", []),
        "rejectedCandidates": production_result.get("rejectedCandidates", []),
        "candidateDiagnostics": pipeline.get("candidateDiagnostics", []),
        "eligibleCandidateCount": pipeline.get("eligibleCandidateCount", 0),
        "productionFetches": [{key: value for key, value in item.items() if key != "raw"}
                              for item in captures],
        "sitemaps": _production_sitemap_surface(captures, discovery.metadata_names(meta)),
        "bounds": {"maxSeeds": discovery.MAX_SEEDS, "maxLinks": discovery.MAX_LINKS,
                   "maxSitemapBytes": discovery.MAX_SITEMAP_BYTES,
                   "maxSitemapChildren": discovery.MAX_SITEMAP_CHILDREN,
                   "maxSitemapEntries": discovery.MAX_SITEMAP_URLS,
                   "maxSitemapArticles": discovery.MAX_SITEMAP_ARTICLES,
                   "fetchBudget": 1 + discovery.MAX_SITEMAP_CHILDREN + discovery.MAX_SITEMAP_ARTICLES},
    }
    prod_report = {"candidates": production_result.get("candidates", []),
                   "rejectedCandidates": production_result.get("rejectedCandidates", [])}
    case["identityChecks"], case["structureChecks"], case["urlStages"] = _captured_page_checks(meta, captures, prod_report)
    # The ordinary discovery result contains rejection details; re-run only the
    # pure discovery classifier against captured responses is not needed. Attach
    # exact reasons surfaced in monitored candidate diagnostics where present.
    case["diagnosticDiscovery"] = _deep_sitemap_inspection(meta, captures, fetcher)
    case["parser"] = {"result": pipeline.get("parserResult", "skipped"),
                      "parsedSourceCount": pipeline.get("parsedSourceCount", 0),
                      "facts": pipeline.get("verifiedFacts", {}),
                      "sourceEvidence": pipeline.get("sourceEvidence", []),
                      "eligibleSourceUrls": pipeline.get("eligibleSourceUrls", [])}
    case["validation"] = {"result": pipeline.get("validationResult", "not_applicable"),
                          "reason": pipeline.get("validationReason")}
    if pipeline.get("pipelineState") == "VERIFIED_FACTS":
        row = pipeline.get("proposedRecord")
        if row is None and pipeline.get("registryChange") == "none":
            registry, _ = monitored.load_registry(monitored.MONITORED)
            row = next((item for item in registry["series"] if item.get("tmdbId") == tmdb_id), None)
        if not isinstance(row, dict):
            raise ValueError("VERIFIED_FACTS has no proposed monitored record")
        try:
            domains = discovery.provider_specs()[provider]["officialDomains"]
            facts = promotion._validate_evidence(row, domains)
            rebuilt = promotion._facts_summary(facts, row, dt.date.fromisoformat(row["lastChecked"]))
            fact_summary = {field: rebuilt.get(field) for field in FACT_FIELDS}
            current = {field: (row.get("verifiedFacts") or {}).get(field) for field in FACT_FIELDS}
            mismatches = [field for field in FACT_FIELDS if fact_summary.get(field) != current.get(field)]
            case["independentReconstruction"] = {"result": "failed" if mismatches else "passed",
                                                  "facts": fact_summary,
                                                  "mismatchedFields": mismatches,
                                                  "comparedFields": list(FACT_FIELDS)}
        except (update.AutomationError, ValueError, KeyError) as exc:
            case["independentReconstruction"] = {"result": "failed",
                                                  "errorType": type(exc).__name__,
                                                  "mismatch": getattr(exc, "diagnostic", None)}
    case["classification"] = classify_case(case)
    return case


def coverage_summary(cases: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for case in cases:
        category = case.get("classification", "unknown")
        counts[category] = counts.get(category, 0) + 1
    gaps = sorted(category for category in counts if category not in
                   ("PRODUCTION_PATH_VERIFIED", "ROUTING_NOT_DISNEY_PLUS"))
    production_change = "insufficient_evidence"
    if not gaps:
        production_change = False
    elif any(category in counts for category in
             ("IDENTITY_BINDING_REJECTED", "ARTICLE_STRUCTURE_REJECTED", "PARSER_NO_FACTS")):
        production_change = "insufficient_evidence"
    return {"successfulProductionCases": counts.get("PRODUCTION_PATH_VERIFIED", 0),
            "discoveryInsufficientCases": sum(1 for case in cases
                                               if (case.get("pipeline") or {}).get("pipelineState") == "DISCOVERY_INSUFFICIENT"),
            "failureStageCounts": counts,
            "primaryObservedGaps": gaps,
            "productionChangeRecommended": production_change,
            "recommendationEvidence": [f"{case.get('tmdbId')}: {case.get('classification')}"
                                       for case in cases if case.get("classification") in gaps]}


def summary_lines(report: dict) -> list[str]:
    lines = ["### Disney+ Discovery Coverage Diagnostics", "", "Read-only report; diagnostic-only URLs are excluded from monitored facts.", ""]
    for case in report.get("cases", []):
        pipeline = case.get("pipeline") or {}
        diag = case.get("diagnosticDiscovery") or {}
        lines.extend([f"#### {case.get('tmdbId')} — {case.get('title', 'metadata unavailable')}",
                      f"Provider: {case.get('routing', {}).get('provider')}",
                      f"Production discovery: {pipeline.get('discoveryResult', 'not_run')}",
                      f"Production candidates: {pipeline.get('eligibleCandidateCount', 0)} eligible / {len(pipeline.get('candidateUrls') or [])} URLs",
                      f"Diagnostic relevant URLs: {diag.get('relevantUrlCount', len(diag.get('relevantUrls') or []))}",
                      f"Stage: {case.get('classification')}",
                      f"Parser: {(case.get('parser') or {}).get('result', 'not_run')}",
                      f"Validation: {(case.get('validation') or {}).get('result', 'not_applicable')}",
                      f"Independent reconstruction: {(case.get('independentReconstruction') or {}).get('result', 'not_applicable')}", ""])
    coverage = report.get("coverageSummary") or {}
    lines.extend(["#### Coverage summary", f"Primary observed gaps: {', '.join(coverage.get('primaryObservedGaps') or []) or 'none observed'}",
                  f"Production hardening: {coverage.get('productionChangeRecommended')}",
                  "Next investigation: review the JSON evidence for the first failing discovery, identity, structure, parser, or reconstruction stage.",
                  f"Protected files unchanged: {report.get('protectedFileIntegrity', {}).get('unchanged')}"])
    return lines


def run(*, metadata_fetcher=discovery.tmdb_metadata, fetcher=update.fetch,
        processor=monitored.process) -> dict:
    before = protected_hashes()
    cases = []
    error_types = []
    for tmdb_id in CASES:
        try:
            cases.append(diagnose_case(tmdb_id, metadata_fetcher=metadata_fetcher,
                                       fetcher=fetcher, processor=processor))
        except Exception as exc:
            error_types.append(type(exc).__name__)
            cases.append({"tmdbId": tmdb_id, "classification": "DIAGNOSTIC_ERROR",
                          "errorType": type(exc).__name__, "observations": []})
    after = protected_hashes()
    return {"schemaVersion": 1, "reportType": "disney_plus_discovery_coverage_diagnostics",
            "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "readOnly": True, "cases": cases, "coverageSummary": coverage_summary(cases),
            "protectedFileIntegrity": {"beforeSha256": before, "afterSha256": after,
                                       "unchanged": before == after},
            "diagnosticErrors": error_types}


def _write_report(report: dict) -> Path:
    output_dir = Path(os.environ["RUNNER_TEMP"])
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "disney-plus-discovery-coverage-diagnostics.json"
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    token = discovery.local_tmdb_token()
    if token and token in rendered:
        raise ValueError("Diagnostic report contains TMDB token")
    output.write_text(rendered, encoding="utf-8")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write("\n".join(summary_lines(report)) + "\n")
    return output


def main() -> int:
    report = run()
    _write_report(report)
    failed = (not report["protectedFileIntegrity"]["unchanged"] or
              bool(report.get("diagnosticErrors")) or
              any(case.get("classification") == "METADATA_UNAVAILABLE"
                  for case in report.get("cases", [])) or
              any((case.get("independentReconstruction") or {}).get("result") == "failed"
                  for case in report.get("cases", [])))
    if failed:
        raise SystemExit("Disney+ coverage diagnostics failed; inspect JSON artifact")
    return 0


if __name__ == "__main__":
    if sys.argv[1:]:
        raise SystemExit("Unexpected command-line arguments")
    raise SystemExit(main())
