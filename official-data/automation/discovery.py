"""Read-only discovery of official provider-owned sources for unregistered TV shows.

TMDB is routing metadata only. No result from this module is a factual status.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import unicodedata
from html.parser import HTMLParser
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, urljoin, urlparse

import update

PROVIDERS = update.ROOT / "official-data" / "discovery_providers.json"
MAX_SEEDS = 5
MAX_LINKS = 4
MAX_SITEMAP_BYTES = 1_000_000
MAX_SITEMAP_CHILDREN = 8
MAX_SITEMAP_URLS = 2_500
MAX_SITEMAP_ARTICLES = 20
MAX_AMC_SEARCH_RESULTS = 20
AMC_SEARCH_HOME = "https://www.amcglobalmedia.com/"


def provider_specs() -> dict:
    specs = json.loads(PROVIDERS.read_text(encoding="utf-8"))
    for name, spec in specs.items():
        if not spec["officialDomains"] or not spec["candidateDomains"] or not spec["seeds"]:
            raise ValueError(f"Incomplete discovery provider: {name}")
        if not set(spec["candidateDomains"]).issubset(set(spec["officialDomains"])):
            raise ValueError(f"Candidate domain outside provider allowlist: {name}")
        if any(not update.allowed(seed.replace("{slug}", "example"), spec["candidateDomains"]) for seed in spec["seeds"]):
            raise ValueError(f"Invalid discovery seed: {name}")
    return specs


def normalize(value: str) -> str:
    value = value.replace("\u2018", "'").replace("\u2019", "'")
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


def slug(value: str) -> str:
    return normalize(value).replace(" ", "-")


def metadata_names(metadata: dict) -> list[str]:
    return list(dict.fromkeys(name for name in (metadata.get("title"), metadata.get("originalName")) if isinstance(name, str) and name.strip()))


def route_provider(metadata: dict, specs: dict) -> tuple[str | None, list[str], str | None]:
    signals: dict[str, list[str]] = {}
    for provider, spec in specs.items():
        strict_network = provider in ("AMC", "DISNEY_PLUS")
        # Preserve '+': Disney is not Disney+, and AMC+ is not AMC.
        normalize_route = (lambda value: " ".join(unicodedata.normalize("NFKC", value).casefold().split())) if strict_network else normalize
        names = {normalize_route(n) for n in spec["routingNames"]}
        fields = ("networks",) if provider in ("AMC", "DISNEY_PLUS") else ("networks", "productionCompanies")
        for field in fields:
            for item in metadata.get(field) or []:
                label = item.get("name", "") if isinstance(item, dict) else item
                if isinstance(label, str) and normalize_route(label) in names:
                    signals.setdefault(provider, []).append(f"{field}:{label}")
        homepage = metadata.get("homepage") or ""
        if provider not in ("AMC", "DISNEY_PLUS") and isinstance(homepage, str) and update.allowed(homepage, spec["officialDomains"]):
            signals.setdefault(provider, []).append("official_homepage")
    if len(signals) != 1:
        return None, [], "ambiguous_provider" if signals else "unknown_provider"
    provider = next(iter(signals))
    return provider, signals[provider], None


def tmdb_metadata(tmdb_id: int, token: str, fetcher=update.fetch) -> dict:
    if not token:
        raise ValueError("TMDB_API_TOKEN is required for --discover without --metadata-file")
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?language=en-US"
    raw, _ = fetcher(url, ["api.themoviedb.org"], {"Authorization": f"Bearer {token}", "Accept": "application/json"})
    data = json.loads(raw)
    if data.get("id") != tmdb_id or not isinstance(data.get("name"), str):
        raise ValueError("TMDB response identity mismatch")
    return {"tmdbId": data["id"], "title": data["name"], "originalName": data.get("original_name"),
            "networks": data.get("networks") or [], "productionCompanies": data.get("production_companies") or [],
            "homepage": data.get("homepage"), "originCountry": data.get("origin_country") or []}


def local_tmdb_token() -> str:
    if os.environ.get("TMDB_API_TOKEN"):
        return os.environ["TMDB_API_TOKEN"]
    path = update.ROOT / "local.properties"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("TMDB_API_TOKEN="):
                return line.partition("=")[2].strip()
    return ""


def title_identity(page: update.Page, names: list[str]) -> bool:
    heading = normalize(page.heading.strip() or page.title.strip())
    # Only a complete show heading identifies an index. An article mentioning
    # the words within a longer title, remake or spin-off is not enough.
    return bool(heading and heading in {normalize(name) for name in names})


def branded_index_identity(page: update.Page, names: list[str], provider: str) -> bool:
    if title_identity(page, names):
        return True
    heading = normalize(page.heading.strip())
    return provider == "NETFLIX" and any(heading == normalize(name) + " cast news videos and more" for name in names)


def homepage_identity(page: update.Page, homepage: str, domains: list[str]) -> bool:
    if not homepage or not update.allowed(homepage, domains):
        return False
    identifier = urlparse(homepage).path.rstrip("/").split("/")[-1].casefold()
    # A distinctive official show ID, not another occurrence of a common title.
    if len(identifier) < 8 or not re.search(r"\d", identifier) or not re.fullmatch(r"[a-z0-9.]+", identifier):
        return False
    return any(update.allowed(link, domains) and identifier in [part.casefold() for part in urlparse(link).path.split("/")]
               for link in page.links)


def release_structure(url: str, page: update.Page, provider: str) -> bool:
    path = urlparse(url).path.lower()
    body = normalize("".join(page.parts[:200]))
    if provider == "PARAMOUNT":
        return ("/shows/" in path and "/releases" in path and "release" in body and
                any(paramount_release_link(urljoin(url, link), url) for link in page.links))
    if provider == "NETFLIX":
        return path.startswith("/tudum/") and ("news" in body or "featured" in body)
    if provider == "APPLE":
        return "/tv-pr/originals/" in path and ("news" in body or "press release" in body)
    if provider == "WBD":
        return "/property/" in path and "/media-releases" in path and "media releases" in body
    return False  # An Amazon-wide news index cannot establish one series.


def article_link(url: str, provider: str, names: list[str]) -> bool:
    path = urlparse(url).path.casefold()
    prefixes = {slug(name) for name in names}
    last = path.rstrip("/").split("/")[-1]
    matches = any(re.search(rf"(?<![a-z0-9]){re.escape(prefix)}(?![a-z0-9])", last) for prefix in prefixes)
    if provider == "AMAZON":
        return path.startswith("/news/entertainment/") and matches
    if provider == "NETFLIX":
        return path.startswith("/tudum/articles/") and matches
    return False


def article_identity(page: update.Page, names: list[str]) -> bool:
    heading = normalize(page.heading.strip())
    permitted = ("season", "renewed", "returns", "premieres", "release", "watch", "how", "everything", "new")
    return any(heading.startswith(normalize(name) + " " + cue + " ") for name in names for cue in permitted)


def announcement_article_identity(page: update.Page, names: list[str]) -> bool:
    """Require the exact TMDB title phrase in the article heading plus a season claim cue."""
    heading = normalize(page.heading.strip() or page.title.strip())
    if not heading:
        return False
    has_title = any(re.search(rf"(?<![a-z0-9]){re.escape(normalize(name))}(?![a-z0-9])", heading)
                    for name in names)
    cues = ("season", "renewed", "renewal", "final season", "premieres", "premiere",
            "returns", "returning", "debut")
    return has_title and any(cue in heading for cue in cues)


def _announcement_structure(url: str, page: update.Page, provider: str) -> bool:
    path = urlparse(url).path.casefold()
    body = normalize("".join(page.parts[:300]))
    path_ok = bool(re.match(r"^/\d{4}/\d{2}/\d{2}/", path)) if provider == "AMC" else path.startswith("/news/")
    return path_ok and len(body) >= 80 and any(term in body for term in ("season", "renewed", "premiere", "premieres", "final"))


class _AmcSearchForm(HTMLParser):
    """Extract the same first-party GET search contract qualified in provider_audit."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self.current: dict | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"action": attrs.get("action", ""),
                            "method": (attrs.get("method") or "get").casefold(), "inputs": []}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None:
            self.current["inputs"].append({"name": attrs.get("name"),
                                           "type": (attrs.get("type") or "text").casefold()})

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


