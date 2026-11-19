import sqlite3

from exchange.issuance_store import load_issuance
from source.store import source_transaction


def export_by_issue_number(conn: sqlite3.Connection, issue_number: int) -> bytes:
    with source_transaction(conn, write=False):
        issued = load_issuance(conn, issue_number)
    return issued.body
