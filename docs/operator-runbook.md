# Bitcast X v3 operator runbook

Bitcast X v3 ingests miner commitments, reconciles campaigns, calculates and persists the complete
weight vector, publishes the signed DEEBLY payload, and submits mechanism-1 weights by default.
Operators can set `BITCAST_X_ENABLE_DATA_PUBLISH=false` or
`BITCAST_X_ENABLE_WEIGHT_SUBMISSION=false` for an intentional diagnostic run.

## Legacy campaign overlap

Before the first v3 start, stop v2 cleanly and copy its `connections.db`, complete
`reward_snapshots` directory, and complete diskcache `tweet_store` directory into the v3
persistent volume. Configure their locations with `BITCAST_X_LEGACY_CONNECTIONS_PATH`,
`BITCAST_X_LEGACY_SNAPSHOTS_PATH`, and `BITCAST_X_LEGACY_TWEET_STORE_PATH` (defaults are beneath
`STATE_DIR`). Preserve every file in the diskcache directory, including `cache.db`; it contains
the cumulative tweets and engagement identities that bounded provider searches cannot recreate.
The imported connection database must remain at schema version 2. V3 fails closed while legacy
campaigns exist if imported state is missing or malformed; it never starts fresh replacement
state.

Before starting v3, run `bitcast-x legacy-state-info` against the copied volume. It opens the
legacy databases read-only, parses every reward snapshot, counts connection/tweet/engagement
records, and emits a manifest hash. Record the JSON result before and after transferring the state;
the hashes and counts must match. A missing or corrupt import exits non-zero.

The shipped `BITCAST_X_LEGACY_CONNECTION_TWEET_IDS` records the registration tweet used by v2, but
v3 intentionally does not acquire new legacy registrations. It retains the imported connection
state and continues polling `BITCAST_X_LEGACY_FASTTRACK_URL`. Fast-tracked tweets are merged into
the cumulative tweet store before connection-tag processing. V3 deliberately does not generate
social maps; both legacy scoring and the new protocol consume maps from the campaign feed.
Keep `BITCAST_X_CAMPAIGN_FEED_MAX_RESPONSE_BYTES` at least 16 MB for the current approximately
8 MB full-map snapshot; this bound is separate from miner-response and LLM-response limits.

Legacy non-burn weights are preserved exactly. Only the allocation on burn UID 0 is distributed
across productive `preclaim_v2` miners. While any legacy campaign remains, if no v2 miner is
productive that allocation is instead routed to the temporary legacy treasury UID 155, matching
the outgoing validator. UID 155 must be present or the cycle fails closed. Remove this routing and
the treasury constant with the isolated legacy engine after the final legacy liability drains.
Unavailable X evidence, invalid imported state, or stale hotkey routing retains the prior durable
shadow result and marks the current cycle unhealthy rather than silently changing allocation.
Legacy ingestion run IDs include the finalized block, so retries of a block are idempotent while
later blocks create distinct metric-history rows.

Campaign routing is persisted in `validator.sqlite3`; changing an existing campaign between
`legacy_connection` and `preclaim_v2` is rejected. Once the campaign feed contains no legacy
campaigns, v3 produces its normal vector. Remove the isolated legacy engine and imported state only
after legacy emissions and referral liabilities have independently been confirmed as drained.

A canonical legacy reward snapshot is the terminal scoring boundary for that campaign. Later cycles
replay its fixed tweet rewards and may publish them, but do not rediscover tweets, refresh engagement,
or invoke the LLM for that campaign. Legacy campaigns without a snapshot continue cumulative
discovery and scoring until their first-emission rewards are frozen.

### Desearch activity budget

New preclaim submissions receive a replaceable pre-close verification preview. Preview tweet and
engagement evidence is stored separately from consensus state and uses the v2 refresh schedule:
hourly for tweets under one hour old, every four hours until 24 hours old, and daily thereafter.
Unavailable evidence is retried no more than once per minute, and unchanged preview payloads are
not republished. Preview rows include mutable performance-bonus percentages and breakdowns for the
currently selected campaign tweets, but their USD targets remain zero. At the first healthy preview
on or after one day before `closes_at`, the validator durably pins and publishes the deterministic
featured tweet. Failed ingestion retries the identical payload after one minute. A temporary loss
of the selected tweet's evidence preserves the last good preview and retries on later cycles rather
than publishing a destructive replacement. The first post-close scoring pass still fetches fresh
evidence before assigning tweets and freezing rewards, then reuses the pinned feature. If its
required evidence is still unavailable, final economics and weight submission wait until recovery.
Frozen legacy campaigns contribute no further search or scoring calls.

## Runtime contract

- Use the immutable image tag or digest produced from a reviewed `main` commit; never deploy
  `latest` as the rollback reference.
