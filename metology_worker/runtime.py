"""Attempt ownership and lifecycle, independent of CUDA and network libraries."""
import hashlib
import logging
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from contextlib import ExitStack

LOG = logging.getLogger(__name__)
STAGES = {'starting', 'downloading', 'processing', 'exporting_ply', 'uploading_result'}


class WorkerError(Exception):
    def __init__(self, code, retryable):
        super().__init__(code)
        self.code, self.retryable = code, retryable


class LeaseLost(Exception):
    pass


class Cancelled(Exception):
    pass


class Shutdown(Exception):
    pass


class LeaseGuard:
    def __init__(self, db, assignment, interval=20, shutdown_deadline=None):
        self.db, self.assignment = db, assignment
        self.interval = interval
        self.shutdown_deadline = shutdown_deadline or (lambda: None)
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.current_stage = 'starting'
        self.cancelled = self.lost = self.finished = False
        self._renew(assignment['lease_expires_at'])

    def _renew(self, expires):
        if isinstance(expires, str):
            expires = datetime.fromisoformat(expires)
        remaining = (expires - datetime.now(timezone.utc)).total_seconds()
        self.deadline = time.monotonic() + remaining - 2

    def check(self, allow_cancel=False):
        if self.lost or time.monotonic() >= self.deadline:
            self.lost = True
            raise LeaseLost()
        deadline = self.shutdown_deadline()
        if deadline is not None and time.monotonic() >= deadline:
            raise Shutdown()
        if self.cancelled and not allow_cancel:
            raise Cancelled()

    def _heartbeat(self):
        with self.lock:
            if self.finished:
                return
            self.check(allow_cancel=True)
            result = self.db.heartbeat(self.assignment, self.current_stage)
            if not result or not result['accepted']:
                self.lost = True
                raise LeaseLost()
            self._renew(result['lease_expires_at'])
            self.cancelled = result['cancel_requested']

    def stage(self, value):
        if value not in STAGES:
            raise ValueError('invalid_worker_stage')
        with self.lock:
            self.check()
            self.current_stage = value
            failures = 0
            while True:
                try:
                    self._heartbeat()
                    break
                except ConnectionError:
                    self.check()
                    delay = (1, 2, 5, 10)[min(failures, 3)]
                    failures += 1
                    self.stopped.wait(min(delay, max(0, self.deadline - time.monotonic())))
            self.check()

    def _loop(self):
        delay = self.interval
        while not self.stopped.wait(delay):
            started = time.monotonic()
            try:
                self._heartbeat()
                delay = max(min(0.1, self.interval), self.interval - (time.monotonic() - started))
            except (LeaseLost, Shutdown):
                return
            except ConnectionError:
                delay = min(2, self.interval)
            except Exception:
                # A broken contract/heartbeat must not allow publication.
                self.lost = True
                LOG.error('heartbeat_internal_error')
                return

    def __enter__(self):
        self.thread = threading.Thread(target=self._loop, name='lease-heartbeat', daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stopped.set()
        self.thread.join()


def file_metadata(path):
    size = path.stat().st_size
    with path.open('rb') as stream:
        header = stream.read(4096)
        if not header.startswith(b'ply\n') and not header.startswith(b'ply\r\n'):
            raise WorkerError('result_invalid', False)
        if b'format binary_' not in header:
            raise WorkerError('result_invalid', False)
        stream.seek(0)
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return size, digest.hexdigest()


def run_attempt(db, storage, engine, assignment, root, shutdown_deadline=None):
    root = Path(root)
    uploaded = False
    preserve = False

    def cleanup_result():
        nonlocal uploaded
        if uploaded:
            try:
                storage.delete(assignment)
                uploaded = False
            except Exception:
                LOG.error('result_cleanup_failed')

    with LeaseGuard(db, assignment, shutdown_deadline=shutdown_deadline) as guard:
        with ExitStack() as files:
            try:
                root.mkdir(parents=True, exist_ok=True)
                directory = files.enter_context(tempfile.TemporaryDirectory(prefix='attempt-', dir=root))
                source, result = Path(directory) / 'input', Path(directory) / 'result.ply'
                guard.stage('downloading')
                storage.download(assignment, source, guard.check)
                guard.stage('processing')
                engine.reconstruct(source, result, guard.stage, guard.check)
                guard.check()
                size, digest = file_metadata(result)
                guard.stage('uploading_result')
                uploaded = True  # Includes a PUT whose response is lost.
                storage.upload(assignment, result, guard.check)
                with guard.lock:
                    guard.check()
                    # Keep uncertain successful commits safe from heartbeat/cleanup.
                    guard.finished = True
                    preserve = True
                    for _ in range(3):
                        try:
                            outcome = db.complete(assignment, size, digest)
                            break
                        except ConnectionError:
                            continue
                    else:
                        preserve = True
                        LOG.error('completion_unknown')
                        return 'completion_unknown'
                    if not outcome or not isinstance(outcome.get('accepted'), bool):
                        return 'completion_unknown'
                    if outcome['accepted']:
                        return 'succeeded'
                    preserve = False
                    guard.finished = False
                    # Distinguish cancellation with ownership from lost lease.
                    guard._heartbeat()
                    guard.check()
                    raise LeaseLost()
            except Cancelled:
                cleanup_result()
                with guard.lock:
                    try:
                        guard.check(allow_cancel=True)
                        outcome = db.acknowledge(assignment)
                        guard.finished = True
                        if not outcome or not outcome['accepted']:
                            return 'lease_lost'
                    except (LeaseLost, Shutdown, ConnectionError):
                        return 'lease_lost'
                return 'canceled'
            except (LeaseLost, Shutdown):
                return 'lease_lost'
            except Exception as error:
                if preserve:
                    LOG.error('completion_unknown')
                    return 'completion_unknown'
                if isinstance(error, WorkerError):
                    code, retryable = error.code, error.retryable
                elif isinstance(error, OSError) and error.errno == 28:
                    code, retryable = 'worker_storage_exhausted', True
                elif isinstance(error, ConnectionError):
                    code, retryable = 'infrastructure_unavailable', True
                else:
                    code, retryable = 'internal_error', False
                LOG.error('attempt_failed code=%s', code)
                with guard.lock:
                    try:
                        guard.check(allow_cancel=True)
                        if guard.cancelled:
                            cleanup_result()
                            outcome = db.acknowledge(assignment)
                        else:
                            outcome = db.fail(assignment, code, code, retryable)
                        guard.finished = True
                        if not outcome or not outcome['accepted']:
                            return 'lease_lost'
                    except (LeaseLost, Shutdown, ConnectionError):
                        return 'lease_lost'
                return 'canceled' if guard.cancelled else 'failed'
            finally:
                if not preserve:
                    cleanup_result()
