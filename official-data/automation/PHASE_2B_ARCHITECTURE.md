# Phase 2B.1: monitored request transport

## Decision

Use a small Python WSGI service behind a TLS reverse proxy. Its HTTP core is
provider neutral; `wsgi.py` is the deployment adapter. Deploy it with a durable,
shared SQLite volume (or replace `RequestStore` with a transactional shared store).
Do not run multiple isolated ephemeral replicas: that would defeat global limits
and duplicate suppression. The endpoint is public and anonymous; no account or
device identifier is needed. A fine-grained, repository-scoped GitHub credential
with Actions:write is held only by this service. An installation token from a
GitHub App with the same permission can be supplied instead. GitHub checkout's
own `GITHUB_TOKEN` gets Contents:write only in the monitored workflow.

1. After the Room watchlist insert succeeds, Android enqueues unique WorkManager
   work per TMDB ID. The local add never waits for the network. The worker POSTs
   only `{"tmdbId":123}` to `/v1/series-requests` when connected.
2. The service validates the exact JSON contract and body size, reserves a
   request in SQLite under `BEGIN IMMEDIATE`, and applies per-IP, global, and
   per-ID cooldown limits transactionally. It dispatches the restricted GitHub
   workflow with `tmdb_id` only. The ID is a routing key, not lifecycle evidence.
3. The workflow runs the existing `update.py --process-discovered` entry point.
   That monitored step writes only the monitored registry and audit; it never
   promotes staging directly. A separate Phase 2B.2 step may promote validated
   facts after the monitored step succeeds. Its `queue: max` concurrency group
   is shared with the manual Phase 2A writer and daily production updater.
   GitHub currently supports up to 100 pending runs in that group; excess runs
   can be canceled and require operational monitoring.
4. No user authentication is required for this anonymous feature. Coarse rate
   limits, request size validation, server-side cooldown, and optional edge/WAF
   rules protect it. Configure `TRUSTED_PROXY_IPS` only with actual ingress IPs;
   that proxy must replace `X-Forwarded-For`, never append an untrusted client
   value. Otherwise the service uses `REMOTE_ADDR` and ignores forwarded headers.
   Store only a daily
   HMAC of IP for rate limiting, never the raw address in the application DB or
   monitored registry. Reverse-proxy access logs and their retention need a
   separate deployment privacy policy. Do not collect device IDs or accounts.
5. A duplicate ID in cooldown returns `already_queued` without another dispatch.
   SQLite serialization prevents concurrent duplicates. A GitHub 200/204 response
   means dispatch accepted, not that the workflow ran successfully. A rare lost
   HTTP response after GitHub accepted dispatch can cause a later duplicate;
   monitored processing itself is idempotent.
6. GitHub 429, 5xx and timeouts receive bounded retry. 401/403/404/422 are
   permanent deployment failures (backend HTTP 424). Backend 503/429 and network errors make the
   Android worker retry with exponential backoff; other 4xx stop retrying.
   WorkManager's persisted work survives process death and offline periods.
7. Result retrieval remains the existing official-data sync after a separate
   successful promotion. `queued` and `already_queued` are
   transport states, never verified factual statuses. No staging status is
   displayed as production truth.
8. The backend URL is public build configuration, defaulting to
   `https://mexxhr.pythonanywhere.com`. The same backend serves two fixed read-only TMDB metadata routes for
   Android search/details, with its `TMDB_API_TOKEN` held server-side. These
   routes use a separate hourly SQLite quota (120 requests per client and 3000
   globally), so interactive searches cannot exhaust the stricter dispatch
   quota (10 per client and 200 globally). Both classes retain daily IP HMAC
   pseudonyms and return `Retry-After` on HTTP 429. They do not assert lifecycle facts. Never
   build either backend credential or TMDB token into Android.

## Deployment configuration

Run the `backend.wsgi:application` callable with a production WSGI runner
behind HTTPS, using the repository root as Python working directory and
persistent SQLite storage. Required environment names: `GITHUB_TOKEN`
(fine-grained PAT or GitHub App installation token), `TARGET_REPOSITORY`
(`owner/repo`), `REQUEST_DB_PATH`, `TMDB_API_TOKEN`, `CLIENT_IP_HMAC_KEY`
(random server-only value). Optional: `GITHUB_REF=main`, `TRUSTED_PROXY_IPS`
(comma-separated trusted ingress IPs). Rotate tokens
server-side. No values belong in Git, Android resources, APK or project ZIP.

The backend's GitHub permission is Actions:write for this repository. The
workflow `process-series-request.yml` has Contents:write only for the four
allowlisted monitored/production data and audit files. It does not receive the
backend dispatch credential. Configure edge rate
limits too; core SQLite limits remain active if edge rules are unavailable.

Local tests inject a fake dispatcher and a temporary SQLite database. They do
not require or use a GitHub credential. `python backend/dev_server.py` starts a
loopback-only WSGI endpoint with a fake dispatcher; never expose that mode
publicly. Android requires HTTPS, so this HTTP test endpoint is for backend
integration tests rather than a phone build.

## Phase 2B.2: controlled monitored promotion

