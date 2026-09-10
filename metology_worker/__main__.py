import argparse
import logging
import os
import platform
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .config import Config, PROCESSOR_JOB_TYPES
from .runtime import file_metadata, run_attempt

LOG = logging.getLogger('metology_worker')


def doctor(config):
    import importlib.util
    checks = {
        'Linux / WSL2': platform.system() == 'Linux',
        'Python 3.10': sys.version_info[:2] == (3, 10),
    }
    if 'facelift' in config.processors:
        checks['FaceLift checkout'] = (config.facelift_path / 'inference.py').is_file()
        checks['CUDA compiler (nvcc)'] = shutil.which('nvcc') is not None
        checks['ffmpeg'] = shutil.which('ffmpeg') is not None
        for name in ['torch', 'psycopg', 'boto3', 'PIL', 'pillow_heif',
                     'diff_gaussian_rasterization', 'onnxruntime']:
            checks[name] = importlib.util.find_spec(name) is not None
        if checks['torch']:
            import torch
            checks['CUDA accessible'] = torch.cuda.is_available()
        for relative in ['checkpoints/mvdiffusion/pipeckpts', 'checkpoints/gslrm/ckpt_0000000000021125.pt',
                         'mvdiffusion/data/fixed_prompt_embeds_6view/clr_embeds.pt']:
            checks[relative] = (config.facelift_path / relative).exists()
    if 'orbithead' in config.processors:
        checks['OrbitHead checkout'] = (config.orbithead_path / 'orbithead' / 'pipeline.py').is_file()
        checks['ffmpeg'] = shutil.which('ffmpeg') is not None
        checks['COLMAP'] = shutil.which('colmap') is not None
        openmvs = os.environ.get('OPENMVS_BIN')
        checks['OpenMVS'] = bool(openmvs and (Path(openmvs) / 'DensifyPointCloud').is_file()) or (
            shutil.which('DensifyPointCloud') is not None
        )
        for name in ['numpy', 'cv2', 'PIL', 'trimesh', 'scipy', 'rembg', 'skimage', 'onnxruntime']:
            checks[name] = importlib.util.find_spec(name) is not None
    for name in ['psycopg', 'boto3']:
        checks[name] = importlib.util.find_spec(name) is not None
    if config.ssh.enabled:
        checks['asyncssh'] = importlib.util.find_spec('asyncssh') is not None
        checks['SSH private key'] = bool(config.ssh.key_path and config.ssh.key_path.is_file())
        checks['SSH known_hosts'] = config.ssh.known_hosts.is_file()
    for name, ok in checks.items():
        print(f'{"OK" if ok else "MISSING"}: {name}')
    return 0 if all(checks.values()) else 1


def create_engines(config):
    engines = {}
    for processor in config.processors:
        job_type = PROCESSOR_JOB_TYPES[processor]
        if processor == 'facelift':
            from .facelift import FaceLiftEngine
            engines[job_type] = FaceLiftEngine(config.facelift_path)
        elif processor == 'orbithead':
            from .orbithead import OrbitHeadEngine
            engines[job_type] = OrbitHeadEngine(config.orbithead_path)
        else:
            raise ValueError('invalid_worker_processor')
    return engines


def infer_local(config, source, output):
    if len(config.processors) != 1:
        raise ValueError('infer_local_requires_single_processor')
    if output.exists():
        raise ValueError('output_already_exists')
    if not source.is_file():
        raise ValueError('input_file_missing')
    engine = next(iter(create_engines(config).values()))
    config.temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='local-', dir=config.temp_root) as directory:
        local = Path(directory) / 'input'
        result_format = getattr(engine, 'result_format', 'ply')
        result = Path(directory) / f'result.{result_format}'
        shutil.copyfile(source, local)
        engine.reconstruct(local, result, lambda stage: LOG.info('stage=%s', stage), lambda: None)
        size, digest = file_metadata(result, result_format)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('xb') as destination, result.open('rb') as generated:
            shutil.copyfileobj(generated, destination)
        print(f'Result: {output}\nBytes: {size}\nSHA-256: {digest}')
    return 0


def run(config):
    from .database import create_database
    from .service import consume_queue, listener_factory
    from .storage import create_storage
    from .tempfiles import process_directory
    from .tunnel import database_transport

    if platform.system() != 'Linux':
        raise ValueError('run_requires_linux_or_wsl2')
    config.validate_remote()
    worker_id = uuid.uuid4()
    stopped = threading.Event()
    deadline = [None]

    def shutdown(signum, frame):
        if not stopped.is_set():
            deadline[0] = time.monotonic() + config.shutdown_seconds
            stopped.set()
            LOG.info('shutdown_requested')

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    with database_transport(config) as (db_config, check_ready):
        if stopped.is_set():
            return 0
        db = create_database(db_config, check_ready)
        db.check_version()
        engines = create_engines(config)
        storage = create_storage(config)
        with process_directory(config.temp_root, worker_id) as root:
            def execute(a):
                if (a['input_bucket'] != config.s3_bucket or a['result_bucket'] != config.s3_bucket
                        or a['input_size_bytes'] <= 0 or a['parameters'] != {}):
                    raise RuntimeError('invalid_claim_configuration')
                engine = engines.get(a['job_type'])
                if engine is None:
                    raise RuntimeError('invalid_claim_protocol')
                outcome = run_attempt(db, storage, engine, a, root, lambda: deadline[0])
                LOG.info('attempt_outcome=%s', outcome)
            LOG.info('worker_ready processors=%s', ','.join(config.processors))
            consume_queue(db, listener_factory(db_config, check_ready), worker_id, stopped, execute)
    return 0


def check_database(config):
    from .database import create_database
    from .tunnel import database_transport
    if not config.database_url:
        raise ValueError('missing_database_configuration')
    with database_transport(config) as (db_config, check_ready):
        create_database(db_config, check_ready).check_version()
    print('OK: PostgreSQL worker_api/v1')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Metology worker (Linux / WSL2)')
    parser.add_argument('--env-file', type=Path, help='Optional local dotenv file; existing environment wins')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor', help='Check environment and model files without database access')
    commands.add_parser('run', help='Consume worker_api/v1 jobs')
    commands.add_parser('check-db', help='Check PostgreSQL worker_api/v1, including optional SSH tunnel')
    local = commands.add_parser('infer-local', help='Generate one local result without PostgreSQL/S3')
    local.add_argument('input', type=Path)
    local.add_argument('output', type=Path)
    args = parser.parse_args(argv)
    if args.env_file:
        from dotenv import load_dotenv
        if not args.env_file.is_file():
            parser.error('env_file_missing')
        load_dotenv(args.env_file, override=False)
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    # Do not enable boto/SQL debug logs: they may contain request credentials.
    for name in ['botocore', 'boto3', 'urllib3', 'psycopg', 'asyncssh']:
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        config = Config.load()
        if args.command == 'doctor':
            return doctor(config)
        if args.command == 'infer-local':
            return infer_local(config, args.input.resolve(), args.output.resolve())
        if args.command == 'check-db':
            return check_database(config)
        return run(config)
    except Exception as error:
        # Only bounded internal codes, never str(network_error) or a raw traceback.
        code = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        if not code.replace('_', '').isalnum() or len(code) > 80:
            code = type(error).__name__
        LOG.error('startup_or_run_failed code=%s', code)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
