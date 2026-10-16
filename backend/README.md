# Request service modes

The request service keeps its deployed behavior unless an operator explicitly sets
`SERIES_REQUEST_MODE=batch`. The default is `legacy`, which still requires
`TARGET_REPOSITORY`, `GITHUB_TOKEN`, and `GITHUB_REF` and dispatches the existing
per-request workflow. In `batch` mode, `POST /v1/series-requests` writes a durable
pending obligation before returning 202 and does not construct or call the GitHub
dispatcher. Batch mode requires `SERIES_BATCH_API_SECRET`; configure it through
the service environment and the corresponding Actions secret only during a separately
authorized deployment. Never reuse `GITHUB_TOKEN` for this secret.

Batch endpoints are intended for HTTPS only. The service rejects HTTP on the
authenticated internal routes. GitHub Actions should send the secret as a Bearer
authorization value and must not print the request or response headers. The snapshot
route accepts only `{}` and returns at most three due IDs with opaque receipts and
HMAC proofs. Snapshot advances each selected row's `next_eligible_at` by 600 seconds;
it does not claim or remove work. The completion route accepts one to three exact
`tmdbId`/`requestReceipt`/`ackProof` items. The public status route accepts one exact
ID/receipt pair and returns only `pending`, `complete`, or `unknown`. A superseded
or incorrect receipt is also `unknown`, so arbitrary receipts cannot reveal queue
membership. Status shares the existing metadata quota (120/client/hour, 3000/global),
preserving the separate admission quota (10/client/hour, 200/global).
Responses carry `Cache-Control: no-store`.

The additive `pending_requests` table has exactly five fields: `tmdb_id`,
`request_receipt`, `requested_at`, `next_eligible_at`, and nullable `completed_at`.
Pending rows have no TTL. Completed rows are removed after seven days. The existing
request and rate-event tables are preserved. `RequestStore.import_legacy_requests()`
is an explicit staged migration helper and is never invoked at service startup; it
imports only still-live rows inside the legacy 24-hour acknowledgement window as
pending, leaves old rows and quota events intact, and is safe to repeat. It does not
interpret an old dispatch acknowledgement as completed processing.

Adding this code does not activate batch mode, configure secrets, schedule a workflow,
or change production data. Keep the deployed environment in its current mode until a
separately reviewed cutover is authorized.

Phase 1 workflow integration is manual-only: `controlled-series-batch.yml` and
`refresh-monitored-series.yml` default to local dry-run validation (zero API/provider
calls). Live code paths additionally require the exact `true` value for
`SERIES_BATCH_PROCESSING_ENABLED` or `SERIES_MONITORED_REFRESH_ENABLED` respectively.
Canonical writes still require `OFFICIAL_DATA_PROMOTION_ENABLED=true`. No gate or
secret is configured by this change. Future cadence after Phase 3 review is
`3,13,23,33,43,53 * * * *` for requests and `43 7 * * *` for monitored refresh;
these are documented cron expressions, not installed schedules.

GitHub configuration for the request consumer: `SERIES_BATCH_API_URL` is the HTTPS
origin only; `SERIES_BATCH_API_HOST` is the separately configured exact hostname;
`SERIES_BATCH_API_SECRET` is a dedicated Actions secret matching the backend
environment. Redirects are forbidden and replies are strictly bounded/validated.
Do not log receipts/proofs. The consumer does not retry an uncertain push; it leaves
pending membership for the next clean run.

Rollback must retain pending rows accepted during batch mode. Merely switching to
legacy mode will not consume that backlog: drain/reconcile it under a separately
approved cutover/rollback procedure. The explicit importer is for a paced transition,
not periodic housekeeping. It retains expired legacy acknowledgement rows in the
old table for inspection, but only imports the current 24-hour horizon; investigate
known older unfinished work separately rather than silently deleting it. Back up
the durable SQLite file and verify the remaining backlog before retiring any old
credential or dispatcher.
