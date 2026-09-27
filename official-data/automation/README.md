# Official Data automation (V2.3)

`.github/workflows/update-official-data.yml` runs daily at 07:17 UTC and via `workflow_dispatch`. Manual runs default to `dry_run=true`. It uses Python 3.12 standard library and the repository `GITHUB_TOKEN` (`contents: write`); no paid service, LLM, PAT or server is required. The Android app continues to consume the same schemaVersion 1 JSON from raw GitHub. Startup refresh (stale after 24 hours), Settings refresh and Room cache already exist.

Run locally from the repository root:

```bash
python -m unittest discover -s official-data/automation -p 'test_*.py' -v
python official-data/automation/update.py --dry-run
```

`sources.json` registers the 12 fixed initial titles by existing `tmdbId`, title, provider, official URL, allowed domains and aliases. Provider adapters are Paramount Press Express, Netflix Tudum, Apple TV Press, WBD Pressroom and Amazon About Amazon. Each adapter selects a small number of candidate official release links. Existing canonical source URLs are also checked. The updater fetches at most three article candidates per title plus the listing, with a 12-second timeout, 8 MB response limit, identifying User-Agent, and two backoff retries only on transient HTTP/network errors. Every request, redirect and accepted evidence URL must remain HTTPS and within the title's allowlist. No search result or third-party page becomes evidence.

Detection requires the series name and season in the same title/sentence as an explicit renewal, final-season, cancellation or premiere statement. A date needs a premiere verb and a valid month/day/year; when an official release omits the year, the announcement year is used only if unambiguous. A year-only premiere keeps `releaseDate=null`. Cancellation requires an explicit cancellation/no-return statement. Absence of news never means cancellation. False negatives are preferable to speculative facts.

All fetching/detection finishes before validation/writing. The validator checks registry identity, schema, dates, status, HTTPS/domain, duplicate IDs, nonempty source, generatedAt and season monotonicity, preserved old records, and a maximum of **four factual series changes per run**. A failed provider, access block or parser failure preserves that series record and is named in the Actions log. Independently verified changes from healthy providers may still be written and committed; the workflow then ends failed to surface the blocked provider. Global validation failure, including the mass-change guard, prevents every write. A provider block is a known V2.3 limitation for that provider, not a reason to trust another source for its title.

Conflict rules: the highest season wins; same-season final/canceled status is sticky, while a verified premiere date is an independent fact. A date upgrades `RENEWED` to `RELEASE_DATE_CONFIRMED`, but never erases `FINAL_SEASON`. A newer season resets the old season's date. Same-season lower-grade news cannot downgrade an existing date. Existing records remain untouched on no evidence, HTTP error or parser error. `lastChecked` in a published record is the successful source check associated with its last factual update. Routine successful checks appear in the Actions log and do not rewrite the feed daily. `generatedAt` advances only when factual canonical data change. Only then is `official-data/history/changes.jsonl` appended with UTC timestamp, ID/title, old/new record, source URL, provider and detection rule, followed by a bot commit. No HTML is logged.

To add a series later, first determine the correct TMDB TV ID and official source, add a registry entry and provider link-selection rule if needed, add fixtures, and validate the canonical data. V2.3 intentionally does not generate registry entries for arbitrary TMDB additions. The bundled Android asset remains the release-time offline fallback; automated remote updates do not rewrite the asset or APK.

Provider HTML and geographic access can change. For example, Paramount Press Express currently returns a geographic block from this development environment, and WBD may return HTTP 403. Such runs report failure and do not alter production JSON. GitHub-hosted runners may have different access; a scheduled run must be observed before claiming successful unattended updates. Direct article discovery for Netflix and Amazon is limited; V2.4 should add durable provider-owned feeds or listing endpoints where available.

