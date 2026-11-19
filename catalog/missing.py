from __future__ import annotations

from collections.abc import Collection


def validate_missing_code(code: int | None, ordinary_codes: Collection[int]) -> None:
    if code is None:
        return
    if type(code) is not int or not 0 <= code <= 255:
        raise ValueError('missing code must be a byte or None')
    if code in ordinary_codes:
        raise ValueError(f'missing code {code} also has an ordinary definition')


def is_missing_code(code: int, missing_code: int | None) -> bool:
    return missing_code is not None and code == missing_code
