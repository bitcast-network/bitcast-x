# TASK: Investigation + Rewrite Spec for bitcast-x (DO NOT REWRITE YET)

You are being tasked with a THOROUGH INVESTIGATION and SPEC AUTHORING task. You must NOT begin the actual rewrite. Your deliverable is a single document: `REWRITE-SPEC.md` at the repo root.

## Context

The bitcast-x repo is the Bittensor SN93 subnet mechanism for X (Twitter). It is ~21,331 LOC of Python across 112 files. The owner (Will) wants a ground-up rewrite following the same design principles that were successfully applied to 4 prior rewrites in this org:

1. **bitcast-v2** (subnet) — 19,517 → 4,460 LOC, 77% reduction
2. **bitcast-api** (v2 code merged into original repo via PR #419)
3. **creator-portal-v3** — 17,876 → 14,045 LOC (React frontend, 63 components)
4. **stitch3-v2** (Stitch3 Platform)

The prior rewrites all followed these 5 DESIGN PRINCIPLES (proven, non-negotiable):

1. **LEAN** — No dead code, no cruft. Strip ALL template boilerplate (license headers, auto_update, dev/ scripts, CloudWatch/Wandb where excessive). Every file must have a clear purpose — if you can't explain why it exists in one sentence, delete it.
2. **CLEAN** — Readable, self-documenting. Type hints on all function signatures (Python 3.12+ style). Docstrings on all public classes/functions. No commented-out code, no contextless TODOs. Consistent error handling (custom exceptions, not bare `except Exception`).
3. **SOLID** — Each module does ONE thing well. Keep the reward engine's dependency injection pattern. BUT don't create 5 layers of abstract interfaces for a 3-line function — the current codebase has `interfaces/`, `services/`, `models/`, `exceptions/` as separate package trees for the reward engine. Consolidate into fewer, richer files where appropriate. Pydantic models replace dataclasses and custom classes.
4. **PERFORMANT** — Async-first: all I/O (Twitter API, LLM calls, miner queries) must be async. `asyncio.gather` for parallel miner queries. Cache API responses (port the concept, not the implementation).
5. **MAINTAINABLE** — Configuration in ONE module (`config.py`), not scattered across multiple files. No circular imports. Dependencies in `pyproject.toml`, not `requirements.txt`. Tests must run without a Bittensor network connection (mock the chain).

**CRITICAL INSTRUCTION** (from prior rewrites, proven): "Read the old code for business logic. Do NOT copy its structure or patterns — only port the actual business value."

## Your Deliverable: REWRITE-SPEC.md

Produce a comprehensive rewrite spec at the repo root, following the exact structure proven on SUB-113 (creator-portal-v3) and SUB-115 (stitch3-v2). The spec MUST include the sections below.

### 1. Executive Summary
- Current state: LOC, file count, architecture overview
- Why a rewrite is warranted (cruft, tech debt, pain points you identify)
- Target architecture in brief

### 2. Reference Codebase
- Path to current codebase (read-only reference for business logic)
- LOC breakdown by module
- Critical instruction about porting business value, not structure

### 3. Design Principles
- The 5 principles above, verbatim

### 4. Scope
- In scope (what the rewrite covers)
- Out of scope (DB changes, infra/terraform, production deployment — staging only)

### 5. Current Architecture Analysis (THOROUGH — this is the core of your investigation)

For EACH major module, document:
- **Purpose** (one sentence)
- **Current LOC / file count**
- **Key business logic** (what it actually DOES — the value to preserve)
- **Pain points / cruft** (dead code, over-abstraction, scattered config, etc.)
- **Dependencies** (what it imports, what imports it)
- **Rewrite recommendation** (consolidate, split, keep, delete)

Major modules to investigate (from LOC breakdown):
- `social_discovery/` (4,812 LOC) — AI out-link dampening, recursive discovery, pool management
- `clients/` (3,728 LOC) — Twitter client, OpenRouter, Chute, RapidAPI, desearch, its_ai, prompts
- `tweet_scoring/` (3,072 LOC)
- `reward_engine/` (2,893 LOC) — interfaces/, services/, models/, orchestrator, twitter_evaluator
- `utils/` (1,724 LOC) — config, logging, misc, uids
- `account_connection/` (1,639 LOC) — connection client, DB, publisher, scanner, referral codes, tag parser
- `tweet_filtering/` (966 LOC)
- `tweet_bonus/` (333 LOC)
- `api/` (303 LOC) — get_query_axons
- `weight_copy/` (160 LOC)
- `base/` (miner, neuron, validator, weight_utils)

Also investigate:
- The bittensor version situation (recently upgraded 9.12.2 → 10.3.0 per commit c561913)
- The recent scalecodec conflict fix (commit c1c92b2)
- Any SQLite/DB usage (connection_db.py, migrations.py)
- Test coverage (what tests exist, what's missing)
- Config management (how scattered is configuration?)
- The `.env.example` and environment variable usage

### 6. Proposed Target Architecture
- Module structure for the rewrite (tree diagram)
- How the 5 principles apply to each decision
- Consolidation plan (which modules merge, which split)
- Config consolidation strategy
- Async strategy (which calls become async)

### 7. Business Logic to Port (Module by Module)
- List every piece of business logic that MUST be preserved
- For each: where it lives now, where it should live in the rewrite, any simplification opportunity

### 8. What to DELETE / Not Port
- Specific files/patterns to eliminate
- Template boilerplate
- Over-abstraction layers

### 9. Risk Assessment
- What could break during the rewrite
- Consensus-critical logic (reward math MUST be identical — same precedent as bitcast-v2)
- Backwards compatibility concerns (miners, validators on the network)
- Database schema concerns

### 10. Acceptance Criteria (for the FUTURE rewrite ticket, not this investigation)
- Model/Framework constraints
- Test requirements (must run without Bittensor network connection)
- CI requirements
- Staging deploy requirements
- Note: NO LOC reduction targets (Will explicitly rejected these — the design principles are the quality bar, not line count)

### 11. Constraints (for the future rewrite)
- Model: Fable 5 (Claude Code via AO) — no other model acceptable
- Must work against Bittensor 10.3.0 (current)
- Production deploys require Will's approval
- Must deploy via existing ECS pattern where applicable

## How to Work

1. READ the codebase thoroughly. Use `find`, `wc -l`, `cat`, `grep` to understand every module. Don't skim — this spec will be the basis for a multi-hour rewrite, so accuracy matters.
2. For each module, read the actual source files, not just the file names.
3. Identify pain points by comparing against the 5 design principles.
4. Write the spec to `REWRITE-SPEC.md` at the repo root.
5. Commit and push to a branch named `investigation/rewrite-spec`.
6. Open a PR titled "Investigation: bitcast-x rewrite spec".
7. **DO NOT modify any source code. DO NOT begin the rewrite. Your only output is REWRITE-SPEC.md.**

## What Will Reviews

Will will review the PR and the `REWRITE-SPEC.md` document. He will decide:
- Whether to proceed with the rewrite
- Whether to adjust the scope
- Whether to open a Linear ticket (like SUB-113, SUB-115) for the actual rewrite

Be thorough. Be honest about pain points. Be specific about what to port vs delete. This spec is the foundation for the entire rewrite — getting it wrong means a wasted multi-hour AO session later.
