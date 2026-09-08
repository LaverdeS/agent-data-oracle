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
- `GET /api/v1/queues/{evaluation_id}/evidence` requires `evidence:read`.

Successful released responses include `contract_version: "v1"` and the full
mandatory evidence document. A candidate-bearing queue held for founder audit
is not disclosed. Reusing an idempotency key with different identifiers returns
`409`; the same identifiers replay the original immutable result.

Errors use `application/problem+json` with a stable `code`, including
`authentication_required`, `insufficient_scope`, `rate_limited`,
`validation_failed`, `idempotency_conflict`, `source_unavailable`,
`global_pause`, and `resource_not_found`. `429` responses include
`Retry-After`. Interactive API consoles are disabled; the OpenAPI document is
available at `/api/v1/openapi.json`.
