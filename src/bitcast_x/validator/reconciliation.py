"""Replay verified history into deterministic open and exclusive attribution."""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from bitcast_x.campaigns import (
    CampaignFeed,
    CampaignRecord,
    ecosystem_map_at,
    eligible_creator_ids_in_map,
)
from bitcast_x.errors import ReconciliationUnavailableError
from bitcast_x.matcher import MatchCandidate, choose_match, normalize_match_text
from bitcast_x.protocol import (
    AttributionReason,
    AttributionResult,
    ClaimEvent,
    CommitmentPosition,
    DraftReveal,
    MiningProtocol,
    SubmissionEvent,
)
from bitcast_x.validator.store import ValidatorStore
from bitcast_x.x_provider import Tweet, XProvider

LOGGER = logging.getLogger(__name__)


class QualificationChecker(Protocol):
    """Historical financial-barrier decision used by reconciliation."""

    async def eligible(self, miner_hotkey: str, block: int) -> bool: ...


def _public_match_text(campaign: CampaignRecord) -> str:
    """Return every public content token that cannot prove private draft access."""

    parts = [
        campaign.display,
        campaign.brief,
        *campaign.required_terms,
        *campaign.inclusion_keywords,
    ]
    if campaign.tag is not None:
        parts.append(campaign.tag)
    if campaign.quoted_tweet_id is not None:
        parts.extend(
            (
                campaign.quoted_tweet_id,
                f"https://x.com/i/status/{campaign.quoted_tweet_id}",
            )
        )
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class _LocatedClaim:
    claim: ClaimEvent
    miner_hotkey: str
    position: CommitmentPosition
    timestamp: datetime
    event_index: int

    @property
    def order(self) -> tuple[int, int, int, str]:
        return (
            self.position.block,
            self.position.extrinsic_index,
            self.event_index,
            self.claim.claim_id,
        )


@dataclass(frozen=True, slots=True)
class _LocatedSubmission:
    submission: SubmissionEvent
    position: CommitmentPosition
    timestamp: datetime
    event_index: int
    reveal: DraftReveal | None

    @property
    def order(self) -> tuple[int, int, int, str]:
        return (
            self.position.block,
            self.position.extrinsic_index,
            self.event_index,
            self.submission.submission_id,
        )


