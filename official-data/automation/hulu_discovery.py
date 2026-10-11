"""Hulu candidate wiring for the shared discovery/monitored production pipeline.

Reuse qualified first-party link ranking, form and editorial extraction. Unlike
qualification, production has no known article URL or expected season/status.
No lifecycle facts are asserted here; monitored's strict parser is mandatory.
"""
from urllib.parse import urljoin, urlparse

import discovery
import provider_audit as qualified
import update

MAX_FETCHES = 20
MAX_ROOT_FETCHES = 5
MAX_CANDIDATES = 4


def article_identity(raw: str, names: list[str]) -> bool:
    page = update.parse_page(raw)
    heading = discovery.normalize(page.heading.strip() or page.title.strip())
    if any(term in heading for term in ("spinoff", "spin off", "remake", "different series")):
        return False
    text = qualified._QualificationText()
    text.feed(raw)
    # Corporate/slate releases may identify a show in an editorial block,
    # rather than in the company-level h1. Never use search result snippets,
    # related stories or navigation as this identity evidence.
    for block in [page.heading.strip() or page.title.strip()] + text.text()[:25000].splitlines():
        probe = update.Page()
        probe.heading = block
        if discovery.announcement_article_identity(probe, names):
            return True
    return False


def discover(metadata: dict, spec: dict, report: dict, fetcher) -> dict:
    names = discovery.metadata_names(metadata)
    domains = spec["candidateDomains"]
    seen = set()
    count = 0
    report["attemptedOfficialUrls"] = []

    def fetch(url):
        nonlocal count
        if count >= MAX_FETCHES or url in seen or not update.allowed(url, domains):
            return None
        seen.add(url)
        count += 1
        report["attemptedOfficialUrls"].append(url)
        try:
            raw, final = fetcher(url, domains)
            if not update.allowed(final, domains):
                report["rejectedCandidates"].append({"url": url, "reasons": ["redirect_outside_allowlist"]})
                return None
            update.parse_page(raw)  # Includes access-block/challenge detection.
            return raw, final
        except (update.ProviderUnavailable, update.AutomationError, ValueError) as exc:
            reason = discovery.safe_failure(exc)
            report["warnings"].append(f"HULU {url}: {reason}")
            report["rejectedCandidates"].append({"url": url, "reasons": ["fetch_or_parse_failed"], "detail": reason})
            if isinstance(exc, update.ProviderUnavailable) and reason != "HTTP 404":
                report.setdefault("unavailableUrls", []).append(url)
            return None

    def accept(url, fetched, parent, provenance):
        raw, final = fetched
        if not urlparse(final).path.startswith("/pressrelease/") or not article_identity(raw, names):
            report["rejectedCandidates"].append({"url": final, "reasons": ["article_identity_or_structure_not_confirmed"]})
            return
        if final in report["candidateUrls"]:
            return
        report["candidateUrls"].append(final)
        report["candidates"].append({
            "candidateUrl": final, "candidateDomain": urlparse(final).hostname,
            "discoveryMethod": "official Hulu article link", "pageType": "official_article",
            "providerRoutingConfidence": "NETWORK", "sourceIdentityConfidence": "CONFIRMED",
            "evidenceLevel": "STRONG", "identityValidated": True,
            "releaseStructure": True, "homepageIdMatched": False,
            "factualParserEligible": True, "reasons": [],
            "discoveryProvenance": provenance, "discoveryRoot": parent,
            "sourceAuthority": "official_provider_announcement", "requestedUrl": url,
            "finalUrl": final, "redirectWithinOfficialBoundary": True})

    slugs = list(dict.fromkeys(discovery.slug(name) for name in names))
    roots = list(dict.fromkeys(template.format(slug=part) for template in spec["seeds"]
                               for part in slugs))[:discovery.MAX_SEEDS]
    queue = [(url, None) for url in roots]
    walked = 0
    while queue and walked < MAX_ROOT_FETCHES and len(report["candidates"]) < MAX_CANDIDATES:
        url, parent = queue.pop(0)
        if url in seen:
            continue
        walked += 1
        fetched = fetch(url)
        if not fetched:
            continue
        raw, final = fetched
        if urlparse(final).path.startswith("/pressrelease/"):
            accept(url, fetched, parent, "official_link_traversal")
        else:
            links = qualified._qualification_links(raw, final, domains, names, "")
            queue.extend((link, final) for link in links[:qualified.MAX_QUALIFICATION_LINKS_PER_PAGE]
                         if link not in seen)

    if not report["candidates"]:
        home = qualified.HULU_SEARCH_HOME
        fetched = fetch(home)
        if fetched:
            raw, final = fetched
            # Same validated GET form contract as qualification. Production
            # refines by "season" only: never guess a lifecycle or season.
            for index, query in enumerate((metadata["title"], metadata["title"] + " season")):
                if len(query) > 200:
                    continue
                search = discovery._amc_search_url(raw, final, domains, query)
                result = fetch(search) if search else None
                if not result:
                    continue
                result_raw, result_final = result
                links = qualified._qualification_links(result_raw, result_final, domains, names, "")
                # Context search can find a company-level release whose URL
                # and link text omit the show. It remains only a candidate;
                # fetch and editorial identity checks below are mandatory.
                search_links = qualified._QualificationLinks()
                search_links.feed(result_raw)
                for href, _ in search_links.links:
                    link = urljoin(result_final, href)
                    if (update.allowed(link, domains) and urlparse(link).path.startswith("/pressrelease/")
                            and link not in links):
                        links.append(link)
                reserve = 2 if index == 0 else 0
                for link in links[:qualified.MAX_OFFICIAL_SEARCH_RESULTS]:
                    if count >= MAX_FETCHES - reserve or len(report["candidates"]) >= MAX_CANDIDATES:
                        break
                    if not urlparse(link).path.startswith("/pressrelease/"):
                        continue
                    article = fetch(link)
                    if article:
                        accept(link, article, result_final,
                               "hulu_official_site_search" if index == 0 else "hulu_official_site_search_context")
                if report["candidates"]:
                    break
    if report["candidates"]:
        report.update(result="candidate_found", evidenceLevel="STRONG")
    elif report.get("unavailableUrls"):
        report.update(result="provider_unavailable", reason="official_provider_access_failed")
    else:
        report["reason"] = "no_valid_official_candidate"
    return report
