def validate_label(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} must be a nonblank string')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ValueError(f'{field} must be valid UTF-8 text') from exc
    return value


def validate_unit(unit: str) -> str:
    return validate_label(unit, 'unit')


def require_batch_unit(batch_unit: str, dictionary_unit: str) -> None:
    if batch_unit != dictionary_unit:
        raise ValueError(f'batch unit {batch_unit!r} differs from dictionary unit {dictionary_unit!r}')
