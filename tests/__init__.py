"""Regression fixtures and independent expected values for the observation lake."""

import json

from demo import SAMPLES


MIXED_ROWS = [[5, 'Z-first', 0, 'D1'], [9, 'Z-first', 1, 'D1'],
              [11, 'A-later', 0, 'D2'], [13, 'A-later', 1, 'D2']]
MIXED_TOTALS = [4, 0, 38]
ZERO_ROWS = [[0, 'zero-probe', 0, 'D0'], [None, 'zero-probe', 1, 'D0']]
ZERO_TOTALS = [1, 1, 0]


def sample_bytes(name):
    return (SAMPLES / name).read_bytes()


def report_rows(report):
    return [[item.value, item.origin.page_id, item.origin.slot, item.origin.dictionary_version]
            for item in report.details]


def report_totals(report):
    totals = report.totals
    return [totals.non_missing_count, totals.missing_count, totals.total]


def assert_report(test, report, rows, totals, unit='mC'):
    test.assertEqual(report.release.unit, unit)
    test.assertEqual([item.ordinal for item in report.details], list(range(len(rows))))
    test.assertEqual(report_rows(report), rows)
    test.assertEqual(report_totals(report), totals)


def lake_counts(conn):
    tables = ('lake_results', 'lake_receipts', 'physical_pages', 'physical_rows')
    return tuple(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in tables)


def fail_commit_after_insert(conn, table):
    conn.execute('CREATE TABLE commit_gate (gate_id INTEGER PRIMARY KEY)')
    conn.execute('''
        CREATE TABLE commit_checks (
            gate_id INTEGER NOT NULL REFERENCES commit_gate(gate_id) DEFERRABLE INITIALLY DEFERRED
        )
    ''')
    conn.execute(f'''
        CREATE TRIGGER check_at_commit AFTER INSERT ON {table}
        BEGIN
            INSERT INTO commit_checks (gate_id) VALUES (1);
        END
    ''')
    trace = []
    conn.set_trace_callback(trace.append)
    return trace


def assert_failed_commit(test, conn, trace):
    conn.set_trace_callback(None)
    operations = {'BEGIN IMMEDIATE', 'COMMIT', 'ROLLBACK'}
    test.assertEqual([statement for statement in trace if statement in operations],
                     ['BEGIN IMMEDIATE', 'COMMIT', 'ROLLBACK'])
    test.assertFalse(conn.in_transaction)
    test.assertEqual(conn.execute('SELECT COUNT(*) FROM commit_checks').fetchone(), (0,))


def emit_scenario(case_id, inputs, expected, actual, downstream_expected, downstream_actual,
                  code_paths, oracle):
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': case_id,
        'input': inputs,
        'expected': expected,
        'actual': actual,
        'downstream_expected': downstream_expected,
        'downstream_actual': downstream_actual,
        'code_paths': code_paths,
        'oracle': oracle,
    }, ensure_ascii=False, separators=(',', ':')), flush=True)
