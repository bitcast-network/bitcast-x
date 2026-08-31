"""Crash-safe validator commitment journal and per-miner cursors."""

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from bitcast_x.chain import ChainCommitment
from bitcast_x.errors import ProtocolError
from bitcast_x.protocol import (
    CommitmentEnvelope,
    CommitmentPosition,
    CommittedBatch,
)
from bitcast_x.protocol.models import AttributionResult
from bitcast_x.sqlite import apply_migrations

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bitcast_x.brief_filter import BriefEvaluation
    from bitcast_x.campaigns import CampaignRecord
    from bitcast_x.rewards import RewardDecision, TweetReward
    from bitcast_x.validator.scoring import ScoredAttribution


def _same_campaign_contract(first: str, second: str) -> bool:
    """Compare campaign records after removing backward-compatible null placeholders."""

    from bitcast_x.campaigns import CampaignRecord

    try:
        first_record = CampaignRecord.model_validate_json(first)
        second_record = CampaignRecord.model_validate_json(second)
        if (first_record.max_members is None) != (second_record.max_members is None):
            first_record = first_record.model_copy(update={"max_members": None})
            second_record = second_record.model_copy(update={"max_members": None})
        return first_record == second_record
    except ValueError:
        return False


def _campaign_has_frozen_results(connection: sqlite3.Connection, campaign_id: str) -> bool:
    """Return whether a campaign has produced an irreversible reward allocation."""

    reward_row = connection.execute(
        "SELECT rewards_json FROM campaign_rewards WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchone()
    if reward_row is not None:
        if _rewards_have_positive_allocation(str(reward_row["rewards_json"])):
            return True

    publication_row = connection.execute(
        "SELECT payload_json, succeeded FROM publications WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchone()
    return bool(
        publication_row is not None
        and publication_row["succeeded"]
        and _publication_has_reward_allocation(str(publication_row["payload_json"]))
    )


def _rewards_have_positive_allocation(rewards_json: str) -> bool:
    """Return whether stored campaign economics contain a positive allocation."""

    try:
        rewards = json.loads(rewards_json)
        if not isinstance(rewards, list):
            return True
        return any(
            isinstance(reward, dict) and float(reward.get("daily_usd_floor") or 0.0) > 0
            for reward in rewards
        )
    except (TypeError, ValueError):
        # Unreadable economic state must fail closed rather than be reopened.
        return True


def _publication_has_reward_allocation(payload_json: str) -> bool:
    """Return whether one stored publication contains a positive tweet allocation."""

    try:
        payload = json.loads(payload_json)
        tweets = payload.get("tweets", []) if isinstance(payload, dict) else []
        return any(
            isinstance(tweet, dict) and float(tweet.get("usd_target") or 0.0) > 0
            for tweet in tweets
        )
    except (TypeError, ValueError):
        # A malformed successful publication is not safe to replace.
        return True


def _campaign_has_provisional_results(
    connection: sqlite3.Connection,
    campaign_id: str,
) -> bool:
    """Return whether retryable zero-value state already exists for a campaign."""

    return bool(
        connection.execute(
            """
            SELECT EXISTS(
                SELECT 1 FROM reconciliations WHERE campaign_id = ?
            ) OR EXISTS(
                SELECT 1 FROM campaign_rewards WHERE campaign_id = ?
            ) OR EXISTS(
                SELECT 1 FROM publications WHERE campaign_id = ?
            )
            """,
            (campaign_id,) * 3,
        ).fetchone()[0]
    )


def _load_frozen_campaign(stored_json: str | None, campaign_id: str) -> "CampaignRecord | None":
    """Validate one durable campaign contract without importing models at module load."""

    from bitcast_x.campaigns import CampaignRecord

    if stored_json is None:
        return None
    try:
        campaign = CampaignRecord.model_validate_json(stored_json)
    except ValueError:
        return None
    return campaign if campaign.access.campaign_id == campaign_id else None


@dataclass(frozen=True, slots=True)
class VerifiedBatchRecord:
    """A complete verified batch with its finalized timestamp and ordering position."""

    batch: CommittedBatch
    position: CommitmentPosition
    timestamp: datetime
    history_start: CommitmentPosition | None = None


@dataclass(frozen=True, slots=True)
class FeaturedTweetSelection:
    """One creator-visible featured tweet pinned near campaign close."""

    campaign_id: str
    tweet_id: str
    selection_pool: tuple[str, ...]
    selected_block: int
    selected_at: datetime


def _featured_tweet_selection(
    row: sqlite3.Row,
    campaign_id: str,
) -> FeaturedTweetSelection:
    """Validate and materialize one stored featured selection."""

    try:
        raw_pool = json.loads(str(row["selection_pool_json"]))
        selected_at = datetime.fromisoformat(str(row["selected_at"]))
        tweet_id = str(row["tweet_id"])
        selected_block = int(row["selected_block"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProtocolError(
            f"stored featured tweet selection is invalid for campaign {campaign_id}"
        ) from exc
    if (
        not isinstance(raw_pool, list)
        or not raw_pool
        or any(not isinstance(item, str) or not item for item in raw_pool)
        or len(set(raw_pool)) != len(raw_pool)
        or tweet_id not in raw_pool
        or selected_block < 0
        or selected_at.tzinfo is None
        or selected_at.utcoffset() is None
    ):
        raise ProtocolError(
            f"stored featured tweet selection is invalid for campaign {campaign_id}"
        )
    return FeaturedTweetSelection(
        campaign_id=campaign_id,
        tweet_id=tweet_id,
        selection_pool=tuple(raw_pool),
        selected_block=selected_block,
        selected_at=selected_at.astimezone(UTC),
    )


class ValidatorStore:
    """Persist observed chain anchors before atomically advancing verified cursors."""

    def __init__(self, path: Path, *, start_block: int = 0) -> None:
        if start_block < 0:
            raise ValueError("start_block cannot be negative")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._initial_scanned_block = start_block - 1
        self._lock = threading.RLock()
        try:
            self._initialize()
        except sqlite3.DatabaseError as exc:
            if "file is not a database" not in str(exc).casefold():
                raise
            quarantined = self._quarantine_unreadable_database()
            LOGGER.error(
                "quarantined unreadable validator database path=%s files=%s",
                self.path,
                [str(item) for item in quarantined],
            )
            self._initialize()

    def _quarantine_unreadable_database(self) -> tuple[Path, ...]:
        """Preserve an unreadable journal and SQLite sidecars before rebuilding."""

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        moved: list[Path] = []
        for source in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if not source.exists():
                continue
            destination = source.with_name(f"{source.name}.corrupt-{stamp}")
            source.replace(destination)
            moved.append(destination)
        if not moved:
            raise FileNotFoundError(f"unreadable validator database disappeared: {self.path}")
        return tuple(moved)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            base_migrations = (
                """
                CREATE TABLE IF NOT EXISTS scan_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    last_finalized_block INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS commitments (
                    hotkey TEXT NOT NULL,
                    block INTEGER NOT NULL,
                    extrinsic_index INTEGER NOT NULL,
                    block_timestamp TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_count INTEGER NOT NULL,
                    batch_hash TEXT NOT NULL,
                    PRIMARY KEY(hotkey, block, extrinsic_index)
                );
                CREATE INDEX IF NOT EXISTS idx_commitment_sequence
                    ON commitments(hotkey, sequence, block, extrinsic_index);
                CREATE TABLE IF NOT EXISTS miner_cursors (
                    hotkey TEXT PRIMARY KEY,
                    last_sequence INTEGER NOT NULL,
                    last_batch_hash TEXT,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS verified_batches (
                    hotkey TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    block INTEGER NOT NULL,
                    extrinsic_index INTEGER NOT NULL,
                    batch_hash TEXT NOT NULL,
                    batch_json TEXT NOT NULL,
                    PRIMARY KEY(hotkey, sequence),
                    UNIQUE(hotkey, block, extrinsic_index)
                );
                CREATE TABLE IF NOT EXISTS reconciliations (
                    snapshot_id TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    campaign_json TEXT NOT NULL,
                    results_json TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id, campaign_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_reconciliation_campaign
                    ON reconciliations(campaign_id);
                CREATE TABLE IF NOT EXISTS scored_reconciliations (
                    snapshot_id TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    scored_json TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id, campaign_id),
                    FOREIGN KEY(snapshot_id, campaign_id)
                        REFERENCES reconciliations(snapshot_id, campaign_id)
                );
                CREATE TABLE IF NOT EXISTS shadow_weights (
                    block INTEGER PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    weights_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publications (
                    snapshot_id TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    succeeded INTEGER NOT NULL CHECK(succeeded IN (0, 1)),
                    PRIMARY KEY(snapshot_id, campaign_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_publication_campaign
                    ON publications(campaign_id);
                CREATE TABLE IF NOT EXISTS campaign_rewards (
                    campaign_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    campaign_json TEXT NOT NULL,
                    rewards_json TEXT NOT NULL,
                    decisions_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rewarded_tweets (
                    tweet_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    miner_hotkey TEXT NOT NULL,
                    FOREIGN KEY(campaign_id) REFERENCES campaign_rewards(campaign_id)
                );
                    """,
                """
                CREATE TABLE IF NOT EXISTS llm_evaluations (
                    prompt_hash TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL
                );
                    """,
                """
                CREATE TABLE IF NOT EXISTS campaign_protocols (
                    campaign_id TEXT PRIMARY KEY,
                    mining_protocol TEXT NOT NULL
                );
                    """,
            )
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current <= len(base_migrations):
                apply_migrations(connection, base_migrations)
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(campaign_protocols)")
            }
            if "exclusive_miner_hotkey" not in columns:
                connection.execute(
                    "ALTER TABLE campaign_protocols ADD COLUMN exclusive_miner_hotkey TEXT"
                )
            if "campaign_contract_json" not in columns:
                connection.execute(
                    "ALTER TABLE campaign_protocols ADD COLUMN campaign_contract_json TEXT"
                )
            connection.execute(
                """
                UPDATE campaign_protocols
                SET campaign_contract_json = (
                    SELECT reconciliations.campaign_json
                    FROM reconciliations
                    WHERE reconciliations.campaign_id = campaign_protocols.campaign_id
                )
                WHERE campaign_contract_json IS NULL
                  AND EXISTS (
                    SELECT 1 FROM reconciliations
                    WHERE reconciliations.campaign_id = campaign_protocols.campaign_id
                  )
                """
            )
            # Version four records the guarded additive column. The no-op SQL
            # keeps re-adoption safe when an otherwise-current DB has its
            # user_version cleared by an operator or older tooling.
            # Version five similarly records the complete campaign contract.
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            cursor_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(miner_cursors)")
            }
            commitment_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(commitments)")
            }
            verified_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(verified_batches)")
            }
            if (
                current <= 5
                and "history_id" in cursor_columns
                and "history_id" in commitment_columns
                and "history_id" in verified_columns
            ):
                connection.execute("PRAGMA user_version = 6")
            apply_migrations(
                connection,
                (
                    *base_migrations,
                    "SELECT 1;",
                    "SELECT 1;",
                    """
                    ALTER TABLE miner_cursors
                        ADD COLUMN history_id TEXT NOT NULL DEFAULT '';
                    ALTER TABLE commitments
                        ADD COLUMN history_id TEXT NOT NULL DEFAULT '';
                    ALTER TABLE verified_batches RENAME TO verified_batches_pre_history;
                    CREATE TABLE verified_batches (
                        hotkey TEXT NOT NULL,
                        history_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        block INTEGER NOT NULL,
                        extrinsic_index INTEGER NOT NULL,
                        batch_hash TEXT NOT NULL,
                        batch_json TEXT NOT NULL,
                        PRIMARY KEY(hotkey, history_id, sequence),
                        UNIQUE(hotkey, block, extrinsic_index)
                    );
                    INSERT INTO verified_batches(
                        hotkey, history_id, sequence, block, extrinsic_index,
                        batch_hash, batch_json
                    )
                    SELECT hotkey, '', sequence, block, extrinsic_index,
                           batch_hash, batch_json
                    FROM verified_batches_pre_history;
                    DROP TABLE verified_batches_pre_history;
                    """,
                ),
            )
            # This table is a rollback-safe additive extension. It deliberately
            # leaves user_version unchanged so an older binary can ignore it
            # during rollback while newer binaries recreate it after recovery.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS featured_tweet_selections (
                    campaign_id TEXT PRIMARY KEY,
                    campaign_json TEXT NOT NULL,
                    tweet_id TEXT NOT NULL,
                    selection_pool_json TEXT NOT NULL,
                    selected_block INTEGER NOT NULL CHECK(selected_block >= 0),
                    selected_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO scan_state(singleton, last_finalized_block)
                VALUES (1, ?)
                """,
                (self._initial_scanned_block,),
            )

    def bind_campaign_protocols(
        self,
        campaigns: tuple["CampaignRecord", ...],
    ) -> tuple["CampaignRecord", ...]:
        """Adopt mutable feed records and retain creator-visible pinned contracts."""

        bound_campaigns: list[CampaignRecord] = []
        observed_campaign_ids = {item.access.campaign_id for item in campaigns}
        with self._transaction() as connection:
            for campaign in campaigns:
                campaign_id = campaign.access.campaign_id
                protocol = campaign.access.mining_protocol.value
                exclusive_hotkey = campaign.access.exclusive_miner_hotkey
                campaign_json = campaign.model_dump_json()
                row = connection.execute(
                    """
                    SELECT mining_protocol, exclusive_miner_hotkey, campaign_contract_json
                    FROM campaign_protocols WHERE campaign_id = ?
                    """,
                    (campaign_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO campaign_protocols(
                            campaign_id, mining_protocol, exclusive_miner_hotkey,
                            campaign_contract_json
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (campaign_id, protocol, exclusive_hotkey, campaign_json),
                    )
                    bound_campaigns.append(campaign)
                    continue

                stored_json = row["campaign_contract_json"]
                unchanged = (
                    stored_json is not None
                    and _same_campaign_contract(stored_json, campaign_json)
                    and row["mining_protocol"] == protocol
                    and row["exclusive_miner_hotkey"] == exclusive_hotkey
                )
                if unchanged:
                    bound_campaigns.append(campaign)
                    continue
                featured_selection_exists = connection.execute(
                    "SELECT 1 FROM featured_tweet_selections WHERE campaign_id = ?",
                    (campaign_id,),
                ).fetchone()
                if (
                    _campaign_has_frozen_results(connection, campaign_id)
                    or featured_selection_exists is not None
                ):
                    # A changed feed record must never replace the contract that
                    # produced durable results. It also must not deny service to
                    # every unrelated campaign in the feed. Keep using the
                    # frozen contract for this campaign and make the rejected
                    # mutation operationally visible.
                    frozen_campaign = _load_frozen_campaign(stored_json, campaign_id)
                    if frozen_campaign is None:
                        LOGGER.critical(
                            "quarantined campaign with unreadable frozen contract campaign=%s",
                            campaign_id,
                        )
                        continue

                    changed_fields: list[str] = []
                    for field in type(campaign).model_fields:
                        observed = getattr(campaign, field)
                        frozen = getattr(frozen_campaign, field)
                        if observed == frozen:
                            continue
                        if field == "access":
                            changed_fields.extend(
                                f"access.{access_field}"
                                for access_field in type(campaign.access).model_fields
                                if getattr(campaign.access, access_field)
                                != getattr(frozen_campaign.access, access_field)
                            )
                        else:
                            changed_fields.append(field)
                    LOGGER.error(
                        "rejected campaign mutation after durable state froze; "
                        "using frozen contract campaign=%s changed_fields=%s",
                        campaign_id,
                        ",".join(changed_fields) or "unknown",
                    )
                    bound_campaigns.append(frozen_campaign)
                    continue
                reopening_provisional = _campaign_has_provisional_results(
                    connection,
                    campaign_id,
                )
                connection.execute(
                    """
                    UPDATE campaign_protocols
                    SET mining_protocol = ?,
                        exclusive_miner_hotkey = ?,
                        campaign_contract_json = ?
                    WHERE campaign_id = ?
                    """,
                    (protocol, exclusive_hotkey, campaign_json, campaign_id),
                )
                if reopening_provisional:
                    LOGGER.warning(
                        "reopened zero-value campaign with latest contract campaign=%s",
                        campaign_id,
                    )
                else:
                    LOGGER.warning(
                        "adopted canonical campaign update before reward settlement campaign=%s",
                        campaign_id,
                    )
                bound_campaigns.append(campaign)
            omitted_selections = connection.execute(
                """
                SELECT campaign_id, campaign_json
                FROM featured_tweet_selections
                ORDER BY campaign_id
                """
            ).fetchall()
            for row in omitted_selections:
                campaign_id = str(row["campaign_id"])
                if campaign_id in observed_campaign_ids or _campaign_has_frozen_results(
                    connection,
                    campaign_id,
                ):
                    continue
                pinned_campaign = _load_frozen_campaign(
                    str(row["campaign_json"]),
                    campaign_id,
                )
                if pinned_campaign is None:
                    LOGGER.critical(
                        "quarantined omitted campaign with unreadable featured contract "
                        "campaign=%s",
                        campaign_id,
                    )
                    continue
                LOGGER.warning(
                    "retained omitted campaign with pinned featured tweet campaign=%s",
                    campaign_id,
                )
                bound_campaigns.append(pinned_campaign)
        return tuple(bound_campaigns)

    def featured_tweet_selection(
        self,
        campaign_id: str,
        campaign_json: str,
    ) -> FeaturedTweetSelection | None:
        """Return the durable featured selection for an unchanged campaign."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT campaign_json, tweet_id, selection_pool_json,
                       selected_block, selected_at
                FROM featured_tweet_selections
                WHERE campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()
        if row is None:
            return None
        if not _same_campaign_contract(str(row["campaign_json"]), campaign_json):
            raise ProtocolError(f"campaign {campaign_id} changed after featured tweet selection")
        return _featured_tweet_selection(row, campaign_id)

    def pin_featured_tweet_selection(
        self,
        *,
        campaign_id: str,
        campaign_json: str,
        tweet_id: str,
        selection_pool: tuple[str, ...],
        selected_block: int,
        selected_at: datetime,
    ) -> FeaturedTweetSelection:
        """Atomically pin the first valid selection and replay it thereafter."""

        if not campaign_id or not tweet_id:
            raise ValueError("campaign_id and tweet_id cannot be blank")
        if selected_block < 0:
            raise ValueError("selected_block cannot be negative")
        if selected_at.tzinfo is None or selected_at.utcoffset() is None:
            raise ValueError("selected_at must be timezone-aware")
        if not selection_pool or tweet_id not in selection_pool:
            raise ValueError("featured tweet must belong to a non-empty selection pool")
        if len(set(selection_pool)) != len(selection_pool):
            raise ValueError("featured selection pool must contain unique tweet IDs")

        normalized_at = selected_at.astimezone(UTC)
        pool_json = json.dumps(selection_pool, separators=(",", ":"))
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT campaign_json, tweet_id, selection_pool_json,
                       selected_block, selected_at
                FROM featured_tweet_selections
                WHERE campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO featured_tweet_selections(
                        campaign_id, campaign_json, tweet_id, selection_pool_json,
                        selected_block, selected_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        campaign_id,
                        campaign_json,
                        tweet_id,
                        pool_json,
                        selected_block,
                        normalized_at.isoformat(),
                    ),
                )
                return FeaturedTweetSelection(
                    campaign_id=campaign_id,
                    tweet_id=tweet_id,
                    selection_pool=selection_pool,
                    selected_block=selected_block,
                    selected_at=normalized_at,
                )
            if not _same_campaign_contract(str(row["campaign_json"]), campaign_json):
                raise ProtocolError(
                    f"campaign {campaign_id} changed after featured tweet selection"
                )
            return _featured_tweet_selection(row, campaign_id)

    def scanned_block(self) -> int:
        """Return the last fully persisted finalized block."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_finalized_block FROM scan_state WHERE singleton = 1"
            ).fetchone()
        return int(row["last_finalized_block"])

    def persist_block(self, block: int, observations: list[ChainCommitment]) -> None:
        """Atomically journal all observations and advance the global block scan cursor."""

        with self._transaction() as connection:
            current = int(
                connection.execute(
                    "SELECT last_finalized_block FROM scan_state WHERE singleton = 1"
                ).fetchone()["last_finalized_block"]
            )
            if block <= current:
                return
            if block != current + 1:
                raise ProtocolError(f"expected finalized block {current + 1}")
            for observation in observations:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO commitments(
                        hotkey, block, extrinsic_index, block_timestamp,
                        history_id, sequence, event_count, batch_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation.hotkey,
                        observation.block,
                        observation.extrinsic_index,
                        observation.timestamp.isoformat(),
                        observation.envelope.history_id.hex()
                        if observation.envelope.history_id is not None
                        else "",
                        observation.envelope.sequence,
                        observation.envelope.event_count,
                        observation.envelope.batch_hash.hex(),
                    ),
                )
            connection.execute(
                "UPDATE scan_state SET last_finalized_block = ? WHERE singleton = 1",
                (block,),
            )

    def cursor(self, hotkey: str) -> tuple[int, str | None]:
        """Return the last atomically verified sequence and hash for a miner."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_sequence, last_batch_hash FROM miner_cursors WHERE hotkey = ?",
                (hotkey,),
            ).fetchone()
        return (int(row["last_sequence"]), row["last_batch_hash"]) if row else (0, None)

    def history_cursor(self, hotkey: str) -> tuple[str | None, int, str | None]:
        """Return the active history ID and its independently scoped batch cursor."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT history_id, last_sequence, last_batch_hash
                FROM miner_cursors WHERE hotkey = ?
                """,
                (hotkey,),
            ).fetchone()
        if row is None:
            return None, 0, None
        history_id = str(row["history_id"])
        return (history_id or None, int(row["last_sequence"]), row["last_batch_hash"])

    def commitment_for_sequence(
        self,
        hotkey: str,
        sequence: int,
        *,
        event_count: int | None = None,
        batch_hash: str | None = None,
    ) -> ChainCommitment | None:
        """Return the finalized anchor proving the exact batch served by a miner."""

        if (event_count is None) != (batch_hash is None):
            raise ValueError("event_count and batch_hash must be supplied together")

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT block, extrinsic_index, block_timestamp, history_id,
                       event_count, batch_hash
                FROM commitments
                WHERE hotkey = ? AND sequence = ? ORDER BY block, extrinsic_index
                """,
                (hotkey, sequence),
            ).fetchall()
        if not rows:
            return None
        if batch_hash is not None and event_count is not None:
            rows = [
                row
                for row in rows
                if int(row["event_count"]) == event_count and row["batch_hash"] == batch_hash
            ]
            if not rows:
                raise ProtocolError(f"no finalized commitment matches batch sequence {sequence}")
        elif len(rows) != 1:
            raise ProtocolError(f"multiple commitments claim sequence {sequence}")
        row = rows[0]
        return ChainCommitment(
            hotkey=hotkey,
            block=int(row["block"]),
            extrinsic_index=int(row["extrinsic_index"]),
            timestamp=datetime.fromisoformat(row["block_timestamp"]),
            envelope=CommitmentEnvelope(
                sequence=sequence,
                event_count=int(row["event_count"]),
                batch_hash=bytes.fromhex(row["batch_hash"]),
                history_id=(bytes.fromhex(str(row["history_id"])) if row["history_id"] else None),
            ),
        )

    def next_commitment_sequence(self, hotkey: str, after_sequence: int) -> int | None:
        """Return the lowest observed sequence beyond a miner's verified cursor."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MIN(sequence) AS sequence FROM commitments
                WHERE hotkey = ? AND sequence > ?
                """,
                (hotkey, after_sequence),
            ).fetchone()
        return int(row["sequence"]) if row and row["sequence"] is not None else None

    def persist_verified(self, batch: CommittedBatch, observation: ChainCommitment) -> None:
        """Atomically journal a targeted chain proof and advance one miner cursor."""

        if observation.hotkey != batch.miner_hotkey:
            raise ProtocolError("batch proof belongs to a different miner hotkey")
        envelope_history_id = (
            observation.envelope.history_id.hex()
            if observation.envelope.history_id is not None
            else ""
        )
        if envelope_history_id != (batch.history_id or ""):
            raise ProtocolError("batch proof belongs to a different miner history")
        position = CommitmentPosition(
            block=observation.block,
            extrinsic_index=observation.extrinsic_index,
        )
        with self._transaction() as connection:
            existing_anchor = connection.execute(
                """
                SELECT sequence, event_count, batch_hash, block_timestamp, history_id
                FROM commitments
                WHERE hotkey = ? AND block = ? AND extrinsic_index = ?
                """,
                (batch.miner_hotkey, position.block, position.extrinsic_index),
            ).fetchone()
            expected_anchor = (
                observation.envelope.sequence,
                observation.envelope.event_count,
                observation.envelope.batch_hash.hex(),
                observation.timestamp.isoformat(),
                observation.envelope.history_id.hex()
                if observation.envelope.history_id is not None
                else "",
            )
            if existing_anchor is None:
                connection.execute(
                    """
                    INSERT INTO commitments(
                        hotkey, block, extrinsic_index, block_timestamp,
                        history_id, sequence, event_count, batch_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch.miner_hotkey,
                        position.block,
                        position.extrinsic_index,
                        expected_anchor[3],
                        expected_anchor[4],
                        expected_anchor[0],
                        expected_anchor[1],
                        expected_anchor[2],
                    ),
                )
            elif tuple(existing_anchor) != expected_anchor:
                raise ProtocolError("verified commitment position changed")
            row = connection.execute(
                """
                SELECT history_id, last_sequence, last_batch_hash
                FROM miner_cursors WHERE hotkey = ?
                """,
                (batch.miner_hotkey,),
            ).fetchone()
            current_history_id = str(row["history_id"]) if row else ""
            last_sequence = int(row["last_sequence"]) if row else 0
            last_hash = row["last_batch_hash"] if row else None
            batch_history_id = batch.history_id or ""
            if batch_history_id != current_history_id:
                if not batch_history_id:
                    raise ProtocolError("legacy batch cannot replace the active miner history")
                if batch.sequence != 1 or batch.previous_batch_hash is not None:
                    raise ProtocolError("new miner history must begin with sequence 1")
                reused = connection.execute(
                    """
                    SELECT 1 FROM verified_batches
                    WHERE hotkey = ? AND history_id = ?
                    UNION ALL
                    SELECT 1 FROM commitments
                    WHERE hotkey = ? AND history_id = ?
                      AND (block != ? OR extrinsic_index != ?)
                    LIMIT 1
                    """,
                    (
                        batch.miner_hotkey,
                        batch_history_id,
                        batch.miner_hotkey,
                        batch_history_id,
                        position.block,
                        position.extrinsic_index,
                    ),
                ).fetchone()
                if reused is not None:
                    raise ProtocolError("miner history ID was already used")
                previous = connection.execute(
                    """
                    SELECT block, extrinsic_index FROM verified_batches
                    WHERE hotkey = ? ORDER BY block DESC, extrinsic_index DESC LIMIT 1
                    """,
                    (batch.miner_hotkey,),
                ).fetchone()
                if previous is not None and (position.block, position.extrinsic_index) <= (
                    int(previous["block"]),
                    int(previous["extrinsic_index"]),
                ):
                    raise ProtocolError("new history must begin after verified history")
                last_sequence = 0
                last_hash = None
            if batch.sequence <= last_sequence:
                existing = connection.execute(
                    """
                    SELECT batch_hash, block, extrinsic_index FROM verified_batches
                    WHERE hotkey = ? AND history_id = ? AND sequence = ?
                    """,
                    (batch.miner_hotkey, batch_history_id, batch.sequence),
                ).fetchone()
                if existing is not None and (
                    existing["batch_hash"] == batch.batch_hash
                    and int(existing["block"]) == position.block
                    and int(existing["extrinsic_index"]) == position.extrinsic_index
                ):
                    return
                raise ProtocolError("verified historical batch changed")
            if batch.sequence != last_sequence + 1 or batch.previous_batch_hash != last_hash:
                raise ProtocolError("verified batch does not extend the durable cursor")
            previous_position = connection.execute(
                """
                SELECT block, extrinsic_index FROM verified_batches
                WHERE hotkey = ? AND history_id = ? AND sequence = ?
                """,
                (batch.miner_hotkey, batch_history_id, last_sequence),
            ).fetchone()
            if previous_position is not None and (
                position.block,
                position.extrinsic_index,
            ) <= (
                int(previous_position["block"]),
                int(previous_position["extrinsic_index"]),
            ):
                raise ProtocolError("verified batch positions must increase with sequence")
            connection.execute(
                """
                INSERT INTO verified_batches(
                    hotkey, history_id, sequence, block, extrinsic_index,
                    batch_hash, batch_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch.miner_hotkey,
                    batch_history_id,
                    batch.sequence,
                    position.block,
                    position.extrinsic_index,
                    batch.batch_hash,
                    batch.model_dump_json(),
                ),
            )
            connection.execute(
                """
                INSERT INTO miner_cursors(
                    hotkey, history_id, last_sequence, last_batch_hash, last_error
                ) VALUES (?, ?, ?, ?, NULL)
                ON CONFLICT(hotkey) DO UPDATE SET
                    history_id = excluded.history_id,
                    last_sequence = excluded.last_sequence,
                    last_batch_hash = excluded.last_batch_hash,
                    last_error = NULL
                """,
                (batch.miner_hotkey, batch_history_id, batch.sequence, batch.batch_hash),
            )

    def record_error(self, hotkey: str, error: str) -> None:
        """Record an explanatory reconciliation error without advancing state."""

        with self._transaction() as connection:
            history_id, sequence, batch_hash = self.history_cursor(hotkey)
            connection.execute(
                """
                INSERT INTO miner_cursors(
                    hotkey, history_id, last_sequence, last_batch_hash, last_error
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(hotkey) DO UPDATE SET last_error = excluded.last_error
                """,
                (hotkey, history_id or "", sequence, batch_hash, error),
            )

    def verified_batches(self, *, through_block: int | None = None) -> list[VerifiedBatchRecord]:
        """Return globally ordered verified history for deterministic replay."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT b.batch_json, b.block, b.extrinsic_index, c.block_timestamp,
                       CASE WHEN b.history_id != '' THEN (
                           SELECT first.block FROM verified_batches first
                           WHERE first.hotkey = b.hotkey
                             AND first.history_id = b.history_id
                             AND first.sequence = 1
                       ) END AS history_block,
                       CASE WHEN b.history_id != '' THEN (
                           SELECT first.extrinsic_index FROM verified_batches first
                           WHERE first.hotkey = b.hotkey
                             AND first.history_id = b.history_id
                             AND first.sequence = 1
                       ) END AS history_index
                FROM verified_batches b
                JOIN commitments c
                  ON c.hotkey = b.hotkey
                 AND c.block = b.block
                 AND c.extrinsic_index = b.extrinsic_index
                WHERE (? IS NULL OR b.block <= ?)
                ORDER BY b.block, b.extrinsic_index, b.hotkey, b.sequence
                """,
                (through_block, through_block),
            ).fetchall()
        return [
            VerifiedBatchRecord(
                batch=CommittedBatch.model_validate_json(row["batch_json"]),
                position=CommitmentPosition(
                    block=int(row["block"]),
                    extrinsic_index=int(row["extrinsic_index"]),
                ),
                timestamp=datetime.fromisoformat(row["block_timestamp"]),
                history_start=(
                    CommitmentPosition(
                        block=int(row["history_block"]),
                        extrinsic_index=int(row["history_index"]),
                    )
                    if row["history_block"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def persist_reconciliation(
        self,
        *,
        snapshot_id: str,
        campaign_id: str,
        campaign_json: str,
        results: list[AttributionResult],
    ) -> None:
        """Store retryable attribution state until a positive reward allocation freezes it."""

        results_json = TypeAdapter(list[AttributionResult]).dump_json(results).decode()
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT campaign_json, results_json FROM reconciliations
                WHERE campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()
            if existing is not None and _campaign_has_frozen_results(connection, campaign_id):
                if (
                    _same_campaign_contract(existing["campaign_json"], campaign_json)
                    and existing["results_json"] == results_json
                ):
                    return
                raise ProtocolError("frozen campaign reconciliation changed on replay")
            if existing is not None:
                # A fresh retry replaces all downstream zero-value state. Removing
                # the child score first also permits a new feed snapshot identifier.
                connection.execute(
                    "DELETE FROM scored_reconciliations WHERE campaign_id = ?",
                    (campaign_id,),
                )
                connection.execute(
                    "DELETE FROM rewarded_tweets WHERE campaign_id = ?",
                    (campaign_id,),
                )
                connection.execute(
                    "DELETE FROM campaign_rewards WHERE campaign_id = ?",
                    (campaign_id,),
                )
                connection.execute(
                    "DELETE FROM publications WHERE campaign_id = ?",
                    (campaign_id,),
                )
            connection.execute(
                """
                INSERT INTO reconciliations(
                    snapshot_id, campaign_id, campaign_json, results_json
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(campaign_id) DO UPDATE SET
                    snapshot_id = excluded.snapshot_id,
                    campaign_json = excluded.campaign_json,
                    results_json = excluded.results_json
                """,
                (snapshot_id, campaign_id, campaign_json, results_json),
            )

    def reconciliation(
        self,
        snapshot_id: str,
        campaign_id: str,
        campaign_json: str,
    ) -> list[AttributionResult] | None:
        """Return stored attribution state for inspection or final replay."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT snapshot_id, campaign_json, results_json FROM reconciliations
                WHERE campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()
        if row is not None and not _same_campaign_contract(row["campaign_json"], campaign_json):
            raise ProtocolError(
                f"campaign {campaign_id} changed after frozen reconciliation "
                f"from snapshot {row['snapshot_id']} to {snapshot_id}"
            )
        return (
            TypeAdapter(list[AttributionResult]).validate_json(row["results_json"])
            if row is not None
            else None
        )

    def campaign_reconciled(self, campaign_id: str) -> bool:
        """Return whether a campaign has stored reconciliation state for this cycle."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM reconciliations WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
        return row is not None

    def campaign_finalized(self, campaign_id: str) -> bool:
        """Return whether positive economics make a campaign immutable."""

        with self._connect() as connection:
            return _campaign_has_frozen_results(connection, campaign_id)

    def reconciled_campaigns(self) -> list["CampaignRecord"]:
        """Return campaigns retained by a positive, immutable reward allocation."""

        from bitcast_x.campaigns import CampaignRecord

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT campaign_id, campaign_json FROM reconciliations ORDER BY campaign_id"
            ).fetchall()
            frozen_rows = [
                row
                for row in rows
                if _campaign_has_frozen_results(connection, str(row["campaign_id"]))
            ]
        return [CampaignRecord.model_validate_json(row["campaign_json"]) for row in frozen_rows]

    def scored_reconciliation(
        self, snapshot_id: str, campaign_id: str
    ) -> list["ScoredAttribution"] | None:
        """Return a previously frozen score set without refetching mutable engagements."""

        from bitcast_x.validator.scoring import ScoredAttribution

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT scored_json FROM scored_reconciliations
                WHERE campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()
        if row is None:
            return None
        return TypeAdapter(list[ScoredAttribution]).validate_json(row["scored_json"])

    def persist_scores(
        self,
        snapshot_id: str,
        campaign_id: str,
        scored: list["ScoredAttribution"],
    ) -> None:
        """Store retryable engagement evidence until positive economics freeze it."""

        from bitcast_x.validator.scoring import ScoredAttribution

        payload = TypeAdapter(list[ScoredAttribution]).dump_json(scored).decode()
        with self._transaction() as connection:
            reconciliation = connection.execute(
                """
                SELECT snapshot_id FROM reconciliations WHERE campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()
            if reconciliation is None:
                raise ProtocolError("cannot freeze scores before campaign reconciliation")
            frozen_snapshot_id = str(reconciliation["snapshot_id"])
            existing = connection.execute(
                """
                SELECT scored_json FROM scored_reconciliations
                WHERE campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()
            if existing is not None and _campaign_has_frozen_results(connection, campaign_id):
                if existing["scored_json"] == payload:
                    return
                raise ProtocolError("frozen campaign scores changed on replay")
            if existing is not None:
                connection.execute(
                    "DELETE FROM scored_reconciliations WHERE campaign_id = ?",
                    (campaign_id,),
                )
            connection.execute(
                """
                INSERT INTO scored_reconciliations(snapshot_id, campaign_id, scored_json)
                VALUES (?, ?, ?)
                """,
                (frozen_snapshot_id, campaign_id, payload),
            )

    def persist_shadow_weights(
        self, block: int, snapshot_id: str, weights: dict[int, float]
    ) -> None:
        """Persist a deterministic audit vector without submitting it on chain."""

        payload = TypeAdapter(dict[int, float]).dump_json(weights).decode()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT snapshot_id, weights_json FROM shadow_weights WHERE block = ?",
                (block,),
            ).fetchone()
            if existing is not None:
                if existing["weights_json"] == payload:
                    return
                raise ProtocolError("shadow weights changed for a frozen block")
            connection.execute(
                """
                INSERT INTO shadow_weights(block, snapshot_id, weights_json) VALUES (?, ?, ?)
                """,
                (block, snapshot_id, payload),
            )

    def llm_evaluation(self, prompt_hash: str) -> "BriefEvaluation | None":
        """Return one frozen semantic-evaluation result by exact prompt digest."""

        from bitcast_x.brief_filter import BriefEvaluation

        with self._connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM llm_evaluations WHERE prompt_hash = ?",
                (prompt_hash,),
            ).fetchone()
        return BriefEvaluation.model_validate_json(row["result_json"]) if row else None

    def persist_llm_evaluation(
        self, prompt_hash: str, result: "BriefEvaluation"
    ) -> "BriefEvaluation":
        """Freeze one prompt verdict and return the effective first-writer result."""

        from bitcast_x.brief_filter import BriefEvaluation

        payload = result.model_dump_json()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT result_json FROM llm_evaluations WHERE prompt_hash = ?",
                (prompt_hash,),
            ).fetchone()
            if existing is not None:
                return BriefEvaluation.model_validate_json(existing["result_json"])
            connection.execute(
                "INSERT INTO llm_evaluations(prompt_hash, result_json) VALUES (?, ?)",
                (prompt_hash, payload),
            )
        return result

    def publication_succeeded(self, snapshot_id: str, campaign_id: str) -> bool:
        """Return whether ingestion accepted a positive final allocation already."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json, succeeded FROM publications
                WHERE campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()
        return bool(
            row is not None
            and row["succeeded"]
            and _publication_has_reward_allocation(str(row["payload_json"]))
        )

    def record_publication(
        self,
        snapshot_id: str,
        campaign_id: str,
        *,
        run_id: str,
        payload: dict[str, object],
        succeeded: bool,
    ) -> None:
        """Durably record an attempt, retaining successful positive output forever."""

        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT run_id, payload_json, attempts, succeeded FROM publications
                WHERE campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()
            if (
                existing is not None
                and bool(existing["succeeded"])
                and _publication_has_reward_allocation(str(existing["payload_json"]))
            ):
                if existing["run_id"] != run_id or existing["payload_json"] != payload_json:
                    raise ProtocolError("successful publication changed on replay")
                return
            attempts = int(existing["attempts"]) + 1 if existing is not None else 1
            connection.execute(
                """
                INSERT INTO publications(
                    snapshot_id, campaign_id, run_id, payload_json, attempts, succeeded
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(campaign_id) DO UPDATE SET
                    snapshot_id = excluded.snapshot_id,
                    run_id = excluded.run_id,
                    payload_json = excluded.payload_json,
                    attempts = excluded.attempts,
                    succeeded = excluded.succeeded
                """,
                (snapshot_id, campaign_id, run_id, payload_json, attempts, int(succeeded)),
            )

    def rewarded_tweet_ids(self) -> set[str]:
        """Return every tweet already frozen into an earlier campaign assignment."""

        with self._connect() as connection:
            rows = connection.execute("SELECT tweet_id FROM rewarded_tweets").fetchall()
        return {str(row["tweet_id"]) for row in rows}

    def campaign_rewards(
        self,
        campaign_id: str,
        campaign_json: str,
    ) -> tuple[list["TweetReward"], list["RewardDecision"]] | None:
        """Return one campaign's frozen economic assignment across feed snapshots."""

        from bitcast_x.rewards import RewardDecision, TweetReward

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT campaign_json, rewards_json, decisions_json
                FROM campaign_rewards WHERE campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()
            if row is None or not _campaign_has_frozen_results(connection, campaign_id):
                return None
        if not _same_campaign_contract(row["campaign_json"], campaign_json):
            raise ProtocolError(f"campaign {campaign_id} changed after reward assignment")
        return (
            TypeAdapter(list[TweetReward]).validate_json(row["rewards_json"]),
            TypeAdapter(list[RewardDecision]).validate_json(row["decisions_json"]),
        )

    def persist_campaign_rewards(
        self,
        *,
        snapshot_id: str,
        campaign_id: str,
        campaign_json: str,
        rewards: list["TweetReward"],
        decisions: list["RewardDecision"],
    ) -> None:
        """Replace zero-value economics until a real allocation freezes assignments."""

        from bitcast_x.rewards import RewardDecision, TweetReward

        rewards_json = TypeAdapter(list[TweetReward]).dump_json(rewards).decode()
        decisions_json = TypeAdapter(list[RewardDecision]).dump_json(decisions).decode()
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT campaign_json, rewards_json, decisions_json
                FROM campaign_rewards WHERE campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()
            frozen = _campaign_has_frozen_results(connection, campaign_id)
            if existing is not None and frozen:
                if (
                    _same_campaign_contract(existing["campaign_json"], campaign_json)
                    and existing["rewards_json"] == rewards_json
                    and existing["decisions_json"] == decisions_json
                ):
                    return
                raise ProtocolError(f"campaign {campaign_id} rewards changed on replay")
            if existing is not None:
                connection.execute(
                    "DELETE FROM rewarded_tweets WHERE campaign_id = ?",
                    (campaign_id,),
                )
            connection.execute(
                """
                INSERT INTO campaign_rewards(
                    campaign_id, snapshot_id, campaign_json, rewards_json, decisions_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(campaign_id) DO UPDATE SET
                    snapshot_id = excluded.snapshot_id,
                    campaign_json = excluded.campaign_json,
                    rewards_json = excluded.rewards_json,
                    decisions_json = excluded.decisions_json
                """,
                (campaign_id, snapshot_id, campaign_json, rewards_json, decisions_json),
            )
            for decision in decisions:
                if not decision.accepted:
                    continue
                try:
                    connection.execute(
                        """
                        INSERT INTO rewarded_tweets(tweet_id, campaign_id, miner_hotkey)
                        VALUES (?, ?, ?)
                        """,
                        (decision.tweet_id, campaign_id, decision.miner_hotkey),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ProtocolError(f"tweet {decision.tweet_id} was already rewarded") from exc
