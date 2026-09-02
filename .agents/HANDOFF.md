# Handoff

Goal: make campaign eligibility forward-sticky without allowing later maps to qualify older posts,
and protect an eligible creator's author-influence baseline from later downward recalibration.

Status: PR #124 is open, mergeable, and awaiting the required `mizu-tx` review. The companion
bitcast-api PR #539 is merged and healthy on staging; its production job remains gated. Stitch PR
#580 is merged and healthy on staging; its production job also remains gated.

Files changed:
- `src/bitcast_x/campaigns.py`
- `src/bitcast_x/legacy/engine.py`
- `src/bitcast_x/validator/reconciliation.py`
- `src/bitcast_x/validator/scoring.py`
- focused tests and protocol documentation
- existing release surfaces for version `2.2.1`

Decision: rank eligibility is the union of qualifying maps from campaign opening through the post
time. Leaving the cutoff does not revoke access; entering later grants access only from that map
onward. Author influence is the higher of the first eligibility-granting influence and the map
active when the post was published. Stitch does not duplicate this historical calculation.

Verification:
- Feature branch CI previously passed quality, container, CodeQL, and Semgrep checks.
- `pytest -q`: 428 passed, 3 skipped before the release-only follow-up.
- Release identity and metadata tests: 5 passed after the `2.2.1` bump.
- Ruff format and lint checks pass.

Risk: this is shared validator scoring behavior. Do not approve the API production deployment until
validators have upgraded to the reviewed `v2.2.1` release. Existing finalized rewards remain
unchanged.

Next action: obtain the required review, merge PR #124, publish the immutable `v2.2.1` release,
upgrade validators, then approve the waiting API and Stitch production deployments.
