import unittest
from metology_worker.database import Database


class Cursor:
    def __init__(self, row=None):
        self.row = row or {'accepted': True, 'protocol_version': 1, 'job_type': 'photo_3D_fl'}

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, row=None):
        self.calls = []
        self.row = row
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        return Cursor(self.row)


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

    def test_claim_can_advertise_multiple_supported_types(self):
        connection = Connection({'accepted': True, 'protocol_version': 1, 'job_type': 'OrbitHead'})
        Database(lambda: connection, ('OrbitHead', 'photo_3D_fl')).claim('worker')
        self.assertEqual(connection.calls[-1][1], ('worker', ['OrbitHead', 'photo_3D_fl']))
