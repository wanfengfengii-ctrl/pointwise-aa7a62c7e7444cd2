import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from catalog.codebook import decode_code
from catalog.dictionaries import get_dictionary, register
from source.freeze import freeze, get_frozen_batch
from source.page_validation import validate_page
from source.pages import SourcePage, load_page, store_page
from source.store import open_source, source_transaction


class SourceTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.path = Path(directory) / 'station.sqlite'

    def test_v1_declared_order_survives_reopen(self):
        first = SourcePage('Z-first', 'D1', b'\x00\x01')
        later = SourcePage('A-later', 'D2', b'\x00\x01')
        with closing(open_source(self.path)) as conn:
            register(conn, 'D1', {0: 5, 1: 9}, 'mC', 255)
            register(conn, 'D2', {0: 11, 1: 13}, 'mC', 255)
            with source_transaction(conn):
                for page in (later, first):
                    validate_page(conn, page, 'mC')
                    store_page(conn, page)
            self.assertEqual(freeze(conn, 'night-07', 'mC', (first, later)), 'night-07')
        with closing(open_source(self.path)) as conn:
            frozen = get_frozen_batch(conn, 'night-07')
            self.assertEqual((frozen.batch_id, frozen.unit), ('night-07', 'mC'))
            self.assertEqual([(page.page_id, page.dictionary_version, list(page.codes))
                              for page in frozen.pages],
                             [('Z-first', 'D1', [0, 1]), ('A-later', 'D2', [0, 1])])

    def test_v2_conflicting_definition_preserves_original(self):
        with closing(open_source(self.path)) as conn:
            register(conn, 'D1', {0: 5}, 'mC', 255)
            freeze(conn, 'original', 'mC', (SourcePage('p', 'D1', bytes([0])),))
        with closing(open_source(self.path)) as conn:
            with self.assertRaisesRegex(ValueError, 'conflicting definition.*D1'):
                register(conn, 'D1', {0: 11}, 'mC', 255)
            book = get_dictionary(conn, 'D1')
            self.assertEqual(decode_code(book, 0), 5)
            self.assertIsNone(decode_code(book, 255))
            self.assertEqual(get_frozen_batch(conn, 'original').pages[0].dictionary_version, 'D1')

    def test_v2_same_definition_and_other_user_version_survive_reopen(self):
        with closing(open_source(self.path)) as conn:
            original = register(conn, 'D1', [(1, 9), (0, 5)], 'mC', 255)
            register(conn, 'user-version', {0: -2}, 'mC')
        with closing(open_source(self.path)) as conn:
            repeated = register(conn, 'D1', [(0, 5), (1, 9)], 'mC', 255)
            self.assertEqual(repeated, original)
            self.assertEqual(repeated.entries, ((0, 5), (1, 9)))
            self.assertEqual(get_dictionary(conn, 'user-version').entries, ((0, -2),))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM dictionaries').fetchone(), (2,))

    def test_v3_invalid_late_code_rolls_back_entire_freeze(self):
        with closing(open_source(self.path)) as conn:
            register(conn, 'D1', {0: 5, 1: 9}, 'mC', 255)
            freeze(conn, 'prior', 'mC', (SourcePage('prior-page', 'D1', bytes([1])),))
            pages = (SourcePage('valid-first', 'D1', bytes([0])),
                     SourcePage('invalid-late', 'D1', bytes([0, 99])))
            with self.assertRaisesRegex(ValueError, 'invalid-late.*slot 1.*undefined code 99'):
                freeze(conn, 'rejected', 'mC', pages)
        with closing(open_source(self.path)) as conn:
            with self.assertRaises(KeyError):
                get_frozen_batch(conn, 'rejected')
            for page in pages:
                with self.assertRaises(KeyError):
                    load_page(conn, page.page_id)
            self.assertEqual(get_frozen_batch(conn, 'prior').pages[0].codes, bytes([1]))

    def test_v3_unknown_dictionary_rejects_freeze(self):
        with closing(open_source(self.path)) as conn:
            with self.assertRaisesRegex(ValueError, 'unready.*unknown dictionary.*unknown'):
                freeze(conn, 'rejected', 'mC', (SourcePage('unready', 'unknown', bytes([0])),))
            with self.assertRaisesRegex(KeyError, 'unknown frozen batch'):
                get_frozen_batch(conn, 'rejected')

    def test_v3_unit_mismatch_rejects_freeze(self):
        with closing(open_source(self.path)) as conn:
            register(conn, 'L1', {0: 5}, 'mL')
            with self.assertRaisesRegex(ValueError, 'wrong-unit.*batch unit.*mC.*mL'):
                freeze(conn, 'rejected', 'mC', (SourcePage('wrong-unit', 'L1', bytes([0])),))
            with self.assertRaises(KeyError):
                get_frozen_batch(conn, 'rejected')

    def test_repeat_freeze_cannot_replace_a_frozen_batch(self):
        with closing(open_source(self.path)) as conn:
            register(conn, 'D1', {0: 5}, 'mC')
            original = SourcePage('original', 'D1', bytes([0]))
            freeze(conn, 'fixed', 'mC', (original,))
            replacement = SourcePage('replacement', 'D1', bytes([0, 0]))
            with self.assertRaisesRegex(ValueError, 'already exists'):
                freeze(conn, 'fixed', 'mC', (replacement,))
            self.assertEqual(get_frozen_batch(conn, 'fixed').pages, (original,))
            with self.assertRaises(KeyError):
                load_page(conn, 'replacement')
