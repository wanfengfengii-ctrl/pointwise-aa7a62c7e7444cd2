from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from catalog.codebook import Codebook, decode_code
from exchange.envelope import V1Envelope


@dataclass(frozen=True)
class ObservationRow:
    ordinal: int
    value: int | None
    page_id: str
    slot: int
    dictionary_version: str


def materialize_rows(
    envelope: V1Envelope, dictionaries: Mapping[str, Codebook],
) -> tuple[ObservationRow, ...]:
    """Turn pages with adjudicated evidence into fixed rows in original batch order."""
    rows = []
    for page in envelope.pages:
        book = dictionaries[page.dictionary_version]
        for slot, code in enumerate(page.codes):
            try:
                value = decode_code(book, code)
            except ValueError as exc:
                raise ValueError(f'page {page.page_id!r} slot {slot}: {exc}') from exc
            rows.append(ObservationRow(len(rows), value, page.page_id, slot, page.dictionary_version))
    return tuple(rows)
