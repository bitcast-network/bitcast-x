"""Small forward-only SQLite migration and online-backup utilities."""

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from bitcast_x.errors import ProtocolError


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
