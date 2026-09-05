"""Short transactions through the versioned SQL API, never Django tables."""
from contextlib import contextmanager


class Database:
    def __init__(self, connect):
        self.connect = connect

    def call(self, function, args=(), casts=None):
        placeholders = ', '.join(casts or ['%s'] * len(args))
        with self.connect() as connection:
            return connection.execute(
                f'SELECT * FROM worker_api.{function}({placeholders})', args
            ).fetchone()

    def check_version(self):
        row = self.call('protocol_version_v1')
        if row is None or next(iter(row.values())) != 1:
            raise RuntimeError('unsupported_worker_protocol')

    def claim(self, worker_id):
        row = self.call('claim_job_v1', (worker_id, ['photo_3D_fl']), ['%s::uuid', '%s::text[]'])
        if row is not None and (row['protocol_version'] != 1 or row['job_type'] != 'photo_3D_fl'):
            raise RuntimeError('invalid_claim_protocol')
        return row

    @staticmethod
    def identity(a):
        return a['job_id'], a['attempt_id'], a['lease_token']

    def heartbeat(self, a, stage):
        return self.call('heartbeat_job_v1', self.identity(a) + (stage,))

    def complete(self, a, size, digest):
        return self.call('complete_job_v1', self.identity(a) + (size, digest))

    def fail(self, a, code, summary, retryable):
        return self.call('fail_job_v1', self.identity(a) + (code, summary, retryable))

    def acknowledge(self, a):
        return self.call('acknowledge_cancellation_v1', self.identity(a))


def create_database(config):
    import psycopg
    from psycopg.rows import dict_row
    from .service import connection_options

    @contextmanager
    def connect():
        try:
            with psycopg.connect(config.database_url, connect_timeout=5,
                                 row_factory=dict_row,
                                 options='-c statement_timeout=10000 -c lock_timeout=5000',
                                 **connection_options(config)) as conn:
                yield conn
        except (psycopg.OperationalError, psycopg.InterfaceError):
            raise ConnectionError('database_unavailable') from None

    return Database(connect)
