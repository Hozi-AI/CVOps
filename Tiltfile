# -*- mode: Python -*-
# Tiltfile for CVOps — inner-loop developer environment.
#
# Model:
#   • Stateful infra (postgres, redis, garage) → containers via docker_compose.
#   • App code (api, frontend)                → host processes via local_resource
#                                                (uvicorn --reload, npm run dev).
#   • Edge nginx (container)                   → proxies / → host Vite dev (with
#                                                HMR) and /api/v1 → host API, so
#                                                the real app is browser-usable at
#                                                http://localhost (and dev VMs).
#   Vite also proxies /api/v1 → http://localhost:8000 for the React app.
#
# For container-based pre-prod testing, use docker compose with profiles directly:
#   cd manifests
#   docker compose --profile app up                       # prod-target stack + worker-preprocessing
#   docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile app up
#
# Usage:
#   tilt up                       # postgres, redis, garage, api (host), frontend (host)
#   tilt down                     # stop everything
#
# Docs: https://docs.tilt.dev/

# ── Host prerequisites ──────────────────────────────────────────────────────
# Fail loud if the host can't run the app processes.
def require(cmd, hint):
    result = str(local('command -v %s >/dev/null 2>&1 && echo ok || echo missing' % cmd, quiet=True))
    if 'ok' not in result:
        fail('Missing host tool: %s\n  → %s' % (cmd, hint))

require('python3',  'install Python 3.12+ (mise/pyenv/system)')
require('node',     'install Node 20+')
require('npm',      'usually bundled with node')
require('docker',   'install Docker — needed for the infra containers')
require('openssl',  'install openssl — needed to generate dev secrets')

# ── Bootstrap manifests/.env from manifests/.env.example ────────────────────
if not os.path.exists('manifests/.env'):
    print('⚠  No manifests/.env file found — copying manifests/.env.example. Edit secrets before going live.')
    local('cp manifests/.env.example manifests/.env', quiet=True)

# Reconcile: append any keys present in .env.example but MISSING from an existing
# .env. The bootstrap copy above only fires when .env is absent entirely, so a
# dev whose .env predates a newly-added var would otherwise never receive it
# (the symptom: "some vars aren't generated"). Newly-appended change_me
# placeholders get filled by the generator immediately below.
local('''
    cd manifests
    [ -f .env.example ] || exit 0
    added=0
    while IFS= read -r line; do
        case "$line" in ''|\\#*) continue ;; *=*) ;; *) continue ;; esac
        key=${line%%=*}
        if ! grep -qE "^${key}=" .env; then
            echo "$line" >> .env
            added=1
        fi
    done < .env.example
    [ "$added" = 1 ] && echo "⚠  Appended missing keys from .env.example to manifests/.env"
    exit 0
''', quiet=False)

# Replace any leftover `change_me*` placeholders with freshly-generated secrets.
# A copied-but-unedited .env carries `GARAGE_RPC_SECRET=change_me_rpc_secret_hex_32_bytes`,
# which Garage rejects at startup ("Invalid RPC secret key: expected 32 bytes of
# random hex"). Idempotent — only placeholder values are touched, and POSTGRES_PASSWORD
# is left alone so it stays in sync with the DATABASE_URL string in the same file.
local('''
    cd manifests
    grep -q change_me .env || exit 0
    tmp=$(mktemp)
    while IFS= read -r line; do
        case "$line" in
            POSTGRES_PASSWORD=*|DATABASE_URL=*|POSTGRES_EXPORTER_DSN=*)
                echo "$line" ;;
            GARAGE_DEFAULT_ACCESS_KEY=GKchange_me*)
                echo "GARAGE_DEFAULT_ACCESS_KEY=GK$(openssl rand -hex 12)" ;;
            *=change_me*)
                echo "${line%%=*}=$(openssl rand -hex 32)" ;;
            *)
                echo "$line" ;;
        esac
    done < .env > "$tmp"
    # Only replace .env when something actually changed. POSTGRES_PASSWORD /
    # DATABASE_URL keep their `change_me_postgres` value by design, so `grep
    # change_me` stays true forever — an unconditional `mv` would rewrite .env on
    # every Tiltfile parse, churn its mtime, and (since .env is a read_file dep +
    # the compose env_file) trigger an endless re-parse + rebuild loop.
    if cmp -s "$tmp" .env; then
        rm -f "$tmp"
    else
        mv "$tmp" .env
        echo "⚠  Generated dev secrets for change_me placeholders in manifests/.env"
    fi
''', quiet=False)

