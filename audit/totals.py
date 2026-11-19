from collections.abc import Iterable
from dataclasses import dataclass

from audit.detail import ObservationDetail


@dataclass(frozen=True)
class ObservationTotals:
    non_missing_count: int
    missing_count: int
    total: int


def calculate_totals(details: Iterable[ObservationDetail]) -> ObservationTotals:
    non_missing_count = 0
    missing_count = 0
    total = 0
    for detail in details:
        if detail.value is None:
            missing_count += 1
        else:
            non_missing_count += 1
            total += detail.value
    return ObservationTotals(non_missing_count, missing_count, total)
