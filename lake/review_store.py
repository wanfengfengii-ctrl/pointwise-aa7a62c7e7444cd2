"""Lake-side storage for registered review tasks and verified window rows."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewTask:
    task_id: str
    commitment: bytes
    commitment_id: str
    batch_id: str
    unit: str
    start: int
    stop: int
    result_number: int | None


def initialize_review(conn: sqlite3.Connection) -> None:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS review_tasks (
            task_id TEXT PRIMARY KEY NOT NULL,
            commitment BLOB NOT NULL,
            commitment_id TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            unit TEXT NOT NULL,
            window_start INTEGER NOT NULL CHECK (window_start >= 0),
            window_stop INTEGER NOT NULL CHECK (window_stop > window_start),
            result_number INTEGER UNIQUE REFERENCES lake_results(result_number)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS review_rows (
            task_id TEXT NOT NULL REFERENCES review_tasks(task_id),
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            value_hex TEXT,
            origin_page TEXT NOT NULL,
            origin_slot INTEGER NOT NULL CHECK (origin_slot >= 0),
            dictionary_version TEXT NOT NULL,
            PRIMARY KEY (task_id, ordinal)
        )
    ''')


def load_task(conn: sqlite3.Connection, task_id: str) -> ReviewTask:
    record = conn.execute('''
        SELECT commitment, commitment_id, batch_id, unit,
               window_start, window_stop, result_number
        FROM review_tasks WHERE task_id = ?
    ''', (task_id,)).fetchone()
    if record is None:
        raise KeyError(f'unknown review task {task_id!r}')
    return ReviewTask(task_id, *record)


def save_task(conn: sqlite3.Connection, task: ReviewTask) -> None:
    conn.execute('''
        INSERT INTO review_tasks (
            task_id, commitment, commitment_id, batch_id, unit,
            window_start, window_stop, result_number
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (task.task_id, task.commitment, task.commitment_id, task.batch_id,
          task.unit, task.start, task.stop, task.result_number))


def stored_rows(conn: sqlite3.Connection, task_id: str, start: int, stop: int
                ) -> dict[int, tuple]:
    rows = {}
    for ordinal, value_hex, page, slot, version in conn.execute('''
        SELECT ordinal, value_hex, origin_page, origin_slot, dictionary_version
        FROM review_rows
        WHERE task_id = ? AND ordinal >= ? AND ordinal < ?
    ''', (task_id, start, stop)):
        rows[ordinal] = (value_hex, page, slot, version)
    return rows


def covered_count(conn: sqlite3.Connection, task_id: str, start: int, stop: int) -> int:
    return conn.execute('''
        SELECT COUNT(*) FROM review_rows
        WHERE task_id = ? AND ordinal >= ? AND ordinal < ?
    ''', (task_id, start, stop)).fetchone()[0]



def bind_result(conn: sqlite3.Connection, task_id: str, result_number: int) -> None:
    cursor = conn.execute(
        'UPDATE review_tasks SET result_number = ? WHERE task_id = ? AND result_number IS NULL',
        (result_number, task_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f'review task {task_id!r} is already bound to a result')
