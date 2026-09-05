"""Real SSH port forwarding to a local TCP echo service, no external servers."""
import asyncio
import importlib.util
import tempfile
import unittest
import threading
from contextlib import contextmanager
from pathlib import Path

from metology_worker.config import SSHConfig
from metology_worker.tunnel import SSHTunnel
from metology_worker.service import consume_queue


@unittest.skipUnless(importlib.util.find_spec('asyncssh'), 'Install asyncssh dependency')
class TunnelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncssh
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.client_key = asyncssh.generate_private_key('ssh-ed25519')
        self.host_key = asyncssh.generate_private_key('ssh-ed25519')
        self.key_path = root / 'key'
        self.key_path.write_bytes(self.client_key.export_private_key())
        self.connections = []

        async def echo(reader, writer):
            try:
                while data := await reader.read(1024):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        self.echo_server = await asyncio.start_server(echo, '127.0.0.1', 0)
        self.target_port = self.echo_server.sockets[0].getsockname()[1]
        owner = self

        class Server(asyncssh.SSHServer):
            def connection_made(self, connection):
                owner.connections.append(connection)
            def begin_auth(self, username):
                return True
            def public_key_auth_supported(self):
                return True
            def validate_public_key(self, username, key):
                return username == 'worker' and key == owner.client_key.convert_to_public()
            def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
                return dest_host == '127.0.0.1' and dest_port == owner.target_port

        self.server = await asyncssh.create_server(Server, '127.0.0.1', 0, server_host_keys=[self.host_key])
        self.known_hosts = root / 'known_hosts'
        self.known_hosts.write_bytes(
            f'[127.0.0.1]:{self.server.get_port()} '.encode() + self.host_key.export_public_key()
        )
        self.config = SSHConfig(enabled=True, host='127.0.0.1', port=self.server.get_port(),
                                username='worker', key_path=self.key_path, known_hosts=self.known_hosts,
                                local_port=0, remote_port=self.target_port)

    async def asyncTearDown(self):
        for connection in self.connections:
            connection.abort()
        self.server.close()
        self.echo_server.close()
        await self.server.wait_closed()
        await self.echo_server.wait_closed()

    async def exchange(self, port):
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        try:
            writer.write(b'database-traffic')
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(16), 3), b'database-traffic')
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_forwards_traffic_and_closes_owned_listener(self):
        tunnel = SSHTunnel(self.config)
        await asyncio.to_thread(tunnel.__enter__)
        try:
            port = tunnel.local_port
            await self.exchange(port)
        finally:
            await asyncio.to_thread(tunnel.__exit__, None, None, None)
        with self.assertRaises(OSError):
            await asyncio.open_connection('127.0.0.1', port)

    async def test_reconnect_preserves_local_port(self):
        tunnel = SSHTunnel(self.config)
        await asyncio.to_thread(tunnel.__enter__)
        try:
            port = tunnel.local_port
            self.connections[-1].abort()
            for _ in range(100):
                await asyncio.sleep(0.05)
                if len(self.connections) >= 2 and tunnel.ready.is_set():
                    break
            self.assertGreaterEqual(len(self.connections), 2)
            self.assertEqual(tunnel.local_port, port)
            await self.exchange(port)
        finally:
            await asyncio.to_thread(tunnel.__exit__, None, None, None)

    async def test_unknown_host_key_is_rejected(self):
        self.known_hosts.write_text('')
        tunnel = SSHTunnel(self.config)
        with self.assertRaisesRegex(RuntimeError, 'ssh_authentication_or_host_key_failed'):
            await asyncio.to_thread(tunnel.__enter__)

    async def test_permanent_reconnect_failure_exits_queue_instead_of_retrying_forever(self):
        tunnel = SSHTunnel(self.config)
        await asyncio.to_thread(tunnel.__enter__)
        try:
            self.known_hosts.write_text('')
            self.connections[-1].abort()
            for _ in range(100):
                if tunnel.failure:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(tunnel.failure, 'ssh_authentication_or_host_key_failed')
            with self.assertRaisesRegex(RuntimeError, 'ssh_authentication_or_host_key_failed'):
                tunnel.check_ready()

            @contextmanager
            def listen():
                tunnel.check_ready()
                yield None
            with self.assertRaisesRegex(RuntimeError, 'ssh_authentication_or_host_key_failed'):
                consume_queue(None, listen, 'worker', threading.Event(), lambda a: None)
        finally:
            await asyncio.to_thread(tunnel.__exit__, None, None, None)
