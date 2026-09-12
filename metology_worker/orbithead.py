"""Run OrbitHead's CLI in its own Python environment."""
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from .runtime import WorkerError

LOG = logging.getLogger(__name__)


def orbithead_python(root):
    override = os.environ.get('ORBITHEAD_PYTHON')
    if override:
        return override
    executable = 'Scripts/python.exe' if os.name == 'nt' else 'bin/python'
    return str(Path(root) / '.venv' / executable)


class OrbitHeadEngine:
    result_format = 'glb'

    def __init__(self, root):
        self.root = Path(root).resolve()
        if not (self.root / 'orbithead' / 'cli.py').is_file():
            raise ValueError('orbithead_checkout_missing')
        self.python = orbithead_python(self.root)
        self.options = []
        for flag, default, minimum in [('gpu', 0, 0), ('frames', 48, 1), ('da3-res', 756, 1),
                                       ('jpeg', 92, 0), ('texture-size', 8192, 1)]:
            value = int(os.environ.get('ORBITHEAD_' + flag.upper().replace('-', '_'), default))
            if value < minimum or (flag == 'jpeg' and value > 100):
                raise ValueError('invalid_orbithead_' + flag.replace('-', '_'))
            self.options.extend(['--' + flag, str(value)])
        geometry = os.environ.get('ORBITHEAD_GEOMETRY', 'da3')
        if geometry not in {'da3', 'mvs'}:
            raise ValueError('invalid_orbithead_geometry')
        self.options.extend(['--geometry', geometry, '--no-preview', '--clean'])
        for flag in ['carve', 'masked-sfm']:
            value = os.environ.get('ORBITHEAD_' + flag.upper().replace('-', '_'), '0')
            if value not in {'0', '1'}:
                raise ValueError('invalid_orbithead_' + flag.replace('-', '_'))
            if value == '1':
                self.options.append('--' + flag)

    def reconstruct(self, source, target, on_stage, check_cancel):
        check_cancel()
        source = Path(source).resolve()
        output = source.parent / 'orbithead'
        status = source.parent / 'orbithead-error.json'
        status.unlink(missing_ok=True)
        command = [self.python, str(Path(__file__).with_name('orbithead_runner.py')),
                   str(self.root), str(status), 'run', str(source), str(output), *self.options]
        try:
            result = subprocess.run(command, cwd=self.root, check=False)
        except FileNotFoundError:
            raise WorkerError('infrastructure_unavailable', True) from None
        check_cancel()
        if result.returncode:
            if status.is_file():
                error = json.loads(status.read_text(encoding='utf-8'))
                LOG.error('orbithead_subprocess_failed command=%s returncode=%s stderr=%s',
                          error['command'], error['returncode'], error['stderr'])
                if error['code']:
                    raise WorkerError(error['code'], error['retryable'])
            raise subprocess.CalledProcessError(result.returncode, command)
        generated = output / 'head.glb'
        if not generated.is_file():
            raise WorkerError('result_invalid', False)
        on_stage('exporting_ply')
        shutil.move(generated, target)
