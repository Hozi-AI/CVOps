FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Preinstall the common ML stack GLOBALLY so each train's `--system-site-packages`
# venv inherits it and only layers repo-specific extras on top — keeping per-run
# installs small. Most trainers use ultralytics (pulls torch) + mlflow. Kept in its
# own cache layer so source changes below don't trigger a multi-GB reinstall.
# NOTE: CPU wheels by default; for GPU swap to an nvidia/cuda base + torch CUDA index.
RUN pip install --no-cache-dir \
      "mlflow>=2.12,<3" \
      "ultralytics>=8.2,<9"

COPY packages/worker-common ./packages/worker-common
COPY services/api ./services/api
RUN pip install --no-cache-dir ./packages/worker-common ./services/api

# cvops-steps holds the canonical step.train implementation, shared with the
# API registry. Sibling monorepo package, not on PyPI.
COPY packages/steps ./packages/steps
RUN pip install --no-cache-dir ./packages/steps

COPY services/worker-training ./services/worker-training
RUN pip install --no-cache-dir ./services/worker-training

# Run as an unprivileged user. step.train needs no Docker (git clone + a
# throwaway venv under the temp dir), only a writable HOME for git/pip/ultralytics
# caches. Global site-packages stay root-owned and read-only to this user.
ENV HOME=/app
RUN groupadd --system --gid 1001 app && \
    useradd  --system --uid 1001 --gid app --home /app --shell /usr/sbin/nologin app && \
    chown -R 1001:1001 /app
USER 1001:1001

CMD ["python", "-m", "worker_training.main"]
