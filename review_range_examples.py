"""Original-order range review examples and joint verification.

This module drives the station-side commitment/slice flow and the lake-side
registration/admission flow end to end.  Running it performs the V1 checks from
the earlier milestones plus the joint review-range checks:

* a target task over window ``[1,5)`` completes only when every position is
  proven, disclosing only the window's rows;
* overlapping and out-of-order slices wait for completion and count each
  position once;
* a legal replay after completion returns the fixed result number;
* a slice issued from an independent station (same batch ID) is rejected even
  after its ``commitment_id`` is rewritten to the target;
* tampered value/original-slot/dictionary-version rows and an out-of-window
  slice are rejected and leave the gap intact;
* a late foreign-key failure at COMMIT keeps the earlier progress, and the
  same final slice completes after the fault is cleared;
* the completed result survives repack and reopen with the same report.
"""

import json
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from audit.reconcile import read
from catalog.dictionaries import register
from exchange.issue import issue, issue_slice, make_commitment
from lake.admission import accept, accept_slice, register_review
from lake.repack import repack
from lake.store import open_lake
from source.freeze import freeze
from source.pages import SourcePage
from source.store import open_source
from tests import fail_commit_after_insert

TASK_ID = "review-task"
COMMITMENT_WINDOW = (1, 5)
WINDOW_ROWS = [
    (1, 9, "Z-first", 1, "D1"),
    (2, 11, "A-later", 0, "D2"),
    (3, 13, "A-later", 1, "D2"),
    (4, None, "M-tail", 0, "D3"),
]
WINDOW_TOTALS = (3, 1, 33)


def freeze_example(conn, *, alternative=False):
    register(conn, "D1", {0: 5, 1: 9}, "mC", 255)
    register(conn, "D2", {0: 11, 1: 13}, "mC", 255)
    register(conn, "D3", {0: 0, 1: 7}, "mC", 255)
    first_tail_code = 0 if alternative else 255
    pages = (
        SourcePage("Z-first", "D1", bytes([0, 1])),
        SourcePage("A-later", "D2", bytes([0, 1])),
        SourcePage("M-tail", "D3", bytes([first_tail_code, 1])),
    )
    return freeze(conn, "review-06", "mC", pages)


def v1_reports(lake, target_body, other_body):
    target = read(lake, accept(lake, target_body).result_number)
    other = read(lake, accept(lake, other_body).result_number)
    expected = (
        (0, 5, "Z-first", 0, "D1"),
        (1, 9, "Z-first", 1, "D1"),
        (2, 11, "A-later", 0, "D2"),
        (3, 13, "A-later", 1, "D2"),
        (4, None, "M-tail", 0, "D3"),
        (5, 7, "M-tail", 1, "D3"),
    )
    alternate = expected[:4] + ((4, 0, "M-tail", 0, "D3"),) + expected[5:]
    for report, expected_rows, expected_totals in (
        (target, expected, (5, 1, 45)),
        (other, alternate, (6, 0, 45)),
    ):
        rows = tuple(
            (row.ordinal, row.value, row.origin.page_id, row.origin.slot,
             row.origin.dictionary_version)
            for row in report.details
        )
        totals = report.totals
        actual = (report.release.batch_id, report.release.unit, rows,
                  (totals.non_missing_count, totals.missing_count, totals.total))
        wanted = ("review-06", "mC", expected_rows, expected_totals)
        if actual != wanted:
            raise AssertionError((actual, wanted))
    return target, other


def _report_summary(report):
    return {
        "result_number": report.release.result_number,
        "batch_id": report.release.batch_id,
        "unit": report.release.unit,
        "receipt": report.release.receipt,
        "rows": [
            [item.ordinal, item.value, item.origin.page_id, item.origin.slot,
             item.origin.dictionary_version]
            for item in report.details
        ],
        "totals": [report.totals.non_missing_count, report.totals.missing_count,
                   report.totals.total],
    }


def _expect_error(label, call):
    try:
        call()
    except (ValueError, KeyError):
        return
    raise AssertionError(f"{label} should have been rejected")


