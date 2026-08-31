# Miner-validator compatibility policy

This policy governs changes to the behavior defined by the local
[Bitcast X v3 protocol](protocol.md). Both documents are versioned with each release of this
repository.

Validator-to-miner transport version `2` is the first Bitcast X v3 wire version. Transport version
`3` adds the finalized block and extrinsic index beside each otherwise unchanged complete batch. The campaign feed,
commitment envelope, canonical batch hashes, signed batch request/response and attribution reason
strings are consensus-visible contracts.

- Validators reject unsupported protocol versions, malformed extra fields, broken sequence links
  and changed historical batches. They do not guess through incompatibility.
- The first signed `DX3` batch is an atomic recovery boundary. Updated validators preserve their
  accepted prefix and accept a new, never-before-used history ID only at sequence 1 with no
  previous hash. Each history remains internally append-only and a closed history cannot be
  reactivated. Older validators quarantine `DX3`; they must be upgraded during the rollout.
- Resumes are future-only. Claims and submissions must belong to the same side of the latest
  verified history boundary. Existing verified batches and positive campaign economics remain immutable.
- Campaign manifest v4 adds a required positive `max_members` cutoff. The strict v3 manifest stays
  available unchanged during rollout; updated clients prefer v4 and fall back to v3 only when the
  v4 endpoint has not yet been published. A v3 response containing the new field is invalid.
- Adding the first published cutoff does not rewrite a campaign with a positive reward allocation.
  Once positive economics exist, changing the cutoff is a campaign-contract mutation and is
  rejected. A zero-value campaign remains provisional and adopts the latest published cutoff.
- Miners must retain every committed complete batch through campaign end, reconciliation, the
  seven-day emission period and the audit-retention window.
- Additive internal database or operator API changes do not change the protocol version. Miner
  schema version 3 and validator schema version 6 key batches by history ID and are forward-only;
  create a verified backup before upgrading and restore that backup to roll back.
- New attribution reason strings may be added without changing the wire version when they refine
  an existing rejected outcome without changing acceptance. Consumers must preserve unknown reason
  strings and provide a generic rejection fallback rather than treating the enum as closed.
- An additive LLM prompt version does not change the miner-validator wire version when existing
  prompts remain byte-stable, the campaign selects the new version explicitly, and a golden digest
  pins its exact text. Removing or rewriting a prompt version remains a compatibility change.
- Final `preclaim_v2` publications keep attribution and economic disposition separate: each
  attribution decision includes `reward_status`, `reward_reason` and `daily_usd_floor`. Preview
  publications leave the economic disposition pending. Final publications also retain a pending
  disposition for an individual tweet whose required evidence was unavailable; independently
  verified tweets still freeze. Legacy `alpha_target` and `weight` fields are null because
  preclaim economics do not share their established meanings.
- Attribution, score, reward, and successful publication rows with no positive USD allocation are
  retryable state, not a compatibility boundary. Validators replace that state on each successful
  cycle, adopt the latest complete record for the same campaign ID, and use payload-addressed
  preview run IDs for replaceable status updates. The first positive per-tweet daily USD allocation
  makes the complete campaign result immutable across restarts and feed snapshots.
- A pinned featured tweet is a narrower pre-allocation compatibility boundary: its identity and
  campaign contract are immutable across restarts, but preview evidence, scores, bonus recipients,
  and zero-value publications remain replaceable until positive economics freeze. Final rewards
  must replay the pinned identity; unavailable selected-tweet evidence defers settlement.
- Any change to canonical encoding, hash domains, batch/event fields, matcher normalization or
  thresholds requires a new protocol version and golden vectors. The coordinated `DX3` rollout is
  the explicit exception to an extended overlap because every current miner and validator is
  expected to upgrade together.
- Submission event version 3 adds `creator_x_id`. Validators replay historical version-2 events
  without changing their canonical hashes. For exclusive campaigns, version-2 direct submissions
  are accepted only when committed before block `8,920,000`; version-3 creator binding is mandatory
  at and after that activation block. Miners must emit only version 3 after upgrading.
- During an overlap, validators may read explicitly supported old and new versions but must produce
  one deterministic decision for each campaign according to the latest published `mining_protocol`
  until that campaign has a positive reward allocation.
- Miners retain `/v2/batches` as a position-free compatibility endpoint during the v3 overlap. New
  validators use `/v3/batches`; miner-reported positions are untrusted hints and must match the
  exact finalized extrinsic and on-chain envelope before a cursor advances.
- During the legacy campaign overlap, v3 calculates `legacy_connection` locally from the frozen v2
  connection and reward-snapshot state. Validators preserve its non-burn allocation and distribute
  only UID 0 excess across productive `preclaim_v2` miners, or routes it to temporary legacy
  treasury UID 155 when none are productive. State, evidence, pricing, or metagraph
  validation failure must preserve prior authoritative output rather than substitute a partial
  vector.
- Removing a version requires published notice longer than the maximum campaign plus retention
  window and evidence that no live campaign references it.

Bittensor SDK compatibility is separate from the Bitcast wire version. This release pins SDK
`11.0.2`; upgrades require real local Subtensor conformance for endpoint advertisement, finalized
commitments, historical reads, btauth and `SetWeights` intent construction before merge.
