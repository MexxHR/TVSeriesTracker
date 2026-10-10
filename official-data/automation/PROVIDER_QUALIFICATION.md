# V2.7.1 Provider Discovery Qualification

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

## Current limits

Disney+ `/news` shows recent material, so finding older cases through a generic index remains to be proved. AMC has official archive pagination at `/press-/page/N/`, but a bounded traversal still needs to demonstrate retrieval of each known case. FX's confirmed Lowdown article is reached from its show headline page; the Mayans M.C. and Snowfall probes are series pages, not independent press articles. Hulu has multiple press releases, but the Deli Boys and Reasonable Doubt show-specific indexes alone do not prove provider-wide discovery. A conservative failure on these cases is an expected qualification outcome, not a reason to add known URLs to a production adapter.

The V2.7.1 ZIP excludes the five protected live files: canonical JSON, production audit, sources registry, monitored registry, and monitored audit. Apply it as a code supplement to the accepted GitHub main, preserving those live copies.
