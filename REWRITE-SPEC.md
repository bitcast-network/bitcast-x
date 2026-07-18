# bitcast-x Rewrite Specification

**Status:** Investigation deliverable — for review. No rewrite work has started.
**Author:** Claude (investigation task), 2026-07-18
**Reviewer:** Will

---

## 1. Executive Summary

### Current state

bitcast-x is the Bittensor SN93 subnet mechanism for X/Twitter: validators discover social influence networks via PageRank, track miner account connections through on-chain-style tweet tags, score tweet engagement, LLM-evaluate tweets against campaign briefs, and distribute alpha-token emissions proportionally. The codebase is **~21.8k LOC of Python source across 112 files** (plus ~11.8k LOC of tests) and is a fork of the bittensor-subnet-template that grew by accretion.

The product logic is sound and battle-tested (583 passing tests encode the numeric expectations), but the code around it has significant drag:

- **Everything is synchronous.** All Twitter, LLM, AI-detection, pricing, and briefs HTTP goes through blocking `requests`, with concurrency faked via `ThreadPoolExecutor` and `time.sleep`, called from inside async code paths.
- **Heavy duplication.** The two LLM clients are ~90% copy-paste of each other; four separate retry implementations; the social-map "latest file" glob is reimplemented 4× with *inconsistent* exclusion rules; the USD→weight conversion (consensus-critical) is computed independently in 4 places; pool-parameter defaults are hardcoded in 3 places.
- **Confirmed dead code.** A full template miner with no miner entrypoint, `bitcast/api/get_query_axons.py`, `tests/helpers.py` (imports APIs removed from bittensor 10), `tweet_scoring/engagement_analyzer.py` (superseded, kept alive only by its own 366-LOC test), an unused `Brief` dataclass, unused ABCs, and a `models/` package for one unused dataclass.
- **Two parallel config systems** (template argparse + env-var module) with a cross-layer import (`base/validator.py` imports `MECHID` from the app config), ~30 log lines emitted at import time, and consensus constants hardcoded outside config (AI sink formula constants, discovery reference date, bonus multipliers, `CONNECTION_TWEET_IDS`).
- **Documentation actively lies about consensus math.** The README documents the wrong PageRank weights; docstrings claim baseline factor 0.5 (it is 2) and a 10% performance-bonus cap (it is 20%); the README's "5% managed fee" and "flat $50 referral bonus" do not exist in code. Anyone treating docs as spec would rewrite the mechanism incorrectly.
- **No dict discipline.** Tweets, briefs, engagements, connections, social maps, and snapshots are all bare `Dict[str, Any]` passed by convention; the only validated model (`Brief`) is not on the live path.
- **Infrastructure gaps.** CI runs only Semgrep — the 583 tests never run in CI. The consensus-critical vendored `weight_utils.py` has zero tests. On-chain `version_key` is 0 because `__version__ = "0.0.0"` while the app separately claims 1.5.1. State (SQLite DB, caches, social maps, scored-tweet JSON) lives inside the source tree.

### Why rewrite (not refactor)

The same 5 design principles have been proven on 4 prior org rewrites (bitcast-v2: 19,517→4,460 LOC; bitcast-api; creator-portal-v3; stitch3-v2). The pain here is structural — sync-everywhere transport, template scaffolding, 4-tree package layouts, dict-typed contracts — and incremental refactoring would touch nearly every file anyway. A ground-up rewrite that ports **business logic only** (not structure) is the cheaper path, with the bitcast-v2 precedent for keeping consensus math bit-identical.

### Target architecture (summary)

A single `bitcast/` package with one config module (pydantic-settings), typed pydantic models as the data contract, an async-first client layer (httpx) with one shared retry helper, the reward pipeline kept under the existing DI pattern but flattened from 24 files/4 sub-trees to ~8 rich modules, the three tweet modules (scoring/filtering/bonus) merged into one `scoring/` package passing data in memory instead of via disk JSON round-trips, and a mockable chain boundary so the full test suite runs with no Bittensor network. Details in §6.

---

## 2. Reference Codebase

Repo: `bitcast-network/bitcast-x`, branch `main` at `c1c92b2`. All analysis in this spec refers to this revision.

| Module | Path | Files | LOC |
|---|---|---|---|
| social_discovery | `bitcast/validator/social_discovery/` | 19 | 4,812 |
| clients | `bitcast/validator/clients/` | 9 | 3,728 |
| tweet_scoring | `bitcast/validator/tweet_scoring/` | 9 | 3,072 |
| reward_engine | `bitcast/validator/reward_engine/` | 24 | 2,893 |
| utils (validator) | `bitcast/validator/utils/` | 13 | 1,724 |
| account_connection | `bitcast/validator/account_connection/` | 10 | 1,639 |
| tweet_filtering | `bitcast/validator/tweet_filtering/` | 4 | 966 |
| base (framework) | `bitcast/base/` | 6 | 966 |
| utils (template) | `bitcast/utils/` | 5 | 466 |
| tweet_bonus | `bitcast/validator/tweet_bonus/` | 3 | 333 |
| api | `bitcast/validator/api/` | 2 | 303 |
| weight_copy | `bitcast/validator/weight_copy/` | 3 | 160 |
| api (template) | `bitcast/api/` | 2 | 129 |
| neurons | `neurons/` | 2 | 97 |
| core | `core/` | 2 | 81 |
| scripts | `scripts/` | 2 py + 4 sh | 190 |
| validator glue | `bitcast/validator/{forward,__init__}.py` | 2 | ~130 |
| **Source total** | | **~112** | **~21,800** |
| tests | `tests/` | 62 | 11,825 |

---

## 3. Design Principles

Non-negotiable; proven on bitcast-v2, bitcast-api, creator-portal-v3, stitch3-v2:

1. **LEAN** — No dead code/cruft. Strip template boilerplate. Every file has a clear purpose or delete it.
2. **CLEAN** — Type hints (Python 3.12+), docstrings on public APIs, custom exceptions not bare except, no commented-out code.
3. **SOLID** — Each module does ONE thing. Keep the reward engine DI pattern. Consolidate interfaces/services/models trees into fewer richer files. Pydantic over dataclasses.
4. **PERFORMANT** — Async-first I/O (Twitter API, LLM, miner queries). `asyncio.gather` for parallel queries. Cache API responses.
5. **MAINTAINABLE** — Config in ONE module (`config.py`). No circular imports. `pyproject.toml` not `requirements.txt`. Tests run without the Bittensor network (mock chain).

These principles are the quality bar. There are **no LOC-reduction targets** (§10).

---

## 4. Scope

**In scope**
- Ground-up rewrite of the validator codebase: all modules in §2, packaging, config, test infrastructure, CI.
- Port of every piece of business logic in §7, with consensus-critical math bit-identical (§9).
- Staging deployment for verification (parallel-run against the current validator).

