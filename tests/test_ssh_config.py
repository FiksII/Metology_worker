import unittest
from unittest.mock import patch
from pathlib import Path

from metology_worker.config import Config


class SSHConfigTests(unittest.TestCase):
    def test_direct_connection_remains_default(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(Config.load().ssh.enabled)

    def test_ssh_configuration_and_passphrase_privacy(self):
        with patch.dict('os.environ', {
            'SSH_TUNNEL_ENABLED': '1', 'SSH_HOST': 'host', 'SSH_USER': 'worker',
            'SSH_KEY_PATH': '~/.ssh/id_ed25519', 'SSH_KEY_PASSPHRASE': 'private-passphrase',
            'USERPROFILE': str(Path.home()), 'HOME': str(Path.home()),
            'SSH_LOCAL_PORT': '0',
        }, clear=True):
            config = Config.load()
        self.assertTrue(config.ssh.enabled)
        self.assertEqual(config.ssh.local_port, 0)
        self.assertTrue(config.ssh.key_path.is_absolute())
        self.assertNotIn('private-passphrase', repr(config))

    def test_invalid_ports_are_rejected(self):
        for name, value in [('SSH_PORT', '0'), ('SSH_LOCAL_PORT', '-1'), ('SSH_REMOTE_PORT', '65536')]:
            with patch.dict('os.environ', {name: value}, clear=True):
                with self.assertRaises(ValueError):
                    Config.load()

    def test_unencrypted_hop_beyond_ssh_server_is_rejected(self):
        with patch.dict('os.environ', {
            'SSH_TUNNEL_ENABLED': '1', 'SSH_HOST': 'host', 'SSH_USER': 'worker',
            'SSH_KEY_PATH': str(Path.home() / '.ssh/id_ed25519'), 'SSH_REMOTE_HOST': 'database.internal',
        }, clear=True):
            with self.assertRaisesRegex(ValueError, 'ssh_remote_database_requires_tls'):
                Config.load()
