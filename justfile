# CVOps — task runner (alternative to `tilt up`)
#
# Same core stack as the Tiltfile (postgres, redis, garage in Docker; api,
# frontend, worker-preprocessing as host processes), driven by plain bash.
# The Tiltfile is untouched — use whichever you have installed.
#
#   just            list recipes
#   just check      verify host prerequisites
#   just dev        set everything up and start the stack   (alias: just up)
#   just stop       stop the host processes
#   just down       stop host processes + containers
#
# Shell: bash everywhere. On Windows that means Git Bash (no WSL needed) — run
# `just` from a Git Bash terminal. If `bash` on your PATH is WSL's, recipes
# detect it and stop with a message instead of running in the wrong place.
#
# Not covered here (Linux-specific, heavy): the CVAT stack. Use
# `tilt up -- --cvat` for that. The training stack is available as `just training`.

set shell := ["bash", "-eu", "-c"]
set windows-shell := ["bash", "-eu", "-c"]
set script-interpreter := ["bash", "-eu"]

export JUST_OS := os()
# Git Bash rewrites "/garage"-style args into Windows paths; turn that off.
export MSYS_NO_PATHCONV := "1"
export MSYS2_ARG_CONV_EXCL := "*"
# Host port Redis is published on. Override if 6379 is taken/stuck on your machine:
#   REDIS_HOST_PORT=6390 just dev
export REDIS_HOST_PORT := env("REDIS_HOST_PORT", "6379")
# --progress plain: compose's interactive (tty) progress renderer hung in a real
# terminal once the apps were running (seen as "Container cvops-nginx-1 Running 0.0s").
export CVOPS_COMPOSE := "docker compose --progress plain --project-name cvops --env-file manifests/.env -f manifests/docker-compose.yml -f manifests/docker-compose.just.yml"

alias up := dev

# Shared bash helpers, pasted at the top of every recipe script.
lib := '''
if [ "$JUST_OS" = windows ] && [ "$(uname -s)" = Linux ]; then
  echo "error: 'bash' here is WSL's bash. Run just from Git Bash (or put Git's bash first on PATH)." >&2
  exit 1
fi
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) IS_WIN=1 ;; *) IS_WIN=0 ;; esac
# Docker Desktop's `docker` on PATH is a sh wrapper (sh -> basename -> uname) before it
# reaches docker.exe; those extra MSYS processes intermittently hung Git Bash. Skip it.
if [ "$IS_WIN" = 1 ] && command -v docker.exe >/dev/null 2>&1; then
  docker() { command docker.exe "$@"; }
fi
ENV_FILE=manifests/.env
# Debug aid: JUST_TRACE=1 just --no-deps dev   → prints every command the script runs
if [ -n "${JUST_TRACE:-}" ]; then set -x; fi

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# get_env KEY [default] — value from manifests/.env (CR-safe)
get_env() {
  # Pure bash (no grep|head|cut|tr pipeline): that pipeline hung under Git Bash
  # when the key was missing from the file.
  local line v=""
  if [ -f "$ENV_FILE" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      line=${line%$'\r'}
      case $line in "$1="*) v=${line#*=}; break ;; esac
    done < "$ENV_FILE"
  fi
  if [ -n "$v" ]; then printf '%s' "$v"; else printf '%s' "${2:-}"; fi
}
req_env() {
  local v
  v=$(get_env "$1")
  [ -n "$v" ] || die "manifests/.env has no value for $1 (run: just env)"
  printf '%s' "$v"
}

# Prints a Python >= 3.12 command, or fails. python3 is skipped when it is the
# Microsoft Store stub (it fails the version probe), and `py -3.12` is the
# Windows launcher fallback.
find_python() {
  local c
  for c in "${PYTHON:-}" python3.12 python3 python; do
    [ -n "$c" ] || continue
    if command -v "$c" >/dev/null 2>&1 \
       && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
      echo "$c"; return 0
    fi
  done
  if command -v py >/dev/null 2>&1 && py -3.12 -c 'import sys' >/dev/null 2>&1; then
    echo "py -3.12"; return 0
  fi
  return 1
}

# Venv interpreter, relative to services/api (Scripts/ on Windows, bin/ elsewhere).
api_py() {
  if   [ -x services/api/.venv/Scripts/python.exe ]; then echo .venv/Scripts/python.exe
  elif [ -x services/api/.venv/bin/python ];         then echo .venv/bin/python
  else die "API venv not found — run: just install"
  fi
}

# Export the env the host-side processes need (localhost-rewritten, like the Tiltfile).
load_host_env() {
  local u p d
  u=$(req_env POSTGRES_USER); p=$(req_env POSTGRES_PASSWORD); d=$(req_env POSTGRES_DB)
  export DATABASE_URL="postgresql+asyncpg://$u:$p@localhost:5432/$d"
  # 127.0.0.1, not localhost: on Windows "localhost" resolves to ::1 first and Docker
  # Desktop's IPv6 port forward hangs, so the redis client times out.
  export REDIS_URL="redis://127.0.0.1:${REDIS_HOST_PORT:-6379}/0"
  export S3_ENDPOINT="http://localhost:3900"
  export S3_ACCESS_KEY=$(req_env GARAGE_DEFAULT_ACCESS_KEY)
  export S3_SECRET_KEY=$(req_env GARAGE_DEFAULT_SECRET_KEY)
  export S3_BUCKET=$(req_env GARAGE_DEFAULT_BUCKET)
  export S3_REGION=garage
  export JWT_SECRET=$(req_env JWT_SECRET)
  export WORKER_TOKEN=$(req_env WORKER_TOKEN)
  export CVAT_WEBHOOK_SECRET=$(req_env CVAT_WEBHOOK_SECRET)
  export MODEL_DEPLOYER_URL="http://localhost:8001"
  export PYTHONUNBUFFERED=1
}

need_docker() {
  docker info >/dev/null 2>&1 || die "Docker isn't reachable — start Docker Desktop (or the docker daemon) and retry"
}

# wait_healthy SERVICE [SECONDS] — wait for a compose service's healthcheck
wait_healthy() {
  local svc=$1 secs=${2:-90} cid st i
  for i in $(seq 1 "$secs"); do
    cid=$($CVOPS_COMPOSE ps -q "$svc" 2>/dev/null | tr -d '\r' || true)
    if [ -n "$cid" ]; then
      st=$(docker inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null | tr -d '\r' || true)
      if [ "$st" = healthy ]; then ok "$svc is healthy"; return 0; fi
    fi
    sleep 1
  done
  die "$svc did not become healthy within ${secs}s"
}
'''

