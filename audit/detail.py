from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from audit.provenance import Origin, read_origin
from lake.materialize import ObservationRow


@dataclass(frozen=True)
class ObservationDetail:
    ordinal: int
    value: int | None
    origin: Origin


def build_detail(rows: Iterable[ObservationRow]) -> tuple[ObservationDetail, ...]:
    ordered = sorted(rows, key=lambda row: row.ordinal)
    return tuple(ObservationDetail(row.ordinal, row.value, read_origin(row)) for row in ordered)
