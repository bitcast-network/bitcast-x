# Bitcast X v3 protocol

This document is the self-contained protocol overview for the Bitcast X miner and validator in
this repository. It covers SN93 mechanism 1, the `preclaim_v2` mining path, the temporary
`legacy_connection` overlap, scoring, and weight construction. No external source repository is
required to understand or implement the released behavior.

The exact schemas and formulas shipped by a release are authoritative. Their primary source files
are linked below, and the golden-vector and deterministic replay tests pin behavior. A release must
update this document and the [compatibility policy](protocol-compatibility.md) when it changes a
consensus-visible rule.

## Roles and trust boundaries

- A **campaign publisher** publishes the current campaign manifest and immutable,
  digest-addressed ecosystem maps. These define campaign timing, creator eligibility, content
  requirements, attribution mode, and reward inputs.
- A **miner** is an SN93 hotkey that commits creator claims and submissions on chain, advertises an
  HTTP endpoint through its metagraph axon record, and serves the complete committed batches.
- A **validator** discovers miners from finalized metagraph state, authenticates batch requests,
  verifies every batch against historical chain state, independently obtains public X evidence,
  freezes attribution and scoring, and calculates mechanism-1 weights.
- X-data and LLM providers are availability and evidence dependencies. Their failure does not
  become a rejection. An unavailable tweet remains explicitly pending while independently
  verifiable campaign tweets continue through final scoring and rewards.

The campaign publisher, X provider, and configured LLM are not decentralized by this protocol.
Validators independently verify miner history and repeat the scoring rules, but they consume the
same published campaign input and external public-content evidence.

## Version map

Several version numbers cover different boundaries and must not be conflated:

| Boundary | Current version | Meaning |
| --- | --- | --- |
| Campaign manifest | `4` | Adds each campaign's required top-`max_members` creator-rank cutoff |
| Validator-to-miner HTTP | `3` | `/v3/batches` includes each batch's claimed finalized chain position |
| Temporary HTTP overlap | `2` | `/v2/batches` returns the same complete batches without positions |
| Claim, submission, and batch content | `2` | Strict event schemas, canonical hashing, and `DX2` on-chain envelopes |
| Campaign mining mode | `preclaim_v2` or `legacy_connection` | Selects the new committed-claim path or temporary imported legacy behavior |

The strict wire models are in
[`src/bitcast_x/protocol/models.py`](../src/bitcast_x/protocol/models.py), canonical encoding is in
[`src/bitcast_x/protocol/canonical.py`](../src/bitcast_x/protocol/canonical.py), and the fixed
commitment envelope is in
[`src/bitcast_x/protocol/commitments.py`](../src/bitcast_x/protocol/commitments.py).

## Public campaign input

The configured campaign URL returns a v4 manifest. Each map reference includes its ecosystem ID,
activation time, byte size, path, and `sha256-<hex>` digest. Miners may list campaigns without
downloading maps. Validators download the maps needed for scoring, enforce the response-size bound,
and verify each digest before caching. Rank eligibility unions the qualifying creators from every map
whose active interval overlaps the campaign. For scoring, validators still select the map active when
the tweet was published as the tweet-time influence input.

Each campaign configures:

- `campaign_id`, `mechanism_id`, and `mining_protocol`;
- the UTC open/close interval and finalized `scoring_close_block`;
- an optional `exclusive_miner_hotkey`;
- eligible ecosystem pools, their historical maps, and a positive `max_members` cutoff;
- brief text, required terms, language, tag, quoted-tweet, keyword, and prompt-version rules;
- reward pool, optional per-creator tweet limit, and emission block interval.

Campaign and map schemas reject unknown fields, ambiguous duplicate IDs, naive timestamps, invalid
windows, and incomplete emission bounds. The `campaign_id` is the stable identity. Until final
results exist, validators adopt the latest complete record published for that ID, including routing
and content-rule corrections. The contract stored with final reconciliation is immutable.