[private]
default:
    @just --list --unsorted

# ── setup ────────────────────────────────────────────────────────────────

# Verify host tools: python 3.12+, node 20+, docker (running), openssl, curl
[group('setup')]
[script]
check:
    {{lib}}
    say "Checking host prerequisites ($(uname -s))"
    bad=0
    if PY=$(find_python); then ok "python: $PY — $($PY --version 2>&1)"
    else warn "Python 3.12+ not found (Windows: winget install Python.Python.3.12; or set PYTHON=...)"; bad=1; fi
    if command -v node >/dev/null 2>&1; then
      major=$(node -p 'process.versions.node.split(".")[0]')
      if [ "$major" -ge 20 ]; then ok "node: $(node --version)"; else warn "node $(node --version) is too old (need 20+)"; bad=1; fi
    else warn "node not found (need 20+)"; bad=1; fi
    command -v npm >/dev/null 2>&1 && ok "npm: $(npm --version)" || { warn "npm not found"; bad=1; }
    if command -v docker >/dev/null 2>&1; then
      if docker info >/dev/null 2>&1; then ok "docker: engine reachable ($(docker --version))"
      else warn "docker is installed but the engine isn't reachable — start Docker Desktop"; bad=1; fi
      docker compose version >/dev/null 2>&1 && ok "docker compose: $(docker compose version --short 2>/dev/null)" || { warn "docker compose plugin not found"; bad=1; }
    else warn "docker not found"; bad=1; fi
    command -v openssl >/dev/null 2>&1 && ok "openssl: $(openssl version | cut -d' ' -f1-2)" || { warn "openssl not found (needed to generate secrets)"; bad=1; }
    command -v curl >/dev/null 2>&1 && ok "curl found" || { warn "curl not found"; bad=1; }
    command -v ffmpeg >/dev/null 2>&1 && ok "ffmpeg found" || warn "ffmpeg not found (only needed to run the extract_frames step on the host)"
    [ "$bad" = 0 ] || die "fix the items above, then re-run: just check"
    ok "all required tools present"

