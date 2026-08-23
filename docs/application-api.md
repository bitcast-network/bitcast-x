# Miner application API compatibility

The authenticated `/api/v1` surface is the stable boundary between a Bitcast X
miner node and any creator product built on it. It is deliberately separate
from the signed validator protocol mounted on the same process. Platform user
sessions, wallet connections, creator payments, and product-specific policy
belong outside this API.

OpenAPI is served at `/api/v1/openapi.json` and Swagger UI at `/api/v1/docs`.
Both require `Authorization: Bearer <BITCAST_X_MINER_API_TOKEN>`, like every
other `/api/v1` route. Use a unique token containing at least 256 bits of
entropy and terminate TLS before exposing a node beyond localhost.

## Resources

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/qualification` | Current miner qualification snapshot |
| `GET` | `/api/v1/ecosystems` | Ecosystems enabled by this operator |
| `GET` | `/api/v1/leaderboard` | Combined, paginated creator ranking |
| `GET` | `/api/v1/campaigns[/{campaign_id}]` | Campaign discovery and detail |
| `GET` | `/api/v1/campaigns/{campaign_id}/eligibility/{creator_x_id}` | Creator eligibility |
| `GET` | `/api/v1/campaigns/{campaign_id}/tweets` | Campaign results |
| `POST`, `GET` | `/api/v1/claims` | Create and query pre-publication claims |
| `GET` | `/api/v1/claims/{claim_id}` | Claim status |
| `POST`, `GET` | `/api/v1/submissions` | Create and query published submissions |
| `GET` | `/api/v1/submissions/{submission_id}` | Submission, scoring, and reward status |

Collection responses use `{ "items": [...], "next_cursor": null,
"has_more": false }`. Clients must tolerate additive response fields and
unknown error codes. Mutations require an `Idempotency-Key` of 8–256 characters;
reusing a key with a different payload returns `409 idempotency_conflict`.
`external_id` lets a platform reconcile its own durable record without making
that identifier protocol-authoritative.

All `/api/v1` responses carry `Cache-Control: no-store`. Errors use:

```json
{
  "error": {
    "code": "creator_not_eligible",
    "message": "Human-readable detail.",
    "retryable": false
  }
}
```

The node signs central reads with its miner hotkey. The central API requires
that hotkey to be currently registered on netuid 93. A deregistered hotkey is
reported as `403 miner_not_registered`; chain/RPC unavailability is
`503 central_api_unavailable`; central throttling is
`429 central_api_rate_limited` and preserves `Retry-After`.

## Evolution rules

* Additive optional response fields and new error codes are compatible in v1.
* Removing, renaming, narrowing, or changing the meaning of an existing field,
  capability, status, authentication rule, or idempotency behavior is not.
* An incompatible contract is published under a new path version. Old and new
  versions overlap for at least the longest supported campaign plus its result
  retention window.
* The OpenAPI route contract and consumer tests in the reference template and
  Stitch3 must pass before a compatible change merges.
* Consensus protocol versioning remains governed separately by
  [protocol-compatibility.md](protocol-compatibility.md).

This separation avoids implementation drift without duplicating scoring: the
node reuses its protocol engine and central result client, while the application
projection owns authentication, stable naming, collection envelopes,
idempotency, and errors.

## Known consumers

This contract is shared infrastructure, not a private route set for one UI. Its
known consumers include Stitch3, Test Drive, and the `x-miner-template`
reference product. Changes must preserve those consumers even when they live in
separate repositories and deploy on different schedules.

A producer-only test suite is therefore insufficient for an application API
change. Before merge, verify the OpenAPI contract here and the affected
cross-repository consumer suites. Keep the old and new behavior available for
the documented overlap window when consumers cannot upgrade atomically; never
repurpose an existing v1 field, status, error, authentication rule, or mutation
identity while a supported consumer may still depend on it.
