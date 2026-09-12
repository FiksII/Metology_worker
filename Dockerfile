# syntax=docker/dockerfile:1
ARG CUDA_PROFILE=cu128

# Lightweight target for checking the worker and SSH without CUDA or model downloads.
FROM python:3.10-slim-bookworm AS worker-only
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FACELIFT_PATH=/opt/worker/worker_processors/FaceLift \
    ORBITHEAD_PATH=/opt/worker/worker_processors/orbithead \
    WORKER_TEMP_ROOT=/var/lib/metology-worker/tmp
WORKDIR /opt/worker
COPY pyproject.toml ./
COPY metology_worker/ ./metology_worker/
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install .
COPY worker_processors/FaceLift/ ./worker_processors/FaceLift/
COPY worker_processors/orbithead/ ./worker_processors/orbithead/
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
      cmake ninja-build pkg-config libboost-all-dev libeigen3-dev \
      libflann-dev libfreeimage-dev libmetis-dev libgoogle-glog-dev \
      libgflags-dev libsqlite3-dev libglew-dev libceres-dev libcgal-dev \
      libcurl4-openssl-dev libssl-dev libopencv-dev libnanoflann-dev \
      libglu1-mesa-dev libglfw3-dev libpng-dev libjpeg-dev libtiff-dev libegl1 \
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
ARG ORBITHEAD_CUDA_ARCH=86
ARG ORBITHEAD_BUILD_JOBS=2
RUN git clone --branch 3.12.6 --depth 1 https://github.com/colmap/colmap.git /tmp/colmap \
    && cmake -S /tmp/colmap -B /tmp/colmap/build -GNinja -DCMAKE_BUILD_TYPE=Release \
      -DGUI_ENABLED=OFF -DCMAKE_CUDA_ARCHITECTURES=${ORBITHEAD_CUDA_ARCH} -DCUDA_ENABLED=ON \
    && cmake --build /tmp/colmap/build -j"${ORBITHEAD_BUILD_JOBS}" \
    && cmake --install /tmp/colmap/build \
    && rm -rf /tmp/colmap
RUN git clone --depth 1 https://github.com/cdcseacave/VCG.git /opt/vcglib \
    && git clone --branch v2.3.0 --depth 1 https://github.com/cdcseacave/openMVS.git /tmp/openMVS \
    && cmake -S /tmp/openMVS -B /tmp/openMVS/build -DCMAKE_BUILD_TYPE=Release \
      -DVCG_ROOT=/opt/vcglib -DOpenMVS_USE_CUDA=ON \
      -DCMAKE_CUDA_ARCHITECTURES=${ORBITHEAD_CUDA_ARCH} \
      -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc -DOpenMVS_USE_OPENMP=ON \
      -DOpenMVS_USE_PYTHON=OFF -DOpenMVS_ENABLE_TESTS=OFF -DCMAKE_INSTALL_PREFIX=/opt/openmvs \
      -DCUDA_CUDA_LIBRARY=/usr/local/cuda/lib64/stubs/libcuda.so \
      "-DCMAKE_CXX_FLAGS=-U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0" \
      "-DCMAKE_C_FLAGS=-U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0" \
    && cmake --build /tmp/openMVS/build -j"${ORBITHEAD_BUILD_JOBS}" \
    && cmake --install /tmp/openMVS/build \
    && rm -rf /tmp/openMVS

FROM gpu-dependencies AS gpu
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV FACELIFT_PATH=/opt/worker/worker_processors/FaceLift \
    ORBITHEAD_PATH=/opt/worker/worker_processors/orbithead \
    OPENMVS_BIN=/opt/openmvs/bin/OpenMVS \
    WORKER_TEMP_ROOT=/var/lib/metology-worker/tmp \
    HF_HOME=/root/.cache/huggingface \
    U2NET_HOME=/root/.cache/u2net \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics
WORKDIR /opt/worker
COPY pyproject.toml ./
COPY metology_worker/ ./metology_worker/
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install .
COPY worker_processors/FaceLift/ ./worker_processors/FaceLift/
COPY worker_processors/orbithead/ ./worker_processors/orbithead/
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip freeze > /opt/worker/installed-requirements.txt
# OrbitHead has its own Python 3.12 / Torch / CUDA dependencies; do not mix with FaceLift.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --project /opt/worker/worker_processors/orbithead --frozen --no-dev --extra gpu --extra da3
RUN /opt/worker/worker_processors/orbithead/.venv/bin/python -c \
    "from rembg import new_session; new_session('birefnet-portrait'); new_session('u2net_human_seg')"
RUN test -f "$FACELIFT_PATH/inference.py" \
    && test -f "$ORBITHEAD_PATH/orbithead/pipeline.py" \
    && test -f "$FACELIFT_PATH/mvdiffusion/data/fixed_prompt_embeds_6view/clr_embeds.pt" \
    && test -f "$OPENMVS_BIN/DensifyPointCloud" \
    && mkdir -p "$FACELIFT_PATH/checkpoints" "$WORKER_TEMP_ROOT" /root/.cache
ENTRYPOINT ["python", "-m", "metology_worker"]
CMD ["run"]
