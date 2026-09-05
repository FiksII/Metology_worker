#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TASK_ROOT"
PROFILE="${1:-worker-only}"
case "$PROFILE" in
  worker-only|cu124|cu128) ;;
  *) echo 'Usage: bash scripts/install.sh [worker-only|cu124|cu128]' >&2; exit 2 ;;
esac
PYTHON_BIN="${PYTHON_BIN:-python3.10}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info[:2] == (3, 10), "Python 3.10 required"'
if [[ "$PROFILE" != worker-only ]]; then
  for tool in nvcc ffmpeg git g++; do
    command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 1; }
  done
  [[ -f worker_processors/FaceLift/inference.py ]] || { echo 'FaceLift checkout missing' >&2; exit 1; }
  CUDA_RELEASE=12.4
  [[ "$PROFILE" == cu128 ]] && CUDA_RELEASE=12.8
  nvcc --version | grep -q "release $CUDA_RELEASE," || {
    echo "Select CUDA toolkit $CUDA_RELEASE through PATH/CUDA_HOME before installation." >&2; exit 1;
  }
fi
if [[ ! -d .venv ]]; then
  "$PYTHON_BIN" -m venv .venv
fi
.venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 10), "Existing .venv must use Python 3.10"'
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -e '.[dev]'
if [[ "$PROFILE" != worker-only ]]; then
  if [[ "$PROFILE" == cu124 ]]; then
    .venv/bin/python -m pip install torch==2.4.0 torchvision==0.19.0 xformers==0.0.27.post2 \
      --index-url https://download.pytorch.org/whl/cu124
  else
    .venv/bin/python -m pip install torch==2.7.0 torchvision==0.22.0 xformers==0.0.30 \
      --index-url https://download.pytorch.org/whl/cu128
  fi
  .venv/bin/python -m pip install -r requirements-facelift.txt ninja
  # Upstream pins old torch in metadata; FaceLift itself also installs this with --no-deps.
  .venv/bin/python -m pip install facenet-pytorch==2.6.0 --no-deps
  # Same rasterizer repository as FaceLift/setup_env.sh; record the resolved commit below.
  .venv/bin/python -m pip install --no-build-isolation \
    git+https://github.com/graphdeco-inria/diff-gaussian-rasterization
fi
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip freeze > installed-requirements.txt
echo 'Installed. Activate with: source .venv/bin/activate'
echo 'Next: python -m metology_worker doctor'
