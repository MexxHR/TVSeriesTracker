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
   It checks that production files are unchanged and commits only the monitored
   registry and audit. Its `queue: max` concurrency group is shared with the
   manual Phase 2A writer. GitHub currently supports up to 100 pending runs in
   that group; excess runs can be canceled and require operational monitoring.
   It never promotes staging to canonical production data.
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
7. Result retrieval in 2B.1 remains the existing official-data sync, after a
   separate future promotion decision. `queued` and `already_queued` are
   transport states, never verified factual statuses. No staging status is
   displayed as production truth.
8. The backend URL is public build configuration and may be empty. Empty config
   makes the worker exit in a controlled unavailable state; watchlist use remains
   local. The same backend serves two fixed read-only TMDB metadata routes for
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
workflow `process-series-request.yml` has Contents:write only to commit monitored
files. It does not receive the backend dispatch credential. Configure edge rate
limits too; core SQLite limits remain active if edge rules are unavailable.

Local tests inject a fake dispatcher and a temporary SQLite database. They do
not require or use a GitHub credential. `python backend/dev_server.py` starts a
loopback-only WSGI endpoint with a fake dispatcher; never expose that mode
publicly. Android requires HTTPS, so this HTTP test endpoint is for backend
integration tests rather than a phone build.
