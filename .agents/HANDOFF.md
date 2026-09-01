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
