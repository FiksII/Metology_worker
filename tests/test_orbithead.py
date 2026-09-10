import sys
import tempfile
import unittest
from pathlib import Path

from metology_worker.orbithead import OrbitHeadEngine


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


if __name__ == '__main__':
    unittest.main()
