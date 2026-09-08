# Agent API v1

`/api/v1` provides the same immutable evidence-queue contract shown to an
operator. It is for an external agent delegated by that operator; it is not an
autonomous action, safety assessment, compliance service, or authority to take
downstream action.

Create a key from the authenticated **Delegated agent keys** page. Its complete
secret is displayed once. Send it only as `Authorization: Bearer <secret>`.
Keys may have `queues:submit`, `queues:read`, `queues:refresh`,
`evidence:read`, and `reviews:report-agent` scopes. Each ordinary request is
limited to 30 per key per rolling minute. Revocation is immediate.

## Queue endpoints

- `POST /api/v1/queues` requires `queues:submit` and an `Idempotency-Key` of
  16–128 characters. Its JSON body is exactly
  `{"identifiers":[{"type":"upc|model|brand","literal":"..."}]}`.
- `GET /api/v1/queues/{evaluation_id}` requires `queues:read`.
- `GET /api/v1/queues/{evaluation_id}/evidence` requires `evidence:read` and
  records a bounded source-evidence retrieval event. That event contains no
  submitted identifier or evidence text.
- `POST /api/v1/queues/{evaluation_id}/reviews` requires
  `reviews:report-agent`. Its JSON body is exactly
  `{"outcome":"reviewed_same_product|reviewed_different_product|need_more_identifiers|not_sure"}`.
  It appends an external-agent review report; it cannot create or supersede a
  human acknowledgement.

Successful released responses include `contract_version: "v1"` and the full
mandatory evidence document plus a `reviews` object. That object keeps the
current human acknowledgement, its immutable supersession history, and
external-agent reports distinct. Neither kind of report is a safety, legal,
recall-status, or removal finding. A candidate-bearing queue held for founder
audit is not disclosed. Reusing an idempotency key with different identifiers
returns `409`; the same identifiers replay the original immutable result.

Errors use `application/problem+json` with a stable `code`, including
`authentication_required`, `insufficient_scope`, `rate_limited`,
`validation_failed`, `idempotency_conflict`, `source_unavailable`,
`global_pause`, and `resource_not_found`. `429` responses include
`Retry-After`. Interactive API consoles are disabled; the OpenAPI document is
available at `/api/v1/openapi.json`.