# Parse .env into a dict so we can build localhost-rewritten connection strings
# for the host-side api and frontend processes.
def load_env(path):
    out = {}
    for line in str(read_file(path)).splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        out[k.strip()] = v.strip()
    return out

env = load_env('manifests/.env')

# Stale-env guard: catches both missing GARAGE_* (pre-Garage MinIO era) and
# any placeholder still on disk that would break Garage at startup.
if 'GARAGE_RPC_SECRET' not in env or 'MINIO_ROOT_USER' in str(read_file('manifests/.env')):
    fail("""manifests/.env is stale (missing GARAGE_* vars or still has MINIO_*).
  Diff against manifests/.env.example, then regenerate secrets with:
    openssl rand -hex 32                # for each *_SECRET / *_TOKEN / JWT_SECRET / WORKER_TOKEN
    echo "GK$(openssl rand -hex 12)"    # for GARAGE_DEFAULT_ACCESS_KEY""")

# Helper: pull a required key from .env or fail.
def envreq(key):
    if key not in env:
        fail('manifests/.env is missing required key: %s' % key)
    return env[key]

# ── Optional heavy stacks (OFF by default for low-powered machines) ─────────
# CVAT (labelling) and the training/MLflow stack are the expensive parts of the
# system:
#   • CVAT pulls ~10 containers (db, two redis, clickhouse, opa, traefik, ui,
#     server, eight workers) plus a git-submodule checkout and a nuctl download.
#   • worker-training carries a multi-GB torch/ultralytics image, and MLflow is
#     only useful once training runs exist.
# Both are SKIPPED by default, so a plain `tilt up` brings up just the core
# stack (postgres, redis, garage, api, frontend, preprocessing worker, nginx).
# Opt in per-run or persistently:
#   tilt up -- --cvat            # CVAT stack for this run
#   tilt up -- --training        # training worker + MLflow for this run
#   tilt up -- --heavy           # both
#   export CVOPS_ENABLE_CVAT=1       # persistent (shell env, or manifests/.env)
#   export CVOPS_ENABLE_TRAINING=1
config.define_bool('cvat')
config.define_bool('training')
config.define_bool('heavy')
cfg = config.parse()

def _truthy(v):
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')

def feature_enabled(flag):
    # CLI flag (or --heavy) wins; otherwise fall back to a CVOPS_ENABLE_<FLAG>
    # env var, looked up both in the real shell env and in manifests/.env.
    if cfg.get('heavy') or cfg.get(flag):
        return True
    key = 'CVOPS_ENABLE_%s' % flag.upper()
    return _truthy(os.getenv(key, env.get(key, '')))

ENABLE_CVAT = feature_enabled('cvat')
ENABLE_TRAINING = feature_enabled('training')

print('CVOps optional stacks → CVAT: %s | training+MLflow: %s' % (
    'on' if ENABLE_CVAT else 'off (use --cvat or CVOPS_ENABLE_CVAT=1)',
    'on' if ENABLE_TRAINING else 'off (use --training or CVOPS_ENABLE_TRAINING=1)',
))

# ── Infra containers (docker compose) ───────────────────────────────────────
# Two separate compose projects so that CVAT failures never affect the main
# infra (postgres, redis, garage, nginx). If the cvops-cvat project has issues
# (disk full, Docker daemon problems, etc.) the API and frontend stay up.
# Main project: base infra (postgres, redis, garage, mlflow, nginx) only — the
# CVAT stack lives in the separate `cvops-cvat` project below, so its services
# are NOT loaded here (loading the override in both projects would double-declare
# every CVAT dc_resource, e.g. cvat_clickhouse).
# Activate the `worker` + `mlflow` profiles ONLY when training is enabled, so
# worker-training (the `training` queue consumer) and the MLflow tracking server
# come up as containers. worker-training carries the heavy ML stack
# (torch/ultralytics) on its image, so it can't run as a host process like the
# other workers; MLflow is its tracking backend. With training off, neither is
# loaded — the core stack stays lightweight. The `worker` profile pulls ONLY
# worker-training, not the `app`-profile services (api/frontend run on the host),
# so it doesn't collide with the host processes.
main_profiles = []
if ENABLE_TRAINING:
    main_profiles = main_profiles + ['worker', 'mlflow']

