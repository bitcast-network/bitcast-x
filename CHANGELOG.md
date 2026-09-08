# Changelog

All notable changes to Bitcast X are recorded here. This project follows
[Semantic Versioning](https://semver.org/); software release versions are separate from the wire,
campaign-manifest, and event-schema versions documented in `docs/protocol.md`.

## [3.0.0] - Unreleased

### Breaking changes

- Retire `legacy_connection` campaign execution, connection collection, legacy reward and referral
  emissions, legacy pricing, and temporary treasury routing. A feed containing a retired campaign
  aborts the entire validator cycle before campaign scoring, result publication, or weight submission.
- Remove `legacy-state-info`, the `bitcast_x.legacy` and `bitcast_x.validator.legacy` Python modules,
  `BITCAST_X_LEGACY_*` settings, provider search/reply methods, and legacy scorer extension arguments.
  These incompatible operator and package changes require a software major release. See the
  [upgrade guide](docs/upgrade-3.0.md) for the affected interfaces and migration steps.

### Compatibility

- The retirement does not change the miner application `/api/v1` contract, canonical batch hashes,
  `DX2`/`DX3` commitments, `/v2/batches` or `/v3/batches`, or preclaim reward calculations. Historical
  `legacy_connection` records remain readable. Miner schema 3 and validator schema 6 are unchanged.
- Existing legacy archives are left in place. Historical settlement remains separate from validator
  campaign emissions. Automatic source updates following `origin/main` can activate this removal
  at merge time; they do not wait for a release tag or stop at a software major-version boundary.

### Changed

- Cache the miner qualification snapshot for 60 seconds and reuse a fetched campaign during direct
  submission, reducing repeated upstream reads.

### Fixed

- Exclusive direct campaigns accept already-published tweets during the evaluation-day grace
  period only when the creator was historically eligible and the submission is committed no later
  than the campaign's scoring-close block.

## [2.2.0] - 2026-08-31

### Added

- A miner hotkey can recover from lost local commitment state with the explicit `resume-history`
  operator command. The command generates the history ID itself; no validator cursor or sequence
  number is operator input.
- The first signed `DX3` batch is the atomic future-only boundary. Validators preserve accepted
  history, reject reused history IDs, and isolate claims across histories.

### Compatibility

- This is a coordinated miner-validator protocol rollout. Validators without `DX3` support will
  quarantine a resumed miner until upgraded; ordinary `DX2` histories remain
  unchanged.

## [2.1.0] - 2026-08-22

### Added

- The authenticated miner application API now exposes the complete versioned `/api/v1` contract
  for third-party products, including enabled ecosystems, leaderboard reads, idempotent creator
  claims and submissions, and stable upstream error envelopes.

### Compatibility

- `/api/v1` is a public integration contract. Existing fields and semantics remain supported for
  the lifetime of v1; incompatible changes require a new path version and an overlap window.

### Changed

- The generic brief-instruction compliance prompt is available as version 6. Version 1 retains its
  original byte-stable sponsor evaluation, and retired prompt versions 3 and 4 remain unavailable.

### Fixed

- Finney miners and validators now use the qualification schedule bundled with the reviewed
  release, preventing stale environment files from silently retaining obsolete eligibility rules.
- Miner-hotkey stake qualification now counts all alpha staked to the miner hotkey on the subnet,
  rather than only stake supplied by the hotkey's controlling coldkey.

## [2.0.0] - 2026-08-13

### Added

- Bittensor v11 reference miner, authenticated miner API, and validator implementation.
- Self-contained protocol, compatibility, and operator documentation.
- Container and PM2 source-install paths with role-specific configuration examples.
- Durable state inspection, backup, recovery, shadow reporting, and upgrade safeguards.

### Changed

- Source-install automatic updates are explicit opt-in.
- Package and container release metadata now identify the same immutable software release.
- Production validators publish signed campaign results and submit mechanism-1 weights by default;
  either output can still be disabled explicitly for diagnostics.
- Enabled production outputs now fail at startup when required reconciliation providers are not
  configured, instead of running as an ingestion-only validator.

### Compatibility

- Current protocol boundary versions remain those listed in `docs/protocol.md`; the `2.0.0`
  software release does not renumber those independent contracts.
- Earlier development builds reported package version `0.1.0` and did not carry a public release
  compatibility commitment.