class CampaignReconciler:
    """Independently fetch tweets and apply protocol attribution at scoring close."""

    def __init__(
        self,
        store: ValidatorStore,
        x_provider: XProvider,
        qualification: QualificationChecker,
    ) -> None:
        self.store = store
        self._x = x_provider
        self._qualification = qualification
        self._qualification_cache: dict[tuple[str, int], bool] = {}

    async def reconcile_feed(
        self,
        feed: CampaignFeed,
        *,
        finalized_block: int,
    ) -> list[AttributionResult]:
        """Reconcile campaigns whose scoring close is finalized, preserving unavailable ones."""

        results: list[AttributionResult] = []
        for campaign in sorted(feed.campaigns, key=lambda item: item.access.campaign_id):
            reconciliation_block = (
                campaign.emission_start_block
                if campaign.emission_start_block is not None
                else campaign.access.scoring_close_block
            )
            if reconciliation_block > finalized_block:
                continue
            campaign_json = campaign.model_dump_json()
            campaign_results = self.store.reconciliation(
                feed.snapshot_id,
                campaign.access.campaign_id,
                campaign_json,
            )
            if campaign_results is None:
                try:
                    campaign_results = await self.reconcile_campaign(
                        campaign,
                        feed,
                        through_block=finalized_block,
                        defer_unavailable_tweets=True,
                    )
                except ReconciliationUnavailableError as exc:
                    LOGGER.warning(
                        "final reconciliation deferred campaign=%s block=%s error=%s",
                        campaign.access.campaign_id,
                        finalized_block,
                        exc,
                    )
                    continue
            self.store.persist_reconciliation(
                snapshot_id=feed.snapshot_id,
                campaign_id=campaign.access.campaign_id,
                campaign_json=campaign_json,
                results=campaign_results,
            )
            results.extend(campaign_results)
        return results

    async def reconcile_campaign(
        self,
        campaign: CampaignRecord,
        feed: CampaignFeed,
        *,
        through_block: int | None = None,
        defer_unavailable_tweets: bool = False,
    ) -> list[AttributionResult]:
        """Replay one campaign, optionally preserving unavailable tweets as pending."""

        records = self.store.verified_batches(
            through_block=(
                campaign.access.scoring_close_block if through_block is None else through_block
            )
        )
        claims: list[_LocatedClaim] = []
        submissions: list[_LocatedSubmission] = []
        for record in records:
            reveals = {reveal.claim_id: reveal for reveal in record.batch.reveals}
            for event_index, event in enumerate(record.batch.events):
                if event.campaign_id != campaign.access.campaign_id:
                    continue
                if isinstance(event, ClaimEvent):
                    claims.append(
                        _LocatedClaim(
                            claim=event,
                            miner_hotkey=record.batch.miner_hotkey,
                            position=record.position,
                            timestamp=record.timestamp,
                            event_index=event_index,
                        )
                    )
                else:
                    submissions.append(
                        _LocatedSubmission(
                            submission=event,
                            position=record.position,
                            timestamp=record.timestamp,
                            event_index=event_index,
                            reveal=reveals.get(event.claim_id) if event.claim_id else None,
                        )
                    )
        by_tweet: dict[str, list[_LocatedSubmission]] = defaultdict(list)
        for submission in submissions:
            by_tweet[submission.submission.tweet_id].append(submission)
        fetched: dict[str, Tweet | None] = {}
        unavailable_tweet_ids: set[str] = set()
        for tweet_id in sorted(by_tweet):
            if not any(
                item.position.block <= campaign.access.scoring_close_block
                for item in by_tweet[tweet_id]
            ):
                continue
            evidence = await self._x.fetch_tweet_by_id(tweet_id)
            if not evidence.provider_available:
                if defer_unavailable_tweets:
                    unavailable_tweet_ids.add(tweet_id)
                    LOGGER.warning(
                        "tweet evidence unavailable; deferring campaign=%s tweet_id=%s",
                        campaign.access.campaign_id,
                        tweet_id,
                    )
                    continue
                raise ReconciliationUnavailableError(f"X provider unavailable for tweet {tweet_id}")
            fetched[tweet_id] = evidence.tweet
        consumed: set[tuple[str, str]] = set()
        output: list[AttributionResult] = []

        def tweet_order(tweet_id: str) -> tuple[datetime, str]:
            value = fetched.get(tweet_id)
            return (value.created_at if value is not None else campaign.closes_at, tweet_id)

        ordered_tweets = sorted(by_tweet, key=tweet_order)
        for tweet_id in ordered_tweets:
            all_candidates = sorted(by_tweet[tweet_id], key=lambda item: item.order)
            exclusive_submission = self._exclusive_submission(campaign, all_candidates)
            if tweet_id in unavailable_tweet_ids:
                output.append(
                    self._pending_evidence(
                        tweet_id,
                        campaign,
                        submission=exclusive_submission,
                    )
                )
                continue
            candidates = [
                item
                for item in all_candidates
                if item.position.block <= campaign.access.scoring_close_block
            ]
            if not candidates:
                output.append(
                    self._reject(
                        tweet_id,
                        campaign,
                        AttributionReason.LATE_SUBMISSION,
                        submission=exclusive_submission,
                    )
                )
                continue
            tweet = fetched[tweet_id]
            if tweet is None:
                output.append(
                    self._reject(
                        tweet_id,
                        campaign,
                        AttributionReason.TWEET_NOT_FOUND,
                        submission=exclusive_submission,
                    )
                )
                continue
            try:
                campaign_failure = self._campaign_failure(campaign, feed, tweet)
                if campaign_failure is not None:
                    output.append(
                        self._reject(
                            tweet_id,
                            campaign,
                            campaign_failure,
                            submission=exclusive_submission,
                        )
                    )
                    continue
                if campaign.access.exclusive_miner_hotkey is not None:
                    output.append(
                        await self._exclusive(
                            campaign,
                            tweet,
                            candidates,
                            through_block=through_block,
                        )
                    )
                    continue
                result = await self._open(
                    campaign,
                    tweet,
                    candidates,
                    claims,
                    consumed,
                    through_block=through_block,
                )
            except ReconciliationUnavailableError as exc:
                if not defer_unavailable_tweets:
                    raise
                LOGGER.warning(
                    "tweet reconciliation unavailable; deferring campaign=%s tweet_id=%s error=%s",
                    campaign.access.campaign_id,
                    tweet_id,
                    exc,
                )
                output.append(
                    self._pending_evidence(
                        tweet_id,
                        campaign,
                        submission=exclusive_submission,
                    )
                )
                continue
            output.append(result)
            if result.accepted and result.miner_hotkey is not None and result.claim_id is not None:
                consumed.add((result.miner_hotkey, result.claim_id))
        return output

    def _campaign_failure(
        self, campaign: CampaignRecord, feed: CampaignFeed, tweet: Tweet
    ) -> AttributionReason | None:
        if not campaign.opens_at <= tweet.created_at <= campaign.closes_at:
            return AttributionReason.CAMPAIGN_INELIGIBLE
        ecosystems = tuple(
            ecosystem
            for pool in campaign.pools
            if (ecosystem := ecosystem_map_at(feed, pool, tweet.created_at)) is not None
        )
        if not ecosystems:
            raise ReconciliationUnavailableError(
                f"no ecosystem map active when tweet {tweet.tweet_id} was published"
            )
        if not any(
            tweet.author_x_id in eligible_creator_ids_in_map(ecosystem, campaign.max_members)
            for ecosystem in ecosystems
        ):
            return AttributionReason.CAMPAIGN_INELIGIBLE
        normalized = normalize_match_text(tweet.text)
        if any(normalize_match_text(term) not in normalized for term in campaign.required_terms):
            return AttributionReason.CAMPAIGN_INELIGIBLE
        if tweet.text.startswith("RT @") or tweet.in_reply_to_status_id is not None:
            return AttributionReason.CAMPAIGN_INELIGIBLE
        if campaign.tag is not None and campaign.tag.casefold() not in tweet.text.casefold():
            return AttributionReason.CAMPAIGN_INELIGIBLE
        if (
            campaign.quoted_tweet_id is not None
            and tweet.quoted_tweet_id != campaign.quoted_tweet_id
        ):
            return AttributionReason.CAMPAIGN_INELIGIBLE
        if campaign.inclusion_keywords and not any(
            keyword.casefold() in tweet.text.casefold() for keyword in campaign.inclusion_keywords
        ):
            return AttributionReason.CAMPAIGN_INELIGIBLE
        return None

    async def _exclusive(
        self,
        campaign: CampaignRecord,
        tweet: Tweet,
        submissions: list[_LocatedSubmission],
        *,
        through_block: int | None,
    ) -> AttributionResult:
        expected = campaign.access.exclusive_miner_hotkey
        candidate_seen = False
        rejected_candidate: SubmissionEvent | None = None
        pending_candidate: SubmissionEvent | None = None
        for located in reversed(submissions):
            event = located.submission
            if event.miner_hotkey != expected or event.claim_id is not None:
                continue
            candidate_seen = True
            if rejected_candidate is None:
                rejected_candidate = event
            if not await self._qualified(event.miner_hotkey, located.position.block):
                continue
            qualification_block = min(
                through_block or campaign.access.scoring_close_block,
                campaign.access.scoring_close_block,
            )
            if not await self._qualified(event.miner_hotkey, qualification_block):
                if (
                    pending_candidate is None
                    and through_block is not None
                    and through_block < campaign.access.scoring_close_block
                ):
                    pending_candidate = event
                continue
            return AttributionResult(
                tweet_id=tweet.tweet_id,
                campaign_id=campaign.access.campaign_id,
                accepted=True,
                reason=AttributionReason.ACCEPTED,
                miner_hotkey=event.miner_hotkey,
                submission_id=event.submission_id,
            )
        if pending_candidate is not None:
            return AttributionResult(
                tweet_id=tweet.tweet_id,
                campaign_id=campaign.access.campaign_id,
                accepted=False,
                reason=AttributionReason.MINER_NOT_QUALIFIED,
                pending=True,
                miner_hotkey=pending_candidate.miner_hotkey,
                submission_id=pending_candidate.submission_id,
            )
        return self._reject(
            tweet.tweet_id,
            campaign,
            (
                AttributionReason.MINER_NOT_QUALIFIED
                if candidate_seen
                else AttributionReason.WRONG_EXCLUSIVE_MINER
            ),
            submission=rejected_candidate,
        )

    async def _open(
        self,
        campaign: CampaignRecord,
        tweet: Tweet,
        submissions: list[_LocatedSubmission],
        claims: list[_LocatedClaim],
        consumed: set[tuple[str, str]],
        *,
        through_block: int | None,
    ) -> AttributionResult:
        if campaign.access.mining_protocol is not MiningProtocol.PRECLAIM_V2:
            return self._reject(tweet.tweet_id, campaign, AttributionReason.CAMPAIGN_INELIGIBLE)
        claims_by_id = {(item.miner_hotkey, item.claim.claim_id): item for item in claims}
        valid_by_miner: dict[str, MatchCandidate] = {}
        located_by_candidate: dict[tuple[str, str], _LocatedClaim] = {}
        submission_by_candidate: dict[tuple[str, str], SubmissionEvent] = {}
        failures: list[tuple[tuple[int, int, int, str], AttributionReason]] = []
        pending_qualification: dict[
            tuple[int, int, int, str], tuple[ClaimEvent, SubmissionEvent]
        ] = {}
        for located in submissions:
            event = located.submission
            claim = claims_by_id.get((event.miner_hotkey, event.claim_id or ""))
            if claim is None:
                failures.append((located.order, AttributionReason.CLAIM_NOT_ACTIVE))
                continue
            if (
                claim.claim.creator_x_id != tweet.author_x_id
                or claim.claim.campaign_id != event.campaign_id
            ):
                failures.append((located.order, AttributionReason.AUTHOR_MISMATCH))
                continue
            if claim.timestamp >= tweet.created_at:
                failures.append((located.order, AttributionReason.CLAIM_AFTER_PUBLICATION))
                continue
            claim_identity = (claim.miner_hotkey, claim.claim.claim_id)
            if claim_identity in consumed:
                failures.append((located.order, AttributionReason.CLAIM_NOT_ACTIVE))
                continue
            active = self._active_claim_identities(claims, claim, tweet.created_at, consumed)
            if claim_identity not in active:
                failures.append((located.order, AttributionReason.CLAIM_NOT_ACTIVE))
                continue
            if (
                located.reveal is None
                or located.reveal.commitment() != claim.claim.draft_commitment
            ):
                failures.append((located.order, AttributionReason.DRAFT_REVEAL_MISMATCH))
                continue
            if not await self._qualified(claim.miner_hotkey, claim.position.block):
                failures.append((located.order, AttributionReason.MINER_NOT_QUALIFIED))
                continue
            qualification_block = min(
                through_block or campaign.access.scoring_close_block,
                campaign.access.scoring_close_block,
            )
            if not await self._qualified(claim.miner_hotkey, qualification_block):
                failures.append((located.order, AttributionReason.MINER_NOT_QUALIFIED))
                if (
                    through_block is not None
                    and through_block < campaign.access.scoring_close_block
                ):
                    pending_qualification[located.order] = (claim.claim, event)
                continue
            candidate = MatchCandidate(
                miner_hotkey=claim.miner_hotkey,
                claim_id=claim.claim.claim_id,
                draft=located.reveal.draft,
            )
            valid_by_miner[claim.miner_hotkey] = candidate
            located_by_candidate[(candidate.miner_hotkey, candidate.claim_id)] = claim
            submission_by_candidate[(candidate.miner_hotkey, candidate.claim_id)] = event
        if not valid_by_miner:
            reason = max(failures, default=((0, 0, 0, ""), AttributionReason.CLAIM_NOT_ACTIVE))[1]
            latest_order = max(failures, default=((0, 0, 0, ""), reason))[0]
            if latest_order in pending_qualification:
                claim_event, event = pending_qualification[latest_order]
                return AttributionResult(
                    tweet_id=tweet.tweet_id,
                    campaign_id=campaign.access.campaign_id,
                    accepted=False,
                    reason=reason,
                    pending=True,
                    miner_hotkey=event.miner_hotkey,
                    submission_id=event.submission_id,
                    claim_id=claim_event.claim_id,
                )
            return self._reject(
                tweet.tweet_id,
                campaign,
                reason,
            )
        decision = choose_match(
            tweet.text,
            list(valid_by_miner.values()),
            public_text=_public_match_text(campaign),
        )
        if decision.winner is None:
            reason = AttributionReason(decision.reason)
            return AttributionResult(
                tweet_id=tweet.tweet_id,
                campaign_id=campaign.access.campaign_id,
                accepted=False,
                reason=reason,
                winner_score=decision.winner_score,
                runner_up_score=decision.runner_up_score,
            )
        winner = decision.winner
        winner_claim = located_by_candidate[(winner.miner_hotkey, winner.claim_id)]
        winner_submission = submission_by_candidate[(winner.miner_hotkey, winner.claim_id)]
        return AttributionResult(
            tweet_id=tweet.tweet_id,
            campaign_id=campaign.access.campaign_id,
            accepted=True,
            reason=AttributionReason.ACCEPTED,
            miner_hotkey=winner.miner_hotkey,
            submission_id=winner_submission.submission_id,
            claim_id=winner_claim.claim.claim_id,
            winner_score=decision.winner_score,
            runner_up_score=decision.runner_up_score,
        )

    @staticmethod
    def _active_claim_identities(
        claims: list[_LocatedClaim],
        target: _LocatedClaim,
        published_at: datetime,
        consumed: set[tuple[str, str]],
    ) -> set[tuple[str, str]]:
        active = sorted(
            (
                item
                for item in claims
                if item.miner_hotkey == target.miner_hotkey
                and item.claim.campaign_id == target.claim.campaign_id
                and item.claim.creator_x_id == target.claim.creator_x_id
                and item.timestamp < published_at
                and (item.miner_hotkey, item.claim.claim_id) not in consumed
            ),
            key=lambda item: item.order,
        )
        return {(item.miner_hotkey, item.claim.claim_id) for item in active[-5:]}

    async def _qualified(self, hotkey: str, block: int) -> bool:
        key = (hotkey, block)
        if key not in self._qualification_cache:
            self._qualification_cache[key] = await self._qualification.eligible(hotkey, block)
        return self._qualification_cache[key]

    @staticmethod
    def _exclusive_submission(
        campaign: CampaignRecord,
        submissions: list[_LocatedSubmission],
    ) -> SubmissionEvent | None:
        expected = campaign.access.exclusive_miner_hotkey
        if expected is None:
            return None
        return next(
            (
                located.submission
                for located in reversed(submissions)
                if located.submission.miner_hotkey == expected
                and located.submission.claim_id is None
            ),
            None,
        )

    @staticmethod
    def _reject(
        tweet_id: str,
        campaign: CampaignRecord,
        reason: AttributionReason,
        *,
        submission: SubmissionEvent | None = None,
    ) -> AttributionResult:
        return AttributionResult(
            tweet_id=tweet_id,
            campaign_id=campaign.access.campaign_id,
            accepted=False,
            reason=reason,
            miner_hotkey=submission.miner_hotkey if submission is not None else None,
            submission_id=submission.submission_id if submission is not None else None,
        )

    @staticmethod
    def _pending_evidence(
        tweet_id: str,
        campaign: CampaignRecord,
        *,
        submission: SubmissionEvent | None = None,
    ) -> AttributionResult:
        """Keep unavailable evidence explicit without turning it into rejection."""

        return AttributionResult(
            tweet_id=tweet_id,
            campaign_id=campaign.access.campaign_id,
            accepted=False,
            reason=AttributionReason.EVIDENCE_UNAVAILABLE,
            pending=True,
            miner_hotkey=submission.miner_hotkey if submission is not None else None,
            submission_id=submission.submission_id if submission is not None else None,
        )
