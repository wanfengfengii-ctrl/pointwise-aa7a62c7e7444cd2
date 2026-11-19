import sqlite3
from dataclasses import dataclass

from catalog.units import validate_label


@dataclass(frozen=True)
class SourcePage:
    page_id: str
    dictionary_version: str
    codes: bytes

    def __post_init__(self) -> None:
        validate_label(self.page_id, 'page ID')
        validate_label(self.dictionary_version, 'dictionary version')
        if not isinstance(self.codes, bytes):
            raise ValueError('source page codes must be bytes')


def initialize_pages(conn: sqlite3.Connection) -> None:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS source_pages (
            page_id TEXT PRIMARY KEY NOT NULL,
            dictionary_version TEXT NOT NULL REFERENCES dictionaries(version),
            codes BLOB NOT NULL
        )
    ''')


def store_page(conn: sqlite3.Connection, page: SourcePage) -> None:
    """Save an already validated page within the caller's write transaction."""
    inserted = conn.execute('''
        INSERT INTO source_pages (page_id, dictionary_version, codes)
        VALUES (?, ?, ?) ON CONFLICT(page_id) DO NOTHING
    ''', (page.page_id, page.dictionary_version, page.codes))
    if inserted.rowcount == 0 and load_page(conn, page.page_id) != page:
        raise ValueError(f'conflicting definition for source page {page.page_id!r}')


def load_page(conn: sqlite3.Connection, page_id: str) -> SourcePage:
    record = conn.execute(
        'SELECT dictionary_version, codes FROM source_pages WHERE page_id = ?',
        (page_id,),
    ).fetchone()
    if record is None:
        raise KeyError(f'unknown source page {page_id!r}')
    version, codes = record
    return SourcePage(page_id, version, codes)
