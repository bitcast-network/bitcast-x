<p align="center">
  <a href="https://www.bitcast.network/">
    <img src="assets/lockup_gradient.svg" alt="Bitcast Logo" width="800" />
  </a>
</p>

# Bitcast X — Decentralized Social Mining on X.com

Bitcast X is a Bittensor subnet that incentivizes X content creators to connect brands to
audiences. Creators publish tweets to satisfy defined briefs and earn rewards based on engagement
from influential accounts within curated social networks.

---

## ⚙️ How Bitcast X works

- **Brands** define campaign briefs for creator content.
- **Creators** publish original X content for active campaign briefs.
- **Miner platforms** coordinate creator participation and commit verifiable claims and results.
- **Validators** independently verify submissions, score engagement, calculate rewards, and submit
  weights.
- **Bittensor SN93** distributes on-chain emissions according to the resulting mechanism-1 weights.

This repository contains the Bittensor v11 miner and validator implementation. It is the complete
public reference for released behavior: the [protocol](docs/protocol.md) defines the flow,
attribution, scoring, rewards, and trust boundaries; the
[core tweet flow testing plan](docs/tweet-flow-testing.md) describes deterministic claim,
submission, and validation coverage without publishing new X posts; the
[compatibility policy](docs/protocol-compatibility.md) defines safe evolution; and the
[operator runbook](docs/operator-runbook.md) defines deployment and recovery.

## ⛏️ Reference miner

This repository provides protocol building blocks and a deliberately basic reference miner—not a
creator dashboard or a platform template. A miner platform owns its creator product, onboarding,
support, payouts and acquisition; this package owns canonical claims, batching, Bittensor
commitments, signed validator transport and crash recovery.

Install and inspect the CLI:

```bash
uv sync --all-extras
uv run bitcast-x --help
```

