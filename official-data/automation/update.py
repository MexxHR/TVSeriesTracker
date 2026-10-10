"""Conservative, standard-library-only Official Data updater. Run from repository root."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import html
from html.parser import HTMLParser
from http.client import BadStatusLine, IncompleteRead, RemoteDisconnected
import json
import os
from pathlib import Path
import re
import sys
import tempfile
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
MONTHS.update({name[:3]: value for name, value in list(MONTHS.items())})
MONTH_PATTERN = "|".join(sorted(MONTHS, key=len, reverse=True))
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
CARDINALS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
NUM = r"(?:\d{1,2}|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|one|two|three|four|five|six|seven|eight|nine|ten)"
SEASON = rf"(?:\bseason\s+({NUM})\b|\b({NUM})\s+season\b)"


class AutomationError(Exception):
    pass


class ProviderUnavailable(AutomationError):
    """An official endpoint could not be reached or returned an access block."""


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


def fetch(url: str, domains: list[str], headers: dict[str, str] | None = None) -> tuple[str, str]:
    if not allowed(url, domains):
        raise AutomationError(f"Source outside allowlist: {url}")
    opener = build_opener(SafeRedirect(domains))
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": "TVSeriesTrackerOfficialDataBot/2.3.2 (+https://github.com/MexxHR/TVSeriesTracker)", "Accept": "text/html", **(headers or {})})
            with opener.open(req, timeout=12) as response:
                final = response.geturl()
                if not allowed(final, domains):
                    raise AutomationError(f"Final URL outside allowlist: {final}")
                payload = response.read(8_000_001)
                if len(payload) > 8_000_000:
                    raise AutomationError(f"Response too large (8 MB limit): {final}")
                if attempt:
                    print(f"Fetch {url} attempt {attempt + 1}/3: success", file=sys.stderr)
                return payload.decode("utf-8", errors="replace"), final
        except HTTPError as exc:
            if exc.code != 429 and not 500 <= exc.code <= 599:
                error_type = ProviderUnavailable if exc.code in (401, 403, 404) else AutomationError
                raise error_type(f"HTTP {exc.code}: {url}") from exc
            reason = f"HTTP {exc.code}"
            cause = exc
        except (IncompleteRead, RemoteDisconnected, BadStatusLine, URLError,
                TimeoutError, ConnectionError) as exc:
            # IncompleteRead.partial is intentionally discarded. Only a full
            # response can be parsed as official evidence.
            if isinstance(exc, IncompleteRead) and len(exc.partial) > 8_000_000:
                raise AutomationError(f"Response too large (8 MB limit): {url}") from exc
            reason = type(exc).__name__
            cause = exc
        print(f"Fetch {url} attempt {attempt + 1}/3: {reason}", file=sys.stderr)
        if attempt == 2:
            raise ProviderUnavailable(f"Fetch failed after 3 attempts ({reason}): {url}") from cause
        time.sleep(1 + attempt * 2)
    raise ProviderUnavailable(f"Fetch failed: {url}")


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
        raise ProviderUnavailable("Official site returned access block/challenge")
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


class _PrimaryEditorialText(HTMLParser):
    """Collect editorial text while excluding navigation and related-content blocks."""
    BLOCKS = {"p", "h1", "h2", "h3", "h4", "li", "blockquote", "div", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.current: list[str] = []
        self.stack: list[tuple[str, bool]] = []

    def _flush(self):
        value = re.sub(r"\s+", " ", " ".join(self.current)).strip()
        if value:
            self.blocks.append(value)
        self.current = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        labels = (attrs.get("class", "") + " " + attrs.get("id", "")).casefold()
        skip = tag in ("head", "nav", "footer", "aside", "script", "style") or any(
            marker in labels for marker in ("related", "recommend", "carousel", "breadcrumb", "site-footer"))
        self.stack.append((tag, skip))
        if tag in self.BLOCKS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self._flush()
        if any(existing == tag for existing, _ in self.stack):
            index = len(self.stack) - 1 - [existing for existing, _ in self.stack[::-1]].index(tag)
            self.stack = self.stack[:index]

    def handle_data(self, data):
        if not any(skipped for _, skipped in self.stack):
            self.current.append(data)

    def text(self) -> str:
        self._flush()
        return "\n".join(self.blocks)


class EditorialArticleAdapter(Adapter):
    """Parser for official newsroom articles whose pages include related stories."""
    def parse(self, raw: str, url: str) -> dict:
        article = super().parse(raw, url)
        text = _PrimaryEditorialText()
        text.feed(raw)
        article["body"] = text.text()[:25000]
        return article


class ParamountAdapter(Adapter):
    name, source_name = "PARAMOUNT", "Paramount Press Express"

    def candidates(self, page, base, entry):
        return [urljoin(base, link) for link in page.links if "view=" in link and "/releases/" in urljoin(base, link)]

    def parse(self, raw: str, url: str) -> dict:
        article = super().parse(raw, url)
        if urlparse(url).hostname in ("paramountplus.com", "www.paramountplus.com"):
            article["sourceName"] = "Paramount+"
        return article


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


class AmcAdapter(EditorialArticleAdapter):
    name, source_name = "AMC", "AMC Networks"


class DisneyPlusAdapter(EditorialArticleAdapter):
    name, source_name = "DISNEY_PLUS", "Disney+ Press"


ADAPTERS = {a.name: a for a in (ParamountAdapter(), NetflixAdapter(), AppleAdapter(), WbdAdapter(), AmazonAdapter(), AmcAdapter(), DisneyPlusAdapter())}
PROVIDER_DOMAINS = {"PARAMOUNT": "paramountpressexpress.com", "NETFLIX": "netflix.com", "APPLE": "apple.com", "WBD": "press.wbd.com", "AMAZON": "aboutamazon.com", "AMC": "amcglobalmedia.com", "DISNEY_PLUS": "press.disneyplus.com"}
EXTRA_DOMAINS = {225891: ["paramountplus.com"]}


def number(value: str) -> int:
    return int(value) if value.isdigit() else (ORDINALS | CARDINALS)[value.lower()]


def season_in(text: str) -> int | None:
    m = re.search(rf"\b({NUM})\b\s+and\s+final\s+season\b", text, re.I)
    if m:
        return number(m.group(1))
    m = re.search(SEASON, text, re.I)
    return number(next(g for g in m.groups() if g)) if m else None


def date_in(text: str, announced: dt.date | None) -> tuple[str | None, int | None]:
    m = re.search(r"\b(" + MONTH_PATTERN + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?\b", text, re.I)
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
    m = re.search(r"\b(" + MONTH_PATTERN + r")\s+\d{1,2},?\s+20\d{2}\b", body[:1500], re.I)
    if not m:
        return None
    d, _ = date_in(m.group(), None)
    return dt.date.fromisoformat(d) if d else None


def premiere_date_in(sentence: str, announced: dt.date | None, strict_binding: bool = False,
                     aliases: list[str] | None = None) -> tuple[str | None, int | None, int | None]:
    # Read a date only after an explicit premiere/streaming verb in this same
    # series-and-season sentence. Publication and production dates are unrelated.
    return_verb = r"return(?:s|ing)?" if strict_binding else r"returns?"
    verbs = list(re.finditer(rf"\b(?:premieres?|{return_verb}|debuts?|arrives?|streams?|streaming)\b", sentence, re.I))
    if not verbs:
        return None, None, None
    season_refs = list(re.finditer(rf"(?:{SEASON}|\b{NUM}\b\s+and\s+final\s+season\b)", sentence, re.I))
    for index, verb in enumerate(verbs):
        next_verb = verbs[index + 1].start() if index + 1 < len(verbs) else len(sentence)
        tail = sentence[verb.start():min(verb.start() + 110, next_verb)]
        if strict_binding and re.search(r"\b(?:production|filming|shooting)\b", tail, re.I):
            continue
        month = re.search(r"\b(" + MONTH_PATTERN + r")\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+20\d{2})?\b", tail, re.I)
        year = re.search(r"\b(?:in|on)\s+(20\d{2})\b", tail[:60], re.I)
        if not (month and month.start() <= 85) and not year:
            continue
        near = sorted((m for m in season_refs if m.start() < next_verb),
                      key=lambda m: max(m.start() - verb.end(), verb.start() - m.end(), 0))
        if not near or max(near[0].start() - verb.end(), verb.start() - near[0].end(), 0) > 85:
            continue
        if strict_binding:
            if re.match(r"return(?:s|ing)?\b", verb.group(), re.I):
                # A bare "returning" can refer to another show mentioned in
                # the same sentence. Bind it to a title in its own comma
                # clause, or to an explicit "the series" anaphor with no
                # intervening competing title.
                clause_start = max(sentence.rfind(",", 0, verb.start()),
                                   sentence.rfind(";", 0, verb.start()),
                                   sentence.rfind(":", 0, verb.start())) + 1
                clause = sentence[clause_start:verb.start()]
                title_in_clause = any(alias.casefold() in clause.casefold() for alias in (aliases or []))
                generic_series = bool(re.search(r"\b(?:the|this|our)\s+(?:series|show|drama)\b", clause, re.I))
                competing_clause = clause
                for alias in aliases or []:
                    competing_clause = re.sub(re.escape(alias), " ", competing_clause, flags=re.I)
                other_title = bool(re.search(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b", competing_clause))
                if other_title or (not title_in_clause and not generic_series):
                    continue
                negation_context = sentence[max(0, verb.start() - 70):min(next_verb, verb.start() + 80)]
                if re.search(r"\b(?:not|never|no longer|doesn't|don't|isn't|aren't|wasn't|weren't|"
                             r"won't|will not|will never|cannot|can't|could not|might not|may|might|could|"
                             r"potentially|possibly|reportedly|allegedly|rumou?red|expected to|plans? to|"
                             r"planned to|scheduled to)\b",
                             negation_context, re.I):
                    continue
            # A different season between this reference and the date makes
            # the binding ambiguous. Never borrow the article's headline season.
            date_pos = verb.start() + (month.start() if month else year.start())
            if any(other is not near[0] and min(near[0].start(), date_pos) < other.start() < date_pos
                   for other in season_refs):
                continue
        season = season_in(near[0].group())
        if month and month.start() <= 85:
            date, release_year = date_in(month.group(), announced)
            return date, release_year, season
        return None, int(year.group(1)), season
    return None, None, None


def explicit_production_start(text: str, aliases: list[str]) -> tuple[re.Match | None, int | None]:
    """Match an explicit start of production for a named, numbered season.

    Production can establish that a later season is underway, but a generic
    production mention, plan, cast/crew update, or production for another show
    cannot. Keep the series and season adjacent to the production assertion.
    """
    season_refs = list(re.finditer(rf"(?:{SEASON}|\b{NUM}\b\s+and\s+final\s+season\b)", text, re.I))
    # Multiple seasons in one sentence make the production target unclear.
    if len(season_refs) != 1:
        return None, None
    season_ref = season_refs[0]
    season = season_in(season_ref.group())
    # Starting production of a first season does not prove a renewal.
    if season is None or season < 2:
        return None, None
    title_refs = [m for alias in aliases for m in re.finditer(re.escape(alias), text, re.I)]
    production_refs = list(re.finditer(r"\bproduction\b", text, re.I))
    if not title_refs or not production_refs:
        return None, None

    # Only accept direct title/season pairing, or the common "production of
    # Season N of TITLE" construction. This prevents borrowing a nearby show
    # title or season from cast biographies and unrelated article clauses.
    paired = any(
        (title.end() <= season_ref.start() and season_ref.start() - title.end() <= 24) or
        (season_ref.end() <= title.start() and title.start() - season_ref.end() <= 24)
        for title in title_refs
    )
    production_of_title_season = any(
        prod.start() < season_ref.start() < title.start() and
        season_ref.start() - prod.end() <= 75 and title.start() - season_ref.end() <= 30
        for prod in production_refs for title in title_refs
    )
    if not (paired or production_of_title_season):
        return None, None

    # Require production to have actually started or be underway. Future plans,
    # expectations, generic credits, and ongoing discussion do not qualify.
    title_pattern = "(?:" + "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True)) + ")"
    season_pattern = rf"(?:{SEASON})"
    # Keep the assertion structure explicit: production of the season of this
    # title is underway, or this title's season production is underway.
    underway_patterns = (
        rf"\bproduction\s+(?:of|for)\s+.{{0,70}}?{season_pattern}\s+(?:of\s+)?{title_pattern}"
        rf"\s+(?:is|has been|was)\s+(?:now\s+)?underway\b",
        rf"{title_pattern}\s*.{{0,12}}?{season_pattern}\s+production\s+"
        rf"(?:is|has been|was)\s+(?:now\s+)?underway\b",
    )
    underway = next((m for pattern in underway_patterns if (m := re.search(pattern, text, re.I))), None)
    headline_order = re.search(
        rf"{title_pattern}.{{0,16}}?\b(?:commenced|commences|began|begins|started|starts)\s+"
        rf"(?:the\s+)?{season_pattern}\s+production\b", text, re.I)
    matches = [m for m in (underway, headline_order) if m]
    if not matches:
        return None, None
    action = min(matches, key=lambda m: m.start())
    # The actual action must be connected to the paired title/season span.
    if abs(action.start() - season_ref.start()) > 85:
        return None, None
    if not any(abs(action.start() - prod.start()) <= 45 for prod in production_refs):
        return None, None
    context = text[max(0, action.start() - 70):min(len(text), action.end() + 45)]
    if re.search(r"\b(?:not|never|has not|have not|had not|hasn't|haven't|hadn't|"
                 r"isn't|aren't|wasn't|weren't|won't|will not|expected to|plans? to|"
                 r"planned to|scheduled to|may|might|could|rumou?red|reportedly|allegedly)\b"
                 r".{0,80}\b(?:underway|in progress|begun|started|commenced|commences|begins|starts)\b",
                 context, re.I):
        return None, None
    return action, season


def detect(article: dict, entry: dict, today: dt.date, extended_final: bool = False,
           strict_binding: bool = False) -> list[dict]:
    # A sentence/title must itself identify the series and the season statement.
    heading = article["title"]
    body = article["body"][:25000]
    # Monitored sources accept only an independently identified publication
    # date. The first date in filtered body text can be a past premiere.
    announced = (dt.date.fromisoformat(article["publicationDate"]) if article.get("publicationDate")
                 else None) if strict_binding else announcement_date(body)
    chunks = [heading] + re.split(r"[\n.!?]+", body)
    # Publisher copy commonly uses typographic apostrophes in an otherwise
    # exact series title. Normalize only that punctuation for strict binding.
    apostrophes = str.maketrans({"\u2018": "'", "\u2019": "'"})
    aliases = [a.translate(apostrophes).casefold() if strict_binding else a.casefold()
               for a in entry["aliases"]]
    facts = []
    for chunk in chunks:
        s = re.sub(r"\s+", " ", chunk).strip()
        if strict_binding:
            s = s.translate(apostrophes)
        if not any(a in s.casefold() for a in aliases):
            continue
        season = season_in(s)
        if not season:
            continue
        status = None
        rule = None
        if strict_binding:
            final = re.search(rf"\b(?:{NUM}\s+and\s+final\s+season|season\s+{NUM}\s+.{{0,25}}\bfinal\s+season|final\s+season\s+{NUM})\b", s, re.I)
            canceled = re.search(rf"\b(?:season\s+{NUM}|{NUM}\s+season)\b.{{0,35}}\b(?:cancelled|canceled|will not (?:return|continue))\b", s, re.I)
            renewed = re.search(rf"\b(?:renewed|greenlit|greenlighted)\s+for\s+(?:a\s+)?(?:season\s+{NUM}|{NUM}\s+season)\b", s, re.I)
            if not renewed:
                # Some official releases use “renewed <exact series title>
                # for a fourth season”. Require the configured title as the
                # verb's direct object in this same sentence.
                objects = "|".join(re.escape(a) for a in sorted(set(aliases), key=len, reverse=True))
                renewed = re.search(
                    rf"\b(?:renewed|greenlit|greenlighted)\s+(?:the\s+)?(?:{objects})\s+for\s+"
                    rf"(?:a\s+)?(?:season\s+{NUM}|{NUM}\s+season)\b", s, re.I)
                if renewed and re.search(r"\b(?:not|never|denies?|rumou?rs?|false)\b.{0,20}$",
                                         s[:renewed.start()], re.I):
                    renewed = None
            match = None
            if final:
                status, rule, match = "FINAL_SEASON", "explicit-final-season", final
            elif canceled and not re.search(r"\b(?:not|never|denies?|rumou?rs?|false)\b.{0,20}\b(?:cancelled|canceled)\b", s, re.I):
                status, rule, match = "CANCELED", "explicit-cancellation", canceled
            elif renewed:
                status, rule, match = "RENEWED", "explicit-renewal", renewed
            if match:
                # An assertion spanning two season references is ambiguous.
                refs = list(re.finditer(SEASON, match.group(), re.I))
                if len(refs) > 1 or season_in(match.group()) is None:
                    status = rule = None
                else:
                    season = season_in(match.group())
            if not match:
                production, production_season = explicit_production_start(s, aliases)
                if production:
                    status, rule, match, season = "RENEWED", "explicit-production-start", production, production_season
        elif (re.search(rf"\b(?:{NUM}\s+and\s+final\s+season|season\s+{NUM}\s+.{0,18}final|final\s+season\s+{NUM})\b", s, re.I)
                or (extended_final and re.search(rf"\bseason\s+{NUM}\s+.{{0,18}}\bfinal\s+season\b", s, re.I))):
            status, rule = "FINAL_SEASON", "explicit-final-season"
        elif not re.search(r"\b(?:not|never|denies?|rumou?rs?|false)\b.{0,20}\b(?:cancelled|canceled)\b", s, re.I) and re.search(rf"\b(?:season\s+{NUM}|{NUM}\s+season)\b.{{0,35}}\b(?:cancelled|canceled|will not (?:return|continue))\b", s, re.I):
            status, rule = "CANCELED", "explicit-cancellation"
        elif re.search(rf"\b(?:renewed|greenlit|greenlighted|return(?:s|ing)?|coming back)\s+for\s+(?:a\s+)?(?:season\s+{NUM}|{NUM}\s+season)\b", s, re.I):
            status, rule = "RENEWED", "explicit-renewal"
        date, year, date_season = (None, None, None)
        if status != "CANCELED":
            date, year, date_season = premiere_date_in(s, announced, strict_binding, aliases)
            if date and rule == "explicit-renewal" and not re.search(r"\b(?:renewed|greenlit|greenlighted)\b", s, re.I):
                # "returns for Season Three on August 2" announces a premiere,
                # not a new lifecycle decision that should own canonical source.
                status, rule = None, None
        common = {"tmdbId": entry["tmdbId"], "title": entry["title"],
                  "sourceName": article["sourceName"], "sourceUrl": article["url"],
                  "announcementDate": announced.isoformat() if announced else None}
        if strict_binding:
            excerpt = s[:200]
            # A long production announcement may put the season/name before a
            # release-year clause. Preserve an exact contiguous excerpt with
            # both ends so independent promotion can reparse the same facts.
            if len(s) > 200 and rule == "explicit-production-start":
                production_at = re.search(r"\b(?:production|filming|shooting)\b", s, re.I)
                if production_at and len(s[production_at.start():]) <= 200:
                    excerpt = s[production_at.start():]
            common["evidenceText"] = excerpt
        if status:
            facts.append({**common, "nextSeasonNumber": season, "status": status,
                          "releaseDate": None, "releaseYear": None, "rule": rule,
                          **({"factType": "LIFECYCLE"} if strict_binding else {})})
        if date_season and (date or year):
            facts.append({**common, "nextSeasonNumber": date_season,
                          "status": "RELEASE_DATE_CONFIRMED" if date else "RENEWED",
                          "releaseDate": date, "releaseYear": year,
                          "rule": "explicit-premiere" if date or not strict_binding else "explicit-release-year",
                          **({"factType": "RELEASE_DATE" if date else "RELEASE_YEAR"} if strict_binding else {}),
                          "dateRevision": bool(re.search(r"\b(?:rescheduled|postponed|moved|shifted)\b", s, re.I))})
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
        expected_domains = [PROVIDER_DOMAINS.get(e.get("provider"))] + EXTRA_DOMAINS.get(e["tmdbId"], [])
        urls = [e.get("officialUrl", "")] + e.get("fallbackUrls", []) + e.get("evidenceUrls", [])
        if (e.get("provider") not in ADAPTERS or e.get("allowedDomains") != expected_domains
                or not e.get("title") or not e.get("aliases")
                or any(not allowed(url, e["allowedDomains"]) for url in urls)):
            raise AutomationError(f"Invalid registry entry: {e.get('title')}")


def validate_dataset(data: dict, registry: dict, old: dict | None = None, *,
                     allow_supplemental_ids: set[int] | None = None):
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
        supplemental = i not in known
        if supplemental:
            explicitly_allowed = allow_supplemental_ids or set()
            if i not in previous and i not in explicitly_allowed:
                raise AutomationError(f"Supplemental series may only be added by promotion: {i}")
            if i in previous and i not in explicitly_allowed and r != previous[i]:
                raise AutomationError(f"Supplemental series may only be changed by promotion: {i}")
            # Automatically promoted discovered series are canonical records,
            # but deliberately do not enter the trusted bootstrap registry.
            # Keep them valid for subsequent daily updater runs by applying
            # the same provider-owned hostname policy used at promotion.
            from discovery import provider_specs
            domains = sorted({domain for spec in provider_specs().values() for domain in spec["officialDomains"]})
        else:
            domains = known[i]["allowedDomains"]
        if (not isinstance(i, int) or isinstance(i, bool) or i <= 0 or
                not isinstance(r.get("title"), str) or not r["title"].strip() or
                (not supplemental and r.get("title") != known[i]["title"]) or r.get("status") not in STATUSES):
            raise AutomationError(f"Unknown series/status: {i}")
        if not isinstance(r.get("sourceName"), str) or not r["sourceName"].strip() or not isinstance(r.get("sourceUrl"), str) or not allowed(r["sourceUrl"], domains):
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
        if supplemental and i in previous and r["nextSeasonNumber"] < previous[i]["nextSeasonNumber"]:
            raise AutomationError(f"Season regression: {i}")
    if old and not set(previous).issubset(set(ids)):
        raise AutomationError("Destructive deletion")


def canonical_fact_order(facts: list[dict]) -> list[dict]:
    """Rank verified evidence independently of discovery and parser order.

    Keep the existing season/status/date priority. Equivalent assertions use
    stable official URL and fact fields as tie breakers; no source is promoted
    merely because it appeared first in a fetch or persisted evidence array.
    """
    priority = {"RELEASE_DATE_CONFIRMED": 0, "RENEWED": 1, "FINAL_SEASON": 3, "CANCELED": 4}
    return sorted(facts, key=lambda f: (
        f["nextSeasonNumber"], priority[f["status"]], bool(f["releaseDate"]),
        f["announcementDate"] or "", f["sourceUrl"], f["sourceName"],
        f.get("releaseDate") or "", f.get("releaseYear") or 0,
        f.get("rule") or "", f.get("evidenceText") or "",
    ), reverse=True)


def merge(old: dict | None, facts: list[dict], today: dt.date) -> tuple[dict | None, dict | None]:
    if not facts:
        return old, None
    # Trusted daily updates retain their established merge preference. The
    # monitored and independent-promotion callers first apply the shared
    # canonical_fact_order to remove input-order ties in their evidence set.
    priority = {"RELEASE_DATE_CONFIRMED": 0, "RENEWED": 1, "FINAL_SEASON": 3, "CANCELED": 4}
    facts = sorted(facts, key=lambda f: (f["nextSeasonNumber"], priority[f["status"]], bool(f["releaseDate"]), f["announcementDate"] or ""), reverse=True)
    if old:
        facts = [f for f in facts if f["nextSeasonNumber"] >= old["nextSeasonNumber"]]
    if not facts:
        return old, None
    season = facts[0]["nextSeasonNumber"]
    same = [f for f in facts if f["nextSeasonNumber"] == season]
    lifecycle = next((f for f in same if f["rule"] not in ("explicit-premiere", "explicit-release-year")), None)
    if old and season == old["nextSeasonNumber"]:
        # Existing final/cancelled state is sticky; same-season date is independent.
        status = old["status"] if old["status"] in ("FINAL_SEASON", "CANCELED") else (lifecycle["status"] if lifecycle else old["status"])
        # A weaker renewal assertion cannot erase an already confirmed
        # premiere status for the same season.
        if old["status"] == "RELEASE_DATE_CONFIRMED" and (not lifecycle or lifecycle["status"] == "RENEWED"):
            status = old["status"]
    else:
        status = lifecycle["status"] if lifecycle else same[0]["status"]
    date_evidence = [f for f in same if f["releaseDate"]]
    if old and season == old["nextSeasonNumber"] and old["releaseDate"]:
        date_evidence.append(old)
    dates = {f["releaseDate"] for f in date_evidence}
    if len(dates) > 1:
        revisions = [f for f in same if f["releaseDate"] and f.get("dateRevision") and f["announcementDate"]]
        latest = max(revisions, key=lambda f: f["announcementDate"]) if revisions else None
        if not latest or any(f["releaseDate"] != latest["releaseDate"] and
                             (not f["announcementDate"] or f["announcementDate"] >= latest["announcementDate"])
                             for f in date_evidence):
            raise AutomationError(f"Conflicting release dates for {same[0]['title']} season {season}: {sorted(dates)}")
        dated = latest
    else:
        dated = next((f for f in same if f["releaseDate"]), None)
    year_only = next((f for f in same if f["releaseYear"]), None)
    if year_only and old and season == old["nextSeasonNumber"] and old.get("releaseDate") and \
            year_only["releaseYear"] != dt.date.fromisoformat(old["releaseDate"]).year and not dated:
        raise AutomationError(f"Conflicting release year for {same[0]['title']} season {season}")
    if year_only and old and season == old["nextSeasonNumber"] and old.get("releaseYear") and \
            year_only["releaseYear"] != old["releaseYear"] and not dated:
        raise AutomationError(f"Conflicting release year for {same[0]['title']} season {season}")
    release_date = dated["releaseDate"] if dated else (old["releaseDate"] if old and season == old["nextSeasonNumber"] else None)
    release_year = int(release_date[:4]) if release_date else (year_only["releaseYear"] if year_only else (old["releaseYear"] if old and season == old["nextSeasonNumber"] else None))
    if status == "CANCELED":
        release_date = release_year = None
    if status == "RELEASE_DATE_CONFIRMED" and not release_date:
        status = "RENEWED"
    # A single canonical URL must prove the strongest status assertion. Keep
    # independent date evidence in the audit when a different article proves it.
    if status in ("FINAL_SEASON", "CANCELED") and old and season == old["nextSeasonNumber"] and old["status"] == status and (not lifecycle or lifecycle["status"] != status):
        source = old
    elif not lifecycle and old and season == old["nextSeasonNumber"] and old["status"] == status:
        source = old
    else:
        source = lifecycle if lifecycle else (dated if dated else same[0])
    result = {k: source.get(k) for k in ("tmdbId", "title", "nextSeasonNumber", "sourceName", "sourceUrl", "announcementDate")}
    result.update(status=status, releaseDate=release_date, releaseYear=release_year, lastChecked=today.isoformat())
    if old and all(result.get(k) == old.get(k) for k in ("nextSeasonNumber", "status", "releaseDate", "releaseYear")):
        return old, None
    supporting = sorted({f["sourceUrl"] for f in same if f["sourceUrl"] != source["sourceUrl"]})
    return result, {"rule": source.get("rule", "previous-status-evidence"), "provider": source["sourceName"], "sourceUrl": source["sourceUrl"], "supportingSourceUrls": supporting}


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
    domains = entry["allowedDomains"]
    errors = []
    unavailable_errors = 0
    failed_urls: set[str] = set()
    cached: dict[str, tuple[str, str]] = {}
    discovered: list[str] = []

    # Only WBD has configured endpoint fallbacks. An explicit evidence URL can
    # still be checked if a listing is unavailable (e.g. local Paramount block).
    for endpoint in [entry["officialUrl"]] + entry.get("fallbackUrls", []):
        try:
            raw, final = fetcher(endpoint, domains)
            page = parse_page(raw)
            cached[endpoint] = (raw, final)
            cached[final] = (raw, final)
            if entry["provider"] == "AMAZON" or "/media-release/" in urlparse(final).path:
                discovered.append(final)
            discovered.extend(adapter.candidates(page, final, entry))
            break
        except (AutomationError, ValueError) as exc:
            failed_urls.add(endpoint)
            errors.append(f"{endpoint}: {exc}")
            unavailable_errors += isinstance(exc, ProviderUnavailable)

    # Collect every selected candidate before resolving conflicts. Configured
    # evidence is first, followed by existing evidence and up to eight recent
    # show-listing releases; no candidate stops the scan after its first fact.
    candidates = entry.get("evidenceUrls", []) + ([current["sourceUrl"]] if current else []) + discovered
    unique = []
    for url in candidates:
        if not allowed(url, domains):
            raise AutomationError(f"Candidate outside allowlist: {url}")
        if url not in unique:
            unique.append(url)
    articles = []
    for url in unique[:10]:
        if url in failed_urls:
            continue
        try:
            content, actual = cached.get(url) or fetcher(url, domains)
            articles.append(adapter.parse(content, actual))
        except (AutomationError, ValueError) as exc:
            failed_urls.add(url)
            errors.append(f"{url}: {exc}")
            unavailable_errors += isinstance(exc, ProviderUnavailable)
    if not articles:
        reason = f"No usable official releases for {entry['title']} ({adapter.name}): {'; '.join(errors)}"
        if errors and unavailable_errors == len(errors):
            raise ProviderUnavailable(reason)
        raise AutomationError(reason)
    facts = [fact for article in articles for fact in detect(article, entry, today)]
    return facts, len(articles)


def audit_bytes(path: Path | None = None) -> bytes:
    path = path or AUDIT
    content = path.read_bytes() if path.exists() else b""
    try:
        for line in content.decode("utf-8").splitlines():
            if line.strip() and not isinstance(json.loads(line), dict):
                raise ValueError("Audit entry must be an object")
    except (UnicodeError, ValueError) as exc:
        raise AutomationError(f"Audit corruption: {exc}") from exc
    return content


def staged_file(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
        staged = Path(stream.name)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return staged


def publish(proposed: dict, registry: dict, changes: list[dict], original_data: bytes, old_audit: bytes,
            old_audit_exists: bool, *, data_path: Path | None = None, audit_path: Path | None = None,
            previous_data: dict | None = None, allow_supplemental_ids: set[int] | None = None):
    data_path = data_path or DATA
    audit_path = audit_path or AUDIT
    new_data = (json.dumps(proposed, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    new_audit = old_audit + b"".join((json.dumps(change, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8") for change in changes)
    staged_data = staged_audit = None
    committed = False
    replaced = False
    try:
        staged_data = staged_file(data_path, new_data)
        staged_audit = staged_file(audit_path, new_audit)
        os.replace(staged_audit, audit_path)
        replaced = True
        staged_audit = None
        os.replace(staged_data, data_path)
        staged_data = None
        written = data_path.read_bytes()
        if written != new_data or audit_path.read_bytes() != new_audit:
            raise AutomationError("Write verification failed")
        if allow_supplemental_ids:
            validate_dataset(json.loads(written), registry, previous_data,
                             allow_supplemental_ids=allow_supplemental_ids)
        else:
            validate_dataset(json.loads(written), registry, previous_data)
        audit_bytes(audit_path)
        committed = True
    finally:
        if replaced and not committed:
            # Restore both files if either replacement or verification failed.
            restore_data = staged_file(data_path, original_data)
            os.replace(restore_data, data_path)
            if old_audit_exists:
                restore_audit = staged_file(audit_path, old_audit)
                os.replace(restore_audit, audit_path)
            elif audit_path.exists():
                audit_path.unlink()
        for staged in (staged_data, staged_audit):
            if staged is not None:
                staged.unlink(missing_ok=True)


def run(dry_run: bool, collector=collect, today: dt.date | None = None) -> dict:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    try:
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        validate_registry(registry)
        original_data = DATA.read_bytes()
        original = json.loads(original_data)
        validate_dataset(original, registry, original)
        old_audit_exists = AUDIT.exists()
        old_audit = audit_bytes()
    except (OSError, UnicodeError, ValueError, AutomationError) as exc:
        return {"dryRun": dry_run, "publishable": False, "reports": [], "changes": [], "warnings": [],
                "failures": [f"GLOBAL_SAFETY_FAILURE: {exc}"]}
    current = {r["tmdbId"]: r for r in original["series"]}
    proposed = copy.deepcopy(original)
    changes = []
    failures = []
    warnings = []
    reports = []
    for entry in registry["series"]:
        old = current.get(entry["tmdbId"])
        try:
            facts, count = collector(entry, old, today)
        except ProviderUnavailable as exc:
            preservation = "existing verified record preserved" if old else "no record created"
            warnings.append(f"PROVIDER_UNAVAILABLE: {entry['provider']}/{entry['title']}: {exc}; {preservation}")
            reports.append({"title": entry["title"], "provider": entry["provider"], "result": "unavailable", "reason": str(exc), "preservation": preservation})
            continue
        except (AutomationError, ValueError) as exc:
            failures.append(f"SERIES_VALIDATION_FAILURE: {entry['provider']}/{entry['title']}: {exc}")
            reports.append({"title": entry["title"], "provider": entry["provider"], "result": "failed", "reason": str(exc)})
            continue
        try:
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
        except (AutomationError, ValueError) as exc:
            failures.append(f"SERIES_VALIDATION_FAILURE: {entry['provider']}/{entry['title']}: {exc}")
            reports.append({"title": entry["title"], "provider": entry["provider"], "result": "failed", "reason": str(exc)})
    if len(changes) > 4:
        failures.append(f"GLOBAL_SAFETY_FAILURE: Mass change protection: {len(changes)} factual changes exceeds 4")
    if changes:
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        previous = dt.datetime.fromisoformat(original["generatedAt"].replace("Z", "+00:00"))
        if now <= previous:
            failures.append("GLOBAL_SAFETY_FAILURE: generatedAt regression")
        else:
            proposed["generatedAt"] = now.isoformat().replace("+00:00", "Z")
    try:
        validate_dataset(proposed, registry, original)
    except AutomationError as exc:
        failures.append(f"GLOBAL_SAFETY_FAILURE: {exc}")
    result = {"dryRun": dry_run, "publishable": not failures, "reports": reports, "changes": changes, "warnings": warnings, "failures": failures}
    if not dry_run and result["publishable"] and changes:
        try:
            publish(proposed, registry, changes, original_data, old_audit, old_audit_exists,
                    previous_data=original)
        except (OSError, AutomationError, ValueError) as exc:
            result["failures"].append(f"GLOBAL_SAFETY_FAILURE: publishing failed: {exc}")
            result["publishable"] = False
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--discover", type=int, metavar="TMDB_ID", help="Report official-source discovery without changing production data")
    parser.add_argument("--process-discovered", type=int, metavar="TMDB_ID", help="Process eligible discovery sources into the monitored registry only")
    parser.add_argument("--promote-monitored", type=int, metavar="TMDB_ID", help="Independently validate and promote one monitored record")
    parser.add_argument("--metadata-file", type=Path, help="Optional local TMDB metadata fixture for discovery or monitored processing")
    args = parser.parse_args()
    selected = [args.discover is not None, args.process_discovered is not None,
                args.promote_monitored is not None]
    if sum(selected) > 1:
        parser.error("Choose only one of --discover, --process-discovered or --promote-monitored")
    if args.discover is not None:
        if args.dry_run:
            parser.error("--discover is always read-only; do not combine it with --dry-run")
        from discovery import discover_cli
        report = discover_cli(args.discover, args.metadata_file)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(1 if report["result"] == "metadata_unavailable" else 0)
    if args.process_discovered is not None:
        from monitored import process
        try:
            metadata = json.loads(args.metadata_file.read_text(encoding="utf-8")) if args.metadata_file else None
            report = process(args.process_discovered, dry_run=args.dry_run, metadata=metadata)
        except (OSError, UnicodeError, ValueError, AutomationError) as exc:
            report = {"tmdbId": args.process_discovered, "pipelineState": "VALIDATION_FAILED",
                      "validationReason": type(exc).__name__, "dryRun": args.dry_run, "registryChange": "none"}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(1 if report["pipelineState"] in ("VALIDATION_FAILED", "METADATA_UNAVAILABLE") else 0)
    if args.promote_monitored is not None:
        if args.metadata_file:
            parser.error("--metadata-file does not apply to --promote-monitored")
        if args.dry_run:
            # A preview must not create __pycache__ files in a clean checkout.
            sys.dont_write_bytecode = True
        from promotion import promote
        report = promote(args.promote_monitored, dry_run=args.dry_run)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(1 if report["fatal"] else 0)
    if args.metadata_file:
        parser.error("--metadata-file requires --discover or --process-discovered")
    output = run(args.dry_run)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    for warning in output["warnings"]:
        print(f"::warning::{warning}", file=sys.stderr)
    sys.exit(0 if output["publishable"] else 1)
