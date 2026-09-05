import io
import tempfile
import unittest
from pathlib import Path

from metology_worker.storage import Storage
from metology_worker.runtime import WorkerError


class Client:
    def __init__(self, data, head_size=3):
        self.data, self.head_size = data, head_size
        self.body = None

    def head_object(self, **kwargs):
        return {'ContentLength': self.head_size}

    def get_object(self, **kwargs):
        self.body = io.BytesIO(self.data)
        return {'Body': self.body, 'ContentLength': len(self.data)}


class StorageTests(unittest.TestCase):
    def download(self, client):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'input'
            Storage(client).download(dict(input_bucket='b', input_object_key='k', input_size_bytes=3), path, lambda: None)
            return path.read_bytes()

    def test_download_returns_exact_bytes_and_closes_response(self):
        client = Client(b'abc')
        self.assertEqual(self.download(client), b'abc')
        self.assertTrue(client.body.closed)

    def test_head_mismatch_prevents_get(self):
        client = Client(b'abc', 4)
        with self.assertRaises(WorkerError) as error:
            self.download(client)
        self.assertEqual(error.exception.code, 'input_object_invalid')
        self.assertIsNone(client.body)

    def test_changed_object_cannot_exceed_claim_size(self):
        client = Client(b'abcd')
        with self.assertRaises(WorkerError):
            self.download(client)
        self.assertTrue(client.body.closed)

    def test_truncated_stream_is_rejected(self):
        with self.assertRaises(WorkerError):
            self.download(Client(b'ab'))