**Out of scope**
- Production deployment (requires Will's explicit approval — §11).
- Any change to the on-chain mechanism, brief server, ingestion API, or reference-validator API *contracts* (wire formats stay compatible; internals may change).
- New features. Behavior changes are limited to bug fixes explicitly listed in §7.4, each individually flagged.
- The offline `social_discovery/stability/` tuning tool may be ported last or parked (not on the validator hot path).

---

## 5. Current Architecture Analysis

Each module: purpose, size, key business logic, pain points, dependencies, and disposition. File:line references are to the revision in §2.

### 5.1 social_discovery (19 files, 4,812 LOC)

**Purpose:** Bi-weekly PageRank analysis of X interaction networks to produce per-pool "social maps" (account → influence score), plus map download/publish for validators that don't run discovery themselves.

**Key business logic** (consensus-critical unless noted):
- **Edge weights** — from `bitcast/validator/utils/config.py:89-94`: retweet 1.0, mention/tag 2.0, quote 3.0, PageRank α=0.85. ⚠️ **README.md:93-96 documents different (wrong) weights — config is authoritative.**
- **Graph build** (`social_discovery.py:367-434`): per directed pair, `interaction_weights` keeps the **max** weight (PageRank input) while `relationship_scores` keeps the **sum** (cabal-protection matrix). Replies skipped; self-interactions skipped; invalid usernames skipped.
- **Filter pipeline** (order matters): low-seed relaxation when seeds < 20 (forces min weight/tweets to 1), tweet-age filter (fail-safe keep on parse error), keyword/follower/language relevance check, min-interaction-weight cut with **seed accounts always preserved**.
- **PageRank** — `nx.pagerank(weight='weight', alpha=0.85, max_iter=1000)`, default tol 1e-6; personalized restart on core accounts + promoted affiliates; normalized scores; **absolute influence = `round(norm_score × total_pool_followers / 1000, 2)`**.
- **AI out-link dampening (v2)** (`social_discovery.py:109-170`): for account with AI score `ai` (capped at `AI_SCORE_CAP=0.75`) and out-weight `W`, add edge to `__ai_sink__` of weight `W·ai/(1−ai)`; dangling mass redistributed excluding sink. Candidate cap `AI_MAX_ACCOUNTS_CHECKED` (0 = unlimited), deterministic tie-break by username.
- **AI account scoring** (`ai_detection.py`): deterministic sampling — eligible tweets ≥200 chars, sorted by `sha256("{cycle-bucket}:{username}:{tweet_id}")`, take 4; scores from its-ai batched 250/request; mean → bucketized to 0.2 steps; account cache TTL 14 days. The cycle bucket is `(today − 2025-11-09).days // 14` so all validators sample identically within a cycle.
- **Two-stage discovery** (`recursive_discovery.py`): Stage 1 core (strict: min weight 2, min tweets 5, ≤100 seeds, ≤10 iterations, Jaccard convergence 0.95), Stage 2 extended (relaxed: 1/1/300, ≤3 iterations, convergence 0.90, personalized PageRank, AI dampening applied here).
- **Schedule** (`should_run_discovery_today`): runs when `(today_utc − 2025-11-09).days − pool.date_offset` is ≥0 and divisible by 14 ("every 2nd Sunday", staggered per pool via `date_offset`).
- **Pools** come from `POOLS_API_URL` (`{BITCAST_API_URL}/api/v2/validator/pools`), not a local file; `PoolManager` applies per-field defaults.
- **Artifacts**: `social_maps/{pool}/{YYYY.MM.DD_HH.MM.SS}.json` (metadata + accounts sorted by score, incl. `ai_score`, affiliate fields), `_adjacency.json` (compact v2.0 sparse edges), `_metadata.json`, summary + milestone log. Publish → `{DATA_CLIENT_URL}/api/v1/x-social-map` (gated by `ENABLE_DATA_PUBLISH`); download → `{REFERENCE_VALIDATOR_URL}:8094/social-map/{pool}`.

**Pain points:** two ~450-line god functions (`analyze_network`, `two_stage_discovery`); four divergent copies of the social-map file glob (one missing exclusions); pool defaults duplicated 3×; blocking `PoolManager` constructed fresh at 9+ call sites (network round-trip each time); consensus constants (`DISCOVERY_REFERENCE_DATE`, `AI_SINK_NODE`, core-stage iteration/convergence, `/1000` scale) hardcoded outside config; circular-import hacks (7 in-function imports); README substantially stale; `stability/` reimplements the graph build instead of reusing it; no typed models anywhere.

**Dependencies:** networkx, scipy, numpy, httpx (download only), requests (pools); imports clients, twitter_cache, ai_score_cache, data_publisher, and — across a package boundary — `tweet_scoring.social_map_loader.parse_social_map_filename`. Consumed by forward.py, tweet_scoring, account_connection, startup_checks, reference API.

**Disposition:** **Split + consolidate.** Break the two god functions into staged pipeline functions; merge the three download-side files (`social_map_client`, `social_map_downloader`, `download_social_map`) into one transport module; one canonical map-file reader; pool config as a cached, typed model; all constants into config. `ai_detection.py`, `adjacency_utils.py`, `discovery_manager.py` port nearly as-is. `stability/` ports last, refactored to reuse the shared graph-build.

### 5.2 clients (9 files, 3,728 LOC)

**Purpose:** External API clients — Twitter data (Desearch + RapidAPI providers behind an ABC), LLM brief evaluation (Chutes + OpenRouter), its-ai AI-text detection, and the evaluation prompt registry.

**Key business logic:**
- **Normalized tweet schema** — the load-bearing contract (`twitter_provider.py:65-85`): 18 fields (`tweet_id`, `created_at` in Twitter format, `text`, `author` lowercased, `tagged_accounts`, `retweeted_user/-tweet_id`, `quoted_user/-tweet_id`, `lang`, counts, reply linkage). Both providers map wildly different wire formats (Desearch flat JSON; RapidAPI nested GraphQL with `RT @` regex detection and note-tweet extended text) onto it. Preserve every field mapping.
- **Cache/merge/deletion semantics** (`twitter_client.py:230-434`): 36h freshness short-circuit; incremental cutoff = cache time − 1h; merge by tweet_id; cached tweets missing from 2 consecutive successful fetches are treated as deleted (hidden but kept in cache); cache timestamp only advances on successful non-empty fetch (rate-limit artifact guard). Timeline cache TTL 90 days.
- **LLM evaluation:** Qwen3-32B (Chutes `Qwen/Qwen3-32B`; OpenRouter `qwen/qwen3-32b:nitro`), temperature 0, max_tokens 4096, markdown response parsed by regex (`## Verdict` YES/NO), tweets cropped to 10,000 chars, diskcache keyed on the full prompt string, 7-day sliding TTL. Provider chosen once via `LLM_PROVIDER` (default chutes); **no fallback cascade exists**.
- **Prompts v1–v4** (`prompts.py`): v1 baseline (verbatim-quote evidence, "when in doubt → NO", no negative sponsor coverage); v2 adds ≥80%-about-sponsor rule; v4 = v2 minus the no-negativity rule (neutral sentiment); v3 for unsponsored/conversational briefs. Brief selects via `prompt_version`.
- **its-ai client:** raw `score` 0–1 passed through (thresholding happens in ai_detection); **fail-open** on short text/non-English/quota/network (returns None); only missing key or 401 raises `ItsAiConfigError`.
- Retry policy everywhere: statuses {429, 500, 502, 503, 504}, 3 retries.

**Pain points:** 100% synchronous (`requests` + ThreadPoolExecutor + `time.sleep`); `ChuteClient.py`/`OpenRouterClient.py` are ~90% copy-paste with CamelCase filenames; four distinct retry implementations; RapidAPI provider is 903 LOC mixing transport with GraphQL parsing and its `fetch_tweet_by_id` is an unimplemented stub; several endpoints bypass the shared request helper; base URLs/models/page caps/timeouts hardcoded per file; zero tests for the LLM clients and prompts.

**Dependencies:** requests, diskcache, bittensor logging. Consumed by every domain module.

**Disposition:** **Consolidate + rewrite transport.** One async `request_with_retry` (httpx); one LLM client parameterized by a provider table (kills ~350 duplicated LOC); split RapidAPI parsing from transport; promote the tweet schema to a pydantic model; keep prompts v1–v4 verbatim; keep cache/deletion semantics exactly.

### 5.3 tweet_scoring (9 files, 3,072 LOC)

**Purpose:** Discover tweets from connected accounts, fetch engagements (retweeters/quoters), and compute weighted engagement scores against the social map.

**Key business logic (consensus-critical):**
- **Core formula** (`score_calculator.py:63-153`):
  `total = author_influence × BASELINE_TWEET_SCORE_FACTOR(=2) + Σ_engagers(influence × weight × cabal_scale)`, retweet weight 1.0, quote weight 3.0 (quote **overrides** retweet for the same engager — dict keyed by username gives max 1 contribution per engager per tweet), self-engagement and non-considered accounts excluded, result `round(…, 6)`. ⚠️ Docstrings/README claim factor 0.5 — **the config constant 2 is authoritative**. Mentions are never scored in the engagement path despite `PAGERANK_MENTION_WEIGHT` existing.
- **Cabal protection**: engager contribution scaled by `0.1 + 0.9/relationship_score` when the pairwise relationship score > 0 (bounds contribution to 10–100%).
- **Influence pinning (SUB-110)** (`social_map_loader.py:397-518`): author influence resolved from the social map **active at the tweet's posting time** (map active window = `[map_ts, next_map_ts)`), cached per map; fallback to the pool's minimum considered influence.
- **Multimap windows**: brief windows spanning map regenerations union top-N members of every overlapping map; accounts present only in older maps carry `STALE_INFLUENCE_DECAY = 0.5` × last score.
- **Discovery**: lightweight (search-query built with `until:`+1day exclusivity) and thorough (timeline refresh once per 8h cycle, then zero-API store reads). Engagements: retweeters endpoint + quote search validated by `quoted_tweet_id`; tiered re-fetch intervals 1h/4h/24h by tweet age.
- **TweetStore** — diskcache (not SQLite) at `cache/twitter/tweet_store`, 3GB, permanent accumulation, metrics merged monotonically (max), engagement records keep `first_seen`.
- **Fasttrack**: polls `https://www.stitch3.ai/api/fast-track` (cap 1000 ids) every other forward step; fetches missed tweets by id, processes connection tags in them, idempotent.

**Pain points:** dead `engagement_analyzer.py` (86 LOC + 366-LOC test); name collision with `tweet_filtering/tweet_filter.py`; `TweetStore.query_tweets` scans every cache key per brief; social-map JSON re-read repeatedly across loader functions; four bespoke JSON savers; `score_breakdown` uses cryptic single-letter keys; `NUM_LLM_CHECKS`, `ENGAGEMENT_MAX_WORKERS`, fasttrack URL, cabal coefficients not in config.

**Dependencies:** clients, social_discovery (PoolManager, adjacency), account_connection (TagParser, ConnectionDatabase), diskcache, numpy.

**Disposition:** **Merge with tweet_filtering + tweet_bonus into one `scoring/` package** (§6). Port `score_calculator`, `tweet_store`, `social_map_loader` (time-pinning, decay), `tweet_discovery`, fasttrack logic exactly; delete `engagement_analyzer`; rename the content filter to kill the name collision.

### 5.4 reward_engine (24 files, 2,893 LOC)

**Purpose:** Orchestrates the full reward computation: briefs → evaluation → score matrix → emission targets → normalized weight distribution → referral bonuses, with 7-day snapshot-frozen payouts.

**Key business logic (consensus-critical throughout):**
- **Brief states** (`brief_fetcher.py:66-104`): `scoring` while `start ≤ today ≤ end + 1`; `emission` for the following 7 days (`REWARDS_DELAY_DAYS=1`, `EMISSIONS_PERIOD=7`). Briefs fetched synchronously from `{BITCAST_API_URL}/api/v2/validator/x-briefs`, diskcache fallback on failure, RuntimeError → all-to-burn fallback rewards.
- **Pipeline order** (`orchestrator.py:37-256`): fetch briefs → partition by state → load connections/eligibility → (thorough) refresh timelines → run evaluator on emission briefs → aggregate to UID×brief float64 matrix → emission targets → distribution → referral bonuses (fail-safe wrapped, never affects base rewards).
- **Tweet→brief assignment** (`tweet_brief_assignment.py`): greedy max-payout matching, payout estimated with the same 0.65 smoothing; deterministic sort `(-payout, brief_id, tweet_id)`; one tweet → one brief; per-author `max_tweets` cap; snapshot-committed tweets excluded.
- **Per-tweet math** (`twitter_evaluator.py:694-770`): `smoothed = score^0.65`; `usd_target = (budget/7) × smoothed/Σsmoothed`; `alpha_target = usd/price`; `weight = usd / (price × total_miner_emissions)`. Performance bonus then featured bonus applied **after assignment, before targets**. Unmapped authors → dropped (or `NOCODE_UID=114` only when `SIMULATE_CONNECTIONS`).
- **Snapshots**: first emission run freezes per-tweet USD to `reward_snapshots/{pool}/{brief}_{ts}.json`; subsequent 6 days replay `total_usd/7` per UID from the **oldest** snapshot file (mtime-based selection — fragile, see §9).
- **Distribution** (`reward_distribution_service.py`): per-brief `cap` (default 1.0) column scaling → global `/total` if Σ>1 → row-sum to rewards → **UID 0 absorbs `max(1−Σothers, 0)`** → treasury transfer `min(SUBNET_TREASURY_PERCENTAGE, burn)` from UID 0 to UID 106 (**percentage currently 0 — no-op**).
- **Referrals** (`referral_bonus_service.py` + `utils/referral_rewards.py`): amounts are per-referral **locked DB values** (not flat $50 — the 50.0 is only a legacy column default); fallback formula = 80% log-scaled followers (1k→25k) + 20% log-scaled influence (1→1000), clamped to pool `max_referral_amount` (default $100). Activation sets `payout_date = tomorrow`, once. On payout day both referee and referrer UIDs get their locked USD converted to weight via `usd / (price × total_emissions)`.
- **Token pricing** (`utils/token_pricing.py`): CoinGecko `bitcast`/usd; `total_miner_emissions = 7200 × alpha_out_emission × 0.41 × mech_ratio(netuid 93, fallback 0.15)`; both 10-min TTL cached with retries.
- **DI pattern (KEEP):** `forward.py` constructs the orchestrator with injected aggregator/calculator/distributor/registry; orchestrator defaults each dependency if None. This seam is what makes the engine testable.

⚠️ **Corrections to folklore:** there is **no 5% managed-UID fee** anywhere in the code (UID 114 is only a fallback mapping target), and **no flat $50 referral bonus**. The README/task description overstate both.

**Pain points:** 4-tree layout (interfaces/services/models/utils, 5 files that only re-export); ABCs with single implementations; `QueryBasedEvaluator` and `models/brief.py` dead; `PlatformRegistry` wraps a one-entry dict; ABC signature doesn't even match the real evaluator; sync `requests` for briefs/pricing inside async flow; USD→weight math duplicated 4×; burn UID 0 hardcoded twice; 975-LOC evaluator mixing orchestration with consensus math.

**Disposition:** **Flatten to ~6 richer modules, keep constructor injection, isolate the consensus math** into a pure, heavily-tested `reward_math.py`. Adopt pydantic `Brief` on the live path. Fix snapshot selection determinism (flagged behavior change, §7.4).

### 5.5 utils — validator (13 files, 1,724 LOC)

**Purpose:** Config, caching, publishing, pricing, dates, validation, error helpers, startup checks, run IDs.

**Key business logic:** `config.py` is the de-facto single config module (all env vars + consensus constants — full inventory in §5.13); `DiscoveryCache` diskcache singleton (1GB, 90-day TTL, keys `user_tweets_{u}`/`user_info_{u}`); AI-score cache piggybacking the same store (tweet scores permanent, account scores 14-day TTL); `data_publisher` — hotkey-**signed**, gzip'd async POST (aiohttp) to the ingestion API expecting 202, global singleton (this is the publishing path; **wandb is not used for data publishing**, only run monitoring); `parse_brief_date` (UTC, end-of-day 23:59:59 semantics for brief `end_date` — consensus-relevant); `is_valid_twitter_username`; startup checks (fatal only if a pool has no social map at all).

**Pain points:** import-time logging; env var named `AI_SCORE_TTL_DAYS` feeding a constant named `AI_SCORE_TTL_SECONDS`; cache naming chaos (twitter/discovery/tweet used interchangeably; clearing "twitter" cache also wipes AI scores — no namespace isolation; `scripts/clear_empty_twitter_caches.py` docstring cites a nonexistent script name); deprecated `datetime.utcnow()` mixed with tz-aware and naive datetimes; `error_handling.py` helpers that add little over raising typed exceptions; `MECHID` parsed in two places.

**Disposition:** config → single typed settings module (§6); caches → one package with named, isolated namespaces and a TTL policy table; publisher absorbed as the single publishing funnel; `date_utils`, `twitter_validators`, `referral_rewards`, `token_pricing` port nearly as-is (pricing goes async); `error_handling` shrinks to a custom-exception hierarchy.

### 5.6 account_connection (10 files, 1,639 LOC)

**Purpose:** Discover and persist miner↔X-account connections by scanning replies to a pinned connection tweet for tags, including the referral program plumbing.

**Key business logic:**
- **Tag formats** (`tag_parser.py`, case-insensitive, each with optional `-{referral_code}` suffix): `Stitch-hk:{47-48 char base58 hotkey}`, `Stitch3-{id}`, legacy `bitcast-hk:{hotkey}`, legacy `bitcast-x{id}`. Referral code = URL-safe unpadded base64 of the referrer handle, **MySQL `TO_BASE64`-compatible** (server generates them).
- **Replacement semantics** (`connection_db.upsert_connection`): one row per lowercased username (`UNIQUE`); newest tag always wins for tag/tweet_id; referral fields replaced only if not locked (`payout_date IS NULL`) **and** the new referee amount is higher; self-referral zeroed.
- **Scanning** (`connection_scanner.py`): every 20 min (standard/discovery modes), fetch replies to `CONNECTION_TWEET_IDS = ['2031383975088836738']` (hardcoded); authors must exist in the union of pool social maps; locked referral amount = max across the user's pools of the referral formula.
- **SQLite schema v1** (`migrations.py`, `PRAGMA user_version` runner): `connections(connection_id PK, tweet_id, tag, account_username UNIQUE, added, updated, referral_code, referred_by, referee_amount REAL DEFAULT 50.0, referrer_amount REAL DEFAULT 50.0, payout_date DATE)` + 5 indexes. v0→v1 migration collapses the legacy pool-scoped table (timestamped `.db.bak`, documented merge rules).
- Download from reference validator (`/account-connections`, 404 tolerated); publish full table via signed publisher after each scan.

**Pain points:** DB file lives inside the package directory (state in source tree); fresh `sqlite3.connect` per method, no WAL; `models/` package for one **unused** dataclass; self-referral guard duplicated; whole-table republish every 20 min despite computed change deltas; CLI monkeypatches config at runtime.

**Disposition:** **Keep logic, restructure persistence.** `tag_parser` and `referral_code` port as-is (data-driven pattern registry); DB behind a single repository with WAL + configurable state dir; keep the `user_version` migration runner pattern; merge client+CLI; publish deltas only (flagged change, §7.4).

### 5.7 tweet_filtering (4 files, 966 LOC)

**Purpose:** LLM evaluation of scored tweets against briefs, participant-engagement exclusion, per-author caps.

**Key business logic:** optimistic multi-check — up to `NUM_LLM_CHECKS=3` evaluations (check digit appended to text for distinct cache keys), **pass if ANY passes**; content pre-filter (language, excludes RTs/replies, `tag`/`qrt`/`inclusion_keywords` requirements); **participant-engagement exclusion** — after eval, accounts with ≥1 passing tweet become participants and their RT/QRT contributions are stripped from every tweet's breakdown with scores recomputed (must run post-eval; ordering guaranteed vs. ranking); `apply_max_tweets_filter` keeps top-N per author sorted `(-score, -views, -favorites, created_at)`.

**Pain point:** receives scored tweets in memory but **re-reads them from disk** (newest JSON glob) — race-prone hand-off from scoring.

**Disposition:** merge into `scoring/` package; pass data in memory; one persistence writer for audit JSON.

### 5.8 tweet_bonus (3 files, 333 LOC)

**Purpose:** Post-filter multiplicative bonuses.

**Key business logic:** **performance bonus** — 4 metrics (views, views/follower, total engagements, engagement/view), each `(value/max) × 0.05`, summed → up to **+20%** (docstring says 10% — wrong); **featured tweet** — only within 1 day of brief end, deterministic pick `sha256(sorted_ids)[0] % 5` from top-5-by-views, persisted per brief; engagers of the featured tweet + its author get ×1.05. **Ordering: performance bonus before featured bonus**, both before target calculation.

**Pain point:** `performance_bonus` writes results to a doubled `tweet_bonus/tweet_bonus/` path (bug).

**Disposition:** merge both files into `scoring/bonuses.py`; constants to config; fix the save path (flagged, §7.4).

### 5.9 api (2 files, 303 LOC)

**Purpose:** Reference-validator FastAPI service (port 8094) serving `/health`, `/weights`, `/weights/{uid}`, `/social-map/{pool}`, `/account-connections` from the validator's `.npz` state; slowapi per-IP rate limits (60/10/20/5/5 per minute); **no authentication** — the weight-copy trust model is "trust the reference validator's IP".

**Disposition:** keep as one module; wire formats unchanged (weight-copy validators on the network depend on them); consider optional auth as a future ticket, not this rewrite.

### 5.10 weight_copy (3 files, 160 LOC)

**Purpose:** Default validator mode — every 360 steps (~60 min), async-fetch `/weights` from the reference validator and copy scores locally; guards on size mismatch; all errors keep existing scores.

**Disposition:** merge the two files into one `weight_copy.py`; already the cleanest module in the repo.

### 5.11 base/, bitcast/utils/, bitcast/api/, neurons/, core/ (~1,740 LOC)

**Purpose:** Bittensor framework layer, forked from bittensor-subnet-template.

**Key logic to preserve:**
- **Main loop**: `neurons/validator.py` → `BaseValidatorNeuron.run()` in a daemon thread; forward every `VALIDATOR_WAIT=10s`; error-swallowing loop (log, sleep 30, continue). **Two clocks**: app cadence is step-based (scoring 120 steps ≈ 20 min, thorough 2880 ≈ 8 h, map download 4320 ≈ 12 h, weight-copy fetch 360 ≈ 60 min); on-chain weight setting is block-based (`block − last_update > epoch_length=100`).
- **set_weights pipeline (consensus-critical)**: NaN check → L1 normalize → `process_weights_for_netuid` (vendored `weight_utils.py`: min-allowed, max-weight-limit, exclude-quantile) → `convert_weights_and_uids_for_emit` (max-upscale, ×65535, round, drop zeros) → `subtensor.set_weights(..., version_key=spec_version, mechid=MECHID)`. **`weight_utils.py` has zero tests.**
- **EMA scoring**: `scores = 0.9·new + 0.1·old`, scatter by UID, hotkey-replacement zeroing on metagraph resync, mech-scoped state files `state_mech_{MECHID}.npz`.
- `get_all_uids` returns **all** UIDs (uid 0 forced first) — not sampling.
- **Auto-update** (`core/auto_update.py`): every 10–15 min, `git reset --hard origin/<branch>` + rerun setup + pm2 restart. Destructive; already caused the scalecodec outage.
- **bittensor 10.3.0 surface**: `bt.logging` (pervasive), `bt.Wallet/Subtensor/Metagraph/Dendrite/Axon/Config`; **no custom Synapse/protocol exists** (miners are off-chain). v10 removed lowercase aliases (the 9.12.2→10.3.0 upgrade in `c561873` was a mechanical rename) and added the `mechid=` kwarg.
- **scalecodec fix** (`c1c92b2`): auto-update's `pip install` over a 9.x venv left stale `scalecodec`/`bt-decode`/`bittensor-cli` conflicting with bittensor 10's `cyscale` → import crash on mainnet validators. Fix = explicit `pip uninstall` in `setup_env.sh`. The rewrite must keep this purge until deployments use a lockfile-driven sync.

**Pain points:** dead template miner (`base/miner.py`, no entrypoint), dead `bitcast/api/get_query_axons.py`, dead `tests/helpers.py` (imports `bittensor.mock` — removed in bt 10); cross-layer `MECHID` import into the base framework; `__version__="0.0.0"` → on-chain `version_key=0` while config claims 1.5.1; template argparse config with dead args (`--wandb.entity=opentensor-dev`, `--neuron.sample_size`, blacklist args); `setup.py` with Python 3.8 classifiers; `min_compute.yml` claiming 1 CPU/2 GB for a mode that runs PageRank over thousands of timelines.

**Disposition:** rewrite as a thin `chain/` layer: collapse `BaseNeuron`+`BaseValidatorNeuron` (one concrete neuron exists), keep the vendored `weight_utils.py` **verbatim with new golden tests**, delete miner/template dead code, inject MECHID via config, make auto-update non-destructive (`--ff-only` or clean-tree gate).

### 5.12 SQLite / DB / persistent state (cross-cutting)

| Store | Tech | Location | Contents |
|---|---|---|---|
| Connections | SQLite (schema v1, `user_version`) | `bitcast/validator/account_connection/connections.db` | account↔tag↔referral rows |
| TweetStore | diskcache, 3GB, permanent | `cache/twitter/tweet_store/` | tweets, engagements, fetch timestamps |
| Twitter cache | diskcache, 1GB, 90d TTL | `cache/twitter/` | user timelines + info + AI scores (shared namespace!) |
| LLM cache | diskcache, 1GB, 7d sliding | `cache/llm/` | verdicts keyed by full prompt |
| Briefs cache | diskcache | `cache/briefs/` | last successful briefs response |
| Social maps | JSON files | `bitcast/validator/social_discovery/social_maps/{pool}/` | maps + adjacency + metadata + logs |
| Scored/filtered/bonus/snapshots | JSON files | inside the respective source packages | pipeline audit + snapshot state |
| Chain state | `.npz` | `~/.bittensor/miners/.../state_mech_{MECHID}.npz` | scores, hotkeys, step |

Everything except chain state lives **inside the source tree** — hazardous with `git reset --hard` auto-updates. Rewrite: single configurable `STATE_DIR`; SQLite schema and the reward-snapshot JSON format are network-relevant (§9) and keep their formats.

### 5.13 Config scatter and .env inventory (cross-cutting)

Two config systems: `bitcast/utils/config.py` (template argparse: netuid, epoch_length, wallet/subtensor/axon/logging args, dead wandb/blacklist/sample_size args) and `bitcast/validator/utils/config.py` (env-driven app config). Full env-var surface (defaults in parentheses):

`WALLET_NAME`, `HOTKEY_NAME`, `VALIDATOR_MODE` (weight_copy | standard | discovery), `MECHID` (1), `NETUID` (93, via scripts), `BITCAST_API_URL`, `BITCAST_BRIEFS_ENDPOINT`, `POOLS_API_URL`, `DATA_CLIENT_URL`, `ENABLE_DATA_PUBLISH` (false), `REFERENCE_VALIDATOR_URL` (`http://44.241.197.212`), `TWITTER_API_PROVIDER` (**rapidapi** — README wrongly says desearch), `DESEARCH_API_KEY`, `RAPID_API_KEY` (comma-separated list), `LLM_PROVIDER` (chutes), `CHUTES_API_KEY`, `OPENROUTER_API_KEY`, `DISABLE_LLM_CACHING`, `WANDB_API_KEY`, `WANDB_PROJECT`, `AI_DAMPENING_ENABLED` (false), `ITS_AI_API_URL`, `ITS_AI_BATCH_API_URL`, `ITS_AI_API_KEY`, `ITS_AI_TIMEOUT` (300), `ITS_AI_MAX_RETRIES` (3), `ITS_AI_RETRY_BACKOFF` (2), `ITS_AI_BATCH_SIZE` (250), `AI_DETECTION_CONCURRENCY` (4), `AI_SAMPLE_SIZE` (4), `AI_MIN_TWEET_CHARS` (200), `AI_SCORE_BUCKET` (0.2), `AI_SCORE_CAP` (0.75), `AI_SCORE_TTL_DAYS` (14 — feeds a constant named `_SECONDS`), `AI_MAX_ACCOUNTS_CHECKED` (0), `SUBNET_TREASURY_UID` (106), `NOCODE_UID` (114), `SIMULATE_CONNECTIONS` (false).

Consensus constants hardcoded (correctly versioned-in-code, wrongly scattered): PageRank weights/α, `BASELINE_TWEET_SCORE_FACTOR=2`, `STALE_INFLUENCE_DECAY=0.5`, `EMISSIONS_PERIOD=7`, `REWARDS_DELAY_DAYS=1`, `REWARD_SMOOTHING_EXPONENT=0.65`, `SUBNET_TREASURY_PERCENTAGE=0`, cadence steps, engagement fetch tiers, `CONNECTION_TWEET_IDS`, plus module-local strays: `DISCOVERY_REFERENCE_DATE=2025-11-09`, `DISCOVERY_CYCLE_DAYS=14`, core-stage 10/0.95, `NUM_LLM_CHECKS=3`, `FEATURED_BONUS_MULTIPLIER=1.05`, `MAX_BONUS_PER_METRIC=0.05`, cabal `0.1 + 0.9/x`, fasttrack URL/cap, LLM model names/endpoints, provider base URLs/page caps.

### 5.14 Test coverage (cross-cutting)

583 test functions / 49 files / 11,825 LOC — good coverage of the domain (scoring formula, cabal scaling, time-pinning, bonuses, tag parsing, migrations, providers) and these tests are the **golden reference for the port**. Gaps: zero tests for `base/` (including consensus-critical `weight_utils.py`), `forward.py`, `weight_copy/`, LLM clients, prompts, `adjacency_utils`, map transport/publish, `core/auto_update.py`. No pytest config file; conftest autouse fixtures mock `requests`/`time.sleep` globally (tests already run offline for the domain layer) but there is no shared chain mock — ad-hoc `MagicMock` per test. **CI runs Semgrep only; tests never run in CI** (and `semgrep.yml` has a `branches: [main, main]` typo).

---

## 6. Proposed Target Architecture

### 6.1 Module tree

```
bitcast-x/
├── pyproject.toml              # packaging, deps, pytest/ruff/mypy config, version (single source)
├── src/bitcast/
│   ├── config.py               # ONE config module: pydantic-settings (env) + consensus constants,
│   │                           #   grouped (ChainConfig, ApiKeys, EndpointsConfig, ConsensusParams,
│   │                           #   CadenceConfig, AIDetectionConfig); no import-time side effects
│   ├── models.py               # pydantic: Tweet, UserInfo, Brief, PoolConfig, Connection,
│   │                           #   SocialMap/AccountEntry, ScoredTweet, EmissionTarget, Snapshot
│   ├── exceptions.py           # custom exception hierarchy (replaces log_and_raise_* / bare except)
│   ├── clients/
│   │   ├── http.py             # ONE async request_with_retry (httpx, statuses {429,5xx})
│   │   ├── twitter/            # provider Protocol; desearch.py; rapidapi.py + rapidapi_parser.py;
│   │   │   └── service.py      #   cache/merge/incremental/deletion semantics (ported exactly)
│   │   ├── llm.py              # one client, provider table {chutes, openrouter}; parsing
│   │   ├── prompts.py          # v1–v4 verbatim
│   │   └── its_ai.py           # fail-open AI detection (async)
│   ├── discovery/              # social_discovery, staged:
│   │   ├── graph.py            #   build → filter → pagerank → score (pure, testable stages)
│   │   ├── ai_dampening.py     #   sink math + deterministic sampling/bucketing
│   │   ├── pipeline.py         #   two-stage orchestration
│   │   ├── pools.py            #   cached typed PoolConfig fetch
│   │   ├── schedule.py         #   14-day cycle / date_offset
│   │   ├── maps.py             #   ONE reader/writer (canonical glob) + adjacency codec
│   │   └── transport.py        #   download + publish + staleness (merges 4 current files)
│   ├── connections/            # account_connection:
│   │   ├── tags.py             #   TagParser + referral codes (data-driven registry)
│   │   ├── db.py               #   repository: WAL SQLite in STATE_DIR + user_version migrations
│   │   ├── scanner.py          #   scan/lock-referral service (guard logic in ONE place)
│   │   └── transport.py        #   download client (+ CLI), delta publish
│   ├── scoring/                # tweet_scoring + tweet_filtering + tweet_bonus merged:
│   │   ├── store.py            #   TweetStore (diskcache, indexed to avoid full scans)
│   │   ├── social_map.py       #   loader: time-pinning, multimap, stale decay (ported exactly)
│   │   ├── discovery.py        #   tweet discovery + engagement fetch (async, tiered intervals)
│   │   ├── content_filter.py   #   lang/type/tag/qrt/keyword pre-filter (renamed — kills collision)
│   │   ├── calculator.py       #   THE formula + cabal scaling (pure, golden-tested)
│   │   ├── brief_filter.py     #   LLM multi-check + participant exclusion + max_tweets (in-memory)
│   │   ├── bonuses.py          #   performance + featured (ordering preserved)
│   │   ├── fasttrack.py
│   │   └── persistence.py      #   ONE audit-JSON writer
│   ├── rewards/                # reward_engine flattened, DI kept:
│   │   ├── orchestrator.py     #   constructor-injected pipeline (no ABCs; Protocol seams)
│   │   ├── evaluator.py        #   TwitterEvaluator orchestration (thin)
│   │   ├── reward_math.py      #   smoothing/targets/aggregation/assignment — pure + golden-tested
│   │   ├── distribution.py     #   matrix → caps → normalize → burn remainder → treasury
│   │   ├── briefs.py           #   async fetch + pydantic Brief + state assignment
│   │   ├── snapshots.py        #   deterministic canonical-snapshot selection
│   │   ├── referrals.py        #   activation/payout + amount formula
│   │   └── pricing.py          #   ONE cached (price, total_emissions) pair per cycle
│   ├── chain/                  # bittensor boundary (the ONLY module importing bt beyond logging):
│   │   ├── neuron.py           #   collapsed validator neuron: loop, sync, EMA, state
│   │   ├── weights.py          #   vendored weight_utils VERBATIM + set_weights flow
│   │   └── interface.py        #   Protocol over subtensor/metagraph → mockable in tests
│   ├── publishing.py           # signed/gzip publisher + (payload_type → endpoint) registry
│   ├── caching.py              # namespaced diskcache stores + TTL policy table + lifecycle
│   ├── api.py                  # reference-validator FastAPI (wire-compatible)
│   ├── weight_copy.py          # client + forward merged
│   ├── forward.py              # mode dispatch + cadence (step clocks documented)
│   └── main.py                 # entrypoint: startup checks, wandb, auto-update thread
├── ops/                        # setup/run scripts, pm2 ecosystem.config.js, auto-update
└── tests/                      # ported golden tests + new chain-mock + weight_utils tests
```

### 6.2 How the principles apply

- **LEAN:** no miner, no template argparse remnants, no `interfaces/` ceremony, no per-module `__init__` re-export shims. Every §8 item is simply not ported.
- **CLEAN:** Python 3.12+, full type hints, `exceptions.py` hierarchy (`BitcastError` → `ApiError`, `ConfigError`, `ConsensusError`, …), tz-aware `datetime.now(timezone.utc)` everywhere, docstrings on public APIs only.
- **SOLID:** DI preserved at the orchestrator seam via `typing.Protocol` (structural, no ABC boilerplate); `reward_math.py` and `calculator.py` are pure functions — the consensus core has no I/O. Pydantic models are the inter-module contract, replacing dict conventions.
- **PERFORMANT:** httpx `AsyncClient` pools; `asyncio.gather` for pool fan-out, timeline refresh, engagement fetches, and LLM batch evaluation (replacing every ThreadPoolExecutor); `asyncio.sleep` pacing; diskcache behind `asyncio.to_thread` where it matters; scoring→filtering hand-off in memory (disk JSON becomes audit-only output).
- **MAINTAINABLE:** one `config.py`; dependency direction strictly `chain/clients ← domain ← forward/main` (no in-function import hacks); `pyproject.toml` with a lockfile and dependency-groups (runtime/api/dev); `STATE_DIR` for all mutable state.

### 6.3 Config strategy

Single `config.py` with pydantic-settings groups; `.env` loading only in entrypoints; a `settings.dump()` called once at startup replaces import-time logging. Consensus constants live in a frozen `ConsensusParams` model so they're greppable, typed, and clearly marked "do not touch without a network-wide coordinated release." `MECHID` read once; `CONNECTION_TWEET_IDS` becomes env-configurable with the current value as default.

### 6.4 Async strategy

The forward loop is already asyncio-driven; the rewrite makes the I/O beneath it actually async. Sync remnants (`requests` in briefs, pricing, pools, its-ai, both Twitter providers, both LLM clients) all move to the shared httpx helper. CPU-bound PageRank stays sync (networkx) executed via `asyncio.to_thread` from the discovery manager, replacing the current hand-rolled daemon-thread + nested-event-loop-detection code.

### 6.5 Consolidation plan (headline merges)

| Today | Target |
|---|---|
| ChuteClient.py + OpenRouterClient.py | `clients/llm.py` (provider table) |
| 4 retry implementations | `clients/http.py` |
| social_map_client + social_map_downloader + download_social_map | `discovery/transport.py` |
| 4 divergent map-file globs | `discovery/maps.py` |
| tweet_scoring + tweet_filtering + tweet_bonus | `scoring/` package, in-memory pipeline |
| reward_engine interfaces/services/models/utils (24 files) | `rewards/` (~8 files, DI kept) |
| emission_calculation + reward_distribution + treasury_allocation | `rewards/distribution.py` |
| 4 independent USD→weight computations | `rewards/pricing.py` (one cached pair per cycle) |
| bitcast/utils/config.py + bitcast/validator/utils/config.py | `config.py` |
| twitter_cache + ai_score_cache + cache_utils | `caching.py` (isolated namespaces) |
| connection_publisher + brief_tweet_publisher + social_map_publisher + referral publish | `publishing.py` registry |
| wc_client + wc_forward | `weight_copy.py` |
| BaseNeuron + BaseValidatorNeuron | `chain/neuron.py` |

---

## 7. Business Logic to Port

Every item below MUST be preserved. "Golden tests" = existing tests that encode the numeric expectation and must pass unchanged (modulo import paths).

### 7.1 Consensus-critical math (bit-identical — see §9)

| Logic | Today | Target | Notes |
|---|---|---|---|
| PageRank weights RT 1.0 / mention 2.0 / quote 3.0, α=0.85, max_iter 1000, tol 1e-6 | `utils/config.py:89-94`, `social_discovery.py` | `discovery/graph.py` | README weights are wrong; config wins |
| Max-weight edges for PageRank, sum-weight for relationship matrix; reply/self/invalid-username skips | `social_discovery.py:367-434` | `discovery/graph.py` | |
| Filter order + low-seed relaxation (<20) + seed preservation | `social_discovery.py:219-539` | `discovery/graph.py` | order matters |
| Influence = `round(norm × followers/1000, 2)` | `social_discovery.py:611-626` | `discovery/graph.py` | |
| AI sink `W·ai/(1−ai)`, cap 0.75, dangling redistribution excl. sink, candidate cap tie-break | `social_discovery.py:109-170` | `discovery/ai_dampening.py` | golden: test_scoring_v2 |
| Deterministic AI sampling (sha256 cycle bucket), bucket 0.2, sample 4, min 200 chars, fail-open | `ai_detection.py` | `discovery/ai_dampening.py` | golden: test_ai_detection |
| Discovery schedule: ref date 2025-11-09, cycle 14, per-pool date_offset | `social_discovery.py:672-690` | `discovery/schedule.py` | same constants feed AI bucket — keep aligned |
| Two-stage params: core 2/5/100, 10 iters, Jaccard 0.95; extended 1/1/300, 3 iters, 0.90; personalized PR | `recursive_discovery.py` | `discovery/pipeline.py` | |
| Tweet score = `author_infl × 2 + Σ(infl × weight × cabal)`; quote overrides RT; 1 contribution/engager; round 6dp | `score_calculator.py` | `scoring/calculator.py` | golden: test_score_calculator |
| Cabal scale `0.1 + 0.9/relationship_score` | `score_calculator.py:122-131` | `scoring/calculator.py` | |
| Influence pinned to map active at posting time (SUB-110); multimap union; stale decay 0.5 | `social_map_loader.py` | `scoring/social_map.py` | golden: test_influence_at_time, test_multimap_loading |
| Brief states: scoring `start≤t≤end+1`; emission next 7 days | `brief_fetcher.py:66-104` | `rewards/briefs.py` | end_of_day=23:59:59 semantics from date_utils |
| Assignment: greedy by `(-payout, brief_id, tweet_id)`, one tweet↔one brief, max_tweets cap, committed excluded | `tweet_brief_assignment.py` | `rewards/reward_math.py` | golden: test_tweet_brief_assignment |
| Targets: `smoothed = score^0.65`; `usd = (budget/7)·prop`; `weight = usd/(price·emissions)` | `twitter_evaluator.py:694-770` | `rewards/reward_math.py` | |
| Snapshot freeze + 7-day replay (`total_usd/7` per UID); committed tweet exclusion | `twitter_evaluator.py`, `reward_snapshot.py` | `rewards/snapshots.py` | selection determinism fixed — §7.4 |
| Distribution: per-brief cap → global normalize → UID-0 remainder absorption → treasury `min(pct, burn)` (pct=0) | `reward_distribution_service.py`, `treasury_allocation.py` | `rewards/distribution.py` | golden: test_reward_distribution_service |
| Pricing: CoinGecko bitcast/usd; emissions `7200 × alpha_out × 0.41 × mech_ratio(93, fb 0.15)`; 10-min TTL | `token_pricing.py` | `rewards/pricing.py` | compute ONCE per cycle, share everywhere |
| Referrals: locked per-row amounts; formula 80/20 log followers/influence, clamp pool max (100); activation → payout_date=tomorrow, set-once; both sides paid; NOCODE_UID fallback mapping | `referral_bonus_service.py`, `referral_rewards.py`, orchestrator | `rewards/referrals.py` | **no flat $50; no 5% fee** |
| EMA α=0.9; hotkey-replacement zeroing; L1 → process_weights → u16 emit; `version_key`, `mechid` | `base/validator.py`, `weight_utils.py` | `chain/` | weight_utils verbatim + NEW golden tests |
| Bonuses: perf 4×(value/max)×0.05 then featured ×1.05 (deterministic top-5-by-views sha pick), ordering | `tweet_bonus/` | `scoring/bonuses.py` | golden: test_performance_bonus, test_featured_tweet |

### 7.2 Behavioral logic (semantics-identical)

- Normalized 18-field tweet schema + both providers' full field mappings, pagination, and early-cutoff rules → `models.py` + `clients/twitter/`.
- Cache/merge/deletion semantics: 36h freshness, cutoff−1h incremental, missing_count≥2 deletion, timestamp-preserve-on-empty → `clients/twitter/service.py`.
- LLM: Qwen3-32B, temp 0, prompt-keyed 7-day sliding cache, regex verdict parse, 10k crop; optimistic 3-check any-pass → `clients/llm.py` + `scoring/brief_filter.py`.
- Prompts v1–v4 verbatim → `clients/prompts.py`.
- its-ai fail-open policy and raw-score pass-through → `clients/its_ai.py`.
- Tag grammar (4 patterns + referral suffix), base58 hotkey validation, MySQL-compatible base64url codec → `connections/tags.py`.
- Upsert semantics: newest tag wins; referral lock (`payout_date` set-once, replace only if unlocked and higher); self-referral zeroing (once, in the service) → `connections/db.py` + `scanner.py`.
- SQLite schema v1 + `user_version` migration runner + v0→v1 collapse rules → `connections/db.py` (same schema — §9).
- Scan flow: replies to CONNECTION_TWEET_IDS, author must be in map union, locked amount = max across pools → `connections/scanner.py`.
- Participant-engagement exclusion after LLM filtering (ordering!) and max_tweets sort key → `scoring/brief_filter.py`.
- Tiered engagement re-fetch (1h/4h/24h), quote validation by `quoted_tweet_id`, `until:`+1day search building → `scoring/discovery.py`.
- TweetStore permanence + monotonic metric merge + `first_seen` → `scoring/store.py`.
- Fasttrack poll (stitch3 endpoint, 1000 cap, idempotency, connection-tag processing) → `scoring/fasttrack.py`.
- Validator modes (weight_copy default / standard / discovery), step cadences, block-gated weight setting, error-swallowing main loop, mech-scoped state, startup checks (fatal only on zero maps) → `chain/`, `forward.py`, `main.py`.
- Weight-copy guards (size mismatch → keep existing; errors → keep existing; 360-step cadence) → `weight_copy.py`.
- Reference API endpoints, response shapes, and rate limits unchanged → `api.py`.
- Signed publisher (hotkey signature over `signer:timestamp:sorted-json`, gzip >1MB, expect 202) + all four payload types → `publishing.py`.
- Cache TTL policy: timelines 90d, LLM 7d sliding, AI tweet permanent, AI account 14d, briefs last-good → `caching.py`.
- scalecodec purge in environment setup until lockfile cutover → `ops/`.

### 7.3 Simplification opportunities (no behavior change)

In-memory scoring→filtering hand-off (JSON becomes audit output); one pricing computation shared by the 4 current call sites (removes float-drift risk between stages); single map-file glob; cached PoolManager (one fetch per cycle instead of 9+); Protocol instead of ABC for the DI seams; data-driven tag registry.

### 7.4 Intentional behavior changes (each needs explicit sign-off in the rewrite ticket)

1. **Snapshot selection determinism** — replace oldest-by-mtime with filename-timestamp ordering (mtime is not copy-safe). Grandfather existing snapshot files.
2. **Performance-bonus save path** — fix the doubled `tweet_bonus/tweet_bonus/` directory (audit output only, not consensus).
3. **Delta publishing** — publish changed connections instead of the full table every 20 min (ingestion-API-visible; confirm the API tolerates deltas or keep full-table behind a flag).
4. **Auto-update safety** — `git reset --hard` → fail-closed update (ff-only / clean-tree check).
5. **State out of source tree** — `STATE_DIR` env with migration shim that relocates `connections.db`, caches, social maps, snapshots on first run.
6. **`version_key`** — set a real `__version__` so spec_version ≠ 0. Verify chain-side implications before enabling (validators currently all emit 0).

---

## 8. What to DELETE / Not Port

**Dead code (verified unreferenced):**
- `bitcast/base/miner.py` + `add_miner_args` + blacklist/priority scaffolding — no miner entrypoint exists; mining is off-chain.
- `bitcast/api/get_query_axons.py` — dead template helper (defaults to netuid 21).
- `tests/helpers.py` — imports `bittensor.mock` / `PrometheusInfo`, removed in bittensor 10; referenced by nothing.
- `bitcast/validator/tweet_scoring/engagement_analyzer.py` + `tests/.../test_engagement_analyzer.py` — superseded by `TweetDiscovery._build_engagements_from_store`.
- `reward_engine/models/brief.py` dataclass (live path uses dicts; replaced by pydantic Brief), `EvaluationResultCollection.add_empty_result/get_result`, `AccountResult.create_error_result`, `EmissionTarget.scaling_factors`/boost plumbing, most `ScoreMatrix` accessors.
- `reward_engine/interfaces/` (all ABCs incl. never-used `QueryBasedEvaluator`) and `services/platform_registry.py` — one platform needs no registry.
- `account_connection/models/` package — `AccountMapping` unused on the runtime path.
- `utils/uids.py::check_uid_availability`; dead argparse args (`--wandb.*` template defaults, `--neuron.sample_size/timeout/vpermit_tao_limit`, `--blacklist.*`).
- conftest `mock_youtube_api_calls` fixture (references a nonexistent `platforms.youtube` module — leftover from the YouTube sibling repo).
- `desearch_provider` dead `yesterday` computation; RapidAPI unused buggy `api_key` property; "Restored from main branch" stale comments; `SOCIAL_DISCOVERY_LOOKBACK` unused import; `close()` no-op compat shims.

**Template boilerplate not ported:** Yuma Rao/Opentensor MIT headers; `setup.py` (Py3.8 classifiers) → `pyproject.toml`; the template argparse config layer beyond the args actually used (netuid, epoch_length, wallet/subtensor/logging/axon, disable flags); `min_compute.yml` rewritten with honest per-mode requirements.

**Over-abstraction not ported:** the `TwitterProvider` mega-ABC (→ slim Protocol with optional capabilities); `DataPublisher` ABC layer (only `UnifiedDataPublisher` is used); the LLM singleton-class pattern; per-tree `__init__.py` re-export shims; `error_handling.log_and_raise_*` wrappers (→ typed exceptions).

**Stale docs:** all module READMEs (multiple documented-wrong constants) — rewritten from code; wrong docstrings (baseline 0.5, bonus 10%).

---

## 9. Risk Assessment

### 9.1 Consensus divergence (highest risk)

Validators must produce near-identical weights or lose vtrust/dividends. Per the bitcast-v2 precedent, **all math in §7.1 must be bit-identical**, verified by:
- Porting the existing 583 tests as golden tests (they encode exact numeric expectations).
- A **parity harness**: run old and new pipelines on identical fixture inputs (recorded briefs, tweets, maps, connections, pinned price/emissions) and assert `np.array_equal` on the final weight vector — not "close", equal. Cover: empty briefs, fallback path, snapshot replay days, referral payout day, cap-constrained briefs, AI-dampened maps.
- Specific float hazards: keep float64 matrices; keep operation order in `smoothed/Σsmoothed`; keep `round(…, 2)` influence and `round(…, 6)` scores; **compute the (price, emissions) pair once per cycle** — today's 4 independent fetches can already diverge across a 10-min TTL boundary, and the rewrite must not accidentally "fix" this differently than the reference validator during parallel-run.
- External nondeterminism that already exists (CoinGecko price timing, Twitter API pagination differences, LLM nondeterminism at temp 0) is mitigated by the network's weight-copy design — the reference validator is the de-facto consensus source. Staging must parallel-run against prod reference outputs and diff weights over multiple cycles.

### 9.2 Backwards compatibility with the live network

- **Wire formats frozen:** reference API responses (`/weights`, `/social-map/{pool}`, `/account-connections`), social-map JSON + adjacency v2.0, published payload schemas + signature format, briefs/pools API consumption, tag grammar (incl. legacy `bitcast-*` tags — still live on X), stitch3 fasttrack contract, referral-code MySQL compatibility.
- **`set_weights` call shape:** bittensor 10.3.0 `mechid=` kwarg, `version_key` (see §7.4-6 before changing), u16 conversion — must match exactly or the extrinsic diverges from other validators.
- **Rollout:** weight_copy validators (the majority) only depend on the reference API — lowest risk. Standard/discovery operators auto-update; the destructive updater + venv drift caused the last outage, so ship the rewrite as a **fresh-venv release** with the scalecodec purge retained, and stage the update path itself.

### 9.3 Data/state migration

- **SQLite:** schema v1 unchanged; ship as migration v2 = no-op schema + optional file relocation to `STATE_DIR` (keep the `user_version` runner). Never lose `payout_date` locks or locked referral amounts — these are money.
- **Snapshots:** must keep reading existing snapshot JSON (mid-emission briefs at cutover would otherwise re-freeze at different values — direct payout impact). Grandfather rule required.
- **Caches/TweetStore:** losing them is tolerable but costly (90 days of timeline data, engagement `first_seen` history feeds scoring). Prefer relocation over invalidation; TweetStore pickle formats must load or be migrated.
- **Social maps:** must parse existing files (filename-timestamp format, exclusion rules) — startup would otherwise force a full discovery run.

### 9.4 Other risks

- The two-clock cadence (step vs block) is easy to subtly break; encode both in tests.
- Async rewrite changes request interleaving against rate-limited APIs (RapidAPI plans, Chutes quotas) — load-test in staging with real keys.
- The doc/code mismatches (§1) mean any rewrite guided by docs is wrong by construction; this spec's file:line references and the golden tests are the source of truth.

---

## 10. Acceptance Criteria (for the future rewrite ticket)

Quality bar is the 5 design principles — **no LOC-reduction targets**.

1. **Consensus parity:** parity harness (§9.1) shows byte-identical weight vectors between old and new pipelines across the fixture matrix; all ported golden tests pass with unchanged expected values.
2. **Behavioral compatibility:** reference API responses byte-compatible for identical state; published payloads validate against recorded prod samples; existing `connections.db`, snapshots, and social maps load without data loss.
3. **Principles verified in review:** no dead code (§8 items absent); Python 3.12+ type hints passing `mypy --strict` (or agreed profile); no bare `except:`/`except Exception` without re-raise or typed handling; single `config.py`; import graph acyclic (enforced by lint rule); pydantic models on all inter-module contracts; DI seam preserved (orchestrator constructible with fakes).
4. **Async:** no blocking `requests` calls; no `ThreadPoolExecutor` for I/O; Twitter/LLM/miner-facing fan-out uses `asyncio.gather`; event loop never blocked >100 ms by I/O (spot-check under trace).
5. **Tests without chain:** full suite runs offline with the `chain/interface.py` mock — no subtensor, no network, no API keys; includes NEW tests for `weight_utils` (u16 conversion, normalization edge cases), forward cadence, and weight_copy guards.
6. **CI:** GitHub Actions running lint (ruff), typecheck, and the full pytest suite on every PR; Semgrep retained; `branches: [main, main]` typo fixed.
7. **Packaging:** `pyproject.toml` with locked dependencies (bittensor pinned 10.3.0), dependency groups (runtime/api/dev), single version source feeding both app version and spec_version.
8. **Staging:** deployed to staging in all three modes; standard mode parallel-runs ≥3 full scoring cycles against the prod reference validator with weight-vector diffs reviewed; discovery mode produces a social map diff-compared against a prod-generated map for the same cycle.
9. **Behavior changes:** only the six items in §7.4, each individually approved in the ticket.
10. **Docs:** README and module docs regenerated from code (correct weights/constants); `.env.example` complete per §5.13.

## 11. Constraints

- **Model:** the rewrite is executed with Fable 5 only.
- **Framework:** Bittensor pinned at 10.3.0 (uppercase class API, `mechid` support); netuid 93, MECHID 1.
- **Deployment:** staging only. Any production deploy — including the reference validator and the auto-update release channel — requires Will's explicit approval.
- **Consensus math:** bit-identical per §9.1; the six flagged behavior changes (§7.4) require individual sign-off.
- **External contracts frozen:** briefs/pools/ingestion/stitch3/reference-validator wire formats and the tag grammar are not negotiable in this rewrite.
