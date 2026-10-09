#!/usr/bin/env python3
"""Install Photos and its local service, retaining the private library and keys."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from photos.library import atomic, packed
from urllib.parse import urlsplit
import re


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prefix', type=Path, default=Path.home()/'.local')
    parser.add_argument('--library', type=Path, default=Path.home()/'.local/share/photos')
    parser.add_argument('--node')
    parser.add_argument('--node-id')
    parser.add_argument('--ssh-host', help='Owner SSH destination for the ThinkPad loopback tunnel')
    parser.add_argument('--defer-sync', action='store_true', help='Install the worker but leave its start to a running import')
    parser.add_argument('--source', action='append', default=[])
    parser.add_argument('--no-dependencies', action='store_true')
    parser.add_argument('--no-service', action='store_true')
    parser.add_argument('--harmony', action='store_true', help='Register an already installed signed Photos package in Harmony')
    args = parser.parse_args()
    if args.ssh_host and (not re.fullmatch('[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+', args.ssh_host) or not args.node):
        parser.error('Supply a literal owner@thinkpad SSH destination and loopback node URL')
    if args.node:
        origin = urlsplit(args.node)
        if origin.scheme != 'http' or origin.hostname != '127.0.0.1' or not origin.port or origin.path or origin.query or origin.fragment or origin.username:
            parser.error('Use the SSH loopback tunnel to the ThinkPad node')
        if not re.fullmatch('[0-9a-f]{64}', args.node_id or ''):
            parser.error('Pin the ThinkPad node identity when enabling sync')
    repo = Path(__file__).resolve().parents[1]
    if args.harmony:
        current = Path.home()/'.local/share/harmony/apps/photos/current.json'
        if not current.is_file() or json.loads(current.read_bytes()).get('app_id') != 'photos':
            parser.error('Install the signed Photos bundle through Harmony before registering its catalog entry')
    installed = args.prefix/'lib/photos'
    installed.mkdir(parents=True,exist_ok=True,mode=0o700)
    binary = repo/'build/photos-desktop'
    if not binary.is_file():
        parser.error('Build Photos before installing')
    processor = repo/'build/image-editor/image_edit'
    if not processor.is_file():
        parser.error('Build the Photos image processor before installing')
    authenticator = repo/'build/vault-auth/vault_auth'
    if not authenticator.is_file():
        parser.error('Build the Photos authentication helper before installing')
    gallery = repo/'build/library-query/library_query'
    if not gallery.is_file():
        parser.error('Build the Photos gallery engine before installing')
    video = repo/'build/video-editor/video_edit'
    if not video.is_file():
        parser.error('Build the Photos video editor before installing')
    shutil.copyfile(binary,installed/'photos-desktop.next')
    os.chmod(installed/'photos-desktop.next',0o755)
    os.replace(installed/'photos-desktop.next',installed/'photos-desktop')
    shutil.copyfile(processor,installed/'image_edit.next')
    os.chmod(installed/'image_edit.next',0o755)
    os.replace(installed/'image_edit.next',installed/'image_edit')
    shutil.copyfile(authenticator,installed/'vault_auth.next')
    os.chmod(installed/'vault_auth.next',0o755)
    os.replace(installed/'vault_auth.next',installed/'vault_auth')
    shutil.copyfile(gallery,installed/'library_query.next')
    os.chmod(installed/'library_query.next',0o755)
    os.replace(installed/'library_query.next',installed/'library_query')
    shutil.copyfile(video,installed/'video_edit.next')
    os.chmod(installed/'video_edit.next',0o755)
    os.replace(installed/'video_edit.next',installed/'video_edit')
    shutil.copyfile(repo/'scripts/worker.py',installed/'worker.py')
    shutil.copyfile(repo/'scripts/service.py',installed/'service.py')
    shutil.copytree(repo/'photos',installed/'photos',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
    if not args.no_dependencies:
        if not (installed/'venv/bin/python').exists():
            venv.EnvBuilder(with_pip=True).create(installed/'venv')
        subprocess.run([str(installed/'venv/bin/python'),'-m','pip','install','--no-cache-dir','-r',str(repo/'requirements.txt')],check=True)
    config_path = Path.home()/'.config/photos/config.json'
    config = json.loads(config_path.read_bytes()) if config_path.exists() else {}
    config.setdefault('root',str(args.library.resolve()))
    config['sources'] = list(dict.fromkeys([*config.get('sources', []), *args.source]))
    if args.node:
        if config.get('node_id') and config['node_id'] != args.node_id:
            parser.error('Existing ThinkPad identity differs; settings were preserved')
        config.update(node=args.node,node_id=args.node_id)
    atomic(config_path,packed(config).encode())
    launcher = args.prefix/'bin/photos'
    launcher.parent.mkdir(parents=True,exist_ok=True)
    atomic(launcher,('#!/bin/sh\nexec "'+str(installed/'photos-desktop')+'" "$@"\n').encode())
    launcher.chmod(0o755)
    desktop = args.prefix/'share/applications/photos.desktop'
    atomic(desktop,('[Desktop Entry]\nType=Application\nName=Photos\nComment=Private local photos synced with ThinkPad\nExec='+str(launcher)+'\nIcon=photos\nTerminal=false\nCategories=Graphics;Photography;\n').encode())
    icon = args.prefix/'share/icons/hicolor/scalable/apps/photos.svg'
    icon.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(repo/'assets/photos.svg',icon)
    if args.harmony:
        catalog_path = Path.home()/'.config/harmony/apps.json'
        catalog = json.loads(catalog_path.read_bytes()) if catalog_path.is_file() else {}
        entries = catalog.setdefault('apps', [])
        descriptor = next((entry for entry in entries if entry.get('app_id') == 'photos'), None)
        if descriptor is None:
            descriptor = dict(app_id='photos')
            entries.append(descriptor)
        for key, value in dict(label='Photos', icon='▧', description='Private local photos synced with ThinkPad',
                               renderer='window', registry_id='photos', icon_path=str(icon)).items():
            descriptor.setdefault(key, value)
        atomic(catalog_path, packed(catalog).encode())
    if not args.no_service:
        if args.ssh_host:
            service = Path.home()/'.config/systemd/user/photos-tunnel.service'
            atomic(service,('[Unit]\nDescription=Photos private SSH tunnel to ThinkPad Daochi\nAfter=network-online.target\n\n[Service]\nType=simple\nExecStart=/usr/bin/ssh -F /dev/null -N -T -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:'+str(urlsplit(args.node).port)+':127.0.0.1:18080 '+args.ssh_host+'\nRestart=always\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n').encode())
        service = Path.home()/'.config/systemd/user/photos.service'
        atomic(service,('[Unit]\nDescription=Photos private local library service\n\n[Service]\nType=simple\nExecStart='+str(installed/'venv/bin/python')+' '+str(installed/'service.py')+'\nRestart=on-failure\nRestartSec=5\nUMask=0077\n\n[Install]\nWantedBy=default.target\n').encode())
        service = Path.home()/'.config/systemd/user/photos-sync.service'
        atomic(service,('[Unit]\nDescription=Photos local import and ThinkPad-only encrypted sync\nAfter=network-online.target photos-tunnel.service\n\n[Service]\nType=simple\nExecStart='+str(installed/'venv/bin/python')+' '+str(installed/'worker.py')+' --daemon\nRestart=on-failure\nRestartSec=15\nUMask=0077\nNice=10\n\n[Install]\nWantedBy=default.target\n').encode())
        subprocess.run(['systemctl','--user','daemon-reload'],check=True)
        active = ['photos.service'] + (['photos-tunnel.service'] if args.ssh_host else [])
        subprocess.run(['systemctl','--user','enable','photos.service','photos-sync.service',*(['photos-tunnel.service'] if args.ssh_host else [])],check=True)
        if not args.defer_sync:
            active.append('photos-sync.service')
        subprocess.run(['systemctl','--user','restart',*active],check=True)
    print('Photos standalone app and private local service installed; library and keys retained')
    print('Publish build/photos-portable.zib with Harmony scripts/publish-app.py for the Apps Install action')


if __name__=='__main__':
    main()
