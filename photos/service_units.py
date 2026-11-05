"""Photos processes follow the lifetime of the on-demand local library."""
import ipaddress
from pathlib import Path


def argument(value):
    value = str(value)
    if any(character in value for character in '\0\n\r'):
        raise ValueError('Invalid Photos service path')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def shared_node(value):
    """The desktop Harmony node's private address, as host:port."""
    host, separator, port = str(value).rpartition(':')
    try:
        address = ipaddress.ip_address(host)
        port = int(port)
    except ValueError:
        raise ValueError('Give the desktop node as a private IPv4 address and port') from None
    if not separator or address.version != 4 or not address.is_private or not 0 < port < 65536:
        raise ValueError('Give the desktop node as a private IPv4 address and port')
    return f'{address}:{port}'


def units(installed, *, tunnel=None, tunnel_present=False, defer_sync=False, proxy=None):
    installed = Path(installed)
    python = argument(installed / 'venv/bin/python')
    sync = '' if defer_sync else 'Wants=photos-sync.service\n'
    has_tunnel = tunnel is not None or tunnel_present
    after = ' photos-tunnel.service' if has_tunnel else ''
    wants = ['photos-tunnel.service'] if has_tunnel else []
    if proxy:
        after += ' photos-node.socket'
        wants.append('photos-node.socket')
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
Description=Photos local import and private-node encrypted sync
BindsTo=photos.service
After=network-online.target photos.service{after}
{('Wants=' + ' '.join(wants)) if wants else ''}

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
    if proxy:
        # The account's shared photos live on the desktop Harmony node, which
        # listens on the LAN; Photos still only talks to loopback.
        port, target = proxy
        if type(port) is not int or not 0 < port < 65536:
            raise ValueError('Invalid Photos loopback port')
        target = shared_node(target)
        result['photos-node.socket'] = f'''[Unit]
Description=Photos loopback to the desktop Harmony node
BindsTo=photos-sync.service

[Socket]
ListenStream=127.0.0.1:{port}
'''
        result['photos-node.service'] = f'''[Unit]
Description=Photos loopback proxy to the desktop Harmony node
Requires=photos-node.socket
After=photos-node.socket

[Service]
Type=notify
ExecStart=/usr/lib/systemd/systemd-socket-proxyd --exit-idle-time=60s {target}
PrivateTmp=true
NoNewPrivileges=true
'''
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