`prompt_version` explicitly selects one of three semantic-evaluation templates. Version 1 is a
generic compliance prompt that checks only whether the post follows every instruction in the
brief, without adding product-, brand-, review-, or sentiment-specific rules. Version 2 evaluates
conventional sponsored coverage. Version 5 evaluates honest product or service reviews: positive,
neutral, mixed, critical, and negative conclusions are equally valid; the post must instead make
the product or service its primary subject, contain a specific assessment with supporting
substance, and meet the brief's objective coverage requirements. Sentiment, ratings, and
conclusions prescribed by a version-5 brief are not eligibility requirements. Versions 2 and 5
remain byte-stable, and every available template is pinned by a golden SHA-256 test.

The schema and digest checks are implemented in
[`src/bitcast_x/campaigns.py`](../src/bitcast_x/campaigns.py).

## Miner claim and submission flow

For an open `preclaim_v2` campaign:

1. The platform creates a random 128-bit `claim_id` and 256-bit nonce. The private draft is NFKC
   normalized and committed as
   `SHA-256("dx2/draft" + NUL + canonical_json({claim_id, draft_nfkc, nonce}))`.
2. The miner queues a public `ClaimEvent` containing the campaign, immutable numeric creator X ID,
   UTC creation time, and draft commitment. The draft and nonce remain private at this stage.
3. The miner includes the claim in its next hash-linked batch and commits the batch envelope through
   Bittensor's Commitments pallet. The platform reports `safe_to_post` only after finalization and
   an exact read-back of the stored bytes.
4. After the creator publishes, the platform queues a `SubmissionEvent` containing the campaign,
   numeric tweet ID, miner hotkey, and `claim_id`. The corresponding draft reveal is served with the
   completed batch so validators can recompute the earlier commitment.
5. The submission must be committed no later than `scoring_close_block`.

At most five unconsumed claims are active for each `(miner hotkey, campaign, creator X ID)`. Claims
are ordered by finalized block, extrinsic index, and event index; a sixth claim evicts the oldest.
An accepted claim is consumed and cannot win another tweet.

An exclusive campaign skips claims and lexical matching. Its submission uses a null `claim_id` and
is accepted only from the campaign's fixed exclusive miner hotkey after the normal tweet,
qualification, and timing checks.

The reference platform API exposes these operations behind bearer authentication at
`/api/claims`, `/api/submissions`, `/api/campaigns`, and `/api/qualification`. The API is an
operator convenience; the committed events and batches are the protocol record.

## Batches, chain anchors, and validator transport

Events are durably queued and placed into append-only batches. Sequence 1 has no previous hash;
every later batch includes the prior batch hash. Batch content is canonical UTF-8 JSON with sorted
keys, NFKC strings, UTC timestamps, explicit nulls, and no non-finite numbers. Its digest is the
domain-separated SHA-256 hash `dx2/batch`.

The on-chain envelope is exactly 45 bytes:

```text
"DX2" | sequence:u64 big-endian | event_count:u16 big-endian | batch_hash:32 bytes
```

The miner serves complete finalized batches through `POST /v3/batches`. Requests use Bittensor v11
HTTP authentication bound to the miner receiver hotkey, request method, path, body, timestamp, and
nonce. Replayed requests and callers without a current validator permit are rejected. Request,
response, page-count, and per-validator rate bounds apply.

The v3 response includes the miner-reported `(block, extrinsic_index)` for each batch. A validator
does not trust that position: it reads the historical finalized block, verifies the exact signed
Commitments extrinsic and stored envelope, then verifies the batch sequence, previous hash, event
count, and batch hash. It finally compares the reconstructed tip with the miner's latest on-chain
envelope. Only then does its durable per-miner cursor advance.

An unavailable endpoint retains its cursor for a later retry. A signature, sequence, hash,
position, or manifest conflict quarantines that miner's current reconciliation without changing
other miners' verified history.

The implementation is in [`src/bitcast_x/miner/engine.py`](../src/bitcast_x/miner/engine.py),
[`src/bitcast_x/transport.py`](../src/bitcast_x/transport.py), and
[`src/bitcast_x/validator/ingestion.py`](../src/bitcast_x/validator/ingestion.py).

