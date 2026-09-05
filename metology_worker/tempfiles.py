"""Private per-process directories with flock-protected crash recovery (Linux/WSL)."""
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def _run_directory(path, root):
    if path.is_symlink() or not path.is_dir() or path.resolve().parent != root:
        return False
    try:
        uuid.UUID(path.name.removeprefix('run-'))
    except ValueError:
        return False
    return path.name.startswith('run-')


def cleanup_stale(root, age_seconds=86400):
    import fcntl
    root = Path(root).resolve()
    for path in root.iterdir():
        if not _run_directory(path, root) or time.time() - path.stat().st_mtime < age_seconds:
            continue
        lock = path / '.lock'
        if lock.is_symlink():
            continue
        try:
            with lock.open('a') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Only an unlocked, old, immediate child under the configured root.
                shutil.rmtree(path)
        except (BlockingIOError, FileNotFoundError):
            continue


@contextmanager
def process_directory(root, worker_id):
    import fcntl
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    cleanup_stale(root)
    path = root / f'run-{uuid.UUID(str(worker_id))}'
    path.mkdir(mode=0o700)
    with (path / '.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield path
        finally:
            if _run_directory(path, root):
                shutil.rmtree(path)
