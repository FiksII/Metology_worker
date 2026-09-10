import hashlib
import struct
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from metology_worker.runtime import run_attempt, WorkerError, LeaseGuard, LeaseLost


def assignment():
    return dict(job_id='job', attempt_id='attempt', lease_token='token',
                lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
                input_bucket='private', input_object_key='input', input_size_bytes=3,
                result_bucket='private', result_object_key='assigned/result.ply')


class DB:
    def __init__(self):
        self.stages = []
        self.outcomes = []
        self.accepted = True
        self.cancel = False
        self.ambiguous = False

    def heartbeat(self, a, stage):
        self.stages.append(stage)
        return dict(accepted=self.accepted, cancel_requested=self.cancel,
                    lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=120))

    def complete(self, a, size, digest):
        self.outcomes.append(('complete', size, digest))
        if self.ambiguous:
            raise ConnectionError('response lost')
        return dict(accepted=self.accepted and not self.cancel, job_status='canceled' if self.cancel else 'succeeded')

    def fail(self, a, code, summary, retryable):
        self.outcomes.append(('fail', code, summary, retryable))
        return dict(accepted=True)

    def acknowledge(self, a):
        self.outcomes.append(('cancel',))
        return dict(accepted=True)


class Storage:
    def __init__(self):
        self.objects = {}

    def download(self, a, path, check):
        check()
        path.write_bytes(b'img')

    def upload(self, a, path, check):
        check()
        self.objects[a['result_object_key']] = path.read_bytes()

    def delete(self, a):
        self.objects.pop(a['result_object_key'], None)


class Engine:
    def reconstruct(self, source, target, stage, check):
        check()
        stage('exporting_ply')
        target.write_bytes(b'ply\nformat binary_little_endian 1.0\nend_header\n123')


class GlbEngine:
    result_format = 'glb'

    def reconstruct(self, source, target, stage, check):
        check()
        stage('exporting_ply')
        target.write_bytes(b'glTF' + struct.pack('<II', 2, 12))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db, self.storage = DB(), Storage()

    def run_job(self, engine=None):
        return run_attempt(self.db, self.storage, engine or Engine(), assignment(), self.root)

    def test_success_uploads_then_completes_with_digest_and_cleans_local_files(self):
        self.assertEqual(self.run_job(), 'succeeded')
        data = self.storage.objects['assigned/result.ply']
        self.assertEqual(self.db.outcomes, [('complete', len(data), hashlib.sha256(data).hexdigest())])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.db.stages[:4], ['downloading', 'processing', 'exporting_ply', 'uploading_result'])

    def test_glb_result_uploads_then_completes_with_digest(self):
        self.assertEqual(self.run_job(GlbEngine()), 'succeeded')
        data = self.storage.objects['assigned/result.ply']
        self.assertTrue(data.startswith(b'glTF'))
        self.assertEqual(self.db.outcomes, [('complete', len(data), hashlib.sha256(data).hexdigest())])

    def test_cancel_does_not_publish(self):
        self.db.cancel = True
        self.assertEqual(self.run_job(), 'canceled')
        self.assertEqual(self.storage.objects, {})
        self.assertEqual(self.db.outcomes, [('cancel',)])

    def test_lost_lease_never_completes_or_fails(self):
        self.db.accepted = False
        self.assertEqual(self.run_job(), 'lease_lost')
        self.assertEqual(self.db.outcomes, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_attempt_in_heartbeat_never_mutates_again(self):
        self.db.heartbeat = lambda *args: None
        self.assertEqual(self.run_job(), 'lease_lost')
        self.assertEqual(self.db.outcomes, [])

    def test_stage_retries_brief_database_failure_without_consuming_job_attempt(self):
        original = self.db.heartbeat
        calls = [0]
        def heartbeat(*args):
            calls[0] += 1
            if calls[0] == 1:
                raise ConnectionError('temporary')
            return original(*args)
        self.db.heartbeat = heartbeat
        guard = LeaseGuard(self.db, assignment())
        with patch.object(guard.stopped, 'wait', return_value=False):
            guard.stage('processing')
        guard.check()
        self.assertEqual(calls[0], 2)
        self.assertEqual(self.db.outcomes, [])

    def test_permanent_error_is_sanitized(self):
        class BadEngine:
            def reconstruct(self, *args):
                raise WorkerError('input_not_supported', False)
        self.assertEqual(self.run_job(BadEngine()), 'failed')
        self.assertEqual(self.db.outcomes[0][1:], ('input_not_supported', 'input_not_supported', False))

    def test_ambiguous_complete_preserves_uploaded_object(self):
        self.db.ambiguous = True
        self.assertEqual(self.run_job(), 'completion_unknown')
        self.assertIn('assigned/result.ply', self.storage.objects)
        self.assertTrue(all(x[0] == 'complete' for x in self.db.outcomes))

    def test_non_network_error_resolving_complete_does_not_delete_possibly_accepted_result(self):
        attempts = iter([ConnectionError('lost'), RuntimeError('unexpected response')])
        def complete(*args):
            raise next(attempts)
        self.db.complete = complete
        self.assertEqual(self.run_job(), 'completion_unknown')
        self.assertIn('assigned/result.ply', self.storage.objects)
        self.assertEqual(self.db.outcomes, [])

    def test_cancel_between_upload_and_complete_deletes_object(self):
        original = self.storage.upload
        def upload(*args):
            original(*args)
            self.db.cancel = True
        self.storage.upload = upload
        self.assertEqual(self.run_job(), 'canceled')
        self.assertEqual(self.storage.objects, {})

    def test_background_heartbeat_runs_while_computation_blocks(self):
        with LeaseGuard(self.db, assignment(), interval=0.01) as guard:
            guard.stage('processing')
            time.sleep(0.06)
            self.assertGreaterEqual(len(self.db.stages), 3)

    def test_expired_lease_check_rejects_work_without_db_call(self):
        a = assignment()
        a['lease_expires_at'] = datetime.now(timezone.utc) - timedelta(seconds=1)
        with LeaseGuard(self.db, a) as guard:
            with self.assertRaises(LeaseLost):
                guard.check()
        self.assertEqual(self.db.stages, [])

    def test_rejected_cancellation_acknowledgement_is_not_reported_as_canceled(self):
        self.db.cancel = True
        self.db.acknowledge = lambda a: {'accepted': False}
        self.assertEqual(self.run_job(), 'lease_lost')

    def test_rejected_failure_is_not_reported_as_accepted_failure(self):
        self.db.fail = lambda *a: {'accepted': False}
        class BadEngine:
            def reconstruct(self, *args):
                raise WorkerError('input_not_supported', False)
        self.assertEqual(self.run_job(BadEngine()), 'lease_lost')

    def test_disk_full_creating_attempt_directory_reports_retryable_failure(self):
        with patch('metology_worker.runtime.tempfile.TemporaryDirectory', side_effect=OSError(28, 'disk full')):
            self.assertEqual(self.run_job(), 'failed')
        self.assertEqual(self.db.outcomes[0][1], 'worker_storage_exhausted')
        self.assertTrue(self.db.outcomes[0][-1])


if __name__ == '__main__':
    unittest.main()
