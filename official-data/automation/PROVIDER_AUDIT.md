# V2.7.0 Official Provider Coverage Audit

The manual **Provider Coverage Audit** GitHub Actions workflow runs
`provider_audit.py --all` on a GitHub-hosted runner. It has `contents: read`
permission and no backend, TMDB, or GitHub write credential. The JSON report is
attached to that run; the workflow summary lists recommendations. A blocked
provider is a valid audit observation, so HTTP 403/429 does not fail the job.

The audit inventory is `official-data/provider_audit_registry.json`. It is
separate from `official-data/discovery_providers.json` and the trusted
`official-data/sources.json`. Audit entries are probes, not production
allowlist entries or verified series facts. The tool reads public first-party
pages with bounded HTTPS requests and never calls the monitored or production
publishers. It does not bypass blocked sites.

Run locally from the repository root:

```bash
python official-data/automation/provider_audit.py --all
python official-data/automation/provider_audit.py --provider DISNEY_PLUS --summary
```

The default output is machine-readable JSON. A local result describes only the
local network; GitHub-runner accessibility must be decided from the manual
workflow artifact. Inspect each probe's fetch, link-discovery and parser fields
separately. A fetched hardcoded article does not prove deterministic discovery,
and an extracted phrase does not become production truth. The audit recommends
`READY_FOR_IMPLEMENTATION` only when its evidence for access, discovery and
parser suitability is sufficient; otherwise it reports a narrower category or
`NEEDS_MORE_RESEARCH`.

After the first GitHub run, review the full artifact and the linked original
official pages before choosing V2.7.1 candidates. V2.7.0 does not add provider
adapters, widen production domains, or alter the accepted promotion gates.
