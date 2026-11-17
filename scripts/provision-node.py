#!/usr/bin/env python3
"""Register Photos only on an existing local-only Mesh node.

By default this is the ThinkPad's node, run on the ThinkPad. The desktop
Harmony node, which keeps the account's shared photos, is registered with
--origin and --env pointing at its address and environment file.
"""
import argparse
import json
from pathlib import Path
import shlex
import urllib.request
import urllib.error

parser=argparse.ArgumentParser()
parser.add_argument('--origin',default='http://127.0.0.1:18080')
parser.add_argument('--env',type=Path,default=Path.home()/'daochi-local/daochi.env')
args=parser.parse_args()
origin=args.origin.rstrip('/')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
node=json.load(opener.open(origin+'/api/v1/node'))
for peer in node.get('known_nodes',[]):
    policy=peer.get('sync') or {}
    if policy.get('direction') not in ('pull','none') and (not policy.get('apps') or {'photos','entity','*'} & set(policy['apps'])):
        raise SystemExit('Outward Photos replication must be disabled before registration')
try:
    app=json.load(opener.open(origin+'/api/v1/apps/photos'))
    if app.get('status')!='active':
        raise SystemExit('Existing Photos registration is inactive; refusing to overwrite')
except urllib.error.HTTPError as error:
    if error.code!=404:
        raise
    values={}
    for line in args.env.read_text().splitlines():
        key,separator,value=line.partition('=')
        if separator and key.strip() in ('MESH_ADMIN_TOKEN','DAOCHI_ADMIN_TOKEN'):
            parts=shlex.split(value)
            values[key.strip()]=parts[0] if parts else ''
    token=values.get('MESH_ADMIN_TOKEN',values.get('DAOCHI_ADMIN_TOKEN'))
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
