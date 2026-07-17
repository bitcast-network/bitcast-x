# Decentralizing Social Discovery: Analysis of Non-Deterministic PageRank in bitcast-x

**Status:** Analysis / discussion document — no code changes proposed here are implemented.
**Scope:** `bitcast/validator/social_discovery/`, `bitcast/validator/weight_copy/`, `bitcast/validator/forward.py`, `bitcast/validator/utils/config.py`, and the stability tooling under `social_discovery/stability/`.

---

## 1. Executive Summary

The subnet's core computation — discovering X/Twitter influence networks via iterated, personalized PageRank (`TwitterNetworkAnalyzer.analyze_network()` + `two_stage_discovery()`) — is not reproducible across independent validators. The pipeline consumes hundreds to thousands of third-party API calls per discovery cycle (Desearch.ai or RapidAPI for tweets, its-ai.org for AI detection), and those APIs return slightly different data depending on *when* and *from what cache state* they are called. Small input differences are then amplified by three nested feedback loops (iterative seed re-selection, two-stage core→extended personalization, and cross-cycle seeding from the previous social map), so two honest validators running the identical code at the same hour produce materially different social maps.

The current workaround is architectural centralization: exactly one validator runs in `discovery` mode; everyone else either downloads its social map (`standard` mode) or copies its weights outright (`weight_copy` mode — the **default**, per `VALIDATOR_MODE = os.getenv('VALIDATOR_MODE', 'weight_copy')` in `config.py:185`). The reference validator is a hardcoded IP: `REFERENCE_VALIDATOR_URL = 'http://44.241.197.212'` (`config.py:192`). This works, but it makes the subnet's scoring a single point of failure and of trust, and it is exactly the "weight copying" pattern that Bittensor's consensus mechanics (commit-reveal, bond penalties) are designed to discourage at the chain level.

The good news: **bit-exact reproducibility is not required.** Yuma Consensus aggregates validator weights with a stake-weighted median and clips outliers, so it tolerates *bounded* divergence. The realistic goal is not "every validator computes the same map" but "every validator computes a map close enough that miner-level weights agree within Yuma's clipping tolerance." That reframing opens up several practical paths, analyzed in §7, with a recommended phased plan in §8.

**Bottom line recommendation:** in the short term, decentralize *verification* before decentralizing *computation* — publish the raw input snapshot alongside the social map so any validator can deterministically recompute and audit it — then move to a shared-snapshot + deterministic-recompute architecture, and only later (if ever) to fully independent discovery with consensus-tolerant divergence. Full detail in §8.

---

## 2. Background: What the Social Map Is and Why It Matters

The social map is the root input of the entire reward pipeline:

```
two_stage_discovery()  →  social_maps/{pool}/{timestamp}.json
        ↓
tweet_scoring/social_map_loader.load_latest_social_map()
        ↓
ScoreCalculator  (tweet score = author_influence × BASELINE_TWEET_SCORE_FACTOR
                              + Σ influence(engager) × engagement_weight)
        ↓
RewardOrchestrator.calculate_rewards()  →  self.update_scores()  →  on-chain weights
```

Every account's `score` in the map is `round(normalized_pagerank × total_pool_followers / 1000, 2)` (`social_discovery.py:623-626`). Which accounts appear in the map at all determines who is "considered" for engagement scoring, and `min_influence_score` (`score_calculator.py:46-51`) is derived from the map's minimum. So both **membership** and **ranking** of the map directly move miner rewards. A divergent map is not a cosmetic difference — it changes who gets paid.

Discovery runs bi-weekly per pool (`DISCOVERY_CYCLE_DAYS = 14`, anchored at `DISCOVERY_REFERENCE_DATE = date(2025, 11, 9)`, with per-pool `date_offset` staggering — `social_discovery.py:39-43`, `should_run_discovery_today()`), in a background thread via `DiscoveryManager.maybe_start()`.

---

## 3. Root Cause: Non-Deterministic Inputs

The PageRank computation itself is effectively deterministic. `nx.pagerank(G, weight='weight', alpha=PAGERANK_ALPHA, max_iter=1000, ...)` (`social_discovery.py:600-603`) is a power iteration to tolerance 1e-6; given the identical graph, personalization vector, and dangling distribution, every validator gets the same result to well beyond the 2-decimal rounding applied to final scores. Node ordering is even canonicalized (`usernames_sorted = sorted(list(all_users))`, line 635), and the codebase shows deliberate determinism work elsewhere (see §3.6). **The non-determinism is entirely in the data layer.** Sources, in rough order of impact:

### 3.1 Rolling time windows anchored to wall-clock "now"

Every fetch uses a cutoff relative to the moment of execution:

