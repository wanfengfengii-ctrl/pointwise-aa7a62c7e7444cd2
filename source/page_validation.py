import sqlite3

from catalog.codebook import decode_code
from catalog.dictionaries import get_dictionary
from catalog.units import require_batch_unit, validate_unit
from source.pages import SourcePage


def validate_page(conn: sqlite3.Connection, page: SourcePage, batch_unit: str) -> None:
    validate_unit(batch_unit)
    try:
        book = get_dictionary(conn, page.dictionary_version)
    except KeyError as exc:
        raise ValueError(
            f'page {page.page_id!r}: unknown dictionary {page.dictionary_version!r}'
        ) from exc
    try:
        require_batch_unit(batch_unit, book.unit)
    except ValueError as exc:
        raise ValueError(f'page {page.page_id!r}: {exc}') from exc
    for slot, code in enumerate(page.codes):
        try:
            decode_code(book, code)
        except ValueError as exc:
            raise ValueError(f'page {page.page_id!r} slot {slot}: {exc}') from exc
