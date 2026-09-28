import copy
import datetime as dt
from http.client import IncompleteRead
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).parent))
import update as u

TODAY = dt.date(2026, 9, 27)
ENTRY = {"tmdbId": 153312, "title": "Tulsa King", "provider": "PARAMOUNT", "officialUrl": "https://www.paramountpressexpress.com/show/releases/", "allowedDomains": ["paramountpressexpress.com"], "aliases": ["Tulsa King"]}
BASE = {"tmdbId": 153312, "title": "Tulsa King", "nextSeasonNumber": 3, "status": "RENEWED", "releaseDate": None, "releaseYear": None, "sourceName": "Paramount Press Express", "sourceUrl": "https://www.paramountpressexpress.com/old", "announcementDate": "2026-01-01", "lastChecked": "2026-09-26"}


def article(text, url="https://www.paramountpressexpress.com/release"):
    return {"title": text, "body": text + "\nSeptember 1, 2026", "url": url, "sourceName": "Paramount Press Express"}


class Response:
    def __init__(self, url, content):
        self.url = url
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def geturl(self):
        return self.url

    def read(self, limit):
        if isinstance(self.content, BaseException):
            raise self.content
        return self.content[:limit]


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.url = ENTRY["officialUrl"]
        self.calls = []
        self.sleeps = []
        p = patch.object(u.time, "sleep", side_effect=self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)

    def serve(self, outcomes):
        def open_request(request, timeout):
            self.assertEqual(timeout, 12)
            url = request.full_url
            self.calls.append(url)
            item = outcomes[url].pop(0)
            if isinstance(item, BaseException):
                raise item
            return Response(url, item)
        class Opener:
            def open(self, request, timeout):
                return open_request(request, timeout)
        p = patch.object(u, "build_opener", return_value=Opener())
        p.start()
        self.addCleanup(p.stop)

    def test_incomplete_read_then_success_discards_partial(self):
        self.serve({self.url: [IncompleteRead(b"<h1>partial", 10), b"<h1>complete</h1>"]})
        self.assertEqual(u.fetch(self.url, ENTRY["allowedDomains"])[0], "<h1>complete</h1>")
        self.assertEqual(self.calls, [self.url] * 2)
        self.assertEqual(self.sleeps, [1])

    def test_incomplete_read_all_attempts_endpoint_failure(self):
        self.serve({self.url: [IncompleteRead(b"partial", 10) for _ in range(3)]})
        with self.assertRaisesRegex(u.AutomationError, "after 3 attempts"):
            u.fetch(self.url, ENTRY["allowedDomains"])
        self.assertEqual(self.calls, [self.url] * 3)
        self.assertEqual(self.sleeps, [1, 3])

    def test_primary_incomplete_read_fallback_succeeds(self):
        entry = next(e for e in json.loads(u.REGISTRY.read_text(encoding="utf-8"))["series"] if e["provider"] == "WBD")
        fallback = entry["fallbackUrls"][0]
        self.serve({entry["officialUrl"]: [IncompleteRead(b"partial", 10) for _ in range(3)],
                    fallback: [b"<html><h1>The White Lotus renewed for season 4</h1></html>"]})
        # The fallback itself is an official release and is reused as evidence.
        facts, count = u.collect(entry, None, TODAY, u.fetch)
        self.assertEqual(count, 1)
        self.assertTrue(facts)
        self.assertEqual({f["nextSeasonNumber"] for f in facts}, {4})
        self.assertEqual(self.calls, [entry["officialUrl"]] * 3 + [fallback])

    def test_http_503_then_success(self):
        self.serve({self.url: [HTTPError(self.url, 503, "unavailable", {}, None), b"<h1>ok</h1>"]})
        self.assertEqual(u.fetch(self.url, ENTRY["allowedDomains"])[0], "<h1>ok</h1>")
        self.assertEqual(self.sleeps, [1])

    def test_http_429_then_success(self):
        self.serve({self.url: [HTTPError(self.url, 429, "limited", {}, None), b"<h1>ok</h1>"]})
        self.assertEqual(u.fetch(self.url, ENTRY["allowedDomains"])[0], "<h1>ok</h1>")
        self.assertEqual(self.calls, [self.url] * 2)

    def test_http_403_uses_fallback_without_retry(self):
        entry = next(e for e in json.loads(u.REGISTRY.read_text(encoding="utf-8"))["series"] if e["provider"] == "WBD")
        fallback = entry["fallbackUrls"][0]
        self.serve({entry["officialUrl"]: [HTTPError(entry["officialUrl"], 403, "forbidden", {}, None)],
                    fallback: [b"<html><h1>The White Lotus renewed for season 4</h1></html>"]})
        facts, count = u.collect(entry, None, TODAY, u.fetch)
        self.assertEqual(count, 1)
        self.assertTrue(facts)
        self.assertEqual(self.calls, [entry["officialUrl"], fallback])
        self.assertEqual(self.sleeps, [])

    def test_response_too_large_is_rejected(self):
        self.serve({self.url: [b"<h1>valid</h1>" + b"x" * 8_000_000]})
        with self.assertRaisesRegex(u.AutomationError, "Response too large"):
            u.fetch(self.url, ENTRY["allowedDomains"])
        self.assertEqual(self.calls, [self.url])
        self.assertEqual(self.sleeps, [])

    def test_unexpected_programming_error_propagates(self):
        self.serve({self.url: [RuntimeError("bug in fetch dependency")]})
        with self.assertRaisesRegex(RuntimeError, "bug in fetch dependency"):
            u.fetch(self.url, ENTRY["allowedDomains"])
        self.assertEqual(self.calls, [self.url])


