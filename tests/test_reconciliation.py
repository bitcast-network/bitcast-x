"""End-to-end attribution replay tests over verified validator history."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bitcast_x.campaigns import CampaignFeed, CampaignRecord, EcosystemMap, SocialAccount
from bitcast_x.chain import ChainCommitment
from bitcast_x.errors import ProtocolError, ReconciliationUnavailableError
from bitcast_x.protocol import (
    CREATOR_BINDING_ACTIVATION_BLOCK,
    AttributionReason,
    CampaignAccess,
    ClaimEvent,
    CommitmentEnvelope,
    CommittedBatch,
    DraftReveal,
    MiningProtocol,
    SubmissionEvent,
)
from bitcast_x.rewards import TweetReward
from bitcast_x.state import shadow_report
from bitcast_x.validator.publishing import ShadowResultPublisher
from bitcast_x.validator.reconciliation import CampaignReconciler
from bitcast_x.validator.rewards import RewardCoordinator
from bitcast_x.validator.scoring import AttributionScorer
from bitcast_x.validator.store import ValidatorStore
from bitcast_x.x_provider import EngagementFetch, Tweet, TweetFetch

MINER = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
OTHER_MINER = "5FHneW46xGXgs5mUiveU4sbTyGBzmst2jfFvCw9zThqAXhGK"
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class FakeQualification:
    def __init__(self, eligible: bool = True) -> None:
        self.value = eligible
        self.calls: list[int] = []

    async def eligible(self, _miner_hotkey: str, _block: int) -> bool:
        self.calls.append(_block)
        return self.value


class QualificationByBlock:
    def __init__(self, eligible_from: int) -> None:
        self.eligible_from = eligible_from
        self.calls: list[int] = []

    async def eligible(self, _miner_hotkey: str, block: int) -> bool:
        self.calls.append(block)
        return block >= self.eligible_from


class QualificationAtBlocks:
    def __init__(self, eligible_blocks: set[int]) -> None:
        self.eligible_blocks = eligible_blocks
        self.calls: list[int] = []

    async def eligible(self, _miner_hotkey: str, block: int) -> bool:
        self.calls.append(block)
        return block in self.eligible_blocks


class FakeX:
    def __init__(self, evidence: dict[str, TweetFetch]) -> None:
        self.evidence = evidence

    async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
        return self.evidence[tweet_id]

    async def fetch_engagements(self, _tweet_id: str) -> EngagementFetch:
        return EngagementFetch(engagements={}, provider_available=True)

    async def close(self) -> None:
        pass


class CapturingPublisher:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def publish(
        self,
        *,
        endpoint: str,
        payload_type: str,
        run_id: str,
        payload: dict[str, object],
    ) -> bool:
        assert endpoint == "https://ingestion.example/api/v1/brief-tweets"
        assert payload_type == "brief_tweets"
        assert run_id == "v3:snapshot:campaign"
        self.payloads.append(payload)
        return True


class MultiCampaignPublisher:
    def __init__(self) -> None:
        self.run_ids: list[str] = []
        self.payloads: list[dict[str, object]] = []

    async def publish(
        self,
        *,
        endpoint: str,
        payload_type: str,
        run_id: str,
        payload: dict[str, object],
    ) -> bool:
        assert endpoint == "https://ingestion.example/api/v1/brief-tweets"
        assert payload_type == "brief_tweets"
        self.run_ids.append(run_id)
        self.payloads.append(payload)
        return True


def campaign(
    *,
    exclusive: str | None = None,
    scoring_close_block: int = 20,
) -> CampaignRecord:
    return CampaignRecord(
        access=CampaignAccess(
            campaign_id="campaign",
            mechanism_id=1,
            mining_protocol=MiningProtocol.PRECLAIM_V2,
            scoring_close_block=scoring_close_block,
            exclusive_miner_hotkey=exclusive,
        ),
        title="Campaign",
        brief="Talk about the wallet in your own words",
        ecosystem_id="ecosystem",
        opens_at=NOW,
        closes_at=NOW + timedelta(days=1),
        reward_pool_usd="1000.00",
        required_terms=("#Launch",),
    )


def feed(record: CampaignRecord) -> CampaignFeed:
    return CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=(record,),
        ecosystem_maps=(
            EcosystemMap(
                ecosystem_id="ecosystem",
                name="Ecosystem",
                eligible_creator_x_ids=("456",),
                updated_at=NOW,
            ),
        ),
    )


def tweet(
    tweet_id: str = "999",
    *,
    created_at: datetime | None = None,
    text: str | None = None,
    quoted_tweet_id: str | None = None,
) -> Tweet:
    return Tweet(
        tweet_id=tweet_id,
        author_x_id="456",
        created_at=created_at or NOW + timedelta(minutes=10),
        text=text
        or (
            "I spent a week testing the wallet. Fast confirmations help, but the recovery "
            "flow is what won me over. #Launch"
        ),
        author="creator",
        quoted_tweet_id=quoted_tweet_id,
    )


def persist_batch(
    store: ValidatorStore,
    batch: CommittedBatch,
    *,
    block: int,
    timestamp: datetime,
) -> None:
    anchor = ChainCommitment(
        hotkey=batch.miner_hotkey,
        block=block,
        extrinsic_index=2,
        timestamp=timestamp,
        envelope=CommitmentEnvelope(
            sequence=batch.sequence,
            event_count=len(batch.events),
            batch_hash=bytes.fromhex(batch.batch_hash),
        ),
    )
    store.persist_block(block, [anchor])
    store.persist_verified(batch, anchor)


def open_history(
    path: Path,
    *,
    claim_timestamp: datetime = NOW + timedelta(minutes=1),
    draft: str | None = None,
    revealed_draft: str | None = None,
) -> ValidatorStore:
    store = ValidatorStore(path, start_block=10)
    claim_reveal = DraftReveal(
        claim_id="01" * 16,
        draft=draft
        or (
            "I spent a week testing the wallet — fast confirmations help, but the recovery "
            "flow is what won me over. #Launch"
        ),
        nonce="02" * 32,
    )
    submitted_reveal = (
        claim_reveal
        if revealed_draft is None
        else DraftReveal(
            claim_id=claim_reveal.claim_id,
            draft=revealed_draft,
            nonce=claim_reveal.nonce,
        )
    )
    claim = ClaimEvent(
        claim_id=claim_reveal.claim_id,
        campaign_id="campaign",
        creator_x_id="456",
        created_at=claim_timestamp,
        draft_commitment=claim_reveal.commitment(),
    )
    first = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(claim,),
    )
    submission = SubmissionEvent(
        submission_id="03" * 16,
        campaign_id="campaign",
        tweet_id="999",
        claim_id=claim.claim_id,
        miner_hotkey=MINER,
        creator_x_id="456",
    )
    second = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=2,
        previous_batch_hash=first.batch_hash,
        events=(submission,),
        reveals=(submitted_reveal,),
    )
    persist_batch(store, first, block=10, timestamp=claim_timestamp)
    persist_batch(store, second, block=11, timestamp=NOW + timedelta(minutes=20))
    return store


def miner_claim_history(
    *,
    hotkey: str,
    claim_id: str,
    draft: str,
    nonce: str,
    claim_block: int,
    submissions: tuple[tuple[int, str], ...],
    tweet_id: str = "999",
) -> list[tuple[int, CommittedBatch, datetime]]:
    """Build one miner's hash-linked claim and submission retry history."""

    reveal = DraftReveal(claim_id=claim_id, draft=draft, nonce=nonce)
    claim = ClaimEvent(
        claim_id=claim_id,
        campaign_id="campaign",
        creator_x_id="456",
        created_at=NOW + timedelta(minutes=1),
        draft_commitment=reveal.commitment(),
    )
    batch = CommittedBatch.create(
        miner_hotkey=hotkey,
        sequence=1,
        previous_batch_hash=None,
        events=(claim,),
    )
    history = [(claim_block, batch, NOW + timedelta(minutes=1))]
    for sequence, (block, submission_id) in enumerate(submissions, start=2):
        submission = SubmissionEvent(
            submission_id=submission_id,
            campaign_id="campaign",
            tweet_id=tweet_id,
            claim_id=claim_id,
            miner_hotkey=hotkey,
            creator_x_id="456",
        )
        next_batch = CommittedBatch.create(
            miner_hotkey=hotkey,
            sequence=sequence,
            previous_batch_hash=batch.batch_hash,
            events=(submission,),
            reveals=(reveal,),
        )
        history.append((block, next_batch, NOW + timedelta(minutes=20, seconds=sequence)))
        batch = next_batch
    return history