- `twitter_client.fetch_user_tweets()` computes `incremental_cutoff = datetime.now() - timedelta(days=fetch_days)` (`SOCIAL_DISCOVERY_FETCH_DAYS = 30`) when the cache is cold, or `cache_timestamp - 1h` when warm (`twitter_client.py:306-322`).
- `analyze_network()`'s optional age filter uses `cutoff_date = datetime.now() - timedelta(days=self.max_data_age_days)` (`social_discovery.py:340`).

Two validators starting discovery even minutes apart observe different 30-day windows: tweets posted in between, tweets deleted in between, and — critically — different follower counts (see 3.4). There is no epoch-aligned snapshot boundary anywhere in the fetch path.

### 3.2 API-level result instability (Desearch.ai / RapidAPI)

`DesearchProvider.fetch_user_tweets()` pages through `/twitter/user/posts` (and optionally `/twitter/replies`) with `tweet_limit = MAX_TWEETS_PER_FETCH = 200` per cycle. The provider is an indexing service over X, not X itself: two identical requests can return slightly different tweet sets due to indexing lag, pagination cursor behavior, eventual consistency across their backend shards, and rate-limit-induced partial results. For prolific accounts, the 200-tweet cap means the returned *sample* of the 30-day window depends on exactly what the provider had indexed at call time.

### 3.3 Per-validator cache state (path dependence of inputs)

The tweet cache (`bitcast/validator/utils/twitter_cache`) stores all tweets indefinitely and drives *incremental* fetching: a validator that has run before fetches only tweets newer than its own `cache_timestamp`, and `skip_if_cache_fresh=True` (used throughout discovery, freshness window ~24h; `SOCIAL_DISCOVERY_CACHE_HOURS = 36` governs the related freshness constant) skips the API entirely if its local cache is recent. Consequences:

- A validator's effective input is `(its historical fetch results) ∪ (incremental deltas)`. Two validators with different operational histories have permanently different tweet corpora, including **deleted tweets**: `_post_process_tweets()` keeps deleted tweets in cache and filters them from the returned view, but a validator that never saw a tweet before deletion simply doesn't have it.
- Mid-iteration, `analyze_network` calls with `skip_if_cache_fresh=True` mean iterations 2..N mostly reuse iteration 1's fetches — good for intra-run stability on one machine, but it freezes *that machine's* particular snapshot, not a shared one.

### 3.4 Live follower counts scale every score

`total_pool_followers = Σ followers_count` over all pool members (`social_discovery.py:611-614`) multiplies every normalized PageRank score. Follower counts change continuously, so even with an identical graph and identical PageRank vector, two validators fetching `user_info` an hour apart produce different absolute scores. Because final scores are rounded to 2 decimals and used for rank ordering downstream, this alone breaks byte-identical maps (though it preserves *relative* order within one validator's run — the problem is the account-level `user_info` differs per account, not by a uniform factor, since each account's info is fetched at a different time).

### 3.5 Concurrency and error non-determinism

- Fetching and relevance checking run on a `ThreadPoolExecutor` with `SOCIAL_DISCOVERY_MAX_WORKERS = 10` and `as_completed()` iteration. Ordering doesn't matter for the graph (edges are keyed by user pairs), but **transient failures do**: `_fetch_tweets_safe()` swallows exceptions and returns an empty tweet list plus `followers_count: 0`; `_check_relevance_safe()` returns `is_relevant=False` on error. A 429 or timeout on one validator silently removes an account (and its entire out-edge set) from that validator's graph.
- its-ai batch scoring "fails open": `ITS_AI_MAX_RETRIES = 3`, but after retries a failed batch means those accounts get *no* AI dampening (`ai_detection.py` failure policy), so one validator may dampen an account that another does not — changing that node's entire outgoing transition row via the `AI_SINK_NODE` mechanism (`_apply_ai_sink()`, sink weight `W × ai/(1−ai)`, capped by `AI_SCORE_CAP = 0.75`).

### 3.6 What is already deterministic (evidence the team knows this problem)

Several mitigations already exist and are explicitly commented as consensus measures:

- AI-check tweet sampling is seeded by `hash(cycle_bucket + username + tweet_id)` where the bucket is the **bi-weekly discovery cycle**, not the day: *"the seed — and therefore the sampled tweets and resulting scores — is identical for all validators running a pool's discovery anywhere within the same cycle"* (`ai_detection.current_date_bucket()`; `DISCOVERY_CYCLE_DAYS` comment: "consensus determinism").
- AI scores are bucketized to `AI_SCORE_BUCKET = 0.2` bands to "absorb API jitter" (`bucketize()`).
- `_select_ai_check_candidates()` tie-breaks by username "so every validator selects the identical set **from the same graph**" (`social_discovery.py:115-121`) — the parenthetical is the tell: determinism holds *conditional on the same graph*, which is exactly what validators don't have.

These are the right instincts, applied at the leaves. The trunk — the tweet corpus itself — remains per-validator.

---

