import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from metology_worker.facelift import FaceLiftEngine
from metology_worker.runtime import WorkerError


HAS_IMAGES = all(importlib.util.find_spec(name) for name in ['PIL', 'pillow_heif'])


@unittest.skipUnless(HAS_IMAGES, 'Install worker dependencies for image codec tests')
class ImageAdapterTests(unittest.TestCase):
    def engine(self):
        from PIL import Image
        engine = FaceLiftEngine.__new__(FaceLiftEngine)
        engine.torch = MagicMock()
        engine.torch.cuda.OutOfMemoryError = MemoryError
        engine.generator = MagicMock()
        engine.pipeline = engine.embedding = engine.model = None
        engine.intrinsics = engine.extrinsics = None
        def process(name, source, output, *args, **kwargs):
            with Image.open(Path(source) / name) as image:
                self.assertEqual(image.format, 'PNG')
                self.assertEqual(image.mode, 'RGB')
            path = Path(output) / 'gaussians.ply'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'ply\nformat binary_little_endian 1.0\nend_header\n1')
            return str(path)
        engine.module = SimpleNamespace(process_single_image=process)
        return engine

    def test_supported_formats_are_decoded_from_extensionless_input(self):
        from PIL import Image
        from pillow_heif import from_pillow
        with tempfile.TemporaryDirectory() as tmp:
            for fmt in ['JPEG', 'PNG', 'HEIF']:
                with self.subTest(format=fmt):
                    root = Path(tmp) / fmt
                    root.mkdir()
                    source, target = root / 'input', root / 'result.ply'
                    photo = Image.new('RGB', (16, 16), 'blue')
                    if fmt == 'HEIF':
                        from_pillow(photo).save(source)
                    else:
                        photo.save(source, format=fmt)
                    self.engine().reconstruct(source, target, lambda s: None, lambda: None)
                    self.assertTrue(target.read_bytes().startswith(b'ply\n'))

    def test_unsupported_gif_is_permanent_error(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'input'
            Image.new('RGB', (16, 16)).save(source, format='GIF')
            with self.assertRaises(WorkerError) as error:
                self.engine().reconstruct(source, Path(tmp) / 'result.ply', lambda s: None, lambda: None)
            self.assertEqual(error.exception.code, 'input_not_supported')
            self.assertFalse(error.exception.retryable)