@pytest.mark.asyncio
async def test_open_campaign_attributes_independently_fetched_ordinary_edit(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    qualification = FakeQualification()
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        qualification,
    )

    results = await reconciler.reconcile_feed(feed(campaign()), finalized_block=20)

    assert len(results) == 1
    assert results[0].accepted is True
    assert results[0].miner_hotkey == MINER
    assert results[0].submission_id == "03" * 16
    assert results[0].claim_id == "01" * 16
    assert qualification.calls == [10, 20]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("campaign_updates", "public_only_text", "quoted_tweet_id"),
    [
        (
            {"display": "Public launch phrase", "brief": "Unrelated", "required_terms": ()},
            "Public launch phrase",
            None,
        ),
        (
            {"brief": "Unrelated", "required_terms": (), "tag": "#Launch"},
            "#Launch",
            None,
        ),
        (
            {
                "brief": "Unrelated",
                "required_terms": (),
                "inclusion_keywords": ("public-keyword",),
            },
            "public-keyword",
            None,
        ),
        (
            {"brief": "Unrelated", "required_terms": (), "quoted_tweet_id": "123"},
            "https://t.co/public-quote",
            "123",
        ),
    ],
)
async def test_public_campaign_material_alone_does_not_prove_draft_access(
    tmp_path: Path,
    campaign_updates: dict[str, object],
    public_only_text: str,
    quoted_tweet_id: str | None,
) -> None:
    store = open_history(tmp_path / "validator.sqlite3", draft=public_only_text)
    record = campaign().model_copy(update=campaign_updates)
    published = tweet(text=public_only_text, quoted_tweet_id=quoted_tweet_id)
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=published, provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(record, feed(record)))[0]

    assert result.accepted is False
    assert result.reason.value == "score_below_floor"
    assert result.winner_score == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_open_claim_unqualified_at_commitment_cannot_be_rescued_by_later_top_up(
    tmp_path: Path,
) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign()
    qualification = QualificationByBlock(18)
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        qualification,
    )

    result = (await reconciler.reconcile_campaign(record, feed(record), through_block=20))[0]

    assert result.accepted is False
    assert result.pending is False
    assert result.reason.value == "miner_not_qualified"
    assert qualification.calls == [10]


@pytest.mark.asyncio
async def test_open_preview_can_recover_at_close_only_if_claim_was_initially_qualified(
    tmp_path: Path,
) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign()
    qualification = QualificationAtBlocks({10, 20})
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        qualification,
    )

    pending = (await reconciler.reconcile_campaign(record, feed(record), through_block=15))[0]
    accepted = (await reconciler.reconcile_campaign(record, feed(record), through_block=20))[0]

    assert pending.accepted is False
    assert pending.pending is True
    assert pending.reason.value == "miner_not_qualified"
    assert pending.miner_hotkey == MINER
    assert pending.submission_id == "03" * 16
    assert pending.claim_id == "01" * 16
    assert accepted.accepted is True
    assert accepted.pending is False
    assert accepted.miner_hotkey == MINER
    assert qualification.calls == [10, 15, 20]


