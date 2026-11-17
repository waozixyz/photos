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
from photos.service_units import shared_node, units
from urllib.parse import urlsplit
import re


def installed_proxy(unit_root):
    """The loopback port and desktop node address of an installed proxy, kept on reinstall."""
    socket, service = unit_root/'photos-node.socket', unit_root/'photos-node.service'
    if not socket.is_file() or not service.is_file():
        return None
    port = next(int(line.rpartition(':')[2]) for line in socket.read_text().splitlines() if line.startswith('ListenStream='))
    target = next(line.split()[-1] for line in service.read_text().splitlines() if line.startswith('ExecStart='))
    return port, target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prefix', type=Path, default=Path.home()/'.local')
    parser.add_argument('--library', type=Path, default=Path.home()/'.local/share/photos')
    parser.add_argument('--node')
    parser.add_argument('--node-id')
    parser.add_argument('--ssh-host', help='Owner SSH destination for the ThinkPad loopback tunnel')
    parser.add_argument('--shared-node', help="The desktop Harmony node's private address:port, which keeps the account's shared photos")
    parser.add_argument('--shared-node-id', help="The desktop Harmony node's pinned identity")
    parser.add_argument('--shared-port', type=int, default=18087, help='Loopback port of the proxy to the desktop node')
    parser.add_argument('--defer-sync', action='store_true', help='Install the worker but leave its start to a running import')
    parser.add_argument('--source', action='append', default=[])
    parser.add_argument('--no-dependencies', action='store_true')
    parser.add_argument('--no-service', action='store_true')
    parser.add_argument('--semantic-models', type=Path, help='Install verified local semantic models from this directory')
    parser.add_argument('--text-models', type=Path, help='Install verified offline text recognition languages from this directory')
    parser.add_argument('--face-models', type=Path, help='Install verified offline face recognition models from this directory')
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
    if args.shared_node:
        try:
            args.shared_node = shared_node(args.shared_node)
        except ValueError as error:
            parser.error(str(error))
        if not re.fullmatch('[0-9a-f]{64}', args.shared_node_id or '') or not 1024 <= args.shared_port < 65536:
            parser.error('Pin the desktop node identity and choose a loopback port for it')
    repo = Path(__file__).resolve().parents[1]
    if args.harmony:
        current = Path.home()/'.local/share/harmony/apps/photos/current.json'
        if not current.is_file() or json.loads(current.read_bytes()).get('app_id') != 'photos':
            parser.error('Install the signed Photos bundle through Harmony before registering its catalog entry')
    installed = args.prefix/'lib/photos'
    installed.mkdir(parents=True,exist_ok=True,mode=0o700)
    sessions = repo/'build/access-sessions/access_sessions'
    if not sessions.is_file():
        parser.error('Build native Photos access sessions before installing')
    shutil.copyfile(sessions,installed/'access_sessions.next')
    os.chmod(installed/'access_sessions.next',0o755)
    os.replace(installed/'access_sessions.next',installed/'access_sessions')
    service_broker = repo/'build/photos-service/photos_service'
    if not service_broker.is_file():
        parser.error('Build the native Photos local service before installing')
    shutil.copyfile(service_broker,installed/'photos_service.next')
    os.chmod(installed/'photos_service.next',0o755)
    os.replace(installed/'photos_service.next',installed/'photos_service')
    cryptography = repo/'build/sync-crypto/sync_crypto'
    if not cryptography.is_file():
        parser.error('Build native Photos encryption before installing')
    shutil.copyfile(cryptography,installed/'sync_crypto.next')
    os.chmod(installed/'sync_crypto.next',0o755)
    os.replace(installed/'sync_crypto.next',installed/'sync_crypto')
    account_signer = repo/'build/account-sign/account_sign'
    if not account_signer.is_file():
        parser.error('Build native Photos account signing before installing')
    shutil.copyfile(account_signer,installed/'account_sign.next')
    os.chmod(installed/'account_sign.next',0o755)
    os.replace(installed/'account_sign.next',installed/'account_sign')
    transaction = repo/'build/sync-transaction/sync_transaction'
    if not transaction.is_file():
        parser.error('Build native Mesh request signing before installing')
    shutil.copyfile(transaction,installed/'sync_transaction.next')
    os.chmod(installed/'sync_transaction.next',0o755)
    os.replace(installed/'sync_transaction.next',installed/'sync_transaction')
    transport = repo/'build/sync-http/sync_http'
    if not transport.is_file():
        parser.error('Build native Photos loopback transport before installing')
    shutil.copyfile(transport,installed/'sync_http.next')
    os.chmod(installed/'sync_http.next',0o755)
    os.replace(installed/'sync_http.next',installed/'sync_http')
    pages = repo/'build/sync-page/sync_page'
    if not pages.is_file():
        parser.error('Build native Photos sync pages before installing')
    shutil.copyfile(pages,installed/'sync_page.next')
    os.chmod(installed/'sync_page.next',0o755)
    os.replace(installed/'sync_page.next',installed/'sync_page')
    recovery = repo/'build/recovery-crypto/recovery_crypto'
    if not recovery.is_file():
        parser.error('Build native Photos recovery before installing')
    shutil.copyfile(recovery,installed/'recovery_crypto.next')
    os.chmod(installed/'recovery_crypto.next',0o755)
    os.replace(installed/'recovery_crypto.next',installed/'recovery_crypto')
    metadata_reader = repo/'build/read-metadata/read_metadata'
    if not metadata_reader.is_file():
        parser.error('Build Photos metadata reading before installing')
    shutil.copyfile(metadata_reader,installed/'read_metadata.next')
    os.chmod(installed/'read_metadata.next',0o755)
    os.replace(installed/'read_metadata.next',installed/'read_metadata')
    folder_importer = repo/'build/import-folders/import_folders'
    if not folder_importer.is_file():
        parser.error('Build Photos folder discovery before installing')
    shutil.copyfile(folder_importer,installed/'import_folders.next')
    os.chmod(installed/'import_folders.next',0o755)
    os.replace(installed/'import_folders.next',installed/'import_folders')
    metadata_importer = repo/'build/import-metadata/import_metadata'
    if not metadata_importer.is_file():
        parser.error('Build the Photos metadata importer before installing')
    shutil.copyfile(metadata_importer,installed/'import_metadata.next')
    os.chmod(installed/'import_metadata.next',0o755)
    os.replace(installed/'import_metadata.next',installed/'import_metadata')
    importer = repo/'build/import-original/import_original'
    if not importer.is_file():
        parser.error('Build the Photos original importer before installing')
    shutil.copyfile(importer,installed/'import_original.next')
    os.chmod(installed/'import_original.next',0o755)
    os.replace(installed/'import_original.next',installed/'import_original')
    for name in ['face-infer','face-pixels','face-match']:
        target = name.replace('-','_'); source = repo/'build'/name/target
        if not source.is_file(): parser.error('Build the local face recognition engine before installing')
        shutil.copyfile(source,installed/(target+'.next'))
        os.chmod(installed/(target+'.next'),0o755)
        os.replace(installed/(target+'.next'),installed/target)
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
    editor = repo/'build/library-editor/library_edit'
    if not editor.is_file():
        parser.error('Build the Photos property editor before installing')
    shutil.copyfile(editor,installed/'library_edit.next')
    os.chmod(installed/'library_edit.next',0o755)
    os.replace(installed/'library_edit.next',installed/'library_edit')
    video = repo/'build/video-editor/video_edit'
    if not video.is_file():
        parser.error('Build the Photos video editor before installing')
    decoder = repo/'build/photo-decoder/photo_decode'
    if not decoder.is_file():
        parser.error('Build the Photos HEIC/RAW decoder before installing')
    tensor = repo/'build/clip-image-tensor/clip_image_tensor'
    if not tensor.is_file():
        parser.error('Build the Photos image tensor helper before installing')
    semantic = repo/'build/semantic-infer/semantic_infer'
    if not semantic.is_file():
        parser.error('Build the Photos semantic search engine before installing')
    text = repo/'build/detect-text/detect_text'
    if not text.is_file():
        parser.error('Build the Photos text recognition engine before installing')
    qr = repo/'build/detect-qr/detect_qr'
    if not qr.is_file():
        parser.error('Build the Photos QR recognition engine before installing')
    motion = repo/'build/motion-photo/motion_photo'
    if not motion.is_file():
        parser.error('Build the Photos Motion Photo reader before installing')
    shutil.copyfile(motion,installed/'motion_photo.next')
    os.chmod(installed/'motion_photo.next',0o755)
    os.replace(installed/'motion_photo.next',installed/'motion_photo')
    shutil.copyfile(qr,installed/'detect_qr.next')
    os.chmod(installed/'detect_qr.next',0o755)
    os.replace(installed/'detect_qr.next',installed/'detect_qr')
    shutil.copyfile(text,installed/'detect_text.next')
    os.chmod(installed/'detect_text.next',0o755)
    os.replace(installed/'detect_text.next',installed/'detect_text')
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
    shutil.copyfile(decoder,installed/'photo_decode.next')
    os.chmod(installed/'photo_decode.next',0o755)
    os.replace(installed/'photo_decode.next',installed/'photo_decode')
    shutil.copyfile(tensor,installed/'clip_image_tensor.next')
    os.chmod(installed/'clip_image_tensor.next',0o755)
    os.replace(installed/'clip_image_tensor.next',installed/'clip_image_tensor')
    shutil.copyfile(semantic,installed/'semantic_infer.next')
    os.chmod(installed/'semantic_infer.next',0o755)
    os.replace(installed/'semantic_infer.next',installed/'semantic_infer')
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
    if args.shared_node:
        # Photos moves to the desktop node; the ThinkPad keeps the copies made
        # before entity data, which Photos only reads while moving them.
        shared = f'http://127.0.0.1:{args.shared_port}'
        if config.get('node_id') not in (None, args.shared_node_id) and not config.get('legacy_node'):
            config.update(legacy_node=config['node'], legacy_node_id=config['node_id'])
        if config.get('legacy_node') == shared:
            parser.error('The ThinkPad loopback port is taken by the desktop node; settings were preserved')
        config.update(node=shared, node_id=args.shared_node_id)
    atomic(config_path,packed(config).encode())
    launcher = args.prefix/'bin/photos'
    launcher.parent.mkdir(parents=True,exist_ok=True)
    atomic(launcher,('#!/bin/sh\nexec "'+str(installed/'photos-desktop')+'" "$@"\n').encode())
    launcher.chmod(0o755)
    desktop = args.prefix/'share/applications/photos.desktop'
    atomic(desktop,('[Desktop Entry]\nType=Application\nName=Photos\nComment=Private local photos synced with ThinkPad\nExec='+str(launcher)+'\nIcon=photos\nTerminal=false\nCategories=Graphics;Photography;\n').encode())
    icon = args.prefix/'share/icons/hicolor/256x256/apps/photos.png'
    icon.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(repo/'assets/photos.png',icon)
    (installed/'assets').mkdir(exist_ok=True)
    for name in ['world.png', 'MAP-LICENSE.txt', 'SEMANTIC-MODELS.json', 'CLIP-LICENSE.txt', 'TEXT-MODELS.json', 'TESSERACT-LICENSE.txt','FACE-MODELS.json','SFACE-LICENSE.txt','YUNET-LICENSE.txt','OQS-LICENSE.txt','LIBOQS-LICENSE.txt']:
        shutil.copyfile(repo/'assets'/name, installed/'assets'/name)
    if args.semantic_models:
        subprocess.run(['python3',str(repo/'scripts/semantic-models.py'),'--source',str(args.semantic_models),
                        '--destination',str(installed/'models')],check=True)
    if args.text_models:
        subprocess.run(['python3',str(repo/'scripts/text-models.py'),'--source',str(args.text_models),
                        '--destination',str(installed/'models/ocr')],check=True)
    if args.face_models:
        subprocess.run(['python3',str(repo/'scripts/face-models.py'),'--source',str(args.face_models),
                        '--destination',str(installed/'models/face')],check=True)
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
        if descriptor.get('icon_path') == str(args.prefix/'share/icons/hicolor/scalable/apps/photos.svg'):
            descriptor['icon_path'] = str(icon)
        atomic(catalog_path, packed(catalog).encode())
    if not args.no_service and not args.defer_sync:
        tunnel = (args.ssh_host, urlsplit(args.node).port) if args.ssh_host else None
        unit_root = Path.home()/'.config/systemd/user'
        has_tunnel = tunnel is not None or (unit_root/'photos-tunnel.service').is_file()
        proxy = (args.shared_port, args.shared_node) if args.shared_node else installed_proxy(unit_root)
        has_proxy = proxy is not None
        for name, content in units(installed, tunnel=tunnel, tunnel_present=has_tunnel, proxy=proxy).items():
            atomic(unit_root/name, content.encode())
        workers = ['photos.service', 'photos-sync.service']
        if has_tunnel:
            workers.append('photos-tunnel.service')
        if has_proxy:
            workers += ['photos-node.socket', 'photos-node.service']
        # Only the socket starts at login. Requests activate the library and
        # its scoped workers; a clean idle exit releases their entire groups.
        subprocess.run(['systemctl','--user','disable',*workers],check=True)
        subprocess.run(['systemctl','--user','stop',*workers],check=True)
        subprocess.run(['systemctl','--user','daemon-reload'],check=True)
        subprocess.run(['systemctl','--user','enable','photos.socket'],check=True)
        subprocess.run(['systemctl','--user','restart','photos.socket'],check=True)
    elif args.defer_sync and not args.no_service:
        print('Service activation migration deferred; the running import and current services were preserved')
    print('Photos standalone app and private local service installed; library and keys retained')
    print('Publish build/photos-portable.zx with Harmony scripts/publish-app.py for the Apps Install action')


if __name__=='__main__':
    main()
