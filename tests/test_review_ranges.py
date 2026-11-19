import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from audit.reconcile import read
from catalog.dictionaries import register
from exchange.issue import issue_slice, make_commitment
from lake.admission import accept_slice, register_review
from lake.repack import repack
from lake.store import open_lake
from review_range_examples import freeze_example
from source.store import open_source
from tests import (
    assert_failed_commit, fail_commit_after_insert, lake_counts, report_rows,
    report_totals,
)

WINDOW_ROWS = [[9, 'Z-first', 1, 'D1'], [11, 'A-later', 0, 'D2'],
               [13, 'A-later', 1, 'D2'], [None, 'M-tail', 0, 'D3']]
WINDOW_TOTALS = [3, 1, 33]


class ReviewRangeTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.root = directory
        self.source_path = directory / 'station.sqlite'
        self.lake_path = directory / 'lake.sqlite'

    # -- station helpers ---------------------------------------------------

    def _station(self, alternative=False, name='station.sqlite'):
        path = self.root / name
        with closing(open_source(path)) as source:
            freeze_example(source, alternative=alternative)
        return path

    def _commitment(self, path=None):
        path = path or self.source_path
        if not path.exists():
            self._station()
        with closing(open_source(path)) as source:
            return make_commitment(source, 'review-06')

    def _slices(self, ranges, path=None):
        commitment = self._commitment(path)
        with closing(open_source(path or self.source_path)) as source:
            return commitment, {rng: issue_slice(source, commitment, *rng) for rng in ranges}

    # -- commitments and slices -------------------------------------------

    def test_commitment_is_fixed_across_reopen(self):
        self._station()
        first = self._commitment()
        second = self._commitment()
        self.assertEqual(first, second)
        document = json.loads(first)
        self.assertEqual((document['version'], document['kind']), (2, 'review-commitment'))
        self.assertEqual((document['batch_id'], document['unit'], document['row_count']),
                         ('review-06', 'mC', 6))
        self.assertEqual(set(document['evidence']), {'algorithm', 'domain', 'root'})

    def test_commitment_of_unknown_batch_is_key_error(self):
        with closing(open_source(self.root / 'empty.sqlite')) as source:
            with self.assertRaisesRegex(KeyError, 'unknown frozen batch'):
                make_commitment(source, 'no-such-batch')

    def test_empty_batch_has_commitment_but_no_slice(self):
        with closing(open_source(self.root / 'empty.sqlite')) as source:
            from source.freeze import freeze
            freeze(source, 'empty', 'mC', ())
            commitment = make_commitment(source, 'empty')
            self.assertEqual(json.loads(commitment)['row_count'], 0)
            with self.assertRaises(ValueError):
                issue_slice(source, commitment, 0, 0)
            with self.assertRaises(ValueError):
                issue_slice(source, commitment, 0, 1)

    def test_slice_endpoint_validation(self):
        self._station()
        commitment = self._commitment()
        with closing(open_source(self.source_path)) as source:
            for start, stop in ((0, 0), (-1, 2), (3, 3), (0, 7), (4, 2)):
                with self.subTest(start=start, stop=stop):
                    with self.assertRaises(ValueError):
                        issue_slice(source, commitment, start, stop)
            for start, stop in ((True, 2), (0, False), (0.0, 2), (0, '2')):
                with self.subTest(start=start, stop=stop):
                    with self.assertRaises(ValueError):
                        issue_slice(source, commitment, start, stop)
            # Endpoints against the empty range of a non-empty batch are fine.
            self.assertEqual(json.loads(issue_slice(source, commitment, 0, 6))['stop'], 6)

    def test_slice_can_be_issued_after_source_reopen(self):
        self._station()
        commitment = self._commitment()
        with closing(open_source(self.source_path)) as source:
            body = issue_slice(source, commitment, 1, 5)
        document = json.loads(body)
        self.assertEqual((document['version'], document['kind'],
                          document['start'], document['stop']),
                         (2, 'review-slice', 1, 5))
        self.assertEqual([row['ordinal'] for row in document['rows']], [1, 2, 3, 4])
        self.assertEqual(document['commitment_id'], __import__('hashlib').sha256(
            commitment).hexdigest())

    def test_slice_discloses_only_its_window(self):
        self._station()
        commitment, slices = self._slices([(2, 4)])
        document = json.loads(slices[(2, 4)])
        self.assertEqual([row['ordinal'] for row in document['rows']], [2, 3])
        self.assertEqual({row['page_id'] for row in document['rows']}, {'A-later'})
        # No page codes, opening dictionaries or row values outside the slice.
        serialized = slices[(2, 4)].decode('utf-8')
        self.assertNotIn('M-tail', serialized)
        self.assertNotIn('Z-first', serialized)
        self.assertNotIn('dictionaries', serialized)
        for entry in document['proof']:
            self.assertEqual(set(entry), {'side', 'hash'})
        # The commitment itself never carries readings.
        text = commitment.decode('utf-8')
        for token in ('"value"', 'M-tail', 'A-later'):
            self.assertNotIn(token, text)

    # -- registration ------------------------------------------------------

    def test_register_review_rejects_bad_windows_and_rebinds(self):
        self._station()
        commitment = self._commitment()
        with closing(open_lake(self.lake_path)) as lake:
            for start, stop in ((2, 2), (5, 1), (-1, 2), (0, 7)):
                with self.subTest(start=start, stop=stop):
                    with self.assertRaises(ValueError):
                        register_review(lake, 'task', commitment, start, stop)
            with self.assertRaises(ValueError):
                register_review(lake, '  ', commitment, 1, 5)
            register_review(lake, 'task', commitment, 1, 5)
            # Same binding is a legal replay.
            register_review(lake, 'task', commitment, 1, 5)
            with self.assertRaisesRegex(ValueError, 'bound differently'):
                register_review(lake, 'task', commitment, 1, 4)
            with self.assertRaisesRegex(ValueError, 'bound differently'):
                register_review(lake, 'task', self._commitment(self._station(
                    alternative=True, name='other.sqlite')), 1, 5)
            with self.assertRaises(KeyError):
                accept_slice(lake, 'unknown-task', b'{}')

    def test_registered_window_survives_reopen(self):
        self._station()
        commitment = self._commitment()
        with closing(open_lake(self.lake_path)) as lake:
            register_review(lake, 'task', commitment, 1, 5)
        with closing(open_lake(self.lake_path)) as lake:
            register_review(lake, 'task', commitment, 1, 5)
            self.assertIsNone(accept_slice(lake, 'task', self._slices(
                [(2, 4)], path=self.source_path)[1][(2, 4)]))

    # -- the documented window scenario -----------------------------------

    def test_window_completes_once_and_replays(self):
        self._station()
        commitment, slices = self._slices([(2, 4), (1, 3), (4, 5)])
        with closing(open_lake(self.lake_path)) as lake:
            register_review(lake, 'task', commitment, 1, 5)
            self.assertIsNone(accept_slice(lake, 'task', slices[(2, 4)]))
            self.assertIsNone(accept_slice(lake, 'task', slices[(1, 3)]))
            self.assertIsNone(accept_slice(lake, 'task', slices[(2, 4)]))
        with closing(open_lake(self.lake_path)) as lake:
            number = accept_slice(lake, 'task', slices[(4, 5)])
            self.assertEqual(number, 1)
            report = read(lake, number)
            self.assertEqual(report_rows(report), WINDOW_ROWS)
            self.assertEqual(report_totals(report), WINDOW_TOTALS)
            self.assertEqual(report.release.batch_id, 'review-06')
            self.assertEqual(report.release.unit, 'mC')
            # Completed task keeps returning its fixed result for valid replays.
            self.assertEqual(accept_slice(lake, 'task', slices[(2, 4)]), number)

    def test_window_report_survives_repack_and_reopen(self):
        self._station()
        commitment, slices = self._slices([(1, 3), (2, 5), (1, 5)])
        with closing(open_lake(self.lake_path)) as lake:
            register_review(lake, 'task', commitment, 1, 5)
            number = accept_slice(lake, 'task', slices[(1, 5)])
            self.assertEqual(number, 1)
            self.assertEqual(repack(lake, number, 2), 2)
            before = read(lake, number)
        with closing(open_lake(self.lake_path)) as lake:
            report = read(lake, number)
            self.assertEqual(report, before)
            self.assertEqual(report_rows(report), WINDOW_ROWS)
            self.assertEqual(report_totals(report), WINDOW_TOTALS)

    def test_out_of_order_overlap_counts_positions_once(self):
        self._station()
        commitment, slices = self._slices([(1, 2), (2, 3), (4, 5), (3, 4)])
        with closing(open_lake(self.lake_path)) as lake:
            register_review(lake, 'task', commitment, 1, 5)
            for rng in ((1, 2), (2, 3), (4, 5)):
                self.assertIsNone(accept_slice(lake, 'task', slices[rng]))
            number = accept_slice(lake, 'task', slices[(3, 4)])
            self.assertEqual(report_totals(read(lake, number)), WINDOW_TOTALS)
            self.assertEqual({row.ordinal for row in read(lake, number).details},
                             {1, 2, 3, 4})

    # -- tampering and cross-batch material --------------------------------

    def test_tampered_rows_are_rejected_and_gap_remains(self):
        self._station()
        commitment, slices = self._slices([(1, 3), (3, 5), (4, 5), (3, 4)])
        valid = json.loads(slices[(3, 5)])
        tampered = []
        doc = json.loads(json.dumps(valid))
        doc['rows'][0]['value'] = 999
        tampered.append(json.dumps(doc))
        doc = json.loads(json.dumps(valid))
        doc['rows'][0]['slot'] = 0
        tampered.append(json.dumps(doc))
        doc = json.loads(json.dumps(valid))
        doc['rows'][1]['dictionary_version'] = 'D1'
        tampered.append(json.dumps(doc))
        with closing(open_lake(self.lake_path)) as lake:
            register_review(lake, 'task', commitment, 1, 5)
            self.assertIsNone(accept_slice(lake, 'task', slices[(1, 3)]))
            for body in tampered:
                with self.assertRaises(ValueError):
                    accept_slice(lake, 'task', body)
            with self.assertRaises(ValueError):
                accept_slice(lake, 'task', slices[(3, 5)].replace(b'"value":13', b'"value":12'))
            self.assertIsNone(accept_slice(lake, 'task', slices[(4, 5)]))
            # Missing ordinal 3 still blocks completion even after a replay.
            self.assertIsNone(accept_slice(lake, 'task', slices[(4, 5)]))
            self.assertEqual(lake.execute('SELECT COUNT(*) FROM lake_results').fetchone(), (0,))
            number = accept_slice(lake, 'task', slices[(3, 4)])
            self.assertEqual(report_rows(read(lake, number)), WINDOW_ROWS)

    def test_foreign_batch_slice_rejected_even_with_rewritten_identity(self):
        self._station()
        commitment = self._commitment()
        other_station = self._station(alternative=True, name='other.sqlite')
        foreign = self._slices([(4, 5)], path=other_station)[1][(4, 5)]
        document = json.loads(foreign)
        document['commitment_id'] = json.loads(commitment)['evidence']['root']
        with closing(open_lake(self.lake_path)) as lake:
            register_review(lake, 'task', commitment, 1, 5)
            for body in (foreign,
                         json.dumps(document),
                         json.dumps({**json.loads(foreign),
                                     'commitment_id': __import__('hashlib').sha256(
                                         commitment).hexdigest()})):
                with self.assertRaises(ValueError):
                    accept_slice(lake, 'task', body)
            # Still incomplete: the rejected material filled no position.
            self.assertEqual(lake.execute('SELECT COUNT(*) FROM lake_results').fetchone(), (0,))

    def test_out_of_window_slice_rejected_and_gap_preserved(self):
        self._station()
        commitment, slices = self._slices([(0, 2), (1, 4), (4, 5)])
        with closing(open_lake(self.lake_path)) as lake:
            register_review(lake, 'task', commitment, 1, 5)
            self.assertIsNone(accept_slice(lake, 'task', slices[(1, 4)]))
            with self.assertRaisesRegex(ValueError, 'outside the registered window'):
                accept_slice(lake, 'task', slices[(0, 2)])
            self.assertIsNone(accept_slice(lake, 'task', slices[(1, 4)]))
            number = accept_slice(lake, 'task', slices[(4, 5)])
            self.assertEqual(report_rows(read(lake, number)), WINDOW_ROWS)

    def test_duplicate_position_conflict_inside_slice(self):
        self._station()
        commitment, slices = self._slices([(1, 4)])
        doc = json.loads(slices[(1, 4)])
        # Duplicate ordinal 1 with a different value but keep proof material.
        doc['rows'].insert(1, dict(doc['rows'][0], value=999))
        doc['stop'] = doc['start'] + len(doc['rows'])
        with closing(open_lake(self.lake_path)) as lake:
            register_review(lake, 'task', commitment, 1, 5)
            with self.assertRaises(ValueError):
                accept_slice(lake, 'task', json.dumps(doc))

    # -- commit failure ----------------------------------------------------

    def test_commit_failure_preserves_progress_then_retry_completes(self):
        self._station()
        commitment, slices = self._slices([(1, 3), (3, 5)])
        with closing(open_lake(self.lake_path)) as lake:
            register_review(lake, 'task', commitment, 1, 5)
            self.assertIsNone(accept_slice(lake, 'task', slices[(1, 3)]))
            trace = fail_commit_after_insert(lake, 'lake_receipts')
            with self.assertRaisesRegex(sqlite3.IntegrityError,
                                        'FOREIGN KEY constraint failed'):
                accept_slice(lake, 'task', slices[(3, 5)])
            self.assertTrue(any('INSERT INTO lake_receipts' in s for s in trace))
            assert_failed_commit(self, lake, trace)
            self.assertEqual(lake_counts(lake), (0, 0, 0, 0))
            # Re-sending an already accepted slice still waits for completion.
            self.assertIsNone(accept_slice(lake, 'task', slices[(1, 3)]))
            # The rejected final slice left no verified coverage: ordinals 3 and
            # 4 are still missing, so nothing can complete the task yet.
            self.assertEqual(lake.execute('SELECT COUNT(*) FROM lake_results').fetchone(), (0,))
            lake.execute('DROP TRIGGER check_at_commit')
            lake.execute('DROP TABLE commit_checks')
            lake.execute('DROP TABLE commit_gate')
            number = accept_slice(lake, 'task', slices[(3, 5)])
            self.assertEqual(report_totals(read(lake, number)), WINDOW_TOTALS)
        with closing(open_lake(self.lake_path)) as lake:
            self.assertEqual(report_rows(read(lake, number)), WINDOW_ROWS)
            self.assertEqual(accept_slice(lake, 'task', slices[(1, 3)]), number)


if __name__ == '__main__':
    unittest.main()
