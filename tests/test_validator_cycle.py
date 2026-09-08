"""Exercise economic output gating through a complete offline validator cycle."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bitcast_x.campaigns import CampaignFeed, CampaignRecord
from bitcast_x.config import Settings
from bitcast_x.protocol import CampaignAccess, MiningProtocol
from bitcast_x.rewards import TweetReward
from bitcast_x.state import shadow_report
from bitcast_x.validator import service
from bitcast_x.validator.store import ValidatorStore

HOTKEY = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
BLOCK = 10_000_000
NOW = datetime(2026, 8, 5, tzinfo=UTC)


def campaign(campaign_id: str, protocol: MiningProtocol) -> CampaignRecord:
    return CampaignRecord(
        access=CampaignAccess(
            campaign_id=campaign_id,
            mechanism_id=1,
            mining_protocol=protocol,
            scoring_close_block=BLOCK - 10,
        ),
        title=campaign_id,
        brief="brief",
        ecosystem_id="eco",
        opens_at=NOW,
        closes_at=NOW + timedelta(days=1),
        reward_pool_usd="700",
        emission_start_block=BLOCK - 5,
        emission_end_block=BLOCK + 5,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["empty", "preclaim", "pending_preclaim", "legacy", "mixed", "frozen_legacy"]
)
async def test_cycle_preserves_preclaim_outputs_and_rejects_legacy(
    case: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    preclaim = campaign("preclaim", MiningProtocol.PRECLAIM_V2)
    legacy = campaign("legacy", MiningProtocol.LEGACY_CONNECTION)
    records = {
        "empty": (),
        "preclaim": (preclaim,),
        "pending_preclaim": (preclaim,),
        "legacy": (legacy,),
        "mixed": (preclaim, legacy),
        "frozen_legacy": (preclaim,),
    }[case]
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    if case in {"preclaim", "mixed", "frozen_legacy"}:
        frozen = preclaim
        if case == "frozen_legacy":
            frozen = preclaim.model_copy(
                update={
                    "access": preclaim.access.model_copy(
                        update={"mining_protocol": MiningProtocol.LEGACY_CONNECTION}
                    )
                }
            )
        store.bind_campaign_protocols((frozen,))
        store.persist_reconciliation(
            snapshot_id="old",
            campaign_id="preclaim",
            campaign_json=frozen.model_dump_json(),
            results=[],
        )
        store.persist_scores("old", "preclaim", [])
        store.persist_campaign_rewards(
            snapshot_id="old",
            campaign_id="preclaim",
            campaign_json=frozen.model_dump_json(),
            rewards=[
                TweetReward(
                    campaign_id="preclaim",
                    tweet_id="123",
                    creator_x_id="1",
                    miner_hotkey=HOTKEY,
                    score=1,
                    daily_usd_floor=100,
                )
            ],
            decisions=[],
        )
    before = shadow_report(tmp_path)
    archive = tmp_path / "connections.db"
    archive.write_bytes(b"historical archive must not be opened or modified")
    ops = SimpleNamespace(started=True, should_exit=False, serve=AsyncMock())
    feed = CampaignFeed(
        snapshot_id="new",
        published_at=NOW,
        campaigns=records,
        ecosystem_maps=(),
    )

    async def fetch() -> CampaignFeed:
        ops.should_exit = True  # Finish after this one complete cycle.
        return feed

    graph = SimpleNamespace(
        neurons=[
            SimpleNamespace(uid=0, hotkey="burn"),
            SimpleNamespace(uid=7, hotkey=HOTKEY),
        ]
    )
    chain = SimpleNamespace(
        current_block=AsyncMock(return_value=BLOCK),
        metagraph=AsyncMock(return_value=graph),
        close=AsyncMock(),
    )
    ingestor = SimpleNamespace(
        discover=AsyncMock(return_value=[]),
        reconcile_all=AsyncMock(return_value=[]),
    )
    reconciler = SimpleNamespace(
        reconcile_feed=AsyncMock(return_value=[]),
        completed_campaign_ids=frozenset(),
    )
    publisher = SimpleNamespace(publish=AsyncMock(), publish_preview=AsyncMock())
    submit = AsyncMock()
    monkeypatch.setattr(
        service,
        "load_wallet",
        lambda _settings: SimpleNamespace(hotkey=SimpleNamespace(ss58_address=HOTKEY)),
    )
    monkeypatch.setattr(service.BittensorChain, "connect", AsyncMock(return_value=chain))
    monkeypatch.setattr(service.uvicorn, "Server", lambda _config: ops)
    monkeypatch.setattr(service, "configure_loki_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "shutdown_loki_logging", AsyncMock())
    monkeypatch.setattr(service, "ValidatorIngestor", lambda *_args, **_kwargs: ingestor)
    monkeypatch.setattr(
        service,
        "CampaignFeedClient",
        lambda *_args, **_kwargs: SimpleNamespace(fetch=fetch, close=AsyncMock()),
    )
    monkeypatch.setattr(service, "CampaignReconciler", lambda *_args, **_kwargs: reconciler)
    monkeypatch.setattr(
        service, "DesearchProvider", lambda *_args, **_kwargs: SimpleNamespace(close=AsyncMock())
    )
    monkeypatch.setattr(
        service, "LlmBriefFilter", lambda *_args, **_kwargs: SimpleNamespace(close=AsyncMock())
    )
    monkeypatch.setattr(
        service, "DataPublisher", lambda *_args, **_kwargs: SimpleNamespace(close=AsyncMock())
    )
    monkeypatch.setattr(service, "ShadowResultPublisher", lambda *_args, **_kwargs: publisher)
    monkeypatch.setattr(service, "submit_weights_if_due", submit)

    await service.ValidatorService(
        Settings(
            _env_file=None,
            state_dir=tmp_path,
            desearch_api_key="offline-test",
            chutes_api_key="offline-test",
            enable_data_publish=True,
            enable_weight_submission=True,
        )
    ).run()

    assert archive.read_bytes() == b"historical archive must not be opened or modified"
    publisher.publish_preview.assert_not_awaited()
    if case in {"legacy", "mixed", "frozen_legacy"}:
        reconciler.reconcile_feed.assert_not_awaited()
        publisher.publish.assert_not_awaited()
        submit.assert_not_awaited()
        assert shadow_report(tmp_path) == before
    else:
        publisher.publish.assert_awaited_once()
        if case == "pending_preclaim":
            submit.assert_not_awaited()
            assert shadow_report(tmp_path)["shadow_blocks"] == 0
        else:
            submit.assert_awaited_once()
            assert submit.await_args is not None
            expected = {0: 0.0, 7: 1.0} if case == "preclaim" else {0: 1.0, 7: 0.0}
            assert submit.await_args.args[3] == expected
            assert shadow_report(tmp_path)["shadow_blocks"] == 1
