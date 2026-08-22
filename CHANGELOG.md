# Changelog

All notable changes to Bitcast X are recorded here. This project follows
[Semantic Versioning](https://semver.org/); software release versions are separate from the wire,
campaign-manifest, and event-schema versions documented in `docs/protocol.md`.

## [Unreleased]

## [2.1.0] - 2026-08-22

### Added

- The authenticated miner application API now exposes the complete versioned `/api/v1` contract
  for third-party products, including enabled ecosystems, leaderboard reads, idempotent creator
  claims and submissions, and stable upstream error envelopes.

### Compatibility

- `/api/v1` is a public integration contract. Existing fields and semantics remain supported for
  the lifetime of v1; incompatible changes require a new path version and an overlap window.

### Changed

- Prompt version 1 now performs generic brief-instruction compliance, while retired prompt
  versions 3 and 4 are no longer accepted; versions 2 and 5 remain byte-stable.

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