@pytest.mark.asyncio
async def test_unqualified_miner_is_finally_rejected_at_scoring_close(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign()
    qualification = QualificationAtBlocks({10})
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        qualification,
    )

    result = (await reconciler.reconcile_campaign(record, feed(record), through_block=20))[0]

    assert result.accepted is False
    assert result.pending is False
    assert result.reason.value == "miner_not_qualified"
    assert qualification.calls == [10, 20]


@pytest.mark.asyncio
async def test_claim_finalized_after_publication_is_rejected(tmp_path: Path) -> None:
    publication = NOW + timedelta(minutes=10)
    store = open_history(
        tmp_path / "validator.sqlite3",
        claim_timestamp=publication + timedelta(seconds=1),
    )
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(created_at=publication), provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(campaign(), feed(campaign())))[0]

    assert result.accepted is False
    assert result.reason.value == "claim_after_publication"


@pytest.mark.asyncio
async def test_changed_reveal_is_rejected_against_the_committed_open_claim(tmp_path: Path) -> None:
    store = open_history(
        tmp_path / "validator.sqlite3",
        revealed_draft="A different draft was revealed after publication. #Launch",
    )
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(campaign(), feed(campaign())))[0]

    assert result.accepted is False
    assert result.reason.value == "draft_reveal_mismatch"


@pytest.mark.asyncio
async def test_open_submission_without_a_committed_claim_is_rejected(tmp_path: Path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    submission = SubmissionEvent(
        submission_id="03" * 16,
        campaign_id="campaign",
        tweet_id="999",
        claim_id="04" * 16,
        miner_hotkey=MINER,
        creator_x_id="456",
    )
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(submission,),
    )
    persist_batch(store, batch, block=10, timestamp=NOW + timedelta(minutes=20))
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(campaign(), feed(campaign())))[0]

    assert result.accepted is False
    assert result.reason.value == "claim_not_active"


@pytest.mark.asyncio
async def test_eligible_tweet_author_must_match_the_open_claim(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign()
    snapshot = feed(record).model_copy(
        update={
            "ecosystem_maps": (
                EcosystemMap(
                    ecosystem_id="ecosystem",
                    name="Ecosystem",
                    eligible_creator_x_ids=("456", "789"),
                    updated_at=NOW,
                ),
            )
        }
    )
    other_author = tweet().model_copy(update={"author_x_id": "789", "author": "othercreator"})
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=other_author, provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(record, snapshot))[0]

    assert result.accepted is False
    assert result.reason.value == "author_mismatch"


@pytest.mark.asyncio
async def test_tweet_published_after_campaign_close_is_rejected(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    late_tweet = tweet(created_at=NOW + timedelta(days=1, seconds=1))
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=late_tweet, provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(campaign(), feed(campaign())))[0]

    assert result.accepted is False
    assert result.reason is AttributionReason.POST_OUTSIDE_CAMPAIGN_WINDOW


@pytest.mark.asyncio
async def test_exclusive_campaign_failure_preserves_submission_identity(tmp_path: Path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    submission = SubmissionEvent(
        submission_id="03" * 16,
        campaign_id="campaign",
        tweet_id="999",
        claim_id=None,
        miner_hotkey=MINER,
        creator_x_id="456",
    )
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(submission,),
    )
    persist_batch(store, batch, block=10, timestamp=NOW + timedelta(minutes=20))
    record = campaign(exclusive=MINER)
    evidence = tweet(created_at=NOW - timedelta(days=1))
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=evidence, provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(record, feed(record)))[0]

    assert result.accepted is False
    assert result.reason is AttributionReason.POST_OUTSIDE_CAMPAIGN_WINDOW
    assert result.miner_hotkey == MINER
    assert result.submission_id == "03" * 16


@pytest.mark.asyncio
async def test_late_submission_is_audited_without_fetching_x(tmp_path: Path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=21)
    submission = SubmissionEvent(
        submission_id="03" * 16,
        campaign_id="campaign",
        tweet_id="999",
        claim_id=None,
        miner_hotkey=MINER,
        creator_x_id="456",
    )
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(submission,),
    )
    persist_batch(store, batch, block=21, timestamp=NOW + timedelta(days=2))
    record = campaign(exclusive=MINER)
    reconciler = CampaignReconciler(store, FakeX({}), FakeQualification())

    result = (await reconciler.reconcile_campaign(record, feed(record), through_block=25))[0]

    assert result.accepted is False
    assert result.reason.value == "late_submission"
    assert result.miner_hotkey == MINER
    assert result.submission_id == "03" * 16


@pytest.mark.asyncio
async def test_campaign_freezes_only_after_its_reconciliation_window(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign().model_copy(update={"emission_start_block": 30, "emission_end_block": 40})
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        FakeQualification(),
    )

    before_emission = await reconciler.reconcile_feed(feed(record), finalized_block=29)
    at_emission = await reconciler.reconcile_feed(feed(record), finalized_block=30)

    assert before_emission == []
    assert len(at_emission) == 1
    assert at_emission[0].accepted is True


@pytest.mark.asyncio
async def test_exclusive_campaign_skips_claim_and_matcher(tmp_path: Path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    submission = SubmissionEvent(
        submission_id="03" * 16,
        campaign_id="campaign",
        tweet_id="999",
        claim_id=None,
        miner_hotkey=MINER,
        creator_x_id="456",
    )
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(submission,),
    )
    persist_batch(store, batch, block=10, timestamp=NOW + timedelta(minutes=20))
    record = campaign(exclusive=MINER)
    qualification = FakeQualification()
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        qualification,
    )

    result = (await reconciler.reconcile_campaign(record, feed(record)))[0]

    assert result.accepted is True
    assert result.claim_id is None
    assert result.miner_hotkey == MINER
    assert qualification.calls == [10, 20]


