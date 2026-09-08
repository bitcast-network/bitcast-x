# Upgrading to software 3.0

Software 3.0.0 retires `legacy_connection` campaign execution and the temporary Python interfaces
used by that engine. It is a major release under the [release policy](release-policy.md). The
version number in this checkout prepares the release; it does not mean a release tag or image has
been published. Use a reviewed, immutable release revision for deployment.

The canonical network's final legacy campaign emission window ended on 2026-09-01. Operators of
custom feeds or other networks must check their own campaign and retention windows before
upgrading. A date from the canonical deployment does not establish that every downstream legacy
integration has stopped using these interfaces.

## Removed interfaces

| Consumer | Removal | Required adjustment |
| --- | --- | --- |
| Validators | Legacy collection, attribution, rewards, referrals, pricing and treasury routing | Supply a preclaim-only live campaign feed. Complete any remaining legacy emissions on a reviewed older release first. |
| Operator scripts | `bitcast-x legacy-state-info` | Remove it from startup checks. Keep archived imports for audit; use the older release's read-only inspector if needed. `state-info` continues to inspect current miner and validator databases. |
| Python callers | `bitcast_x.legacy` and `bitcast_x.validator.legacy`, including their exports | Remove legacy execution and weight-combination calls or retain the older package in a separate archival environment. There is no replacement legacy engine. |
| Python provider integrations | `TweetSearchFetch`; `search_tweets` and `fetch_replies` on `XProvider`, `DesearchProvider` and `PreviewXProvider` | Remove dependencies on these search interfaces. Preclaim uses `fetch_tweet_by_id` and `fetch_engagements`. |
| Python scorer integrations | `AttributionScorer(engagement_merger=...)` and `score(tweet_evidence=...)` | Remove legacy evidence-merging and fallback hooks. The preclaim preview `cached_evidence` argument remains supported. |
| Python chain integrations | `BittensorChain.legacy_daily_miner_alpha` | Remove legacy alpha-pricing calls; preclaim economics do not use this calculation. |
| Configuration integrations | `Settings.legacy_*` fields and `config.LEGACY_CONNECTION_TWEET_IDS` | Remove attribute and constant references. Old environment keys are ignored by settings parsing, but no longer control behavior. |

The retired environment keys are `BITCAST_X_LEGACY_CONNECTIONS_PATH`,
`BITCAST_X_LEGACY_SNAPSHOTS_PATH`, `BITCAST_X_LEGACY_TWEET_STORE_PATH`,
`BITCAST_X_LEGACY_NOCODE_UID`, `BITCAST_X_LEGACY_CONNECTION_TWEET_IDS`, and
`BITCAST_X_LEGACY_FASTTRACK_URL`. The separate `config/legacy.env.example` file is removed.

## Upgrade behavior and retained compatibility

A fetched legacy campaign, or a legacy contract restored by frozen campaign binding, aborts the
complete validator cycle before campaign reconciliation, scoring, publication and weight submission.
The node can still ingest finalized miner batches before rejecting that feed. It records a
consensus error and retains durable history. Restore the correct preclaim-only feed; do not relabel
an old frozen campaign as preclaim or delete its state to bypass the check. Existing unrelated
preclaim campaigns receive no new economic outputs from a rejected cycle.

The retirement leaves the public miner `/api/v1` application contract, canonical hashes, signed
batch transport and preclaim scoring/reward rules intact. `DX2`, `DX3`, `/v2/batches` and `/v3/batches`
remain supported for preclaim history. `MiningProtocol.LEGACY_CONNECTION` remains decodable for
historical records; it no longer enables execution.

There is no database schema migration in this change: miner schema 3 and validator schema 6 remain
in use. Preserve the complete state directory and wallets, including committed batches, pending
events, campaign and ecosystem-map bindings, preview state and frozen results. Follow the
[backup and rollback procedure](operator-runbook.md#upgrade-and-rollback). `backup-state` copies
the current miner and validator SQLite databases; archive imported `connections.db`,
`reward_snapshots` and `legacy_tweet_store` separately. This change neither deletes those imports
nor settles outstanding historical obligations.

Existing preclaim miners do not need a new history, wallet, batch format or application endpoint
because of retirement. Changes already on main since software 2.2.0 also retain the direct
submission grace-period fix and reduce repeated campaign/qualification reads; see the changelog.

## Automatic updates and rollout notice

Source auto-updates are opt-in. An operator who set `BITCAST_X_AUTO_UPDATE=true` and left
`BITCAST_X_AUTO_UPDATE_REF=origin/main` can receive this removal when it merges. The updater checks
database schema compatibility and candidate readiness; it does not reject a new software major
version or wait for a release tag. A version bump alone therefore does not provide an overlap period.

Operators who still require the removed behavior should disable automatic updates before the
retirement merge and keep their reviewed revision until ready to upgrade. Published retirement
notice must reach these operators ahead of that merge. This upgrade guide describes the change;
it is not evidence that earlier notice was delivered. Maintainers should record the actual notice
and its timing against the [compatibility policy](protocol-compatibility.md) when assessing rollout.

Canary the reviewed revision against preserved state, verify readiness and retained frozen results,
and compare preclaim outputs with an independent validator before expanding the rollout. Keep the
previous image digest or source revision and the verified backup available for rollback.
