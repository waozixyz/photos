"""Photos processes follow the lifetime of the on-demand local library."""
from pathlib import Path


def argument(value):
    value = str(value)
    if any(character in value for character in '\0\n\r'):
        raise ValueError('Invalid Photos service path')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def units(installed, *, tunnel=None, tunnel_present=False, defer_sync=False):
    installed = Path(installed)
    python = argument(installed / 'venv/bin/python')
    sync = '' if defer_sync else 'Wants=photos-sync.service\n'
    has_tunnel = tunnel is not None or tunnel_present
    result = {
        'photos.socket': '''[Unit]
Description=Photos private on-demand library socket

[Socket]
ListenStream=%t/kryon/services/photos.sock
DirectoryMode=0700
SocketMode=0600
RemoveOnStop=true

[Install]
WantedBy=sockets.target
''',
        'photos.service': f'''[Unit]
Description=Photos private local library service
Requires=photos.socket
After=photos.socket
{sync}
[Service]
Type=simple
ExecStart={python} {argument(installed / 'service.py')}
Restart=on-failure
RestartSec=5
TimeoutStopSec=5
KillMode=control-group
UMask=0077
''',
        'photos-sync.service': f'''[Unit]
Description=Photos local import and ThinkPad-only encrypted sync
BindsTo=photos.service
After=network-online.target photos.service{' photos-tunnel.service' if has_tunnel else ''}
{'Wants=photos-tunnel.service' if has_tunnel else ''}

[Service]
Type=simple
ExecStart={python} {argument(installed / 'worker.py')} --daemon
Restart=on-failure
RestartSec=15
TimeoutStopSec=5
KillMode=control-group
UMask=0077
Nice=10
''',
    }
    if tunnel:
        host, port = tunnel
        result['photos-tunnel.service'] = f'''[Unit]
Description=Photos private SSH tunnel to ThinkPad Daochi
BindsTo=photos-sync.service
After=network-online.target photos.service

[Service]
Type=simple
ExecStart=/usr/bin/ssh -F /dev/null -N -T -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:{port}:127.0.0.1:18080 {argument(host)}
Restart=always
RestartSec=10
TimeoutStopSec=5
KillMode=control-group
'''
    elif has_tunnel:
        # Keep the owner's existing SSH command and host pin configuration.
        result['photos-tunnel.service.d/lifetime.conf'] = '''[Unit]
BindsTo=photos-sync.service
After=photos.service
'''
    return result
