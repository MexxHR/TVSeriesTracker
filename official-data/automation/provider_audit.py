"""Read-only audit of candidate first-party TV provider sources.

This module is intentionally outside the updater's provider registry and write
paths. It emits audit observations only; it cannot promote or persist facts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
from html.parser import HTMLParser
import http.client
import json
import os
from pathlib import Path
import re
import socket
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import update

ROOT = update.ROOT
REGISTRY = ROOT / "official-data" / "provider_audit_registry.json"
MAX_BYTES = 8_000_000
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 12
MAX_QUALIFICATION_DEPTH = 12
MAX_QUALIFICATION_PAGES = 20
MAX_QUALIFICATION_LINKS_PER_PAGE = 60
MAX_PROVIDER_QUALIFICATION_FETCHES = 80
RECOMMENDATIONS = (
    "READY_FOR_IMPLEMENTATION",
    "FETCHABLE_BUT_DISCOVERY_NEEDS_WORK",
    "BLOCKED_FROM_GITHUB_RUNNER",
    "INSUFFICIENT_OFFICIAL_EVIDENCE",
    "UNSUITABLE_STRUCTURE",
    "NEEDS_MORE_RESEARCH",
)
PROTECTED_PATHS = {
    ROOT / "official-data" / "official_series_data.json",
    ROOT / "official-data" / "history" / "changes.jsonl",
    ROOT / "official-data" / "sources.json",
    ROOT / "official-data" / "monitored_series.json",
    ROOT / "official-data" / "history" / "monitored_changes.jsonl",
}


class RedirectRejected(Exception):
    pass


class BoundedRedirect(HTTPRedirectHandler):
    def __init__(self, domains: list[str]):
        super().__init__()
        self.domains = domains
        self.count = 0
        self.statuses: list[int] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        self.statuses.append(int(code))
        if self.count > MAX_REDIRECTS or not update.allowed(newurl, self.domains):
            raise RedirectRejected("redirect_rejected")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def load_registry(path: Path = REGISTRY) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_registry(data)
    return data


def validate_registry(data: dict) -> None:
    if not isinstance(data, dict) or data.get("schemaVersion") != 1 or not isinstance(data.get("providers"), list):
        raise ValueError("Invalid provider audit registry schema")
    seen = set()
    for provider in data["providers"]:
        if not isinstance(provider, dict):
            raise ValueError("Invalid audit provider")
        identifier, domains, tests = provider.get("provider"), provider.get("officialDomains"), provider.get("tests")
        if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", identifier)
                or identifier in seen or not isinstance(domains, list) or not domains
                or any(not isinstance(d, str) or not re.fullmatch(r"(?:[a-z0-9-]+\.)*[a-z0-9-]+", d) for d in domains)
                or not isinstance(tests, list)):
            raise ValueError("Invalid provider identifier, domains, or tests")
        seen.add(identifier)
        for case in tests:
            if (not isinstance(case, dict) or not isinstance(case.get("series"), str)
                    or not isinstance(case.get("aliases"), list) or not case["aliases"]
                    or any(not isinstance(a, str) or not a.strip() for a in case["aliases"])
                    or not (isinstance(case.get("discoveryUrl"), str) or
                            isinstance(case.get("discoveryRoots"), list) and case["discoveryRoots"] and
                            all(isinstance(root, str) for root in case["discoveryRoots"]))
                    or not isinstance(case.get("articleUrl"), str)):
                raise ValueError(f"Invalid test case for {identifier}")
            urls = ([case["discoveryUrl"]] if isinstance(case.get("discoveryUrl"), str) else []) + \
                   list(case.get("discoveryRoots", [])) + [case["articleUrl"]]
            for url in urls:
                if not update.allowed(url, domains):
                    raise ValueError(f"Audit URL outside approved domain boundary: {identifier}")


def classify_failure(exc: Exception) -> tuple[str, int | None]:
    if isinstance(exc, HTTPError):
        code = int(exc.code)
        if code == 403:
            return "HTTP_403", code
        if code == 429:
            return "HTTP_429", code
        if code in (401, 407):
            return "AUTH_REQUIRED", code
        if 300 <= code < 400:
            return "REDIRECT_REJECTED", code
        return f"HTTP_{code}", code
    if isinstance(exc, RedirectRejected):
        return "REDIRECT_REJECTED", None
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "TIMEOUT", None
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "TIMEOUT", None
        if isinstance(reason, socket.gaierror):
            return "DNS_FAILURE", None
        return "NETWORK_FAILURE", None
    return "FETCH_ERROR", None


def _is_github_actions(explicit: bool | None) -> bool:
    if explicit is not None:
        return bool(explicit)
    return os.environ.get("GITHUB_ACTIONS", "").strip().casefold() == "true"


def _public_url(url: str | None) -> str | None:
    """Keep the observable origin/path while excluding query values and fragments."""
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{parsed.path}"


def fetch_url(url: str, domains: list[str]) -> tuple[dict, str | None]:
    """One bounded request, with no credentials and no retry loop."""
    base = {"status": "not_fetched", "httpStatus": None, "initialHttpStatus": None, "finalStatus": None,
            "finalUrl": None, "redirectCount": 0, "redirectsWithinBoundary": None,
            "contentType": None, "responseBytes": None, "failureCategory": None}
    if not update.allowed(url, domains):
        return {**base, "status": "rejected", "failureCategory": "OFFICIAL_DOMAIN_REJECTED"}, None
    redirects = BoundedRedirect(domains)
    opener = build_opener(redirects)
    request = Request(url, headers={"User-Agent": "TVSeriesTrackerProviderAudit/1.0", "Accept": "text/html"})
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            final = response.geturl()
            status = int(response.status)
            if not update.allowed(final, domains):
                return ({**base, "status": "rejected", "httpStatus": status,
                         "initialHttpStatus": redirects.statuses[0] if redirects.statuses else status, "finalStatus": status,
                         "finalUrl": None, "redirectCount": redirects.count,
                         "redirectsWithinBoundary": False, "failureCategory": "REDIRECT_REJECTED"}, None)
            content_type = response.headers.get_content_type().lower()
            payload = response.read(MAX_BYTES + 1)
            if len(payload) > MAX_BYTES:
                return ({**base, "status": "failed", "httpStatus": status,
                         "initialHttpStatus": redirects.statuses[0] if redirects.statuses else status, "finalStatus": status,
                         "finalUrl": _public_url(final), "redirectCount": redirects.count,
                         "redirectsWithinBoundary": True, "contentType": content_type,
                         "responseBytes": len(payload), "failureCategory": "RESPONSE_TOO_LARGE"}, None)
            if content_type not in ("text/html", "application/xhtml+xml"):
                return ({**base, "status": "failed", "httpStatus": status,
                         "initialHttpStatus": redirects.statuses[0] if redirects.statuses else status, "finalStatus": status,
                         "finalUrl": _public_url(final), "redirectCount": redirects.count,
                         "redirectsWithinBoundary": True, "contentType": content_type,
                         "responseBytes": len(payload), "failureCategory": "UNSUPPORTED_CONTENT"}, None)
            return ({**base, "status": "fetched", "httpStatus": status,
                     "initialHttpStatus": redirects.statuses[0] if redirects.statuses else status, "finalStatus": status,
                     "finalUrl": _public_url(final), "redirectCount": redirects.count,
                     "redirectsWithinBoundary": True, "contentType": content_type,
                     "responseBytes": len(payload)}, payload.decode("utf-8", errors="replace"))
    except HTTPError as exc:
        category, status = classify_failure(exc)
        final_url = exc.geturl()
        final_url = _public_url(final_url) if update.allowed(final_url, domains) else None
        return ({**base, "status": "blocked" if status in (403, 429) else "failed",
                 "httpStatus": status, "initialHttpStatus": redirects.statuses[0] if redirects.statuses else status,
                 "finalStatus": status, "finalUrl": final_url, "redirectCount": redirects.count,
                 "redirectsWithinBoundary": True, "failureCategory": category}, None)
    except (RedirectRejected, URLError, TimeoutError, socket.timeout, ConnectionError,
            http.client.HTTPException, OSError) as exc:
        category, status = classify_failure(exc)
        return ({**base, "status": "blocked" if category in ("TIMEOUT", "DNS_FAILURE") else "failed",
                 "httpStatus": status, "initialHttpStatus": redirects.statuses[0] if redirects.statuses else status,
                 "redirectCount": redirects.count,
                 "redirectsWithinBoundary": False if category == "REDIRECT_REJECTED" else None,
                 "failureCategory": category}, None)


class _PublishedDate(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.dates: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("property", attrs.get("name", "")).casefold() in (
                "article:published_time", "datepublished", "date_published", "pubdate"):
            self.dates.append(attrs.get("content", ""))
        elif tag == "time" and attrs.get("datetime"):
            self.dates.append(attrs["datetime"])


def publication_date(raw: str) -> str | None:
    parser = _PublishedDate()
    parser.feed(raw)
    dates = set()
    for value in parser.dates:
        try:
            dates.add(dt.date.fromisoformat(value[:10]).isoformat())
        except (TypeError, ValueError):
            pass
    return next(iter(dates)) if len(dates) == 1 else None


def _canonical_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}{parsed.path.rstrip('/')}?{parsed.query}"


def _content(raw: str, url: str, test: dict, today: dt.date | None = None) -> dict:
    try:
        page = update.parse_page(raw)
    except (update.AutomationError, ValueError, UnicodeError):
        preview = raw[:5000].casefold()
        blocker = ("WAF_BLOCKED" if any(term in preview for term in ("captcha", "access denied", "request blocked", "unusual traffic"))
                   else "GEO_BLOCKED" if any(term in preview for term in ("not available in your location", "geographic location is not allowed", "not available in your country"))
                   else None)
        return {"extractable": False, "pageTitle": None, "publicationDate": publication_date(raw),
                "signals": [], "seasonNumbers": [], "explicitRenewal": False,
                "explicitCancellation": False, "explicitFinalSeason": False,
                "explicitReleaseYear": False, "explicitReleaseDate": False,
                "factualParserProducedFactForFixture": False, "parserFactCount": 0,
                "blockClassification": blocker}
    heading = page.heading.strip() or page.title.strip()
    body = re.sub(r"\s+", " ", html.unescape("".join(page.parts)))[:25000]
    text = heading + "\n" + body
    season_matches = re.findall(rf"\bseason\s+({update.NUM})\b|\b({update.NUM})\s+season\b", text, re.I)
    seasons = sorted({update.number(a or b) for a, b in season_matches if a or b})
    renewal = bool(re.search(r"\b(?:renewed|greenlit|greenlighted)\b.{0,60}\bseason\b|\bseason\b.{0,60}\b(?:renewed|greenlit|greenlighted)\b", text, re.I))
    cancellation = bool(re.search(r"\b(?:canceled|cancelled)\b.{0,60}\bseason\b|\bseason\b.{0,60}\b(?:canceled|cancelled)\b", text, re.I))
    final_season = bool(re.search(r"\b(?:\d{1,2}(?:st|nd|rd|th)?\s+and\s+final\s+season|final\s+season(?:\s+\d{1,2})?)\b", text, re.I))
    release_date = bool(re.search(r"\b(?:premieres?|returns?|debuts?)\b.{0,90}\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:,?\s+20\d{2})?", text, re.I))
    release_year = bool(re.search(r"\b(?:premieres?|returns?|debuts?)\s+(?:in\s+)?20\d{2}\b", text, re.I))
    signals = [name for name, found in (("renewal", renewal), ("cancellation", cancellation),
               ("final_season", final_season), ("release_year", release_year),
               ("release_date", release_date)) if found]
    article = {"title": heading, "body": body, "url": url,
               "sourceName": "Provider coverage audit",
               "publicationDate": publication_date(raw)}
    entry = {"tmdbId": 1, "title": test["series"], "aliases": test["aliases"]}
    try:
        audit_date = today or dt.datetime.now(dt.timezone.utc).date()
        facts = update.detect(article, entry, audit_date, strict_binding=True)
    except (update.AutomationError, ValueError, UnicodeError):
        facts = []
    scan = (heading + " " + body[:1000]).casefold()
    if any(term in scan for term in ("captcha", "access denied", "request blocked", "unusual traffic")):
        blocker = "WAF_BLOCKED"
    elif any(term in scan for term in ("not available in your location", "geographic location is not allowed", "not available in your country")):
        blocker = "GEO_BLOCKED"
    else:
        blocker = None
    return {"extractable": bool(heading and body), "pageTitle": heading[:300],
            "publicationDate": article["publicationDate"], "signals": signals,
            "seasonNumbers": seasons, "explicitRenewal": renewal,
            "explicitCancellation": cancellation, "explicitFinalSeason": final_season,
            "explicitReleaseYear": release_year, "explicitReleaseDate": release_date,
            "factualParserProducedFactForFixture": bool(facts), "parserFactCount": len(facts),
            "blockClassification": blocker}


def _linked(index_raw: str, index_url: str, article_url: str, domains: list[str]) -> bool:
    try:
        page = update.parse_page(index_raw)
    except (update.AutomationError, ValueError, UnicodeError):
        return False
    expected = _canonical_url(article_url)
    for href in page.links:
        absolute = urljoin(index_url, href)
        if update.allowed(absolute, domains) and _canonical_url(absolute) == expected:
            return True
    return False


def audit_provider(provider: dict, fetcher=fetch_url, github_actions: bool | None = None,
                   today: dt.date | None = None) -> dict:
    domains = provider["officialDomains"]
    tests, any_fetchable, any_linked, any_parser, any_combined = [], False, False, False, False
    is_github_actions = _is_github_actions(github_actions)
    article_failures = []
    article_outcomes = []
    for case in provider["tests"]:
        index_fetch, index_raw = fetcher(case["discoveryUrl"], domains)
        article_fetch, article_raw = (index_fetch, index_raw) if _canonical_url(case["discoveryUrl"]) == _canonical_url(case["articleUrl"]) else fetcher(case["articleUrl"], domains)
        index_fetchable = index_fetch["status"] == "fetched"
        article_fetchable = article_fetch["status"] == "fetched"
        linked = bool(index_raw and index_fetchable and _linked(index_raw, index_fetch["finalUrl"] or case["discoveryUrl"], case["articleUrl"], domains))
        content = _content(article_raw, article_fetch["finalUrl"] or case["articleUrl"], case, today) if article_raw else {
            "extractable": False, "pageTitle": None, "publicationDate": None, "signals": [],
            "seasonNumbers": [], "explicitRenewal": False, "explicitCancellation": False,
            "explicitFinalSeason": False, "explicitReleaseYear": False, "explicitReleaseDate": False,
            "factualParserProducedFactForFixture": False, "parserFactCount": 0,
            "blockClassification": None}
        any_fetchable |= index_fetchable or article_fetchable
        any_linked |= linked
        any_parser |= bool(content["factualParserProducedFactForFixture"])
        combined = bool(linked and content["factualParserProducedFactForFixture"])
        any_combined |= combined
        if article_fetch.get("failureCategory"):
            article_failures.append(article_fetch["failureCategory"])
        content_block = content.get("blockClassification")
        article_outcomes.append(article_fetch.get("failureCategory") or content_block)
        tests.append({"series": case["series"], "discoveryUrl": _public_url(case["discoveryUrl"]),
                      "articleUrl": _public_url(case["articleUrl"]), "fetch": {"discovery": index_fetch, "article": article_fetch},
                      "discovery": {"linkedFromOfficialIndex": linked,
                                    "linkedAndParserFactForThisCase": combined}, "content": content})
    if not provider["tests"]:
        recommendation = "NEEDS_MORE_RESEARCH"
    elif is_github_actions and article_outcomes and all(
            x in ("HTTP_403", "HTTP_429", "WAF_BLOCKED", "GEO_BLOCKED")
            for x in article_outcomes):
        recommendation = "BLOCKED_FROM_GITHUB_RUNNER"
    elif any_combined:
        # One current probe does not establish robust discovery across an ecosystem.
        recommendation = "FETCHABLE_BUT_DISCOVERY_NEEDS_WORK"
    elif article_failures and not is_github_actions and not any_parser:
        recommendation = "NEEDS_MORE_RESEARCH"
    elif any_fetchable and not any_parser:
        recommendation = "INSUFFICIENT_OFFICIAL_EVIDENCE"
    elif any_fetchable:
        recommendation = "FETCHABLE_BUT_DISCOVERY_NEEDS_WORK"
    else:
        recommendation = "NEEDS_MORE_RESEARCH"
    return {"provider": provider["provider"], "recommendation": recommendation,
            "officialDomains": sorted(domains), "tests": tests,
            "fetchable": any_fetchable, "articleDiscovered": bool(any_linked),
            "discoveryViable": bool(any_combined),
            "factualParserProducedFactForFixture": bool(any_parser), "notes": list(provider.get("notes", []))}


def audit(registry: dict, provider_id: str | None = None, *, fetcher=fetch_url,
          github_actions: bool | None = None, today: dt.date | None = None) -> dict:
    validate_registry(registry)
    providers = registry["providers"]
    if provider_id:
        providers = [item for item in providers if item["provider"] == provider_id.upper()]
        if not providers:
            raise ValueError(f"Unknown audit provider: {provider_id}")
    is_github_actions = _is_github_actions(github_actions)
    results = sorted((audit_provider(item, fetcher, is_github_actions, today) for item in providers), key=lambda row: row["provider"])
    summary = {name: [row["provider"] for row in results if row["recommendation"] == name]
               for name in RECOMMENDATIONS}
    return {"schemaVersion": 1,
            "environment": {"githubActions": is_github_actions},
            "providers": results, "summary": summary}


def _case_roots(case: dict) -> list[str]:
    roots = case.get("discoveryRoots")
    if roots is None:
        roots = [case["discoveryUrl"]] if case.get("discoveryUrl") else []
    if not isinstance(roots, list) or any(not isinstance(url, str) for url in roots):
        return []
    return sorted(set(roots))


class _QualificationLinks(HTMLParser):
    """Extract visible first-party links while excluding site chrome."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._anchor: dict | None = None
        self._tag_stack: list[tuple[str, bool]] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class", "") + " " + attrs.get("id", "")).casefold()
        skipped = tag in ("head", "nav", "footer", "aside", "script", "style") or any(
            marker in classes for marker in ("related", "recommend", "carousel", "breadcrumb", "site-footer"))
        self._tag_stack.append((tag, skipped))
        # Archive pagination commonly sits inside <nav>. Its page links are
        # structural discovery links, unlike general site navigation.
        pagination = bool(re.search(r"/page/\d+/?(?:[?#].*)?$", attrs.get("href", "")))
        if tag == "a" and attrs.get("href") and (pagination or not any(flag for _, flag in self._tag_stack)):
            self._anchor = {"href": attrs["href"], "text": []}

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            self.links.append((self._anchor["href"], " ".join(self._anchor["text"])))
            self._anchor = None
        if any(existing == tag for existing, _ in self._tag_stack):
            index = len(self._tag_stack) - 1 - [existing for existing, _ in self._tag_stack[::-1]].index(tag)
            self._tag_stack = self._tag_stack[:index]

    def handle_data(self, data):
        if self._anchor is not None and not any(flag for _, flag in self._tag_stack):
            self._anchor["text"].append(data.strip())


