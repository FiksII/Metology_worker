"""Run the actual single-image function with CPU doubles at the GPU boundary."""
import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


class FaceLiftTests(unittest.TestCase):
    def function(self):
        path = Path(__file__).resolve().parents[1] / 'worker_processors/FaceLift/inference.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'process_single_image')
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), fn], type_ignores=[])
        import os
        namespace = {'os': os, 'print': lambda *a: None, 'DEFAULT_IMG_SIZE': 512}
        for name in ['Image', 'np', 'torch', 'preprocess_image', 'preprocess_image_without_cropping',
                     'remove', 'rearrange', 'edict', 'render_turntable', 'imageseq2video']:
            namespace[name] = MagicMock()
        exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
        return namespace

    def test_ply_only_returns_before_video_and_reports_export(self):
        ns = self.function()
        pipeline = MagicMock()
        pipeline.return_value.images = [MagicMock() for _ in range(6)]
        model = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            stages = []
            result = ns['process_single_image'](
                'input.png', tmp, tmp, True, pipeline, MagicMock(), MagicMock(),
                model, MagicMock(), MagicMock(), 3.0, 50,
                ply_only=True, on_stage=stages.append, check_cancel=lambda: None,
            )
            self.assertEqual(result, str(Path(tmp) / 'input/gaussians.ply'))
        self.assertEqual(stages, ['processing', 'exporting_ply'])
        ns['render_turntable'].assert_not_called()
        ns['imageseq2video'].assert_not_called()

    def test_cancel_before_processing_does_not_invoke_model(self):
        ns = self.function()
        pipeline = MagicMock()
        def stop():
            raise InterruptedError('cancel')
        with self.assertRaises(InterruptedError):
            ns['process_single_image']('input.png', '.', '.', True, pipeline,
                MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), 3.0, 50,
                ply_only=True, check_cancel=stop)
        pipeline.assert_not_called()