docker_compose(
    ['manifests/docker-compose.yml'],
    env_file='manifests/.env',
    project_name='cvops',
    profiles=main_profiles,
)

# cvat_cvat is declared `external` in the override, so it must pre-exist before
# compose attaches to it. Create it idempotently here (and start_env.sh does the
# same). External means compose adopts the existing network as-is and does NOT
# check/manage its labels — which is what lets a network created here (or by
# nuctl/start_env) be shared across the cvops-cvat compose project, nuclio, and
# CVAT without the "incorrect label com.docker.compose.network" conflict.
#
# The entire CVAT compose project is loaded only when CVAT is enabled — gating
# it here keeps its ~10 containers (and the external-network requirement) off a
# default `tilt up`.
if ENABLE_CVAT:
    local('docker network inspect cvat_cvat >/dev/null 2>&1 || docker network create cvat_cvat', quiet=True)

    docker_compose(
        ['manifests/docker-compose.override.yml'],
        env_file='manifests/.env',
        project_name='cvops-cvat',
    )

dc_resource('postgres',
    labels=['1-infra'],
    links=[link('postgres://localhost:5432', 'pg')],
)

dc_resource('garage-init',
    labels=['1-infra'],
)

dc_resource('garage',
    labels=['1-infra'],
    resource_deps=['garage-init'],
    links=[
        link('http://localhost:3900', 's3 endpoint'),
        link('http://localhost:3903', 'admin api'),
    ],
)

dc_resource('redis',
    labels=['1-infra'],
    links=[link('redis://localhost:6379', 'redis')],
)

# MLflow tracking server (+ one-shot DB create). Gated to the `mlflow` compose
# profile (activated only when training is on, above), so they're loaded only
# then — dc_resource on an unloaded service errors, hence the guard.
if ENABLE_TRAINING:
    dc_resource('mlflow-init',
        labels=['2-app'],
        resource_deps=['postgres'],
    )

    dc_resource('mlflow',
        labels=['2-app'],
        resource_deps=['mlflow-init', 'garage-bootstrap'],
        links=[link('http://localhost:5000', 'mlflow ui')],
    )

