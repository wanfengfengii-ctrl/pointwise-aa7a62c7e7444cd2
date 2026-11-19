import json
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from audit.reconcile import read
from catalog.dictionaries import register
from exchange.exports import export_by_issue_number
from exchange.issue import issue
from lake.admission import accept
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
            }
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