@pytest.mark.asyncio
@pytest.mark.parametrize("submitted_creator_x_id", [None, "789"])
async def test_exclusive_campaign_rejects_missing_or_wrong_submitter_identity_at_activation(
    tmp_path: Path,
    submitted_creator_x_id: str | None,
) -> None:
    store = ValidatorStore(
        tmp_path / "validator.sqlite3",
        start_block=CREATOR_BINDING_ACTIVATION_BLOCK,
    )
    submission = SubmissionEvent(
        version=2 if submitted_creator_x_id is None else 3,
        submission_id="03" * 16,
        campaign_id="campaign",
        tweet_id="999",
        claim_id=None,
        miner_hotkey=MINER,
        creator_x_id=submitted_creator_x_id,
    )
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(submission,),
    )
    persist_batch(
        store,
        batch,
        block=CREATOR_BINDING_ACTIVATION_BLOCK,
        timestamp=NOW + timedelta(minutes=20),
    )
    record = campaign(
        exclusive=MINER,
        scoring_close_block=CREATOR_BINDING_ACTIVATION_BLOCK + 1,
    )
    qualification = FakeQualification()
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        qualification,
    )

    result = (await reconciler.reconcile_campaign(record, feed(record)))[0]

    assert result.accepted is False
    assert result.reason is AttributionReason.AUTHOR_MISMATCH
    assert result.miner_hotkey == MINER
    assert result.submission_id == submission.submission_id
    assert qualification.calls == []


@pytest.mark.asyncio
async def test_exclusive_campaign_accepts_legacy_submission_before_activation(
    tmp_path: Path,
) -> None:
    store = ValidatorStore(
        tmp_path / "validator.sqlite3",
        start_block=CREATOR_BINDING_ACTIVATION_BLOCK - 1,
    )
    submission = SubmissionEvent(
        version=2,
        submission_id="03" * 16,
        campaign_id="campaign",
        tweet_id="999",
        claim_id=None,
        miner_hotkey=MINER,
    )
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(submission,),
    )
    persist_batch(
        store,
        batch,
        block=CREATOR_BINDING_ACTIVATION_BLOCK - 1,
        timestamp=NOW + timedelta(minutes=20),
    )
    record = campaign(
        exclusive=MINER,
        scoring_close_block=CREATOR_BINDING_ACTIVATION_BLOCK + 1,
    )
    qualification = FakeQualification()
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        qualification,
    )

    result = (await reconciler.reconcile_campaign(record, feed(record)))[0]

    assert result.accepted is True
    assert result.miner_hotkey == MINER
    assert result.submission_id == submission.submission_id
    assert qualification.calls == [
        CREATOR_BINDING_ACTIVATION_BLOCK - 1,
        CREATOR_BINDING_ACTIVATION_BLOCK + 1,
    ]


@pytest.mark.asyncio
async def test_exclusive_campaign_rejects_a_different_miner(tmp_path: Path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    submission = SubmissionEvent(
        submission_id="03" * 16,
        campaign_id="campaign",
        tweet_id="999",
        claim_id=None,
        miner_hotkey=MINER,
        creator_x_id="456",
    )
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(submission,),
    )
    persist_batch(store, batch, block=10, timestamp=NOW + timedelta(minutes=20))
    record = campaign(exclusive=OTHER_MINER)
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(record, feed(record)))[0]

    assert result.accepted is False
    assert result.reason.value == "wrong_exclusive_miner"


@pytest.mark.asyncio
async def test_exclusive_submission_unqualified_at_commitment_cannot_be_rescued(
    tmp_path: Path,
) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    submission = SubmissionEvent(
        submission_id="03" * 16,
        campaign_id="campaign",
        tweet_id="999",
        claim_id=None,
        miner_hotkey=MINER,
        creator_x_id="456",
    )
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(submission,),
    )
    persist_batch(store, batch, block=10, timestamp=NOW + timedelta(minutes=20))
    record = campaign(exclusive=MINER)
    qualification = QualificationByBlock(18)
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        qualification,
    )

    result = (await reconciler.reconcile_campaign(record, feed(record), through_block=20))[0]

    assert result.accepted is False
    assert result.pending is False
    assert result.reason.value == "miner_not_qualified"
    assert result.miner_hotkey == MINER
    assert result.submission_id == "03" * 16
    assert qualification.calls == [10]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("victim_blocks", "attacker_blocks"),
    [
        ((10, 12, 14), (11, 13)),
        ((11, 13, 14), (10, 12)),
    ],
)
async def test_claim_ids_are_namespaced_by_miner_across_order_and_retry(
    tmp_path: Path,
    victim_blocks: tuple[int, int, int],
    attacker_blocks: tuple[int, int],
) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    claim_id = "01" * 16
    history = miner_claim_history(
        hotkey=MINER,
        claim_id=claim_id,
        draft=tweet().text,
        nonce="02" * 32,
        claim_block=victim_blocks[0],
        submissions=(
            (victim_blocks[1], "03" * 16),
            (victim_blocks[2], "05" * 16),
        ),
    )
    history.extend(
        miner_claim_history(
            hotkey=OTHER_MINER,
            claim_id=claim_id,
            draft="Talk about the wallet. #Launch",
            nonce="04" * 32,
            claim_block=attacker_blocks[0],
            submissions=((attacker_blocks[1], "04" * 16),),
        )
    )
    for block, batch, timestamp in sorted(history, key=lambda item: item[0]):
        persist_batch(store, batch, block=block, timestamp=timestamp)
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(campaign(), feed(campaign())))[0]

    assert result.accepted is True
    assert result.miner_hotkey == MINER
    assert result.claim_id == claim_id
    assert result.submission_id == "05" * 16


