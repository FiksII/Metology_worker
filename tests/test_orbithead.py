import importlib.util
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

    def tearDown(self):
        sys.path[:] = self.path
        sys.modules.pop('orbithead', None)
        sys.modules.pop('orbithead.pipeline', None)

    def test_reconstruct_moves_pipeline_glb_to_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'checkout'
            package = root / 'orbithead'
            package.mkdir(parents=True)
            (package / '__init__.py').write_text('', encoding='utf-8')
            (package / 'pipeline.py').write_text(
                "from pathlib import Path\n"
                "def run_pipeline(video, out_dir, preview, keep_intermediate):\n"
                "    out = Path(out_dir)\n"
                "    out.mkdir()\n"
                "    glb = out / 'head.glb'\n"
                "    glb.write_bytes(b'glTF' + (2).to_bytes(4, 'little') + (12).to_bytes(4, 'little'))\n"
                "    return {'glb': {'png': str(glb)}}\n",
                encoding='utf-8',
            )
            source = Path(tmp) / 'capture.mov'
            target = Path(tmp) / 'result.glb'
            source.write_bytes(b'video')

            stages = []
            OrbitHeadEngine(root).reconstruct(source, target, stages.append, lambda: None)

            self.assertEqual(stages, ['exporting_ply'])
            self.assertTrue(target.read_bytes().startswith(b'glTF'))

    def test_ffprobe_failure_reports_input_not_supported_with_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'checkout'
            package = root / 'orbithead'
            package.mkdir(parents=True)
            (package / '__init__.py').write_text('', encoding='utf-8')
            (package / 'pipeline.py').write_text(
                "import subprocess\n"
                "def run_pipeline(video, out_dir, preview, keep_intermediate):\n"
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

    def test_video_size_probe_uses_ffprobe_4_side_data_section(self):
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

        self.assertIn('stream=width,height:stream_side_data_list=rotation', calls[0])
        self.assertNotIn('stream=width,height:stream_side_data=rotation', calls[0])


if __name__ == '__main__':
    unittest.main()
