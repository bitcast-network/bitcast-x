# Core tweet flow testing plan

This plan describes how contributors should test claim, submission, and tweet-validation changes
without publishing a new post on X for every test. The goal is a small, deterministic harness that
exercises Bitcast X behavior through its real public boundaries while remaining fast enough for
ordinary pull requests.

## Principles

- Tests must never create, edit, or delete an X post.
- Synthetic fixtures are the default. A passing test must not depend on X, an X-data provider, a
  funded wallet, or another hosted service being available.
- Exercise production code wherever practical: miner and validator stores, canonical events and
  batches, commitment verification, attribution, scoring, and publication payloads.
- Keep test-only evidence injection outside production configuration. A deployed validator must
  not have a switch that lets an operator replace independently fetched X evidence with fixtures.
- Prefer a few complete journeys and focused edge cases over a second implementation of the
  protocol in test code.
- A changed consensus-visible result must be explicit in the pull request. Tests should fail when
  attribution reasons, scores, rewards, or published payloads change unexpectedly.

## Scope and repository boundary

The complete harness and its synthetic fixtures belong in this repository. They let miners,
validators, and external contributors verify the released implementation without access to a
Bitcast-operated product or service.

This plan covers only the core Bitcast X mechanism:

- miner claim and submission APIs;
- durable miner events, batches, and chain commitments;
- signed miner-to-validator transport and historical verification;
- independently fetched, normalized X evidence;
- campaign eligibility, qualification, attribution, and draft matching;
- engagement scoring, reward construction, frozen state, and publication payloads.

Creator products, user authentication, platform ledgers, browser interfaces, payments, and other
downstream consumers are outside this plan. No new repository or shared testing service is needed.

## Smallest useful harness

Add one integration test module that composes the existing production components with two narrow
test doubles:

1. A `FixtureXProvider` implements the existing normalized X-provider interface from immutable
   synthetic `Tweet`, `TweetFetch`, and `EngagementFetch` values.
2. An in-memory chain adapter records commitment envelopes, finalized positions, qualification
   state, and the metagraph information needed by the miner and validator.

Everything between those boundaries should be real application code:

```text
miner API -> MinerStore -> batch commitment -> chain adapter
                                            -> validator ingestion -> ValidatorStore
fixture X evidence -> reconciliation -> scoring/rewards -> publication result
```

Use temporary SQLite databases and ephemeral test keypairs. Drive time and block height from the
scenario rather than wall-clock sleeps. A test should complete in seconds and leave no external
state behind.

The fixtures should contain only the inputs needed to understand a scenario: campaign and ecosystem
map, claim draft, normalized tweet evidence, normalized engagement evidence, qualification state,
and expected result. Handwritten synthetic fixtures are preferred to large captured API responses.

## Required journeys

The deterministic integration module
[`tests/integration/test_tweet_flow.py`](../tests/integration/test_tweet_flow.py) proves both happy
paths through the same production components. Run it directly while developing tweet-flow changes:

```bash
uv run pytest -q tests/integration/test_tweet_flow.py
```

### Open `preclaim_v2` campaign

1. Create a draft claim through the miner API.
2. Finalize and read back its batch commitment before reporting `safe_to_post`.
3. Submit the fixture tweet against the claim and finalize the submission batch.
4. Have the validator discover and verify the miner's complete history.
5. Fetch synthetic public evidence through `FixtureXProvider`.
6. Verify the claim predates publication, the reveal matches, and the draft clears the matcher.
7. Freeze attribution and scoring, construct rewards, and produce the expected publication result.

### Exclusive `preclaim_v2` campaign

1. Submit a completed fixture tweet with a null `claim_id`.
2. Verify it was submitted by the campaign's exclusive miner.
3. Apply the normal author, campaign, content, qualification, timing, scoring, and publication
   checks without running the draft matcher.

The initial edge-case table should stay focused on failures that cross more than one component:

| Scenario | Expected outcome |
| --- | --- |
| Claim commitment is not finalized | Creator is not told it is safe to post |
| Claim is finalized after tweet publication | `claim_after_publication` |
| Draft reveal does not match the commitment | `draft_reveal_mismatch` |
| Published text does not clear the matcher | `score_below_floor` or `ambiguous_match` |
| Tweet author does not match the open claim | `author_mismatch` |
| Tweet is outside the campaign or content rules | `campaign_ineligible` |
| Submission is committed after scoring close | `late_submission` |
| Miner is not qualified | pending before close, final rejection at close |
| X provider is unavailable | campaign remains unreconciled; prior state is retained |
| Miner or validator restarts between stages | the same durable IDs and result are recovered |

