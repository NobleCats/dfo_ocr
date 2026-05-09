"""Runtime hook for the fully bundled onefile release.

PaddleOCR 3.x imports PaddleX/ModelScope. Recent ModelScope versions may import
torch for optional utilities even though this app uses Paddle inference. The
release excludes torch to reduce size and avoid Torch DLL failures, so provide a
minimal torch stub before app imports begin.
"""

from __future__ import annotations

import sys
import types


def _install_torch_stub() -> None:
    if "torch" in sys.modules:
        return

    torch = types.ModuleType("torch")
    torch.__version__ = "0.0-dfogang-stub"

    distributed = types.ModuleType("torch.distributed")
    distributed.is_available = lambda: False
    distributed.is_initialized = lambda: False
    distributed.get_rank = lambda: 0
    distributed.get_world_size = lambda: 1

    cuda = types.ModuleType("torch.cuda")
    cuda.is_available = lambda: False
    cuda.device_count = lambda: 0

    torch.distributed = distributed
    torch.cuda = cuda
    torch.Tensor = object

    sys.modules["torch"] = torch
    sys.modules["torch.distributed"] = distributed
    sys.modules["torch.cuda"] = cuda


_install_torch_stub()
