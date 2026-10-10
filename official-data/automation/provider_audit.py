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
                    or not isinstance(case.get("discoveryUrl"), str)
                    or not isinstance(case.get("articleUrl"), str)):
                raise ValueError(f"Invalid test case for {identifier}")
            for key in ("discoveryUrl", "articleUrl"):
                if not update.allowed(case[key], domains):
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
    parser.add_argument("--output", help="Optional path for the JSON report")
    parser.add_argument("--summary", action="store_true", help="Print concise human summary instead of JSON")
    args = parser.parse_args(argv)
    try:
        report = audit(load_registry(), args.provider)
        encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output:
            output = _safe_output_path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(encoded, encoding="utf-8")
        print(human_summary(report) if args.summary else encoded, end="" if args.summary else "")
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Provider audit error: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