class MobLandTests(unittest.TestCase):
    def setUp(self):
        registry = json.loads(u.REGISTRY.read_text(encoding="utf-8"))
        self.entry = next(e for e in registry["series"] if e["title"] == "MobLand")
        data = json.loads(u.DATA.read_text(encoding="utf-8"))
        self.old = next(r for r in data["series"] if r["title"] == "MobLand")

    def official_renewal(self):
        return {"title": "THE HARRIGANS AREN’T GOING ANYWHERE: MOBLAND RENEWED FOR SEASON THREE ON PARAMOUNT+",
                "body": "September 3, 2026 – The Harrigan family’s fight for power is far from over. "
                        "Today, Paramount+ announced that its hit original crime drama MobLand has been renewed for a third season, "
                        "ahead of the series’ highly-anticipated season two premiere on Friday, September 18.",
                "url": self.old["sourceUrl"], "sourceName": "Paramount Press Express"}

    def test_real_official_pattern_keeps_s3_without_s2_date(self):
        facts = u.detect(self.official_renewal(), self.entry, TODAY)
        u.validate_facts(facts, self.entry)
        selected, evidence = u.merge(self.old, facts, TODAY)
        self.assertEqual((selected["nextSeasonNumber"], selected["status"], selected["releaseDate"]),
                         (3, "RENEWED", None))
        self.assertIsNone(evidence)
        self.assertTrue(any(f["nextSeasonNumber"] == 2 and f["releaseDate"] == "2026-09-18" for f in facts))
        self.assertFalse(any(f["nextSeasonNumber"] == 3 and f["releaseDate"] for f in facts))

    def test_renewal_with_weak_unrelated_date_remains_valid(self):
        source = self.official_renewal()
        source["body"] += "\nArticle published September 3, 2026; production began August 1, 2026."
        facts = u.detect(source, self.entry, TODAY)
        u.validate_facts(facts, self.entry)
        selected, evidence = u.merge(self.old, facts, TODAY)
        self.assertEqual((selected["status"], selected["releaseDate"], evidence), ("RENEWED", None, None))

    def test_earlier_return_verb_does_not_claim_later_season_premiere(self):
        source = self.official_renewal()
        source["body"] = "September 3, 2026 - MobLand will return for a third season, ahead of the season two premiere on Friday, September 18."
        facts = u.detect(source, self.entry, TODAY)
        u.validate_facts(facts, self.entry)
        self.assertTrue(any(f["nextSeasonNumber"] == 2 and f["releaseDate"] == "2026-09-18" for f in facts))
        self.assertFalse(any(f["nextSeasonNumber"] == 3 and f["releaseDate"] for f in facts))

    def test_publication_date_is_not_premiere(self):
        facts = u.detect({"title": "MobLand renewed for Season Three",
                          "body": "September 3, 2026 - MobLand renewed for Season Three.",
                          "url": self.old["sourceUrl"], "sourceName": "Paramount Press Express"}, self.entry, TODAY)
        self.assertTrue(facts)
        self.assertFalse(any(f["releaseDate"] for f in facts))

    def test_production_start_date_is_not_premiere(self):
        facts = u.detect(article("MobLand Season Three production begins September 18, 2026"), self.entry, TODAY)
        self.assertFalse(any(f["releaseDate"] for f in facts))

    def test_strong_s3_premiere_is_retained(self):
        facts = u.detect(article("MobLand Season Three premieres October 30, 2026"), self.entry, TODAY)
        u.validate_facts(facts, self.entry)
        selected, evidence = u.merge(self.old, facts, TODAY)
        self.assertEqual((selected["status"], selected["releaseDate"]), ("RENEWED", "2026-10-30"))
        self.assertEqual(selected["sourceUrl"], self.old["sourceUrl"])
        self.assertIsNotNone(evidence)

    def test_two_strong_s3_dates_still_conflict(self):
        first = u.detect(article("MobLand Season Three premieres October 30, 2026"), self.entry, TODAY)
        second = u.detect(article("MobLand Season Three premieres November 6, 2026"), self.entry, TODAY)
        with self.assertRaisesRegex(u.AutomationError, "Conflicting release dates"):
            u.merge(self.old, first + second, TODAY)


