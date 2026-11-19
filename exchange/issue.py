import sqlite3

from catalog.dictionaries import get_dictionary
from exchange.envelope import V1Envelope, V1Page, encode_v1
from exchange.evidence import freeze_dictionary_evidence
from exchange.issuance_store import IssuedPackage, save_issuance
from exchange.review_commitments import issue_slice, make_commitment
from source.freeze import get_frozen_batch
from source.store import source_transaction

__all__ = ['issue', 'make_commitment', 'issue_slice']


def issue(conn: sqlite3.Connection, batch_id: str) -> IssuedPackage:
    with source_transaction(conn):
        batch = get_frozen_batch(conn, batch_id)
        pages = tuple(V1Page(page.page_id, page.dictionary_version, page.codes)
                      for page in batch.pages)
        versions = dict.fromkeys(page.dictionary_version for page in pages)
        evidence = tuple(freeze_dictionary_evidence(get_dictionary(conn, version))
                         for version in versions)
        body = encode_v1(V1Envelope(batch.batch_id, batch.unit, pages, evidence))
        issued = save_issuance(conn, batch.batch_id, body)
    return issued