def _amc_search_url(raw: str, final_url: str, domains: list[str], title: str) -> str | None:
    parser = _AmcSearchForm()
    parser.feed(raw)
    for form in parser.forms:
        if form["method"] != "get" or not any(item["name"] == "s" for item in form["inputs"]):
            continue
        action = urljoin(final_url, form["action"] or final_url)
        if not update.allowed(action, domains):
            continue
        result = action + ("&" if "?" in action else "?") + urlencode({"s": title})
        if len(result) <= 2048:
            return result
    return None


def _title_path_match(url: str, names: list[str]) -> bool:
    parts = set(re.findall(r"[a-z0-9]+", urlparse(url).path.casefold()))
    stop = {"the", "and", "for", "with", "from", "that", "this", "series", "show"}
    return any((words := {word for word in re.findall(r"[a-z0-9]+", re.sub(r"[\u2019']s\b", "s", name.casefold()))
                          if len(word) > 2 and word not in stop}) and words.issubset(parts)
               for name in names)


def _amc_article_link(url: str, domains: list[str], names: list[str]) -> bool:
    path = urlparse(url).path
    return (update.allowed(url, domains) and
            bool(re.match(r"^/\d{4}/\d{2}/\d{2}/", path)) and _title_path_match(url, names))


def _disney_article_link(url: str, domains: list[str], names: list[str]) -> bool:
    return update.allowed(url, domains) and urlparse(url).path.casefold().startswith("/news/") and _title_path_match(url, names)


