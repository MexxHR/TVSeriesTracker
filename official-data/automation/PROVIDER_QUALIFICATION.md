# V2.7.1.1 Provider Discovery Qualification

This read-only extension of the V2.7 provider audit examines FX, Hulu, Disney+ and AMC. It does not register production providers, update monitored records, promote facts, or alter canonical Official Data.

## Run

```text
python official-data/automation/provider_audit.py --qualify --all
python official-data/automation/provider_audit.py --qualify --provider HULU
```

The report is JSON on standard output. Do not write it into a protected Official Data path. The manual GitHub Actions workflow **Provider Discovery Qualification** runs the tests and all four qualifications with read-only repository permission, writes the report to a runner temporary file, checks the checkout for changes, and uploads artifact `provider-discovery-qualification`. Provider HTTP or evidence failure is a case result; test or report failure is a workflow error.

Each case in `provider_audit_registry.json` specifies a real first-party known article or official series page, first-party discovery roots, expected title/facts, and a positive, negative, or ambiguity role. The known URL is ground truth for comparison. It must not be a discovery seed. Discovery follows only bounded links from official roots and enforces the provider's domain, redirect, timeout, and response-size limits. The report records fetch and parser outcomes without article bodies. Series-scoped roots and series overview pages are useful diagnostics but cannot by themselves establish generic announcement discovery.

A qualified provider needs multiple independent series announcements found through generic official surfaces, expected facts from the existing strict parser, passing safety cases, and a real GitHub-hosted runner result. A local success is only `LOCAL_QUALIFICATION_CANDIDATE`. Do not activate a production adapter until the GitHub artifact is inspected.

## Hulu Deli Boys ambiguity

The V2.7 raw `final_season` signal came from a Fred Armisen biography in the Deli Boys renewal article. That biography mentions the eighth and final season of **Big Mouth**. The Deli Boys announcement itself renews **Deli Boys** for season 2. Raw page-wide signal detection is diagnostic; it is not a verified fact. The strict parser must produce only the title-bound Deli Boys renewal and must not turn the Big Mouth sentence into a Deli Boys FINAL_SEASON fact. The deterministic fixture reproduces both sentences and tests this distinction. Production parser semantics remain unchanged.

## V2.7.1.1 gap findings and bounded changes

FX's Mayans M.C. show page has a `FINAL SEASON` heading and a separate `Season 5` heading. The page does not provide a primary editorial statement binding that show, season, and final status in one evidence span. The strict parser's zero facts are therefore the safe result; the Mayans parser remains unchanged. Lowdown and Snowfall controls and weak or unrelated Mayans fixtures preserve this boundary. A show page also does not satisfy the independent announcement requirement by itself.

AMC's Interview with the Vampire press article has a primary editorial sentence that explicitly says the named series was renewed for a fourth season. The strict parser previously recognized `renewed for season N` but missed `renewed [exact series alias] for a fourth season`; curly apostrophes also prevented exact alias matching. The narrow generic direct-object rule now accepts that sentence with the same series, season, and status binding. Split-sentence and cross-series fixtures must continue to yield no fact. Daryl Dixon's known article was outside the bounded first twelve AMC press archive pages. Qualification now validates AMC's first-party GET search form, searches by the registry series title, and follows at most 20 official result links. The known article URL remains only a comparison target and diagnostic, never a seed.

Hulu's official search form can surface some historical releases by series title, including Difficult People and Shrill. The same bounded, first-party form-validation path is used for Hulu qualification. A search result counts only if its official response actually links the known article. The historical Handmaid's Tale corporate press release was not observed in the direct series-title search result; until the actual GitHub-runner artifact proves all cases, Hulu remains unqualified. The Deli Boys / Big Mouth ambiguity rule is unchanged.

Disney+ exposes an official sitemap index at `/sitemap.xml`, linking an official XML URL set that includes the three known historical `/news/` announcements. The qualification crawler reads only same-domain XML sitemap links, caps bytes, entries, children and candidate articles, and selects candidate paths using registry aliases. No known article URL is used to generate candidates. This is a read-only qualification route, not a production discovery adapter.

All four outcomes require the real GitHub-hosted **Provider Discovery Qualification** workflow. Local fixture success, local HTTP observations, and a `LOCAL_QUALIFICATION_CANDIDATE` recommendation are not GitHub-runner acceptance. The V2.7.1.1 ZIP excludes the five protected live files: canonical JSON, production audit, sources registry, monitored registry, and monitored audit. Apply it as a code supplement to the accepted GitHub main, preserving those live copies.
