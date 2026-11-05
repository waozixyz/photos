"""Capture synthetic Python query behavior; never read an owner library."""
import json
import math
from pathlib import Path
import sqlite3
import struct
import tempfile
from datetime import datetime, timezone

from photos.library import Library, LibraryError, packed
from photos import query, worker


def capture():
    scratch = Path('build/tests/tmp')
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='gallery-oracle-', dir=scratch) as temporary:
        root = Path(temporary).resolve() / 'library'
        library = Library(root)
        identities = [f'{i + 1:064x}' for i in range(26)]
        person = 'f' * 64
        files = []
        for index, identity in enumerate(identities):
            year = 2020 + index // 6
            day = 1 + index % 6
            if index == 0:
                year = 1
            elif index == 1:
                year = 1900
            elif index == 2:
                year = 1969
            elif index == 3:
                year = 1970
            taken = datetime(year, 7, day, 12, tzinfo=timezone.utc).timestamp()
            metadata = {'unknown': {'large': 2**60 + 123, 'values': [None, '旅行', {'keep': True}]}}
            if index == 18:
                metadata['photosLive'] = {'video': identities[19]}
            if index == 19:
                metadata['photosLive'] = {'photo': identities[18]}
            if index == 20:
                metadata['photosMotion'] = {'version': 1, 'offset': 100, 'length': 20}
            kind = 'video' if index in {19, 21} else 'photo'
            extension = '.mp4' if kind == 'video' else ('.jpg' if index == 20 else '.png')
            size = 20 * 2**20 + index if index % 2 else 5 * 2**30 + index
            if index == 25:
                size = 10485760
            manifest = packed({'chunks': ['synthetic-encrypted-chunk']}) if index != 24 else '{}'
            name = ['Beach 100%_fun.png', 'Birthday cake.png', 'Café.png'][index % 3]
            library.db.execute('''INSERT INTO photos(id,name,extension,kind,size,taken,width,height,metadata,albums,
                favorite,hidden,archived,trashed,caption,tags,updated,dirty,manifest)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (identity, name, extension, kind, size, taken, 40, 30, packed(metadata), packed(['Family', 'Café']),
                 int(index % 3 == 0), int(index == 22), int(index == 21), int(index == 23),
                 'A birthday cake with family' if index % 3 == 1 else '',
                 'family, beach' if index % 2 == 0 else 'work', 1000.0 + index, index % 2, manifest))
            original = library.original(library.photo(identity))
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(b'synthetic-gallery-original')
            files.append(str(original.relative_to(root)))
            if index % 2 == 0:
                preview = library.thumbnail(identity)
                preview.parent.mkdir(parents=True, exist_ok=True)
                preview.write_bytes(b'synthetic-gallery-preview')
                files.append(str(preview.relative_to(root)))
            score = [1.0, .96, .93, .8, 0.0][index % 5]
            vector = struct.pack('<512f', score, math.sqrt(1.0 - score * score), *([0.0] * 510))
            library.db.execute('INSERT INTO semantic_embeddings VALUES(?,?,?,?)', (identity, 'clip-vit-b32-int8-v1', vector, ''))
            if 4 <= index < 18:
                library.db.execute('INSERT INTO person_tags VALUES(?,?)', (identity, person))
        library.db.execute('INSERT INTO people VALUES(?,?,?,?,?)', (person, 'Ada', 0, 1000.0, '1900-02-28'))
        for number, name, predicate in [(1, 'Family', ''), (2, 'Café', ''), (3, 'Smart family', 'tag:family')]:
            library.db.execute('''INSERT INTO albums(id,name,created,updated,filter,pinned,cover)
                VALUES(?,?,?,?,?,?,?)''', (str(number), name, 1000.0 + number, 1000.0, predicate, number == 1, identities[0]))
        library.set_setting('people_enabled', True)
        library.set_setting('people_processing', False)
        library.set_setting('semantic_enabled', True)
        library.set_setting('semantic_processing', False)
        library.set_setting('export', {'status': 'Synthetic export', 'continuous': True, 'unknown': None})
        library.db.commit()

        def normalize(value):
            if isinstance(value, str):
                return value.replace(str(root), '@ROOT@').replace(str(Path.cwd()), '@PACKAGE@')
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            if isinstance(value, list):
                return [normalize(item) for item in value]
            return value

        cases = []

        def case(kind, request):
            try:
                if kind == 'page':
                    response = query.page(library, **request)
                elif kind == 'library':
                    response = library.query(**request)
                elif kind == 'ui':
                    response = worker.ui_query(library, request)
                else:
                    response = query.validate(library, request)
                cases.append({'kind': kind, 'request': request, 'response': normalize(response)})
            except (LibraryError, ValueError, TypeError) as error:
                cases.append({'kind': kind, 'request': request, 'error': str(error), 'error_type': type(error).__name__})

        for request in [
            {}, {'limit': 1}, {'offset': 2, 'limit': 2, 'sort': 'oldest'},
            {'view': 'favorites'}, {'view': 'videos'}, {'view': 'archive'},
            {'view': 'hidden', 'hidden_access': True}, {'view': 'hidden', 'hidden_access': False},
            {'view': 'trash'}, {'album': 'Family'}, {'album': 'Smart family'},
            {'search': 'year:2021 -tag:work'}, {'search': 'name:"Café"'},
            {'search': 'is:favorite has:tags'}, {'search': 'caption:"birthday cake"'},
            {'search': 'album:"Family" type:photo'}, {'search': 'on:1900-07-02'},
            {'search': 'from:1969-07-03 to:1970-07-04'}, {'search': "name:';DROP TABLE photos;--"},
            {'search': 'no-match'}, {'view': 'large'}, {'view': 'large', 'large_type': 'videos'},
            {'view': 'large', 'large_type': 'photos', 'offset': 1, 'limit': 2},
            {'view': 'similar', 'similar_id': identities[5], 'limit': 3},
            {'view': 'similar_groups', 'limit': 3},
            {'view': 'lane', 'person': person}, {'view': 'lane', 'person': person, 'lane_year': 2021, 'limit': 2},
            {'view': 'memories', 'memory_date': '2025-07-03'},
            {'jump_day': '2021-07-03', 'limit': 2}, {'jump_day': '2021-07-03', 'sort': 'oldest', 'limit': 2},
            {'view': 'albums', 'search': 'café'}, {'album_mode': 'all', 'album_sort': 'newest'},
            {'view': 'people', 'person': person}, {'view': 'people'}, {'view': 'auto_people'},
            {'view': 'map', 'map_gallery': True}, {'view': 'map'},
        ]:
            case('library', request)
        for request in [
            {'view': 'photos', 'sort': 'newest'},
            {'view': 'photos', 'sort': 'oldest', 'offset': 2, 'limit': 3},
            {'view': 'large', 'sort': 'oldest', 'large_type': 'all'},
            {'view': 'similar', 'sort': 'newest', 'similar_id': identities[5]},
            {'view': 'validate', 'sort': 'newest', 'search': 'year:2024'},
            {'view': 'photos', 'sort': 'wrong'}, {'view': 'photos', 'sort': 'newest', 'search': 'type:unknown'},
            {'view': 'large', 'sort': 'newest', 'large_type': 'wrong'},
            {'view': 'similar', 'sort': 'newest', 'similar_id': 'invalid'},
            {'view': 'photos', 'sort': 'newest', 'map_bounds': None},
        ]:
            case('page', request)
        for request in ['', ' ', '\u2003\n', '\0', '  year:2021  ', 'type:unknown', 'year:2021\0', 'x' * 513]:
            case('validate', request)
        for request in [
            {}, {'gallery_format': 2}, {'view': 'large', 'gallery_format': 2},
            {'view': 'similar', 'similar_id': identities[5], 'gallery_format': 2},
            {'view': 'hidden', 'hidden_access': 1, 'gallery_format': 2},
            {'view': 'people', 'gallery_format': 2}, {'view': 'lane', 'person': person, 'gallery_format': 2},
        ]:
            case('ui', request)
        for request in [
            {'view': 'large', 'large_type': 'wrong'}, {'sort': 'wrong'}, {'search': None},
            {'offset': None}, {'offset': '2', 'limit': '1'}, {'limit': 0},
            {'offset': '+2', 'limit': '02'}, {'offset': ' 002 ', 'limit': '+0001'},
            {'offset': '\u2003٢\u2003', 'limit': '１'}, {'offset': '0_2', 'limit': '0_1'},
            {'offset': 2.9, 'limit': 1.9}, {'offset': -2.9, 'limit': True},
            {'offset': False, 'limit': False}, {'offset': 2**70, 'limit': 2**70},
            {'offset': str(2**70), 'limit': str(-(2**70))},
            {'offset': 1e100, 'limit': 2.9},
            {'offset': '1' * 4301}, {'limit': '0' * 4301},
            {'offset': '2.0'}, {'offset': '2__0'}, {'offset': []}, {'limit': None},
            {'offset': -5, 'limit': 200}, {'jump_day': '2021-02-29'},
            {'view': 'similar_groups', 'jump_day': '2021-07-01'},
        ]:
            case('library', request)
        dump = '\n'.join(library.db.iterdump())
        library.db.close()
        output = {'sql': dump, 'files': files, 'cases': cases}
        path = Path('tests/fixtures/golden/gallery-baseline.json')
        path.write_text(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
        print(f'Captured {len(cases)} synthetic gallery cases in {path}')


if __name__ == '__main__':
    capture()
