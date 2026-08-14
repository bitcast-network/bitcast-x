"""Generic platform-control service layered over the reusable miner SDK."""

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from bitcast_x.campaigns import CampaignRecord
from bitcast_x.errors import ProtocolError
from bitcast_x.miner.engine import MinerSdk
from bitcast_x.miner.results import MinerResultsClient
from bitcast_x.miner.store import EventStatus


class CampaignSource(Protocol):
    """Campaign operations required by a platform-facing miner."""

    async def fetch_campaigns(self) -> tuple[CampaignRecord, ...]: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class MinerControlService:
    """Use-case layer with explicit, durable commitment completion semantics."""

    sdk: MinerSdk
    campaign_source: CampaignSource
    commit_timeout_seconds: float
    results_client: MinerResultsClient | None = None

    async def campaigns(self) -> list[dict[str, object]]:
        """Return open campaigns through the configured public metadata source."""

        if self.results_client is not None:
            return list(await self.results_client.campaigns())
        campaigns = await self.campaign_source.fetch_campaigns()
        return [campaign.model_dump(mode="json") for campaign in campaigns]

    async def create_claim(
        self,
        campaign_id: str,
        creator_x_id: str,
        draft: str,
    ) -> dict[str, str]:
        """Create a claim and wait briefly for finalized commitment."""

        claim_id = self.sdk.create_claim(
            campaign_id=campaign_id,
            creator_x_id=creator_x_id,
            draft=draft,
        )
        await self._await_commit()
        status = self.sdk.claim_status(claim_id)
        return {"claim_id": claim_id, "status": status.value if status else "unknown"}

    async def submit_tweet(
        self,
        campaign_id: str,
        tweet_id: str,
        claim_id: str | None,
    ) -> dict[str, str]:
        """Commit a published tweet mapping for validator retrieval."""

        if claim_id is not None:
            expected = self._claim_campaign_id(claim_id)
            if expected is None:
                raise ProtocolError("submission claim_id does not belong to this miner")
            if expected != campaign_id:
                raise ProtocolError(
                    f"campaign_id {campaign_id!r} does not match claim campaign {expected!r}"
                )

        submission_id = self.sdk.submit_tweet(
            campaign_id=campaign_id,
            tweet_id=tweet_id,
            claim_id=claim_id,
        )
        await self._await_commit()
        status = self.sdk.submission_status(submission_id)
        return {
            "submission_id": submission_id,
            "status": status.value if status else "unknown",
        }

    def claim_status(self, claim_id: str) -> dict[str, str]:
        """Read one durable claim status."""

        status = self.sdk.claim_status(claim_id)
        if status is None:
            return {"claim_id": claim_id, "status": "not_found"}
        result = {"claim_id": claim_id, "status": status.value}
        campaign_id = self._claim_campaign_id(claim_id)
        if campaign_id is not None:
            result["campaign_id"] = campaign_id
        return result

    def submission_status(self, submission_id: str) -> dict[str, str]:
        """Read one durable submission status."""

        status = self.sdk.submission_status(submission_id)
        return {
            "submission_id": submission_id,
            "status": status.value if status else "not_found",
        }

    async def submissions(self) -> list[dict[str, object]]:
        """Return durable submissions enriched with latest validator results."""

        submissions = self.sdk.submissions()
        if self.results_client is None:
            return submissions
        output: list[dict[str, object]] = []
        for submission in submissions:
            result = await self.results_client.submission(str(submission["submission_id"]))
            output.append({**submission, **result})
        return output

    async def sync_submission_results(self) -> None:
        """Persist final central results for locally pending submissions."""

        if self.results_client is None:
            return
        for submission in self.sdk.submissions():
            if submission["status"] != EventStatus.VERIFICATION_PENDING.value:
                continue
            submission_id = str(submission["submission_id"])
            result = await self.results_client.submission(submission_id)
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

    def _claim_campaign_id(self, claim_id: str) -> str | None:
        """Read the campaign bound to a durable claim event."""

        with sqlite3.connect(self.sdk.engine.store.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM events WHERE event_id = ? AND kind = 'claim'",
                (claim_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        campaign_id = payload.get("campaign_id")
        return campaign_id if isinstance(campaign_id, str) else None