@pytest.mark.asyncio
async def test_consuming_claim_id_for_one_miner_does_not_consume_another_miners_claim(
    tmp_path: Path,
) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    claim_id = "01" * 16
    history = miner_claim_history(
        hotkey=MINER,
        claim_id=claim_id,
        draft=tweet("999").text,
        nonce="02" * 32,
        claim_block=10,
        submissions=((12, "03" * 16),),
        tweet_id="999",
    )
    history.extend(
        miner_claim_history(
            hotkey=OTHER_MINER,
            claim_id=claim_id,
            draft=tweet("998").text,
            nonce="04" * 32,
            claim_block=11,
            submissions=((13, "04" * 16),),
            tweet_id="998",
        )
    )
    for block, batch, timestamp in sorted(history, key=lambda item: item[0]):
        persist_batch(store, batch, block=block, timestamp=timestamp)
    reconciler = CampaignReconciler(
        store,
        FakeX(
            {
                "998": TweetFetch(tweet=tweet("998"), provider_available=True),
                "999": TweetFetch(tweet=tweet("999"), provider_available=True),
            }
        ),
        FakeQualification(),
    )

    results = await reconciler.reconcile_campaign(campaign(), feed(campaign()))

    assert {(item.tweet_id, item.miner_hotkey) for item in results} == {
        ("998", OTHER_MINER),
        ("999", MINER),
    }


def test_legacy_null_language_placeholder_preserves_frozen_campaign_replay(
    tmp_path: Path,
) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    record = campaign()
    legacy_payload = json.loads(record.model_dump_json())
    legacy_payload["language"] = None
    legacy_json = json.dumps(legacy_payload, sort_keys=True)
    current_json = record.model_dump_json()

    store.persist_reconciliation(
        snapshot_id="snapshot",
        campaign_id="campaign",
        campaign_json=legacy_json,
        results=[],
    )
    store.persist_reconciliation(
        snapshot_id="snapshot",
        campaign_id="campaign",
        campaign_json=current_json,
        results=[],
    )
    store.persist_campaign_rewards(
        snapshot_id="snapshot",
        campaign_id="campaign",
        campaign_json=legacy_json,
        rewards=[],
        decisions=[],
    )
    store.persist_campaign_rewards(
        snapshot_id="snapshot",
        campaign_id="campaign",
        campaign_json=current_json,
        rewards=[],
        decisions=[],
    )

    assert store.reconciliation("snapshot-2", "campaign", current_json) == []
    assert store.reconciled_campaigns() == []
    assert store.campaign_rewards("campaign", current_json) is None
    assert store.campaign_finalized("campaign") is False


@pytest.mark.asyncio
async def test_provider_outage_is_pending_in_final_feed(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign()
    snapshot = feed(record)
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=None, provider_available=False)}),
        FakeQualification(),
    )

    with pytest.raises(ReconciliationUnavailableError, match="provider unavailable"):
        await reconciler.reconcile_campaign(record, snapshot)

    results = await reconciler.reconcile_feed(snapshot, finalized_block=20)

    assert len(results) == 1
    result = results[0]
    assert result.pending is True
    assert result.reason is AttributionReason.EVIDENCE_UNAVAILABLE
    assert (
        store.reconciliation(
            snapshot.snapshot_id,
            record.access.campaign_id,
            record.model_dump_json(),
        )
        == results
    )
    assert len(store.verified_batches()) == 2


@pytest.mark.asyncio
async def test_authoritative_tweet_absence_freezes_a_rejection(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign()
    snapshot = feed(record)
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=None, provider_available=True)}),
        FakeQualification(),
    )

    results = await reconciler.reconcile_feed(snapshot, finalized_block=20)

    assert len(results) == 1
    result = results[0]
    assert result.accepted is False
    assert result.reason.value == "tweet_not_found"
    assert (
        store.reconciliation(
            snapshot.snapshot_id,
            record.access.campaign_id,
            record.model_dump_json(),
        )
        == results
    )


def _exclusive_final_campaign(campaign_id: str) -> CampaignRecord:
    record = campaign(exclusive=MINER)
    return record.model_copy(
        update={
            "access": record.access.model_copy(update={"campaign_id": campaign_id}),
            "emission_start_block": 30,
            "emission_end_block": 40,
        }
    )


def _two_campaign_finalization(
    tmp_path: Path,
) -> tuple[ValidatorStore, CampaignFeed, CampaignRecord, CampaignRecord]:
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    campaign_a = _exclusive_final_campaign("campaign-a")
    campaign_b = _exclusive_final_campaign("campaign-b")
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(
            SubmissionEvent(
                submission_id="03" * 16,
                campaign_id="campaign-a",
                tweet_id="998",
                claim_id=None,
                miner_hotkey=MINER,
                creator_x_id="456",
            ),
            SubmissionEvent(
                submission_id="04" * 16,
                campaign_id="campaign-b",
                tweet_id="999",
                claim_id=None,
                miner_hotkey=MINER,
                creator_x_id="456",
            ),
        ),
    )
    persist_batch(store, batch, block=10, timestamp=NOW + timedelta(minutes=20))
    snapshot = feed(campaign_a).model_copy(
        update={
            "campaigns": (campaign_a, campaign_b),
            "ecosystem_maps": (
                EcosystemMap(
                    ecosystem_id="ecosystem",
                    name="Ecosystem",
                    eligible_creator_x_ids=("456",),
                    updated_at=NOW,
                    accounts=(SocialAccount(x_id="456", username="creator", influence=10.0),),
                ),
            ),
        }
    )
    return store, snapshot, campaign_a, campaign_b


