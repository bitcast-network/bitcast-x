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
  URL is redirected to it. Feed and map caches written by earlier releases stay usable, an
  unreadable or retired feed fails the validator cycle instead of stopping the process, and a stored
  legacy contract is quarantined rather than failing every cycle.
- Reshape miner store and engine internals: `MinerStore.preview_batch` becomes `batch_draft`,
  `receipts(event_id=...)` becomes `receipts(event_ids=(...))`, `MinerStore.enqueue` requires its
  queue bounds (`MinerEngine.enqueue` supplies them from the batch policy), `resume_history` moves
  from `MinerEngine` to `MinerStore`, and `MinerStore.close`, `current_history_id`,
  `history_has_batches`, `start_history` and `submission_id` are removed. `MinerSdk`,
  `MinerControlService` (including its `CampaignSource` fallback), `config.QUALIFICATION_OWNER_HOTKEY`
  and the miner HTTP API are unchanged.
- Replace chat-model brief evaluation with JEV through OpenRouter. One JEV request per tweet scores the
  campaign's prompt-version rules (v1, v2, v5, v6) as separate yes/no gates beside a final verdict
  and a product-identity check; required tags and links are checked in code, and the tweet passes
  only when every check passes. Each version's request is pinned by a golden digest. Verdicts share
  the durable evaluation cache, and provider failure defers settlement during the evidence grace
  period rather than rejecting content. Validators retain their existing
  `BITCAST_X_OPENROUTER_API_KEY`, which is required when production outputs are enabled; no separate
  TypeSafe key is needed.
  `BITCAST_X_LLM_PROVIDER`, `BITCAST_X_CHUTES_API_KEY` and
  `BITCAST_X_LLM_NUM_CHECKS` are removed, along with `LlmBriefFilter`, `parse_brief_evaluation`
  and the `bitcast_x.prompts` markdown prompt generators.

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
  reconciler keeps qualification answers for fixed past blocks, and for the moving preview block
  only the latest block's, so long-running previews no longer grow it per cycle.
- Engagement scoring looks up relationship edges in a sparse map built once per campaign pool,
  instead of allocating a dense N×N matrix for every tweet (about 376 MB per tweet on the live
  indie_hacker map). Scores are bit-identical; scoring 400 tweets on that map drops from 7.8 s to
  0.4 s and peak memory from 523 MB to 143 MB.
- Reward tuning values (performance bonus per metric, featured multiplier, featured pool size) are
  module constants instead of repeated keyword defaults, and `score_blend` is a required argument
  so `weight_score_blend` in settings is the only default. When no featured tweet was pinned, the
  published `selection_pool` now uses the same view-rank order as a pinned selection (it was
  previously sorted by tweet ID); the selected tweet is unchanged.
- Oversized campaign-feed, miner and LLM responses all raise `ResponseTooLargeError` from one bounded
  reader. An oversized campaign feed previously raised a bare `ValueError`.
- Validator settings logic lives in `config.py`: one `missing_validator_settings()` rule used by
  the startup check, the economics on/off decision and the PM2 launcher (the copies disagreed on
  empty strings). All three require the existing OpenRouter key for JEV. The env templates no
  longer pin the tweet-length limit or the weight cadence and version key; existing `.env` files
  that set them keep their values, so remove those lines to follow release defaults.
- Miner claims fetch the central campaign once per request.
- Miner submission no longer decodes every stored submission, and result polling skips the central
  API when nothing is pending. Batch selection uses one store read and a binary search instead of
  one write transaction per candidate; batch bytes are unchanged. Receipt listings parse each batch
  once and read referenced claims in one query.
- Miner API error codes are typed instead of derived from message text; codes, statuses and
  messages are unchanged. `run-miner` and `run-miner-api` share one validator-permit check,
  protocol app and commitment loop. The miner store uses the shared SQLite helpers.
- `config/miner.env.example` states the enforced 64-character minimum for
  `BITCAST_X_MINER_API_TOKEN`.
- Remote Loki log forwarding no longer runs with the placeholder token, which could not
  authenticate: it starts once `BITCAST_X_LOKI_TOKEN` is set, using the shared URL and username
  unless they are overridden. Setting `BITCAST_X_LOKI_URL=` still disables it.

### Fixed

- A claim whose draft exceeds 20,000 characters once NFKC-normalized is refused (422 from the
  miner API). Its reveal is stored normalized and re-checked on every read, so such a claim could
  be created but never read back to build a batch, after which no event from any creator committed.
- Campaign feed caches left by earlier releases stay usable. A torn or retired-format
  `campaign-feed.json` is now downloaded again instead of failing every cycle until an operator
  deleted it, and the map-binding bootstrap reads map references from any manifest version.
  Cached ecosystem maps that still carry the retired consumer-only `max_referral_amount` default
  are reused rather than all downloaded again on upgrade.
- Restore the rule that a stored campaign contract missing `max_members` matches one that has it.
  An earlier change in this release removed it as unused, but campaigns bound before the field
  existed and frozen afterwards (or the reverse) then failed every replay check, stopping
  reconciliation, publication and weights for as long as they stayed in the feed.
- Settlement no longer freezes a campaign without a tweet whose evidence a provider briefly could
  not return. Validators whose fetch failed froze different rewards, and for a pinned tweet no
  featured bonus, from validators whose fetch succeeded, for the campaign's whole emission window.
  Settlement now waits up to 900 blocks (three hours) for missing evidence, leaving that campaign
  out of weights meanwhile, then settles without what is still missing. A failed tweet lookup is
  retried after an hour rather than six, so the wait can still recover it.
- Validators submit weights before publishing final results. A campaign whose final payload could
  not be built, such as one whose rewarded miner had deregistered, stopped weight submission for
  every campaign until its emission ended.
- Final result payloads now sign the form ingestion verifies. Payloads rewarding UIDs of different
  digit counts (for example 9 and 10) failed signature verification with 401 and were resent every
  cycle without being accepted.
- Both miner modes apply one endpoint-advertisement rule. The chain rate-limits serve calls (50
  blocks on SN93), so a restart soon after the last advertisement cannot re-advertise: `run-miner`
  used to exit and crash-loop until the limit passed, while `run-miner-api` logged the failure and
  reported ready even when the chain advertised a different endpoint or none, leaving the miner
  unreachable. A rejected advertisement is now accepted only when the chain already advertises
  this exact endpoint; otherwise startup fails and the supervisor retries.
- Retrying a miner submission with the same `Idempotency-Key` but a changed `external_id` now
  returns `409 idempotency_conflict`, as documented and as claims already did. It previously
  returned the existing submission and silently ignored the new input.
- A miner that queued two submissions citing the same claim before its next batch committed could
  no longer commit anything: the batch revealed that claim twice, which every batch validation
  rejects. A batch now reveals each claim once; validators already let at most one of the
  submissions consume the claim. An affected miner recovers on its next commit attempt.
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

### Security

- Removed the `diskcache` dependency, which has an unpatched unsafe-pickle advisory
  (CVE-2025-69872). Validator preview state now lives in a JSON table in `preview.sqlite3` instead
  of the `preview-cache` directory. On first start the validator imports the existing entries off
  its event loop, loading only plain values so no stored code can run; an interrupted import runs
  again on the next start, and an unusable old cache never blocks startup. The old directory is
  left unchanged so a rollback keeps its preview state. Delete it once rollback is no longer needed.
  Entries no preview has written for 14 days, which belong to closed campaigns, are dropped, so the
  store stays bounded as diskcache's size limit kept the old cache.

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
