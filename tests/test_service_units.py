"""Verify the activation/dependency graph with systemd's actual unit parser."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from photos.service_units import shared_node, units


class ServiceUnitsTest(unittest.TestCase):
    def test_parser_accepts_on_demand_units_and_workers_do_not_start_at_login(self):
        with tempfile.TemporaryDirectory(prefix='photos-units-') as temporary:
            root = Path(temporary)
            installed = root / 'Photos % cache'
            python = installed / 'venv/bin/python'
            python.parent.mkdir(parents=True)
            python.symlink_to(Path(sys.executable).resolve())
            definitions = units(installed, tunnel=('owner@thinkpad.invalid', 18081), proxy=(18087, '192.168.100.194:18081'))
            for name, content in definitions.items():
                (root / name).write_text(content)
            environment = {key:value for key,value in os.environ.items() if key not in
                           {'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS'}}
            environment['SYSTEMD_UNIT_PATH'] = str(root) + ':/usr/lib/systemd/user'
            result = subprocess.run(['systemd-analyze', '--user', 'verify', '--man=no',
                                     *map(str, root.glob('*.socket')), *map(str, root.glob('*.service'))],
                                    env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('Unknown', result.stderr)
            self.assertNotIn('ignoring', result.stderr)
            self.assertNotIn('[Install]', definitions['photos.service'])
            self.assertNotIn('[Install]', definitions['photos-sync.service'])
            self.assertNotIn('[Install]', definitions['photos-tunnel.service'])
            self.assertIn('BindsTo=photos.service', definitions['photos-sync.service'])
            self.assertIn('BindsTo=photos-sync.service', definitions['photos-tunnel.service'])
            self.assertIn('Wants=photos-tunnel.service photos-node.socket', definitions['photos-sync.service'])
            self.assertIn('ListenStream=127.0.0.1:18087', definitions['photos-node.socket'])
            self.assertIn('BindsTo=photos-sync.service', definitions['photos-node.socket'])
            self.assertTrue(definitions['photos-node.service'].split('ExecStart=')[1].startswith(
                '/usr/lib/systemd/systemd-socket-proxyd --exit-idle-time=60s 192.168.100.194:18081\n'))

    def test_existing_tunnel_keeps_its_command_and_only_receives_lifetime_dependency(self):
        definitions = units(Path('/private/Photos'), tunnel_present=True)
        self.assertNotIn('photos-tunnel.service', definitions)
        self.assertIn('Wants=photos-tunnel.service', definitions['photos-sync.service'])
        self.assertIn('BindsTo=photos-sync.service', definitions['photos-tunnel.service.d/lifetime.conf'])
        self.assertNotIn('ExecStart', definitions['photos-tunnel.service.d/lifetime.conf'])

    def test_desktop_node_is_only_a_private_address(self):
        self.assertEqual(shared_node('192.168.100.194:18081'), '192.168.100.194:18081')
        for value in ('api.waozi.xyz:443', '8.8.8.8:18081', '192.168.100.194', '192.168.100.194:0',
                      '[fd00::1]:18081', '192.168.100.194:18081\nExecStart=/bin/false'):
            with self.assertRaises(ValueError):
                shared_node(value)
            with self.assertRaises(ValueError):
                units(Path('/private/Photos'), proxy=(18087, value))

    def test_invalid_service_path_cannot_inject_unit_directives(self):
        for suffix in ('\nExecStart=/bin/false', '\r', '\0'):
            with self.assertRaises(ValueError):
                units(Path('/private/Photos' + suffix))


if __name__ == '__main__':
    unittest.main()
