import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from metology_worker.tempfiles import cleanup_stale, process_directory


@unittest.skipUnless(sys.platform == 'linux', 'flock requires Linux / WSL')
class TempFilesTests(unittest.TestCase):
    def test_cleanup_removes_only_stale_unlocked_run_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unrelated = root / 'keep'
            unrelated.mkdir()
            stale = root / f'run-{uuid.uuid4()}'
            stale.mkdir()
            (stale / 'photo').write_bytes(b'private')
            os.utime(stale, (1, 1))
            with process_directory(root, uuid.uuid4()) as active:
                os.utime(active, (1, 1))
                cleanup_stale(root)
                self.assertTrue(active.exists())
                self.assertFalse(stale.exists())
                self.assertTrue(unrelated.exists())
            self.assertFalse(active.exists())

    def test_symlink_outside_root_is_never_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'root'
            root.mkdir()
            outside = Path(tmp) / 'outside'
            outside.mkdir()
            link = root / f'run-{uuid.uuid4()}'
            link.symlink_to(outside, target_is_directory=True)
            cleanup_stale(root, age_seconds=0)
            self.assertTrue(outside.exists())
            self.assertTrue(link.is_symlink())