# ── CVAT stack (from docker-compose.override.yml) ───────────────────────────
# Everything CVAT-related lives behind ENABLE_CVAT: the external network, the
# docker-socket widening (needed by nuctl/the CVAT worker), the submodule
# checkout, and every cvat_* container. With CVAT off none of it is declared,
# and dc_resource on an unloaded compose service would error anyway.
if ENABLE_CVAT:
    # cvat_cvat is declared `external: true` in docker-compose.override.yml — it
    # must exist before any CVAT container starts. This is idempotent.
    local_resource('cvat-network',
        cmd='docker network inspect cvat_cvat >/dev/null 2>&1 || docker network create cvat_cvat',
        labels=['1-infra'],
    )

    # nuctl talks directly to /var/run/docker.sock. The socket is owned by the
    # `docker` group, so only members can access it. Group membership only takes
    # effect in a fresh login shell — processes started before `usermod` (including
    # Tilt itself) won't see the new group. Widening the socket permissions here
    # ensures nuctl and the CVAT worker always have Docker access on any machine,
    # without requiring a logout/re-login cycle.
    #
    # Skip the chmod entirely if the socket is already writable (the common case —
    # user is in the `docker` group, or perms were widened on a prior run). Only
    # then reach for sudo, and use `sudo -n` so Tilt never hangs on a hidden
    # password prompt — if passwordless sudo isn't available, fail with an
    # actionable message instead of a stuck build.
    local_resource('docker-socket-perms',
        cmd='test -w /var/run/docker.sock || sudo -n chmod 666 /var/run/docker.sock || ' +
            '{ echo "docker socket not writable and passwordless sudo unavailable." >&2; ' +
            'echo "Run once: sudo chmod 666 /var/run/docker.sock  (or: sudo usermod -aG docker $USER && re-login)" >&2; exit 1; }',
        labels=['1-infra'],
    )

    # CVAT's compose bind-mounts config files (vector.toml, grafana_conf.yml, …)
    # from services/cvat/components/analytics/. These were vendored manually
    # (see docs/09-gaps-and-considerations.md §F) because the git submodule
    # cannot be fetched reliably. We just verify the files exist here.
    local_resource('cvat-submodule',
        cmd='test -f services/cvat/components/analytics/vector/vector.toml && test -f services/cvat/components/analytics/grafana_conf.yml || { echo "CVAT config files missing — see docs/09-gaps-and-considerations.md section F"; exit 1; }',
        labels=['1-infra'],
    )

    dc_resource('cvat_db',             labels=['3-cvat'], resource_deps=['cvat-network', 'cvat-submodule'])
    dc_resource('cvat_redis_inmem',    labels=['3-cvat'], resource_deps=['cvat-network', 'cvat-submodule'])
    dc_resource('cvat_redis_ondisk',   labels=['3-cvat'], resource_deps=['cvat-network', 'cvat-submodule'])
    dc_resource('cvat_clickhouse',     labels=['3-cvat'], resource_deps=['cvat-network', 'cvat-submodule'])
    dc_resource('cvat_opa',            labels=['3-cvat'], resource_deps=['cvat-network', 'cvat-submodule'])
    dc_resource('cvat_server',         labels=['3-cvat'], resource_deps=['cvat-network', 'cvat-submodule'])

    # The CVAT superuser is bootstrapped by the `cvat_admin_init` compose service
    # (docker-compose.override.yml) — an idempotent get_or_create + set_password that
    # runs under both `tilt up` and `docker compose --profile app`. We deliberately
    # do NOT also run a `cvat-superuser` local_resource here: the two raced (whichever
    # created the admin first made the other's `createsuperuser` loop spin), which
    # left the resource stuck in_progress and blocked anything depending on it.
    dc_resource('cvat_ui',             labels=['3-cvat'])
    dc_resource('traefik',
        labels=['3-cvat'],
        resource_deps=['cvat-submodule'],
        links=[link('http://localhost:8080', 'cvat')],
    )
    dc_resource('cvat_worker_utils',           labels=['3-cvat'])
    dc_resource('cvat_worker_import',          labels=['3-cvat'])
    dc_resource('cvat_worker_export',          labels=['3-cvat'])
    dc_resource('cvat_worker_annotation',      labels=['3-cvat'])
    dc_resource('cvat_worker_webhooks',        labels=['3-cvat'])
    dc_resource('cvat_worker_quality_reports', labels=['3-cvat'])
    dc_resource('cvat_worker_chunks',          labels=['3-cvat'])
    dc_resource('cvat_worker_consensus',       labels=['3-cvat'])
    # nuclio is required for YOLO model deployment; cvat_grafana (analytics UI) is
    # still profile-gated to `app`/`all` and skipped by the Tilt inner loop.
    # cvat_vector is profile-less, so it loads and is labelled.
    dc_resource('nuclio',              labels=['3-cvat'], resource_deps=['cvat-network', 'cvat-submodule'])
    dc_resource('cvat_vector',         labels=['3-cvat'], resource_deps=['cvat-submodule'])

# Training-queue worker (container — torch/ultralytics live on its image, so it
# can't be a host process). Consumes the `training` stream: clones the trainer
# repo, runs it against the exported dataset, logs to MLflow, writes a
# ModelVersion. Comes up under the `worker` compose profile (activated only when
# training is enabled). First `tilt up --` with training on builds the image
# (heavy ML deps) — which is exactly why it's opt-in.
if ENABLE_TRAINING:
    dc_resource('worker-training',
        labels=['2-app'],
        resource_deps=['postgres', 'redis', 'garage-bootstrap', 'mlflow', 'migrate-up'],
    )

# ── Garage S3 bootstrap (cluster layout + bucket + key) ─────────────────────
# A fresh Garage node serves S3 only after a layout is applied. We also create
# the bucket and import the access/secret key from .env so the API can auth.
# All steps are idempotent so this resource can re-run safely.
local_resource('garage-bootstrap',
    cmd='''
        set -e
        cd manifests
        ak=$(grep ^GARAGE_DEFAULT_ACCESS_KEY .env | cut -d= -f2)
        sk=$(grep ^GARAGE_DEFAULT_SECRET_KEY .env | cut -d= -f2)
        bucket=$(grep ^GARAGE_DEFAULT_BUCKET .env | cut -d= -f2)

        gx() { docker compose exec -T garage /garage "$@"; }

        for i in $(seq 1 60); do
            if gx status >/dev/null 2>&1; then break; fi
            sleep 1
        done

        if ! gx layout show 2>/dev/null | grep -qE "cluster layout version: [1-9]"; then
            node_id=$(gx node id -q | cut -d@ -f1)
            gx layout assign -z dc1 -c 1G "$node_id"
            gx layout apply --version 1
        fi

        gx bucket create "$bucket" 2>/dev/null || true

        if ! gx key info "$ak" >/dev/null 2>&1; then
            gx key import "$ak" "$sk" --yes
            gx key rename "$ak" cvops-default >/dev/null 2>&1 || true
        fi

        gx bucket allow --read --write --owner "$bucket" --key "$ak" >/dev/null
        gx bucket info "$bucket"
    ''',
    labels=['1-infra'],
    resource_deps=['garage'],
)

