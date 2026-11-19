import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from audit.reconcile import read
from catalog.dictionaries import register
from demo import SAMPLES, prepare_station
from exchange.envelope import decode_v1
from exchange.exports import export_by_issue_number
from exchange.issue import issue
from lake.admission import accept
from lake.store import open_lake
from source.store import open_source
from tests import (
    MIXED_ROWS, MIXED_TOTALS, ZERO_ROWS, ZERO_TOTALS, assert_failed_commit, assert_report,
    emit_scenario, fail_commit_after_insert, lake_counts, report_rows, report_totals, sample_bytes,
)


class TransferTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.source_path = Path(directory) / 'station.sqlite'
        self.lake_path = Path(directory) / 'lake.sqlite'

    def _issue_mixed(self):
        with closing(open_source(self.source_path)) as conn:
            batch_id = prepare_station(conn, SAMPLES / 'station-mixed.json')
            issued = issue(conn, batch_id)
            body = export_by_issue_number(conn, issued.issue_number)
            self.assertEqual(body, issued.body)
            return issued

    def test_v4_issued_context_survives_source_close(self):
        issued = self._issue_mixed()
        envelope = decode_v1(issued.body)
        payloads = [[page.page_id, page.dictionary_version, list(page.codes)]
                    for page in envelope.pages]
        expected_payloads = [['Z-first', 'D1', [0, 1]], ['A-later', 'D2', [0, 1]]]
        self.assertEqual((envelope.batch_id, envelope.unit), ('station-07', 'mC'))
        self.assertEqual(payloads, expected_payloads)
        definitions = {item.version: (item.definition.entries, item.definition.unit,
                                      item.definition.missing_code) for item in envelope.dictionaries}
        self.assertEqual(definitions, {'D1': (((0, 5), (1, 9)), 'mC', 255),
                                       'D2': (((0, 11), (1, 13)), 'mC', 255)})
        with closing(open_lake(self.lake_path)) as lake:
            accepted = accept(lake, issued.body)
            report = read(lake, accepted.result_number)
            self.assertEqual(report.release, accepted)
            self.assertEqual(accepted.batch_id, 'station-07')
            assert_report(self, report, MIXED_ROWS, MIXED_TOTALS)
        emit_scenario('v4.issued_context', json.loads(sample_bytes('station-mixed.json')),
                      expected_payloads, payloads, {'rows': MIXED_ROWS, 'totals': MIXED_TOTALS},
                      {'rows': report_rows(report), 'totals': report_totals(report)},
                      ['exchange/issue.py', 'exchange/envelope.py', 'lake/admission.py',
                       'lake/materialize.py', 'audit/reconcile.py'],
                      'Read 0 and 1 from D1 as 5 and 9 and from D2 as 11 and 13. Their sum is 38.')

    def test_v4_fixed_export_survives_new_dictionary_and_reopen(self):
        issued = self._issue_mixed()
        with closing(open_lake(self.lake_path)) as lake:
            old = accept(lake, issued.body)
            original_report = read(lake, old.result_number)
            assert_report(self, original_report, MIXED_ROWS, MIXED_TOTALS)
        with closing(open_source(self.source_path)) as source:
            register(source, 'D3', {0: 40, 1: 50}, 'mC', 255)
        with closing(open_source(self.source_path)) as source:
            exported = export_by_issue_number(source, issued.issue_number)
            self.assertEqual(exported, issued.body)
        with closing(open_lake(self.lake_path)) as lake:
            self.assertEqual(read(lake, old.result_number), original_report)
            replay = accept(lake, exported)
            self.assertNotEqual(replay.result_number, old.result_number)
            assert_report(self, read(lake, replay.result_number), MIXED_ROWS, MIXED_TOTALS)

    def test_v4_independent_mixed_uses_page_evidence(self):
        body = sample_bytes('independent-mixed.json')
        with closing(open_lake(self.lake_path)) as lake:
            release = accept(lake, body)
            report = read(lake, release.result_number)
            self.assertEqual(release.batch_id, 'independent-07')
            self.assertEqual(report.release, release)
            assert_report(self, report, MIXED_ROWS, MIXED_TOTALS)
        emit_scenario('v4.independent_mixed', json.loads(body), MIXED_ROWS, report_rows(report),
                      MIXED_TOTALS, report_totals(report),
                      ['exchange/evidence.py', 'lake/admission.py', 'lake/materialize.py',
                       'lake/layout.py', 'audit/reconcile.py'],
                      'Look up each page in its own fixture map in page and slot order, then sum.')

    def test_v4_unknown_batch_does_not_issue(self):
        with closing(open_source(self.source_path)) as source:
            with self.assertRaisesRegex(KeyError, 'unknown frozen batch'):
                issue(source, 'unregistered-day')
            self.assertEqual(source.execute('SELECT COUNT(*) FROM issuances').fetchone(), (0,))

    def test_v5_independent_zero_and_missing(self):
        body = sample_bytes('zero-missing.json')
        with closing(open_lake(self.lake_path)) as lake:
            release = accept(lake, body)
            report = read(lake, release.result_number)
            assert_report(self, report, ZERO_ROWS, ZERO_TOTALS)
            self.assertTrue(release.receipt)
        emit_scenario('v5', json.loads(body), ZERO_ROWS, report_rows(report),
                      ZERO_TOTALS, report_totals(report),
                      ['lake/admission.py', 'lake/materialize.py', 'audit/totals.py'],
                      'Code 0 maps to integer zero and code 255 is the one missing slot.')

    def _assert_rejected(self, body, message):
        with closing(open_lake(self.lake_path)) as lake:
            counts = lake_counts(lake)
            with self.assertRaisesRegex(ValueError, message):
                accept(lake, body)
            self.assertEqual(lake_counts(lake), counts)
        with closing(open_lake(self.lake_path)) as lake:
            self.assertEqual(lake_counts(lake), counts)

    def test_v6_missing_evidence_leaves_no_artifacts(self):
        self._assert_rejected(sample_bytes('missing-evidence.json'), 'missing dictionary evidence.*D2')

    def test_v6_conflicting_definition_rejected_in_either_order(self):
        doc = json.loads(sample_bytes('independent-mixed.json'))
        doc['dictionaries'].append({'version': 'D2', 'unit': 'mC',
                                    'entries': [[0, 35], [1, 13]], 'missing_code': 255})
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                if reverse:
                    doc['dictionaries'].reverse()
                self._assert_rejected(json.dumps(doc), 'conflicting dictionary evidence.*D2')

    def test_v6_invalid_code_rejects_entire_package(self):
        doc = json.loads(sample_bytes('independent-mixed.json'))
        doc['pages'][1]['codes'].append(99)
        self._assert_rejected(json.dumps(doc), 'A-later.*slot 2.*undefined code 99')

    def test_v6_unit_mismatch_rejects_entire_package(self):
        doc = json.loads(sample_bytes('independent-mixed.json'))
        doc['dictionaries'][0]['unit'] = 'mL'
        self._assert_rejected(json.dumps(doc), 'A-later.*batch unit.*mC.*mL')

    def test_identical_dictionary_declarations_are_allowed(self):
        doc = json.loads(sample_bytes('independent-mixed.json'))
        doc['dictionaries'].append({'version': 'D2', 'unit': 'mC',
                                    'entries': [[0, 11], [1, 13]], 'missing_code': 255})
        with closing(open_lake(self.lake_path)) as lake:
            release = accept(lake, json.dumps(doc))
            assert_report(self, read(lake, release.result_number), MIXED_ROWS, MIXED_TOTALS)

    def test_v7_sqlite_commit_rollback_preserves_old_result(self):
        with closing(open_lake(self.lake_path)) as lake:
            old = accept(lake, sample_bytes('zero-missing.json'))
            report = read(lake, old.result_number)
            assert_report(self, report, ZERO_ROWS, ZERO_TOTALS)
            counts = lake_counts(lake)
            trace = fail_commit_after_insert(lake, 'lake_receipts')
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'FOREIGN KEY constraint failed'):
                accept(lake, sample_bytes('independent-mixed.json'))
            self.assertTrue(any('INSERT INTO lake_receipts' in statement for statement in trace))
            assert_failed_commit(self, lake, trace)
        with closing(open_lake(self.lake_path)) as lake:
            self.assertEqual(lake_counts(lake), counts)
            self.assertEqual(lake.execute('SELECT result_number FROM lake_results').fetchall(),
                             [(old.result_number,)])
            self.assertEqual(lake.execute('SELECT receipt FROM lake_receipts').fetchall(), [(old.receipt,)])
            self.assertEqual(read(lake, old.result_number), report)
            with self.assertRaises(KeyError):
                read(lake, old.result_number + 1)
