"""Sequential claim loop; notifications are hints, polling is authoritative."""
import logging
import random
from contextlib import contextmanager

LOG = logging.getLogger(__name__)


def consume_queue(db, listen, worker_id, stopped, execute):
    failures = 0
    while not stopped.is_set():
        try:
            with listen() as listener:
                while not stopped.is_set():
                    assignment = db.claim(worker_id)
                    if assignment is None:
                        listener.wait()
                    else:
                        execute(assignment)
                    failures = 0
        except ConnectionError:
            delay = (1, 2, 5, 10, 30)[min(failures, 4)]
            failures += 1
            LOG.warning('database_reconnect')
            stopped.wait(delay * random.uniform(0.8, 1.2))


def listener_factory(config):
    import psycopg

    class Listener:
        def __init__(self, connection):
            self.connection = connection

        def wait(self):
            # Consume at most one hint; queued jobs are always drained first.
            notifications = self.connection.notifies(timeout=30, stop_after=1)
            try:
                for _ in notifications:
                    break
            finally:
                notifications.close()

    @contextmanager
    def listen():
        try:
            with psycopg.connect(config.database_url, autocommit=True, connect_timeout=5,
                                 **connection_options(config)) as connection:
                connection.execute(psycopg.sql.SQL('LISTEN {}').format(
                    psycopg.sql.Identifier(config.notify_channel)
                ))
                yield Listener(connection)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            raise ConnectionError('listen_unavailable') from None

    return listen


def connection_options(config):
    values = dict(keepalives=1, keepalives_idle=10, keepalives_interval=5,
                  keepalives_count=2, tcp_user_timeout=10000)
    if not config.insecure:
        values['sslmode'] = 'verify-full'
    return values