# ── Host-side install resources (one-time, auto-run on first tilt up) ──────
# Editable install for the API package; uvicorn comes in as a dep.
# Build an isolated venv at services/api/.venv and install the API package into
# it. A bare `pip install --user` fails on PEP-668 / externally-managed hosts
# (Arch, Debian 12+): "error: externally-managed-environment". The venv sidesteps
# that and keeps the editable install off the system interpreter. `python -m venv`
# is idempotent, so re-running just reuses the existing venv.
#
# TMPDIR override: pip unpacks/builds wheels in $TMPDIR, which defaults to /tmp.
# On this host /tmp is a RAM-backed tmpfs with a per-user quota (~7.7G shared),
# so large dependency trees (torch, etc.) blow the quota with EDQUOTA mid-install.
# Point pip at a dir under .venv instead — it lives on the root fs (noquota,
# hundreds of GB free) and is already gitignored. Applied to every host pip
# install below for the same reason.
local_resource('api-install',
    cmd='cd services/api && python3 -m venv .venv && mkdir -p .venv/pip-tmp && TMPDIR="$PWD/.venv/pip-tmp" PIP_DEFAULT_TIMEOUT=60 PIP_RETRIES=10 .venv/bin/python -m pip install -e ".[dev]"',
    deps=['services/api/pyproject.toml'],
    labels=['5-setup'],
)

# Install the step implementations (cvops_steps) into the API venv so the
# engine registry picks up extract_frames at startup. Base deps only (no ml/train
# extras → no torch); the engine import is best-effort, but without this the
# registry is empty and workflow creation rejects step.extract_frames.
local_resource('steps-install',
    cmd='cd services/api && mkdir -p .venv/pip-tmp && TMPDIR="$PWD/.venv/pip-tmp" PIP_DEFAULT_TIMEOUT=60 PIP_RETRIES=10 .venv/bin/python -m pip install -e ../../packages/steps',
    deps=['packages/steps/pyproject.toml'],
    resource_deps=['api-install'],
    labels=['5-setup'],
)

# npm install for the frontend.
local_resource('frontend-install',
    cmd='cd services/frontend && npm install --silent',
    deps=['services/frontend/package.json'],
    labels=['5-setup'],
)

# ── App processes (host) ────────────────────────────────────────────────────
# Connection strings rewritten to localhost:<published-port> — the host process
# hits Docker's exposed ports, not the compose-internal network DNS.
api_env = {
    'DATABASE_URL': 'postgresql+asyncpg://%s:%s@localhost:5432/%s' % (
        envreq('POSTGRES_USER'), envreq('POSTGRES_PASSWORD'), envreq('POSTGRES_DB'),
    ),
    'REDIS_URL':       'redis://localhost:6379/0',
    'S3_ENDPOINT':     'http://localhost:3900',
    # S3_PUBLIC_ENDPOINT intentionally unset: the API derives the presign host
    # per-request from the browser's Host header, so uploads work from localhost
    # and dev VMs alike. Set it only to force a fixed host/scheme (e.g. HTTPS).
    'S3_ACCESS_KEY':   envreq('GARAGE_DEFAULT_ACCESS_KEY'),
    'S3_SECRET_KEY':   envreq('GARAGE_DEFAULT_SECRET_KEY'),
    'S3_BUCKET':       envreq('GARAGE_DEFAULT_BUCKET'),
    'S3_REGION':       'garage',
    'JWT_SECRET':      envreq('JWT_SECRET'),
    'WORKER_TOKEN':    envreq('WORKER_TOKEN'),
    # The API's /internal/cvat/webhook verifies the HMAC signature CVAT signs
    # review-completion callbacks with; must match the secret worker-cvat
    # registers the webhook with (below).
    'CVAT_WEBHOOK_SECRET': envreq('CVAT_WEBHOOK_SECRET'),
    'PYTHONUNBUFFERED': '1',
    # worker-cvat exposes GET /models on port 8001 (proxied by cvats.py router).
    'MODEL_DEPLOYER_URL': 'http://localhost:8001',
}

