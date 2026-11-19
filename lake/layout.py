import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import groupby

from lake.materialize import ObservationRow


@dataclass(frozen=True)
class PhysicalPage:
    page_number: int
    rows: tuple[ObservationRow, ...]


def initialize_layout(conn: sqlite3.Connection) -> None:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS physical_pages (
            result_number INTEGER NOT NULL REFERENCES lake_results(result_number),
            page_number INTEGER NOT NULL CHECK (page_number >= 0),
            PRIMARY KEY (result_number, page_number)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS physical_rows (
            result_number INTEGER NOT NULL,
            page_number INTEGER NOT NULL,
            physical_slot INTEGER NOT NULL CHECK (physical_slot >= 0),
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            value_hex TEXT,
            origin_page TEXT NOT NULL,
            origin_slot INTEGER NOT NULL CHECK (origin_slot >= 0),
            dictionary_version TEXT NOT NULL,
            PRIMARY KEY (result_number, page_number, physical_slot),
            UNIQUE (result_number, ordinal),
            FOREIGN KEY (result_number, page_number)
                REFERENCES physical_pages(result_number, page_number) ON DELETE CASCADE
        )
    ''')


def initial_pages(rows: tuple[ObservationRow, ...], sizes: Iterable[int]) -> tuple[PhysicalPage, ...]:
    pages = []
    offset = 0
    for size in sizes:
        end = offset + size
        if size:
            pages.append(PhysicalPage(len(pages), rows[offset:end]))
        offset = end
    return tuple(pages)


def cut_pages(rows: Sequence[ObservationRow], page_size: int) -> tuple[PhysicalPage, ...]:
    if type(page_size) is not int or page_size <= 0:
        raise ValueError('physical page size must be a positive integer')
    return tuple(
        PhysicalPage(page_number, tuple(rows[start:start + page_size]))
        for page_number, start in enumerate(range(0, len(rows), page_size))
    )


def _stored_rows(result_number: int, pages: Iterable[PhysicalPage]):
    for page in pages:
        for physical_slot, row in enumerate(page.rows):
            value_hex = None if row.value is None else hex(row.value)
            yield (result_number, page.page_number, physical_slot, row.ordinal,
                   value_hex, row.page_id, row.slot, row.dictionary_version)


def replace_layout(conn: sqlite3.Connection, result_number: int, pages: Sequence[PhysicalPage]) -> None:
    conn.execute('DELETE FROM physical_pages WHERE result_number = ?', (result_number,))
    conn.executemany('INSERT INTO physical_pages (result_number, page_number) VALUES (?, ?)',
                     ((result_number, page.page_number) for page in pages))
    conn.executemany('''
        INSERT INTO physical_rows (
            result_number, page_number, physical_slot, ordinal,
            value_hex, origin_page, origin_slot, dictionary_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', _stored_rows(result_number, pages))


def load_layout(conn: sqlite3.Connection, result_number: int) -> tuple[PhysicalPage, ...]:
    records = conn.execute('''
        SELECT page_number, ordinal, value_hex, origin_page, origin_slot, dictionary_version
        FROM physical_rows WHERE result_number = ? ORDER BY page_number, physical_slot
    ''', (result_number,))
    pages = []
    for page_number, group in groupby(records, key=lambda record: record[0]):
        rows = []
        for _, ordinal, value_hex, origin_page, origin_slot, version in group:
            value = None if value_hex is None else int(value_hex, 16)
            rows.append(ObservationRow(ordinal, value, origin_page, origin_slot, version))
        pages.append(PhysicalPage(page_number, tuple(rows)))
    return tuple(pages)