@pytest.mark.asyncio
async def test_unavailable_tweet_does_not_block_its_campaign_rewards(tmp_path: Path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    record = _exclusive_final_campaign("campaign")
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(
            SubmissionEvent(
                submission_id="03" * 16,
                campaign_id="campaign",
                tweet_id="998",
                claim_id=None,
                miner_hotkey=MINER,
                creator_x_id="456",
            ),
            SubmissionEvent(
                submission_id="04" * 16,
                campaign_id="campaign",
                tweet_id="999",
                claim_id=None,
                miner_hotkey=MINER,
                creator_x_id="456",
            ),
        ),
    )
    persist_batch(store, batch, block=10, timestamp=NOW + timedelta(minutes=20))
    snapshot = feed(record).model_copy(
        update={
            "ecosystem_maps": (
                EcosystemMap(
                    ecosystem_id="ecosystem",
                    name="Ecosystem",
                    eligible_creator_x_ids=("456",),
                    updated_at=NOW,
                    accounts=(SocialAccount(x_id="456", username="creator", influence=10.0),),
                ),
            )
        }
    )
    provider = FakeX(
        {
            "998": TweetFetch(tweet=None, provider_available=False),
            "999": TweetFetch(tweet=tweet("999"), provider_available=True),
        }
    )
    attributions = await CampaignReconciler(
        store,
        provider,
        FakeQualification(),
    ).reconcile_feed(snapshot, finalized_block=30)
    coordinator = RewardCoordinator(store, AttributionScorer(provider))

    scored = await coordinator.freeze_scores(snapshot, attributions)
    weights, floors = coordinator.shadow_weights(
        snapshot,
        scored,
        block=35,
        hotkey_to_uid={MINER: 7},
        uids=[0, 7],
        persist=False,
    )

    results_by_tweet = {item.tweet_id: item for item in attributions}
    assert results_by_tweet["998"].pending is True
    assert results_by_tweet["998"].reason is AttributionReason.EVIDENCE_UNAVAILABLE
    assert results_by_tweet["999"].accepted is True
    assert [item.tweet_id for item in floors] == ["999"]
    assert floors[0].daily_usd_floor == pytest.approx(1000 / 7)
    assert weights == {0: 0.0, 7: 1.0}
    assert coordinator.pending_reward_campaign_ids(snapshot, block=35) == ()


@pytest.mark.asyncio
async def test_finalization_isolates_an_unavailable_tweet(tmp_path: Path) -> None:
    store, snapshot, campaign_a, campaign_b = _two_campaign_finalization(tmp_path)
    provider = FakeX(
        {
            "998": TweetFetch(tweet=None, provider_available=False),
            "999": TweetFetch(tweet=tweet("999"), provider_available=True),
        }
    )
    reconciler = CampaignReconciler(store, provider, FakeQualification())

    attributions = await reconciler.reconcile_feed(snapshot, finalized_block=30)
    coordinator = RewardCoordinator(store, AttributionScorer(provider))
    scored = await coordinator.freeze_scores(snapshot, attributions)
    weights, floors = coordinator.shadow_weights(
        snapshot,
        scored,
        block=35,
        hotkey_to_uid={MINER: 7},
        uids=[0, 7],
        persist=False,
    )
    publisher = MultiCampaignPublisher()
    published = await ShadowResultPublisher(
        store,
        publisher,  # type: ignore[arg-type]
        endpoint="https://ingestion.example/api/v1/brief-tweets",
    ).publish(snapshot, scored, floors, block=35, hotkey_to_uid={MINER: 7})

    assert [item.campaign_id for item in attributions] == ["campaign-a", "campaign-b"]
    campaign_a_results = store.reconciliation(
        snapshot.snapshot_id,
        "campaign-a",
        campaign_a.model_dump_json(),
    )
    assert campaign_a_results is not None
    assert campaign_a_results[0].pending is True
    assert campaign_a_results[0].reason is AttributionReason.EVIDENCE_UNAVAILABLE
    assert campaign_a_results[0].miner_hotkey == MINER
    assert campaign_a_results[0].submission_id == "03" * 16
    assert (
        store.reconciliation(
            snapshot.snapshot_id,
            "campaign-b",
            campaign_b.model_dump_json(),
        )
        is not None
    )
    assert weights == {0: 0.0, 7: 1.0}
    assert coordinator.pending_reward_campaign_ids(snapshot, block=35) == ()
    assert store.campaign_rewards("campaign-a", campaign_a.model_dump_json()) is None
    assert store.campaign_rewards("campaign-b", campaign_b.model_dump_json()) is not None
    assert published == 1
    assert len(publisher.run_ids) == 2
    assert publisher.run_ids[0].startswith("v3-preview:snapshot:campaign-a:")
    assert publisher.run_ids[1] == "v3:snapshot:campaign-b"
    campaign_a_decision = publisher.payloads[0]["attribution_decisions"][0]  # type: ignore[index]
    assert campaign_a_decision["status"] == "pending"
    assert campaign_a_decision["reason"] == "evidence_unavailable"
    assert campaign_a_decision["reward_status"] == "pending"