The Phase 2B.1 path was verified on a physical phone through the PythonAnywhere
backend, monitored GitHub workflow, and monitored registry. Android still sends
only `POST /v1/series-requests`; it does not read monitored staging or hold a
GitHub or TMDB credential. The backend exposes no publication endpoint. GitHub
Actions performs repository writes with its workflow-scoped token; the backend
credential remains limited to workflow dispatch.

`VERIFIED_FACTS` means a record may enter the independent production promotion
gate. It never means blind publication. Discovery, factual parsing and monitored
storage remain separate from promotion. The gate checks the persisted evidence,
source identity and official-domain policy, fact/season agreement, production
schema, merge result and exact one-series semantic diff before a write. Unknown
or weaker monitored states, including `UNSUPPORTED_PROVIDER`, are successful
monitored outcomes with no production change. Trusted `sources.json` entries
retain precedence and continue through the original daily updater. Promotion
does not change that registry or widen provider eligibility.

The Android-request workflow runs monitored processing first and attempts
promotion only if that step succeeds and the repository Actions variable
`OFFICIAL_DATA_PROMOTION_ENABLED` is `true`. Leave the variable unset during
the first controlled dry-run and review. Once activated, routine eligible
promotions require no manual approval. It can commit only the monitored registry
and audit and, on a successful promotion, canonical JSON and the existing
production audit. Its explicit changed-file allowlist rejects all other paths.
The Android-request, manual monitored writer and daily production updater share
one non-cancelling `official-data-writes` GitHub Actions concurrency group with
the existing maximum queue setting. This serializes read/modify/write of
canonical data across the workflows. Queue limits still require operational
monitoring during bursts.

Promotion builds and validates the complete candidate in memory, then uses
staged writes with verification and restoration of canonical JSON and production
audit on failure. A semantic no-op does not rewrite either file, advance
`generatedAt`, append audit or create a commit. A successful factual change
appends to `history/changes.jsonl` with the previous/new record, official source
URLs, monitored state and automatic-promotion reason. The monitored audit remains
separate. One request may alter only its own TMDB ID. The daily updater's
maximum-four factual-change guard remains in force.

The local project snapshot currently contains an empty monitored registry;
ONE PIECE and Dark Matter acceptance therefore uses isolated fixtures. Older
monitored rows do not contain persisted parser/validation/eligible-source proof
and fail closed. This means a direct dry-run on the legacy ONE PIECE row cannot
yet produce the requested Season 3 candidate. With
`OFFICIAL_DATA_PROMOTION_ENABLED` unset, first preview a refresh through the
manual **Process discovered series (monitored only)** workflow with
`dry_run=true`. Confirm that it remains `VERIFIED_FACTS` with eligible sources;
preserve the existing monitored row/audit for recovery. Then run the same
workflow with `dry_run=false` to write only monitored staging. Next run the
read-only **Preview monitored promotion** workflow on GitHub main with TMDB ID
`111110`, or run this command in a fresh main checkout:

```bash
python official-data/automation/update.py --promote-monitored 111110 --dry-run
```

Review the proposed ONE PIECE Season 3 record, source URLs, evidence, changed
fields and validation result before permitting any live promotion. In
particular, Season 2's 2026-03-10 premiere must not become a Season 3 date.
`--promote-monitored 62425 --dry-run` must report a non-promotable
`UNSUPPORTED_PROVIDER` outcome without changing production data. A live
promotion must not be used as the first acceptance test.

If a publication fails, stop the affected workflow and inspect its validation
report; the writer restores both canonical and audit bytes. If a published
record later needs manual rollback, pause the data-writing workflows, revert
the single promotion commit (canonical JSON and production audit together),
validate both files and their history, then resume workflows. Do not edit only
one side of that pair or rewrite monitored evidence to hide the event.

## Controlled manual production acceptance (V2.5.2)

The **Preview monitored promotion (read-only)** workflow evaluates one TMDB ID
without a repository write. After its ONE PIECE 111110 report and both Git
mutation guards passed on the GitHub runner, the dedicated **Promote monitored
series to Official Data** workflow provides a separate human-dispatched first
production write. It accepts only a positive TMDB ID on `main`, uses the shared
non-cancelling `official-data-writes` queue and workflow-scoped Contents:write,
and runs the existing `update.py --promote-monitored` engine. It snapshots all
five live data/audit files outside the checkout before the command. A second
guard validates the JSON result, exact two-file allowlist, single-series
canonical diff, complete schema, advanced `generatedAt`, official source,
and exactly one matching audit entry before committing. For the first ONE PIECE
acceptance it also checks S3 RENEWED / 2027 with no S3 release date. Other
rejected states fail without a commit.

Run the workflow manually with `tmdb_id=111110` once. Inspect its canonical and
production audit diff and confirm that the bot commit contains only those two
files. Run the exact same workflow and ID again: the required result is
`NO_CHANGE`, byte-identical canonical and audit, and no second commit. The
workflow never commits a no-op. If validation, publication or push fails, stop
and inspect the report and repository before retrying. The Python publisher
restores both files after a local write/verification failure. After an already
pushed erroneous commit, pause data writers and revert canonical and audit
together, then validate the restored dataset and history.

This manual workflow authorization does **not** activate routine promotion.
`OFFICIAL_DATA_PROMOTION_ENABLED` remains unset; the Android-request workflow
still requires that separate future gate. No production promotion is run while
implementing or packaging this workflow.
