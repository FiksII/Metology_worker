import unittest
from unittest.mock import patch
from metology_worker.config import Config


class ConfigTests(unittest.TestCase):
    def test_local_inference_does_not_require_remote_credentials(self):
        with patch.dict('os.environ', {}, clear=True):
            config = Config.load()
        self.assertTrue(config.facelift_path.is_absolute())
        with self.assertRaises(ValueError):
            config.validate_remote()

    def test_secret_values_are_not_exposed_by_repr(self):
        with patch.dict('os.environ', {'S3_SECRET_ACCESS_KEY': 'secret-example', 'DATABASE_URL': 'secret-dsn'}, clear=True):
            config = Config.load()
        self.assertNotIn('secret-example', repr(config))
        self.assertNotIn('secret-dsn', repr(config))

    def test_remote_plain_http_requires_explicit_development_mode(self):
        env = dict(DATABASE_URL='postgresql://localhost/db', S3_ENDPOINT_URL='http://localhost:9000',
                   S3_BUCKET='private', S3_ACCESS_KEY_ID='x', S3_SECRET_ACCESS_KEY='y')
        with patch.dict('os.environ', env, clear=True):
            with self.assertRaises(ValueError):
                Config.load().validate_remote()
        with patch.dict('os.environ', {**env, 'WORKER_ALLOW_INSECURE': '1'}, clear=True):
            Config.load().validate_remote()