def _disney_sitemap_links(raw: str, domains: list[str], names: list[str]) -> tuple[list[str], list[str]]:
    """Bounded equivalent of provider_audit's Disney sitemap traversal."""
    if len(raw.encode("utf-8")) > MAX_SITEMAP_BYTES or "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        return [], []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return [], []
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    if root.tag == ns + "sitemapindex":
        children = root.findall(ns + "sitemap")
        if len(children) > MAX_SITEMAP_CHILDREN:
            return [], []
        maps = [child.findtext(ns + "loc") for child in children]
        return ([url for url in maps if url and update.allowed(url, domains) and
                 urlparse(url).path.casefold().endswith(".xml")], [])
    if root.tag != ns + "urlset":
        return [], []
    entries = root.findall(ns + "url")
    if len(entries) > MAX_SITEMAP_URLS:
        return [], []
    articles = []
    for entry in entries:
        loc = entry.findtext(ns + "loc")
        if loc and _disney_article_link(loc, domains, names) and loc not in articles:
            articles.append(loc)
            if len(articles) >= MAX_SITEMAP_ARTICLES:
                break
    return [], articles


def paramount_listing_identity(url: str, page: update.Page, names: list[str]) -> bool:
    """Press Express uses a branded document title, often without a sole H1."""
    path = urlparse(url).path.rstrip("/").lower()
    if not any(path.endswith(f"/shows/{slug(name)}/releases") for name in names):
        return False
    title = normalize(page.title)
    heading = normalize(page.heading)
    return any((f" {normalize(name)} releases" in f" {title}" or heading == normalize(name)) for name in names)


def paramount_release_link(url: str, listing: str) -> bool:
    # Reuse the production adapter's view= release-link format, bound to this show.
    parsed, parent = urlparse(url), urlparse(listing)
    return (parsed.scheme == "https" and parsed.hostname == parent.hostname and
            parsed.path.rstrip("/") == parent.path.rstrip("/") and
            bool(re.search(r"(?:^|&)view=", parsed.query)))


def article_page_type(page: update.Page, provider: str) -> bool:
    body = normalize("".join(page.parts[:100]))
    if provider == "NETFLIX":
        return "news" in body and bool(re.search(r"\bby [a-z]+ [a-z]+\b", body))
    if provider == "AMAZON":
        return "prime video" in body and bool(re.search(r"\bseason\b|\bseries\b", body))
    return False


def safe_failure(exc: Exception) -> str:
    # Never echo exception bodies from a provider or a URL query in the report.
    match = re.search(r"\bHTTP (\d{3})\b", str(exc))
    if match:
        return f"HTTP {match.group(1)}"
    if "too large" in str(exc).lower():
        return "response_too_large"
    return type(exc).__name__