# Create manifests/.env from the example and fill in any empty/placeholder secrets
[group('setup')]
[script]
env:
    {{lib}}
    say "Preparing manifests/.env"
    [ -f manifests/.env.example ] || die "manifests/.env.example not found (run just from the repo root)"
    command -v openssl >/dev/null 2>&1 || die "openssl is required to generate secrets"
    if [ ! -f "$ENV_FILE" ]; then cp manifests/.env.example "$ENV_FILE"; ok "created manifests/.env from .env.example"; fi
    # strip CRs that can sneak in on Windows (they would corrupt values)
    tmp=$(mktemp); tr -d '\r' < "$ENV_FILE" > "$tmp"; cat "$tmp" > "$ENV_FILE"; rm -f "$tmp"
    # append keys present in the example but missing from .env
    while IFS= read -r line || [ -n "$line" ]; do
      line=${line%$'\r'}
      case "$line" in ''|\#*) continue ;; *=*) ;; *) continue ;; esac
      key=${line%%=*}
      if ! grep -qE "^${key}=" "$ENV_FILE"; then printf '%s\n' "$line" >> "$ENV_FILE"; ok "added missing key $key"; fi
    done < manifests/.env.example
    if grep -q MINIO_ROOT_USER "$ENV_FILE"; then die "manifests/.env is stale (still has MINIO_*). Delete it and re-run: just env"; fi
    # fill empty values and change_me* placeholders with generated secrets
    # (an existing POSTGRES_PASSWORD is never replaced, so the data volume keeps working)
    secret_for() {
      case "$1" in
        GARAGE_DEFAULT_ACCESS_KEY) echo "GK$(openssl rand -hex 12)" ;;
        POSTGRES_PASSWORD|CVAT_PASSWORD|GRAFANA_ADMIN_PASSWORD) openssl rand -hex 16 ;;
        GARAGE_DEFAULT_SECRET_KEY|GARAGE_RPC_SECRET|GARAGE_ADMIN_TOKEN|GARAGE_METRICS_TOKEN|JWT_SECRET|WORKER_TOKEN|CVAT_WEBHOOK_SECRET) openssl rand -hex 32 ;;
        *) return 1 ;;
      esac
    }
    is_placeholder() { case "$1" in change_me*|GKchange_me*) return 0 ;; *) return 1 ;; esac; }
    tmp=$(mktemp); filled=0
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in ''|\#*) printf '%s\n' "$line" >> "$tmp"; continue ;; *=*) ;; *) printf '%s\n' "$line" >> "$tmp"; continue ;; esac
      key=${line%%=*}; val=${line#*=}; need=0
      if [ -z "$val" ]; then need=1
      elif is_placeholder "$val" && [ "$key" != POSTGRES_PASSWORD ]; then need=1; fi
      if [ "$need" = 1 ] && new=$(secret_for "$key"); then
        printf '%s=%s\n' "$key" "$new" >> "$tmp"; filled=$((filled + 1))
      else
        printf '%s\n' "$line" >> "$tmp"
      fi
    done < "$ENV_FILE"
    cat "$tmp" > "$ENV_FILE"; rm -f "$tmp"
    [ "$filled" = 0 ] || ok "generated $filled secret(s) in manifests/.env"
    ok "manifests/.env ready"

# Start postgres, redis, garage (Docker) and wait until they're healthy
[group('setup')]
[script]
infra: env
    {{lib}}
    need_docker
    say "Starting postgres, redis, garage"
    $CVOPS_COMPOSE up -d postgres redis garage
    wait_healthy postgres 90
    wait_healthy redis 60

# Garage needs a cluster layout, a bucket and the access key from .env (idempotent)
[group('setup')]
[script]
garage-bootstrap:
    {{lib}}
    need_docker
    ak=$(req_env GARAGE_DEFAULT_ACCESS_KEY); sk=$(req_env GARAGE_DEFAULT_SECRET_KEY)
    bucket=$(get_env GARAGE_DEFAULT_BUCKET cvops-blobs)
    gx() { $CVOPS_COMPOSE exec -T garage /garage "$@"; }
    say "Bootstrapping garage (layout, bucket '$bucket', key)"
    ready=0
    for i in $(seq 1 60); do if gx status >/dev/null 2>&1; then ready=1; break; fi; sleep 1; done
    [ "$ready" = 1 ] || die "garage did not answer 'garage status' — is it running? (just infra)"
    if ! gx layout show 2>/dev/null | grep -qE "cluster layout version: [1-9]"; then
      node_id=$(gx node id -q | cut -d@ -f1 | tr -d '\r')
      gx layout assign -z dc1 -c 1G "$node_id"
      gx layout apply --version 1
    fi
    gx bucket create "$bucket" >/dev/null 2>&1 || true
    if ! gx key info "$ak" >/dev/null 2>&1; then
      gx key import "$ak" "$sk" --yes
      gx key rename "$ak" cvops-default >/dev/null 2>&1 || true
    fi
    gx bucket allow --read --write --owner "$bucket" --key "$ak" >/dev/null
    gx bucket info "$bucket" >/dev/null
    ok "garage ready"

# Create services/api/.venv, install api + steps + worker (editable), npm install the frontend
[group('setup')]
[script]
install:
    {{lib}}
    export PIP_DEFAULT_TIMEOUT=60 PIP_RETRIES=10
    PY=$(find_python) || die "Python 3.12+ not found (Windows: winget install Python.Python.3.12; or set PYTHON=...)"
    if [ ! -x services/api/.venv/Scripts/python.exe ] && [ ! -x services/api/.venv/bin/python ]; then
      say "Creating services/api/.venv with: $PY"
      $PY -m venv services/api/.venv
    fi
    VPY=$(api_py)
    cd services/api
    say "Installing cvops-api (+dev extras)"
    "$VPY" -m pip install -e ".[dev]"
    say "Installing cvops-steps"
    "$VPY" -m pip install -e ../../packages/steps
    say "Installing worker-preprocessing"
    "$VPY" -m pip install -e ../worker-preprocessing
    cd ../frontend
    say "npm install (frontend)"
    npm install
    ok "dependencies installed"

# Everything needed before starting the apps: env, infra, garage, install, migrations
[group('setup')]
prepare: env infra garage-bootstrap install migrate

# ── run ──────────────────────────────────────────────────────────────────

# Prepare, then start api + frontend + worker in the background (logs in .just/logs) and nginx
[group('run')]
[script]
dev: prepare
    {{lib}}
    mkdir -p .just/logs .just/pids
    # Only bash builtins around the launches (read, not $(cat ...)): forking a new
    # process right after native python/node start intermittently hung Git Bash.
    start_bg() { # NAME CMD...
      local name=$1 old=""; shift
      if [ -f ".just/pids/$name.pid" ]; then read -r old < ".just/pids/$name.pid" || true; old=${old%$'\r'}; fi
      if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
        ok "$name already running"; return 0
      fi
      # stdin from /dev/null: otherwise python/node keep the shared Windows console's
      # input handle and later processes (docker, curl, cat...) can block starting up.
      nohup "$@" > ".just/logs/$name.log" 2>&1 < /dev/null &
      echo $! > ".just/pids/$name.pid"
      ok "started $name (pid $!, log .just/logs/$name.log)"
    }
    # nginx first: it proxies to the host API/vite at request time, so it doesn't need
    # them up yet. Every docker call must happen BEFORE the host processes start —
    # a native docker.exe launched from Git Bash while python/node are running
    # intermittently never exits.
    say "Starting nginx edge"
    $CVOPS_COMPOSE up -d nginx < /dev/null
    say "Starting host processes"
    coproc NAP { exec sleep 86400; }   # fork-free sleep source for nap(), see below
    trap '[ -z "${NAP_PID:-}" ] || kill "$NAP_PID" 2>/dev/null || true' EXIT
    load_host_env
    VPY=$(api_py)
    export VITE_MLFLOW_URL=$(get_env VITE_MLFLOW_URL http://localhost:5000)
    export REDIS_STREAM=preprocessing
    start_bg api bash -c 'cd services/api && exec "$0" -m uvicorn cvops_api.main:app --host 0.0.0.0 --port 8000 --reload' "$VPY"
    start_bg worker bash -c 'cd services/api && exec "$0" -m cvops_worker' "$VPY"
    # vite is run via node directly: `npm run dev` fails in a detached Git Bash
    # ("Could not determine Node.js install directory").
    start_bg frontend bash -c 'cd services/frontend && exec node node_modules/vite/bin/vite.js --host 0.0.0.0 --port 5173'
    # From here until the apps answer, fork nothing: no curl/seq/sleep/cat. Spawning
    # an MSYS process while the native python/node children are starting
    # intermittently freezes it (seen as `cat`/`seq`/`curl` stuck forever). So the
    # waiting uses bash builtins only: /dev/tcp for the HTTP probe, `read -t` on the
    # coproc (started before the launches) as sleep.
    nap() { read -t "${1:-1}" -u "${NAP[0]}" || true; }
    http_ok() { # PORT PATH — 0 if the local server answers HTTP 200 (127.0.0.1: bash won't fall back from ::1)
      local line=""
      { exec 3<>"/dev/tcp/127.0.0.1/$1"; } 2>/dev/null || return 1
      printf 'GET %s HTTP/1.0\r\nHost: localhost\r\n\r\n' "$2" >&3
      read -r -t 3 line <&3 || true
      exec 3<&- 3>&-
      case $line in "HTTP/"*" 200"*) return 0 ;; *) return 1 ;; esac
    }
    # wait_up NAME PORT PATH SECONDS — poll, bail out early if the process died
    wait_up() {
      local name=$1 port=$2 path=$3 secs=$4 i pid
      say "Waiting for $name"
      for ((i = 1; i <= secs; i++)); do
        if http_ok "$port" "$path"; then ok "$name is up"; return 0; fi
        read -r pid < ".just/pids/$name.pid" || true
        if ! kill -0 "${pid%$'\r'}" 2>/dev/null; then
          tail -n 15 ".just/logs/$name.log" >&2 || true
          die "$name exited — see .just/logs/$name.log"
        fi
        nap 1
      done
      die "$name did not answer on port $port within ${secs}s — see: just logs"
    }
    nap 2
    wait_up api 8000 /openapi.json 90
    wait_up frontend 5173 / 60
    nap 3
    read -r wpid < .just/pids/worker.pid || true
    if ! kill -0 "${wpid%$'\r'}" 2>/dev/null; then
      tail -n 15 .just/logs/worker.log >&2 || true
      die "worker exited — see .just/logs/worker.log (is Redis reachable on port ${REDIS_HOST_PORT:-6379}?)"
    fi
    ok "worker is running"
    http_ok 80 / || die "nginx is not answering on port 80 — see: just status"
    ok "nginx is up"
    [ -z "${NAP_PID:-}" ] || kill "$NAP_PID" 2>/dev/null || true
    echo
    ok "app (nginx)  http://localhost"
    ok "vite dev     http://localhost:5173"
    ok "swagger      http://localhost:8000/docs"
    echo "  stop host processes: just stop    |    stop everything: just down    |    logs: just logs"

