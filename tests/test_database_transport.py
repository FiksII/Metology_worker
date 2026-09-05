import importlib.util
import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

from metology_worker.config import Config, SSHConfig
from metology_worker.service import connection_options
from metology_worker.tunnel import database_transport, tunneled_config


@unittest.skipUnless(importlib.util.find_spec('psycopg'), 'Install worker dependencies')
class DatabaseTransportTests(unittest.TestCase):
    def config(self, mode='disable'):
        with patch.dict('os.environ', {}, clear=True):
            base = Config.load()
        ssh = SSHConfig(enabled=True, host='ssh.example', username='worker',
                        key_path=base.facelift_path / 'unused-key', database_sslmode=mode)
        return replace(base, ssh=ssh, database_url='postgresql://worker:p%40ss@db.example:5432/metology?hostaddr=192.0.2.1')

    def test_tunnel_overrides_socket_address_and_preserves_database_credentials(self):
        from psycopg.conninfo import conninfo_to_dict
        config = tunneled_config(self.config(), 32123)
        values = conninfo_to_dict(config.database_url)
        self.assertEqual(values['hostaddr'], '127.0.0.1')
        self.assertEqual(values['port'], '32123')
        self.assertEqual(values['dbname'], 'metology')
        self.assertEqual(values['user'], 'worker')
        self.assertEqual(values['password'], 'p@ss')
        self.assertEqual(connection_options(config)['sslmode'], 'disable')
        self.assertFalse(config.insecure)  # S3 still requires HTTPS.

    def test_tls_keeps_logical_database_hostname_for_certificate_check(self):
        from psycopg.conninfo import conninfo_to_dict
        config = tunneled_config(self.config('verify-full'), 32123)
        self.assertEqual(conninfo_to_dict(config.database_url)['host'], 'db.example')
        self.assertEqual(connection_options(config)['sslmode'], 'verify-full')

    def test_disabled_transport_never_opens_ssh(self):
        with patch.dict('os.environ', {}, clear=True):
            config = Config.load()
        with patch('metology_worker.tunnel.SSHTunnel') as ssh:
            with database_transport(config) as (actual, ready):
                self.assertIs(actual, config)
                ready()
            ssh.assert_not_called()

    def test_check_db_closes_transport_on_protocol_error_without_loading_gpu(self):
        from metology_worker.__main__ import check_database
        state = []
        @contextmanager
        def transport(config):
            state.append('open')
            try:
                yield config, lambda: None
            finally:
                state.append('close')
        with patch('metology_worker.tunnel.database_transport', transport):
            with patch('metology_worker.database.create_database') as create:
                create.return_value.check_version.side_effect = RuntimeError('unsupported_worker_protocol')
                with self.assertRaisesRegex(RuntimeError, 'unsupported_worker_protocol'):
                    check_database(self.config())
        self.assertEqual(state, ['open', 'close'])