## Qualification and attribution

Qualification is evaluated from finalized historical chain state. A versioned qualification entry
contains an owner hotkey, minimum conviction in alpha, an optional minimum miner-hotkey stake in
alpha, and an effective block. The complete Finney netuid 93 history is release-pinned in the
shared miner and validator package rather than independently configured by operators. A miner
qualifies through either enabled path:

- its controlling coldkey's lock targets the configured owner hotkey with at least the required
  conviction; or
- its miner hotkey has at least the required alpha staked to it on the campaign subnet, aggregated
  across all source coldkeys.

The source coldkey does not affect the miner-hotkey stake path. A missing or zero threshold disables
only its corresponding path; when both paths are disabled, qualification is explicitly disabled.
Existing schedule entries without `minimum_self_stake_alpha` therefore remain lock-only. The
explanatory qualification endpoint retains the compatibility field names `self_stake_alpha`,
`required_self_stake_alpha`, and `qualified_via`; `self_stake_alpha` reports the aggregate stake on
the miner hotkey for the subnet. The final attribution check uses the rule effective at the
campaign's scoring close; pre-close results may remain pending while the miner can still qualify.

Before attribution, the validator independently fetches the tweet and requires:

- publication within the inclusive campaign UTC window;
- an author X ID ranked within the campaign's top `max_members` in at least one configured
  ecosystem map whose active interval overlaps the campaign;
- all required terms and any configured tag or quoted tweet;
- at least one configured inclusion keyword, when present;
- the configured language, except unknown/undetermined provider values;
- an original tweet rather than a retweet or reply.

For an open campaign, the referenced claim must belong to the submitting miner, campaign, and tweet
author; precede publication; remain in the five-claim active set; remain unconsumed; and have an
exact reveal. The latest valid candidate from each miner competes in the matcher.

Matcher text is NFKC-normalized, case-folded, URL-normalized, and whitespace-normalized. The score
is:

```text
0.55 * character-trigram Dice(draft, tweet)
+ 0.45 * multiset-token Jaccard(draft, tweet, public campaign terms removed)
```

A winner needs a score of at least `0.70` and a margin of at least `0.10` over the runner-up. An
exact tie, weak match, or narrow margin abstains rather than assigning the tweet.

Every submitted tweet receives a stable accepted, pending, or rejected reason. Evidence that is
unavailable at final reconciliation is frozen as `evidence_unavailable` pending rather than
rejecting the tweet or blocking the campaign. Campaign checks report the first failed requirement
in deterministic evaluation order: `post_outside_campaign_window`,
`creator_not_eligible_for_campaign`, `required_terms_missing`, `retweet_not_allowed`,
`reply_not_allowed`, `campaign_tag_missing`, `required_quote_missing_or_incorrect`, or
`required_campaign_keyword_missing`. The broader `campaign_ineligible` reason remains valid for
aggregate campaign limits and incompatible campaign routing. The complete reason enum and
model are in [`src/bitcast_x/protocol/models.py`](../src/bitcast_x/protocol/models.py); the replay
rules are in
[`src/bitcast_x/validator/reconciliation.py`](../src/bitcast_x/validator/reconciliation.py), and
the matcher is in [`src/bitcast_x/matcher.py`](../src/bitcast_x/matcher.py).

Rank is deterministic: accounts are ordered by influence descending and immutable numeric X ID
ascending for ties. Being present elsewhere in the full ecosystem map is not campaign eligibility,
and prior participation does not bypass the cutoff for a later tweet. Results frozen before the v4
cutoff was published remain immutable rather than being recalculated retroactively.

## Scoring and rewards

Only accepted attributions that pass the campaign's semantic brief evaluation enter rewards.
Engagement evidence is taken from the configured X provider and frozen for the campaign. The
validator performs the campaign-selected LLM prompt checks with temperature zero; any passing check
passes the tweet. Unavailable engagement or semantic evidence leaves only that tweet's reward
disposition pending; available tweets continue without translating the outage into rejection.
Prompt text and parsing behavior are shipped in this repository. A new prompt version is dormant
until selected by a campaign; changing an existing version would change its durable cache key and
evaluation behavior.

