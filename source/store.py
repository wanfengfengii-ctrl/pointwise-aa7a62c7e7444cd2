from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from os import PathLike


@contextmanager
def source_transaction(conn: sqlite3.Connection, *, write: bool = True) -> Iterator[None]:
    conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
    try:
        yield
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def open_source(path: str | PathLike[str]) -> sqlite3.Connection:
    from catalog.dictionaries import initialize_dictionaries
    from exchange.issuance_store import initialize_issuances
    from exchange.review_commitments import initialize_review_commitments
    from source.batches import initialize_batches
    from source.pages import initialize_pages

    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute('PRAGMA foreign_keys = ON')
        with source_transaction(conn):
            initialize_dictionaries(conn)
            initialize_pages(conn)
            initialize_batches(conn)
            initialize_issuances(conn)
            initialize_review_commitments(conn)
    except BaseException:
        conn.close()
        raise
    return conn