# Run just the API in the foreground (needs: just prepare)
[group('run')]
[script]
api:
    {{lib}}
    load_host_env
    VPY=$(api_py)
    cd services/api
    exec "$VPY" -m uvicorn cvops_api.main:app --host 0.0.0.0 --port 8000 --reload

# Run just the preprocessing worker in the foreground (needs: just prepare)
[group('run')]
[script]
worker:
    {{lib}}
    load_host_env
    export REDIS_STREAM=preprocessing
    VPY=$(api_py)
    cd services/api
    exec "$VPY" -m cvops_worker

# Run just the Vite dev server in the foreground (needs: just install)
[group('run')]
[script]
frontend:
    {{lib}}
    export VITE_MLFLOW_URL=$(get_env VITE_MLFLOW_URL http://localhost:5000)
    cd services/frontend
    exec node node_modules/vite/bin/vite.js --host 0.0.0.0 --port 5173

# HEAVY, opt-in: MLflow + the training worker (builds a multi-GB image the first time)
[group('run')]
[confirm("This builds the training image (torch/ultralytics, several GB) and starts MLflow. Continue?")]
[script]
training: prepare
    {{lib}}
    warn "first build pulls torch/ultralytics — expect a long download"
    $CVOPS_COMPOSE --profile worker --profile mlflow up -d --build mlflow-init mlflow worker-training
    ok "mlflow ui: http://localhost:5000"

# Show container status and whether the apps answer
[group('run')]
[script]
status:
    {{lib}}
    need_docker
    $CVOPS_COMPOSE ps
    echo
    for pair in "api:http://localhost:8000/openapi.json" "frontend:http://localhost:5173/" "nginx:http://localhost/"; do
      name=${pair%%:*}; url=${pair#*:}
      if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then ok "$name up ($url)"; else warn "$name not answering ($url)"; fi
    done

# Follow the logs of the background host processes
[group('run')]
[script]
logs:
    {{lib}}
    ls .just/logs/*.log >/dev/null 2>&1 || die "no logs yet — start the stack with: just dev"
    exec tail -n 50 -f .just/logs/*.log

# ── stop ─────────────────────────────────────────────────────────────────

# Stop the background host processes started by `just dev`
[group('stop')]
[script]
stop:
    {{lib}}
    found=0
    for f in .just/pids/*.pid; do
      [ -e "$f" ] || continue
      found=1
      pid=$(cat "$f"); name=$(basename "$f" .pid)
      if [ "$IS_WIN" = 1 ]; then
        # MSYS pid -> Windows pid, then kill the whole tree (uvicorn --reload, npm -> node)
        winpid=$(ps -p "$pid" 2>/dev/null | awk 'NR==2 {print $4}')
        if [ -n "${winpid:-}" ]; then taskkill //PID "$winpid" //T //F >/dev/null 2>&1 || true; fi
      fi
      kill "$pid" 2>/dev/null || true
      rm -f "$f"; ok "stopped $name"
    done
    [ "$found" = 1 ] || ok "no background processes recorded"
    warn "if a port (8000/5173) is still busy afterwards, end the leftover python/node process in Task Manager"

# Stop host processes and take the containers down (data volumes are kept)
[group('stop')]
[script]
down: stop
    {{lib}}
    need_docker
    $CVOPS_COMPOSE --profile '*' down --remove-orphans
    ok "containers stopped (volumes kept — use: just reset to wipe data)"

# DANGER: stop everything and delete the db / garage / redis volumes
[group('stop')]
[confirm("This deletes ALL CVOps docker volumes (database, blobs, redis). Continue?")]
[script]
reset: stop
    {{lib}}
    need_docker
    $CVOPS_COMPOSE --profile '*' down -v --remove-orphans
    ok "all volumes removed — run 'just dev' for a fresh start (secrets in manifests/.env are kept)"

# ── database ─────────────────────────────────────────────────────────────

# Apply Alembic migrations (starts postgres first if needed)
[group('db')]
[script]
migrate: infra
    {{lib}}
    load_host_env
    VPY=$(api_py)
    cd services/api
    say "alembic upgrade head"
    "$VPY" -m alembic upgrade head

# Roll back the last migration
[group('db')]
[script]
migrate-down: infra
    {{lib}}
    load_host_env
    VPY=$(api_py)
    cd services/api
    "$VPY" -m alembic downgrade -1

# Autogenerate a migration:  just migrate-revision "add foo table"
[group('db')]
[script]
migrate-revision message="tilt-generated": infra
    {{lib}}
    load_host_env
    VPY=$(api_py)
    cd services/api
    "$VPY" -m alembic revision --autogenerate -m "{{message}}"

# Open psql inside the postgres container
[group('db')]
[script]
psql:
    {{lib}}
    need_docker
    u=$(req_env POSTGRES_USER); d=$(req_env POSTGRES_DB); p=$(req_env POSTGRES_PASSWORD)
    exec $CVOPS_COMPOSE exec -e PGPASSWORD="$p" postgres psql -U "$u" "$d"

# Show garage cluster status and layout
[group('db')]
[script]
garage-status:
    {{lib}}
    need_docker
    $CVOPS_COMPOSE exec -T garage /garage status
    $CVOPS_COMPOSE exec -T garage /garage layout show

# ── quality ──────────────────────────────────────────────────────────────

# API test suite (spins up a real postgres via testcontainers — needs Docker)
[group('quality')]
[script]
test:
    {{lib}}
    need_docker
    VPY=$(api_py)
    cd services/api
    "$VPY" -m pytest tests/ -q

# ruff + mypy on the API, eslint + tsc on the frontend
[group('quality')]
lint: lint-api lint-frontend

[group('quality')]
[script]
lint-api:
    {{lib}}
    VPY=$(api_py)
    cd services/api
    "$VPY" -m ruff check src/ tests/
    "$VPY" -m ruff format --check src/ tests/
    "$VPY" -m mypy src/

[group('quality')]
[script]
lint-frontend:
    {{lib}}
    cd services/frontend
    npm run lint
    npm run typecheck

# Production build of the frontend
[group('quality')]
[script]
build-frontend:
    {{lib}}
    cd services/frontend
    npm run build

# Install the repo's shared git hooks (run once after cloning)
[group('quality')]
git-hooks:
    sh scripts/git-setup.sh
