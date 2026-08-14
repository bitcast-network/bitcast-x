"""Route and combine local legacy and pre-claim X campaign weights."""

import math

from bitcast_x.campaigns import CampaignFeed
from bitcast_x.errors import ProtocolError
from bitcast_x.protocol import MiningProtocol

LEGACY_TREASURY_UID = 155


def preclaim_feed(feed: CampaignFeed) -> CampaignFeed:
    """Return the same snapshot with legacy campaigns excluded from v3 processing."""

    return feed.model_copy(
        update={
            "campaigns": tuple(
                campaign
                for campaign in feed.campaigns
                if campaign.access.mining_protocol is MiningProtocol.PRECLAIM_V2
            )
        }
    )


def has_legacy_campaigns(feed: CampaignFeed) -> bool:
    """Return whether this snapshot still requires the temporary legacy path."""

    return any(
        campaign.access.mining_protocol is MiningProtocol.LEGACY_CONNECTION
        for campaign in feed.campaigns
    )


def combine_weights(
    legacy: dict[int, float],
    productive: dict[int, float],
    *,
    uids: list[int],
    burn_uid: int = 0,
    treasury_uid: int = LEGACY_TREASURY_UID,
) -> dict[int, float]:
    """Route legacy excess to productive v2 miners, or temporarily to treasury."""

    known_uids = set(uids)
    _validate_weights("legacy", legacy, known_uids)
    _validate_weights("productive", productive, known_uids)
    legacy_total = math.fsum(legacy.values())
    if not math.isclose(legacy_total, 1.0, rel_tol=0, abs_tol=1e-9):
        raise ProtocolError("legacy weights must sum to one")

    productive_total = math.fsum(weight for uid, weight in productive.items() if uid != burn_uid)
    combined = {uid: legacy.get(uid, 0.0) for uid in uids}
    legacy_burn = combined.get(burn_uid, 0.0)
    if productive_total > 0 and legacy_burn > 0:
        combined[burn_uid] = 0.0
        for uid, weight in productive.items():
            if uid != burn_uid and weight > 0:
                combined[uid] += legacy_burn * weight / productive_total
    elif legacy_burn > 0:
        if treasury_uid not in known_uids:
            raise ProtocolError(f"legacy treasury UID {treasury_uid} is absent from the metagraph")
        combined[burn_uid] = 0.0
        combined[treasury_uid] += legacy_burn
    return combined


def _validate_weights(label: str, weights: dict[int, float], known_uids: set[int]) -> None:
    unknown = set(weights) - known_uids
    if unknown:
        raise ProtocolError(f"{label} weights contain unknown UIDs: {sorted(unknown)}")
    if any(not math.isfinite(value) or value < 0 for value in weights.values()):
        raise ProtocolError(f"{label} weights must be finite and non-negative")