def joint_scenarios(root):
    target_station = root / "station.sqlite"
    alternate_station = root / "alternate.sqlite"
    lake_path = root / "review-lake.sqlite"

    with closing(open_source(target_station)) as conn:
        freeze_example(conn)
        commitment = make_commitment(conn, "review-06")
        slices = {rng: issue_slice(conn, commitment, *rng) for rng in
                  ((2, 4), (1, 3), (4, 5))}
        overlapping = issue_slice(conn, commitment, 1, 5)
        outside = issue_slice(conn, commitment, 0, 2)
    with closing(open_source(alternate_station)) as conn:
        freeze_example(conn, alternative=True)
        other_commitment = make_commitment(conn, "review-06")
        foreign = issue_slice(conn, other_commitment, 4, 5)

    with closing(open_lake(lake_path)) as lake:
        register_review(lake, TASK_ID, commitment, *COMMITMENT_WINDOW)
        # Overlapping, out-of-order and replayed slices still wait.
        assert accept_slice(lake, TASK_ID, slices[(2, 4)]) is None
        assert accept_slice(lake, TASK_ID, slices[(1, 3)]) is None
        assert accept_slice(lake, TASK_ID, slices[(2, 4)]) is None
        # Foreign batch, its rewritten identity and an out-of-window slice fail.
        rewritten = json.loads(foreign)
        rewritten["commitment_id"] = json.loads(commitment)["evidence"]["root"]
        for label, body in (
            ("foreign slice", foreign),
            ("rewritten identity", json.dumps(rewritten)),
            ("out-of-window slice", outside),
        ):
            _expect_error(label, lambda body=body: accept_slice(lake, TASK_ID, body))
        # Tampered rows fail and do not fill the gap.
        valid = json.loads(slices[(4, 5)])
        for label, mutate in (
            ("tampered value", lambda doc: doc["rows"][0].update(value=7)),
            ("tampered slot", lambda doc: doc["rows"][0].update(slot=1)),
            ("tampered dictionary", lambda doc: doc["rows"][0].update(dictionary_version="D1")),
        ):
            doc = json.loads(json.dumps(valid))
            mutate(doc)
            _expect_error(label, lambda doc=doc: accept_slice(lake, TASK_ID, json.dumps(doc)))
        assert lake.execute("SELECT COUNT(*) FROM lake_results").fetchone()[0] == 0

    # Reopen the station: the same commitment is re-issued and signs the tail.
    with closing(open_source(target_station)) as conn:
        reopened = make_commitment(conn, "review-06")
        assert reopened == commitment
        final_slice = issue_slice(conn, reopened, 4, 5)

    with closing(open_lake(lake_path)) as lake:
        # A late commit failure keeps all earlier verified coverage.
        trace = fail_commit_after_insert(lake, "lake_receipts")
        try:
            accept_slice(lake, TASK_ID, final_slice)
        except Exception as exc:
            assert "FOREIGN KEY" in str(exc)
        else:
            raise AssertionError("late commit failure did not abort")
        assert lake.execute("SELECT COUNT(*) FROM lake_results").fetchone()[0] == 0
        lake.execute("DROP TRIGGER check_at_commit")
        lake.execute("DROP TABLE commit_checks")
        lake.execute("DROP TABLE commit_gate")
        assert accept_slice(lake, TASK_ID, slices[(2, 4)]) is None
        number = accept_slice(lake, TASK_ID, final_slice)

    with closing(open_lake(lake_path)) as lake:
        report = read(lake, number)
        summary = _report_summary(report)
        wanted = ([list(row) for row in WINDOW_ROWS], list(WINDOW_TOTALS))
        actual = ([[r[0], r[1], r[2], r[3], r[4]] for r in summary["rows"]],
                  summary["totals"])
        if actual != wanted:
            raise AssertionError((actual, wanted))
        assert accept_slice(lake, TASK_ID, overlapping) == number
        assert repack(lake, number, 2) == 2
        repacked = read(lake, number)
        if _report_summary(repacked) != summary:
            raise AssertionError("repack changed the report")

    with closing(open_lake(lake_path)) as lake:
        if _report_summary(read(lake, number)) != summary:
            raise AssertionError("reopen changed the report")
    return summary, trace


def run_checks():
    """Run the V1 examples and joint review-range checks; return a summary.

    Raises AssertionError (or an underlying exception) on any failed check.
    Does not print, so a verifier can call it without noise.
    """
    with TemporaryDirectory() as directory:
        root = Path(directory) / "v1"
        root.mkdir()
        with closing(open_source(root / "station.sqlite")) as target:
            body = issue(target, freeze_example(target)).body
        with closing(open_source(root / "alternate.sqlite")) as alternate:
            other_body = issue(alternate, freeze_example(alternate, alternative=True)).body
        with closing(open_lake(root / "lake.sqlite")) as lake:
            v1_target, v1_other = v1_reports(lake, body, other_body)

    with TemporaryDirectory() as directory:
        summary, _trace = joint_scenarios(Path(directory))
    return {
        "target": asdict(v1_target),
        "same_batch_id": asdict(v1_other),
        "review_window": summary,
    }


def main():
    print(json.dumps(run_checks(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