## 4. The Amplification Mechanism: Three Nested Feedback Loops

Small input noise would be tolerable if the pipeline were a single pass: two validators would get graphs differing by a few edges and PageRank vectors differing by a small perturbation (PageRank is Lipschitz-continuous in edge weights for fixed topology). The pipeline is not a single pass. It contains three levels of **discrete, chaotic** feedback where continuous noise is converted into set-membership flips that then redirect subsequent data collection.

### Loop 1: Intra-stage seed re-selection (the core amplifier)

Both Stage 1 and Stage 2 iterate:

```
seeds → fetch tweets(seeds) → graph → PageRank → top-N by score → seeds'
```

Stage 1 (`recursive_discovery.py:386-425`): up to `CORE_MAX_ITERATIONS = 10` rounds, seeds = top `core_max_seed_accounts` (default 100) by score, stopping when Jaccard(top_prev, top_curr) ≥ `CORE_CONVERGENCE_THRESHOLD = 0.95`.
Stage 2 (`recursive_discovery.py:454-525`): up to 3 rounds (`max_discovery_iterations`), seeds = top `extended_max_seed_accounts` (default 300), threshold 0.90.

The top-N cut is a **hard discontinuity**. An account at rank 99 vs. 101 is not "slightly different" — it either becomes a seed (its entire 30-day tweet history is fetched, injecting ~dozens of new edges and potentially new nodes) or it doesn't. One flipped boundary account changes the *next iteration's input data*, not just its output score. This is the textbook signature of sensitive dependence: noise → rank perturbation near the cut → different fetch set → structurally different graph → larger rank perturbation.

Convergence detection compounds this: validator A may hit 0.95 Jaccard at iteration 3 and stop; validator B at 0.96 stability continues to iteration 4 with different seeds. They don't just get different graphs — they run **different numbers of iterations** over them.

### Loop 2: Cross-stage coupling via personalized PageRank

Stage 1's output (`core_accounts`) becomes Stage 2's **teleport distribution**: `personalization = {node: 1.0 if node in restart_nodes else 0.0}` (`social_discovery.py:583`). Personalized PageRank concentrates stationary mass around restart nodes, so a divergent core set from Stage 1 systematically re-weights the *entire* Stage 2 ranking, not just the divergent members. The AI-sink dangling redistribution also routes through this same personalization vector (`social_discovery.py:593-598`), so core-set divergence additionally changes where leaked AI influence is returned. Binary relevance filters (`check_user_relevance` with `min_tweets` ≥ threshold, `min_interaction_weight` ≥ 2/1) add more discontinuities: an account with exactly `core_min_tweets = 5` keyword tweets in one validator's corpus and 4 in another's flips wholesale.

### Loop 3: Cross-cycle path dependence

`_get_seed_accounts()` (`recursive_discovery.py:221-260`) seeds each bi-weekly run from the **top accounts of the previous social map** (falling back to `initial_accounts` only when no map exists). On the reference validator this is fine — its history is a single lineage. But it means independent validators don't just diverge within a cycle; their divergence is *persisted and fed forward*. Two validators that ran discovery independently for three months would have social maps whose common ancestor is months old, with drift compounding every 14 days. (Today, standard-mode validators sidestep this by downloading maps, so a downloaded map would actually seed a hypothetical local run — a mixed blessing discussed in §7.2.)

### Why "hundreds of API calls" matters quantitatively

Per Stage-1 iteration: fetch ~100 seeds + relevance-check every discovered account (typically several hundred candidates → hundreds of `check_user_relevance` calls, each potentially a tweet fetch). Stage 1 can run 10 iterations; Stage 2 fetches up to 300 seeds × 3 iterations plus relevance checks, plus its-ai batches. A full pool cycle is plausibly 1,000–5,000 upstream API interactions. If each call has even a ~1% chance of returning a materially different result across validators (timing, pagination, transient failure), the probability that two validators see *identical* aggregate input is essentially zero. The system's own convergence metric confirms the sensitivity: the pipeline considers a run "converged" at 90–95% Jaccard — i.e., **even sequential iterations on the same machine with warm caches disagree on 5–10% of membership**. Cross-validator disagreement can only be worse.

### What the stability tooling does and doesn't measure

`stability/` (`StabilityAnalyzer`, `metrics.py`, `grid_search.py`) quantifies **temporal** stability: it runs production two-stage discovery, fetches `EXTENDED_FETCH_DAYS = 60` of history for the top `TOP_N_ACCOUNTS = 250`, splits into `NUM_WINDOWS = 4` × `WINDOW_DAYS = 15` windows, and scores adjacent-window similarity as a weighted composite (`WEIGHTS`: rank_correlation 0.25, kcore_stability 0.20, top_n_jaccard 0.20, edge_stability 0.15, density_stability 0.10, weight_stability 0.10), with interpretation bands at 0.75 (STABLE) and 0.55 (MODERATE). `GridSearchRunner` sweeps `min_interaction_weight ∈ {2,4}`, `min_tweets ∈ {5,10}`, `max_seed_accounts ∈ {100,200}` etc. to find parameters where the discovered core is a persistent structure rather than noise.

