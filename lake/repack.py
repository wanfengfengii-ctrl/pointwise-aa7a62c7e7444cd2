import sqlite3

from lake.layout import cut_pages, load_layout, replace_layout
from lake.releases import find_release
from lake.store import lake_write_transaction


def repack(conn: sqlite3.Connection, result_number: int, page_size: int) -> int:
    with lake_write_transaction(conn):
        release = find_release(conn, result_number)
        current = load_layout(conn, release.result_number)
        ordered = sorted((row for page in current for row in page.rows), key=lambda row: row.ordinal)
        replacement = cut_pages(ordered, page_size)
        replace_layout(conn, release.result_number, replacement)
    return len(replacement)
