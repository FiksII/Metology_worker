import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from metology_worker.orbithead import OrbitHeadEngine
from metology_worker.runtime import WorkerError


class OrbitHeadEngineTests(unittest.TestCase):
    def setUp(self):
        self.path = list(sys.path)
        env = {key: value for key, value in os.environ.items() if not key.startswith('ORBITHEAD_')}
        self.env = patch.dict(os.environ, {**env, 'ORBITHEAD_PYTHON': sys.executable}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        sys.path[:] = self.path
        sys.modules.pop('orbithead', None)
        sys.modules.pop('orbithead.pipeline', None)

    def test_reconstruct_moves_pipeline_glb_to_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'checkout'
            package = root / 'orbithead'
            package.mkdir(parents=True)
            (package / '__init__.py').write_text('', encoding='utf-8')
            shutil.copyfile(Path(__file__).resolve().parents[1] / 'worker_processors/orbithead/orbithead/cli.py',
                            package / 'cli.py')
            (package / 'pipeline.py').write_text(
                "from pathlib import Path\n"
                "def run_pipeline(video, out_dir, **options):\n"
                "    out = Path(out_dir)\n"
                "    out.mkdir()\n"
                "    import json, os\n"
                "    (out.parent / 'options.json').write_text(json.dumps(dict(options, pid=os.getpid())))\n"
                "    glb = out / 'head.glb'\n"
                "    glb.write_bytes(b'glTF' + (2).to_bytes(4, 'little') + (12).to_bytes(4, 'little'))\n"
                "    return {'glb': {'png': str(glb)}, 'timings': {}}\n",
                encoding='utf-8',
            )
            source = Path(tmp) / 'capture.mov'
            target = Path(tmp) / 'result.glb'
            source.write_bytes(b'video')

            stages = []
            OrbitHeadEngine(root).reconstruct(source, target, stages.append, lambda: None)

            options = json.loads((Path(tmp) / 'options.json').read_text())
            self.assertNotEqual(options.pop('pid'), os.getpid())
            for key, value in dict(gpu=0, geometry='da3', n_frames=48, da3_res=756,
                                   jpeg_quality=92, max_texture_size=8192, preview=False,
                                   keep_intermediate=False, carve=False, masked_sfm=False).items():
                self.assertEqual(options[key], value, key)
            self.assertEqual(stages, ['exporting_ply'])
            self.assertTrue(target.read_bytes().startswith(b'glTF'))

    def test_ffprobe_failure_reports_input_not_supported_with_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'checkout'
            package = root / 'orbithead'
            package.mkdir(parents=True)
            (package / '__init__.py').write_text('', encoding='utf-8')
            shutil.copyfile(Path(__file__).resolve().parents[1] / 'worker_processors/orbithead/orbithead/cli.py',
                            package / 'cli.py')
            (package / 'pipeline.py').write_text(
                "import subprocess\n"
                "def run_pipeline(video, out_dir, **options):\n"
                "    raise subprocess.CalledProcessError(\n"
                "        1, ['ffprobe', video], stderr='moov atom not found')\n",
                encoding='utf-8',
            )
            source = Path(tmp) / 'capture.mov'
            target = Path(tmp) / 'result.glb'
            source.write_bytes(b'bad video')

            with self.assertLogs('metology_worker.orbithead', level='ERROR') as logs:
                with self.assertRaises(WorkerError) as error:
                    OrbitHeadEngine(root).reconstruct(source, target, lambda stage: None, lambda: None)

            self.assertEqual(error.exception.code, 'input_not_supported')
            self.assertFalse(error.exception.retryable)
            text = '\n'.join(logs.output)
            self.assertIn('orbithead_subprocess_failed command=ffprobe returncode=1', text)
            self.assertIn('moov atom not found', text)

    def make_checkout(self, directory, pipeline):
        root = Path(directory) / 'checkout with spaces'
        package = root / 'orbithead'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('', encoding='utf-8')
        shutil.copyfile(Path(__file__).resolve().parents[1] / 'worker_processors/orbithead/orbithead/cli.py',
                        package / 'cli.py')
        (package / 'pipeline.py').write_text(pipeline, encoding='utf-8')
        return root

    def test_overrides_reach_cli_without_changing_parent_gpu_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_checkout(tmp,
                "import json, os\nfrom pathlib import Path\n"
                "def run_pipeline(video, out_dir, **options):\n"
                "    os.environ['CUDA_VISIBLE_DEVICES'] = str(options['gpu'])\n"
                "    out = Path(out_dir)\n    out.mkdir()\n"
                "    (out / 'head.glb').write_text(json.dumps(options))\n"
                "    return {'glb': {}, 'timings': {}}\n")
            target = Path(tmp) / 'result.glb'
            env = dict(ORBITHEAD_GPU='1', ORBITHEAD_GEOMETRY='mvs', ORBITHEAD_FRAMES='64',
                       ORBITHEAD_DA3_RES='1008', ORBITHEAD_JPEG='0', ORBITHEAD_TEXTURE_SIZE='4096',
                       ORBITHEAD_CARVE='1', ORBITHEAD_MASKED_SFM='1', CUDA_VISIBLE_DEVICES='7')
            with patch.dict(os.environ, env):
                OrbitHeadEngine(root).reconstruct(Path(tmp) / 'input.mov', target, lambda _: None, lambda: None)
                self.assertEqual(os.environ['CUDA_VISIBLE_DEVICES'], '7')
            options = json.loads(target.read_text())
            for key, value in dict(gpu=1, geometry='mvs', n_frames=64, da3_res=1008,
                                   jpeg_quality=0, max_texture_size=4096, carve=True, masked_sfm=True,
                                   preview=False, keep_intermediate=False).items():
                self.assertEqual(options[key], value, key)

    def test_missing_executable_and_missing_result_have_distinct_failure_codes(self):
        for pipeline, code, retryable in [
            ("raise FileNotFoundError('COLMAP missing')", 'infrastructure_unavailable', True),
            ("return {'glb': {}, 'timings': {}}", 'result_invalid', False),
        ]:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                root = self.make_checkout(tmp, 'def run_pipeline(*args, **kwargs):\n    ' + pipeline + '\n')
                with self.assertRaises(WorkerError) as error:
                    OrbitHeadEngine(root).reconstruct(Path(tmp) / 'input', Path(tmp) / 'result',
                                                      lambda _: None, lambda: None)
                self.assertEqual(error.exception.code, code)
                self.assertEqual(error.exception.retryable, retryable)

    def test_video_size_probe_reads_rotation_from_full_stream_metadata(self):
        path = Path(__file__).resolve().parents[1] / 'worker_processors' / 'orbithead' / 'orbithead' / 'frames.py'
        with patch.dict(sys.modules, {'cv2': types.ModuleType('cv2'), 'numpy': types.ModuleType('numpy')}):
            spec = importlib.util.spec_from_file_location('orbithead_frames_under_test', path)
            frames = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(frames)

        calls = []

        def run(cmd, capture_output, text, check):
            calls.append(cmd)

            class Result:
                stdout = '{"streams":[{"width":1920,"height":1080,"side_data_list":[{"rotation":90}]}]}'

            return Result()

        with patch.object(frames.subprocess, 'run', side_effect=run):
            self.assertEqual(frames._video_size('capture.mov'), (1080, 1920, 90))

        self.assertIn('-show_streams', calls[0])
        self.assertNotIn('stream=width,height:stream_side_data=rotation', calls[0])


if __name__ == '__main__':
    unittest.main()
