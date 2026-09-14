# Safe local CPSC source refresh

The source client uses only CPSC's documented [Recalls REST API](https://www.cpsc.gov/Recalls/Recalls-RSS) root, `https://www.saferproducts.gov/RestWebServices/Recall`, requesting JSON. Every retained record keeps the CPSC notice URL supplied by that response; a non-HTTPS, non-CPSC notice URL rejects the revision.

## Dependency decision

The refresh client uses Python 3.13's standard-library `urllib.request`, not a new HTTP, retry, scheduler, or telemetry dependency. The official [urllib documentation](https://docs.python.org/3/library/urllib.request.html) supplies an explicit per-request timeout and typed HTTP/transport failure behavior. It is the smallest choice for one short-lived, founder-invoked local command: no additional runtime dependency, no hidden scheduler retry policy, deterministic injected fetch seam in tests, and straightforward maintenance. Exponential retry is bounded to three total attempts; no scheduler is implemented, so no outer retry can multiply that limit.

This remains a local product-development workflow. Ordinary tests and local E2E replay recorded fixtures and never contact CPSC. A live smoke is deliberately human-invoked, validates the received schema without promoting a revision, and never receives an operator submission:

```console
uv run agent-data-oracle job cpsc-live-smoke
```

## Refresh commands

Use a recorded fixture while developing or testing:

```console
uv run agent-data-oracle job cpsc-refresh --mode weekly \
  --fixture tests/fixtures/cpsc/recall-10887.json \
  --expected-record-count 1 \
  --observed-at 2026-09-14T12:00:00Z
```

The same command without `--fixture` makes the bounded official-source request. `daily` requests a seven-day `LastPublishDateStart` overlap from the last completed source observation. `weekly` performs complete reconciliation; a missing prior record becomes an immutable tombstone, never a deletion. A daily refresh needs a prior completed revision; use a weekly refresh first.

`cpsc-status` reports the last run and source status. A completed revision may serve new work for no more than 48 hours from its source observation. A transient refresh failure remains visible during that window; past it, new batches and daily refreshes are disabled. A founder can recover only with a later complete weekly reconciliation. Invalid source schema, conflicting identity, incomplete declared count, or official-notice provenance immediately activates the database-controlled global pause rather than receiving stale-data grace.
