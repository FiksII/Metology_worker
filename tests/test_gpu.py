"""Opt-in integration check: WORKER_TEST_GPU=1, with the full CUDA environment."""
import inspect
import os
import sys
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.environ.get('WORKER_TEST_GPU') == '1', 'requires an NVIDIA GPU and FaceLift dependencies')
class GPUIntegrationTests(unittest.TestCase):
    def test_attention_rasterizer_and_facelift_import(self):
        import torch
        import xformers.ops
        from diff_gaussian_rasterization import GaussianRasterizationSettings, GaussianRasterizer

        from metology_worker.config import Config
        from metology_worker.facelift import FaceLiftEngine

        self.assertTrue(torch.cuda.is_available())
        root = Config.load().facelift_path
        original_path = sys.path[:]
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), original_path))
        sys.path.insert(0, str(root))
        import inference
        self.assertIn('ply_only', inspect.signature(inference.process_single_image).parameters)

        class ModelLoadingBoundary(Exception):
            pass

        # Exercise real engine startup, but stop before downloading/loading large weights.
        with patch.object(inference, 'get_model_paths', side_effect=ModelLoadingBoundary):
            with self.assertRaises(ModelLoadingBoundary):
                FaceLiftEngine(root)
        # xformers 0.0.30 used to select a Hopper-only Flash3 kernel on RTX 50.
        # Compare real GPU attention with the reference math, not just an import.
        q, k, v = [torch.randn(1, 16, 1, 64, device='cuda', dtype=torch.float16) for _ in range(3)]
        actual = xformers.ops.memory_efficient_attention(q, k, v)
        scores = q[:, :, 0].float() @ k[:, :, 0].float().transpose(-1, -2) / 8
        expected = (scores.softmax(-1) @ v[:, :, 0].float()).unsqueeze(2)
        torch.testing.assert_close(actual.float(), expected, atol=2e-3, rtol=2e-3)

        settings = GaussianRasterizationSettings(
            image_height=16, image_width=16, tanfovx=1.0, tanfovy=1.0,
            bg=torch.zeros(3, device='cuda'), scale_modifier=1.0,
            viewmatrix=torch.eye(4, device='cuda'), projmatrix=torch.eye(4, device='cuda'),
            sh_degree=0, campos=torch.zeros(3, device='cuda'), prefiltered=False, debug=False,
        )
        pixels, radii = GaussianRasterizer(raster_settings=settings)(
            means3D=torch.tensor([[0., 0., 2.]], device='cuda'),
            means2D=torch.zeros(1, 3, device='cuda'),
            colors_precomp=torch.ones(1, 3, device='cuda'),
            opacities=torch.ones(1, 1, device='cuda'),
            scales=torch.full((1, 3), 0.1, device='cuda'),
            rotations=torch.tensor([[1., 0., 0., 0.]], device='cuda'),
        )
        torch.cuda.synchronize()
        self.assertEqual(pixels.shape, (3, 16, 16))
        self.assertTrue(torch.isfinite(pixels).all())
        self.assertGreater(radii[0].item(), 0)
        self.assertGreater(pixels.sum().item(), 0)
