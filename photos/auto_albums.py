"""Opt-in people rules for regular albums; never change original files."""
import json
import re
import time

from .library import LibraryError, packed

HASH = re.compile(r'[0-9a-f]{64}')


def export_memberships(library, photo):
    provenance = json.loads(photo['edits']).get('auto_albums',{})
    return {row['name']:identities(provenance[row['id']]) for row in library.db.execute('SELECT id,name FROM albums WHERE deleted=0')
            if row['id'] in provenance and row['name'] in json.loads(photo['albums'])}


def import_memberships(library, photo_id, value):
    if not isinstance(value,dict) or len(value)>128:
        raise LibraryError('Invalid automatic album memberships')
    parsed = {library.album_name(name):identities(selected) for name,selected in value.items()}
    photo = library.photo(photo_id); edits = json.loads(photo['edits'])
    for name, selected in parsed.items():
        if name in json.loads(photo['albums']) and name not in edits.get('removed_albums',[]):
            album = library._ensure_album(name)
            edits.setdefault('auto_albums',{})[album['id']]=selected
    library.db.execute('UPDATE photos SET edits=? WHERE id=?',(packed(edits),photo_id))


def identities(value):
    if not isinstance(value,list) or len(value)>64 or any(not isinstance(i,str) or not HASH.fullmatch(i) for i in value):
        raise LibraryError('Choose up to 64 people for this album')
    return sorted(set(value))


def validate_provenance(value):
    if not isinstance(value,dict) or len(value)>128 or any(not isinstance(i,str) or not HASH.fullmatch(i) for i in value):
        raise LibraryError('Invalid automatic album memberships')
    return {album:identities(selected) for album,selected in value.items()}


def configure(library, name, selected, remove=False):
    selected = identities(selected)
    if type(remove) is not bool:
        raise LibraryError('Choose whether to keep previously added photos')
    with library.db:
        library.db.execute('BEGIN IMMEDIATE')
        album = library.album_details(name)
        if album['filter']:
            raise LibraryError('Choose a regular album for automatic people additions')
        previous = identities(json.loads(album['auto_people']))
        from . import people
        for identity in set(selected)-set(previous):
            person = people.group(library,identity)
            if not library.setting('people_enabled',False) or not person['name'] or person['ignored'] or not any(
                people.member_visible(library,row[0]) for row in library.db.execute('SELECT photo_id FROM person_photos WHERE person_id=?',(identity,))):
                raise LibraryError('Enable People and choose a named, visible person')
        library.db.execute('UPDATE albums SET auto_people=?,updated=?,dirty=1 WHERE id=?',(packed(selected),time.time(),album['id']))
        if remove and set(previous)-set(selected):
            # Only remove memberships recorded as automatic. Imported and
            # deliberately added memberships have no such provenance.
            for row in library.db.execute("SELECT id,edits FROM photos WHERE json_type(edits,'$.auto_albums')='object'").fetchall():
                edits = json.loads(row['edits']); sources = edits['auto_albums'].get(album['id'])
                if isinstance(sources,list) and sources and not set(sources)&set(selected):
                    if not people.member_visible(library,row['id']):
                        continue
                    # A newly selected person may match the same photo. Keep
                    # it and let apply update its provenance to the new rule.
                    if selected:
                        placeholders=','.join('?' for _ in selected)
                        if library.db.execute(f'''SELECT 1 FROM person_photos f JOIN people g ON g.id=f.person_id
                            WHERE f.photo_id=? AND g.id IN ({placeholders}) AND g.ignored=0 AND g.name<>'' LIMIT 1''',
                            [row['id'],*selected]).fetchone():
                            continue
                    library._edit(row['id'],'album_remove',name)
        apply(library, album_id=album['id'])


def apply(library, photo_id=None, album_id=None):
    if not library.setting('people_enabled',False):
        return
    # Callers must hold the writer lock; a concurrent hide can never overtake
    # a match and cause a new automatic membership for private material.
    if not library.db.in_transaction:
        with library.db:
            library.db.execute('BEGIN IMMEDIATE')
            apply(library,photo_id,album_id)
        return
    sql = "SELECT * FROM albums WHERE deleted=0 AND filter='' AND auto_people<>'[]'"
    albums = library.db.execute(sql + (' AND id=?' if album_id else ''),(album_id,) if album_id else ()).fetchall()
    from . import people, live
    for album in albums:
        selected = identities(json.loads(album['auto_people']))
        if not selected:
            continue
        placeholders = ','.join('?' for _ in selected)
        sql = f'''SELECT DISTINCT p.id FROM photos p JOIN person_photos f ON f.photo_id=p.id
            JOIN people g ON g.id=f.person_id WHERE p.hidden=0 AND p.trashed=0 AND g.ignored=0
            AND g.name<>'' AND g.id IN ({placeholders})'''
        candidates = library.db.execute(sql + (' AND p.id=?' if photo_id else ''),[*selected,*([photo_id] if photo_id else [])]).fetchall()
        for row in candidates:
            if not people.member_visible(library,row[0]):
                continue
            photo = library.photo(row[0])
            matches = [r[0] for r in library.db.execute(f'''SELECT DISTINCT g.id FROM person_photos f JOIN people g ON g.id=f.person_id
                WHERE f.photo_id=? AND g.ignored=0 AND g.name<>'' AND g.id IN ({placeholders})''',[photo['id'],*selected])]
            components = live.components(library,photo)
            if any(c['hidden'] or c['trashed'] for c in components):
                continue
            for component in components:
                edits = json.loads(component['edits'])
                # Manual removal stays authoritative through rescans and rule
                # updates. Manual/source memberships remain manual.
                if album['name'] in edits.get('removed_albums',[]):
                    continue
                memberships = set(json.loads(component['albums']))
                provenance = edits.get('auto_albums',{})
                if album['name'] in memberships and album['id'] not in provenance:
                    continue
                previous = provenance.get(album['id'],[])
                if album['name'] in memberships and previous == sorted(matches):
                    continue
                memberships.add(album['name']); provenance[album['id']] = sorted(matches); edits['auto_albums']=provenance
                library.db.execute('UPDATE photos SET albums=?,edits=?,updated=?,dirty=1 WHERE id=?',
                                   (packed(sorted(memberships)),packed(edits),time.time(),component['id']))
