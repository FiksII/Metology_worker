import unittest
from metology_worker.database import Database


class Cursor:
    def fetchone(self):
        return {'accepted': True, 'protocol_version': 1, 'job_type': 'photo_3D_fl'}


class Connection:
    def __init__(self):
        self.calls = []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        return Cursor()


class DatabaseTests(unittest.TestCase):
    def test_fencing_and_result_metadata_are_bound_parameters(self):
        connection = Connection()
        db = Database(lambda: connection)
        db.complete({'job_id': 'j', 'attempt_id': 'a', 'lease_token': 't'}, 42, 'abc')
        sql, args = connection.calls[-1]
        self.assertIn('worker_api.complete_job_v1', sql)
        self.assertEqual(args, ('j', 'a', 't', 42, 'abc'))
        self.assertNotIn('abc', sql)

    def test_claim_only_advertises_exact_supported_type(self):
        connection = Connection()
        Database(lambda: connection).claim('worker')
        self.assertEqual(connection.calls[-1][1], ('worker', ['photo_3D_fl']))