local_resource('api',
    serve_cmd='cd services/api && .venv/bin/python -m uvicorn cvops_api.main:app --host 0.0.0.0 --port 8000 --reload',
    serve_env=api_env,
    deps=['services/api/src'],
    resource_deps=['postgres', 'redis', 'garage-bootstrap', 'api-install', 'steps-install', 'migrate-up'],
    readiness_probe=probe(
        period_secs=5,
        http_get=http_get_action(port=8000, path='/openapi.json'),
    ),
    links=[
        link('http://localhost:8000/docs',  'swagger'),
        link('http://localhost:8000/redoc', 'redoc'),
    ],
    labels=['2-app'],
)

local_resource('frontend',
    # Vite already proxies /api/v1 → http://localhost:8000 (see vite.config.ts).
    serve_cmd='cd services/frontend && npm run dev -- --host 0.0.0.0 --port 5173',
    # MLflow comes up on host :5000 only when training is enabled, so the
    # model-page "MLflow run" link points there. With training off the link is
    # dead (no runs exist anyway). VM devs can override in .env.
    serve_env={'VITE_MLFLOW_URL': env.get('VITE_MLFLOW_URL', 'http://localhost:5000')},
    deps=['services/frontend/src', 'services/frontend/vite.config.ts', 'services/frontend/index.html'],
    resource_deps=['api', 'frontend-install'],
    readiness_probe=probe(
        period_secs=5,
        http_get=http_get_action(port=5173, path='/'),
    ),
    links=[link('http://localhost:5173', 'vite dev')],
    labels=['2-app'],
)

# ── Edge proxy (container) ──────────────────────────────────────────────────
# nginx proxies / → host Vite dev (with HMR) and /api/v1 → host API. This is
# what makes `tilt up` a one-stop, browser-usable stack at http://localhost
# (and http://<dev-vm>:80 for VM-based devs). Swap the Vite proxy for a static
# frontend dist/ build later. Defined (profile-less) in docker-compose.yml.
dc_resource('nginx',
    labels=['2-app'],
    resource_deps=['api', 'frontend'],
    links=[link('http://localhost', 'app (nginx)')],
)

# ── Optional helper resources (manual trigger) ──────────────────────────────
local_resource('git-hooks',
    cmd='sh scripts/git-setup.sh',
    deps=['scripts/git-setup.sh'],
    labels=['5-setup'],
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
)

# Install the preprocessing worker into the API venv (which already carries
# cvops-api + cvops-steps). The worker reuses those packages, so it shares the env.
local_resource('worker-install',
    cmd='cd services/api && mkdir -p .venv/pip-tmp && TMPDIR="$PWD/.venv/pip-tmp" PIP_DEFAULT_TIMEOUT=60 PIP_RETRIES=10 .venv/bin/python -m pip install -e ../worker-preprocessing',
    deps=['services/worker-preprocessing/pyproject.toml'],
    resource_deps=['api-install', 'steps-install'],
    labels=['5-setup'],
)

