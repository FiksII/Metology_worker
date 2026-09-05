import threading
import unittest
import importlib.util
from unittest.mock import patch
from contextlib import contextmanager
from metology_worker.config import Config
from metology_worker.service import consume_queue, listener_factory


class ServiceTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('psycopg'), 'Install worker dependencies')
    def test_listener_uses_configured_channel_as_quoted_identifier(self):
        with patch.dict('os.environ', {'DATABASE_NOTIFY_CHANNEL': 'Jobs_dev'}, clear=True):
            config = Config.load()
        with patch('psycopg.connect') as connect:
            with listener_factory(config)():
                pass
            connection = connect.return_value.__enter__.return_value
            query = connection.execute.call_args.args[0]
            self.assertEqual(query.as_string(), 'LISTEN "Jobs_dev"')
            self.assertTrue(connect.call_args.kwargs['autocommit'])

    def test_listen_precedes_claim_and_pending_jobs_need_no_notify(self):
        events = []
        stop = threading.Event()
        jobs = iter([{'job': 1}, {'job': 2}, None])
        class DB:
            def claim(self, worker):
                events.append('claim')
                return next(jobs)
        class Listener:
            def wait(self):
                events.append('wait')
                stop.set()
        @contextmanager
        def listen():
            events.append('listen')
            yield Listener()
        consume_queue(DB(), listen, 'w', stop, lambda a: events.append(a['job']))
        self.assertEqual(events, ['listen', 'claim', 1, 'claim', 2, 'claim', 'wait'])

    def test_shutdown_stops_claiming_after_current_attempt(self):
        stop = threading.Event()
        class DB:
            def __init__(self):
                self.claims = 0
            def claim(self, worker):
                self.claims += 1
                return {'job': 1}
        @contextmanager
        def listen():
            yield None
        db = DB()
        consume_queue(db, listen, 'w', stop, lambda a: stop.set())
        self.assertEqual(db.claims, 1)
