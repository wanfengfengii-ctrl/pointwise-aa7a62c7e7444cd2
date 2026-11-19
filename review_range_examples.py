import json
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from audit.reconcile import read
from catalog.dictionaries import register
from exchange.issue import issue
from lake.admission import accept
from lake.store import open_lake
from source.freeze import freeze
from source.pages import SourcePage
from source.store import open_source


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
            (row.ordinal, row.value, row.origin.page_id, row.origin.slot, row.origin.dictionary_version)
            for row in report.details
        )
        totals = report.totals
        actual = (report.release.batch_id, report.release.unit, rows,
                  (totals.non_missing_count, totals.missing_count, totals.total))
        wanted = ("review-06", "mC", expected_rows, expected_totals)
        if actual != wanted:
            raise AssertionError((actual, wanted))
    return target, other


def main():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with closing(open_source(root / "station.sqlite")) as target:
            body = issue(target, freeze_example(target)).body
        with closing(open_source(root / "alternate.sqlite")) as alternate:
            other_body = issue(alternate, freeze_example(alternate, alternative=True)).body
        with closing(open_lake(root / "lake.sqlite")) as lake:
            target, other = v1_reports(lake, body, other_body)
    print(json.dumps({"target": asdict(target), "same_batch_id": asdict(other)},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
