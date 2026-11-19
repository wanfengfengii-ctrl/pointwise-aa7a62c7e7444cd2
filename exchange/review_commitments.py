"""Station-side frozen review commitments and slice issuance (V2).

A commitment fixes the identity of one frozen batch: its batch ID, unit, row
count and every original-position row (ordinal, exact value, original page,
original slot and the dictionary version actually written for that page).

Slices disclose only a contiguous sub-range of those rows.  Each slice carries
a proof so the lake can verify the disclosed run against the registered
commitment without ever receiving readings outside the slice.  The proof is a
Merkle path over the committed rows: a slice needs only the sibling hashes on
the paths from its own leaf range up to the root.  Sibling hashes disclose
nothing readable.

Every leaf and internal node is salted before hashing.  Each leaf additionally
commits to a per-row secret salt that is kept at the station: the slice carries
the salt only for rows it already discloses in the clear, so the sibling hashes
in a proof cannot be brute-forced over a low-entropy reading domain.  Different
original positions still hash differently because each leaf commits to the
complete row, including its ordinal, page and slot.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from catalog.codebook import decode_code
from catalog.dictionaries import get_dictionary
from catalog.units import validate_label
from source.freeze import get_frozen_batch
from source.store import source_transaction

COMMITMENT_VERSION = 2
SLICE_VERSION = 2
COMMITMENT_KIND = 'review-commitment'
SLICE_KIND = 'review-slice'

LEAF_TAG = b'review-leaf\x00'
NODE_TAG = b'review-node\x00'
EMPTY_ROOT = hashlib.sha256(NODE_TAG + b'empty').digest()


class ReviewError(ValueError):
    """A commitment or slice violates the review-range contract."""


# ---------------------------------------------------------------------------
# Small encoding helpers


def dump_review(document: dict[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _is_hex64(text: object) -> bool:
    if not isinstance(text, str) or len(text) != 64:
        return False
    try:
        int(text, 16)
    except ValueError:
        return False
    return text == text.lower()


def _encode_value(value: int | None) -> int | str | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ReviewError('committed value must be an integer or missing')
    # Mirror the V1 decimal-versus-hex spelling limit exactly.
    return value if value.bit_length() <= 1024 else hex(value)


def _decode_value(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            reading = int(value, 16)
        except ValueError as exc:
            raise ReviewError('value must be an integer, canonical hexadecimal or null') from exc
        if type(value) is not str or hex(reading) != value:
            raise ReviewError('value must be an integer, canonical hexadecimal or null')
        return reading
    if type(value) is not int:
        raise ReviewError('value must be an integer, canonical hexadecimal or null')
    return value


def _decode_nonblank(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewError(f'{field} must be a nonblank string')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ReviewError(f'{field} must be valid UTF-8 text') from exc
    return value


def _decode_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise ReviewError(f'{field} must be an integer')
    return value


def _decode_nonnegative(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ReviewError(f'{field} must be a non-negative integer')
    return value


def _decode_utf8(raw: bytes, name: str) -> str:
    if not isinstance(raw, (bytes, str)):
        raise ReviewError(f'{name} must be UTF-8 bytes or JSON text')
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ReviewError(f'{name} must be UTF-8 text') from exc


def _load_object(text: str, name: str) -> dict[str, Any]:
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ReviewError(f'invalid {name} JSON: {exc}') from exc
    if not isinstance(document, dict):
        raise ReviewError(f'{name} must be a JSON object')
    return document


def _require_fields(document: dict[str, Any], fields: set[str], name: str) -> None:
    if set(document) != fields:
        raise ReviewError(f'{name} must contain exactly {", ".join(sorted(fields))}')


# ---------------------------------------------------------------------------
# Committed rows and their hashes


@dataclass(frozen=True)
class CommitmentRow:
    ordinal: int
    value: int | None
    page_id: str
    slot: int
    dictionary_version: str
    salt: bytes

    def fields(self) -> dict[str, Any]:
        return {
            'ordinal': self.ordinal,
            'value': _encode_value(self.value),
            'page_id': self.page_id,
            'slot': self.slot,
            'dictionary_version': self.dictionary_version,
        }

    def disclosed(self) -> dict[str, Any]:
        return {**self.fields(), 'salt': self.salt.hex()}

    def canonical(self) -> bytes:
        return dump_review(self.fields())

    def leaf(self) -> bytes:
        return _sha256(LEAF_TAG + self.salt + self.canonical())

    def key(self) -> tuple[int, int | None, str, int, str]:
        return (self.ordinal, self.value, self.page_id, self.slot, self.dictionary_version)


def _node(left: bytes, right: bytes) -> bytes:
    return _sha256(NODE_TAG + left + right)


def _levels(leaves: tuple[bytes, ...]) -> tuple[tuple[bytes, ...], ...]:
    if not leaves:
        return ((),)
    result = [leaves]
    while len(result[-1]) > 1:
        current = result[-1]
        parents = [_node(current[i], current[i + 1])
                   for i in range(0, len(current) - 1, 2)]
        if len(current) % 2 == 1:
            parents.append(current[-1])
        result.append(tuple(parents))
    return tuple(result)


def merkle_root(rows: tuple[CommitmentRow, ...]) -> bytes:
    levels = _levels(tuple(row.leaf() for row in rows))
    return levels[-1][0] if levels[-1] else EMPTY_ROOT


def range_proof(rows: tuple[CommitmentRow, ...], start: int, stop: int
                ) -> list[tuple[str, str]]:
    """Sibling disclosures on the path from rows [start,stop) to the root."""
    levels = _levels(tuple(row.leaf() for row in rows))
    proof: list[tuple[str, str]] = []
    lo, hi = start, stop
    for level in levels[:-1]:
        size = len(level)
        if lo % 2 == 1:
            proof.append(('left', level[lo - 1].hex()))
            lo -= 1
        if hi % 2 == 1 and hi < size:
            proof.append(('right', level[hi].hex()))
            hi += 1
        lo //= 2
        hi = (hi + 1) // 2
    return proof


def verify_range(leaves: tuple[bytes, ...], start: int, stop: int,
                 proof: tuple[tuple[str, str], ...], total: int) -> bytes:
    """Rebuild the root from the disclosed leaves and their sibling proof."""
    if type(start) is not int or type(stop) is not int or not (0 <= start < stop <= total):
        raise ReviewError('slice range does not fit its commitment')
    lo, hi = start, stop
    nodes = list(leaves)
    remaining = list(proof)
    level_size = total
    while level_size > 1:
        if lo % 2 == 1:
            side, text = remaining.pop(0) if remaining else (None, None)
            if side != 'left' or not _is_hex64(text):
                raise ReviewError('slice proof is missing a left sibling')
            nodes.insert(0, bytes.fromhex(text))
            lo -= 1
        if hi % 2 == 1 and hi < level_size:
            side, text = remaining.pop(0) if remaining else (None, None)
            if side != 'right' or not _is_hex64(text):
                raise ReviewError('slice proof is missing a right sibling')
            nodes.append(bytes.fromhex(text))
            hi += 1
        if len(nodes) != hi - lo:
            raise ReviewError('slice proof does not cover its range')
        parents = [_node(nodes[i], nodes[i + 1]) for i in range(0, len(nodes) - 1, 2)]
        if len(nodes) % 2 == 1:
            parents.append(nodes[-1])
        nodes = parents
        lo //= 2
        hi = (hi + 1) // 2
        level_size = (level_size + 1) // 2
    if remaining:
        raise ReviewError('slice proof has unused material')
    if len(nodes) != 1:
        raise ReviewError('slice proof does not reach the commitment root')
    return nodes[0]


# ---------------------------------------------------------------------------
# Commitments


@dataclass(frozen=True)
class Commitment:
    batch_id: str
    unit: str
    row_count: int
    root: str
    raw: bytes
    commitment_id: str


def _derive_salt(seed: bytes, ordinal: int) -> bytes:
    """Deterministic per-row salt, recomputable only from hidden batch content."""
    return hmac.new(seed, b'review-salt\x00' + ordinal.to_bytes(8, 'big'),
                    hashlib.sha256).digest()


def _build_rows(conn: sqlite3.Connection, batch_id: str) -> tuple[CommitmentRow, ...]:
    batch = get_frozen_batch(conn, batch_id)
    rows: list[CommitmentRow] = []
    for page in batch.pages:
        book = get_dictionary(conn, page.dictionary_version)
        for slot, code in enumerate(page.codes):
            try:
                value = decode_code(book, code)
            except ValueError as exc:
                raise ReviewError(
                    f'page {page.page_id!r} slot {slot}: {exc}'
                ) from exc
            rows.append(CommitmentRow(len(rows), value, page.page_id, slot,
                                      page.dictionary_version, b''))
    return tuple(rows)


def _salt_seed(batch_id: str, unit: str, rows: tuple[CommitmentRow, ...]) -> bytes:
    """Seed for salting leaves, bound to the whole hidden batch content.

    An out-of-slice leaf hash therefore depends on undisclosed readings, so it
    cannot be used as an unblinded fingerprint to enumerate a small value set.
    """
    digest = hashlib.sha256(b'review-seed\x00')
    digest.update(dump_review({'batch_id': batch_id, 'unit': unit,
                               'rows': [row.fields() for row in rows]}))
    return digest.digest()


def _salted(batch_id: str, unit: str, rows: tuple[CommitmentRow, ...]
            ) -> tuple[CommitmentRow, ...]:
    seed = _salt_seed(batch_id, unit, rows)
    return tuple(
        CommitmentRow(row.ordinal, row.value, row.page_id, row.slot,
                      row.dictionary_version, _derive_salt(seed, row.ordinal))
        for row in rows
    )


def _render_commitment(batch_id: str, unit: str, rows: tuple[CommitmentRow, ...]) -> bytes:
    return dump_review({
        'version': COMMITMENT_VERSION,
        'kind': COMMITMENT_KIND,
        'batch_id': batch_id,
        'unit': unit,
        'row_count': len(rows),
        'evidence': {
            'algorithm': 'sha256-merkle',
            'domain': {'leaf': LEAF_TAG.hex(), 'node': NODE_TAG.hex()},
            'root': merkle_root(rows).hex(),
        },
    })


def initialize_review_commitments(conn: sqlite3.Connection) -> None:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS review_commitments (
            batch_id TEXT PRIMARY KEY NOT NULL REFERENCES frozen_batches(batch_id),
            body BLOB NOT NULL
        )
    ''')


def make_commitment(conn: sqlite3.Connection, batch_id: str) -> bytes:
    validate_label(batch_id, 'batch ID')
    with source_transaction(conn):
        record = conn.execute(
            'SELECT body FROM review_commitments WHERE batch_id = ?', (batch_id,),
        ).fetchone()
        if record is not None:
            return record[0]
        # Raises KeyError for an unknown frozen batch.
        batch = get_frozen_batch(conn, batch_id)
        rows = _salted(batch.batch_id, batch.unit, _build_rows(conn, batch.batch_id))
        body = _render_commitment(batch.batch_id, batch.unit, rows)
        conn.execute(
            'INSERT INTO review_commitments (batch_id, body) VALUES (?, ?)',
            (batch_id, body),
        )
    return body


def parse_commitment(body: bytes | str) -> Commitment:
    raw = body.encode('utf-8') if isinstance(body, str) else bytes(body)
    text = _decode_utf8(raw, 'commitment')
    document = _load_object(text, 'commitment')
    _require_fields(document,
                    {'version', 'kind', 'batch_id', 'unit', 'row_count', 'evidence'},
                    'commitment')
    if type(document['version']) is not int or document['version'] != COMMITMENT_VERSION:
        raise ReviewError('unsupported commitment version')
    if document['kind'] != COMMITMENT_KIND:
        raise ReviewError('not a review commitment')
    batch_id = _decode_nonblank(document['batch_id'], 'batch ID')
    unit = _decode_nonblank(document['unit'], 'unit')
    row_count = _decode_nonnegative(document['row_count'], 'row_count')
    evidence = document['evidence']
    if not isinstance(evidence, dict):
        raise ReviewError('commitment evidence must be an object')
    if evidence.get('algorithm') != 'sha256-merkle':
        raise ReviewError('unsupported commitment evidence algorithm')
    domain = evidence.get('domain')
    if not isinstance(domain, dict) or domain.get('leaf') != LEAF_TAG.hex() \
            or domain.get('node') != NODE_TAG.hex():
        raise ReviewError('commitment evidence domain is invalid')
    root = evidence.get('root')
    if not _is_hex64(root):
        raise ReviewError('commitment root must be a SHA-256 hex digest')
    return Commitment(batch_id, unit, row_count, root, raw, hashlib.sha256(raw).hexdigest())


def _station_rows(conn: sqlite3.Connection, commitment: Commitment
                  ) -> tuple[CommitmentRow, ...]:
    record = conn.execute(
        'SELECT batch_id FROM review_commitments WHERE body = ?', (commitment.raw,),
    ).fetchone()
    if record is None:
        raise ReviewError('commitment was not generated by this station')
    batch_id = record[0]
    if batch_id != commitment.batch_id:
        raise ReviewError('commitment batch identity mismatch')
    batch = get_frozen_batch(conn, batch_id)
    rows = _salted(batch.batch_id, batch.unit, _build_rows(conn, batch_id))
    if len(rows) != commitment.row_count or merkle_root(rows).hex() != commitment.root:
        raise ReviewError('commitment no longer matches the frozen batch')
    return rows


# ---------------------------------------------------------------------------
# Slices


def _validate_bound(start: object, stop: object, row_count: int) -> tuple[int, int]:
    if type(start) is not int or type(stop) is not int:
        raise ReviewError('slice endpoints must be integers')
    if start < 0 or start >= stop or stop > row_count:
        raise ReviewError(
            f'slice range [{start},{stop}) must satisfy 0 <= start < stop <= {row_count}'
        )
    return start, stop


def issue_slice(conn: sqlite3.Connection, commitment: bytes, start: object, stop: object) -> bytes:
    parsed = parse_commitment(commitment)
    with source_transaction(conn):
        rows = _station_rows(conn, parsed)
        start, stop = _validate_bound(start, stop, len(rows))
        proof = range_proof(rows, start, stop)
        document = {
            'version': SLICE_VERSION,
            'kind': SLICE_KIND,
            'commitment_id': parsed.commitment_id,
            'start': start,
            'stop': stop,
            'rows': [row.disclosed() for row in rows[start:stop]],
            'proof': [{'side': side, 'hash': value} for side, value in proof],
        }
    return dump_review(document)


@dataclass(frozen=True)
class Slice:
    commitment_id: str
    start: int
    stop: int
    rows: tuple[CommitmentRow, ...]
    proof: tuple[tuple[str, str], ...]


def _decode_slice_row(obj: object, index: int) -> CommitmentRow:
    if not isinstance(obj, dict):
        raise ReviewError('slice row must be an object')
    _require_fields(obj, {'ordinal', 'value', 'page_id', 'slot',
                          'dictionary_version', 'salt'}, 'slice row')
    ordinal = _decode_nonnegative(obj['ordinal'], 'row ordinal')
    if ordinal != index:
        raise ReviewError('slice rows must run in ascending original order without gaps')
    value = _decode_value(obj['value'])
    page_id = _decode_nonblank(obj['page_id'], 'page ID')
    slot = _decode_nonnegative(obj['slot'], 'slot')
    version = _decode_nonblank(obj['dictionary_version'], 'dictionary version')
    salt = obj['salt']
    if not isinstance(salt, str):
        raise ReviewError('slice row salt must be hexadecimal text')
    try:
        salt_bytes = bytes.fromhex(salt)
    except ValueError as exc:
        raise ReviewError('slice row salt must be hexadecimal text') from exc
    if len(salt_bytes) != 32:
        raise ReviewError('slice row salt must be 32 bytes')
    return CommitmentRow(ordinal, value, page_id, slot, version, salt_bytes)


def parse_slice(body: bytes | str) -> Slice:
    raw = body.encode('utf-8') if isinstance(body, str) else bytes(body)
    text = _decode_utf8(raw, 'slice')
    document = _load_object(text, 'slice')
    _require_fields(document, {'version', 'kind', 'commitment_id', 'start', 'stop',
                               'rows', 'proof'}, 'slice')
    if type(document['version']) is not int or document['version'] != SLICE_VERSION:
        raise ReviewError('unsupported slice version')
    if document['kind'] != SLICE_KIND:
        raise ReviewError('not a review slice')
    commitment_id = document['commitment_id']
    if not _is_hex64(commitment_id):
        raise ReviewError('commitment_id must be a SHA-256 hex digest')
    start = _decode_int(document['start'], 'slice start')
    stop = _decode_int(document['stop'], 'slice stop')
    if not 0 <= start < stop:
        raise ReviewError('slice endpoints must satisfy 0 <= start < stop')
    rows_doc = document['rows']
    if not isinstance(rows_doc, list):
        raise ReviewError('slice rows must be an array')
    if len(rows_doc) != stop - start:
        raise ReviewError('slice rows must exactly cover its range')
    rows = tuple(_decode_slice_row(obj, start + offset)
                 for offset, obj in enumerate(rows_doc))
    proof_doc = document['proof']
    if not isinstance(proof_doc, list):
        raise ReviewError('slice proof must be an array')
    proof = []
    for item in proof_doc:
        if not isinstance(item, dict):
            raise ReviewError('slice proof entries must be objects')
        _require_fields(item, {'side', 'hash'}, 'slice proof entry')
        side, digest = item['side'], item['hash']
        if side not in ('left', 'right') or not _is_hex64(digest):
            raise ReviewError('slice proof entry is invalid')
        proof.append((side, digest))
    return Slice(commitment_id, start, stop, rows, tuple(proof))


def verify_slice(slice_doc: Slice, commitment: Commitment) -> None:
    """Verify a slice against its registered commitment, in full."""
    if slice_doc.commitment_id != commitment.commitment_id:
        raise ReviewError('slice commitment identity does not match the registered commitment')
    if slice_doc.start < 0 or slice_doc.stop > commitment.row_count:
        raise ReviewError('slice range lies outside the committed batch')
    leaves = tuple(row.leaf() for row in slice_doc.rows)
    root = verify_range(leaves, slice_doc.start, slice_doc.stop,
                        slice_doc.proof, commitment.row_count)
    if root.hex() != commitment.root:
        raise ReviewError('slice proof does not verify against the commitment root')
