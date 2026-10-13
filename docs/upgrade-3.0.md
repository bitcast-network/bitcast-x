# Upgrading to software 3.x

Software 3.0.0 retires `legacy_connection` campaign execution and the temporary Python interfaces
used by that engine. `main` reported 3.0.0 from 2026-09-08 without a release tag. Software 3.1.0
also replaces chat-model brief evaluation with JEV through OpenRouter, restores floor-first
weights, adds optional surplus beneficiaries, and includes the validator reliability and soft
featured-pin changes in the changelog. Measured from 2.2.0, the last tagged release, it is a major
release under the [release policy](release-policy.md). The version number in this checkout prepares
the release; it does not mean a release tag or image has
been published. Use a reviewed, immutable release revision for deployment.

The canonical network's final legacy campaign emission window ended on 2026-09-01. Operators of
custom feeds or other networks must check their own campaign and retention windows before
upgrading. A date from the canonical deployment does not establish that every downstream legacy
integration has stopped using these interfaces.

## Upgrading a node that runs 3.0.0 from main

- **Validators:** set `BITCAST_X_OPENROUTER_API_KEY` and `BITCAST_X_LLM_PROVIDER=openrouter`
  before upgrading. With production outputs enabled, 3.1.0 refuses to start without the key.
  Without it a container validator crash-loops, `start-pm2-validator.sh` refuses to start, and a
  plain `pm2 restart` ends `errored`. An auto-updating validator keeps running 3.0.0 and logs
  `automatic update check failed` with the missing setting every interval until the key is set.
  3.1.0 ignores `BITCAST_X_LLM_PROVIDER` and `BITCAST_X_CHUTES_API_KEY`, but 3.0.0 code defaults
  to Chutes and needs one or the other, so keep them while 3.0.0 code may still run: a rollback,
  or an auto-update checkout that has not been updated.
- **Validators:** allow outbound HTTPS to `api.coingecko.com` for the TAO/USD price.
- **Validators:** upgrade before block `9,420,000` (about 2026-11-02). After it, 3.0.0 quarantines
  every miner that names a surplus beneficiary, and stops on every cycle once 3.1.0 has stored a
  designation. Until then you can roll back to 3.0.0 (set `BITCAST_X_AUTO_UPDATE=false` first, or
  the supervisor updates again); after it, roll forward instead.
- **Auto-updating validators:** update the invoking checkout as soon as 3.1.0 is on `main`
  (`git pull --ff-only && uv sync --frozen --all-extras`). The supervisor runs from that checkout,
  which updates never change, and the 3.0.0 supervisor starts the checkout's code after every
  restart until its next update check. That code exits at startup without the provider settings
  above, and from block `9,420,000` once a designation is stored; the supervisor then exits too
  and the validator stays down until the checkout is updated. 3.1.0 logs
  `auto-update checkout ... runs a supervisor older than 3.1.0` at startup until you do. Back up
  any miner that runs from the same checkout first: its next restart moves it to 3.1.0.
- **Miners:** miners never update themselves. Run `bitcast-x backup-state` before changing the
  image or checkout. The first 3.1.0 start upgrades the miner database to schema 4 and logs
  `upgraded miner state schema 3 -> 4`; 3.0.0 cannot open it afterwards. To roll back, restore
  that backup into a new state directory. If 3.1.0 already committed a batch, then run
  `resume-history` with the 3.0.0 image or checkout before starting the miner, so validators accept
  it again; the 3.1.0 binary would upgrade the restored database again.

## Removed interfaces