- Point `BITCAST_X_STATE_DIR` at operator-owned persistent storage; SQLite databases, WAL files and
  the campaign feed cache and immutable map-binding record live there. Preserve the complete
  directory across upgrades so an already-accepted ecosystem run cannot be rebound to new content.
  On the first upgraded fetch, verified maps from the pre-binding cache are imported automatically.
- Point `BITCAST_X_WALLET_PATH` at an existing Bittensor wallet directory. Containers may either
  mount that directory or receive `HOTKEY_DATA` through their runtime secret provider.
- Run as UID/GID `10001`. Miner HTTP is port `8095`; validator operations are port `8096`.
- Supply secrets through the runtime secret store. Never place wallet material, Desearch keys or
  signed payloads in the image or environment example file.

The validator RPC must provide finalized historical reads for miner-reported commitment blocks.
Startup does not scan a block range: it asks each miner for batches beyond the durable per-miner
cursor, verifies only their reported blocks, and checks that the resulting sequence and hash equal
the miner's latest on-chain commitment. An unavailable miner retains its prior cursor and is retried
on every later cycle. Miner availability and protocol failures are isolated: verified histories
continue through reconciliation and the validator remains ready. Do not advance a miner cursor
manually.

Finney netuid 93 qualification history ships in the reviewed software release and cannot be
overridden by a stale environment file. Upgrade the release to adopt an appended rule. Each entry
has an immutable `version`, owner hotkey, conviction threshold, optional owner-to-miner self-stake
threshold and `effective_block`; the compatibility-named self-stake value is the aggregate alpha
staked to the miner hotkey on the subnet, from any coldkey. Never replace an earlier entry, because
validators use the rule active at each claim, submission and scoring-close block.
`BITCAST_X_QUALIFICATION_*` overrides remain available only for other networks and local tests.

## Start and verify

Use `config/miner.env.example` or `config/validator.env.example` for a minimal role-specific
installation. The root `.env.example` remains the exhaustive reference. Optional legacy and remote
logging settings are isolated in `config/legacy.env.example` and
`config/remote-logging.env.example`; validator evidence/LLM credentials are isolated in
`config/providers.env.example`.

```bash
docker run --rm bitcast-x:<version> --help
docker run --rm --entrypoint id bitcast-x:<version> -u
docker run --rm --entrypoint python bitcast-x:<version> \
  -c 'import bitcast_x; print(bitcast_x.__version__)'
```

For normal operation, either inject `HOTKEY_DATA` from a secret provider or mount an existing
wallet whose hotkey is readable by container UID 10001:

```bash
docker run --rm \
  --mount type=bind,src=/absolute/path/to/wallets,dst=/var/lib/bitcast-wallets \
  --mount type=bind,src=/absolute/path/to/state,dst=/var/lib/bitcast-x \
  bitcast-x:<version> run-miner
```

Run one role per container:

```bash
bitcast-x run-miner
bitcast-x run-validator
```

For a direct Python installation, `run-validator` does not update itself by default. Set
`BITCAST_X_AUTO_UPDATE=true` to opt in. It fetches `BITCAST_X_AUTO_UPDATE_REF` (`origin/main` by
default) on a jittered interval, accepts only a fast-forward commit, builds the candidate in an
isolated worktree and locked virtual environment,
and switches only after the candidate becomes healthy. It does not modify the invoking checkout,
the active environment, `BITCAST_X_STATE_DIR`, imported legacy files or campaign caches. A dirty
tracked checkout is left running and is not updated. Containers remain immutable and do not
self-update by default.

Automatic updates reject any candidate that changes an existing miner or validator database schema
version. A reviewed, rollback-compatible additive table may retain the existing version; candidate
checks exercise that extension on a disposable database copy before activation. Deploy forward-only
schema releases manually using the backup procedure below. Prepared code and dependency environments
live only beneath `BITCAST_X_AUTO_UPDATE_DIR`; never point that directory at the state directory.
Operate direct installs beneath systemd, PM2 or another process supervisor so a fatal validator
error is restarted even when no update is in progress.

### PM2 source install

PM2 runs the checked virtualenv executable in single-process fork mode. The ecosystem file contains
no secrets. For a validator, install Linux, Node.js 18 or newer, PM2, and `uv`, then run:

```bash
npm install --global pm2@latest
./scripts/setup-pm2-validator.sh
```

On first use, the setup script installs locked production dependencies and creates `.env` from the
validator and provider templates with mode `0600`. It substitutes usable wallet, state, and update
paths beneath the current home directory and never overwrites an existing `.env`. Edit that file to
select the existing wallet name/hotkey and add the Desearch and selected LLM key. Restore the
verified legacy state described above when legacy campaigns remain, then launch:

```bash
./scripts/start-pm2-validator.sh
```

The launch script checks the required settings and wallet file, starts or restarts only
`bitcast-x-validator`, waits up to 60 seconds for local health, and runs `pm2 save` only after health
succeeds. Normal operations are:

```bash
pm2 status bitcast-x-validator
pm2 logs bitcast-x-validator --lines 200
pm2 restart bitcast-x-validator --update-env
pm2 stop bitcast-x-validator
```

