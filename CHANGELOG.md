# Changelog

All notable changes to Bitcast X are recorded here. This project follows
[Semantic Versioning](https://semver.org/); software release versions are separate from the wire,
campaign-manifest, and event-schema versions documented in `docs/protocol.md`.

## [3.0.0] - Unreleased

### Added

- Helm chart for running one miner on Kubernetes (`charts/bitcast-x-miner`): `run-miner` or
  `run-miner-api`, state on a PersistentVolumeClaim, the hotkey mounted read-only from an existing
  Secret, and the advertised `publicIP:port` kept identical end to end. Optional: `/api/v1` on
  its own port (`minerApi.port`, needs `BITCAST_X_MINER_API_PORT` support in the image) behind a
  ClusterIP Service and a TLS Ingress, and `extraVolumes`/`extraVolumeMounts`. CI lints and
  renders it and asserts each refusal.
- Configurable `weight_score_blend` validator setting, allocating the mechanism-1 weight vector
  between campaign floors and miners' deduplicated tweet-score shares. **The default is `1.0`:
  allocation is purely on content value**, independent of carried campaign floors. Operators may
  set a lower value to blend floors back in; creator reward floors, payouts, and the burn fallback
  are untouched.

### Breaking changes

- Retire `legacy_connection` campaign execution, connection collection, legacy reward and referral
  emissions, legacy pricing, and temporary treasury routing. A feed containing a retired campaign
  aborts the entire validator cycle before campaign scoring, result publication, or weight submission.
- Remove `legacy-state-info`, the `bitcast_x.legacy` and `bitcast_x.validator.legacy` Python modules,
  `BITCAST_X_LEGACY_*` settings, provider search/reply methods, and legacy scorer extension arguments.
  These incompatible operator and package changes require a software major release. See the
  [upgrade guide](docs/upgrade-3.0.md) for the affected interfaces and migration steps.
- Remove unused validator and protocol APIs: the block-scan store methods (`persist_block`,
  `scanned_block`, `commitment_for_sequence`, `next_commitment_sequence`) and the `start_block`
  store argument, `ValidatorStore.cursor`/`record_error` (which wrote a column nothing read),
  `ClaimLedger`/`ClaimRecord`, the test-only reward wrappers (`assign_tweets`, `apply_v2_bonuses`,
  `calculate_rewards`), `HistoricalQualificationChecker` (use `QualificationReader.eligible`),
  `auto_update_enabled`, `AttributionScorer.score(cached_evidence=...)`, the publisher's unused
  `miner_uid` argument, ignored `snapshot_id` store arguments, the `title`/`ecosystem_id` campaign
  field aliases and `CampaignFeed.protocol_version`. `ShadowResultPublisher` now requires its
  `preview_store`. Startup no longer re-runs a contract backfill that every schema-6 database has
  already applied.
- Remove the remaining legacy compatibility code: `MiningProtocol.LEGACY_CONNECTION` and its
  retirement guard, the v3 manifest fallback, v2 full-feed parsing, `CampaignFeedClient.cached()`,
  and the unused `max_referral_amount` map field. Clients read only the v4 manifest; the retired v3
  URL is redirected to it. Map cache entries written by earlier releases are re-downloaded once, an
  unreadable or retired feed fails the validator cycle instead of stopping the process, and a stored
  legacy contract is quarantined rather than failing every cycle.

### Compatibility

- The retirement does not change the miner application `/api/v1` contract, canonical batch hashes,
  `DX2`/`DX3` commitments, `/v2/batches` or `/v3/batches`, or preclaim reward calculations. Historical
  `legacy_connection` records remain readable. Miner schema 3 and validator schema 6 are unchanged.
- Existing legacy archives are left in place. Historical settlement remains separate from validator
  campaign emissions. Automatic source updates following `origin/main` can activate this removal
  at merge time; they do not wait for a release tag or stop at a software major-version boundary.

### Changed

- Cache the miner qualification snapshot for 60 seconds and reuse a fetched campaign during direct
  submission, reducing repeated upstream reads.
- Final and preview reconciliation load the verified batch history once per pass, grouped by
  campaign, instead of reloading and re-hashing the whole history for every campaign. The
  reconciler's qualification memo is bounded so long-running previews no longer grow it per cycle.
- Engagement scoring looks up relationship edges in a sparse map built once per campaign pool,
  instead of allocating a dense N×N matrix for every tweet (about 376 MB per tweet on the live
  indie_hacker map). Scores are bit-identical; scoring 400 tweets on that map drops from 7.8 s to
  0.4 s and peak memory from 523 MB to 143 MB.
- Remote Loki log forwarding is opt-in. The previous default enabled forwarding with a placeholder
  token that could not authenticate; set all three `BITCAST_X_LOKI_*` values to enable it.

### Fixed

- An unreadable preview cache entry (for example after an evidence model change, or a malformed
  timestamp) is now a cache miss that is fetched again. It previously aborted the whole validator
  cycle before weights, or stopped the process.

- A pinned featured tweet no longer freezes its campaign contract or holds back settlement. Campaign
  edits are adopted until economics settle; if an edit leaves the pinned tweet ineligible, that
  campaign settles without a featured bonus instead of deferring every campaign's economics and
  weight submission for the rest of the emission window. Replaces the pin-release logic and its
  `store_audit_events` table; the pinned tweet is never replaced by a different one.
- Exclusive direct campaigns accept already-published tweets during the evaluation-day grace
  period only when the creator was historically eligible and the submission is committed no later
  than the campaign's scoring-close block.

## [2.2.0] - 2026-08-31

### Added

- A miner hotkey can recover from lost local commitment state with the explicit `resume-history`
  operator command. The command generates the history ID itself; no validator cursor or sequence
  number is operator input.
- The first signed `DX3` batch is the atomic future-only boundary. Validators preserve accepted
  history, reject reused history IDs, and isolate claims across histories.

### Compatibility

- This is a coordinated miner-validator protocol rollout. Validators without `DX3` support will
  quarantine a resumed miner until upgraded; ordinary `DX2` histories remain
  unchanged.

## [2.1.0] - 2026-08-22

### Added

- The authenticated miner application API now exposes the complete versioned `/api/v1` contract
  for third-party products, including enabled ecosystems, leaderboard reads, idempotent creator
  claims and submissions, and stable upstream error envelopes.

### Compatibility

- `/api/v1` is a public integration contract. Existing fields and semantics remain supported for
  the lifetime of v1; incompatible changes require a new path version and an overlap window.

### Changed

- The generic brief-instruction compliance prompt is available as version 6. Version 1 retains its
  original byte-stable sponsor evaluation, and retired prompt versions 3 and 4 remain unavailable.

### Fixed

- Finney miners and validators now use the qualification schedule bundled with the reviewed
  release, preventing stale environment files from silently retaining obsolete eligibility rules.
- Miner-hotkey stake qualification now counts all alpha staked to the miner hotkey on the subnet,
  rather than only stake supplied by the hotkey's controlling coldkey.

## [2.0.0] - 2026-08-13

### Added

- Bittensor v11 reference miner, authenticated miner API, and validator implementation.
- Self-contained protocol, compatibility, and operator documentation.
- Container and PM2 source-install paths with role-specific configuration examples.
- Durable state inspection, backup, recovery, shadow reporting, and upgrade safeguards.

### Changed

- Source-install automatic updates are explicit opt-in.
- Package and container release metadata now identify the same immutable software release.
- Production validators publish signed campaign results and submit mechanism-1 weights by default;
  either output can still be disabled explicitly for diagnostics.
- Enabled production outputs now fail at startup when required reconciliation providers are not
  configured, instead of running as an ingestion-only validator.

### Compatibility

- Current protocol boundary versions remain those listed in `docs/protocol.md`; the `2.0.0`
  software release does not renumber those independent contracts.
- Earlier development builds reported package version `0.1.0` and did not carry a public release
  compatibility commitment.
