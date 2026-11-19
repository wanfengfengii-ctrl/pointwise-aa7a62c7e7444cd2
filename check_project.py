"""Run this project's full verification from the repository root.

The project's single verify entry point runs, in order:

1. the full unittest suite (original V1 regressions plus review ranges);
2. the joint review-range scenarios in ``review_range_examples``, whose module
   also re-runs the V1 example checks.

Any failing step exits non-zero.  Exit zero therefore means every check this
command actually covers passed.
"""
import sys
import unittest


def run_suite():
    suite = unittest.defaultTestLoader.discover('tests', top_level_dir='.')
    if suite.countTestCases():
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    else:
        print('No tests discovered in tests/', file=sys.stderr)
        result = unittest.TestResult()
    for status, failed_cases in (
        ('FAIL', (test for test, _ in result.failures)),
        ('ERROR', (test for test, _ in result.errors)),
        ('UNEXPECTED_SUCCESS', result.unexpectedSuccesses),
    ):
        for test in failed_cases:
            print(f'{status} {test.id()}')
    successful = result.testsRun > 0 and result.wasSuccessful()
    print(f'TEST SUMMARY total={result.testsRun} failures={len(result.failures)} '
          f'errors={len(result.errors)} skipped={len(result.skipped)} '
          f'expected_failures={len(result.expectedFailures)} '
          f'unexpected_successes={len(result.unexpectedSuccesses)} '
          f'result={"PASS" if successful else "FAIL"}')
    return 0 if successful else 1


def main():
    status = run_suite()
    if status != 0:
        print('verify: unittest suite failed', file=sys.stderr)
        return status
    import review_range_examples
    try:
        review_range_examples.run_checks()
    except BaseException as exc:  # report and fail clearly on any check error
        print(f'verify: joint review-range checks failed: {exc!r}', file=sys.stderr)
        return 1
    print('VERIFY SUMMARY result=PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
