"""Adapter to the OrbitHead checkout."""
import importlib
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from .runtime import WorkerError

LOG = logging.getLogger(__name__)


def _subprocess_text(value, limit=2000):
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    text = str(value or '').replace('\r', '\\r').replace('\n', '\\n')
    return text[:limit]


class OrbitHeadEngine:
    result_format = 'glb'

    def __init__(self, root):
        root = Path(root).resolve()
        pipeline = root / 'orbithead' / 'pipeline.py'
        if not pipeline.is_file():
            raise ValueError('orbithead_checkout_missing')
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        sys.modules.pop('orbithead.pipeline', None)
        sys.modules.pop('orbithead', None)
        self.pipeline = importlib.import_module('orbithead.pipeline')
        if Path(self.pipeline.__file__).resolve() != pipeline:
            raise RuntimeError('wrong_orbithead_module')

    def reconstruct(self, source, target, on_stage, check_cancel):
        check_cancel()
        output = Path(source).parent / 'orbithead'
        try:
            report = self.pipeline.run_pipeline(
                str(source), str(output), preview=False, keep_intermediate=False
            )
        except subprocess.CalledProcessError as error:
            command = Path(str(error.cmd[0])).name if error.cmd else 'unknown'
            LOG.error('orbithead_subprocess_failed command=%s returncode=%s stderr=%s',
                      command, error.returncode, _subprocess_text(error.stderr))
            if command in {'ffprobe', 'ffmpeg'}:
                raise WorkerError('input_not_supported', False) from None
            raise
        except FileNotFoundError:
            raise WorkerError('infrastructure_unavailable', True) from None
        check_cancel()
        generated = Path(report.get('glb', {}).get('png', ''))
        if not generated.is_file():
            raise WorkerError('result_invalid', False)
        on_stage('exporting_ply')
        shutil.move(generated, target)
