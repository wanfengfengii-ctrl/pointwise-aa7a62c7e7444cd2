from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping

from catalog.codebook import Codebook, build_codebook, decode_entries, encode_entries
from catalog.units import validate_label
from source.store import source_transaction


def initialize_dictionaries(conn: sqlite3.Connection) -> None:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS dictionaries (
            version TEXT PRIMARY KEY NOT NULL,
            unit TEXT NOT NULL,
            entries TEXT NOT NULL,
            missing_code INTEGER
        )
    ''')


def get_dictionary(conn: sqlite3.Connection, version: str) -> Codebook:
    validate_label(version, 'dictionary version')
    definition = conn.execute(
        'SELECT unit, entries, missing_code FROM dictionaries WHERE version = ?',
        (version,),
    ).fetchone()
    if definition is None:
        raise KeyError(f'unknown dictionary {version!r}')
    unit, entries, missing_code = definition
    return build_codebook(version, decode_entries(json.loads(entries)), unit, missing_code)


def register(
    conn: sqlite3.Connection,
    version: str,
    mapping: Mapping[int, int] | Iterable[tuple[int, int]],
    unit: str,
    missing_code: int | None = None,
) -> Codebook:
    proposed = build_codebook(version, mapping, unit, missing_code)
    with source_transaction(conn):
        try:
            recorded = get_dictionary(conn, version)
        except KeyError:
            conn.execute(
                'INSERT INTO dictionaries (version, unit, entries, missing_code) VALUES (?, ?, ?, ?)',
                (version, unit, json.dumps(encode_entries(proposed)), missing_code),
            )
        else:
            if recorded != proposed:
                raise ValueError(f'conflicting definition for dictionary {version!r}')
            return recorded
    return proposed
