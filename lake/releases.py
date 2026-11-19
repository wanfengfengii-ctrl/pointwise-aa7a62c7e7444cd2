import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class LakeRelease:
    result_number: int
    batch_id: str
    unit: str
    receipt: str


def initialize_releases(conn: sqlite3.Connection) -> None:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS lake_results (
            result_number INTEGER PRIMARY KEY,
            batch_id TEXT NOT NULL,
            unit TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS lake_receipts (
            result_number INTEGER PRIMARY KEY REFERENCES lake_results(result_number),
            receipt TEXT UNIQUE NOT NULL
        )
    ''')


def record_release(conn: sqlite3.Connection, batch_id: str, unit: str) -> LakeRelease:
    cursor = conn.execute('INSERT INTO lake_results (batch_id, unit) VALUES (?, ?)', (batch_id, unit))
    result_number = cursor.lastrowid
    receipt = f'receipt-{result_number}'
    conn.execute('INSERT INTO lake_receipts (result_number, receipt) VALUES (?, ?)',
                 (result_number, receipt))
    return LakeRelease(result_number, batch_id, unit, receipt)


def find_release(conn: sqlite3.Connection, result_number: int) -> LakeRelease:
    if type(result_number) is not int or not 1 <= result_number < 2**63:
        raise KeyError(f'unknown result number {result_number!r}')
    record = conn.execute('''
        SELECT result.batch_id, result.unit, receipt.receipt
        FROM lake_results AS result
        JOIN lake_receipts AS receipt USING (result_number)
        WHERE result.result_number = ?
    ''', (result_number,)).fetchone()
    if record is None:
        raise KeyError(f'unknown result number {result_number!r}')
    return LakeRelease(result_number, *record)
