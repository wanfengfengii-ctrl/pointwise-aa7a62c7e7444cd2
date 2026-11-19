import sqlite3
from dataclasses import dataclass

from audit.detail import ObservationDetail, build_detail
from audit.totals import ObservationTotals, calculate_totals
from lake.layout import load_layout
from lake.releases import LakeRelease, find_release
from lake.store import lake_read_transaction


@dataclass(frozen=True)
class AuditReport:
    release: LakeRelease
    details: tuple[ObservationDetail, ...]
    totals: ObservationTotals


def read(conn: sqlite3.Connection, result_number: int) -> AuditReport:
    with lake_read_transaction(conn):
        release = find_release(conn, result_number)
        pages = load_layout(conn, release.result_number)
        details = build_detail(row for page in pages for row in page.rows)
        totals = calculate_totals(details)
    return AuditReport(release, details, totals)