class DetectionTests(unittest.TestCase):
    def test_lioness_renewal_and_premiere_both_orders(self):
        entry = {"tmdbId": 113962, "title": "Lioness", "aliases": ["Lioness"]}
        renewal_url = "https://www.paramountpressexpress.com/lioness-renewal"
        premiere_url = "https://www.paramountpressexpress.com/lioness-premiere"
        renewal = u.detect({"title": "Lioness renewed for a third season", "body": "October 1, 2025 - Lioness renewed for a third season.",
                            "url": renewal_url, "sourceName": "Paramount Press Express"}, entry, TODAY)
        premiere = u.detect({"title": "Lioness Season Three: Mission Accepted - returns August 2", "body": "June 4, 2026 - Lioness returns for Season Three on Sunday, August 2.",
                             "url": premiere_url, "sourceName": "Paramount Press Express"}, entry, TODAY)
        for facts in (renewal + premiere, premiere + renewal):
            with self.subTest(first=facts[0]["sourceUrl"]):
                selected, evidence = u.merge(None, facts, TODAY)
                self.assertEqual((selected["nextSeasonNumber"], selected["status"], selected["releaseDate"], selected["releaseYear"]),
                                 (3, "RENEWED", "2026-08-02", 2026))
                self.assertEqual(selected["sourceUrl"], renewal_url)
                self.assertIn(premiere_url, evidence["supportingSourceUrls"])

    def test_production_date_is_not_premiere(self):
        facts = u.detect(article("Lioness Season 3 production begins August 2, 2026"),
                         {"tmdbId": 113962, "title": "Lioness", "aliases": ["Lioness"]}, TODAY)
        self.assertFalse(any(f["releaseDate"] for f in facts))

    def test_publication_date_is_not_premiere(self):
        facts = u.detect({"title": "Lioness renewed for Season 3", "body": "August 2, 2026 - Lioness renewed for Season 3.",
                          "url": "https://www.paramountpressexpress.com/release", "sourceName": "Paramount Press Express"},
                         {"tmdbId": 113962, "title": "Lioness", "aliases": ["Lioness"]}, TODAY)
        self.assertTrue(facts)
        self.assertFalse(any(f["releaseDate"] for f in facts))

    def test_previous_season_premiere_does_not_date_new_season(self):
        entry = {"tmdbId": 113962, "title": "Lioness", "aliases": ["Lioness"]}
        facts = u.detect(article("Lioness renewed for Season 3"), entry, TODAY)
        facts += u.detect(article("Lioness Season 2 premiered August 2, 2025"), entry, TODAY)
        selected, _ = u.merge(None, facts, TODAY)
        self.assertEqual(selected["nextSeasonNumber"], 3)
        self.assertIsNone(selected["releaseDate"])

    def test_year_only_premiere(self):
        entry = {"tmdbId": 113962, "title": "Lioness", "aliases": ["Lioness"]}
        facts = u.detect(article("Lioness Season 3 premieres in 2027"), entry, TODAY)
        selected, _ = u.merge(None, facts, TODAY)
        self.assertEqual((selected["releaseDate"], selected["releaseYear"]), (None, 2027))

    def test_conflicting_premiere_dates_fail_safe(self):
        entry = {"tmdbId": 113962, "title": "Lioness", "aliases": ["Lioness"]}
        first = u.detect(article("Lioness Season 3 premieres August 2, 2026", "https://www.paramountpressexpress.com/first"), entry, TODAY)
        second = u.detect(article("Lioness Season 3 premieres August 9, 2026", "https://www.paramountpressexpress.com/second"), entry, TODAY)
        with self.assertRaisesRegex(u.AutomationError, "Conflicting release dates"):
            u.merge(None, first + second, TODAY)

    def test_new_date_conflicting_with_verified_record_fails_safe(self):
        entry = {"tmdbId": 153312, "title": "Tulsa King", "aliases": ["Tulsa King"]}
        old = copy.deepcopy(BASE)
        old.update(releaseDate="2026-10-16", releaseYear=2026)
        new = u.detect(article("Tulsa King Season 3 premieres October 23, 2026"), entry, TODAY)
        with self.assertRaisesRegex(u.AutomationError, "Conflicting release dates"):
            u.merge(old, new, TODAY)

    def test_explicit_reschedule_overrides_older_date(self):
        entry = {"tmdbId": 113962, "title": "Lioness", "aliases": ["Lioness"]}
        first = u.detect({"title": "Lioness Season 3 premieres August 2, 2026", "body": "June 4, 2026 - Lioness Season 3 premieres August 2, 2026.",
                          "url": "https://www.paramountpressexpress.com/first", "sourceName": "Paramount Press Express"}, entry, TODAY)
        revised = u.detect({"title": "Lioness Season 3 premiere moved to August 9, 2026", "body": "July 4, 2026 - Lioness Season 3 premiere moved to August 9, 2026.",
                            "url": "https://www.paramountpressexpress.com/revised", "sourceName": "Paramount Press Express"}, entry, TODAY)
        selected, _ = u.merge(None, first + revised, TODAY)
        self.assertEqual(selected["releaseDate"], "2026-08-09")

    def test_cancelled_fact_cannot_acquire_premiere_date(self):
        entry = {"tmdbId": 113962, "title": "Lioness", "aliases": ["Lioness"]}
        canceled = u.detect(article("Lioness Season 3 canceled"), entry, TODAY)
        premiere = u.detect(article("Lioness Season 3 premieres August 2, 2026"), entry, TODAY)
        selected, _ = u.merge(None, canceled + premiere, TODAY)
        self.assertEqual(selected["status"], "CANCELED")
        self.assertIsNone(selected["releaseDate"])

    def test_publication_date_before_premiere_date_in_sentence(self):
        entry = {"tmdbId": 113962, "title": "Lioness", "aliases": ["Lioness"]}
        facts = u.detect(article("Article published June 4, 2026: Lioness Season 3 premieres August 2, 2026"), entry, TODAY)
        self.assertEqual({f["releaseDate"] for f in facts}, {"2026-08-02"})

    def test_publication_year_cannot_become_season_26(self):
        sentence = "Press Release September 4, 2026 Season four of Silo will premiere on July 9, 2027 on Apple TV"
        self.assertEqual(u.season_in(sentence), 4)
        entry = {"tmdbId": 125988, "title": "Silo", "aliases": ["Silo"]}
        facts = u.detect(article(sentence), entry, TODAY)
        self.assertEqual({f["nextSeasonNumber"] for f in facts}, {4})

    def test_wbd_unofficial_discovery_domain_rejected(self):
        entry = next(e for e in json.loads(u.REGISTRY.read_text(encoding="utf-8"))["series"] if e["provider"] == "WBD")
        self.assertFalse(u.allowed("https://example.com/wbd-feed.xml", entry["allowedDomains"]))
        fake = {"tmdbId": entry["tmdbId"], "title": entry["title"], "nextSeasonNumber": 4,
                "status": "RENEWED", "releaseDate": None, "releaseYear": None,
                "sourceName": "Unverified mirror", "sourceUrl": "https://example.com/wbd-feed.xml",
                "announcementDate": "2026-04-15", "rule": "explicit-renewal"}
        with self.assertRaisesRegex(u.AutomationError, "Invalid evidence source"):
            u.validate_facts([fake], entry)

    def test_renewal(self):
        self.assertEqual(u.detect(article("Tulsa King renewed for season 4"), ENTRY, TODAY)[0]["status"], "RENEWED")

    def test_final(self):
        self.assertEqual(u.detect(article("Tulsa King renewed for a fourth and final season"), ENTRY, TODAY)[0]["status"], "FINAL_SEASON")

    def test_release_date(self):
        f = u.detect(article("Tulsa King season 4 premieres October 16, 2026"), ENTRY, TODAY)[0]
        self.assertEqual((f["status"], f["releaseDate"]), ("RELEASE_DATE_CONFIRMED", "2026-10-16"))

    def test_final_with_date(self):
        old = copy.deepcopy(BASE)
        old["status"] = "FINAL_SEASON"
        old["nextSeasonNumber"] = 4
        fact = u.detect(article("Tulsa King fourth and final season premieres October 16, 2026"), ENTRY, TODAY)
        new, _ = u.merge(old, fact, TODAY)
        self.assertEqual((new["status"], new["releaseDate"]), ("FINAL_SEASON", "2026-10-16"))

    def test_explicit_cancel(self):
        self.assertEqual(u.detect(article("Tulsa King season 4 canceled"), ENTRY, TODAY)[0]["status"], "CANCELED")

    def test_unrelated_number(self):
        self.assertFalse(u.detect(article("Tulsa King renewed; 4 actors join season 3 of Another Show"), ENTRY, TODAY))

    def test_domain(self):
        self.assertFalse(u.allowed("https://paramountpressexpress.com.evil.org/a", ENTRY["allowedDomains"]))
        self.assertFalse(u.allowed("http://www.paramountpressexpress.com/a", ENTRY["allowedDomains"]))

    def test_date_without_evidence(self):
        fact = u.detect(article("Tulsa King season 4 premieres October 16, 2026"), ENTRY, TODAY)[0]
        fact["rule"] = "explicit-renewal"
        with self.assertRaisesRegex(u.AutomationError, "without premiere evidence"):
            u.validate_facts([fact], ENTRY)

    def test_blocked_html(self):
        with self.assertRaises(u.AutomationError):
            u.parse_page("<title>Sorry - Not Allowed</title><h1>Blocked</h1>")

    def test_season_regression(self):
        registry = {"series": [ENTRY]}
        old = {"schemaVersion": 1, "generatedAt": "2026-09-26T00:00:00Z", "series": [BASE]}
        new = copy.deepcopy(old)
        new["series"][0]["nextSeasonNumber"] = 2
        with self.assertRaisesRegex(u.AutomationError, "Season regression"):
            u.validate_dataset(new, registry, old)

    def test_duplicate(self):
        registry = {"series": [ENTRY]}
        data = {"schemaVersion": 1, "generatedAt": "2026-09-26T00:00:00Z", "series": [BASE, BASE]}
        with self.assertRaisesRegex(u.AutomationError, "Duplicate"):
            u.validate_dataset(data, registry)

    def test_newer_season(self):
        fact = u.detect(article("Tulsa King renewed for season 4"), ENTRY, TODAY)
        new, _ = u.merge(BASE, fact, TODAY)
        self.assertEqual(new["nextSeasonNumber"], 4)

    def test_year_only(self):
        f = u.detect(article("Tulsa King season 4 premieres in 2027"), ENTRY, TODAY)[0]
        self.assertEqual((f["releaseDate"], f["releaseYear"]), (None, 2027))

    def test_madison_newest_season_both_orders(self):
        entry = {"tmdbId": 225891, "title": "The Madison", "aliases": ["The Madison"]}
        second = u.detect(article("The Madison renewed for Season 2", "https://www.paramountplus.com/second"), entry, TODAY)
        third = u.detect(article("The Madison renewed for Season 3", "https://www.paramountplus.com/third"), entry, TODAY)
        for facts in (second + third, third + second):
            with self.subTest(order=facts[0]["nextSeasonNumber"]):
                selected, evidence = u.merge(None, facts, TODAY)
                self.assertEqual((selected["nextSeasonNumber"], selected["status"]), (3, "RENEWED"))
                self.assertEqual(selected["sourceUrl"], "https://www.paramountplus.com/third")
                self.assertEqual(evidence["sourceUrl"], selected["sourceUrl"])

    def test_madison_ratings_cannot_own_renewal_source(self):
        entry = {"tmdbId": 225891, "title": "The Madison", "aliases": ["The Madison"]}
        ratings = u.detect(article("The Madison Season 1 ratings reached 8 million; Season 2 already filmed", "https://www.paramountpressexpress.com/ratings"), entry, TODAY)
        renewal = u.detect(article("The Madison renewed for Season 3", "https://www.paramountplus.com/season-3"), entry, TODAY)
        selected, _ = u.merge(None, ratings + renewal, TODAY)
        self.assertEqual((selected["nextSeasonNumber"], selected["status"], selected["sourceUrl"]),
                         (3, "RENEWED", "https://www.paramountplus.com/season-3"))

    def test_collector_reads_all_selected_madison_candidates(self):
        entry = next(e for e in json.loads(u.REGISTRY.read_text(encoding="utf-8"))["series"] if e["title"] == "The Madison")
        old_url = entry["officialUrl"] + "?view=older-season-two"
        new_url = entry["evidenceUrls"][0]
        pages = {
            entry["officialUrl"]: f'<html><h1>The Madison Releases</h1><a href="{old_url}">Older release</a></html>',
            old_url: "<html><h1>The Madison renewed for Season 2</h1></html>",
            new_url: "<html><h1>The Madison renewed for Season 3</h1></html>",
        }
        def fake_fetch(url, domains):
            self.assertTrue(u.allowed(url, domains))
            return pages[url], url
        facts, count = u.collect(entry, None, TODAY, fake_fetch)
        selected, _ = u.merge(None, facts, TODAY)
        self.assertEqual(count, 2)
        self.assertEqual({f["nextSeasonNumber"] for f in facts}, {2, 3})
        self.assertEqual((selected["nextSeasonNumber"], selected["sourceUrl"]), (3, new_url))

    def test_final_status_source_precedes_separate_date_source(self):
        final = u.detect(article("Tulsa King fourth and final season", "https://www.paramountpressexpress.com/final"), ENTRY, TODAY)
        premiere = u.detect(article("Tulsa King season 4 premieres October 16, 2026", "https://www.paramountpressexpress.com/date"), ENTRY, TODAY)
        selected, evidence = u.merge(None, final + premiere, TODAY)
        self.assertEqual((selected["status"], selected["releaseDate"]), ("FINAL_SEASON", "2026-10-16"))
        self.assertEqual(selected["sourceUrl"], "https://www.paramountpressexpress.com/final")
        self.assertEqual(evidence["supportingSourceUrls"], ["https://www.paramountpressexpress.com/date"])

    def test_wbd_primary_403_official_fallback_succeeds(self):
        entry = next(e for e in json.loads(u.REGISTRY.read_text(encoding="utf-8"))["series"] if e["provider"] == "WBD")
        existing = next(r for r in json.loads(u.DATA.read_text(encoding="utf-8"))["series"] if r["tmdbId"] == 111803)
        fallback = entry["fallbackUrls"][0]
        def fake_fetch(url, domains):
            self.assertTrue(u.allowed(url, domains))
            if url == entry["officialUrl"]:
                raise u.AutomationError("HTTP 403")
            if url == fallback:
                return "<html><h1>HBO Original THE WHITE LOTUS Season 4 Begins Filming In France</h1><p>The White Lotus season 4 begins filming.</p></html>", url
            raise AssertionError(f"unexpected URL {url}")
        facts, count = u.collect(entry, existing, TODAY, fake_fetch)
        selected, evidence = u.merge(existing, facts, TODAY)
        self.assertEqual(count, 1)
        self.assertIsNone(evidence)
        self.assertEqual((selected["nextSeasonNumber"], selected["status"], selected["sourceUrl"]),
                         (4, "RENEWED", existing["sourceUrl"]))

    def test_wbd_all_fallbacks_fail(self):
        entry = next(e for e in json.loads(u.REGISTRY.read_text(encoding="utf-8"))["series"] if e["provider"] == "WBD")
        original = next(r for r in json.loads(u.DATA.read_text(encoding="utf-8"))["series"] if r["tmdbId"] == 111803)
        attempted = []
        def fake_fetch(url, domains):
            attempted.append(url)
            raise u.AutomationError({entry["officialUrl"]: "HTTP 403", entry["fallbackUrls"][0]: "timeout", entry["fallbackUrls"][1]: "HTTP 500", original["sourceUrl"]: "HTTP 403"}.get(url, "failed"))
        with self.assertRaisesRegex(u.AutomationError, "No usable official releases"):
            u.collect(entry, original, TODAY, fake_fetch)
        self.assertEqual(attempted[:3], [entry["officialUrl"]] + entry["fallbackUrls"])

    def test_registry_rejects_unofficial_fallback(self):
        registry = json.loads(u.REGISTRY.read_text(encoding="utf-8"))
        next(e for e in registry["series"] if e["provider"] == "WBD")["fallbackUrls"].append("https://example.com/fake")
        with self.assertRaisesRegex(u.AutomationError, "Invalid registry entry"):
            u.validate_registry(registry)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.data = root / "data.json"
        self.registry = root / "sources.json"
        self.audit = root / "audit.jsonl"
        real = json.loads(u.REGISTRY.read_text(encoding="utf-8"))
        self.registry.write_text(json.dumps(real), encoding="utf-8")
        original = json.loads(u.DATA.read_text(encoding="utf-8"))
        self.data.write_text(json.dumps(original), encoding="utf-8")
        self.patches = [patch.object(u, "DATA", self.data), patch.object(u, "REGISTRY", self.registry), patch.object(u, "AUDIT", self.audit)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_http_failure_preserves_data(self):
        before = self.data.read_bytes()
        def fail(entry, old, today):
            raise u.AutomationError("HTTP 503")
        result = u.run(False, fail, TODAY)
        self.assertEqual(len(result["failures"]), 12)
        self.assertEqual(self.data.read_bytes(), before)

    def test_mobland_weak_s2_date_does_not_fail_provider(self):
        before = self.data.read_bytes()
        def collector(entry, old, today):
            if entry["title"] != "MobLand":
                return [], 1
            source = {"title": "MOBLAND RENEWED FOR SEASON THREE ON PARAMOUNT+",
                      "body": "September 3, 2026 - MobLand has been renewed for a third season, ahead of the season two premiere on Friday, September 18.",
                      "url": old["sourceUrl"], "sourceName": "Paramount Press Express"}
            return u.detect(source, entry, today), 1
        result = u.run(True, collector, TODAY)
        mobland = next(r for r in result["reports"] if r["title"] == "MobLand")
        self.assertEqual(mobland["result"], "unchanged")
        self.assertFalse(result["failures"])
        self.assertFalse(result["changes"])
        self.assertEqual(self.data.read_bytes(), before)

    def test_exhausted_incomplete_read_isolated_and_preserves_data(self):
        before = self.data.read_bytes()
        def collector(entry, old, today):
            if entry["title"] != "Tulsa King":
                return [], 1
            class Opener:
                def open(self, request, timeout):
                    return Response(request.full_url, IncompleteRead(b"partial HTML", 100))
            with patch.object(u, "build_opener", return_value=Opener()), patch.object(u.time, "sleep"):
                u.fetch(entry["officialUrl"], entry["allowedDomains"])
        result = u.run(False, collector, TODAY)
        self.assertEqual(len(result["failures"]), 1)
        self.assertIn("IncompleteRead", result["failures"][0])
        self.assertEqual(result["reports"][0]["result"], "failed")
        self.assertTrue(all(report["result"] == "unchanged" for report in result["reports"][1:]))
        self.assertEqual(self.data.read_bytes(), before)
        self.assertFalse(self.audit.exists())

    def test_malformed_html_preserves_data(self):
        def malformed(entry, old, today):
            u.parse_page("<html><body>no heading</body></html>")
        self.assertEqual(len(u.run(False, malformed, TODAY)["failures"]), 12)
        self.assertFalse(self.audit.exists())

    def test_no_change_no_rewrite(self):
        before = self.data.read_bytes()
        result = u.run(False, lambda entry, old, today: ([], 1), TODAY)
        self.assertFalse(result["changes"])
        self.assertEqual(self.data.read_bytes(), before)

    def test_simulated_renewal_and_release(self):
        def collector(entry, old, today):
            if entry["title"] == "Lioness":
                return u.detect(article("Lioness renewed for season 4"), entry, today), 1
            if entry["title"] == "Tulsa King":
                return u.detect(article("Tulsa King season 4 premieres October 16, 2026"), entry, today), 1
            return [], 1
        result = u.run(True, collector, TODAY)
        self.assertFalse(result["failures"])
        self.assertEqual(len(result["changes"]), 1)
        self.assertFalse(self.audit.exists())

    def test_mass_change(self):
        def collector(entry, old, today):
            return u.detect(article(f"{entry['title']} renewed for season 9"), entry, today), 1
        before = self.data.read_bytes()
        result = u.run(False, collector, TODAY)
        self.assertTrue(any("Mass change" in f for f in result["failures"]))
        self.assertEqual(before, self.data.read_bytes())

    def test_healthy_source_updates_despite_other_failure(self):
        def collector(entry, old, today):
            if entry["title"] == "Lioness":
                return u.detect(article("Lioness renewed for season 4"), entry, today), 1
            raise u.AutomationError("provider blocked")
        result = u.run(False, collector, TODAY)
        self.assertEqual(len(result["changes"]), 1)
        self.assertEqual(len(result["failures"]), 11)
        self.assertIn(113962, [r["tmdbId"] for r in json.loads(self.data.read_text())["series"]])
        self.assertTrue(self.audit.exists())

    def test_wbd_total_failure_keeps_white_lotus_and_canonical_bytes(self):
        before = self.data.read_bytes()
        def collector(entry, old, today):
            if entry["provider"] == "WBD":
                raise u.AutomationError("primary 403; fallback timeout; fallback HTTP 500")
            return [], 1
        result = u.run(False, collector, TODAY)
        self.assertEqual(len(result["failures"]), 1)
        self.assertIn("WBD/The White Lotus", result["failures"][0])
        self.assertEqual(self.data.read_bytes(), before)
        self.assertFalse(self.audit.exists())


if __name__ == "__main__":
    unittest.main()
