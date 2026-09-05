import threading
import unittest
from contextlib import contextmanager
from metology_worker.service import consume_queue


class ServiceTests(unittest.TestCase):
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
