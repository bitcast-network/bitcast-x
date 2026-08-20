"""Generic application-control service layered over the reusable miner SDK."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from bitcast_x.campaigns import CampaignRecord
from bitcast_x.errors import ProtocolError
from bitcast_x.miner.engine import MinerSdk
from bitcast_x.miner.results import MinerResultsClient
from bitcast_x.miner.store import EventStatus, OperationMetadata


class CampaignSource(Protocol):
    """Fallback campaign operation required by offline development miners."""

    async def fetch_campaigns(self) -> tuple[CampaignRecord, ...]: ...

    async def close(self) -> None: ...


def _fingerprint(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _timestamp(nanoseconds: int) -> str:
    return (
        datetime.fromtimestamp(nanoseconds / 1_000_000_000, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


@dataclass(slots=True)
class MinerControlService:
    """Join local durable operations with authorized central read data."""

    sdk: MinerSdk
    campaign_source: CampaignSource
    commit_timeout_seconds: float
    results_client: MinerResultsClient | None = None
    enabled_ecosystem_ids: tuple[str, ...] = ()

    def _ecosystems(self, requested: tuple[str, ...] = ()) -> tuple[str, ...]:
        configured = set(self.enabled_ecosystem_ids)
        if requested and configured and not set(requested).issubset(configured):
            raise ProtocolError("requested ecosystem is not enabled by this miner")
        return requested or self.enabled_ecosystem_ids

    async def ecosystems(self) -> list[dict[str, Any]]:
        if self.results_client is None:
            return [
                {
                    "ecosystem_id": ecosystem_id,
                    "name": ecosystem_id.replace("_", " ").title(),
                    "status": "active",
                    "enabled": True,
                }
                for ecosystem_id in self.enabled_ecosystem_ids
            ]
        items = await self.results_client.ecosystems()
        enabled = set(self.enabled_ecosystem_ids)
        return [
            {**item, "enabled": True}
            for item in items
            if not enabled or item.get("ecosystem_id") in enabled
        ]

    async def campaigns(
        self,
        ecosystem_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        selected = self._ecosystems(ecosystem_ids)
        if self.results_client is not None:
            return list(await self.results_client.campaigns(selected))
        campaigns = await self.campaign_source.fetch_campaigns()
        return [
            campaign.model_dump(mode="json")
            for campaign in campaigns
            if campaign.access.mining_protocol.value == "preclaim_v2"
            and (not selected or bool(set(campaign.pools).intersection(selected)))
            and (
                campaign.access.exclusive_miner_hotkey is None
                or campaign.access.exclusive_miner_hotkey == self.sdk.engine.miner_hotkey
            )
        ]

    async def campaign(self, campaign_id: str) -> dict[str, Any] | None:
        if self.results_client is not None:
            try:
                campaign = await self.results_client.campaign(campaign_id)
            except Exception as error:
                if getattr(getattr(error, "response", None), "status_code", None) == 404:
                    return None
                raise
            pools = set(campaign.get("ecosystem_ids", []))
            if self.enabled_ecosystem_ids and not pools.intersection(self.enabled_ecosystem_ids):
                return None
            return campaign
        return next(
            (
                campaign
                for campaign in await self.campaigns()
                if (
                    campaign.get("campaign_id") == campaign_id
                    or campaign.get("access", {}).get("campaign_id") == campaign_id
                )
            ),
            None,
        )

    async def eligibility(self, campaign_id: str, creator_x_id: str) -> dict[str, Any]:
        campaign = await self.campaign(campaign_id)
        if campaign is None:
            raise ProtocolError("campaign is not available to this miner")
        if self.results_client is None:
            raise ProtocolError("central eligibility service is unavailable")
        return await self.results_client.eligibility(campaign_id, creator_x_id)

    async def campaign_tweets(
        self,
        campaign_id: str,
        ecosystem_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if await self.campaign(campaign_id) is None:
            raise ProtocolError("campaign is not available to this miner")
        if self.results_client is None:
            raise ProtocolError("central campaign results service is unavailable")
        return await self.results_client.campaign_tweets(
            campaign_id,
            self._ecosystems(ecosystem_ids),
        )

    async def create_claim(
        self,
        campaign_id: str,
        creator_x_id: str,
        draft: str,
        *,
        idempotency_key: str,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate, persist, and commit a pre-publication claim."""

        campaign = await self.campaign(campaign_id)
        if campaign is None or not campaign.get("capabilities", {}).get("can_claim", False):
            raise ProtocolError("campaign does not accept claims")
        eligibility = await self.eligibility(campaign_id, creator_x_id)
        if not eligibility.get("claim_eligible", False):
            raise ProtocolError("creator is not eligible to claim this campaign")
        metadata = OperationMetadata(
            idempotency_key=idempotency_key,
            request_fingerprint=_fingerprint(
                {
                    "campaign_id": campaign_id,
                    "creator_x_id": creator_x_id,
                    "draft": draft,
                    "external_id": external_id,
                }
            ),
            campaign_snapshot_id=str(campaign["campaign_snapshot_id"]),
            ecosystem_ids=tuple(campaign.get("ecosystem_ids", [])),
            creator_x_id=creator_x_id,
            external_id=external_id,
        )
        claim_id = self.sdk.create_claim(
            campaign_id=campaign_id,
            creator_x_id=creator_x_id,
            draft=draft,
            metadata=metadata,
        )
        await self._await_commit()
        claim = self.claim_status(claim_id)
        if claim is None:
            raise ProtocolError("durable claim disappeared")
        return claim

    async def submit_tweet(
        self,
        campaign_id: str,
        tweet_id: str,
        claim_id: str | None,
        creator_x_id: str,
        *,
        idempotency_key: str,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate and durably accept a published tweet mapping."""

        campaign = await self.campaign(campaign_id)
        if campaign is None or not campaign.get("capabilities", {}).get("can_submit", False):
            raise ProtocolError("campaign does not accept submissions")
        requires_claim = bool(campaign.get("capabilities", {}).get("requires_claim", True))
        if requires_claim and claim_id is None:
            raise ProtocolError("campaign requires a safe pre-publication claim")
        if not requires_claim and claim_id is not None:
            raise ProtocolError("direct campaign submissions must not include a claim")
        if claim_id is not None:
            claim = self.claim_status(claim_id)
            if claim is None:
                raise ProtocolError("submission claim_id does not belong to this miner")
            if claim.get("campaign_id") != campaign_id:
                raise ProtocolError("claim campaign does not match submission campaign")
            if claim.get("creator_x_id") != creator_x_id:
                raise ProtocolError("claim creator does not match submission creator")
            if not claim.get("usability", {}).get("safe_to_post", False):
                raise ProtocolError("claim is not safe to post")

        metadata = OperationMetadata(
            idempotency_key=idempotency_key,
            request_fingerprint=_fingerprint(
                {
                    "campaign_id": campaign_id,
                    "tweet_id": tweet_id,
                    "claim_id": claim_id,
                    "creator_x_id": creator_x_id,
                    "external_id": external_id,
                }
            ),
            campaign_snapshot_id=str(campaign["campaign_snapshot_id"]),
            ecosystem_ids=tuple(campaign.get("ecosystem_ids", [])),
            creator_x_id=creator_x_id,
            external_id=external_id,
        )
        submission_id = self.sdk.submit_tweet(
            campaign_id=campaign_id,
            tweet_id=tweet_id,
            claim_id=claim_id,
            metadata=metadata,
        )
        submission = await self.submission_status(submission_id)
        if submission is None:
            raise ProtocolError("durable submission disappeared")
        return submission

    @staticmethod
    def _claim_resource(receipt: dict[str, object]) -> dict[str, Any]:
        status = str(receipt["status"])
        usability = {
            EventStatus.WAITING_FOR_COMMITMENT.value: "pending",
            EventStatus.SAFE_TO_POST.value: "active",
            EventStatus.EVICTED.value: "evicted",
            EventStatus.CONSUMED.value: "consumed",
        }.get(status, "expired")
        return {
            "claim_id": receipt["claim_id"],
            "external_id": receipt["external_id"],
            "campaign_id": receipt["campaign_id"],
            "campaign_snapshot_id": receipt["campaign_snapshot_id"],
            "ecosystem_ids": receipt["ecosystem_ids"],
            "creator_x_id": receipt["creator_x_id"],
            "commitment": receipt["commitment"],
            "usability": {
                "status": usability,
                "safe_to_post": status == EventStatus.SAFE_TO_POST.value,
                "maximum_active_claims": 5,
                "evicted_by_claim_id": receipt["evicted_by_claim_id"],
                "consumed_by_submission_id": receipt["consumed_by_submission_id"],
            },
            "created_at": _timestamp(int(str(receipt["created_ns"]))),
            "updated_at": _timestamp(int(str(receipt["updated_ns"]))),
        }

    def claim_status(self, claim_id: str) -> dict[str, Any] | None:
        receipt = self.sdk.engine.store.receipt(claim_id)
        if receipt is None or receipt["kind"] != "claim":
            return None
        return self._claim_resource(receipt)

    def claims(
        self,
        *,
        campaign_id: str | None = None,
        creator_x_id: str | None = None,
        external_id: str | None = None,
        ecosystem_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        receipts = self.sdk.engine.store.receipts(
            kind="claim",
            campaign_id=campaign_id,
            creator_x_id=creator_x_id,
            external_id=external_id,
            ecosystem_ids=self._ecosystems(ecosystem_ids),
        )
        return [self._claim_resource(receipt) for receipt in receipts]

    @staticmethod
    def _submission_resource(receipt: dict[str, object]) -> dict[str, Any]:
        return {
            "submission_id": receipt["submission_id"],
            "external_id": receipt["external_id"],
            "campaign_id": receipt["campaign_id"],
            "campaign_snapshot_id": receipt["campaign_snapshot_id"],
            "ecosystem_ids": receipt["ecosystem_ids"],
            "tweet_id": receipt["tweet_id"],
            "claim_id": receipt["claim_id"],
            "creator": {"submitted_x_id": receipt["creator_x_id"]},
            "status": receipt["status"],
            "submission_commitment": receipt["commitment"],
            "created_at": _timestamp(int(str(receipt["created_ns"]))),
            "updated_at": _timestamp(int(str(receipt["updated_ns"]))),
        }

    async def submission_status(self, submission_id: str) -> dict[str, Any] | None:
        receipt = self.sdk.engine.store.receipt(submission_id)
        if receipt is None or receipt["kind"] != "submission":
            return None
        local = self._submission_resource(receipt)
        if self.results_client is None:
            return local
        result = await self.results_client.submission(submission_id)
        return self._merge_submission(local, result)

    @staticmethod
    def _merge_submission(local: dict[str, Any], central: dict[str, Any]) -> dict[str, Any]:
        central_values = {key: value for key, value in central.items() if value is not None}
        merged = {**local, **central_values}
        if (
            local.get("status") == EventStatus.TWEET_RECEIVED.value
            and central.get("status") == EventStatus.VERIFICATION_PENDING.value
        ):
            merged["status"] = local["status"]
        local_creator = local.get("creator") or {}
        central_creator = central.get("creator") or {}
        merged["creator"] = {**local_creator, **central_creator}
        merged["submission_commitment"] = local["submission_commitment"]
        merged["created_at"] = local["created_at"]
        return merged

    async def submissions(
        self,
        *,
        campaign_id: str | None = None,
        creator_x_id: str | None = None,
        tweet_id: str | None = None,
        external_id: str | None = None,
        ecosystem_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        receipts = self.sdk.engine.store.receipts(
            kind="submission",
            campaign_id=campaign_id,
            creator_x_id=creator_x_id,
            external_id=external_id,
            ecosystem_ids=self._ecosystems(ecosystem_ids),
        )
        local = [self._submission_resource(receipt) for receipt in receipts]
        if tweet_id is not None:
            local = [item for item in local if item["tweet_id"] == tweet_id]
        if self.results_client is None:
            return local
        central = await self.results_client.submissions(
            campaign_id=campaign_id,
            tweet_id=tweet_id,
        )
        by_id = {str(item["submission_id"]): item for item in central}
        return [
            self._merge_submission(item, by_id[str(item["submission_id"])])
            if str(item["submission_id"]) in by_id
            else item
            for item in local
        ]

    async def sync_submission_results(self) -> None:
        """Persist final central results for locally pending submissions."""

        if self.results_client is None:
            return
        central = await self.results_client.submissions()
        by_id = {str(item["submission_id"]): item for item in central}
        for submission in self.sdk.submissions():
            if submission["status"] != EventStatus.VERIFICATION_PENDING.value:
                continue
            submission_id = str(submission["submission_id"])
            result = by_id.get(submission_id)
            if result is None:
                continue
            status = result.get("status")
            if status == EventStatus.ATTRIBUTED.value:
                self.sdk.record_submission_result(submission_id, EventStatus.ATTRIBUTED)
            elif status == EventStatus.REJECTED.value:
                self.sdk.record_submission_result(submission_id, EventStatus.REJECTED)

    async def qualification(self) -> dict[str, object]:
        """Read the current on-chain miner qualification snapshot."""

        return dict(await self.sdk.qualification_status())

    async def _await_commit(self) -> None:
        """Wait briefly for finalization while durable work survives timeout."""

        try:
            await asyncio.wait_for(
                self.sdk.engine.commit_ready(force=True),
                timeout=self.commit_timeout_seconds,
            )
        except TimeoutError:
            return