@pytest.mark.asyncio
async def test_final_scoring_isolates_an_unavailable_tweet(tmp_path: Path) -> None:
    store, snapshot, campaign_a, campaign_b = _two_campaign_finalization(tmp_path)

    class SelectiveEngagementProvider(FakeX):
        async def fetch_engagements(self, tweet_id: str) -> EngagementFetch:
            return EngagementFetch(
                engagements={},
                provider_available=tweet_id != "998",
            )

    provider = SelectiveEngagementProvider(
        {
            "998": TweetFetch(tweet=tweet("998"), provider_available=True),
            "999": TweetFetch(tweet=tweet("999"), provider_available=True),
        }
    )
    attributions = await CampaignReconciler(
        store,
        provider,
        FakeQualification(),
    ).reconcile_feed(snapshot, finalized_block=30)
    coordinator = RewardCoordinator(store, AttributionScorer(provider))

    scored = await coordinator.freeze_scores(snapshot, attributions)
    coordinator.shadow_weights(
        snapshot,
        scored,
        block=35,
        hotkey_to_uid={MINER: 7},
        uids=[0, 7],
        persist=False,
    )

    assert [item.attribution.campaign_id for item in scored] == ["campaign-b"]
    assert store.scored_reconciliation(snapshot.snapshot_id, "campaign-a") == []
    assert store.scored_reconciliation(snapshot.snapshot_id, "campaign-b") is not None
    assert coordinator.pending_reward_campaign_ids(snapshot, block=35) == ()
    assert store.campaign_rewards("campaign-a", campaign_a.model_dump_json()) is None
    assert store.campaign_rewards("campaign-b", campaign_b.model_dump_json()) is not None


@pytest.mark.asyncio
async def test_preview_defers_only_the_tweet_with_unavailable_evidence(tmp_path: Path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(
            SubmissionEvent(
                submission_id="03" * 16,
                campaign_id="campaign",
                tweet_id="998",
                claim_id=None,
                miner_hotkey=MINER,
                creator_x_id="456",
            ),
            SubmissionEvent(
                submission_id="04" * 16,
                campaign_id="campaign",
                tweet_id="999",
                claim_id=None,
                miner_hotkey=MINER,
                creator_x_id="456",
            ),
        ),
    )
    persist_batch(store, batch, block=10, timestamp=NOW + timedelta(minutes=20))
    record = campaign(exclusive=MINER)
    reconciler = CampaignReconciler(
        store,
        FakeX(
            {
                "998": TweetFetch(tweet=None, provider_available=False),
                "999": TweetFetch(tweet=tweet("999"), provider_available=True),
            }
        ),
        FakeQualification(),
    )

    results = await reconciler.reconcile_campaign(
        record,
        feed(record),
        through_block=15,
        defer_unavailable_tweets=True,
    )

    assert [item.tweet_id for item in results] == ["999", "998"]
    assert results[0].accepted is True
    assert results[0].submission_id == "04" * 16
    assert results[1].pending is True
    assert results[1].reason is AttributionReason.EVIDENCE_UNAVAILABLE
    assert results[1].miner_hotkey == MINER
    assert results[1].submission_id == "03" * 16


@pytest.mark.asyncio
async def test_eligibility_remains_after_creator_drops_below_rank_cutoff(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign().model_copy(update={"max_members": 1})
    snapshot = feed(record).model_copy(
        update={
            "ecosystem_maps": (
                EcosystemMap(
                    ecosystem_id="ecosystem",
                    name="Initially eligible",
                    eligible_creator_x_ids=("123", "456"),
                    updated_at=NOW,
                    accounts=(
                        SocialAccount(x_id="456", username="creator", influence=2.0),
                        SocialAccount(x_id="123", username="leader", influence=1.0),
                    ),
                ),
                EcosystemMap(
                    ecosystem_id="ecosystem",
                    name="Rank dropped",
                    eligible_creator_x_ids=("123", "456"),
                    updated_at=NOW + timedelta(hours=1),
                    accounts=(
                        SocialAccount(x_id="123", username="leader", influence=2.0),
                        SocialAccount(x_id="456", username="creator", influence=1.0),
                    ),
                ),
            )
        }
    )
    reconciler = CampaignReconciler(
        store,
        FakeX(
            {
                "999": TweetFetch(
                    tweet=tweet(created_at=NOW + timedelta(hours=2)),
                    provider_available=True,
                )
            }
        ),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(record, snapshot))[0]

    assert result.accepted is True


@pytest.mark.asyncio
async def test_rank_cutoff_rejects_explicit_map_member_below_top_n(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign().model_copy(update={"max_members": 1})
    snapshot = feed(record).model_copy(
        update={
            "ecosystem_maps": (
                EcosystemMap(
                    ecosystem_id="ecosystem",
                    name="Active map",
                    eligible_creator_x_ids=("123", "456"),
                    updated_at=NOW,
                    accounts=(
                        SocialAccount(x_id="123", username="leader", influence=2.0),
                        SocialAccount(x_id="456", username="creator", influence=1.0),
                    ),
                ),
            )
        }
    )
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(record, snapshot))[0]

    assert result.accepted is False
    assert result.reason is AttributionReason.CREATOR_NOT_ELIGIBLE_FOR_CAMPAIGN


@pytest.mark.asyncio
async def test_missing_historical_map_keeps_campaign_unreconciled(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign()
    snapshot = feed(record).model_copy(
        update={
            "ecosystem_maps": (
                EcosystemMap(
                    ecosystem_id="ecosystem",
                    name="After campaign",
                    eligible_creator_x_ids=("456",),
                    updated_at=NOW + timedelta(days=2),
                ),
            )
        }
    )
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        FakeQualification(),
    )

    with pytest.raises(ReconciliationUnavailableError, match="no ecosystem map overlaps"):
        await reconciler.reconcile_campaign(record, snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("campaign_update", "tweet_update", "expected_reason"),
    [
        (
            {"required_terms": ("required phrase",)},
            {},
            AttributionReason.REQUIRED_TERMS_MISSING,
        ),
        ({}, {"text": "RT @someone: #Launch wallet"}, AttributionReason.RETWEET_NOT_ALLOWED),
        ({}, {"in_reply_to_status_id": "1"}, AttributionReason.REPLY_NOT_ALLOWED),
        ({"tag": "@bitcast"}, {}, AttributionReason.CAMPAIGN_TAG_MISSING),
        (
            {"quoted_tweet_id": "123"},
            {"quoted_tweet_id": "456"},
            AttributionReason.REQUIRED_QUOTE_MISSING_OR_INCORRECT,
        ),
        (
            {"inclusion_keywords": ("airdrop", "rewards")},
            {},
            AttributionReason.REQUIRED_CAMPAIGN_KEYWORD_MISSING,
        ),
    ],
)
async def test_v2_content_prefilters_reject_ineligible_submissions(
    tmp_path: Path,
    campaign_update: dict[str, object],
    tweet_update: dict[str, object],
    expected_reason: AttributionReason,
) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign().model_copy(update=campaign_update)
    evidence = tweet().model_copy(update=tweet_update)
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=evidence, provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(record, feed(record)))[0]

    assert result.accepted is False
    assert result.reason is expected_reason


