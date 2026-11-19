import json
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from audit.reconcile import read
from catalog.dictionaries import register
from exchange.exports import export_by_issue_number
from exchange.issue import issue, issue_slice, make_commitment
from lake.admission import accept, register_review, accept_slice
from lake.repack import repack
from lake.store import open_lake
from source.freeze import freeze
from source.pages import SourcePage
from source.store import open_source


SAMPLES = Path(__file__).resolve().parent / 'samples'


def prepare_station(conn, sample_path):
    document = json.loads(sample_path.read_text(encoding='utf-8'))
    for item in document['dictionaries']:
        register(conn, item['version'], item['entries'], item['unit'], item['missing_code'])
    pages = tuple(SourcePage(page['page_id'], page['dictionary_version'], bytes(page['codes']))
                  for page in document['pages'])
    return freeze(conn, document['batch_id'], document['unit'], pages)


def prepare_review_station(conn):
    """Freeze the two-page review batch with rows [5, 9, 11, 13]."""
    register(conn, 'D1', {0: 5, 1: 9}, 'mC', 255)
    register(conn, 'D2', {0: 11, 1: 13}, 'mC', 255)
    return freeze(conn, 'review-demo', 'mC',
                  (SourcePage('Z-first', 'D1', bytes([0, 1])),
                   SourcePage('A-later', 'D2', bytes([0, 1]))))


def review_window_scenario(root):
    """Overlapping slices over [0,4) wait, then reopen fills the gap."""
    station_path = root / 'review-station.sqlite'
    lake_path = root / 'review-lake.sqlite'

    with closing(open_source(station_path)) as station:
        prepare_review_station(station)
        commitment = make_commitment(station, 'review-demo')
        slices = {rng: issue_slice(station, commitment, *rng)
                  for rng in ((1, 3), (3, 4))}
        early = issue_slice(station, commitment, 0, 3)

    with closing(open_lake(lake_path)) as lake:
        register_review(lake, 'night-review', commitment, 0, 4)
        # [0,3) and [1,3) overlap on positions 1 and 2 but leave position 3
        # unproven, so neither the overlap nor a replay issues a result.
        first = accept_slice(lake, 'night-review', early)
        second = accept_slice(lake, 'night-review', slices[(1, 3)])
        counts = lake.execute('SELECT COUNT(*) FROM lake_results').fetchone()[0]
        pending = {'result_after_overlap': first, 'result_after_second': second,
                   'results_recorded': counts}

    # Close the station, reopen it, and sign only the still-missing tail.
    with closing(open_source(station_path)) as station:
        reopened = make_commitment(station, 'review-demo')
        assert reopened == commitment
        head = issue_slice(station, reopened, 3, 4)

    with closing(open_lake(lake_path)) as lake:
        number = accept_slice(lake, 'night-review', head)
        pages = repack(lake, number, 3)
        report = read(lake, number)
        summary = {
            'result_number': report.release.result_number,
            'batch_id': report.release.batch_id,
            'unit': report.release.unit,
            'physical_pages': pages,
            'rows': [[item.ordinal, item.value, item.origin.page_id,
                      item.origin.slot, item.origin.dictionary_version]
                     for item in report.details],
            'totals': [report.totals.non_missing_count, report.totals.missing_count,
                       report.totals.total],
            'replay_result_number': accept_slice(lake, 'night-review', slices[(3, 4)]),
        }

    with closing(open_lake(lake_path)) as lake:
        reopened_report = read(lake, number)
        summary['reopen_matches'] = (
            [[item.ordinal, item.value, item.origin.page_id, item.origin.slot,
              item.origin.dictionary_version] for item in reopened_report.details],
            [reopened_report.totals.non_missing_count, reopened_report.totals.missing_count,
             reopened_report.totals.total],
        ) == (summary['rows'], summary['totals'])
    return {**pending, **summary}


def main() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with closing(open_source(root / 'station.sqlite')) as station:
            batch_id = prepare_station(station, SAMPLES / 'station-mixed.json')
            issued = issue(station, batch_id)
            body = export_by_issue_number(station, issued.issue_number)

        lake_path = root / 'lake.sqlite'
        with closing(open_lake(lake_path)) as lake:
            offline = accept(lake, body)
            independent = accept(lake, (SAMPLES / 'independent-mixed.json').read_bytes())
            offline_pages = repack(lake, offline.result_number, 3)
            independent_pages = repack(lake, independent.result_number, 3)

        with closing(open_lake(lake_path)) as lake:
            output = {
                'issue_number': issued.issue_number,
                'offline': {'physical_pages': offline_pages,
                            'report': asdict(read(lake, offline.result_number))},
                'independent': {'physical_pages': independent_pages,
                                'report': asdict(read(lake, independent.result_number))},
                'review_window': review_window_scenario(root),
            }
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