def discover(metadata: dict, registry: dict, fetcher=update.fetch, specs: dict | None = None) -> dict:
    specs = specs or provider_specs()
    tmdb_id = metadata.get("tmdbId")
    title = metadata.get("title")
    if not isinstance(tmdb_id, int) or tmdb_id <= 0 or not isinstance(title, str) or not title.strip():
        raise ValueError("Discovery requires positive tmdbId and nonempty title")
    report = {"tmdbId": tmdb_id, "title": title, "provider": None, "result": "insufficient_evidence",
              "evidenceLevel": "INSUFFICIENT", "candidateUrls": [], "candidates": [], "rejectedCandidates": [],
              "warnings": [], "attemptedOfficialUrls": []}
    trusted = next((entry for entry in registry["series"] if entry["tmdbId"] == tmdb_id), None)
    if trusted:
        report.update(provider=trusted["provider"], result="trusted_registry", trustedSource=trusted["officialUrl"])
        return report
    names = metadata_names(metadata)
    provider, signals, problem = route_provider(metadata, specs)
    report["routeSignals"] = signals
    if not provider:
        report["reason"] = problem
        return report
    report["provider"] = provider
    spec = specs[provider]
    domains = spec["candidateDomains"]
    slugs = list(dict.fromkeys([slug(name) for name in names] + [slug(name).removeprefix("the-") for name in names if slug(name).startswith("the-")]))
    seed_urls = list(dict.fromkeys(template.format(slug=part) for template in spec["seeds"] for part in slugs))[:MAX_SEEDS]
    report["attemptedOfficialUrls"] = seed_urls
    pending = [(url, spec["mechanism"], None) for url in seed_urls]
    seen = set()
    fetched_links = 0
    fetch_budget = (1 + MAX_SITEMAP_CHILDREN + MAX_SITEMAP_ARTICLES if provider == "DISNEY_PLUS" else
                    2 + MAX_AMC_SEARCH_RESULTS if provider == "AMC" else MAX_SEEDS + MAX_LINKS)
    while pending and len(seen) < fetch_budget:
        url, method, parent = pending.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if not update.allowed(url, domains):
            report["rejectedCandidates"].append({"url": url, "reasons": ["domain_not_allowlisted"]})
            continue
        try:
            raw, final = fetcher(url, domains)
            if not update.allowed(final, domains):
                report["rejectedCandidates"].append({"url": url, "reasons": ["redirect_outside_allowlist"]})
                report["warnings"].append(f"Rejected redirect outside official domain: {url}")
                continue
            if provider == "AMC" and method == "official AMC search form":
                search_url = _amc_search_url(raw, final, domains, title.strip())
                if search_url and search_url not in seen and not any(row[0] == search_url for row in pending):
                    pending.append((search_url, "official AMC site search", final))
                continue
            is_sitemap = provider == "DISNEY_PLUS" and urlparse(final).path.casefold().endswith(".xml")
            if is_sitemap:
                sitemap_children, article_urls = _disney_sitemap_links(raw, domains, names)
                for link in sitemap_children + article_urls:
                    link_method = "official Disney+ sitemap" if link in sitemap_children else "official article link"
                    if link not in seen and not any(row[0] == link for row in pending):
                        pending.append((link, link_method, final))
                continue
            page = update.parse_page(raw)
        except (update.ProviderUnavailable, update.AutomationError, ValueError) as exc:
            reason = safe_failure(exc)
            report["warnings"].append(f"{provider} {url}: {reason}")
            report["rejectedCandidates"].append({"url": url, "reasons": ["fetch_or_parse_failed"], "detail": reason})
            if isinstance(exc, update.ProviderUnavailable) and reason != "HTTP 404":
                report.setdefault("unavailableUrls", []).append(url)
            continue
        if provider == "AMC" and method == "official AMC site search":
            for link in page.links:
                linked = urljoin(final, link)
                if (_amc_article_link(linked, domains, names) and linked not in seen and
                        not any(row[0] == linked for row in pending)):
                    pending.append((linked, "official article link", final))
                    fetched_links += 1
                    if fetched_links >= MAX_AMC_SEARCH_RESULTS:
                        break
            continue
        is_article = method == "official article link"
        is_release = method == "official individual release"
        provider_article = provider in ("AMC", "DISNEY_PLUS") and is_article
        identity = (branded_index_identity(page, names, provider) or
                    (provider == "PARAMOUNT" and not is_release and paramount_listing_identity(final, page, names)) or
                    (is_article and article_identity(page, names)) or
                    (provider_article and announcement_article_identity(page, names)) or
                    (is_release and parent is not None and paramount_release_link(final, parent) and
                     any(re.search(rf"(?<![a-z0-9]){re.escape(normalize(name))}(?![a-z0-9])", normalize(page.heading)) for name in names)))
        structure = _announcement_structure(final, page, provider) if provider_article else release_structure(final, page, provider)
        homepage_match = homepage_identity(page, metadata.get("homepage") or "", spec["officialDomains"])
        network_routed = any(s.startswith("networks:") for s in signals)
        page_type = ("official_article" if is_article else "individual_release" if is_release else
                     "show_release_listing" if provider == "PARAMOUNT" and structure else
                     "show_news_index" if provider in ("APPLE", "NETFLIX", "WBD") else "generic_index")
        editorial = (is_article and article_page_type(page, provider)) or (provider_article and structure and identity)
        conflict = any(term in normalize(page.heading) for term in ("spinoff", "spin off", "remake", "different series"))
        eligible = bool(identity and network_routed and not conflict and (
            (not is_article and not is_release and structure and homepage_match and page_type != "generic_index") or
            (provider == "NETFLIX" and editorial and homepage_match) or
            (provider_article and editorial and network_routed) or
            (is_release and parent and paramount_release_link(final, parent) and editorial is False)))
        if eligible:
            level = "STRONG"
        elif not is_article and identity and structure:
            level = "SUPPORTED"
        elif (is_article or is_release) and identity and not conflict:
            level = "SUPPORTED"
        else:
            level = "INSUFFICIENT"
        reasons = []
        if not identity: reasons.append("series_identity_not_confirmed")
        if conflict: reasons.append("conflicting_identity_signal")
        if not structure and not editorial and not is_release: reasons.append("release_structure_not_detected")
        if not homepage_match and provider not in ("AMC", "DISNEY_PLUS"):
            reasons.append("homepage_id_not_matched")
        if not network_routed: reasons.append("network_routing_not_confirmed")
        if is_article and not editorial: reasons.append("article_structure_not_confirmed")
        if page_type == "generic_index": reasons.append("generic_index_not_parser_eligible")
        candidate = {"candidateUrl": final, "candidateDomain": urlparse(final).hostname,
                     "discoveryMethod": method, "pageType": page_type, "providerRoutingConfidence": "NETWORK" if network_routed else "HOMEPAGE_OR_COMPANY",
                     "sourceIdentityConfidence": "CONFIRMED" if identity else "INSUFFICIENT", "evidenceLevel": level, "identityValidated": identity,
                     "releaseStructure": structure, "homepageIdMatched": homepage_match,
                     "factualParserEligible": eligible, "reasons": reasons}
        if provider_article:
            candidate.update({"discoveryProvenance": "amc_official_site_search" if provider == "AMC" else "disney_official_sitemap",
                              "discoveryRoot": parent, "sourceAuthority": "official_provider_announcement",
                              "identityMatchedName": next((name for name in names if
                                  re.search(rf"(?<![a-z0-9]){re.escape(normalize(name))}(?![a-z0-9])",
                                            normalize(page.heading.strip() or page.title.strip()))), None),
                              "requestedUrl": url, "finalUrl": final,
                              "redirectWithinOfficialBoundary": True})
        if provider_article and conflict:
            level = "INSUFFICIENT"
            reasons.append("conflicting_identity_signal")
        if level != "INSUFFICIENT" and final not in report["candidateUrls"]:
            report["candidateUrls"].append(final)
            report["candidates"].append(candidate)
        elif level == "INSUFFICIENT":
            report["rejectedCandidates"].append({"url": final, "reasons": reasons or ["insufficient_evidence"]})
        if provider == "PARAMOUNT" and identity and structure and not is_release:
            for link in update.ParamountAdapter().candidates(page, final, {}):
                if fetched_links >= MAX_LINKS: break
                if update.allowed(link, domains) and paramount_release_link(link, final) and link not in seen and not any(p[0] == link for p in pending):
                    pending.append((link, "official individual release", final))
                    fetched_links += 1
        # Link discovery is bounded and restricted to original provider pages.
        for link in page.links:
            if fetched_links >= MAX_LINKS:
                break
            linked = urljoin(final, link)
            if (linked not in seen and not any(pending_url == linked for pending_url, _, _ in pending)
                    and update.allowed(linked, domains) and article_link(linked, provider, names)):
                pending.append((linked, "official article link", final))
                fetched_links += 1
    if report["candidateUrls"]:
        report["result"] = "candidate_found"
        report["evidenceLevel"] = "STRONG" if any(c["evidenceLevel"] == "STRONG" for c in report["candidates"]) else "SUPPORTED"
    elif report.get("unavailableUrls"):
        report["result"] = "provider_unavailable"
        report["reason"] = "official_provider_access_failed"
    elif report["warnings"]:
        report["reason"] = "no_valid_official_candidate"
    else:
        report["reason"] = "no_valid_official_candidate"
    return report


def discover_cli(tmdb_id: int, metadata_file: Path | None = None) -> dict:
    if tmdb_id <= 0:
        raise ValueError("TMDB ID must be positive")
    registry = json.loads(update.REGISTRY.read_text(encoding="utf-8"))
    update.validate_registry(registry)
    trusted = next((entry for entry in registry["series"] if entry["tmdbId"] == tmdb_id), None)
    if trusted:
        return discover({"tmdbId": tmdb_id, "title": trusted["title"]}, registry)
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8")) if metadata_file else tmdb_metadata(tmdb_id, local_tmdb_token())
    except (OSError, UnicodeError, ValueError, update.AutomationError) as exc:
        return {"tmdbId": tmdb_id, "provider": None, "result": "metadata_unavailable", "evidenceLevel": "INSUFFICIENT",
                "candidateUrls": [], "warnings": [str(exc)]}
    if metadata.get("tmdbId") != tmdb_id:
        raise ValueError("Metadata TMDB ID mismatch")
    return discover(metadata, registry)
