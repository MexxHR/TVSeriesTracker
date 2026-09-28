"""Read-only discovery of official provider-owned sources for unregistered TV shows.

TMDB is routing metadata only. No result from this module is a factual status.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import urljoin, urlparse

import update

PROVIDERS = update.ROOT / "official-data" / "discovery_providers.json"
MAX_SEEDS = 5
MAX_LINKS = 4


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
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


def slug(value: str) -> str:
    return normalize(value).replace(" ", "-")


def metadata_names(metadata: dict) -> list[str]:
    return list(dict.fromkeys(name for name in (metadata.get("title"), metadata.get("originalName")) if isinstance(name, str) and name.strip()))


def route_provider(metadata: dict, specs: dict) -> tuple[str | None, list[str], str | None]:
    signals: dict[str, list[str]] = {}
    for provider, spec in specs.items():
        names = {normalize(n) for n in spec["routingNames"]}
        for field in ("networks", "productionCompanies"):
            for item in metadata.get(field) or []:
                label = item.get("name", "") if isinstance(item, dict) else item
                if isinstance(label, str) and normalize(label) in names:
                    signals.setdefault(provider, []).append(f"{field}:{label}")
        homepage = metadata.get("homepage") or ""
        if isinstance(homepage, str) and update.allowed(homepage, spec["officialDomains"]):
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
        return "/shows/" in path and "/releases" in path and "release" in body
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


def discover(metadata: dict, registry: dict, fetcher=update.fetch, specs: dict | None = None) -> dict:
    specs = specs or provider_specs()
    tmdb_id = metadata.get("tmdbId")
    title = metadata.get("title")
    if not isinstance(tmdb_id, int) or tmdb_id <= 0 or not isinstance(title, str) or not title.strip():
        raise ValueError("Discovery requires positive tmdbId and nonempty title")
    report = {"tmdbId": tmdb_id, "title": title, "provider": None, "result": "insufficient_evidence",
              "evidenceLevel": "INSUFFICIENT", "candidateUrls": [], "candidates": [], "warnings": [], "attemptedOfficialUrls": []}
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
    pending = [(url, spec["mechanism"]) for url in seed_urls]
    seen = set()
    fetched_links = 0
    while pending and len(seen) < MAX_SEEDS + MAX_LINKS:
        url, method = pending.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if not update.allowed(url, domains):
            report["warnings"].append(f"Rejected non-official candidate URL: {url}")
            continue
        try:
            raw, final = fetcher(url, domains)
            if not update.allowed(final, domains):
                report["warnings"].append(f"Rejected redirect outside official domain: {url}")
                continue
            page = update.parse_page(raw)
        except (update.ProviderUnavailable, update.AutomationError, ValueError) as exc:
            report["warnings"].append(f"{provider} {url}: {exc}")
            if isinstance(exc, update.ProviderUnavailable) and "HTTP 404" not in str(exc):
                report.setdefault("unavailableUrls", []).append(url)
            continue
        is_article = method == "official article link"
        identity = branded_index_identity(page, names, provider) or (is_article and article_identity(page, names))
        structure = release_structure(final, page, provider)
        homepage_match = homepage_identity(page, metadata.get("homepage") or "", spec["officialDomains"])
        if not is_article and identity and structure and homepage_match and any(s.startswith("networks:") for s in signals):
            level = "STRONG"
        elif not is_article and identity and structure:
            level = "SUPPORTED"
        elif is_article and identity and not any(term in normalize(page.heading) for term in ("spinoff", "spin off", "remake")):
            level = "SUPPORTED"
        else:
            level = "INSUFFICIENT"
        candidate = {"candidateUrl": final, "candidateDomain": urlparse(final).hostname,
                     "discoveryMethod": method, "evidenceLevel": level, "identityValidated": identity,
                     "releaseStructure": structure, "homepageIdMatched": homepage_match,
                     "factualParserEligible": level == "STRONG"}
        if level != "INSUFFICIENT" and final not in report["candidateUrls"]:
            report["candidateUrls"].append(final)
            report["candidates"].append(candidate)
        # Link discovery is bounded and restricted to original provider pages.
        for link in page.links:
            if fetched_links >= MAX_LINKS:
                break
            linked = urljoin(final, link)
            if (linked not in seen and not any(pending_url == linked for pending_url, _ in pending)
                    and update.allowed(linked, domains) and article_link(linked, provider, names)):
                pending.append((linked, "official article link"))
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
