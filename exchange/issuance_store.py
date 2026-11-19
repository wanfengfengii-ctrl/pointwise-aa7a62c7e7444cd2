import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class IssuedPackage:
    issue_number: int
    batch_id: str
    body: bytes


def initialize_issuances(conn: sqlite3.Connection) -> None:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS issuances (
            issue_number INTEGER PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES frozen_batches(batch_id),
            body BLOB NOT NULL
        )
    ''')


def save_issuance(conn: sqlite3.Connection, batch_id: str, body: bytes) -> IssuedPackage:
    cursor = conn.execute('INSERT INTO issuances (batch_id, body) VALUES (?, ?)', (batch_id, body))
    return IssuedPackage(cursor.lastrowid, batch_id, body)


def load_issuance(conn: sqlite3.Connection, issue_number: int) -> IssuedPackage:
    if type(issue_number) is not int or not 1 <= issue_number < 2**63:
        raise KeyError(f'unknown issue number {issue_number!r}')
    record = conn.execute(
        'SELECT batch_id, body FROM issuances WHERE issue_number = ?', (issue_number,),
    ).fetchone()
    if record is None:
        raise KeyError(f'unknown issue number {issue_number!r}')
    return IssuedPackage(issue_number, record[0], record[1])
