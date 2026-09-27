"""Conservative, standard-library-only Official Data updater. Run from repository root."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "official-data" / "official_series_data.json"
REGISTRY = ROOT / "official-data" / "sources.json"
AUDIT = ROOT / "official-data" / "history" / "changes.jsonl"
STATUSES = {"RENEWED", "RELEASE_DATE_CONFIRMED", "FINAL_SEASON", "CANCELED"}
INITIAL_IDS = {153312, 97951, 113962, 247718, 157741, 106379, 129552, 125988, 299167, 111803, 236235, 225891}
MONTHS = {name.lower(): i for i, name in enumerate(("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1)}
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
NUM = r"(?:\d{1,2}|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)"
SEASON = rf"(?:season\s+({NUM})|({NUM})\s+season)"


class AutomationError(Exception):
    pass


def allowed(url: str, domains: list[str]) -> bool:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and not p.username and not p.password and p.port in (None, 443) and any(host == d or host.endswith("." + d) for d in domains)


class SafeRedirect(HTTPRedirectHandler):
    def __init__(self, domains: list[str]):
        self.domains = domains

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(newurl, self.domains):
            raise AutomationError(f"Redirect outside allowlist: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, domains: list[str]) -> tuple[str, str]:
    if not allowed(url, domains):
        raise AutomationError(f"Source outside allowlist: {url}")
    opener = build_opener(SafeRedirect(domains))
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": "TVSeriesTrackerOfficialDataBot/2.3 (+https://github.com/MexxHR/TVSeriesTracker)", "Accept": "text/html"})
            with opener.open(req, timeout=12) as response:
                final = response.geturl()
                if not allowed(final, domains):
                    raise AutomationError(f"Final URL outside allowlist: {final}")
                payload = response.read(8_000_001)
                if len(payload) > 8_000_000:
                    raise AutomationError(f"Page too large: {final}")
                return payload.decode("utf-8", errors="replace"), final
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise AutomationError(f"HTTP {exc.code}: {url}") from exc
        except (URLError, TimeoutError) as exc:
            if attempt == 2:
                raise AutomationError(f"Network failure: {url}: {exc}") from exc
        time.sleep(1 + attempt * 2)
    raise AutomationError(f"Fetch failed: {url}")


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.heading = ""
        self.links: list[str] = []
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.link: str | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.stack.append(tag)
        if tag == "a":
            self.link = attrs.get("href")
            if self.link:
                self.links.append(self.link)
        if tag in ("p", "h1", "h2", "h3", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "a":
            self.link = None
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]

    def handle_data(self, data):
        if any(t in self.stack for t in ("script", "style", "nav", "footer")):
            return
        s = html.unescape(data).strip()
        if not s:
            return
        if "title" in self.stack:
            self.title += s + " "
        if "h1" in self.stack:
            self.heading += s + " "
        self.parts.append(s + " ")


def parse_page(raw: str) -> Page:
    page = Page()
    page.feed(raw)
    if not (page.heading.strip() or page.title.strip()):
        raise AutomationError("HTML parser found no title/heading")
    if re.search(r"sorry\s*-\s*not allowed|geographic location is not allowed|\baccess denied\b|\bcaptcha\b", (page.title + page.heading + "".join(page.parts[:30])), re.I):
        raise AutomationError("Official site returned access block/challenge")
    return page


class Adapter:
    name = ""
    source_name = ""

    def candidates(self, page: Page, base: str, entry: dict) -> list[str]:
        return []

    def parse(self, raw: str, url: str) -> dict:
        page = parse_page(raw)
        title = page.heading.strip() or page.title.strip()
        body = re.sub(r"[ \t]+", " ", "".join(page.parts))
        return {"title": title, "body": body, "url": url, "sourceName": self.source_name}


class ParamountAdapter(Adapter):
    name, source_name = "PARAMOUNT", "Paramount Press Express"

    def candidates(self, page, base, entry):
        return [urljoin(base, link) for link in page.links if "view=" in link and "/releases/" in urljoin(base, link)]


class NetflixAdapter(Adapter):
    name, source_name = "NETFLIX", "Netflix Tudum"

    def candidates(self, page, base, entry):
        slug = re.sub(r"[^a-z0-9]+", "-", entry["title"].lower()).strip("-")
        return [urljoin(base, link) for link in page.links if "/tudum/articles/" in link and slug in link]


class AppleAdapter(Adapter):
    name, source_name = "APPLE", "Apple TV Press"

    def candidates(self, page, base, entry):
        return [urljoin(base, link) for link in page.links if "/tv-pr/news/" in link and "silo" in link.lower()]


class WbdAdapter(Adapter):
    name, source_name = "WBD", "HBO / Warner Bros. Discovery Pressroom"

    def candidates(self, page, base, entry):
        return [urljoin(base, link) for link in page.links if "/media-release/" in link and "white-lotus" in link.lower()]


class AmazonAdapter(Adapter):
    name, source_name = "AMAZON", "Amazon"

    def candidates(self, page, base, entry):
        return [urljoin(base, link) for link in page.links if "/news/entertainment/" in link and "fallout" in link.lower()]


ADAPTERS = {a.name: a for a in (ParamountAdapter(), NetflixAdapter(), AppleAdapter(), WbdAdapter(), AmazonAdapter())}
PROVIDER_DOMAINS = {"PARAMOUNT": "paramountpressexpress.com", "NETFLIX": "netflix.com", "APPLE": "apple.com", "WBD": "press.wbd.com", "AMAZON": "aboutamazon.com"}


def number(value: str) -> int:
    return int(value) if value.isdigit() else ORDINALS[value.lower()]


def season_in(text: str) -> int | None:
    m = re.search(rf"\b({NUM})\s+and\s+final\s+season\b", text, re.I)
    if m:
        return number(m.group(1))
    m = re.search(SEASON, text, re.I)
    return number(next(g for g in m.groups() if g)) if m else None


def date_in(text: str, announced: dt.date | None) -> tuple[str | None, int | None]:
    m = re.search(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?\b", text, re.I)
    if m:
        year = int(m.group(3)) if m.group(3) else (announced.year if announced else None)
        if year:
            try:
                value = dt.date(year, MONTHS[m.group(1).lower()], int(m.group(2)))
            except ValueError:
                return None, None
            if announced and not m.group(3) and value < announced:
                value = value.replace(year=year + 1)
            return value.isoformat(), value.year
    m = re.search(r"\b(?:premieres?|returns?|debuts?|arrives?)\s+(?:in\s+)?(20\d{2})\b", text, re.I)
    return (None, int(m.group(1))) if m else (None, None)


def announcement_date(body: str) -> dt.date | None:
    m = re.search(r"\b(" + "|".join(MONTHS) + r")\s+\d{1,2},?\s+20\d{2}\b", body[:1500], re.I)
    if not m:
        return None
    d, _ = date_in(m.group(), None)
    return dt.date.fromisoformat(d) if d else None


def detect(article: dict, entry: dict, today: dt.date) -> list[dict]:
    # A sentence/title must itself identify the series and the season statement.
    heading = article["title"]
    body = article["body"][:25000]
    announced = announcement_date(body)
    chunks = [heading] + re.split(r"[\n.!?]+", body)
    aliases = [a.casefold() for a in entry["aliases"]]
    facts = []
    for chunk in chunks:
        s = re.sub(r"\s+", " ", chunk).strip()
        if not any(a in s.casefold() for a in aliases):
            continue
        season = season_in(s)
        if not season:
            continue
        status = None
        rule = None
        if re.search(rf"\b(?:{NUM}\s+and\s+final\s+season|season\s+{NUM}\s+.{0,18}final|final\s+season\s+{NUM})\b", s, re.I):
            status, rule = "FINAL_SEASON", "explicit-final-season"
        elif not re.search(r"\b(?:not|never|denies?|rumou?rs?|false)\b.{0,20}\b(?:cancelled|canceled)\b", s, re.I) and re.search(rf"\b(?:season\s+{NUM}|{NUM}\s+season)\b.{{0,35}}\b(?:cancelled|canceled|will not (?:return|continue))\b", s, re.I):
            status, rule = "CANCELED", "explicit-cancellation"
        elif re.search(rf"\b(?:renewed|greenlit|greenlighted|return(?:s|ing)?|coming back)\s+for\s+(?:a\s+)?(?:season\s+{NUM}|{NUM}\s+season)\b", s, re.I):
            status, rule = "RENEWED", "explicit-renewal"
        date, year = (None, None)
        if re.search(r"\b(?:premieres?|returns?|debuts?|arrives?|release date|coming)\b", s, re.I):
            date, year = date_in(s, announced)
            if date and dt.date.fromisoformat(date) < today:
                date, year = None, None
            if (date or year) and not status:
                status, rule = "RELEASE_DATE_CONFIRMED" if date else "RENEWED", "explicit-premiere"
        if not status:
            continue
        if status == "RELEASE_DATE_CONFIRMED" and not date:
            continue
        facts.append({"tmdbId": entry["tmdbId"], "title": entry["title"], "nextSeasonNumber": season,
                      "status": status, "releaseDate": date, "releaseYear": year,
                      "sourceName": article["sourceName"], "sourceUrl": article["url"],
                      "announcementDate": announced.isoformat() if announced else None, "rule": rule})
    return facts


def validate_registry(registry: dict):
    entries = registry.get("series")
    if not isinstance(entries, list) or len(entries) != 12:
        raise AutomationError("Expected exactly 12 registered series in V2.3")
    ids = [e.get("tmdbId") for e in entries]
    if len(set(ids)) != len(ids) or any(not isinstance(i, int) or i <= 0 for i in ids):
        raise AutomationError("Duplicate or missing tmdbId in registry")
    if set(ids) != INITIAL_IDS:
        raise AutomationError("V2.3 registry must contain the 12 existing TMDB TV IDs")
    for e in entries:
        if e.get("provider") not in ADAPTERS or e.get("allowedDomains") != [PROVIDER_DOMAINS.get(e.get("provider"))] or not e.get("title") or not e.get("aliases") or not allowed(e.get("officialUrl", ""), e["allowedDomains"]):
            raise AutomationError(f"Invalid registry entry: {e.get('title')}")


def validate_dataset(data: dict, registry: dict, old: dict | None = None):
    if data.get("schemaVersion") != 1 or not isinstance(data.get("series"), list):
        raise AutomationError("Unsupported schema")
    try:
        generated = dt.datetime.fromisoformat(data["generatedAt"].replace("Z", "+00:00"))
    except (ValueError, KeyError, TypeError) as exc:
        raise AutomationError("Invalid generatedAt") from exc
    previous_generated = dt.datetime.fromisoformat(old["generatedAt"].replace("Z", "+00:00")) if old else None
    if generated.tzinfo is None or (previous_generated and generated < previous_generated):
        raise AutomationError("generatedAt regression")
    known = {e["tmdbId"]: e for e in registry["series"]}
    ids = [r.get("tmdbId") for r in data["series"]]
    if len(ids) != len(set(ids)):
        raise AutomationError("Duplicate tmdbId")
    previous = {r["tmdbId"]: r for r in old["series"]} if old else {}
    for r in data["series"]:
        i = r.get("tmdbId")
        if i not in known or r.get("title") != known[i]["title"] or r.get("status") not in STATUSES:
            raise AutomationError(f"Unknown series/status: {i}")
        if not r.get("sourceName") or not r.get("sourceUrl") or not allowed(r["sourceUrl"], known[i]["allowedDomains"]):
            raise AutomationError(f"Invalid source: {i}")
        if not isinstance(r.get("nextSeasonNumber"), int) or r["nextSeasonNumber"] <= 0:
            raise AutomationError(f"Invalid season: {i}")
        if i in previous and r["nextSeasonNumber"] < previous[i]["nextSeasonNumber"]:
            raise AutomationError(f"Season regression: {i}")
        for key in ("releaseDate", "announcementDate", "lastChecked"):
            if r.get(key) is not None:
                try:
                    dt.date.fromisoformat(r[key])
                except (ValueError, TypeError) as exc:
                    raise AutomationError(f"Invalid {key}: {i}") from exc
        if not r.get("lastChecked") or (r["releaseDate"] and r.get("releaseYear") != dt.date.fromisoformat(r["releaseDate"]).year):
            raise AutomationError(f"Invalid date/year: {i}")
        if r["status"] == "RELEASE_DATE_CONFIRMED" and not r["releaseDate"]:
            raise AutomationError(f"Date status without date: {i}")
    if old and not set(previous).issubset(set(ids)):
        raise AutomationError("Destructive deletion")


def merge(old: dict | None, facts: list[dict], today: dt.date) -> tuple[dict | None, dict | None]:
    if not facts:
        return old, None
    priority = {"RENEWED": 1, "RELEASE_DATE_CONFIRMED": 2, "FINAL_SEASON": 3, "CANCELED": 4}
    facts = sorted(facts, key=lambda f: (f["nextSeasonNumber"], priority[f["status"]], f["announcementDate"] or ""), reverse=True)
    if old:
        facts = [f for f in facts if f["nextSeasonNumber"] >= old["nextSeasonNumber"]]
    if not facts:
        return old, None
    season = facts[0]["nextSeasonNumber"]
    same = [f for f in facts if f["nextSeasonNumber"] == season]
    if old and season == old["nextSeasonNumber"]:
        # Existing final/cancelled state is sticky; same-season date is independent.
        status = old["status"] if old["status"] in ("FINAL_SEASON", "CANCELED") else facts[0]["status"]
    else:
        status = same[0]["status"]
    dated = next((f for f in same if f["releaseDate"]), None)
    year_only = next((f for f in same if f["releaseYear"]), None)
    release_date = dated["releaseDate"] if dated else (old["releaseDate"] if old and season == old["nextSeasonNumber"] else None)
    release_year = int(release_date[:4]) if release_date else (year_only["releaseYear"] if year_only else (old["releaseYear"] if old and season == old["nextSeasonNumber"] else None))
    if status == "RELEASE_DATE_CONFIRMED" and not release_date:
        status = "RENEWED"
    if status == "RENEWED" and release_date:
        status = "RELEASE_DATE_CONFIRMED"
    source = dated if release_date and dated else same[0]
    result = {k: source.get(k) for k in ("tmdbId", "title", "nextSeasonNumber", "sourceName", "sourceUrl", "announcementDate")}
    result.update(status=status, releaseDate=release_date, releaseYear=release_year, lastChecked=today.isoformat())
    if old and all(result.get(k) == old.get(k) for k in ("nextSeasonNumber", "status", "releaseDate", "releaseYear")):
        return old, None
    return result, {"rule": source["rule"], "provider": source["sourceName"], "sourceUrl": source["sourceUrl"]}


def validate_facts(facts: list[dict], entry: dict):
    for fact in facts:
        if fact.get("tmdbId") != entry["tmdbId"] or fact.get("title") != entry["title"]:
            raise AutomationError(f"Mismatched evidence identity: {entry['title']}")
        if not allowed(fact.get("sourceUrl", ""), entry["allowedDomains"]) or not fact.get("sourceName"):
            raise AutomationError(f"Invalid evidence source: {entry['title']}")
        if fact.get("status") not in STATUSES or not isinstance(fact.get("nextSeasonNumber"), int):
            raise AutomationError(f"Invalid evidence status/season: {entry['title']}")
        if fact.get("releaseDate"):
            if fact.get("rule") != "explicit-premiere" and fact.get("status") != "FINAL_SEASON":
                raise AutomationError(f"Release date without premiere evidence: {entry['title']}")
            try:
                if dt.date.fromisoformat(fact["releaseDate"]).year != fact.get("releaseYear"):
                    raise ValueError("year mismatch")
            except ValueError as exc:
                raise AutomationError(f"Invalid evidence date: {entry['title']}") from exc
        if fact["status"] == "CANCELED" and fact.get("rule") != "explicit-cancellation":
            raise AutomationError(f"Cancellation without explicit evidence: {entry['title']}")


def collect(entry: dict, current: dict | None, today: dt.date, fetcher=fetch) -> tuple[list[dict], int]:
    adapter = ADAPTERS[entry["provider"]]
    raw, final = fetcher(entry["officialUrl"], entry["allowedDomains"])
    page = parse_page(raw)
    candidates = [entry["officialUrl"]] if entry["provider"] == "AMAZON" else []
    candidates += adapter.candidates(page, final, entry)
    if current:
        candidates.insert(0, current["sourceUrl"])
    # Bounded: existing evidence and two most recent listing links.
    unique = []
    for url in candidates:
        if allowed(url, entry["allowedDomains"]) and url not in unique:
            unique.append(url)
    articles = []
    for url in unique[:3]:
        if url == final:
            content, actual = raw, final
        else:
            content, actual = fetcher(url, entry["allowedDomains"])
        articles.append(adapter.parse(content, actual))
    if not articles:
        raise AutomationError(f"No release candidates: {entry['title']} ({adapter.name})")
    facts = [fact for article in articles for fact in detect(article, entry, today)]
    return facts, len(articles)


def run(dry_run: bool, collector=collect, today: dt.date | None = None) -> dict:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    validate_registry(registry)
    original = json.loads(DATA.read_text(encoding="utf-8"))
    validate_dataset(original, registry)
    current = {r["tmdbId"]: r for r in original["series"]}
    proposed = copy.deepcopy(original)
    changes = []
    failures = []
    validation_failures = []
    reports = []
    for entry in registry["series"]:
        old = current.get(entry["tmdbId"])
        try:
            facts, count = collector(entry, old, today)
            validate_facts(facts, entry)
            updated, evidence = merge(old, facts, today)
            reports.append({"title": entry["title"], "provider": entry["provider"], "articles": count, "facts": len(facts), "result": "change" if evidence else "unchanged"})
            if evidence:
                if old:
                    index = next(i for i, r in enumerate(proposed["series"]) if r["tmdbId"] == entry["tmdbId"])
                    proposed["series"][index] = updated
                else:
                    proposed["series"].append(updated)
                changes.append({"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "tmdbId": entry["tmdbId"], "title": entry["title"], "old": old, "new": updated, **evidence})
        except (AutomationError, ValueError, KeyError) as exc:
            failures.append(f"{entry['provider']}/{entry['title']}: {exc}")
            reports.append({"title": entry["title"], "provider": entry["provider"], "result": "failed", "reason": str(exc)})
    if len(changes) > 4:
        validation_failures.append(f"Mass change protection: {len(changes)} factual changes exceeds 4")
    if changes:
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        previous = dt.datetime.fromisoformat(original["generatedAt"].replace("Z", "+00:00"))
        if now <= previous:
            validation_failures.append("generatedAt regression")
        else:
            proposed["generatedAt"] = now.isoformat().replace("+00:00", "Z")
    try:
        validate_dataset(proposed, registry, original)
    except AutomationError as exc:
        validation_failures.append(str(exc))
    result = {"dryRun": dry_run, "reports": reports, "changes": changes, "failures": failures + validation_failures}
    if not dry_run and not validation_failures and changes:
        DATA.write_text(json.dumps(proposed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as log:
            for change in changes:
                log.write(json.dumps(change, ensure_ascii=False, separators=(",", ":")) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    output = run(args.dry_run)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    sys.exit(1 if output["failures"] else 0)
