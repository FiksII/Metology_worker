"""An owned AsyncSSH connection and local forwarder in a dedicated event loop."""
import asyncio
import errno
import logging
import threading
from contextlib import contextmanager
from dataclasses import replace

LOG = logging.getLogger(__name__)


class SSHTunnel:
    def __init__(self, config):
        self.config = config
        self.local_port = config.local_port
        self.ready = threading.Event()
        self.started = threading.Event()
        self.failure = None
        self.loop = self.task = self.thread = None

    def check_ready(self):
        if self.failure:
            raise RuntimeError(self.failure)
        if not self.ready.is_set():
            raise ConnectionError('ssh_tunnel_unavailable')

    def __enter__(self):
        if not self.config.key_path or not self.config.key_path.is_file():
            raise ValueError('ssh_private_key_missing')
        if not self.config.known_hosts.is_file():
            raise ValueError('ssh_known_hosts_missing')
        self.thread = threading.Thread(target=self._thread_main, name='ssh-tunnel', daemon=True)
        self.thread.start()
        if not self.started.wait(30) or self.failure or not self.ready.is_set():
            self.__exit__(None, None, None)
            raise RuntimeError(self.failure or 'ssh_tunnel_startup_timeout')
        return self

    def __exit__(self, *exc):
        if self.loop and not self.loop.is_closed() and self.task:
            try:
                self.loop.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:
                pass  # Loop finished between the is_closed check and scheduling.
        if self.thread:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise RuntimeError('ssh_tunnel_shutdown_timeout')

    def _thread_main(self):
        async def run():
            self.loop = asyncio.get_running_loop()
            self.task = asyncio.current_task()
            await self._maintain()
        try:
            asyncio.run(run())
        except asyncio.CancelledError:
            pass
        except Exception:
            self.failure = 'ssh_tunnel_internal_error'
            LOG.error(self.failure)
        finally:
            self.ready.clear()
            self.started.set()

    async def _maintain(self):
        import asyncssh
        failures = 0
        while True:
            connection = listener = None
            try:
                connection = await asyncssh.connect(
                    self.config.host, port=self.config.port, username=self.config.username,
                    client_keys=[str(self.config.key_path)], passphrase=self.config.passphrase,
                    known_hosts=str(self.config.known_hosts), config=None, agent_path=None,
                    password_auth=False, kbdint_auth=False,
                    connect_timeout=10, keepalive_interval=10, keepalive_count_max=2,
                )
                listener = await connection.forward_local_port(
                    '127.0.0.1', self.local_port, self.config.remote_host, self.config.remote_port,
                )
                self.local_port = listener.get_port()
                self.ready.set()
                self.started.set()
                LOG.info('ssh_tunnel_ready')
                failures = 0
                await connection.wait_closed()
            except (asyncssh.PermissionDenied, asyncssh.HostKeyNotVerifiable):
                self.failure = 'ssh_authentication_or_host_key_failed'
                LOG.error(self.failure)
                self.started.set()
                return
            except (ValueError, asyncssh.KeyImportError):
                self.failure = 'ssh_key_or_configuration_invalid'
                LOG.error(self.failure)
                self.started.set()
                return
            except OSError as error:
                if error.errno == errno.EADDRINUSE:
                    self.failure = 'ssh_local_port_unavailable'
                    LOG.error(self.failure)
                    self.started.set()
                    return
            except asyncssh.Error:
                pass
            finally:
                self.ready.clear()
                if listener:
                    listener.close()
                    await listener.wait_closed()
                if connection:
                    connection.abort()
                    try:
                        await asyncio.wait_for(connection.wait_closed(), timeout=5)
                    except (asyncio.TimeoutError, asyncssh.Error):
                        pass
            LOG.warning('ssh_tunnel_reconnect')
            delay = (1, 2, 5, 10, 30)[min(failures, 4)]
            failures += 1
            await asyncio.sleep(delay)


def tunneled_config(config, port):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    values = conninfo_to_dict(config.database_url)
    # Pin the socket address even if DATABASE_URL contains hostaddr or multiple hosts.
    # Retain a single logical hostname only for PostgreSQL certificate verification.
    host = values.get('host', config.ssh.remote_host)
    if ',' in host:
        raise ValueError('ssh_database_requires_single_host')
    if config.ssh.database_sslmode == 'disable':
        host = '127.0.0.1'
    values.update(host=host, hostaddr='127.0.0.1', port=str(port), sslmode=config.ssh.database_sslmode)
    return replace(config, database_url=make_conninfo(**values))


@contextmanager
def database_transport(config):
    if not config.ssh.enabled:
        yield config, lambda: None
        return
    with SSHTunnel(config.ssh) as tunnel:
        yield tunneled_config(config, tunnel.local_port), tunnel.check_ready