@pytest.mark.asyncio
async def test_campaign_freeze_survives_feed_snapshot_rotation_and_rejects_mutation(
    tmp_path: Path,
) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign()
    provider = FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)})
    reconciler = CampaignReconciler(store, provider, FakeQualification())

    first = await reconciler.reconcile_feed(feed(record), finalized_block=20)
    store.persist_campaign_rewards(
        snapshot_id="snapshot",
        campaign_id=record.access.campaign_id,
        campaign_json=record.model_dump_json(),
        rewards=[
            TweetReward(
                campaign_id=record.access.campaign_id,
                tweet_id="999",
                creator_x_id="456",
                miner_hotkey=MINER,
                score=1.0,
                daily_usd_floor=1.0,
            )
        ],
        decisions=[],
    )
    rotated = feed(record).model_copy(update={"snapshot_id": "snapshot-2"})
    replay = await reconciler.reconcile_feed(rotated, finalized_block=20)

    assert replay == first
    changed = record.model_copy(update={"display": "Mutated after close"})
    with pytest.raises(ProtocolError, match="changed after frozen reconciliation"):
        await reconciler.reconcile_feed(
            feed(changed).model_copy(update={"snapshot_id": "snapshot-3"}),
            finalized_block=20,
        )


@pytest.mark.asyncio
async def test_verified_history_reaches_frozen_weights_and_shadow_publication(
    tmp_path: Path,
) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign().model_copy(update={"emission_start_block": 30, "emission_end_block": 40})
    snapshot = feed(record).model_copy(
        update={
            "ecosystem_maps": (
                EcosystemMap(
                    ecosystem_id="ecosystem",
                    name="Ecosystem",
                    eligible_creator_x_ids=("456",),
                    updated_at=NOW,
                    accounts=(SocialAccount(x_id="456", username="creator", influence=10.0),),
                ),
            )
        }
    )
    provider = FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)})
    attributions = await CampaignReconciler(
        store,
        provider,
        FakeQualification(),
    ).reconcile_feed(snapshot, finalized_block=30)
    coordinator = RewardCoordinator(store, AttributionScorer(provider))

    scored = await coordinator.freeze_scores(snapshot, attributions)
    weights, floors = coordinator.shadow_weights(
        snapshot,
        scored,
        block=35,
        hotkey_to_uid={MINER: 7},
        uids=[0, 7],
    )
    publisher = CapturingPublisher()
    published = await ShadowResultPublisher(
        store,
        publisher,  # type: ignore[arg-type]
        endpoint="https://ingestion.example/api/v1/brief-tweets",
    ).publish(snapshot, scored, floors, block=35, hotkey_to_uid={MINER: 7})

    assert attributions[0].accepted is True
    assert scored[0].score == 20.0
    assert weights == {0: 0.0, 7: 1.0}
    assert len(floors) == 1
    assert published == 1
    assert publisher.payloads[0]["brief_id"] == "campaign"
    decisions = publisher.payloads[0]["attribution_decisions"]
    assert isinstance(decisions, list)
    assert decisions[0]["reward_status"] == "rewarded"
    assert decisions[0]["reward_reason"] == "accepted"
    assert decisions[0]["daily_usd_floor"] == floors[0].daily_usd_floor
    assert store.publication_succeeded("snapshot", "campaign") is True


@pytest.mark.asyncio
async def test_independent_restarted_validators_produce_identical_full_shadow_reports(
    tmp_path: Path,
) -> None:
    reports: list[dict[str, object]] = []
    for operator in ("validator-a", "validator-b"):
        state_dir = tmp_path / operator
        path = state_dir / "validator.sqlite3"
        open_history(path)
        store = ValidatorStore(path, start_block=999)
        record = campaign().model_copy(
            update={"emission_start_block": 30, "emission_end_block": 40}
        )
        snapshot = feed(record).model_copy(
            update={
                "ecosystem_maps": (
                    EcosystemMap(
                        ecosystem_id="ecosystem",
                        name="Ecosystem",
                        eligible_creator_x_ids=("456",),
                        updated_at=NOW,
                        accounts=(SocialAccount(x_id="456", username="creator", influence=10.0),),
                    ),
                )
            }
        )
        provider = FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)})
        attributions = await CampaignReconciler(
            store,
            provider,
            FakeQualification(),
        ).reconcile_feed(snapshot, finalized_block=30)
        coordinator = RewardCoordinator(store, AttributionScorer(provider))
        scored = await coordinator.freeze_scores(snapshot, attributions)
        weights, _floors = coordinator.shadow_weights(
            snapshot,
            scored,
            block=35,
            hotkey_to_uid={MINER: 7},
            uids=[0, 7],
        )

        assert weights == {0: 0.0, 7: 1.0}
        reports.append(shadow_report(state_dir))

    assert reports[0] == reports[1]
    assert reports[0]["campaigns_frozen"] == 1
    assert reports[0]["shadow_blocks"] == 1
