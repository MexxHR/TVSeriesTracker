import json
from pathlib import Path
import sys
import unittest
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).parent))
import discovery as d
import update as u


class DiscoveryV272Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = json.loads(u.REGISTRY.read_text(encoding="utf-8"))
        cls.specs = d.provider_specs()

    @staticmethod
    def _fetcher(pages, calls=None):
        def fetch(url, domains):
            if calls is not None:
                calls.append((url, tuple(domains)))
            value = pages.get(url)
            if isinstance(value, BaseException):
                raise value
            if value is None:
                raise u.ProviderUnavailable("HTTP 404")
            return value if isinstance(value, tuple) else (value, url)
        return fetch

    def _discover(self, metadata, pages, calls=None):
        return d.discover(metadata, self.registry, self._fetcher(pages, calls), self.specs)

    def test_tmdb_mapping_requires_exact_network_and_avoids_company_homepage_routing(self):
        self.assertEqual(d.route_provider({"tmdbId": 1, "title": "X", "networks": [{"name": "AMC"}]}, self.specs)[0], "AMC")
        self.assertEqual(d.route_provider({"tmdbId": 1, "title": "X", "networks": [{"name": "Disney+"}]}, self.specs)[0], "DISNEY_PLUS")
        for metadata in (
            {"tmdbId": 1, "title": "X", "productionCompanies": [{"name": "AMC"}]},
            {"tmdbId": 1, "title": "X", "homepage": "https://press.disneyplus.com/news"},
            {"tmdbId": 1, "title": "X", "networks": [{"name": "Disney"}]},
            {"tmdbId": 1, "title": "X", "networks": [{"name": "AMC+"}]},
            {"tmdbId": 1, "title": "X", "networks": [{"name": "FX"}, {"name": "Hulu"}]},
        ):
            self.assertEqual(d.route_provider(metadata, self.specs)[0], None)
        ambiguous = {"tmdbId": 1, "title": "X", "networks": [{"name": "AMC"}, {"name": "Disney+"}]}
        self.assertEqual(d.route_provider(ambiguous, self.specs)[2], "ambiguous_provider")

    def test_real_qualified_amc_interview_url_matches_possessive_title(self):
        url = ("https://www.amcglobalmedia.com/2026/07/24/"
               "amc-global-media-renews-anne-rices-interview-with-the-vampire-for-a-fourth-season-")
        self.assertTrue(d._amc_article_link(url, ["amcglobalmedia.com"],
                                            ["Anne Rice's Interview with the Vampire"]))

    def test_three_amc_series_are_found_through_the_validated_first_party_get_search(self):
        for index, title in enumerate(("Dark Winds", "The Walking Dead: Daryl Dixon", "Anne Rice's Interview with the Vampire"), 1):
            with self.subTest(title=title):
                path_title = d.slug(title).replace("rice-s", "rices")
                url = f"https://www.amcglobalmedia.com/2026/01/0{index}/{path_title}-renewed-for-season-two/"
                search = "https://www.amcglobalmedia.com/?" + urlencode({"s": title})
                pages = {
                    d.AMC_SEARCH_HOME: '<form method="get" action="/"><input name="s" type="search"></form>',
                    search: f'<h1>Search results</h1><a href="{url}">{title} announcement</a>',
                    url: (f"<title>{title} Renewed for Season Two</title><h1>{title} Renewed for Season Two</h1>"
                         f"<p>AMC has renewed {title} for a second season. The series will return to production with its cast and creative team.</p>"),
                }
                report = self._discover({"tmdbId": 800000 + index, "title": title, "networks": [{"name": "AMC"}]}, pages)
                self.assertEqual(report["result"], "candidate_found")
                candidate = next(row for row in report["candidates"] if row["candidateUrl"] == url)
                self.assertTrue(candidate["factualParserEligible"])
                self.assertTrue(candidate["identityValidated"])
                self.assertEqual(candidate["discoveryProvenance"], "amc_official_site_search")
                self.assertTrue(candidate["redirectWithinOfficialBoundary"])

    def test_three_disney_series_are_found_through_bounded_official_sitemap(self):
        titles = ("Percy Jackson and the Olympians", "Wizards Beyond Waverly Place",
                  "The Proud Family: Louder and Prouder")
        for index, title in enumerate(titles, 1):
            with self.subTest(title=title):
                url = f"https://press.disneyplus.com/news/{d.slug(title)}-renewed-for-season-two"
                sitemap = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                           f"<url><loc>{url}</loc></url><url><loc>{url}</loc></url></urlset>")
                pages = {
                    "https://press.disneyplus.com/sitemap.xml": sitemap,
                    url: (f"<title>{title} Renewed for Season Two</title><h1>{title} Renewed for Season Two</h1>"
                          f"<p>Disney+ has renewed {title} for a second season. The announcement includes details about the series and production.</p>"),
                }
                report = self._discover({"tmdbId": 810000 + index, "title": title, "networks": [{"name": "Disney+"}]}, pages)
                self.assertEqual(report["result"], "candidate_found")
                candidate = next(row for row in report["candidates"] if row["candidateUrl"] == url)
                self.assertTrue(candidate["factualParserEligible"])
                self.assertTrue(candidate["identityValidated"])
                self.assertEqual(candidate["discoveryProvenance"], "disney_official_sitemap")

    def test_fetch_bounds_duplicate_sitemap_urls_and_official_host_filter(self):
        urls = [f"https://press.disneyplus.com/news/safe-show-story-{n}" for n in range(10)]
        xml = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(
            f"<url><loc>{url}</loc></url>" for url in (urls[0], urls[0], *urls[1:],
                "https://press.disneyplus.com.evil.example/news/safe-show-story")) + "</urlset>"
        pages = {"https://press.disneyplus.com/sitemap.xml": xml}
        for url in urls[:4]:
            pages[url] = ("<h1>Safe Show Season Two Renewed</h1><p>Safe Show is renewed for its second season. "
                          "Production details follow in this long official announcement.</p>")
        calls = []
        report = self._discover({"tmdbId": 820001, "title": "Safe Show", "networks": [{"name": "Disney+"}]}, pages, calls)
        self.assertLessEqual(len(calls), 1 + d.MAX_SITEMAP_CHILDREN + d.MAX_SITEMAP_ARTICLES)
        self.assertEqual(len([call for call in calls if call[0] == urls[0]]), 1)
        self.assertLessEqual(len(report["candidateUrls"]), d.MAX_SITEMAP_ARTICLES)
        self.assertTrue(all(u.allowed(call[0], ["press.disneyplus.com"]) for call in calls))

    def test_offdomain_final_url_and_ambiguous_article_identity_never_become_eligible(self):
        title = "Safe Show"
        url = "https://press.disneyplus.com/news/safe-show-renewed-season-two"
        sitemap = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                   f"<url><loc>{url}</loc></url></urlset>")
        pages = {"https://press.disneyplus.com/sitemap.xml": sitemap,
                 url: ("<h1>Safe Show: A Different Series Season Two Renewed</h1>"
                       "<p>Safe Show is renewed for season two, with more official series information here.</p>")}
        report = self._discover({"tmdbId": 830001, "title": title, "networks": [{"name": "Disney+"}]}, pages)
        self.assertEqual(report["candidateUrls"], [])
        pages[url] = ("<h1>Safe Show Renewed for Season Two</h1><p>Safe Show is renewed for season two. "
                      "This official announcement includes more information about the series.</p>",
                      "https://evil.example/news/safe-show-renewed-season-two")
        report = self._discover({"tmdbId": 830001, "title": title, "networks": [{"name": "Disney+"}]}, pages)
        self.assertEqual(report["candidateUrls"], [])
        self.assertTrue(any("redirect_outside_allowlist" in row["reasons"] for row in report["rejectedCandidates"]))

    def test_invalid_sitemaps_and_unvalidated_amc_search_form_fail_closed(self):
        metadata = {"tmdbId": 830002, "title": "Safe Show", "networks": [{"name": "Disney+"}]}
        sitemap = "https://press.disneyplus.com/sitemap.xml"
        for xml in ("<urlset>", "<!DOCTYPE foo><urlset></urlset>", "x" * (d.MAX_SITEMAP_BYTES + 1)):
            with self.subTest(xml=xml[:20]):
                report = self._discover(metadata, {sitemap: xml})
                self.assertEqual(report["candidateUrls"], [])
        amc = {"tmdbId": 830003, "title": "Safe Show", "networks": [{"name": "AMC"}]}
        for form in ('<form method="post" action="/"><input name="s"></form>',
                     '<form method="get" action="https://evil.example/"><input name="s"></form>',
                     '<form method="get" action="/"><input name="other"></form>'):
            with self.subTest(form=form):
                calls = []
                report = self._discover(amc, {d.AMC_SEARCH_HOME: form}, calls)
                self.assertEqual(report["candidateUrls"], [])
                self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