| Consumer | Removal | Required adjustment |
| --- | --- | --- |
| Validators | Legacy collection, attribution, rewards, referrals, pricing and treasury routing | Supply a preclaim-only live campaign feed. Complete any remaining legacy emissions on a reviewed older release first. |
| Operator scripts | `bitcast-x legacy-state-info` | Remove it from startup checks. Keep archived imports for audit; use the older release's read-only inspector if needed. `state-info` continues to inspect current miner and validator databases. |
| Python callers | `bitcast_x.legacy` and `bitcast_x.validator.legacy`, including their exports | Remove legacy execution and weight-combination calls or retain the older package in a separate archival environment. There is no replacement legacy engine. |
| Python provider integrations | `TweetSearchFetch`; `search_tweets` and `fetch_replies` on `XProvider`, `DesearchProvider` and `PreviewXProvider` | Remove dependencies on these search interfaces. Preclaim uses `fetch_tweet_by_id` and `fetch_engagements`. |
| Python scorer integrations | `AttributionScorer(engagement_merger=...)` and `score(tweet_evidence=...)` | Remove legacy evidence-merging and fallback hooks, and the unused `score(cached_evidence=...)` argument; previews cache evidence through `PreviewXProvider`. |
| Python chain integrations | `BittensorChain.legacy_daily_miner_alpha` | Remove legacy alpha-pricing calls; preclaim economics do not use this calculation. |
| Configuration integrations | `Settings.legacy_*` fields and `config.LEGACY_CONNECTION_TWEET_IDS` | Remove attribute and constant references. Old environment keys are ignored by settings parsing, but no longer control behavior. |
| Python protocol and feed integrations | `MiningProtocol.LEGACY_CONNECTION`, `validator.service.ensure_supported_campaigns`, `CampaignFeedClient.cached()`, `EcosystemMap.max_referral_amount`, and v3 manifest / v2 full-feed parsing | Use `preclaim_v2` and the v4 manifest. An invalid or retired feed raises a validation error, which the validator reports as a rejected cycle. |
| Validators | Chat-model brief evaluation: `BITCAST_X_LLM_PROVIDER`, `BITCAST_X_CHUTES_API_KEY`, `BITCAST_X_LLM_NUM_CHECKS` and `Settings.llm_api_key` | JEV runs through OpenRouter using the existing `BITCAST_X_OPENROUTER_API_KEY`. Keep that key; validators previously using only Chutes must set it. A validator with production outputs enabled refuses to start without it. No separate TypeSafe key is needed. Retired settings are ignored. |
| Validators and Python reward integrations | `BITCAST_X_WEIGHT_SCORE_BLEND` (`Settings.weight_score_blend`), `rewards.aggregate_productive_weights` and `RewardCoordinator(score_blend=...)` | Remove the setting. Weights are floor-first; use `allocate_floor_first_weights` and pass a `PricingSnapshot` to `RewardCoordinator.shadow_weights`. |
| Python brief-evaluation integrations | `LlmBriefFilter`, `parse_brief_evaluation` and the `bitcast_x.prompts` markdown prompt generators | Use `JevBriefFilter`; `bitcast_x.prompts.build_request` builds the pinned JEV request. |

The retired environment keys are `BITCAST_X_LEGACY_CONNECTIONS_PATH`,
`BITCAST_X_LEGACY_SNAPSHOTS_PATH`, `BITCAST_X_LEGACY_TWEET_STORE_PATH`,
`BITCAST_X_LEGACY_NOCODE_UID`, `BITCAST_X_LEGACY_CONNECTION_TWEET_IDS`, and
`BITCAST_X_LEGACY_FASTTRACK_URL`. The separate `config/legacy.env.example` file is removed.

## Upgrade behavior and retained compatibility

A fetched legacy campaign fails feed validation and aborts the complete validator cycle before
campaign reconciliation, scoring, publication and weight submission. A frozen legacy contract
restored from the database is quarantined, so it cannot block unrelated preclaim campaigns.
The node can still ingest finalized miner batches before rejecting that feed. It records a
consensus error and retains durable history. Restore the correct preclaim-only feed; do not relabel
an old frozen campaign as preclaim or delete its state to bypass the check. Existing unrelated
preclaim campaigns receive no new economic outputs from a rejected cycle.

The retirement leaves the public miner `/api/v1` application contract, canonical hashes, signed
batch transport and preclaim scoring/reward rules intact. `DX2`, `DX3`, `/v2/batches` and `/v3/batches`
remain supported for preclaim history. `MiningProtocol.LEGACY_CONNECTION` is removed; stored
legacy rows are left in place for audit and are never executed.

