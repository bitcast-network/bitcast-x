"""Crash-safe SQLite state for the reference miner and reusable SDK."""

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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
    CONSUMED = "consumed"
    TWEET_RECEIVED = "tweet_received"
    VERIFICATION_PENDING = "verification_pending"
    ATTRIBUTED = "attributed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class OperationMetadata:
    """Application correlation fields stored without protocol-private material."""

    idempotency_key: str
    request_fingerprint: str
    campaign_snapshot_id: str
    ecosystem_ids: tuple[str, ...]
    creator_x_id: str
    external_id: str | None = None


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
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            batch_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(batches)")
            }
            event_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(events)")}
            if (
                current <= 2
                and "history_id" in batch_columns
                and "batch_history_id" in event_columns
            ):
                # Recover an otherwise-current store whose version pragma was
                # cleared without destructively replaying the table rebuild.
                connection.execute("PRAGMA user_version = 3")
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
                    """
                CREATE TABLE IF NOT EXISTS operation_metadata (
                    event_id TEXT PRIMARY KEY REFERENCES events(event_id),
                    kind TEXT NOT NULL CHECK (kind IN ('claim', 'submission')),
                    idempotency_key TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    external_id TEXT,
                    campaign_snapshot_id TEXT NOT NULL,
                    ecosystem_ids_json TEXT NOT NULL,
                    creator_x_id TEXT NOT NULL,
                    consumed_by_submission_id TEXT,
                    evicted_by_claim_id TEXT,
                    updated_ns INTEGER NOT NULL,
                    UNIQUE(kind, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_operation_metadata_filters
                    ON operation_metadata(kind, creator_x_id, external_id);
                CREATE TABLE IF NOT EXISTS operation_ecosystem (
                    event_id TEXT NOT NULL REFERENCES operation_metadata(event_id),
                    ecosystem_id TEXT NOT NULL,
                    PRIMARY KEY(event_id, ecosystem_id)
                );
                CREATE INDEX IF NOT EXISTS idx_operation_ecosystem_lookup
                    ON operation_ecosystem(ecosystem_id, event_id);
                    """,
                    """
                DROP INDEX IF EXISTS idx_one_prepared_batch;
                ALTER TABLE batches RENAME TO batches_pre_history;
                CREATE TABLE batches (
                    history_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    batch_json TEXT NOT NULL,
                    batch_hash TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (state IN ('prepared', 'finalized')),
                    created_ns INTEGER NOT NULL,
                    finalized_block INTEGER,
                    extrinsic_index INTEGER,
                    PRIMARY KEY(history_id, sequence)
                );
                INSERT INTO batches(
                    history_id, sequence, batch_json, batch_hash, state,
                    created_ns, finalized_block, extrinsic_index
                )
                SELECT '', sequence, batch_json, batch_hash, state,
                       created_ns, finalized_block, extrinsic_index
                FROM batches_pre_history;
                DROP TABLE batches_pre_history;
                CREATE UNIQUE INDEX idx_one_prepared_batch
                    ON batches(state) WHERE state = 'prepared';
                ALTER TABLE events ADD COLUMN batch_history_id TEXT NOT NULL DEFAULT '';
                DROP TABLE IF EXISTS history_resume;
                CREATE TABLE history_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    history_id TEXT NOT NULL,
                    cutoff_ns INTEGER NOT NULL
                );
                    """,
                ),
            )

    def current_history_id(self) -> str | None:
        """Return the active history ID, or ``None`` for the legacy chain."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT history_id FROM history_state WHERE singleton = 1"
            ).fetchone()
        return str(row["history_id"]) if row is not None else None

    def history_has_batches(self, history_id: str) -> bool:
        """Return whether a history ID has already anchored any local batch."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM batches WHERE history_id = ? LIMIT 1", (history_id,)
            ).fetchone()
        return row is not None

    def start_history(self, history_id: str) -> str:
        """Atomically abandon pending work and activate an unused history ID."""

        try:
            valid = len(bytes.fromhex(history_id)) == 32
        except ValueError:
            valid = False
        if not valid:
            raise ProtocolError("history_id must be a 32-byte hexadecimal value")

        with self._transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM batches WHERE history_id = ? LIMIT 1", (history_id,)
                ).fetchone()
                is not None
            ):
                raise ProtocolError("history_id was already used by this miner")
            cutoff_ns = time.time_ns()
            connection.execute("DELETE FROM batches WHERE state = 'prepared'")
            connection.execute(
                """
                UPDATE events
                SET status = ?, rejection_reason = 'history_resumed'
                WHERE created_ns <= ?
                  AND status IN (?, ?, ?, ?)
                """,
                (
                    EventStatus.REJECTED.value,
                    cutoff_ns,
                    EventStatus.WAITING_FOR_COMMITMENT.value,
                    EventStatus.SAFE_TO_POST.value,
                    EventStatus.TWEET_RECEIVED.value,
                    EventStatus.VERIFICATION_PENDING.value,
                ),
            )
            connection.execute("DELETE FROM active_claims")
            connection.execute(
                """
                INSERT INTO history_state(singleton, history_id, cutoff_ns)
                VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    history_id = excluded.history_id,
                    cutoff_ns = excluded.cutoff_ns
                """,
                (history_id, cutoff_ns),
            )
        return history_id

    def enqueue(
        self,
        event: ProtocolEvent,
        *,
        reveal: DraftReveal | None = None,
        metadata: OperationMetadata | None = None,
        max_pending_events: int = 10_000,
        max_pending_bytes: int = 50_000_000,
    ) -> str:
        """Persist an event and application idempotency record atomically."""

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
        if metadata is not None:
            event_creator_x_id = event.creator_x_id
            if event_creator_x_id is None or event_creator_x_id != metadata.creator_x_id:
                raise ProtocolError("operation creator_x_id must match the committed event")
        with self._transaction() as connection:
            if metadata is not None:
                idempotent = connection.execute(
                    """
                    SELECT event_id, request_fingerprint FROM operation_metadata
                    WHERE kind = ? AND idempotency_key = ?
                    """,
                    (event.kind, metadata.idempotency_key),
                ).fetchone()
                if idempotent is not None:
                    if idempotent["request_fingerprint"] != metadata.request_fingerprint:
                        raise ProtocolError("idempotency key was reused with different input")
                    return str(idempotent["event_id"])
            existing = connection.execute(
                "SELECT payload_json, private_reveal_json FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["payload_json"] == payload
                    and existing["private_reveal_json"] == reveal_json
                ):
                    return event_id
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
            if metadata is not None:
                connection.execute(
                    """
                    INSERT INTO operation_metadata(
                        event_id, kind, idempotency_key, request_fingerprint,
                        external_id, campaign_snapshot_id, ecosystem_ids_json,
                        creator_x_id, updated_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event.kind,
                        metadata.idempotency_key,
                        metadata.request_fingerprint,
                        metadata.external_id,
                        metadata.campaign_snapshot_id,
                        json.dumps(metadata.ecosystem_ids, separators=(",", ":")),
                        metadata.creator_x_id,
                        time.time_ns(),
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO operation_ecosystem(event_id, ecosystem_id)
                    VALUES (?, ?)
                    """,
                    [(event_id, ecosystem_id) for ecosystem_id in metadata.ecosystem_ids],
                )
        return event_id

    def status(self, event_id: str) -> EventStatus | None:
        """Return the persisted platform status for a claim or submission."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return EventStatus(row["status"]) if row is not None else None

    def receipt(self, event_id: str) -> dict[str, object] | None:
        """Return one application-safe local receipt with its chain position."""

        receipts = self.receipts(event_id=event_id)
        return receipts[0] if receipts else None

    def receipts(
        self,
        *,
        kind: str | None = None,
        event_id: str | None = None,
        campaign_id: str | None = None,
        creator_x_id: str | None = None,
        external_id: str | None = None,
        ecosystem_ids: tuple[str, ...] = (),
    ) -> list[dict[str, object]]:
        """List durable receipts using indexed application correlation filters."""

        ecosystem_json = json.dumps(ecosystem_ids, separators=(",", ":"))
        query = """
            SELECT e.event_id, e.kind, e.payload_json, e.status, e.created_ns,
                   e.batch_history_id, e.batch_sequence, e.rejection_reason,
                   b.batch_json, b.batch_hash, b.state AS batch_state, b.finalized_block,
                   b.extrinsic_index, m.external_id, m.campaign_snapshot_id,
                   m.ecosystem_ids_json, m.creator_x_id,
                   m.consumed_by_submission_id, m.evicted_by_claim_id,
                   COALESCE(m.updated_ns, e.created_ns) AS updated_ns
            FROM events e
            LEFT JOIN batches b
              ON b.history_id = e.batch_history_id
             AND b.sequence = e.batch_sequence
            LEFT JOIN operation_metadata m ON m.event_id = e.event_id
            WHERE (? IS NULL OR e.kind = ?)
              AND (? IS NULL OR e.event_id = ?)
              AND (? IS NULL OR m.creator_x_id = ?)
              AND (? IS NULL OR m.external_id = ?)
              AND (
                ? = '[]' OR EXISTS (
                    SELECT 1 FROM operation_ecosystem oe
                    WHERE oe.event_id = e.event_id
                      AND oe.ecosystem_id IN (SELECT value FROM json_each(?))
                )
              )
            ORDER BY e.created_ns DESC, e.event_id
        """
        parameters = (
            kind,
            kind,
            event_id,
            event_id,
            creator_x_id,
            creator_x_id,
            external_id,
            external_id,
            ecosystem_json,
            ecosystem_json,
        )
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        receipts: list[dict[str, object]] = []
        for row in rows:
            event = _EVENT_ADAPTER.validate_json(row["payload_json"])
            if campaign_id is not None and event.campaign_id != campaign_id:
                continue
            event_index = None
            if row["batch_json"] is not None:
                batch = CommittedBatch.model_validate_json(row["batch_json"])
                ids = [
                    item.claim_id if isinstance(item, ClaimEvent) else item.submission_id
                    for item in batch.events
                ]
                event_index = ids.index(str(row["event_id"]))
            payload = event.model_dump(mode="json")
            receipts.append(
                {
                    **payload,
                    "event_id": str(row["event_id"]),
                    "kind": str(row["kind"]),
                    "status": str(row["status"]),
                    "created_ns": int(row["created_ns"]),
                    "updated_ns": int(row["updated_ns"]),
                    "external_id": row["external_id"],
                    "campaign_snapshot_id": row["campaign_snapshot_id"],
                    "ecosystem_ids": (
                        json.loads(row["ecosystem_ids_json"]) if row["ecosystem_ids_json"] else []
                    ),
                    "creator_x_id": row["creator_x_id"] or event.creator_x_id,
                    "commitment": {
                        "status": ("finalized" if row["batch_state"] == "finalized" else "queued"),
                        "batch_sequence": row["batch_sequence"],
                        "history_id": batch.history_id if row["batch_json"] else None,
                        "batch_hash": (
                            f"sha256-{row['batch_hash']}" if row["batch_hash"] else None
                        ),
                        "block": row["finalized_block"],
                        "extrinsic_index": row["extrinsic_index"],
                        "event_index": event_index,
                        "failure_reason": row["rejection_reason"],
                    },
                    "consumed_by_submission_id": row["consumed_by_submission_id"],
                    "evicted_by_claim_id": row["evicted_by_claim_id"],
                }
            )
        return receipts

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
                    "creator_x_id": event.creator_x_id,
                    "status": str(row["status"]),
                    "created_ns": int(row["created_ns"]),
                }
            )
        return result

    def submission_id(
        self,
        *,
        campaign_id: str,
        tweet_id: str,
        claim_id: str | None,
        creator_x_id: str,
    ) -> str | None:
        """Return the oldest durable submission for an idempotency identity."""

        for submission in reversed(self.submissions()):
            if (
                submission["campaign_id"] == campaign_id
                and submission["tweet_id"] == tweet_id
                and submission["claim_id"] == claim_id
                and submission["creator_x_id"] == creator_x_id
            ):
                return str(submission["submission_id"])
        return None

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
                WHERE batch_sequence IS NULL AND status IN (?, ?)
                ORDER BY created_ns, event_id
                LIMIT ?
                """,
                (
                    EventStatus.WAITING_FOR_COMMITMENT.value,
                    EventStatus.TWEET_RECEIVED.value,
                    limit,
                ),
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
            history = connection.execute(
                "SELECT history_id FROM history_state WHERE singleton = 1"
            ).fetchone()
            history_id = str(history["history_id"]) if history is not None else ""
            last = connection.execute(
                """
                SELECT sequence, batch_hash FROM batches
                WHERE state = 'finalized' AND history_id = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (history_id,),
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
                history_id=history_id or None,
            )
            connection.execute(
                """
                INSERT INTO batches(
                    history_id, sequence, batch_json, batch_hash, state, created_ns
                ) VALUES (?, ?, ?, ?, 'prepared', ?)
                """,
                (
                    history_id,
                    sequence,
                    batch.model_dump_json(),
                    batch.batch_hash,
                    time.time_ns(),
                ),
            )
            for event_id in event_ids:
                if event_id not in by_id:
                    raise ProtocolError("event disappeared while preparing batch")
                connection.execute(
                    """
                    UPDATE events SET batch_history_id = ?, batch_sequence = ?
                    WHERE event_id = ?
                    """,
                    (history_id, sequence, event_id),
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
            history = connection.execute(
                "SELECT history_id FROM history_state WHERE singleton = 1"
            ).fetchone()
            history_id = str(history["history_id"]) if history is not None else ""
            last = connection.execute(
                """
                SELECT sequence, batch_hash FROM batches
                WHERE state = 'finalized' AND history_id = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (history_id,),
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
            history_id=history_id or None,
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

    def mark_finalized(self, batch: CommittedBatch, position: CommitmentPosition) -> None:
        """Atomically finalize a batch and advance every platform event status."""

        with self._transaction() as connection:
            history_id = batch.history_id or ""
            batch_row = connection.execute(
                """
                SELECT state, batch_json FROM batches
                WHERE history_id = ? AND sequence = ?
                """,
                (history_id, batch.sequence),
            ).fetchone()
            if batch_row is None:
                raise ProtocolError("cannot finalize an unknown batch")
            if batch_row["state"] == "finalized":
                existing = connection.execute(
                    """
                    SELECT finalized_block, extrinsic_index FROM batches
                    WHERE history_id = ? AND sequence = ?
                    """,
                    (history_id, batch.sequence),
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
                WHERE history_id = ? AND sequence = ?
                """,
                (position.block, position.extrinsic_index, history_id, batch.sequence),
            )
            connection.execute(
                """
                UPDATE events
                SET status = CASE kind
                    WHEN 'claim' THEN ?
                    ELSE ?
                END
                WHERE batch_history_id = ? AND batch_sequence = ?
                """,
                (
                    EventStatus.SAFE_TO_POST.value,
                    EventStatus.VERIFICATION_PENDING.value,
                    history_id,
                    batch.sequence,
                ),
            )
            connection.execute(
                """
                UPDATE operation_metadata SET updated_ns = ?
                WHERE event_id IN (
                    SELECT event_id FROM events
                    WHERE batch_history_id = ? AND batch_sequence = ?
                )
                """,
                (time.time_ns(), history_id, batch.sequence),
            )
            committed_batch = CommittedBatch.model_validate_json(batch_row["batch_json"])
            for event_index, event in enumerate(committed_batch.events):
                if isinstance(event, SubmissionEvent):
                    if event.claim_id is not None:
                        connection.execute(
                            "DELETE FROM active_claims WHERE claim_id = ?",
                            (event.claim_id,),
                        )
                        connection.execute(
                            "UPDATE events SET status = ? WHERE event_id = ?",
                            (EventStatus.CONSUMED.value, event.claim_id),
                        )
                        connection.execute(
                            """
                            UPDATE operation_metadata
                            SET consumed_by_submission_id = ?, updated_ns = ?
                            WHERE event_id = ?
                            """,
                            (event.submission_id, time.time_ns(), event.claim_id),
                        )
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
                    connection.execute(
                        """
                        UPDATE operation_metadata
                        SET evicted_by_claim_id = ?, updated_ns = ?
                        WHERE event_id = ?
                        """,
                        (event.claim_id, time.time_ns(), claim_id),
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
            history = connection.execute(
                "SELECT history_id FROM history_state WHERE singleton = 1"
            ).fetchone()
            history_id = str(history["history_id"]) if history is not None else ""
            if through_sequence is None:
                rows = connection.execute(
                    """
                    SELECT batch_json, finalized_block, extrinsic_index FROM batches
                    WHERE state = 'finalized' AND history_id = ? AND sequence > ?
                    ORDER BY sequence LIMIT ?
                    """,
                    (history_id, after_sequence, limit + 1),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT batch_json, finalized_block, extrinsic_index FROM batches
                    WHERE state = 'finalized' AND history_id = ?
                      AND sequence > ? AND sequence <= ?
                    ORDER BY sequence LIMIT ?
                    """,
                    (history_id, after_sequence, through_sequence, limit + 1),
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
