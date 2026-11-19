import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from audit.reconcile import read
from catalog.dictionaries import register
from exchange.issue import issue
from lake.admission import accept
from lake.layout import load_layout
from lake.repack import repack
from lake.store import open_lake
from source.freeze import freeze
from source.pages import SourcePage
from source.store import open_source
from tests import (
    MIXED_ROWS, MIXED_TOTALS, ZERO_ROWS, ZERO_TOTALS, assert_failed_commit, assert_report,
    emit_scenario, fail_commit_after_insert, lake_counts, report_rows, report_totals, sample_bytes,
)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.path = directory / 'lake.sqlite'
        self.source_path = directory / 'station.sqlite'

    def test_v8_cross_dictionary_physical_page_survives_reopen(self):
        body = sample_bytes('independent-mixed.json')
        with closing(open_lake(self.path)) as lake:
            result = accept(lake, body)
            number = result.result_number
            before = read(lake, number)
            self.assertEqual([len(page.rows) for page in load_layout(lake, number)], [2, 2])
            self.assertEqual(repack(lake, number, 3), 2)
        with closing(open_lake(self.path)) as lake:
            layout = load_layout(lake, number)
            sizes = [len(page.rows) for page in layout]
            self.assertEqual(sizes, [3, 1])
            self.assertEqual([row.ordinal for row in layout[0].rows], [0, 1, 2])
            third = layout[0].rows[2]
            self.assertEqual((third.value, third.page_id, third.slot, third.dictionary_version),
                             (11, 'A-later', 0, 'D2'))
            report = read(lake, number)
            self.assertEqual(report, before)
            self.assertEqual(report.release, result)
            assert_report(self, report, MIXED_ROWS, MIXED_TOTALS)
        emit_scenario('v8', {'package': json.loads(body), 'page_size': 3}, [3, 1], sizes,
                      {'rows': MIXED_ROWS, 'totals': MIXED_TOTALS},
                      {'rows': report_rows(report), 'totals': report_totals(report)},
                      ['lake/repack.py', 'lake/layout.py', 'audit/reconcile.py', 'audit/provenance.py'],
                      'Cut the four hand-decoded rows after the third. Its original slot remains zero.')

    def test_v8_invalid_page_size_keeps_old_layout(self):
        with closing(open_lake(self.path)) as lake:
            release = accept(lake, sample_bytes('independent-mixed.json'))
            number = release.result_number
            before = load_layout(lake, number)
            for invalid in (0, -1, True, 1.5):
                with self.subTest(invalid=invalid):
                    with self.assertRaisesRegex(ValueError, 'positive integer'):
                        repack(lake, number, invalid)
                    self.assertEqual(load_layout(lake, number), before)
            assert_report(self, read(lake, number), MIXED_ROWS, MIXED_TOTALS)

    def test_v8_repack_commit_failure_keeps_old_physical_records(self):
        with closing(open_lake(self.path)) as lake:
            release = accept(lake, sample_bytes('independent-mixed.json'))
            number = release.result_number
            before = load_layout(lake, number)
            self.assertEqual([len(page.rows) for page in before], [2, 2])
            report = read(lake, number)
            counts = lake_counts(lake)
            trace = fail_commit_after_insert(lake, 'physical_pages')
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'FOREIGN KEY constraint failed'):
                repack(lake, number, 3)
            self.assertTrue(any('INSERT INTO physical_pages' in statement for statement in trace))
            assert_failed_commit(self, lake, trace)
        with closing(open_lake(self.path)) as lake:
            self.assertEqual(lake_counts(lake), counts)
            self.assertEqual(load_layout(lake, number), before)
            self.assertEqual(read(lake, number), report)
            assert_report(self, report, MIXED_ROWS, MIXED_TOTALS)

    def test_v8_by_result_number_across_admission_and_repack(self):
        with closing(open_lake(self.path)) as lake:
            first = accept(lake, sample_bytes('independent-mixed.json'))
            second = accept(lake, sample_bytes('zero-missing.json'))
            self.assertNotEqual(first.result_number, second.result_number)
            self.assertNotEqual(first.receipt, second.receipt)
            repack(lake, first.result_number, 3)
        with closing(open_lake(self.path)) as lake:
            first_report = read(lake, first.result_number)
            second_report = read(lake, second.result_number)
            self.assertEqual(first_report.release, first)
            self.assertEqual(second_report.release, second)
            assert_report(self, first_report, MIXED_ROWS, MIXED_TOTALS)
            assert_report(self, second_report, ZERO_ROWS, ZERO_TOTALS)
            self.assertEqual([len(page.rows) for page in load_layout(lake, second.result_number)], [2])

    def test_v8_unknown_result_while_another_exists(self):
        with closing(open_lake(self.path)) as lake:
            release = accept(lake, sample_bytes('zero-missing.json'))
            with self.assertRaisesRegex(KeyError, 'unknown result number'):
                read(lake, release.result_number + 100)
            with self.assertRaisesRegex(KeyError, 'unknown result number'):
                repack(lake, release.result_number + 100, 3)
            assert_report(self, read(lake, release.result_number), ZERO_ROWS, ZERO_TOTALS)

    def test_v8_zero_and_missing_after_repack(self):
        with closing(open_lake(self.path)) as lake:
            release = accept(lake, sample_bytes('zero-missing.json'))
            self.assertEqual(repack(lake, release.result_number, 1), 2)
        with closing(open_lake(self.path)) as lake:
            report = read(lake, release.result_number)
            assert_report(self, report, ZERO_ROWS, ZERO_TOTALS)
            self.assertIs(type(report.details[0].value), int)
            self.assertIsNone(report.details[1].value)

    def test_v8_exact_integer_cancellation_after_repack(self):
        large = 9007199254740992
        mapping = [[0, large], [1, 1], [2, -large]]
        body = self._issue_integer_batch(mapping, 'exact')
        with closing(open_lake(self.path)) as lake:
            release = accept(lake, body)
            self.assertEqual(repack(lake, release.result_number, 2), 2)
        expected = [[large, 'left', 0, 'D-exact'], [1, 'left', 1, 'D-exact'],
                    [-large, 'right', 0, 'D-exact']]
        with closing(open_lake(self.path)) as lake:
            report = read(lake, release.result_number)
            assert_report(self, report, expected, [3, 0, 1])
            self.assertTrue(all(type(item.value) is int for item in report.details))
        emit_scenario('v8.exact_integer', json.loads(body), expected, report_rows(report),
                      [3, 0, 1], report_totals(report),
                      ['catalog/codebook.py', 'lake/layout.py', 'lake/repack.py', 'audit/totals.py'],
                      'The opposite large integers cancel exactly and leave the middle reading 1.')

    def _issue_integer_batch(self, mapping, batch_id):
        with closing(open_source(self.source_path)) as source:
            register(source, 'D-exact', mapping, 'mC')
        with closing(open_source(self.source_path)) as source:
            freeze(source, batch_id, 'mC', (SourcePage('left', 'D-exact', bytes([0, 1])),
                                           SourcePage('right', 'D-exact', bytes([2]))))
            return issue(source, batch_id).body

    def test_values_beyond_sqlite_integer_range_round_trip(self):
        huge = 1 << 20000
        body = self._issue_integer_batch([[0, huge], [1, -5], [2, -huge]], 'huge')
        self.assertEqual(json.loads(body)['dictionaries'][0]['entries'][0], [0, hex(huge)])
        with closing(open_lake(self.path)) as lake:
            release = accept(lake, body)
            repack(lake, release.result_number, 3)
        with closing(open_lake(self.path)) as lake:
            report = read(lake, release.result_number)
            self.assertTrue([item.value for item in report.details] == [huge, -5, -huge])
            self.assertEqual(report_totals(report), [3, 0, -5])

    def test_all_missing_rows_keep_zero_total(self):
        doc = json.loads(sample_bytes('zero-missing.json'))
        doc['pages'][0]['codes'] = [255, 255]
        with closing(open_lake(self.path)) as lake:
            release = accept(lake, json.dumps(doc))
            repack(lake, release.result_number, 1)
            assert_report(self, read(lake, release.result_number),
                          [[None, 'zero-probe', 0, 'D0'], [None, 'zero-probe', 1, 'D0']], [0, 2, 0])
