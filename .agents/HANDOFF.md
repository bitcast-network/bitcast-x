# Handoff

Goal: accept already-published tweets for exclusive/direct campaigns during the central API's one-day submission grace period.

Status: direct submissions use the central campaign's `can_submit` capability to bound the submission window and historical creator `eligible` status to authorize the existing post. During `evaluating`, the miner now force-commits the durable event and confirms its finalized block is no later than `scoring_close_block` before returning success. The producer contract is pinned from `bitcast-api`, and focused tests cover on-time, late, and timed-out commitments.

Files changed:
- `src/bitcast_x/miner/control.py`
- `src/bitcast_x/miner/api.py`
- `tests/test_miner_api.py`
- `tests/contracts/bitcast_api_miner_campaign.py`
- `docs/application-api.md`
- `CHANGELOG.md`

Verification:
- `pytest -q`: 423 passed, 3 expected skips
- `ruff format --check src tests`: 109 files formatted
- `ruff check src tests`: passed
- `mypy src`: passed

Decision: do not reinterpret `eligible_if_published_now`; it remains false after the posting window. Direct submission uses historical `eligible` only when the central API separately advertises `can_submit=true`. Grace submissions have a bounded 30-second confirmation wait and return retryable `503 submission_commitment_pending` if confirmation is unavailable; a finalized late commitment returns `409 submission_deadline_passed`. Open-window submissions retain asynchronous batching.

Risk: production remains unchanged until this PR and its `bitcast-api` dependency are merged and deployed.

Next action: push this branch, obtain the required protocol review, then merge and deploy the companion API commit before manually deploying this miner commit and running an end-to-end grace-period submission smoke test.

## September 8 submission latency

Branch `fix/avoid-repeat-submission-campaign-fetch` reuses the campaign visibility
check already completed by direct submission before fetching fresh creator
eligibility. A private helper retains all ecosystem filtering and eligibility
requirements. Production logs showed campaign reads of9.119s and9.441s followed
by47ms eligibility within one submission; scoring is outside this request path.
Companion API changes scope a single-campaign read before computing stats.

Validation:423tests passed and3expected integration skips in the full local run;
two auto-update tests initially lacked uv on PATH and the complete auto-update
file passed after adding the existing uv binary. Ruff and mypy passed. The new
regression verifies exactly one campaign read plus fresh eligibility, while the
existing suite retains rejection, idempotency and grace commitment coverage.

Release state verified from the running ECS task: task22 uses image
69ef77dcd06a57cf418120cbbb6990e23d370381 (started September 1). An earlier
reference to e70af88 was incorrect. The only main commit after the live image
is ef62ef5908d6a79cb8e87d7bd57111911a292a25, the already-merged PR #125 that
caches miner stake qualification for 60 seconds. A release of this PR therefore
also introduces that bounded staleness; creator eligibility remains fresh.

PR #126 requires an independent reviewer before merge. API PR #548 is merged;
its staging deployment succeeded and production approval is pending at
https://github.com/bitcast-network/bitcast-api/actions/runs/34206714155.
Next: obtain the required review and the owner's choice to defer the miner
release or include the qualification cache. No miner deployment was dispatched.
