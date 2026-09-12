"""Dependency-free CLI bridge preserving structured errors across environments."""
import json
import subprocess
import sys
from pathlib import Path


def main():
    root, status, *arguments = sys.argv[1:]
    sys.path.insert(0, root)
    try:
        from orbithead.cli import main as run_cli
        return run_cli(arguments)
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        command = ''
        code = 'infrastructure_unavailable' if isinstance(error, FileNotFoundError) else None
        if isinstance(error, subprocess.CalledProcessError):
            command = Path(str(error.cmd[0])).name if error.cmd else 'unknown'
            if command in {'ffprobe', 'ffmpeg'}:
                code = 'input_not_supported'
        stderr = getattr(error, 'stderr', None) or str(error)
        if isinstance(stderr, bytes):
            stderr = stderr.decode('utf-8', errors='replace')
        Path(status).write_text(json.dumps({
            'command': command, 'returncode': getattr(error, 'returncode', 1),
            'stderr': stderr[:2000].replace(chr(13), r'\r').replace(chr(10), r'\n'),
            'code': code, 'retryable': code == 'infrastructure_unavailable',
        }), encoding='utf-8')
        return 1


if __name__ == '__main__':
    sys.exit(main())
