"""Verify the activation/dependency graph with systemd's actual unit parser."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from photos.service_units import units


class ServiceUnitsTest(unittest.TestCase):
    def test_parser_accepts_on_demand_units_and_workers_do_not_start_at_login(self):
        with tempfile.TemporaryDirectory(prefix='photos-units-') as temporary:
            root = Path(temporary)
            installed = root / 'Photos % cache'
            python = installed / 'venv/bin/python'
            python.parent.mkdir(parents=True)
            python.symlink_to(Path(sys.executable).resolve())
            definitions = units(installed, tunnel=('owner@thinkpad.invalid', 18081))
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

    def test_existing_tunnel_keeps_its_command_and_only_receives_lifetime_dependency(self):
        definitions = units(Path('/private/Photos'), tunnel_present=True)
        self.assertNotIn('photos-tunnel.service', definitions)
        self.assertIn('Wants=photos-tunnel.service', definitions['photos-sync.service'])
        self.assertIn('BindsTo=photos-sync.service', definitions['photos-tunnel.service.d/lifetime.conf'])
        self.assertNotIn('ExecStart', definitions['photos-tunnel.service.d/lifetime.conf'])

    def test_invalid_service_path_cannot_inject_unit_directives(self):
        for suffix in ('\nExecStart=/bin/false', '\r', '\0'):
            with self.assertRaises(ValueError):
                units(Path('/private/Photos' + suffix))


if __name__ == '__main__':
    unittest.main()