This is valuable — a temporally stable core is a *precondition* for cross-validator agreement (if the true network churns 40% per window, no amount of engineering makes independent snapshots agree). But note the gap: **there is no tooling that runs the same discovery twice concurrently (different cache dirs, different API keys) and measures replica divergence.** Temporal stability is a proxy; replica stability is the actual decentralization requirement. §8 Phase 0 proposes adding exactly this measurement, largely by reusing `calculate_cross_window_stability()`-style metrics on replica pairs instead of window pairs.

---

## 5. Current Architecture: Three Modes, One Brain

`VALIDATOR_MODE` (`config.py:185-187`) selects the forward implementation at import time (`neurons/validator.py:18-21`):

| Mode | Forward | What it computes locally | What it trusts remotely |
|---|---|---|---|
| `discovery` | `forward.py` → `DiscoveryManager.get_instance().maybe_start()` | Everything: bi-weekly two-stage discovery, connection scans, reward engine every `SCORING_INTERVAL_STEPS = 120` steps (20 min) | Desearch/its-ai APIs only |
| `standard` | `forward.py`, download branch | Connection scans + reward engine; **not** discovery | Reference validator's social map, downloaded every `SOCIAL_MAP_DOWNLOAD_INTERVAL = 4320` steps (12 h) via `SocialMapClient.download_social_map()` → `GET {REFERENCE_VALIDATOR_ENDPOINT}/social-map/{pool}`; also at startup (`check_and_download_social_maps()`) |
| `weight_copy` (**default**) | `wc_forward.py` | Nothing. Every 360 steps (60 min), `WeightCopyClient.fetch_weights()` → `GET {REFERENCE_VALIDATOR_ENDPOINT}/weights`, then `self.scores = scores.copy()` | The reference validator's entire scoring output |

The reference validator (`REFERENCE_VALIDATOR_URL`, default `http://44.241.197.212`, port 8094) runs discovery mode, publishes maps upstream (`social_map_publisher.publish_social_map()` → `X_SOCIAL_MAP_ENDPOINT` when `ENABLE_DATA_PUBLISH`), and serves the HTTP API.

### Degrees of centralization

This is really **two** distinct centralizations stacked:

1. **Social-map centralization** (standard mode): validators independently run the reward engine but on identical map inputs. They still fetch tweets/engagement themselves and run their own LLM evaluations, so their weights are *mostly* independent — the map is a shared parameter, not a shared answer. This is comparable to subnets sharing a dataset or model checkpoint; moderately defensible.
2. **Full weight centralization** (weight_copy mode, the default): validators exercise zero independent judgment. `wc_forward.py` even documents the failure posture: on API failure, "continues with existing scores." Notably there is **no signature verification, no TLS (plain http), and no sanity-bounds checking** on the fetched weights beyond an array-length match (`wc_forward.py:44-50`). A compromised or malicious reference endpoint could set arbitrary weights for every copying validator in the subnet within an hour.

### Failure and trust profile

- **Single point of failure:** if `44.241.197.212` dies mid-cycle, weight-copy validators freeze on stale scores indefinitely; standard validators keep using their last-downloaded map (fine for up to ~14 days, then discovery goes stale everywhere).
- **Single point of trust:** the reference operator can bias discovery (seed choices, timing, selective retries) invisibly. Nothing published lets a third party verify the map was honestly computed — metadata records the params and hotkey (`{timestamp}_metadata.json` includes `validator_hotkey`, core/extended params, `v2_params`), but not the inputs.
- **Chain-level optics:** all validators submitting near-identical weight vectors makes the subnet's Yuma consensus degenerate (median of N copies of one vector = that vector). vtrust looks perfect precisely because nobody is checking anything. This is the situation Bittensor's commit-reveal (CR3) and bond-penalty mechanics target for *chain-observed* weight copying; copying via an off-chain API sidesteps detection but not the underlying critique — the subnet's validation adds no redundancy.

---

## 6. What Yuma Consensus Actually Requires

Yuma Consensus does **not** require validators to agree exactly. For each miner *j*, the chain computes a stake-weighted aggregate of validator weights `W_ij` with these properties relevant here:

