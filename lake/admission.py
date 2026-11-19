from __future__ import annotations

import sqlite3

from catalog.units import require_batch_unit
from exchange.envelope import decode_v1
from exchange.evidence import index_dictionary_evidence
from lake.layout import initial_pages, replace_layout
from lake.materialize import materialize_rows
from lake.releases import LakeRelease, record_release
from lake.store import lake_write_transaction


def accept(conn: sqlite3.Connection, body: bytes | str) -> LakeRelease:
    envelope = decode_v1(body)
    dictionaries = index_dictionary_evidence(envelope.dictionaries)
    for page in envelope.pages:
        book = dictionaries.get(page.dictionary_version)
        if book is None:
            raise ValueError(
                f'page {page.page_id!r}: missing dictionary evidence for {page.dictionary_version!r}'
            )
        try:
            require_batch_unit(envelope.unit, book.unit)
        except ValueError as exc:
            raise ValueError(f'page {page.page_id!r}: {exc}') from exc
    rows = materialize_rows(envelope, dictionaries)
    pages = initial_pages(rows, (len(page.codes) for page in envelope.pages))
    with lake_write_transaction(conn):
        release = record_release(conn, envelope.batch_id, envelope.unit)
        replace_layout(conn, release.result_number, pages)
    return release
