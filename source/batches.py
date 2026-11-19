import sqlite3
from dataclasses import dataclass

from source.pages import SourcePage, load_page


@dataclass(frozen=True)
class FrozenBatch:
    batch_id: str
    unit: str
    pages: tuple[SourcePage, ...]


def initialize_batches(conn: sqlite3.Connection) -> None:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS frozen_batches (
            batch_id TEXT PRIMARY KEY NOT NULL,
            unit TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS batch_pages (
            batch_id TEXT NOT NULL REFERENCES frozen_batches(batch_id),
            position INTEGER NOT NULL CHECK (position >= 0),
            page_id TEXT NOT NULL REFERENCES source_pages(page_id),
            PRIMARY KEY (batch_id, position),
            UNIQUE (batch_id, page_id)
        )
    ''')


def save_frozen_batch(conn: sqlite3.Connection, batch: FrozenBatch) -> None:
    inserted = conn.execute('''
        INSERT INTO frozen_batches (batch_id, unit) VALUES (?, ?)
        ON CONFLICT(batch_id) DO NOTHING
    ''', (batch.batch_id, batch.unit))
    if inserted.rowcount == 0:
        raise ValueError(f'frozen batch {batch.batch_id!r} already exists')
    conn.executemany('''
        INSERT INTO batch_pages (batch_id, position, page_id) VALUES (?, ?, ?)
    ''', ((batch.batch_id, position, page.page_id) for position, page in enumerate(batch.pages)))


def load_frozen_batch(conn: sqlite3.Connection, batch_id: str) -> FrozenBatch:
    header = conn.execute(
        'SELECT unit FROM frozen_batches WHERE batch_id = ?', (batch_id,),
    ).fetchone()
    if header is None:
        raise KeyError(f'unknown frozen batch {batch_id!r}')
    ordered_ids = conn.execute(
        'SELECT page_id FROM batch_pages WHERE batch_id = ? ORDER BY position', (batch_id,),
    )
    pages = tuple(load_page(conn, page_id) for (page_id,) in ordered_ids)
    return FrozenBatch(batch_id, header[0], pages)