# Install the CVAT worker into the API venv so the host worker-cvat process can
# import it. worker-common + cvat-client are local monorepo packages (not on
# PyPI), so they must be installed editable explicitly; the unioned pyproject
# then pulls the PyPI deps (cvat-sdk/ultralytics/fastapi/uvicorn). Shares the
# venv with the API + steps, same as worker-preprocessing. Gated on ENABLE_CVAT
# so the heavy cvat-sdk/ultralytics install (and the nuctl download below) only
# happen when CVAT is opted into.
if ENABLE_CVAT:
    local_resource('worker-cvat-install',
        cmd='cd services/api && mkdir -p .venv/pip-tmp && TMPDIR="$PWD/.venv/pip-tmp" PIP_DEFAULT_TIMEOUT=60 PIP_RETRIES=10 .venv/bin/python -m pip install -e ../../packages/worker-common -e ../../packages/cvat-client -e ../worker-cvat',
        deps=[
            'services/worker-cvat/pyproject.toml',
            'packages/worker-common/pyproject.toml',
            'packages/cvat-client/pyproject.toml',
        ],
        resource_deps=['api-install', 'steps-install'],
        labels=['4-cvat-app'],
    )

    # Download nuctl (Nuclio CLI) once — used by the CVAT worker to deploy .pt models.
    # Idempotent: skipped if the binary is already present and executable.
    # Resumable & stall-proof: `-C -` continues a partial download from where it
    # stopped (the chmod only runs on success, so a half-downloaded file stays
    # non-executable and the `test -x` guard re-enters the curl to finish it).
    # `--speed-limit/--speed-time` abort a connection that stalls below 2KB/s for
    # 20s instead of hanging forever, and `--retry` then resumes it.
    local_resource('nuctl-install',
        cmd='test -x services/worker-cvat/nuctl || curl -fSL -C - --retry 10 --retry-delay 2 --retry-all-errors --connect-timeout 20 --speed-limit 2048 --speed-time 20 --progress-bar "https://github.com/nuclio/nuclio/releases/download/1.15.9/nuctl-1.15.9-linux-amd64" -o services/worker-cvat/nuctl && chmod +x services/worker-cvat/nuctl',
        labels=['4-cvat-app'],
    )

# ── Worker processes (host) ─────────────────────────────────────────────────
# Redis-Streams preprocessing worker: consumes the `preprocessing` stream and
# runs extract_frames (and future preprocessing steps) out of the API process.
worker_env = dict(api_env)
worker_env['REDIS_STREAM'] = 'preprocessing'

local_resource('worker-preprocessing',
    serve_cmd='cd services/api && .venv/bin/python -m cvops_worker',
    serve_env=worker_env,
    # Watch the step impls too — extract_frames/commit_dataset/export_yolo are
    # editable-installed from packages/steps, so the worker must restart to pick
    # up their changes (Python doesn't hot-reload).
    deps=['services/worker-preprocessing/src', 'packages/steps/src/cvops_steps'],
    resource_deps=['postgres', 'redis', 'garage-bootstrap', 'steps-install', 'worker-install', 'migrate-up'],
    labels=['2-app'],
)

# CVAT-queue worker (host): consumes the `cvat` stream and serves :8001. One
# process now covers both folded-in roles:
#   • step.human_review — pushes the review batch into CVAT, parks the run at the
#     gate; a {kind: cvat_sync} doorbell pulls reviewed annotations back and
#     resumes the run (sync path, via cvat-client).
#   • step.deploy_model + GET /models, POST /deploy on :8001 (proxied by the
#     API's cvat.py router via MODEL_DEPLOYER_URL → worker-cvat:8001) — deploys
#     .pt models to Nuclio via nuctl (deploy path).
# Entry point is `python -m worker_cvat` (→ __main__ → worker.main). The env is
# the union of both roles: CVAT_URL (sync, cvat-client) + CVAT_HOST (deploy).
if ENABLE_CVAT:
    # Pre-build the YOLO Nuclio base image so function deploys skip the heavy
    # pip install step. Built once on first `tilt up --cvat`; rebuilt only when
    # the Dockerfile changes. YOLO_BASE_IMAGE points worker-cvat at this image.
    local_resource('yolo-base-image',
        cmd='docker build -t cvops/yolo-nuclio-base:latest -f services/worker-cvat/yolo-base.Dockerfile services/worker-cvat',
        deps=['services/worker-cvat/yolo-base.Dockerfile'],
        labels=['3-cvat'],
        resource_deps=['cvat-network'],
    )

    worker_cvat_env = dict(api_env)
    worker_cvat_env.update({
        'REDIS_STREAM':        'cvat',
        'MODEL_DEPLOYER_PORT': '8001',
        # sync path (cvat-client reads CVAT_URL, falling back to CVAT_HOST)
        'CVAT_URL':            'http://localhost:8080',
        'CVAT_PUBLIC_URL':     env.get('CVAT_PUBLIC_URL', 'http://localhost:8080'),
        'CVAT_USERNAME':       envreq('CVAT_USERNAME'),
        'CVAT_PASSWORD':       envreq('CVAT_PASSWORD'),
        # deploy path (deployer/cvat_client read CVAT_HOST + nuctl)
        'NUCTL_PATH':          str(local('pwd', quiet=True)).strip() + '/services/worker-cvat/nuctl',
        # pre-built base image — avoids pip install on every function deploy
        'YOLO_BASE_IMAGE':     'cvops/yolo-nuclio-base:latest',
    })
    cvat_host_raw = env.get('CVAT_HOST', 'localhost')
    worker_cvat_env['CVAT_HOST'] = cvat_host_raw if cvat_host_raw.startswith('http') else 'http://%s:8080' % cvat_host_raw

    # worker-cvat waits for postgres/redis/garage (its own infra deps) but is
    # intentionally NOT in the resource_deps of api/frontend/nginx — so a CVAT
    # failure never blocks the main stack.
    local_resource('worker-cvat',
        serve_cmd='cd services/api && .venv/bin/python -m worker_cvat',
        serve_env=worker_cvat_env,
        # Watch the step + cvat-client sources too — they're editable-installed into
        # the venv, so the worker must restart to pick up changes to the human_review
        # step or the CVAT client (Python doesn't hot-reload like uvicorn --reload).
        deps=[
            'services/worker-cvat/src',
            'packages/worker-common/src/cvops_worker_common',
            'packages/steps/src/cvops_steps',
            'packages/cvat-client/src/cvops_cvat_client',
        ],
        # Only its own infra — deliberately NOT gated on cvat_server / the CVAT admin
        # bootstrap, so a slow or failing CVAT stack never blocks the worker. It
        # connects to CVAT lazily, per review/deploy doorbell, and surfaces any CVAT
        # error on that run instead.
        resource_deps=['postgres', 'redis', 'garage-bootstrap', 'steps-install', 'worker-cvat-install', 'nuctl-install', 'docker-socket-perms', 'migrate-up', 'yolo-base-image'],
        labels=['4-cvat-app'],
    )

