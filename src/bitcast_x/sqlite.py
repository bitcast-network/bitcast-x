"""Shared SQLite connections, forward-only migrations and online backups."""

import sqlite3
import threading
import weakref
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from bitcast_x.errors import ProtocolError


def hold_open(owner: object, path: Path) -> Callable[[], object]:
    """Keep ``path`` open while ``owner`` lives; the returned function releases it early.

    Stores open a connection per call, and closing a WAL database's last connection
    checkpoints it and deletes its -wal and -shm files, which the next call recreates.
    On network file systems such as EFS each of those steps is a round trip, so one
    idle connection is held for the owner's lifetime instead.
    """

    # Never used for queries, so whichever thread releases it may close it.
    connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
    # Opens the file and its log like a store connection; run to completion so the
    # idle connection never holds a read snapshot that would block checkpoints.
    connection.execute("PRAGMA journal_mode = WAL").fetchall()
    return weakref.finalize(owner, connection.close)


def connect(path: Path) -> sqlite3.Connection:
    """Open an autocommit connection with the durability settings every store uses."""

    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


@contextmanager
def session(path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a connection for reads and self-contained writes, then close it."""

    connection = connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


@contextmanager
def transaction(path: Path, lock: threading.RLock) -> Iterator[sqlite3.Connection]:
    """Run one serialized ``BEGIN IMMEDIATE`` transaction, rolling back on failure."""

    with lock:
        connection = connect(path)
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


def apply_migrations(connection: sqlite3.Connection, migrations: Sequence[str]) -> None:
    """Apply ordered idempotent schema migrations and reject newer state files."""

    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > len(migrations):
        raise ProtocolError(
            f"state schema version {current} is newer than supported version {len(migrations)}"
        )
    for version, script in enumerate(migrations[current:], start=current + 1):
        try:
            connection.executescript(
                f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;"
            )
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
            raise


def backup_database(source: Path, destination: Path) -> None:
    """Create a consistent SQLite backup without copying live WAL files."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source, timeout=30)
    destination_connection = sqlite3.connect(destination, timeout=30)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
