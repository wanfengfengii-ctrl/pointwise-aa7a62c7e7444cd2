from dataclasses import dataclass

from lake.materialize import ObservationRow


@dataclass(frozen=True)
class Origin:
    page_id: str
    slot: int
    dictionary_version: str


def read_origin(row: ObservationRow) -> Origin:
    return Origin(row.page_id, row.slot, row.dictionary_version)
