import unittest
from unittest.mock import patch
from metology_worker.config import Config


class ConfigTests(unittest.TestCase):
    def test_notify_channel_defaults_to_contract_and_can_be_overridden(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(Config.load().notify_channel, 'metology_jobs')
        with patch.dict('os.environ', {'DATABASE_NOTIFY_CHANNEL': 'metology_jobs_dev'}, clear=True):
            self.assertEqual(Config.load().notify_channel, 'metology_jobs_dev')

    def test_invalid_or_truncated_notify_channel_is_rejected(self):
        for channel in ['', 'jobs; DROP TABLE jobs', 'x' * 64, 'jobs dev']:
            with self.subTest(channel=channel):
                with patch.dict('os.environ', {'DATABASE_NOTIFY_CHANNEL': channel}, clear=True):
                    with self.assertRaisesRegex(ValueError, 'invalid_notify_channel'):
                        Config.load()

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