Validator schema 6 is unchanged; 3.1.0 adds one rollback-safe table, `weight_allocations`, without
changing it. Miner schema 4 rebuilds the miner event table so it can hold surplus-beneficiary
designations. It is forward-only: an older miner refuses the upgraded database, so roll a miner
back only by restoring its pre-upgrade backup. Miners never update automatically; upgrade them by
hand after taking that backup. Preserve the complete state
directory and wallets, including committed batches, pending
events, campaign and ecosystem-map bindings, preview state and frozen results. Follow the
[backup and rollback procedure](operator-runbook.md#upgrade-and-rollback). `backup-state` copies
the current miner and validator SQLite databases; archive imported `connections.db`,
`reward_snapshots` and `legacy_tweet_store` separately. This change neither deletes those imports
nor settles outstanding historical obligations.

Existing preclaim miners do not need a new history, wallet, batch format or application endpoint
because of retirement. Changes already on main since software 2.2.0 also retain the direct
submission grace-period fix and reduce repeated campaign/qualification reads; see the changelog.

JEV changes semantic evaluation for campaigns without frozen positive rewards. Existing frozen
positive results remain immutable; chat-model cache entries remain stored but are not reused as
JEV verdicts. Keep `BITCAST_X_OPENROUTER_API_KEY` configured and coordinate validator rollout so
active campaigns are evaluated with the same release. An unavailable evaluator defers settlement
for that campaign during the 900-block evidence grace period while other campaigns keep emitting;
after grace, available tweets can settle without classifying missing evidence as rejected content.

Preview caching now uses `preview.sqlite3`, with a restricted, read-only import of legacy diskcache
entries. Keep the old `preview-cache` directory until rollback is no longer needed. `backup-state`
copies neither preview cache; previews refill them from X. Featured pins remain fixed without freezing an
unpaid campaign's contract. Follow the runbook's rollback restriction for older releases when a
campaign settles at zero after its pinned tweet becomes ineligible.

## Floor-first weights and surplus beneficiaries

Mechanism-1 weights again cover every miner's USD floors first: each miner's daily floors are
converted to weight at the block's pricing snapshot (alpha's moving-average price and the miner
emission from chain state, and TAO/USD from CoinGecko). The surplus left after floors is split by
tweet-score share. This replaces the pure score-share allocation and its `weight_score_blend`
setting. `BITCAST_X_WEIGHT_SCORE_BLEND` is removed and ignored if still set. Validators need
outbound HTTPS to `api.coingecko.com`. A failed request reuses the last quote for up to a day, also
after a restart; without a price, a cycle's weights fall back to floor proportions.

Miners may name a hotkey to receive their surplus weight with
`BITCAST_X_MINER_SURPLUS_BENEFICIARY` (or the chart's `surplusBeneficiary`). The miner commits the
designation once the chain reaches block `9,420,000`. Every validator must run 3.1.0 or later before
that block, because earlier validators quarantine a batch carrying the designation. Validators on
3.1.0 also quarantine one committed before that block, so until then both releases agree. Unsetting the
value later commits a clearing designation. The miner learns its earlier designation from its own
database, so restore a lost database before unsetting the value; otherwise the earlier designation
stays in force.

## Automatic updates and rollout notice

Source auto-updates are opt-in. An operator who set `BITCAST_X_AUTO_UPDATE=true` and left
`BITCAST_X_AUTO_UPDATE_REF=origin/main` can receive this removal when it merges. The updater checks
validator schema compatibility, the candidate's startup settings and candidate readiness; it does not
reject a new software major version or wait for a release tag. A version bump alone therefore does
not provide an overlap period. A miner database in the same state directory is not checked, because
only the validator updates itself.

Operators who still require the removed behavior should disable automatic updates before the
retirement merge and keep their reviewed revision until ready to upgrade. Published retirement
notice must reach these operators ahead of that merge. This upgrade guide describes the change;
it is not evidence that earlier notice was delivered. Maintainers should record the actual notice
and its timing against the [compatibility policy](protocol-compatibility.md) when assessing rollout.

Canary the reviewed revision against preserved state, verify readiness and retained frozen results,
and compare preclaim outputs with an independent validator before expanding the rollout. Keep the
previous image digest or source revision and the verified backup available for rollback.