# ── Quality gates ───────────────────────────────────────────────────────────
local_resource('api-test',
    cmd='cd services/api && pytest tests/ -q',
    labels=['6-quality'],
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
)

local_resource('api-lint',
    cmd='cd services/api && ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/',
    labels=['6-quality'],
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
)

local_resource('frontend-lint',
    cmd='cd services/frontend && npm run lint && npm run typecheck',
    labels=['6-quality'],
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
)

local_resource('frontend-build',
    cmd='cd services/frontend && npm run build',
    labels=['6-quality'],
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
)

# ── DB operations (run against the local uvicorn process or via containers) ─
# Alembic migrations execute on the host against the local DATABASE_URL.
migrate_env = {'DATABASE_URL': api_env['DATABASE_URL']}

# Auto-runs on `tilt up` (api depends on it) so the schema — including new
# migrations like 0002 — is always applied before the API serves.
local_resource('migrate-up',
    cmd='cd services/api && .venv/bin/alembic upgrade head',
    env=migrate_env,
    labels=['7-db'],
    resource_deps=['postgres', 'api-install'],
)

local_resource('migrate-down',
    cmd='cd services/api && .venv/bin/alembic downgrade -1',
    env=migrate_env,
    labels=['7-db'],
    resource_deps=['postgres', 'api-install'],
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
)

local_resource('migrate-revision',
    cmd='cd services/api && .venv/bin/alembic revision --autogenerate -m "tilt-generated"',
    env=migrate_env,
    labels=['7-db'],
    resource_deps=['postgres', 'api-install'],
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
)

local_resource('psql',
    cmd='''
        cd manifests
        user=$(grep ^POSTGRES_USER .env | cut -d= -f2)
        db=$(grep ^POSTGRES_DB .env | cut -d= -f2)
        pw=$(grep ^POSTGRES_PASSWORD .env | cut -d= -f2)
        docker compose exec -e PGPASSWORD="$pw" postgres psql -U "$user" "$db"
    ''',
    labels=['7-db'],
    resource_deps=['postgres'],
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
)

local_resource('garage-status',
    cmd='cd manifests && docker compose exec -T garage /garage status && docker compose exec -T garage /garage layout show',
    labels=['7-db'],
    resource_deps=['garage'],
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
)

print('CVOps Tiltfile loaded — host dev mode (api + frontend on host, infra in compose). ' +
      'CVAT: %s, training+MLflow: %s.' % (
          'on' if ENABLE_CVAT else 'off', 'on' if ENABLE_TRAINING else 'off'))
