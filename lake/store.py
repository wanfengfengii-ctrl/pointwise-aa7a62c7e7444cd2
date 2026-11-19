from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from os import PathLike


@contextmanager
def _transaction(conn: sqlite3.Connection, begin: str) -> Iterator[None]:
    conn.execute(begin)
    try:
        yield
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def lake_write_transaction(conn: sqlite3.Connection):
    return _transaction(conn, 'BEGIN IMMEDIATE')


def lake_read_transaction(conn: sqlite3.Connection):
    return _transaction(conn, 'BEGIN')


def open_lake(path: str | PathLike[str]) -> sqlite3.Connection:
    from lake.layout import initialize_layout
    from lake.releases import initialize_releases
    from lake.review_store import initialize_review

    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute('PRAGMA foreign_keys = ON')
        with lake_write_transaction(conn):
            initialize_releases(conn)
            initialize_layout(conn)
            initialize_review(conn)
    except BaseException:
        conn.close()
        raise
    return conn
