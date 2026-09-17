# Local operator data rights

Operator data is kept separately from the shared, immutable CPSC source history.
A currently re-authenticated declared operator can download portable JSON at
`/account/data/export` and create deletion work at
`/account/data/deletion-requests`. Exports include the account declaration,
submitted inputs, released-evaluation metadata and evidence rows, acknowledgement history,
agent-review reports, and delegated-key metadata. They deliberately omit key
secrets and secret hashes, founder audit notes, and every other operator's data.

A deletion request revokes every browser session and delegated key immediately.
It is then completed by the local cleanup job, which erases the operator-linked
records and emits a receipt containing only data categories, completion time,
and outcome. The receipt has no operator identifier. CPSC source payloads,
revisions, and recall history are not deleted or changed.

## Local cleanup

Run the cleanup boundary explicitly so local verification is reproducible:

```console
uv run agent-data-oracle job data-cleanup --now 2026-10-04T10:00:00Z
```

The command completes unheld deletion work, removes refresh links whose successor evaluation is 30 days old, and expires unheld founder-test data at
30 days (inclusive of the cutoff), and removes authentication, delegated-key
rate-limit, and sign-in-admission log records at 14 days. An active incident
hold can be recorded locally with a non-empty category:

```console
uv run agent-data-oracle job data-retention-hold --operator-id <uuid> --reason-category security_incident --recorded-at 2026-09-04T10:00:00Z
```

A hold prevents automatic expiry and defers deletion completion. If held work
remains pending at the seven-calendar-day response deadline, cleanup invokes
the existing global-pause path. Resolve the incident and release the hold only
through a future controlled maintenance action; no public automation is enabled.

Application request logs are emitted only to process stdout and are not retained by the application; database-backed authentication, delegated-key rate-limit, and sign-in-admission records are pruned at 14 days. There are no backup artifacts in the current local founder-preview posture, so
there is no claim of surgical backup deletion or restore capability. Any future
backup lifecycle, expiry, and restore proof belongs to #33.