Focused unit tests remain the right place for exhaustive schema validation, matcher vectors,
pagination limits, retry permutations, and individual scoring formulas.

## Current failure coverage

The edge cases above are covered at the narrowest layer that still proves the complete invariant:

- Unfinalized claims and restart-safe miner state are covered in
  [`tests/test_miner_api.py`](../tests/test_miner_api.py) and
  [`tests/test_miner_sdk.py`](../tests/test_miner_sdk.py).
- Claim timing, changed reveals, open-claim author identity, matching, content eligibility, late
  submissions, exclusive-miner identity, qualification transitions, authoritative tweet absence,
  and provider outages are covered in
  [`tests/test_reconciliation.py`](../tests/test_reconciliation.py).
- Commitment encoding, batch hashes, sequence links, and claim lifecycle rules are covered in
  [`tests/test_commitments.py`](../tests/test_commitments.py),
  [`tests/test_protocol_models.py`](../tests/test_protocol_models.py), and
  [`tests/test_protocol_state.py`](../tests/test_protocol_state.py).
- Signed-request authentication, replay protection, receiver binding, rate limiting, and response
  bounds are covered in [`tests/test_transport.py`](../tests/test_transport.py).
- Exact historical commitment verification, manifest gaps, miner isolation, outages, and validator
  cursor recovery are covered in
  [`tests/test_validator_ingestion.py`](../tests/test_validator_ingestion.py).
- Engagement scoring, reward allocation, frozen economics, and publication idempotency are covered
  in [`tests/test_attribution_scoring.py`](../tests/test_attribution_scoring.py),
  [`tests/test_reward_coordinator.py`](../tests/test_reward_coordinator.py), and
  [`tests/test_publishing.py`](../tests/test_publishing.py), with the accepted full result also
  asserted by the deterministic integration journey.

Add a case to the full journey when a change crosses a public boundary or changes the ordering of
claim, commitment, submission, ingestion, validation, scoring, or publication. Add a focused test
when one component's input/output contract is enough to prove the behavior. This keeps failures
easy to diagnose without duplicating the complete setup for every rejection reason.

## Chain coverage

Normal pull-request tests should use the in-memory adapter. They must not submit to a public chain
or require funded wallets.

Before a release that changes chain integration, commitment encoding, authentication, or historical
verification, run the existing opt-in local-Subtensor tests and extend one of them to consume the
same synthetic tweet scenario. This uses real Bittensor calls, signatures, finalization, historical
read-back, and signed miner transport against a disposable local node. It still does not touch
mainnet or testnet.

A public testnet smoke test is optional and should be reserved for SDK or network-behavior changes
that a local node cannot faithfully reproduce. It is not part of every pull request.

## External dependency checks

Fixture tests verify Bitcast behavior, not the availability of Desearch or an LLM provider. Keep
those concerns separate:

- Provider parser tests should use small synthetic wire responses.
- An operator may run a read-only canary against an existing historical tweet to detect provider
  authentication or response-shape failures. It should assert stable identity fields, not mutable
  engagement counts.
- LLM behavior used by deterministic protocol tests should be recorded or stubbed. A separate
  read-only canary may verify provider availability.

These canaries must not block ordinary contributor tests, and they never need to publish a tweet.

## What the completed harness proves

The combined public harness should cover claim creation and finalization, submission persistence,
batch hashing and transport, validator chain verification, campaign and author eligibility,
draft matching, qualification, attribution, engagement scoring, reward construction, restart
recovery, and the validator's published result.

It does not prove that production infrastructure, a hosted X-data provider, an LLM provider, or a
public Bittensor network is currently healthy. Small operational canaries may cover those separate
boundaries without becoming part of the deterministic core harness.

## Contributor workflow and completion criteria

The repository now includes the fast harness, reusable synthetic evidence, both happy paths, and
the cross-component failures that were not previously covered. For an ordinary change:

1. Run the deterministic integration module while iterating.
2. Add or update a focused test for the exact changed invariant.
3. Extend the complete journey only if the change crosses one of its real boundaries.
4. Run the standard checks in [CONTRIBUTING.md](../CONTRIBUTING.md) before opening a pull request.
5. Run or extend the opt-in local-Subtensor journey when chain integration itself changes.

A clean checkout can deterministically prove both claim/submission journeys without credentials,
network access, a public-chain transaction, or an X post. Contributors need no separate setup
beyond the development dependencies documented in [CONTRIBUTING.md](../CONTRIBUTING.md).
