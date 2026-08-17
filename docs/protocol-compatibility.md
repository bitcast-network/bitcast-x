# Miner-validator compatibility policy

This policy governs changes to the behavior defined by the local
[Bitcast X v3 protocol](protocol.md). Both documents are versioned with each release of this
repository.

Protocol version `2` is the first Bitcast X v3 wire version. Protocol version `3` adds the finalized
block and extrinsic index beside each otherwise unchanged complete batch. The campaign feed,
commitment envelope, canonical batch hashes, signed batch request/response and attribution reason
strings are consensus-visible contracts.

- Validators reject unsupported protocol versions, malformed extra fields, broken sequence links
  and changed historical batches. They do not guess through incompatibility.
- Campaign manifest v4 adds a required positive `max_members` cutoff. The strict v3 manifest stays
  available unchanged during rollout; updated clients prefer v4 and fall back to v3 only when the
  v4 endpoint has not yet been published. A v3 response containing the new field is invalid.
- Adding the first published cutoff does not rewrite already frozen campaign results. Once a
  non-null cutoff has been observed, changing it is a campaign-contract mutation and is rejected
  after final results freeze.
- Miners must retain every committed complete batch through campaign end, reconciliation, the
  seven-day emission period and the audit-retention window.
- Additive internal database or operator API changes do not change the protocol version.
- An additive LLM prompt version does not change the miner-validator wire version when existing
  prompts remain byte-stable, the campaign selects the new version explicitly, and a golden digest
  pins its exact text. Removing or rewriting a prompt version remains a compatibility change.
- Final `preclaim_v2` publications keep attribution and economic disposition separate: each
  attribution decision includes `reward_status`, `reward_reason` and `daily_usd_floor`. Preview
  publications leave the economic disposition pending. Final publications also retain a pending
  disposition for an individual tweet whose required evidence was unavailable; independently
  verified tweets still freeze. Legacy `alpha_target` and `weight` fields are null because
  preclaim economics do not share their established meanings.
- Any change to canonical encoding, hash domains, batch/event fields, matcher normalization or
  thresholds requires a new protocol version, golden vectors and a shadow overlap period.
- During an overlap, validators may read explicitly supported old and new versions but must produce
  one deterministic decision for each campaign according to the latest published `mining_protocol`
  until that campaign's final result is frozen.
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
