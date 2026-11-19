import sqlite3
from collections.abc import Iterable

from catalog.units import validate_label, validate_unit
from source.batches import FrozenBatch, load_frozen_batch, save_frozen_batch
from source.page_validation import validate_page
from source.pages import SourcePage, store_page
from source.store import source_transaction


def freeze(
    conn: sqlite3.Connection, batch_id: str, unit: str, pages: Iterable[SourcePage],
) -> str:
    validate_label(batch_id, 'batch ID')
    validate_unit(unit)
    ordered = tuple(pages)
    if any(not isinstance(page, SourcePage) for page in ordered):
        raise ValueError('freeze expects SourcePage objects')
    if len({page.page_id for page in ordered}) != len(ordered):
        raise ValueError('duplicate page ID in frozen batch')
    with source_transaction(conn):
        for page in ordered:
            validate_page(conn, page, unit)
            store_page(conn, page)
        save_frozen_batch(conn, FrozenBatch(batch_id, unit, ordered))
    return batch_id


def get_frozen_batch(conn: sqlite3.Connection, batch_id: str) -> FrozenBatch:
    validate_label(batch_id, 'batch ID')
    # A committed batch, its members and their order are immutable.
    return load_frozen_batch(conn, batch_id)