The engagement score starts at twice the author's influence. Retweets from considered accounts add
`1 * influence`; quotes add `3 * influence`. When a positive relationship score exists from an
engager to the author, that contribution is multiplied by `0.1 + 0.9 / relationship_score`.
Self-engagement is excluded. After brief evaluation, passing campaign participants cannot increase
one another's scores. Influence uses the maximum of the tweet-time value, current considered value,
and the current map minimum; an account dropped from the latest map retains half its prior value.
Scores are rounded to six decimal places.

Reward construction then:

1. divides each campaign reward pool into seven equal daily budgets;
2. greedily assigns a tweet to at most one campaign by its estimated payout, enforcing any
   per-creator campaign limit;
3. applies four relative performance bonuses—views, views per follower, total engagements, and
   engagements per view—each adding at most 5% to score;
4. deterministically selects one of the five most-viewed assigned tweets as featured and applies a
   1.05 score multiplier to its author and engaging accounts;
5. divides the daily budget in proportion to `max(score, 0) ** 0.65` and freezes the per-tweet
   daily USD floors;
6. sums floors by miner UID and normalizes them into the mechanism-1 weight vector.

If no `preclaim_v2` miner has productive content, its standalone fallback assigns the vector to
burn UID 0. The formulas are in [`src/bitcast_x/scoring.py`](../src/bitcast_x/scoring.py) and
[`src/bitcast_x/rewards.py`](../src/bitcast_x/rewards.py). Frozen attribution, evidence, reward
decisions, and weights are durable and reused after restart.

## Temporary legacy overlap

`legacy_connection` is a transition path, not a second public miner wire protocol. A validator
operating during the overlap imports the outgoing validator's complete connection database, reward
snapshots, and cumulative tweet store. A fresh validator cannot reconstruct that historical state
from bounded provider queries; it must start from a verified state archive or wait until all legacy
campaign and referral liabilities have drained.

During overlap, v3 calculates legacy campaigns locally and preserves their non-burn weights. Only
legacy UID 0 excess is replaced by productive `preclaim_v2` weights. If no new-path miner is
productive while a legacy campaign remains, that excess is routed to temporary treasury UID 155;
missing UID 155 fails the cycle closed. The legacy engine, imported state, and treasury routing are
deleted together after the final legacy liability drains.

The exact migration and state verification procedure is in the
[operator runbook](operator-runbook.md).

## External data and operator-visible effects

- On chain: the miner endpoint record, 45-byte batch tip envelopes, and validator weight
  submissions when explicitly enabled.
- Miner-to-validator: complete claims, submissions, and post-publication draft reveals over signed
  HTTP. The draft is private only until its submission is batched.
- Campaign service: manifest and ecosystem-map requests.
- X provider: public tweet and engagement lookup identifiers and campaign discovery queries.
- LLM provider: public tweet text and campaign brief content; no wallet secret or private draft is
  required for semantic scoring.
- Central ingestion, when explicitly enabled: hotkey-signed frozen campaign, attribution, scoring,
  and reward output.
- Remote logging, when configured: application log records. Operators must treat logs as potentially
  sensitive and configure or disable the sink according to their policy.

Wallet secret material remains local to the miner or validator process and is never part of a
protocol event, batch response, campaign feed, LLM prompt, or ingestion payload.

## Compatibility and verification

The [compatibility policy](protocol-compatibility.md) defines when a protocol version must change,
how overlap works, and when an older version may be removed. The
[operator runbook](operator-runbook.md) defines durable state, backup, upgrade, rollback, health,
and incident handling.

Behavior is pinned by the protocol, commitment, transport, reconciliation, scoring, rewards,
qualification, campaign, and restart tests under [`tests/`](../tests/). Run the complete release
gate with:

```bash
uv sync --locked --all-extras
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```