For a least-privilege source or PM2 installation, start with
[`config/miner.env.example`](config/miner.env.example) or
[`config/validator.env.example`](config/validator.env.example). The root `.env.example` is the
exhaustive reference for both roles and optional integrations. Published network and protocol
values are already populated; replace only the public IP, wallet hotkey/path, state paths and
provider credentials that differ for your host. Existing Bittensor keys are loaded from the
configured wallet directory and are never created or copied by the Python application. The durable
state directory must survive process restarts. The
[operator runbook](docs/operator-runbook.md#pm2-source-install) contains checked PM2 commands and
upgrade/rollback steps.

Start the signed endpoint and advertise it through the registered miner hotkey:

```bash
uv run bitcast-x run-miner
```

The minimal creator journey can be exercised from the same installation:

```bash
uv run bitcast-x campaigns
uv run bitcast-x claim --campaign-id CAMPAIGN --creator-x-id 123 --draft "Private draft"
uv run bitcast-x claim-status CLAIM_ID
uv run bitcast-x submit --campaign-id CAMPAIGN --tweet-id 123456789 --claim-id CLAIM_ID
uv run bitcast-x submission-status SUBMISSION_ID
uv run bitcast-x qualification
```

`claim` returns `safe_to_post` only after finalization and exact storage verification. A null claim
is the exclusive-campaign submission shape; validators independently enforce the campaign feed,
exclusive hotkey, X authorship, qualification and attribution rules.

## 🛡️ Validator operation

The validator ingests and verifies miner commitments, reconciles campaigns, calculates the complete
weight vector, and persists reproducible results. Use an RPC that can read the historical
commitment blocks reported by miners, then run:

```bash
uv run bitcast-x run-validator
```

For a validator managed by PM2, the supported quick start is:

```bash
npm install --global pm2@latest
./scripts/setup-pm2-validator.sh
# Edit the generated .env, then:
./scripts/start-pm2-validator.sh
```

The setup script installs the locked runtime and creates a private validator environment without
overwriting an existing `.env`. The launch script validates the required settings, starts or
restarts only `bitcast-x-validator`, waits for local health, and then saves the PM2 process list.
See the [PM2 runbook](docs/operator-runbook.md#pm2-source-install) for prerequisites and upgrades.

Source installs do not update automatically; optional update behavior and manual upgrade
instructions are documented in the [operator runbook](docs/operator-runbook.md).

The validator discovers endpoints from finalized metagraph state; it does not accept manually
configured miner URLs. It requests only batches beyond each durable per-miner cursor, verifies the
reported finalized position against the exact historical block, and independently compares the
completed history with the miner's latest on-chain envelope. Unreachable miners retain their prior
state and heal on a later poll; gaps or conflicting history quarantine only that miner's current
reconciliation run. One unavailable or incompatible miner never prevents the validator from
processing verified histories, becoming ready, or producing the current cycle. Weight submission
is enabled by default and follows the configured on-chain cadence.

Accepted scoring-close evidence, optimistic brief verdicts, engagement scores, participant
exclusions, and performance/featured bonuses are frozen in the validator database. The complete
normalized campaign record is bound to its campaign ID before commitments are reconciled or scored;
any change to its public, access, timing, scoring, or economic terms requires a new campaign ID.
Semantic evaluation requires the selected Chutes or OpenRouter key; provider availability never
becomes a content rejection. During the configured seven-day emission block window, the validator
calculates the complete proposed vector and stores it durably for inspection and reproducibility,
whether or not submission is enabled. Campaign feed records can
also carry the proven tag, quote-ID, inclusion-keyword and prompt-version filters. Tweet language
remains observed evidence but does not affect eligibility.
Open-campaign attribution excludes all public campaign text, required tags, inclusion keywords and
the canonical quoted-post identity from its private token-overlap component.
By default, the validator publishes each frozen campaign once through the hotkey-signed
`/api/v1/brief-tweets` DEEBLY ingestion contract and submits the same durable vector to mechanism 1
at the configured cadence. Operators can disable either output independently for diagnostics. The
public network qualification schedule ships with each reviewed release so miners and validators
cannot retain different rules through stale environment files. It requires either 15,000 alpha
conviction toward the qualification owner hotkey or, from version 2 at block 8,874,000, 15,000
alpha staked to the miner hotkey on netuid 93. The stake is aggregated across every coldkey that
supplied it; the source coldkey does not affect this path.
If either setting could economically activate an eligible campaign, the validator fails closed
while both effective qualification thresholds are zero. The v11 chain adapter uses Bittensor's
`SetWeights` intent—which performs the current subnet conformance and commit-reveal selection.

The packaged container runs as UID 10001 and stores all mutable state under
`/var/lib/bitcast-x`. Validator liveness/readiness/metrics are served on port 8096. Create
consistent live backups with `bitcast-x backup-state --output PATH`; inspect integrity and schema
versions with `bitcast-x state-info`. Compare independent validators using the deterministic hashes
from `bitcast-x shadow-report`.

Platforms can run the same miner as an authenticated, single-writer service with
`bitcast-x run-miner-api`. Its versioned `/api/v1` surface covers qualification, enabled
ecosystems, their combined account leaderboard, protocol-v2 campaigns, creator eligibility,
idempotent claims, submissions, campaign results, and total USD reward recommendations. OpenAPI is
available at `/api/v1/docs`; the signed
validator batch protocol remains on the same port.
Configure a 64-character-or-longer
`BITCAST_X_MINER_API_TOKEN`; control routes require it as a bearer token, while validator routes
continue to use Bittensor hotkey authentication. The command contains no platform branding,
user/session logic, payment policy, or deployment-provider assumptions.

## 🧰 Development

```bash
uv sync --all-extras
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

## 📄 License, security, and support

Bitcast X is open-source software under the [MIT License](LICENSE). Contributions are accepted
under the same license; see [CONTRIBUTING.md](CONTRIBUTING.md) for the checks and licensing
expectations.

The current software release is `2.0.0`. Software release numbers are separate from the protocol
versions described in the protocol documentation. See the
[changelog](CHANGELOG.md), [release policy](docs/release-policy.md),
[security policy](SECURITY.md), and [support guide](SUPPORT.md).
