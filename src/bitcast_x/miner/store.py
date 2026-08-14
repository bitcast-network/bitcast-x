"""Crash-safe SQLite state for the reference miner and reusable SDK."""

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path

from pydantic import TypeAdapter

from bitcast_x.errors import ProtocolError
from bitcast_x.protocol import (
    ClaimEvent,
    CommitmentPosition,
    CommittedBatch,
    DraftReveal,
    ProtocolEvent,
    SubmissionEvent,
)
from bitcast_x.sqlite import apply_migrations

_EVENT_ADAPTER: TypeAdapter[ProtocolEvent] = TypeAdapter(ProtocolEvent)


class EventStatus(StrEnum):
    """Platform-facing lifecycle states persisted by the miner."""

    WAITING_FOR_COMMITMENT = "waiting_for_commitment"
    SAFE_TO_POST = "safe_to_post"
    EVICTED = "evicted"
    TWEET_RECEIVED = "tweet_received"
    VERIFICATION_PENDING = "verification_pending"
    ATTRIBUTED = "attributed"
    REJECTED = "rejected"


class MinerStore:
    """Transactional miner queue, batch history, and creator-operation status."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._initialize()

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
            apply_migrations(
                connection,
                (
                    """
                CREATE TABLE IF NOT EXISTS protocol_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('claim', 'submission')),
                    payload_json TEXT NOT NULL,
                    private_reveal_json TEXT,
                    status TEXT NOT NULL,
                    created_ns INTEGER NOT NULL,
                    batch_sequence INTEGER,
                    rejection_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_queue
                    ON events(batch_sequence, created_ns, event_id);
                CREATE TABLE IF NOT EXISTS batches (
                    sequence INTEGER PRIMARY KEY,
                    batch_json TEXT NOT NULL,
                    batch_hash TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (state IN ('prepared', 'finalized')),
                    created_ns INTEGER NOT NULL,
                    finalized_block INTEGER,
                    extrinsic_index INTEGER
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_prepared_batch
                    ON batches(state) WHERE state = 'prepared';
                CREATE TABLE IF NOT EXISTS active_claims (
                    claim_id TEXT PRIMARY KEY REFERENCES events(event_id),
                    campaign_id TEXT NOT NULL,
                    creator_x_id TEXT NOT NULL,
                    commitment_block INTEGER NOT NULL,
                    extrinsic_index INTEGER NOT NULL,
                    event_index INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_active_claim_fifo
                    ON active_claims(
                        campaign_id, creator_x_id,
                        commitment_block, extrinsic_index, event_index
                    );
                    """,
                ),
            )

    def enqueue(
        self,
        event: ProtocolEvent,
        *,
        reveal: DraftReveal | None = None,
        max_pending_events: int = 10_000,
        max_pending_bytes: int = 50_000_000,
    ) -> None:
        """Persist a unique event before returning control to the platform."""

        if max_pending_events <= 0 or max_pending_bytes <= 0:
            raise ValueError("pending queue limits must be positive")

        event_id = event.claim_id if isinstance(event, ClaimEvent) else event.submission_id
        status = (
            EventStatus.WAITING_FOR_COMMITMENT
            if isinstance(event, ClaimEvent)
            else EventStatus.TWEET_RECEIVED
        )
        payload = event.model_dump_json()
        reveal_json = reveal.model_dump_json() if reveal is not None else None
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json, private_reveal_json FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["payload_json"] == payload
                    and existing["private_reveal_json"] == reveal_json
                ):
                    return
                raise ProtocolError("event id was reused with different content")
            pending = connection.execute(
                """
                SELECT COUNT(*) AS event_count,
                       COALESCE(SUM(
                           LENGTH(CAST(payload_json AS BLOB))
                           + LENGTH(CAST(COALESCE(private_reveal_json, '') AS BLOB))
                       ), 0)
                           AS byte_count
                FROM events
                WHERE status IN (?, ?)
                """,
                (
                    EventStatus.WAITING_FOR_COMMITMENT.value,
                    EventStatus.TWEET_RECEIVED.value,
                ),
            ).fetchone()
            event_bytes = len(payload.encode()) + len((reveal_json or "").encode())
            if (
                int(pending["event_count"]) >= max_pending_events
                or int(pending["byte_count"]) + event_bytes > max_pending_bytes
            ):
                raise ProtocolError("miner pending queue capacity is exhausted")
            connection.execute(
                """
                INSERT INTO events(
                    event_id, kind, payload_json, private_reveal_json,
                    status, created_ns, batch_sequence
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (event_id, event.kind, payload, reveal_json, status.value, time.time_ns()),
            )

    def status(self, event_id: str) -> EventStatus | None:
        """Return the persisted platform status for a claim or submission."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return EventStatus(row["status"]) if row is not None else None

    def submissions(self) -> list[dict[str, object]]:
        """Return durable local submissions for platform status displays."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, payload_json, status, created_ns FROM events
                WHERE kind = 'submission'
                ORDER BY created_ns DESC, event_id
                """
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            event = _EVENT_ADAPTER.validate_json(row["payload_json"])
            if not isinstance(event, SubmissionEvent):
                raise ProtocolError("submission row contains a non-submission event")
            result.append(
                {
                    "submission_id": event.submission_id,
                    "campaign_id": event.campaign_id,
                    "tweet_id": event.tweet_id,
                    "claim_id": event.claim_id,
                    "status": str(row["status"]),
                    "created_ns": int(row["created_ns"]),
                }
            )
        return result

    def record_submission_result(self, submission_id: str, status: EventStatus) -> None:
        """Persist an authenticated remote attribution result idempotently."""

        if status not in {EventStatus.ATTRIBUTED, EventStatus.REJECTED}:
            raise ProtocolError("submission result must be attributed or rejected")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT kind, status FROM events WHERE event_id = ?", (submission_id,)
            ).fetchone()
            if row is None or row["kind"] != "submission":
                raise ProtocolError("submission result does not belong to this miner")
            existing = EventStatus(row["status"])
            if existing in {EventStatus.ATTRIBUTED, EventStatus.REJECTED} and existing != status:
                raise ProtocolError("final submission result changed")
            connection.execute(
                "UPDATE events SET status = ? WHERE event_id = ?",
                (status.value, submission_id),
            )

    def has_claim(self, claim_id: str) -> bool:
        """Return whether a claim and its private reveal belong to this miner."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM events
                WHERE event_id = ? AND kind = 'claim' AND private_reveal_json IS NOT NULL
                """,
                (claim_id,),
            ).fetchone()
        return row is not None

    def queued(self, *, limit: int) -> list[tuple[ProtocolEvent, int]]:
        """Return unbatched events in stable insertion order with their creation times."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json, created_ns FROM events
                WHERE batch_sequence IS NULL
                ORDER BY created_ns, event_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            (_EVENT_ADAPTER.validate_json(row["payload_json"]), int(row["created_ns"]))
            for row in rows
        ]

    def pending_batch(self) -> CommittedBatch | None:
        """Return the single prepared, not-yet-finalized batch after a restart."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT batch_json FROM batches WHERE state = 'prepared'"
            ).fetchone()
        return CommittedBatch.model_validate_json(row["batch_json"]) if row else None

    def prepare_batch(self, miner_hotkey: str, events: tuple[ProtocolEvent, ...]) -> CommittedBatch:
        """Atomically assign queued events to the next immutable prepared batch."""

        if not events:
            raise ProtocolError("cannot prepare an empty batch")
        event_ids = [
            event.claim_id if isinstance(event, ClaimEvent) else event.submission_id
            for event in events
        ]
        with self._transaction() as connection:
            pending = connection.execute(
                "SELECT batch_json FROM batches WHERE state = 'prepared'"
            ).fetchone()
            if pending is not None:
                return CommittedBatch.model_validate_json(pending["batch_json"])
            last = connection.execute(
                """
                SELECT sequence, batch_hash FROM batches
                WHERE state = 'finalized' ORDER BY sequence DESC LIMIT 1
                """
            ).fetchone()
            sequence = int(last["sequence"]) + 1 if last else 1
            previous_hash = str(last["batch_hash"]) if last else None
            rows = connection.execute(
                """
                SELECT event_id, payload_json, private_reveal_json, batch_sequence
                FROM events
                WHERE event_id IN (SELECT value FROM json_each(?))
                """,
                (json.dumps(event_ids, separators=(",", ":")),),
            ).fetchall()
            if len(rows) != len(event_ids) or any(
                row["batch_sequence"] is not None for row in rows
            ):
                raise ProtocolError("batch events must all be uniquely queued")
            by_id = {str(row["event_id"]): row for row in rows}
            reveals = self._reveals_for(connection, events)
            batch = CommittedBatch.create(
                miner_hotkey=miner_hotkey,
                sequence=sequence,
                previous_batch_hash=previous_hash,
                events=events,
                reveals=tuple(reveals),
            )
            connection.execute(
                """
                INSERT INTO batches(sequence, batch_json, batch_hash, state, created_ns)
                VALUES (?, ?, ?, 'prepared', ?)
                """,
                (sequence, batch.model_dump_json(), batch.batch_hash, time.time_ns()),
            )
            for event_id in event_ids:
                if event_id not in by_id:
                    raise ProtocolError("event disappeared while preparing batch")
                connection.execute(
                    "UPDATE events SET batch_sequence = ? WHERE event_id = ?",
                    (sequence, event_id),
                )
        return batch

    def preview_batch(self, miner_hotkey: str, events: tuple[ProtocolEvent, ...]) -> CommittedBatch:
        """Build the exact next batch without mutating queue state."""

        if not events:
            raise ProtocolError("cannot preview an empty batch")
        with self._transaction() as connection:
            pending = connection.execute(
                "SELECT 1 FROM batches WHERE state = 'prepared'"
            ).fetchone()
            if pending is not None:
                raise ProtocolError("cannot preview while a prepared batch exists")
            last = connection.execute(
                """
                SELECT sequence, batch_hash FROM batches
                WHERE state = 'finalized' ORDER BY sequence DESC LIMIT 1
                """
            ).fetchone()
            sequence = int(last["sequence"]) + 1 if last else 1
            previous_hash = str(last["batch_hash"]) if last else None
            reveals = self._reveals_for(connection, events)
        return CommittedBatch.create(
            miner_hotkey=miner_hotkey,
            sequence=sequence,
            previous_batch_hash=previous_hash,
            events=events,
            reveals=tuple(reveals),
        )

    @staticmethod
    def _reveals_for(
        connection: sqlite3.Connection,
        events: tuple[ProtocolEvent, ...],
    ) -> list[DraftReveal]:
        reveals: list[DraftReveal] = []
        for event in events:
            if not isinstance(event, SubmissionEvent) or event.claim_id is None:
                continue
            claim_row = connection.execute(
                """
                SELECT private_reveal_json FROM events
                WHERE event_id = ? AND kind = 'claim'
                """,
                (event.claim_id,),
            ).fetchone()
            if claim_row is None or claim_row["private_reveal_json"] is None:
                raise ProtocolError("submission references a claim without a local reveal")
            reveals.append(DraftReveal.model_validate_json(claim_row["private_reveal_json"]))
        return reveals

    def mark_finalized(self, sequence: int, position: CommitmentPosition) -> None:
        """Atomically finalize a batch and advance every platform event status."""

        with self._transaction() as connection:
            batch_row = connection.execute(
                "SELECT state, batch_json FROM batches WHERE sequence = ?", (sequence,)
            ).fetchone()
            if batch_row is None:
                raise ProtocolError("cannot finalize an unknown batch")
            if batch_row["state"] == "finalized":
                existing = connection.execute(
                    """
                    SELECT finalized_block, extrinsic_index FROM batches WHERE sequence = ?
                    """,
                    (sequence,),
                ).fetchone()
                if (
                    int(existing["finalized_block"]) != position.block
                    or int(existing["extrinsic_index"]) != position.extrinsic_index
                ):
                    raise ProtocolError("finalized batch position changed")
                return
            connection.execute(
                """
                UPDATE batches SET state = 'finalized', finalized_block = ?, extrinsic_index = ?
                WHERE sequence = ?
                """,
                (position.block, position.extrinsic_index, sequence),
            )
            connection.execute(
                """
                UPDATE events
                SET status = CASE kind
                    WHEN 'claim' THEN ?
                    ELSE ?
                END
                WHERE batch_sequence = ?
                """,
                (
                    EventStatus.SAFE_TO_POST.value,
                    EventStatus.VERIFICATION_PENDING.value,
                    sequence,
                ),
            )
            committed_batch = CommittedBatch.model_validate_json(batch_row["batch_json"])
            for event_index, event in enumerate(committed_batch.events):
                if not isinstance(event, ClaimEvent):
                    continue
                connection.execute(
                    """
                    INSERT OR IGNORE INTO active_claims(
                        claim_id, campaign_id, creator_x_id,
                        commitment_block, extrinsic_index, event_index
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.claim_id,
                        event.campaign_id,
                        event.creator_x_id,
                        position.block,
                        position.extrinsic_index,
                        event_index,
                    ),
                )
                active = connection.execute(
                    """
                    SELECT claim_id FROM active_claims
                    WHERE campaign_id = ? AND creator_x_id = ?
                    ORDER BY commitment_block, extrinsic_index, event_index, claim_id
                    """,
                    (event.campaign_id, event.creator_x_id),
                ).fetchall()
                for evicted in active[:-5]:
                    claim_id = str(evicted["claim_id"])
                    connection.execute(
                        "DELETE FROM active_claims WHERE claim_id = ?",
                        (claim_id,),
                    )
                    connection.execute(
                        "UPDATE events SET status = ? WHERE event_id = ?",
                        (EventStatus.EVICTED.value, claim_id),
                    )

    def active_claim_ids(self, campaign_id: str, creator_x_id: str) -> list[str]:
        """Return this miner's active five-slot FIFO in canonical commitment order."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT claim_id FROM active_claims
                WHERE campaign_id = ? AND creator_x_id = ?
                ORDER BY commitment_block, extrinsic_index, event_index, claim_id
                """,
                (campaign_id, creator_x_id),
            ).fetchall()
        return [str(row["claim_id"]) for row in rows]

    def finalized_batches(
        self,
        *,
        after_sequence: int,
        through_sequence: int | None,
        limit: int,
    ) -> tuple[list[tuple[CommittedBatch, CommitmentPosition]], bool]:
        """Page finalized batches with their durable chain positions."""

        with self._connect() as connection:
            if through_sequence is None:
                rows = connection.execute(
                    """
                    SELECT batch_json, finalized_block, extrinsic_index FROM batches
                    WHERE state = 'finalized' AND sequence > ?
                    ORDER BY sequence LIMIT ?
                    """,
                    (after_sequence, limit + 1),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT batch_json, finalized_block, extrinsic_index FROM batches
                    WHERE state = 'finalized' AND sequence > ? AND sequence <= ?
                    ORDER BY sequence LIMIT ?
                    """,
                    (after_sequence, through_sequence, limit + 1),
                ).fetchall()
        has_more = len(rows) > limit
        return (
            [
                (
                    CommittedBatch.model_validate_json(row["batch_json"]),
                    CommitmentPosition(
                        block=int(row["finalized_block"]),
                        extrinsic_index=int(row["extrinsic_index"]),
                    ),
                )
                for row in rows[:limit]
            ],
            has_more,
        )

    def close(self) -> None:
        """Compatibility hook; connections are intentionally short-lived."""
