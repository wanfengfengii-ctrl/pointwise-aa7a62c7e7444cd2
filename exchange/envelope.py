from __future__ import annotations

import json
from dataclasses import dataclass

from catalog.codebook import build_codebook, decode_entries, encode_entries
from catalog.units import validate_label, validate_unit
from exchange.evidence import DictionaryEvidence


@dataclass(frozen=True)
class V1Page:
    page_id: str
    dictionary_version: str
    codes: bytes

    def __post_init__(self) -> None:
        validate_label(self.page_id, 'page ID')
        validate_label(self.dictionary_version, 'dictionary version')
        if not isinstance(self.codes, bytes):
            raise ValueError('V1 page codes must be bytes')


@dataclass(frozen=True)
class V1Envelope:
    batch_id: str
    unit: str
    pages: tuple[V1Page, ...]
    dictionaries: tuple[DictionaryEvidence, ...]

    def __post_init__(self) -> None:
        validate_label(self.batch_id, 'batch ID')
        validate_unit(self.unit)
        pages = tuple(self.pages)
        dictionaries = tuple(self.dictionaries)
        if any(not isinstance(page, V1Page) for page in pages):
            raise ValueError('V1 envelope expects V1Page objects')
        if len({page.page_id for page in pages}) != len(pages):
            raise ValueError('duplicate page ID in V1 envelope')
        if any(not isinstance(item, DictionaryEvidence) for item in dictionaries):
            raise ValueError('V1 envelope expects dictionary declarations')
        object.__setattr__(self, 'pages', pages)
        object.__setattr__(self, 'dictionaries', dictionaries)


def encode_v1(envelope: V1Envelope) -> bytes:
    definitions = []
    for evidence in envelope.dictionaries:
        book = evidence.definition
        definitions.append({
            'version': book.version,
            'unit': book.unit,
            'entries': encode_entries(book),
            'missing_code': book.missing_code,
        })
    document = {
        'version': 1,
        'batch_id': envelope.batch_id,
        'unit': envelope.unit,
        'pages': [
            {'page_id': page.page_id, 'dictionary_version': page.dictionary_version,
             'codes': list(page.codes)}
            for page in envelope.pages
        ],
        'dictionaries': definitions,
    }
    return json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _unique_object(pairs):
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError(f'duplicate JSON key {key!r}')
        fields[key] = value
    return fields


def _require_fields(obj, fields, name):
    if not isinstance(obj, dict) or obj.keys() != fields:
        raise ValueError(f'{name} must contain exactly {", ".join(sorted(fields))}')
    return obj


def _require_array(obj, name):
    if not isinstance(obj, list):
        raise ValueError(f'{name} must be an array')
    return obj


def _decode_page(obj) -> V1Page:
    page = _require_fields(obj, {'page_id', 'dictionary_version', 'codes'}, 'page')
    page_id = validate_label(page['page_id'], 'page ID')
    codes = _require_array(page['codes'], 'codes')
    for slot, code in enumerate(codes):
        if type(code) is not int or not 0 <= code <= 255:
            raise ValueError(f'page {page_id!r} slot {slot}: code must be a byte')
    return V1Page(page_id, page['dictionary_version'], bytes(codes))


def _decode_evidence(obj) -> DictionaryEvidence:
    declaration = _require_fields(obj, {'version', 'unit', 'entries', 'missing_code'}, 'dictionary')
    return DictionaryEvidence(build_codebook(
        declaration['version'], decode_entries(declaration['entries']),
        declaration['unit'], declaration['missing_code'],
    ))


def decode_v1(body: bytes | str) -> V1Envelope:
    if not isinstance(body, (bytes, str)):
        raise ValueError('V1 body must be UTF-8 bytes or JSON text')
    try:
        text = body.decode('utf-8') if isinstance(body, bytes) else body
        obj = json.loads(text, object_pairs_hook=_unique_object)
    except ValueError as exc:
        raise ValueError(f'invalid V1 JSON: {exc}') from exc
    document = _require_fields(obj, {'version', 'batch_id', 'unit', 'pages', 'dictionaries'}, 'V1 envelope')
    if type(document['version']) is not int or document['version'] != 1:
        raise ValueError('unsupported exchange version')
    pages = tuple(_decode_page(page) for page in _require_array(document['pages'], 'pages'))
    declarations = _require_array(document['dictionaries'], 'dictionaries')
    evidence = tuple(_decode_evidence(item) for item in declarations)
    return V1Envelope(document['batch_id'], document['unit'], pages, evidence)
