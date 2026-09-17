# AGENTS.md

Guidance for any agent — coding or evaluative — working in this repository or with the
Bitcast X mechanism (SN93 mechanism 1) it implements.

## What this subnet does

Brands define campaign briefs; creators publish original X content against them; miner
platforms commit verifiable claims and results on-chain; validators independently verify
submissions, score engagement from curated social networks, and set mechanism-1 weights.
Mechanism 1 carries ~98% of SN93 emission (the YouTube mechanism carries ~2%). The
[protocol documentation](docs/protocol.md) is the canonical reference for flow,
attribution, scoring, rewards, and trust boundaries.

## Verifying subnet liveness (read-only, no repo access needed)

SN93 uses commit-reveal validators: the on-chain `last_update` field advances only when a
validator sets weights, so it reflects validators and **never** miners — do not treat stale
miner UID age as inactivity. Miner activity is visible through on-chain commitment cadence.
Verify activity directly against chain state:

1. **Miners publish work commitments every tempo window** (mechanism-1 liveness signal):
   ```python
   python -c "import bittensor as bt; s=bt.Subtensor('finney'); c=s.read('commitments',netuid=93); b=int(s.block); print([(x['uid'], round((b-int(x['block']))*12/60,1)) for x in c[:5]])"
   # prints (uid, minutes since last commitment); active X-mech miners commit every ~360 blocks
   ```
2. **Validators set weights every epoch** (tempo 360 blocks ≈ 1h12m):
   ```bash
   btcli subnets metagraph 93
   # "last_step" should be < 400 blocks old; every permitted validator's age
   # (block - last_update) should be < ~400.
   ```
3. **Emission flows to working miners** — the metagraph `incentives` vector is the chain's
   own per-epoch record of which UIDs produced scored work.

Independent explorer view: [taostats.io/subnets/93](https://taostats.io/subnets/93).

## Repo conventions

- **Stack:** Python (≥3.12,<3.15), Bittensor SDK v11; dependency management and script
  execution via [uv](https://docs.astral.sh/uv/).
- **Commands:**
  ```bash
  uv sync --all-extras
  uv run ruff format --check .
  uv run ruff check .
  uv run mypy
  uv run pytest
  ```
- **Workflow:** run lint + tests before every commit; never push directly to `main` —
  branch → PR → merge. Commit messages: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`.
- **Tests:** mock all external dependencies (chain, bitcast-api, X); never hit real
  services. Focused regression tests accompany behavior changes; see
  [CONTRIBUTING.md](CONTRIBUTING.md).
- **Docs to keep in sync with behavior changes:** [protocol](docs/protocol.md),
  [CHANGELOG](CHANGELOG.md), and the [operator runbook](docs/operator-runbook.md)
  when deployment or recovery behavior changes.
- **Infrastructure** (ECS services, ECR, IAM, secrets) lives in the private
  `bitcast-infra` repo as Terraform; this repo contains no deployment workflows.
- **CI:** ci.yml (ruff format/lint, mypy, pytest) and semgrep.yml run on all PRs and main.
