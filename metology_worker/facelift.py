"""Adapter to the separately versioned FaceLift checkout."""
import importlib
import inspect
import logging
import shutil
import sys
import warnings
from pathlib import Path

from .runtime import WorkerError

LOG = logging.getLogger(__name__)


def configure_attention_backend(torch):
    """Avoid xformers 0.0.30 dispatching Hopper-only Flash3 kernels on Blackwell."""
    if torch.cuda.get_device_capability(0)[0] < 10:
        return
    import xformers
    if xformers.__version__.split('+', 1)[0] == '0.0.30':
        from xformers.ops.fmha.dispatch import _set_use_fa3
        _set_use_fa3(False)
        LOG.info('xformers_flash3_disabled_for_blackwell')


class FaceLiftEngine:
    def __init__(self, root):
        root = Path(root).resolve()
        if not (root / 'inference.py').is_file():
            raise ValueError('facelift_checkout_missing')
        sys.path.insert(0, str(root))
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError('cuda_unavailable')
        self.torch = torch
        configure_attention_backend(torch)
        self.module = importlib.import_module('inference')
        if Path(self.module.__file__).resolve() != root / 'inference.py':
            raise RuntimeError('wrong_inference_module')
        if 'ply_only' not in inspect.signature(self.module.process_single_image).parameters:
            raise RuntimeError('facelift_worker_patch_required')
        device = torch.device('cuda:0')
        diffusion, gslrm, config = self.module.get_model_paths()
        self.pipeline, self.generator, self.embedding = self.module.initialize_mvdiffusion_pipeline(diffusion, device)
        self.model = self.module.initialize_gslrm_model(gslrm, config, device)
        self.model.eval()
        self.intrinsics, self.extrinsics = self.module.setup_camera_parameters(device)

    def reconstruct(self, source, target, on_stage, check_cancel):
        from PIL import Image, ImageOps, UnidentifiedImageError
        from pillow_heif import register_heif_opener
        register_heif_opener()
        check_cancel()
        normalized = source.parent / 'normalized.png'
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(source) as image:
                    if image.format not in {'JPEG', 'PNG', 'HEIF', 'HEIC'}:
                        raise WorkerError('input_not_supported', False)
                    ImageOps.exif_transpose(image).convert('RGB').save(normalized)
        except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, ValueError):
            raise WorkerError('input_not_supported', False) from None
        except OSError as error:
            if error.errno == 28:
                raise
            raise WorkerError('input_not_supported', False) from None
        try:
            with self.torch.inference_mode():
                self.generator.manual_seed(4)
                output = self.module.process_single_image(
                    normalized.name, str(normalized.parent), str(source.parent / 'facelift'),
                    True, self.pipeline, self.generator, self.embedding, self.model,
                    self.intrinsics, self.extrinsics, 3.0, 50,
                    ply_only=True, on_stage=on_stage, check_cancel=check_cancel,
                )
            check_cancel()
            shutil.move(output, target)
        except self.torch.cuda.OutOfMemoryError:
            self.torch.cuda.empty_cache()
            raise WorkerError('model_memory_exhausted', True) from None