`pm2 startup` prints the one privileged command appropriate for the host; review it before running
it. The checked policy restarts crashes after five seconds, stops after ten unstable restarts,
restarts above 2 GB, sends logs to `./logs`, and gives a validator up to 61 minutes to finish its
transactionally safe shutdown.

Before restart or upgrade, record `bitcast-x state-info` and `bitcast-x shadow-report`. Restart the
PM2 validator and require the same database integrity and frozen shadow hashes before allowing new
work. Upgrade and rollback use an explicit reviewed revision:

```bash
bitcast-x backup-state --output /absolute/path/to/backups/pre-upgrade
git fetch --tags origin
git checkout --detach <reviewed-release-tag-or-commit>
uv sync --frozen --all-extras
./scripts/start-pm2-validator.sh

# Roll back code and dependencies while retaining/restoring compatible state.
git checkout --detach <previous-release-tag-or-commit>
uv sync --frozen --all-extras
./scripts/start-pm2-validator.sh
```

If a release migrated state in a way the previous binary cannot read, restore the pre-upgrade
backup instead of opening the newer database with older code. Operators who also want PM2 for a
miner can use the checked `bitcast-x-miner` role in `ecosystem.config.cjs` with the miner environment
template; the helper scripts intentionally remain validator-only.

Miner liveness is `GET :8095/health`; readiness is `GET :8095/ready` and becomes 200 only after
the endpoint advertisement finalizes. Validator liveness is `GET :8096/health`; readiness is
`GET :8096/ready` and becomes 200 after one complete finalized reconciliation cycle. Metrics are
at `GET :8096/metrics` and contain only fixed-name process counters and the latest finalized block.

## Backup and restore

Create an online, transactionally consistent backup; do not copy a live `.sqlite3` file and its WAL
with ordinary filesystem tools.

```bash
bitcast-x state-info
bitcast-x shadow-report
bitcast-x backup-state --output /backups/bitcast-x/2026-08-05T120000Z
```

Verify every database reports `integrity: ok`, copy the backup directory off the node, and retain
its `manifest.json`. To restore, stop the node, move the current state directory aside (do not
overwrite it), copy the backed-up `.sqlite3` files into a new empty state directory, preserve
ownership for UID 10001, run `bitcast-x state-info`, then start the same image version that created
the backup. Upgrade only after the restored node catches up successfully.

## Recovering a miner that lost commitment history

Use this only when the configured hotkey can no longer extend the batch history already accepted
by validators. Ordinary restarts and upgrades must keep using the existing state database.

1. Stop creator traffic and the miner writer, but leave its state directory intact.
2. Back up the complete state directory and record the miner hotkey.
3. Upgrade all validators, then upgrade the miner to the release supporting `DX3` histories.
4. With the miner still stopped, rotate its local history once:

   ```bash
   bitcast-x resume-history \
     --confirm-hotkey <exact-configured-hotkey>
   ```

5. Record the returned history ID, then start the miner. Create a new
   claim; do not reuse any claim from before the boundary.
6. Require every validator to advance through the first resumed batch with no quarantine warning
   before reopening creator traffic.

The command is local, transactional, and idempotent until the first new batch is committed. It
persists the random history ID, preserves old rows for audit, marks pre-boundary pending operations rejected with
`history_resumed`, and clears local active claims. The first batch in the new history starts at
sequence 1 automatically and becomes the signed on-chain boundary. Running recovery again after
new batches exist creates another new history; it never alters either older history.

## Upgrade and rollback

1. Record the running image digest, package version, finalized cursor and readiness response.
2. Create and verify an online backup.
3. Deploy the reviewed new image against the same persistent volume. Forward-only SQLite
   migrations run before processing starts and reject state created by a newer binary.
4. Confirm readiness, per-miner cursor advancement, no manifest gaps, and stable shadow-vector
   totals.
5. Compare frozen campaign attribution, reward decisions and shadow weights with a second
   independent validator before expanding the canary.

For application rollback, redeploy the recorded image digest. If the newer release applied a schema
that the older binary cannot read, stop the node and restore the pre-upgrade backup into a new state
directory; never run two writers against one SQLite volume.

## Incident rules

- X, archive RPC or campaign evidence unavailable: keep the campaign unreconciled and retain the
  previous authoritative state. Do not translate availability into rejection or burn.
- Signature, hash, sequence or historical conflict: quarantine only the affected miner and retain
  its last verified cursor.
- Commitment space exhausted: stop accepting platform work through backpressure; never overwrite
  or skip queued evidence to make room.
- Campaign mutation after a positive reward allocation or frozen-weight replay mismatch: treat as
  a consensus incident, preserve the database and logs, and stop weight submission until the
  incident is resolved. A zero-value campaign mutation is expected: confirm the
  `reopened zero-value campaign with latest contract` log and the next replaceable status update.
