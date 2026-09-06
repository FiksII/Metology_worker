# syntax=docker/dockerfile:1
ARG CUDA_PROFILE=cu128

# Lightweight target for checking the worker and SSH without CUDA or model downloads.
FROM python:3.10-slim-bookworm AS worker-only
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FACELIFT_PATH=/opt/worker/worker_processors/FaceLift \
    WORKER_TEMP_ROOT=/var/lib/metology-worker/tmp
WORKDIR /opt/worker
COPY pyproject.toml ./
COPY metology_worker/ ./metology_worker/
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install .
COPY worker_processors/FaceLift/ ./worker_processors/FaceLift/
ENTRYPOINT ["python", "-m", "metology_worker"]
CMD ["run"]

FROM worker-only AS test
COPY tests/ ./tests/
RUN python -m unittest discover -s tests -v

FROM nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04 AS cuda-cu124
ENV CUDA_PROFILE=cu124 DEFAULT_CUDA_ARCH_LIST="7.5;8.0;8.6;8.9;9.0+PTX"

FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04 AS cuda-cu128
ENV CUDA_PROFILE=cu128 DEFAULT_CUDA_ARCH_LIST="10.0;12.0+PTX"

FROM cuda-${CUDA_PROFILE} AS gpu-dependencies
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:${PATH} \
    CUDA_HOME=/usr/local/cuda
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates python3.10 python3.10-venv python3.10-dev \
      build-essential git ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && python3.10 -m venv /opt/venv
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip setuptools wheel
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$CUDA_PROFILE" = cu124 ]; then \
      python -m pip install torch==2.4.1 torchvision==0.19.1 xformers==0.0.28.post1 \
        --index-url https://download.pytorch.org/whl/cu124; \
    else \
      python -m pip install torch==2.7.0 torchvision==0.22.0 xformers==0.0.30 \
        --index-url https://download.pytorch.org/whl/cu128; \
    fi
COPY requirements-facelift.txt /tmp/requirements-facelift.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install -r /tmp/requirements-facelift.txt ninja \
    && python -m pip install facenet-pytorch==2.6.0 --no-deps
# BuildKit has no GPU: explicitly specify architectures for the CUDA extension.
ARG CUDA_ARCH_LIST
ARG MAX_JOBS=2
ARG RASTERIZER_REF=59f5f77e3ddbac3ed9db93ec2cfe99ed6c5d121d
RUN --mount=type=cache,target=/root/.cache/pip \
    TORCH_CUDA_ARCH_LIST="${CUDA_ARCH_LIST:-$DEFAULT_CUDA_ARCH_LIST}" MAX_JOBS="$MAX_JOBS" \
    python -m pip install --no-build-isolation \
      "git+https://github.com/graphdeco-inria/diff-gaussian-rasterization@${RASTERIZER_REF}"

FROM gpu-dependencies AS gpu
ENV FACELIFT_PATH=/opt/worker/worker_processors/FaceLift \
    WORKER_TEMP_ROOT=/var/lib/metology-worker/tmp \
    HF_HOME=/root/.cache/huggingface \
    U2NET_HOME=/root/.cache/u2net
WORKDIR /opt/worker
COPY pyproject.toml ./
COPY metology_worker/ ./metology_worker/
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install . \
    && python -m pip freeze > /opt/worker/installed-requirements.txt
COPY worker_processors/FaceLift/ ./worker_processors/FaceLift/
RUN test -f "$FACELIFT_PATH/inference.py" \
    && test -f "$FACELIFT_PATH/mvdiffusion/data/fixed_prompt_embeds_6view/clr_embeds.pt" \
    && mkdir -p "$FACELIFT_PATH/checkpoints" "$WORKER_TEMP_ROOT" /root/.cache
ENTRYPOINT ["python", "-m", "metology_worker"]
CMD ["run"]
