# V2.4 Phase 1: read-only official-source discovery

Run `python official-data/automation/update.py --discover 95396` from the project root. The CLI reads TMDB TV details using `TMDB_API_TOKEN` from the environment or ignored `local.properties`. For an offline fixture, add `--metadata-file path/to/metadata.json`. Minimum fixture fields are `tmdbId` and `title`; optional routing fields are `originalName`, `networks`, `productionCompanies`, `homepage`, and `originCountry`. The output is structured JSON. Missing routing evidence yields `insufficient_evidence`; a blocked official site yields `provider_unavailable`. Neither result is an Official Data status.

The separate **Discover official source (read-only)** GitHub workflow has only `workflow_dispatch` and `contents: read`. It accepts a TMDB TV ID and uses the existing `TMDB_API_TOKEN` repository secret. It neither commits nor runs on a schedule. The V2.3.5 daily production workflow, its 12 trusted `sources.json` entries, parsers, retry behavior and Android sync are unchanged. The project copy of canonical JSON and audit was synchronized byte-for-byte with the already published V2.3.5 GitHub feed (12 records, two audit entries) so a project upload cannot roll those facts back. Discovery itself never modifies either file. For an ID already in `sources.json`, the trusted entry wins and no TMDB or discovery fetch occurs.

## Decision path

1. TMDB is queried only for identity and provider routing metadata. TMDB status, seasons and dates are discarded. No discovered candidate is fed to the factual parser in Phase 1.
2. Exact network or production-company names and a parsed HTTPS official homepage hostname route to one provider. Conflicting providers or an unknown provider stop discovery. A homepage alone can route but cannot make a candidate `STRONG`.
3. `official-data/discovery_providers.json` centrally defines provider names, official domains, candidate domains and provider-owned seed URLs. The existing `update.allowed()` enforces HTTPS, parsed host boundaries, no credentials and a safe redirect target. `netflix.com.evil.example` is rejected.
4. Provider-owned release or show news indexes are fetched with the existing 3-attempt/8 MB HTTP layer. Only bounded, allowlisted article links with the complete title as a slug token are followed. Each fetched page must have an exact show-index heading, Netflix's controlled show-index heading, or a constrained title-led article heading; partial title, unrelated show, remake and spin-off headings are rejected. Generic provider landing pages are not candidates.
5. `STRONG` requires show-index identity, release/news structure, a TMDB network route **and an official page link with the same distinctive show ID as TMDB's official homepage**. `SUPPORTED` can identify an official article or an index without that unique cross-check. `INSUFFICIENT` creates no validated candidate. Only `STRONG` is marked `factualParserEligible`, and even it is **not parsed or published in Phase 1**. This is a deterministic eligibility hint, not verified evidence.
6. `attemptedOfficialUrls` are proposed provider-owned URLs; `candidateUrls` contains only URLs that passed a fetch and identity check. A blocked WBD URL can appear in the former but never the latter. Warnings explain fetch failures without inventing evidence.

| Provider | Phase 1 provider-owned mechanism |
| --- | --- |
| PARAMOUNT | Paramount Press Express show release listings under Paramount+ and Television Studios |
| NETFLIX | Tudum show index and bounded official article links |
| APPLE | Apple TV Press show news index and show press page |
| AMAZON | About Amazon entertainment index and bounded title-matched news links |
| WBD | WBD Pressroom property media-release listings; access blocks are reported |

Adding a provider later means adding one registry entry and, only if its official page structure differs, a small structure/link rule. Search snippets, third-party sites, paid services, AI summaries and access-control bypasses are never used.

## Request transport

The Android phone's local Room watchlist is not available to GitHub Actions. Phase 1 uses a **manual workflow-dispatch TMDB ID** as the smallest safe request mechanism; a maintainer can also run the CLI locally. A later repo-hosted request queue can accept reviewed pull requests or authenticated GitHub Issues, but an automatic phone-to-GitHub request needs an authenticated service or explicit user-owned GitHub authorization. No PAT or GitHub credential is added to the APK, and the Android app has no new network/write behavior. This separation leaves discovery independent of the future request transport.

## Five live discovery probes (2026-09-28)

These are read-only source-discovery results, **not** verified lifecycle facts. Their availability can differ on GitHub's runner.

| TMDB ID | Series / provider | Local result |
| --- | --- | --- |
| 111110 | ONE PIECE / Netflix | `candidate_found`, `STRONG`; Tudum show index matches the unique Netflix homepage show ID; official article links also found |
| 95396 | Severance / Apple | `candidate_found`, `STRONG`; official Apple TV Press show news index |
| 157744 | 1923 / Paramount | `provider_unavailable`; both official Press Express paths returned access blocks locally |
| 213306 | Cross / Amazon | `candidate_found`, `SUPPORTED`; official About Amazon article found from entertainment index |
| 100088 | The Last of Us / WBD | `provider_unavailable`; official Pressroom property paths returned HTTP 403 locally |

The provider-owned pages are [Netflix Tudum ONE PIECE](https://www.netflix.com/tudum/one-piece), [Apple Severance news](https://www.apple.com/tv-pr/originals/severance/news/), [Paramount 1923 releases](https://www.paramountpressexpress.com/paramount-plus/shows/1923/releases/), [Amazon entertainment news](https://www.aboutamazon.com/entertainment-news), and [WBD The Last of Us releases](https://press.wbd.com/us/property/last-us/media-releases). They are discovery locations; no status from them is written by this feature.

Known limits: provider HTML and URL patterns can change; region restrictions can block Paramount/WBD; common or identical show titles can remain ambiguous; index truncation can miss older Amazon articles; and discovery does not establish which season a story concerns. A `SUPPORTED` candidate still needs stronger show identity and factual-evidence review before any future automatic publishing. False negatives are preferred.
