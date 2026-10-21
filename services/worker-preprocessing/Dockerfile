# Build context is the REPO ROOT (see manifests/docker-compose.yml) because
# cvops-api, cvops-steps and cvops-worker-common are sibling packages in this
# monorepo, not on PyPI.
FROM python:3.12-slim
WORKDIR /app

RUN pip install --no-cache-dir hatchling

# Install the four packages editable, deps first so layers cache well. Each
# needs its src/ present at install time (hatchling scans for packages).
COPY services/api/pyproject.toml services/api/pyproject.toml
COPY services/api/src/ services/api/src/
RUN pip install --no-cache-dir -e services/api

COPY packages/steps/pyproject.toml packages/steps/pyproject.toml
COPY packages/steps/src/ packages/steps/src/
RUN pip install --no-cache-dir -e packages/steps

COPY packages/worker-common/pyproject.toml packages/worker-common/pyproject.toml
COPY packages/worker-common/src/ packages/worker-common/src/
RUN pip install --no-cache-dir -e packages/worker-common

COPY services/worker-preprocessing/pyproject.toml services/worker-preprocessing/pyproject.toml
COPY services/worker-preprocessing/src/ services/worker-preprocessing/src/
RUN pip install --no-cache-dir -e services/worker-preprocessing

ENV PYTHONUNBUFFERED=1

RUN groupadd --system --gid 1001 app && \
    useradd  --system --uid 1001 --gid app --home /app --shell /usr/sbin/nologin app && \
    chown -R 1001:1001 /app
USER 1001:1001

ENTRYPOINT ["python", "-m", "cvops_worker"]
