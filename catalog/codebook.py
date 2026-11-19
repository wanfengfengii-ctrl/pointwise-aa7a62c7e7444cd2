from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from catalog.missing import is_missing_code, validate_missing_code
from catalog.units import validate_label, validate_unit


@dataclass(frozen=True)
class Codebook:
    version: str
    unit: str
    entries: tuple[tuple[int, int], ...]
    missing_code: int | None = None
    _lookup: Mapping[int, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        validate_label(self.version, 'dictionary version')
        validate_unit(self.unit)
        ordinary = {}
        for entry in self.entries:
            if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                raise ValueError('dictionary entries must be code and reading pairs')
            code, value = entry
            if type(code) is not int or not 0 <= code <= 255:
                raise ValueError(f'invalid dictionary code {code!r}')
            if type(value) is not int:
                raise ValueError(f'reading for code {code} must be an integer')
            if code in ordinary:
                raise ValueError(f'duplicate dictionary code {code}')
            ordinary[code] = value
        validate_missing_code(self.missing_code, ordinary)
        object.__setattr__(self, 'entries', tuple(sorted(ordinary.items())))
        object.__setattr__(self, '_lookup', MappingProxyType(ordinary))


def build_codebook(
    version: str,
    mapping: Mapping[int, int] | Iterable[tuple[int, int]],
    unit: str,
    missing_code: int | None = None,
) -> Codebook:
    entries = mapping.items() if isinstance(mapping, Mapping) else mapping
    return Codebook(version, unit, tuple(entries), missing_code)


def decode_code(book: Codebook, code: int) -> int | None:
    if type(code) is not int or not 0 <= code <= 255:
        raise ValueError(f'code {code!r} is not a byte')
    if is_missing_code(code, book.missing_code):
        return None
    try:
        return book._lookup[code]
    except KeyError as exc:
        raise ValueError(f'undefined code {code} in dictionary {book.version!r}') from exc


def encode_entries(book: Codebook) -> list[list[int | str]]:
    # Use the hexadecimal spelling for large values to avoid decimal conversion limits.
    return [[code, value if value.bit_length() <= 1024 else hex(value)]
            for code, value in book.entries]


def decode_entries(entries: object) -> list[tuple[int, int]]:
    if not isinstance(entries, list):
        raise ValueError('entries must be an array')
    decoded = []
    for entry in entries:
        if not isinstance(entry, list) or len(entry) != 2:
            raise ValueError('entries must contain two-element arrays')
        code, value = entry
        if isinstance(value, str):
            try:
                reading = int(value, 16)
            except ValueError as exc:
                raise ValueError('reading must be an integer or canonical hexadecimal') from exc
            if hex(reading) != value:
                raise ValueError('reading must be an integer or canonical hexadecimal')
            value = reading
        if type(code) is not int or type(value) is not int:
            raise ValueError('entry code and reading must be integers')
        decoded.append((code, value))
    return decoded
