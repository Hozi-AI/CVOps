#!/bin/sh
# Create the next numbered Alembic migration from the current model changes.
#
#   services/api/scripts/new-migration.sh "add foo to bar"
#     → services/api/alembic/versions/0009_add_foo_to_bar.py
#
# Runs `alembic revision --autogenerate` with --rev-id set to the next 000N, so
# the revision ID and the filename follow the 000N_description.py convention
# (tests/db/test_migrations.py enforces it). Autogenerate compares the models
# against a database that is AT the current head, so run `alembic upgrade head`
# first (Tilt's migrate-up resource does that on `tilt up`). Review the
# generated file before committing: autogenerate can't express everything
# (see the circular projects FKs in 0001).
#
# DATABASE_URL defaults to the local Tilt/compose postgres built from
# manifests/.env, the same way the Tiltfile does.
set -eu

msg=${1:-}
if [ -z "$msg" ]; then
  echo 'usage: services/api/scripts/new-migration.sh "short description"' >&2
  exit 1
fi

api_dir=$(cd "$(dirname "$0")/.." && pwd)
cd "$api_dir"

last=$(ls alembic/versions | sed -n 's/^\([0-9][0-9][0-9][0-9]\)[a-z]\{0,1\}_.*\.py$/\1/p' | sort | tail -n 1)
next=$(printf '%04d' "$(expr "${last:-0}" + 1)")

if [ -z "${DATABASE_URL:-}" ]; then
  env_file=../../manifests/.env
  if [ ! -f "$env_file" ]; then
    echo "DATABASE_URL is not set and $env_file does not exist" >&2
    exit 1
  fi
  val() { sed -n "s/^$1=//p" "$env_file" | tail -n 1; }
  DATABASE_URL="postgresql+asyncpg://$(val POSTGRES_USER):$(val POSTGRES_PASSWORD)@localhost:$(val POSTGRES_HOST_PORT | grep . || echo 5432)/$(val POSTGRES_DB)"
  export DATABASE_URL
fi

echo "Creating migration $next: $msg"
exec .venv/bin/alembic revision --autogenerate --rev-id "$next" -m "$msg"
