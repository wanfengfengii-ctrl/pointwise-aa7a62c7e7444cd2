"""Lake-side admission.

`accept` keeps the original V1 whole-package behaviour.  The review-range
functions below add original-order window tasks that admit overlapping slices.

A task binds one trusted commitment to a non-empty original-order window.  A
slice is accepted only if the whole slice fits inside that window and every
disclosed row verifies against the commitment.  Verified rows are staged by
original ordinal; a position counts once.  When every position of the window
has been verified, one fixed result is issued and bound to the task in the same
transaction.  Re-submitting a slice after completion re-verifies it and returns
the existing result number.
"""

from __future__ import annotations

import sqlite3

from catalog.units import require_batch_unit, validate_label
from exchange.envelope import decode_v1
from exchange.evidence import index_dictionary_evidence
from exchange.review_commitments import (
    parse_commitment, parse_slice, verify_slice,
)
from lake.layout import cut_pages, initial_pages, replace_layout
from lake.materialize import ObservationRow, materialize_rows
from lake.releases import LakeRelease, record_release
from lake.review_store import (
    ReviewTask, bind_result, covered_count, load_task, save_task, stored_rows,
)
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


# ---------------------------------------------------------------------------
# Review-range tasks


def _validate_window(start: object, stop: object) -> tuple[int, int]:
    if type(start) is not int or type(stop) is not int:
        raise ValueError('review window endpoints must be integers')
    if start < 0 or start >= stop:
        raise ValueError('review window must satisfy 0 <= start < stop')
    return start, stop


def register_review(
    conn: sqlite3.Connection, task_id: str, commitment: bytes, start: object, stop: object,
) -> None:
    validate_label(task_id, 'task ID')
    parsed = parse_commitment(commitment)
    start, stop = _validate_window(start, stop)
    if stop > parsed.row_count:
        raise ValueError(
            f'review window [{start},{stop}) lies outside the committed batch '
            f'of {parsed.row_count} rows'
        )
    with lake_write_transaction(conn):
        record = conn.execute(
            'SELECT commitment, window_start, window_stop FROM review_tasks WHERE task_id = ?',
            (task_id,),
        ).fetchone()
        if record is not None:
            if record[0] != parsed.raw or record[1] != start or record[2] != stop:
                raise ValueError(f'review task {task_id!r} is already bound differently')
            return
        save_task(conn, ReviewTask(task_id, parsed.raw, parsed.commitment_id,
                                   parsed.batch_id, parsed.unit, start, stop, None))


def _row_record(row) -> tuple[str | None, str, int, str]:
    value_hex = None if row.value is None else hex(row.value)
    return (value_hex, row.page_id, row.slot, row.dictionary_version)


def accept_slice(conn: sqlite3.Connection, task_id: str, body: bytes) -> int | None:
    validate_label(task_id, 'task ID')
    with lake_write_transaction(conn):
        # An unknown task is reported first, so its KeyError is not masked.
        task = load_task(conn, task_id)
        parsed = parse_slice(body)
        commitment = parse_commitment(task.commitment)
        # Verify identity, range and the whole disclosed run before staging.
        verify_slice(parsed, commitment)
        if parsed.start < task.start or parsed.stop > task.stop:
            raise ValueError(
                f'slice [{parsed.start},{parsed.stop}) lies outside the registered '
                f'window [{task.start},{task.stop})'
            )
        existing = stored_rows(conn, task_id, task.start, task.stop)
        staged: list[tuple[int, tuple[str | None, str, int, str]]] = []
        seen: dict[int, tuple[str | None, str, int, str]] = {}
        for row in parsed.rows:
            record = _row_record(row)
            previous = existing.get(row.ordinal, seen.get(row.ordinal))
            if previous is not None and previous != record:
                raise ValueError(
                    f'ordinal {row.ordinal} conflicts with a previously verified row'
                )
            seen[row.ordinal] = record
            staged.append((row.ordinal, record))
        if task.result_number is not None:
            # Completed task: a legally repeated slice returns the fixed result.
            return task.result_number
        conn.executemany('''
            INSERT INTO review_rows (
                task_id, ordinal, value_hex, origin_page, origin_slot, dictionary_version
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, ordinal) DO NOTHING
        ''', ((task_id, ordinal, *record) for ordinal, record in staged))
        if covered_count(conn, task_id, task.start, task.stop) != task.stop - task.start:
            return None
        rows = stored_rows(conn, task_id, task.start, task.stop)
        window_rows = tuple(
            ObservationRow(
                ordinal,
                None if rows[ordinal][0] is None else int(rows[ordinal][0], 16),
                rows[ordinal][1], rows[ordinal][2], rows[ordinal][3],
            )
            for ordinal in range(task.start, task.stop)
        )
        release = record_release(conn, task.batch_id, task.unit)
        replace_layout(conn, release.result_number,
                       cut_pages(window_rows, len(window_rows)))
        bind_result(conn, task_id, release.result_number)
    return release.result_number