1. **Stake-weighted median / clipping:** the consensus weight for a miner is (approximately) the largest weight supported by a κ (≥ 0.5 stake) majority; individual validator weights above consensus are clipped down to it. An outlier validator cannot pull a miner's emission up; it can only marginally influence the median in proportion to its stake.
2. **Validator trust (vtrust) and dividends:** a validator's dividends derive from bonds `B_ij`, which accrue in proportion to how well its weights *anticipate/match* consensus. Divergence is penalized smoothly (reduced dividends), not catastrophically — a validator whose map disagrees on, say, the tail 10% of accounts loses a small dividend fraction, it doesn't get slashed.
3. **Bond EMA smoothing:** bonds are an exponential moving average across epochs, so *transient* divergence (one bad cycle, one API outage) is averaged out. Only persistent, large divergence meaningfully hurts a validator.

Implication: the engineering target is not `map_A == map_B` but roughly `weights_A ≈ weights_B` at the miner level, within the tolerance Yuma's median clipping absorbs. Two things soften the map-divergence → weight-divergence link:

- **Aggregation:** miner weight is a sum over many tweets × many engagers × a 7-day `EMISSIONS_PERIOD`, then smoothed by `REWARD_SMOOTHING_EXPONENT = 0.65` and the validator-side score EMA (`self.update_scores`). Rank noise in the map's tail partially averages out.
- **Head stability:** divergence concentrates in the low-score tail (boundary accounts near the top-N cuts and threshold filters). High-influence accounts — which dominate `Σ influence × engagement` — are precisely the ones both validators agree on (that's what 90–95% Jaccard convergence means: the head is stable, the tail churns).

But two things sharpen it:

- **Membership cliffs downstream:** an account present in map A but absent from map B contributes `influence × engagement_weight` on one validator and **zero** on the other (modulo `STALE_INFLUENCE_DECAY = 0.5` easing for mid-brief drops). A miner whose engagement comes mostly from tail accounts sees large relative reward differences across validators.
- **`min_influence_score` coupling:** `ScoreCalculator` derives its floor from the map's minimum, so tail composition shifts every tweet's baseline slightly.

So the honest answer to "would Yuma tolerate independent discovery today?" is: **probably yes for aggregate emissions of established miners, with measurable dividend loss for validators during high-churn cycles, and materially unfair variance for small miners whose scores ride on tail accounts.** That must be measured, not assumed — hence Phase 0 below.

---

## 7. Solution Space

### 7.1 Option A — Accept approximate consensus (independent discovery, no coordination)

Flip all validators to `discovery` mode; let Yuma's median absorb the divergence.

*Improvements that shrink divergence cheaply (worth doing under any option):*
- Anchor all fetch windows to the discovery cycle boundary, not `datetime.now()`: fetch tweets in `[cycle_start − 30d, cycle_start)` and filter by `created_at`, exactly as `stability/analyzer._filter_tweets_to_window()` already does for windows. This removes §3.1 entirely and turns Desearch into a query over a *fixed* time range.
- Score by **rank, tier, or bucketed score** instead of raw `PR × followers/1000`. Bucketing scores (the `AI_SCORE_BUCKET` trick, applied to influence) collapses small perturbations; using rank tiers (e.g., top-50 / 51–150 / 151–400 weights) collapses them further.
- Replace hard top-N seed cuts with **hysteresis** (an account must fall below rank N+k to lose seed status, exceed N−k to gain it) or seed on `score ≥ θ` with θ set below the boundary noise band. This directly attacks Loop 1.
- Fix the fail-silent handlers: a fetch failure should trigger bounded retry and, if persistent, *exclude the account deterministically for everyone* is impossible — but at minimum, log-and-retry rather than silently zeroing (`_fetch_tweets_safe` returning `followers_count: 0` also corrupts `total_pool_followers`).
- Freeze `followers_count` per cycle (fetch once at cycle start, reuse) so pool difficulty is a per-cycle constant, not a per-call sample.

*Trade-offs:* n× API cost (every validator pays for Desearch + its-ai quota — at current scale maybe the binding constraint), residual divergence persists (Loops 1–3 are dampened, not eliminated), and cross-cycle seeding (Loop 3) still forks lineages — validators would need to seed from a *canonical* prior map (e.g., the highest-stake validator's published map, or an on-chain commitment) or from `initial_accounts` every cycle. No trust required, maximal decentralization, and it's the only option where the subnet's advertised validator count is honest work.

### 7.2 Option B — Shared data snapshot + deterministic recomputation ★

Split discovery into **fetch** (non-deterministic, expensive) and **compute** (deterministic, cheap). One party — per cycle — fetches and publishes the *raw input snapshot*: the tweet corpus (or the derived edge lists + per-account keyword-match counts + follower counts + its-ai raw scores), content-addressed (hash committed on-chain or in the map metadata). Every validator then runs the graph construction + two-stage PageRank **locally from the snapshot**. Because everything from `Step 3: Build interaction network` onward in `analyze_network()` is already deterministic given fixed inputs (sorted usernames, seeded sampling, tie-breaks by username all exist), validators reproduce the map bit-for-bit and can *verify* rather than trust.

Concretely feasible with modest changes:
- `analyze_network()` already accepts `twitter_client` injection and `ai_scores` injection ("injectable for tests/determinism" — the hook exists at `social_discovery.py:260`). A `SnapshotTwitterClient` that serves from a published corpus file is a small class; the stability module's `_build_window_network()` proves the pattern of computing production-compatible PageRank from pre-fetched tweets.
- Snapshot size is manageable: the adjacency serialization already demonstrates ~700–1000× compaction for sparse graphs; a corpus of ~1–2k accounts × 30 days of posts (capped at 200/account) is tens of MB compressed.
- Divergence collapses to a single question: "is the snapshot honest?" — which is *auditable* (any validator can spot-check snapshot tweets against the live API: does tweet X exist, does account Y really have those followers) even though it isn't *reproducible*. Spot-check + challenge is far more tractable than full recomputation, because verifying N sampled facts is cheap while fabricating a snapshot that passes random audits is hard.

*Trade-offs:* fetching is still centralized (one snapshot producer per cycle — but see 7.3 for rotating it); a malicious snapshotter can still bias by *omission* (leaving out tweets is harder to catch than inventing them — mitigate by letting anyone submit "missing tweet" challenges); storage/distribution infra needed (the existing `SocialMapClient` HTTP pattern extends naturally, or IPFS/R2 with the hash in metadata). This is the highest leverage-to-effort option: it converts "trust me" into "check me" without touching API economics.

### 7.3 Option C — Rotating discovery committee

Keep one (or few) discovery executors per cycle, but make *who* rotates and *what they did* verifiable:
- Deterministically select the cycle's discovery validator(s) from the metagraph, e.g., hash(cycle_number ‖ block_hash at cycle start) mod eligible validators (stake-weighted, min-stake gated). The bi-weekly cadence and per-pool `date_offset` already exist as natural rotation slots — different pools could even be assigned to different validators in the same cycle.
- The selected validator publishes snapshot + map (Option B format); others verify-and-adopt, and can vote/flag on-chain (or simply refuse to adopt and fall back to the previous map) if verification fails.

*Trade-offs:* liveness handling needed (selected validator offline → fallback to next in hash order, with a timeout — the 12h `SOCIAL_MAP_DOWNLOAD_INTERVAL` staleness machinery is a starting point); still 1-of-N trust *per cycle*, but the attacker must control the specific rotating slot, and every output is audited by peers. Composes perfectly with Option B — B defines the artifact, C decentralizes who produces it. This kills the permanent single point of failure/trust at low API cost (aggregate API spend unchanged: still one fetcher per cycle).

### 7.4 Option D — Partitioned / collaborative fetching

Shard the fetch workload across validators: validator k fetches accounts where `H(username) mod N = k`, publishes signed per-account result bundles (tweets, follower count, keyword counts), and everyone assembles the union corpus and recomputes deterministically. Disagreements on an account (two validators fetched overlapping shards for redundancy, results differ) resolve by deterministic rule (union of tweet IDs; median follower count).

*Trade-offs:* the most decentralized data layer short of Option A, splits API cost instead of multiplying it (with r-fold redundancy costing r× one validator's share), and produces per-account signed provenance. But it's the most coordination-heavy: membership churn (validators joining/leaving mid-fetch), straggler/liveness protocol, cross-validator result exchange infra, and Sybil considerations (a validator lying about its shard poisons specific accounts — redundancy r ≥ 2–3 with cross-checking needed). Realistic as a later evolution of B+C once the snapshot format and verification tooling exist, not as a first step.

### 7.5 Option E — Algorithmic noise reduction (make the pipeline contraction-dominant)

Attack amplification rather than input noise. Beyond the cheap items in 7.1:
- **Ensemble/bootstrap ranking:** run PageRank over R resampled subgraphs (deterministically seeded) and rank by median score — rank flips near boundaries require consistent evidence, not one noisy edge.
- **Overlapping-window edges:** build edges from 2–3 overlapping 30-day windows (e.g., cycle-aligned days 0–30, 15–45) and require an edge to appear in ≥2, so single-window API blips don't create/destroy edges. The stability module's windowing code is directly reusable.
- **Soft membership:** replace the binary in-map/not-in-map cliff downstream with a score-proportional ramp near the threshold (generalizing `STALE_INFLUENCE_DECAY`), so map-tail disagreement moves rewards continuously instead of discretely.
- **Freeze the iteration count:** always run exactly K stage-2 iterations rather than stopping on a convergence test (removes the "different validators stop at different iterations" branch — the Jaccard check becomes telemetry, not control flow).

*Trade-offs:* none of this achieves reproducibility; it only widens Yuma's effective tolerance band. But it reduces divergence under *every* other option too (and improves temporal stability, which the grid search directly optimizes). Cheap, incremental, measurable with existing stability tooling. Should be pursued regardless.

### 7.6 Option F — Verifiable computation (zk / TEE / optimistic)

- **zkVM proofs** of the PageRank computation: only meaningful given a committed input (i.e., requires Option B's snapshot anyway), and proving ~1000 power iterations over a multi-thousand-node weighted digraph plus all the Python-level filtering logic in a zkVM is heavy engineering for marginal benefit — once the snapshot is public, *anyone can just re-run the deterministic compute in seconds*, which is a strictly cheaper "proof."
- **TEE (SGX/Nitro) attestation of the fetcher:** genuinely interesting for the part B can't fix — proving the *fetch* was performed by the canonical code against the real API without selective omission. An attested fetcher that logs request/response transcripts would close the omission-bias hole in B/C. Trade-offs: infra complexity, TEE trust assumptions, and Desearch's TLS session contents still aren't independently notarizable without something like TLS-notary.
- **Optimistic verification:** publish snapshot+map, allow a challenge window where any validator can post a fraud proof ("this tweet in the snapshot doesn't exist" / "this account's real follower count is 10× off"), with stake at risk. Lightweight, fits Bittensor's social-slashing reality better than zk.

*Verdict:* zk is overkill (deterministic recompute subsumes it); optimistic challenges are the pragmatic sibling and cheap to add on top of B/C; TEE is a v3 idea if snapshot-omission attacks ever become real rather than theoretical.

### Comparison

| Option | Trust model | API cost | Divergence | Eng. effort | Key risk |
|---|---|---|---|---|---|
| A. Independent + tolerant | None (trustless) | ×N validators | Bounded, nonzero | Medium | Quota cost; tail-miner unfairness |
| B. Snapshot + determinism ★ | 1 fetcher/cycle, auditable | ×1 | Zero (given snapshot) | Medium | Omission bias in snapshot |
| C. Rotating committee | 1-of-N per cycle, audited | ×1 | Zero | Medium (needs B) | Liveness of selected validator |
| D. Partitioned fetch | Each shard 1-of-r | ×r redundancy share | Zero | High | Coordination complexity |
| E. Noise reduction | unchanged | unchanged | Reduced | Low | Doesn't remove trust issue |
| F. Verifiable compute | Strongest | ×1 + proving | Zero | Very high | Overkill; TEE/zk complexity |

---

## 8. Recommended Phased Path

**Phase 0 — Measure (1–2 weeks, no protocol change).**
Build a *replica divergence* harness: run `two_stage_discovery()` twice concurrently with isolated cache roots (`CACHE_ROOT` is already a config constant) and separate API keys, then diff the outputs using the existing pairwise metrics (`_calculate_pairwise_stability`: top-N Jaccard, Spearman rank correlation, edge stability). Critically, also propagate both maps through `ScoreCalculator` → reward engine on a fixed tweet set and measure **miner-weight divergence** (L1 distance, max per-miner relative delta). This tells you whether the problem is a 2% tail wobble Yuma would shrug at, or a 20% membership fork. Everything after this is sized by that number. (This reuses ~80% of `stability/`; it's the missing "replica" axis noted in §4.)

**Phase 1 — Determinism hygiene (low-risk PRs, immediately useful).**
Cycle-anchored fetch windows; per-cycle frozen follower counts; fixed iteration counts; retry-not-swallow on fetch errors; canonical prior-map seeding. Each independently shrinks divergence and none changes the trust model. Rerun Phase 0 after; expect a large drop.

**Phase 2 — Publish the snapshot; decentralize verification (the pivotal step).**
Define the snapshot artifact (edge list + per-account features + its-ai raw scores + corpus hash), have the discovery validator publish it alongside the map (extend `social_map_publisher` / the reference API), and ship a `verify_social_map` mode: standard-mode validators recompute the map from the snapshot via an injected `SnapshotTwitterClient` and **refuse to adopt maps that don't reproduce** (falling back to the previous verified map). At this point the reference validator can no longer publish an un-audited map; centralization of *computation* is gone, centralization of *fetching* remains but is spot-checkable. Add random live-API spot checks of snapshot facts + a "missing data" reporting channel (optimistic-verification lite).

**Phase 3 — Rotate the fetcher (removes the fixed single point).**
Deterministic per-cycle, per-pool fetcher selection from the metagraph with hash-order fallback on liveness timeout. Retire the hardcoded `REFERENCE_VALIDATOR_URL` for discovery purposes. Weight-copy mode should simultaneously be deprecated or stake-capped: with standard mode now cheap-and-verified, the copy tier's only remaining justification is operators who won't pay any API costs — and `wc_client`'s unauthenticated plain-HTTP weight fetch is the scariest single line of trust in the system regardless of discovery decentralization.

**Phase 4 (optional, demand-driven) — Partitioned fetching / TEE attestation.**
Only if the subnet grows to where per-cycle fetch cost or snapshot-omission attacks are real concerns.

This ordering front-loads risk reduction per unit effort: Phase 1 is pure hygiene, Phase 2 converts trust into verification without changing API economics, Phase 3 removes the fixed operator. Full Option A (everyone fetches independently) is deliberately *not* the destination — it multiplies API spend for less verifiability than B+C provide.

---

## 9. Is This Worth Fixing Now?

Arguments for **later**:
- The subnet is evidently early (reference date Nov 2025, `__version__ = 1.5.1`, active algorithm churn: AI dampening v2, prompt v4, affiliate boosting all landed recently). Iterating on *what the map should be* is easier with one canonical brain; decentralizing a moving target means re-decentralizing every change.
- The practical harm today is low: honest-operator assumption currently holds, miners are paid, and the map itself is published and inspectable after the fact.
- Desearch/its-ai quota costs make "everyone fetches" genuinely expensive; premature Option A could price out small validators, *reducing* effective decentralization.

Arguments for **now**:
- **The default mode is weight_copy over unauthenticated HTTP to a hardcoded IP.** That's not a decentralization aesthetic problem; it's an operational security problem (single compromise → arbitrary subnet weights for an hour+ across all copying validators) and a credibility problem for a Bittensor subnet, where "validators do no independent work" is the canonical criticism and the thing dTAO-era subnet evaluation punishes.
- Divergence compounds via Loop 3: the longer all lineage flows through one validator's history, the harder cold-starting independent validators becomes (their from-`initial_accounts` maps will differ maximally from the incumbent lineage).
- Phase 0–2 costs are modest and mostly reuse existing code (`stability/`, injection hooks, publisher/client infra), and Phase 1 improvements (temporal stability, error handling) pay for themselves even in the centralized world.

**Recommendation:** do Phases 0–1 immediately (they are cheap, risk-free, and informative), commit to Phase 2 as the near-term architectural goal — it's the step that changes the trust story from "one validator computes, everyone trusts" to "one validator fetches, everyone verifies" — and defer Phases 3–4 until the map algorithm stabilizes and validator count/stake distribution justifies them. Independently of the phasing, harden or sunset `weight_copy` mode (signed responses at minimum) — it is the weakest link today and none of the discovery work fixes it.

---

## Appendix A — Key Parameters Referenced

| Parameter | Value | Location |
|---|---|---|
| `VALIDATOR_MODE` default | `weight_copy` | `utils/config.py:185` |
| `REFERENCE_VALIDATOR_URL` | `http://44.241.197.212` (`:8094`) | `utils/config.py:192-193` |
| `DISCOVERY_CYCLE_DAYS` / reference date | 14 / 2025-11-09 | `social_discovery.py:39-43` |
| `SOCIAL_DISCOVERY_FETCH_DAYS` | 30 | `utils/config.py:84` |
| `MAX_TWEETS_PER_FETCH` | 200 | `utils/config.py:86` |
| `PAGERANK_ALPHA` | 0.85 (max_iter 1000, nx default tol 1e-6) | `utils/config.py:94`, `social_discovery.py:600` |
| PageRank weights (mention/retweet/quote) | 2.0 / 1.0 / 3.0 (README's 1.0/2.0/1.5 is outdated) | `utils/config.py:89-91` |
| Stage 1: max iter / convergence / seeds | 10 / 0.95 Jaccard / `core_max_seed_accounts`=100 | `recursive_discovery.py:369-370,321` |
| Stage 1 filters | `core_min_interaction_weight`=2, `core_min_tweets`=5 | `recursive_discovery.py:319-320` |
| Stage 2: max iter / convergence / seeds | 3 / 0.90 Jaccard / `extended_max_seed_accounts`=300 | `recursive_discovery.py:323-327` |
| `SOCIAL_DISCOVERY_MAX_WORKERS` | 10 | `utils/config.py:105` |
| `SOCIAL_MAP_DOWNLOAD_INTERVAL` | 4320 steps ≈ 12 h | `utils/config.py:158` |
| Weight-copy poll interval | 360 steps ≈ 60 min | `weight_copy/wc_forward.py:22` |
| AI dampening: bucket / cap / sample | `AI_SCORE_BUCKET`=0.2, `AI_SCORE_CAP`=0.75, `AI_SAMPLE_SIZE`=4 | `utils/config.py:123-136` |
| `STALE_INFLUENCE_DECAY` | 0.5 | `utils/config.py:93` |
| Stability windows | 4 × 15 d over 60 d, top 250 accounts | `stability/config.py:25-28` |
| Stability composite weights | rank 0.25, kcore 0.20, jaccard 0.20, edge 0.15, density 0.10, weight 0.10 | `stability/metrics.py:175-182` |
| Score formula | `round(PR_norm × Σfollowers/1000, 2)` | `social_discovery.py:617-626` |