class _QualificationText(HTMLParser):
    """Collect visible block text with paragraph boundaries preserved."""
    BLOCKS = {"p", "h1", "h2", "h3", "h4", "li", "blockquote", "div", "section", "article"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.current: list[str] = []
        self.tag_stack: list[tuple[str, bool]] = []

    def _flush(self):
        value = re.sub(r"\s+", " ", " ".join(self.current)).strip()
        if value:
            self.blocks.append(value)
        self.current = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class", "") + " " + attrs.get("id", "")).casefold()
        skipped = tag in ("head", "nav", "footer", "aside", "script", "style") or any(
            marker in classes for marker in ("related", "recommend", "carousel", "breadcrumb", "site-footer"))
        self.tag_stack.append((tag, skipped))
        if tag in self.BLOCKS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self._flush()
        if any(existing == tag for existing, _ in self.tag_stack):
            index = len(self.tag_stack) - 1 - [existing for existing, _ in self.tag_stack[::-1]].index(tag)
            self.tag_stack = self.tag_stack[:index]

    def handle_data(self, data):
        if not any(flag for _, flag in self.tag_stack):
            self.current.append(html.unescape(data))

    def text(self) -> str:
        self._flush()
        return "\n".join(self.blocks)


def _qualification_links(raw: str, base_url: str, domains: list[str], aliases: list[str], target_url: str) -> list[str]:
    parser = _QualificationLinks()
    parser.feed(raw)
    stop_words = {"the", "and", "for", "with", "from", "that", "this", "series", "show"}
    alias_words = [{word for word in re.findall(r"[a-z0-9]+", alias.casefold())
                    if len(word) > 2 and word not in stop_words} for alias in aliases]
    alias_phrases = [re.sub(r"[^a-z0-9]+", " ", alias.casefold()).strip() for alias in aliases]
    scored = []
    for href, anchor in parser.links:
        absolute = urljoin(base_url, href)
        if not update.allowed(absolute, domains):
            continue
        if _canonical_url(absolute) == _canonical_url(target_url):
            scored.append((3, absolute))
            continue
        text = (anchor + " " + urlparse(absolute).path).casefold()
        normalized = re.sub(r"[^a-z0-9]+", " ", text)
        normalized_words = set(normalized.split())
        relevant = any(phrase and phrase in normalized for phrase in alias_phrases)
        for words in alias_words:
            threshold = max(1, (len(words) + 1) // 2)
            relevant |= bool(words and len(words & normalized_words) >= threshold)
        path = urlparse(absolute).path.casefold()
        segments = [part for part in path.split("/") if part]
        listing_names = {"news", "press", "press-", "press-releases", "releases", "headlines", "media", "newsroom", "archive"}
        current_path = urlparse(base_url).path.rstrip("/")
        archive_path = re.sub(r"/page/\d+$", "", current_path)
        pagination = re.fullmatch(re.escape(archive_path) + r"/page/(\d+)/?", path)
        listing = bool(segments and ((segments[-1] in listing_names and
                       (archive_path in ("", "/") or path.startswith(archive_path))) or
                       (pagination and int(pagination.group(1)) <= 12)))
        if relevant or listing:
            scored.append((2 if relevant else 1, absolute))
    return [url for _, url in sorted(set(scored), key=lambda item: (-item[0], item[1]))]


def _budget_fetch(fetcher, budget: dict, url: str, domains: list[str]):
    if budget["remaining"] <= 0:
        return ({"status": "not_attempted", "httpStatus": None, "finalUrl": None,
                 "failureCategory": "QUALIFICATION_FETCH_BUDGET_EXHAUSTED"}, None)
    budget["remaining"] -= 1
    return fetcher(url, domains)


def _qualification_candidates(case: dict, domains: list[str], fetcher, budget: dict) -> tuple[list[dict], list[dict]]:
    """Walk only bounded, linked first-party pages; never seed from articleUrl."""
    target = _canonical_url(case["articleUrl"])
    queue: list[tuple[str, int]] = []
    for root in _case_roots(case):
        if _canonical_url(root) == target or not update.allowed(root, domains):
            continue
        queue.append((root, 0))
    seen: set[str] = set()
    fetches: list[dict] = []
    found: list[dict] = []
    while queue and len(seen) < MAX_QUALIFICATION_PAGES:
        current, depth = queue.pop(0)
        key = _canonical_url(current)
        if key in seen:
            continue
        seen.add(key)
        result, raw = _budget_fetch(fetcher, budget, current, domains)
        fetches.append({"url": _public_url(current), "fetch": result, "depth": depth})
        if not raw or result.get("status") != "fetched":
            continue
        final_url = result.get("finalUrl") or current
        if not update.allowed(final_url, domains):
            continue
        if key == target or _canonical_url(final_url) == target:
            found.append({"url": current, "finalUrl": final_url, "fetch": result, "raw": raw, "depth": depth})
            break
        if depth >= MAX_QUALIFICATION_DEPTH:
            continue
        links = _qualification_links(raw, final_url, domains, case.get("aliases") or [case["series"]], case["articleUrl"])
        direct = [link for link in links if _canonical_url(link) == target]
        next_links = [(link, depth + 1) for link in direct + [link for link in links if link not in direct][:MAX_QUALIFICATION_LINKS_PER_PAGE]
                      if _canonical_url(link) not in seen]
        queue[0:0] = next_links
    return fetches, found


def _qualification_content(raw: str, url: str, case: dict, today: dt.date | None = None) -> dict:
    """Use the production factual parser in strict mode, returning summaries only."""
    try:
        page = update.parse_page(raw)
        heading = page.heading.strip() or page.title.strip()
        text = _QualificationText()
        text.feed(raw)
        body = text.text()[:25000]
        published = publication_date(raw)
        article = {"title": heading, "body": body, "url": url,
                   "sourceName": "Provider discovery qualification", "publicationDate": published}
        entry = {"tmdbId": int(case.get("tmdbId", 1)), "title": case["series"],
                 "aliases": case.get("aliases") or [case["series"]]}
        facts = update.detect(article, entry, today or dt.datetime.now(dt.timezone.utc).date(), strict_binding=True)
    except (update.AutomationError, ValueError, UnicodeError, TypeError):
        return {"extractable": False, "pageTitle": None, "facts": [], "signals": [],
                "parserFactCount": 0, "blockClassification": None}
    # Broad signals are diagnostics only. They are never accepted as target facts.
    scan = (heading + " " + body).casefold()
    signals = [name for name, pattern in (
        ("renewal", r"\b(?:renewed|greenlit|greenlighted)\b"),
        ("final_season", r"\bfinal season\b"),
        ("cancellation", r"\b(?:cancelled|canceled)\b"),
        ("release_date", r"\b(?:premieres?|returns?|debuts?)\b.{0,100}\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b"),
        ("release_year", r"\b(?:premieres?|returns?|debuts?)\s+(?:in\s+)?20\d{2}\b")) if re.search(pattern, scan, re.I)]
    summarized = [{"factType": fact.get("factType", "LIFECYCLE"), "status": fact.get("status"),
                   "season": fact.get("nextSeasonNumber"), "releaseDate": fact.get("releaseDate"),
                   "releaseYear": fact.get("releaseYear")} for fact in facts]
    unique_summaries = {json.dumps(row, sort_keys=True): row for row in summarized}
    summarized = [unique_summaries[key] for key in sorted(unique_summaries)]
    blocker = "WAF_BLOCKED" if any(x in scan for x in ("captcha", "access denied", "request blocked", "unusual traffic")) else None
    if not blocker and any(x in scan for x in ("not available in your location", "not available in your country")):
        blocker = "GEO_BLOCKED"
    return {"extractable": bool(heading and body), "pageTitle": heading[:300], "facts": summarized,
            "signals": signals, "parserFactCount": len(summarized), "blockClassification": blocker}


def _expected_facts_match(content: dict, case: dict) -> tuple[bool, str | None]:
    expected = case.get("expected") or {}
    facts = content["facts"]
    kind = case.get("kind", "positive")
    markers = expected.get("titleMarkers", [])
    if not isinstance(markers, list) or any(not isinstance(marker, str) or not marker.strip() for marker in markers):
        return False, "INVALID_EXPECTATION"
    title = (content.get("pageTitle") or "").casefold()
    if any(marker.casefold() not in title for marker in markers):
        return False, "ARTICLE_IDENTITY_MISMATCH"
    parser_outcome = expected.get("parserOutcome")
    if parser_outcome not in (None, "facts", "no_facts"):
        return False, "INVALID_EXPECTATION"
    if parser_outcome == "facts" and not facts:
        return False, "EXPECTED_FACT_MISSING"
    if parser_outcome == "no_facts" and facts:
        return False, "UNEXPECTED_FACT"
    if kind == "negative":
        return (not facts, None if not facts else "UNEXPECTED_FACT")
    fact_types = expected.get("factTypes", [])
    if not isinstance(fact_types, list):
        return False, "INVALID_EXPECTATION"
    if kind == "ambiguity" and not fact_types:
        return False, "INVALID_EXPECTATION"
    target_facts = [fact for fact in facts if not fact_types or fact["factType"] in fact_types]
    if not target_facts:
        return False, "EXPECTED_FACT_MISSING"
    season = expected.get("season")
    if season is not None and any(fact["season"] != season for fact in target_facts):
        return False, "CROSS_SEASON_FACT"
    statuses = expected.get("statuses")
    if expected.get("status"):
        statuses = [expected["status"]]
    if statuses and not any(fact["status"] in statuses for fact in target_facts):
        return False, "EXPECTED_STATUS_MISSING"
    if statuses and any(fact["factType"] == "LIFECYCLE" and fact["status"] not in statuses
                        for fact in target_facts):
        return False, "CONFLICTING_LIFECYCLE_STATUS"
    for key, actual_key in (("releaseDate", "releaseDate"), ("releaseYear", "releaseYear")):
        if expected.get(key) is not None and not any(fact[actual_key] == expected[key] for fact in target_facts):
            return False, f"EXPECTED_{key.upper()}_MISSING"
    forbidden = set(expected.get("forbiddenStatuses", []))
    if forbidden.intersection(fact["status"] for fact in facts):
        return False, "FORBIDDEN_STATUS_FOUND"
    # Safety/ambiguity cases reject extra facts: they must all match the expected season and types.
    if kind == "ambiguity" and any(fact not in target_facts for fact in facts):
        return False, "UNRELATED_FACT_CONTAMINATION"
    if kind == "ambiguity":
        allowed_statuses = set(statuses or [])
        if expected.get("status"):
            allowed_statuses = {expected["status"]}
        if allowed_statuses and any(fact["status"] not in allowed_statuses for fact in target_facts):
            return False, "UNEXPECTED_STATUS"
    return True, None


def qualify_provider(provider: dict, fetcher=fetch_url, *, github_actions: bool | None = None,
                     today: dt.date | None = None) -> dict:
    """Read-only multi-case discovery qualification; local results are candidates only."""
    domains = sorted(provider["officialDomains"])
    reports = []
    budget = {"remaining": MAX_PROVIDER_QUALIFICATION_FETCHES}
    for index, case in enumerate(provider.get("tests", [])):
        case_id = case.get("caseId", f"{provider['provider']}-{index + 1:02d}")
        roots = _case_roots(case)
        roots_valid = bool(roots) and all(update.allowed(root, domains) and
                                          _canonical_url(root) != _canonical_url(case["articleUrl"]) for root in roots)
        if not roots_valid:
            traversal, found = [], []
            failure = "INVALID_OR_MISSING_DISCOVERY_ROOT"
        else:
            traversal, found = _qualification_candidates(case, domains, fetcher, budget)
            failure = None
        discovered = bool(found)
        content = (_qualification_content(found[0]["raw"], found[0]["finalUrl"], case, today)
                   if found else {"extractable": False, "pageTitle": None, "facts": [], "signals": [],
                                  "parserFactCount": 0, "blockClassification": None})
        if found:
            known_fetch, known_raw, known_source = found[0]["fetch"], found[0]["raw"], "discovered"
        elif update.allowed(case["articleUrl"], domains):
            known_fetch, known_raw = _budget_fetch(fetcher, budget, case["articleUrl"], domains)
            known_source = "diagnostic_only"
        else:
            known_fetch, known_raw, known_source = ({"status": "rejected", "httpStatus": None,
                "failureCategory": "OFFICIAL_DOMAIN_REJECTED", "finalUrl": None}, None, "diagnostic_only")
        known_final = known_fetch.get("finalUrl") or case["articleUrl"]
        known_safe = bool(known_raw and known_fetch.get("status") == "fetched" and update.allowed(known_final, domains))
        known_content = (_qualification_content(known_raw, known_final, case, today) if known_safe else
                         {"extractable": False, "pageTitle": None, "facts": [], "signals": [],
                          "parserFactCount": 0, "blockClassification": None})
        known_expected_ok, known_expected_failure = (_expected_facts_match(known_content, case) if known_safe else (False, None))
        expected_ok, expected_failure = _expected_facts_match(content, case) if discovered else (False, None)
        kind = case.get("kind", "positive")
        safety_ok = bool(discovered and content["extractable"] and expected_ok and not content.get("blockClassification"))
        if not failure:
            if not discovered:
                failure = next((row["fetch"].get("failureCategory") for row in traversal if row["fetch"].get("failureCategory")), "KNOWN_ARTICLE_NOT_DISCOVERED")
            elif not content["extractable"]:
                failure = "ARTICLE_NOT_EXTRACTABLE"
            elif expected_failure:
                failure = expected_failure
            elif content.get("blockClassification"):
                failure = content["blockClassification"]
        case_report = {"caseId": case_id, "kind": kind, "series": case["series"],
                       "rootScope": case.get("rootScope", "provider"),
                       "sourceStructure": case.get("sourceStructure", "announcement"),
                       "articleUrl": _public_url(case["articleUrl"]), "discoveryRoots": [_public_url(x) for x in roots],
                       "expected": {key: value for key, value in (case.get("expected") or {}).items()
                                    if key in {"factTypes", "status", "statuses", "season", "releaseDate", "releaseYear",
                                               "forbiddenStatuses", "titleMarkers", "parserOutcome"}},
                       "discoveredFromRoot": discovered,
                       "discoveredUrl": _public_url(found[0]["url"]) if found else None,
                       "discoveredFinalUrl": _public_url(found[0]["finalUrl"]) if found else None,
                       "redirects": ({"count": found[0]["fetch"].get("redirectCount", 0),
                                      "withinOfficialBoundary": found[0]["fetch"].get("redirectsWithinBoundary")}
                                     if found else None),
                       "http": [{"url": item["url"], "status": item["fetch"].get("status"),
                                 "httpStatus": item["fetch"].get("httpStatus"),
                                 "failureCategory": item["fetch"].get("failureCategory"), "depth": item["depth"]}
                                for item in traversal],
                       "knownArticleDiagnostic": {"source": known_source, "fetchable": known_safe,
                           "httpStatus": known_fetch.get("httpStatus"),
                           "failureCategory": known_fetch.get("failureCategory"),
                           "parserCompatible": bool(known_safe and known_content["extractable"] and known_expected_ok),
                           "parserFailureCategory": known_expected_failure,
                           "content": known_content},
                       "content": content, "parserCompatible": bool(content["parserFactCount"]),
                       "safetyPassed": safety_ok, "passed": safety_ok, "failureCategory": failure}
        reports.append(case_report)
    reports.sort(key=lambda row: row["caseId"])
    positives = [row for row in reports if row["kind"] == "positive"]
    qualifying_positives = [row for row in positives if row["passed"] and
                            row["rootScope"] == "provider" and row["sourceStructure"] == "announcement"]
    distinct_positive_series = {re.sub(r"\W+", " ", row["series"].casefold()).strip()
                                for row in qualifying_positives}
    distinct_positive_articles = {row["articleUrl"] for row in qualifying_positives}
    all_cases_pass = bool(reports) and all(row["passed"] for row in reports)
    multi_case_pass = len(distinct_positive_series) >= 3 and len(distinct_positive_articles) >= 3 and all_cases_pass
    candidate = multi_case_pass
    github = _is_github_actions(github_actions)
    blocked_codes = {"HTTP_403", "HTTP_429"}
    all_cases_runner_blocked = bool(reports) and all(
        not row["discoveredFromRoot"] and row["failureCategory"] != "INVALID_OR_MISSING_DISCOVERY_ROOT" and
        row["knownArticleDiagnostic"]["failureCategory"] in blocked_codes
        for row in reports)
    safety_failures = {"ARTICLE_IDENTITY_MISMATCH", "UNRELATED_FACT_CONTAMINATION", "CROSS_SEASON_FACT",
                       "FORBIDDEN_STATUS_FOUND", "CONFLICTING_LIFECYCLE_STATUS", "UNEXPECTED_STATUS", "UNEXPECTED_FACT"}
    ambiguity_failed = (any(row["kind"] in ("negative", "ambiguity") and not row["passed"] and
                            (row["discoveredFromRoot"] or row["knownArticleDiagnostic"]["fetchable"] and
                             row["knownArticleDiagnostic"]["content"]["extractable"])
                            for row in reports) or
                        any(row["failureCategory"] in safety_failures and row["discoveredFromRoot"]
                            for row in reports))
    parser_failed = any(row["kind"] == "positive" and row["discoveredFromRoot"] and not row["passed"]
                        for row in reports)
    if github and all_cases_runner_blocked:
        recommendation = "BLOCKED_FROM_GITHUB_RUNNER"
    elif ambiguity_failed:
        recommendation = "AMBIGUITY_SAFETY_NEEDS_WORK"
    elif parser_failed:
        recommendation = "PARSER_COMPATIBILITY_NEEDS_WORK"
    elif candidate and github:
        recommendation = "QUALIFIED_ON_GITHUB_RUNNER"
    elif candidate:
        recommendation = "LOCAL_QUALIFICATION_CANDIDATE"
    elif any(row["discoveredFromRoot"] for row in reports) or any(row["knownArticleDiagnostic"]["fetchable"] for row in reports):
        recommendation = "FETCHABLE_BUT_DISCOVERY_NEEDS_WORK"
    else:
        recommendation = "INSUFFICIENT_OFFICIAL_EVIDENCE"
    return {"provider": provider["provider"], "recommendation": recommendation,
            "officialDomains": domains, "caseCount": len(reports),
            "passedCaseCount": sum(row["passed"] for row in reports),
            "failedCaseCount": sum(not row["passed"] for row in reports),
            "fetchable": any(row["knownArticleDiagnostic"]["fetchable"] or
                              any(item["status"] == "fetched" for item in row["http"]) for row in reports),
            "discoveryViable": any(row["discoveredFromRoot"] for row in reports),
            "multiCaseDiscoveryPassed": multi_case_pass,
            "parserCompatibility": all(row["parserCompatible"] for row in positives) if positives else False,
            "ambiguitySafety": all(row["safetyPassed"] for row in reports if row["kind"] in ("negative", "ambiguity")),
            "qualificationCandidate": candidate, "qualificationReady": bool(candidate and github),
            "environment": {"githubActions": github}, "tests": reports}


def qualify(registry: dict, provider_id: str | None = None, *, fetcher=fetch_url,
            github_actions: bool | None = None, today: dt.date | None = None) -> dict:
    validate_registry(registry)
    providers = registry["providers"]
    if provider_id:
        providers = [item for item in providers if item["provider"] == provider_id.upper()]
        if not providers:
            raise ValueError(f"Unknown audit provider: {provider_id}")
    else:
        # V2.7.1 qualification scope is explicit. Existing audit-only probes for
        # Peacock, Starz, and WBD remain available through the original audit.
        targets = {"FX", "HULU", "DISNEY_PLUS", "AMC"}
        providers = [item for item in providers if item["provider"] in targets]
    github = _is_github_actions(github_actions)
    rows = sorted((qualify_provider(item, fetcher, github_actions=github, today=today) for item in providers),
                  key=lambda row: row["provider"])
    return {"schemaVersion": 1, "reportType": "provider_discovery_qualification",
            "environment": {"githubActions": github}, "providers": rows}


def human_summary(report: dict) -> str:
    lines = ["Provider coverage audit (read-only)"]
    for row in report["providers"]:
        lines.append(f"{row['provider']}: {row['recommendation']} | fetchable={str(row['fetchable']).lower()} | "
                     f"discoverable={str(row['discoveryViable']).lower()} | parserFactFound={str(row['factualParserProducedFactForFixture']).lower()}")
    return "\n".join(lines)


def _safe_output_path(value: str) -> Path:
    path = Path(value).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError:
        return path
    raise ValueError("Report output must be outside the repository")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--all", action="store_true", help="Audit every configured provider")
    choice.add_argument("--provider", metavar="ID", help="Audit one stable provider ID")
    parser.add_argument("--qualify", action="store_true", help="Run bounded multi-case discovery qualification")
    parser.add_argument("--output", help="Optional path for the JSON report")
    parser.add_argument("--summary", action="store_true", help="Print concise human summary instead of JSON")
    args = parser.parse_args(argv)
    try:
        registry = load_registry()
        report = (qualify(registry, args.provider) if args.qualify else audit(registry, args.provider))
        encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output:
            output = _safe_output_path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(encoded, encoding="utf-8")
        if args.summary and args.qualify:
            lines = ["Provider discovery qualification (read-only)"]
            lines.extend(f"{row['provider']}: {row['recommendation']} | cases={row['passedCaseCount']}/{row['caseCount']} | "
                         f"multiCase={str(row['multiCaseDiscoveryPassed']).lower()} | ready={str(row['qualificationReady']).lower()}"
                         for row in report["providers"])
            print("\n".join(lines))
        else:
            print(human_summary(report) if args.summary else encoded, end="" if args.summary else "")
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Provider audit error: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
