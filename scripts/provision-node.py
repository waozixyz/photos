#!/usr/bin/env python3
"""Register Photos only on the ThinkPad's existing local Daochi node."""
import json
from pathlib import Path
import shlex
import urllib.request
import urllib.error

origin='http://127.0.0.1:18080'
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
node=json.load(opener.open(origin+'/api/v1/node'))
for peer in node.get('known_nodes',[]):
    policy=peer.get('sync') or {}
    if policy.get('direction')!='pull' and (not policy.get('apps') or 'photos' in policy['apps'] or '*' in policy['apps']):
        raise SystemExit('Outward Photos replication must be disabled before registration')
try:
    app=json.load(opener.open(origin+'/api/v1/apps/photos'))
    if app.get('status')!='active':
        raise SystemExit('Existing Photos registration is inactive; refusing to overwrite')
except urllib.error.HTTPError as error:
    if error.code!=404:
        raise
    values={}
    for line in (Path.home()/'daochi-local/daochi.env').read_text().splitlines():
        key,separator,value=line.partition('=')
        if separator and key.strip()=='DAOCHI_ADMIN_TOKEN':
            parts=shlex.split(value)
            values[key.strip()]=parts[0] if parts else ''
    token=values.get('DAOCHI_ADMIN_TOKEN')
    token_path = Path.home()/'daochi-local/admin-token'
    if not token and token_path.is_file():
        token = token_path.read_text().strip()
    if not token:
        raise SystemExit('Local node operator authorization is unavailable')
    app=dict(app_id='photos',display_name='Photos',status='active',app_schema_version=1,
             description='Private local-only Photos library',
             collections=[dict(collection_prefix='private.photos.v1.records.*',visibility='private',schema_version=1)],
             capabilities=['photos.local-sync','photos.encrypted-originals'])
    request=urllib.request.Request(origin+'/api/v1/apps',data=json.dumps(app,separators=(',',':')).encode(),method='POST',
                                   headers={'Content-Type':'application/json','X-Daochi-Admin':token})
    app=json.load(opener.open(request))
print(json.dumps(dict(node_id=node['node_id'],app_id=app['app_id'],local_only=True)))
