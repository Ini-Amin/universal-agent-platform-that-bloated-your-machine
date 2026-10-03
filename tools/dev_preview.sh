#!/usr/bin/env bash
# Boot the UI locally: a throwaway pgvector Postgres (Docker) plus the UAP server.
#   bash tools/dev_preview.sh        # HOST defaults to loopback, PORT to 3000
set -euo pipefail
cd "$(dirname "$0")/.."

container="${UAP_PREVIEW_DB_CONTAINER:-uap-preview-db}"
db_port="${UAP_PREVIEW_DB_PORT:-55432}"
host="${HOST:-127.0.0.1}"
port="${PORT:-3000}"

if docker inspect "$container" >/dev/null 2>&1; then
  docker start "$container" >/dev/null
else
  docker run -d --name "$container" \
    -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=uap_local_dev \
    -p "127.0.0.1:${db_port}:5432" pgvector/pgvector:pg17 >/dev/null
fi
# -h forces TCP: the image's init phase runs a socket-only temporary server.
until docker exec "$container" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1; do sleep 1; done

psql_admin() { docker exec -i "$container" psql -v ON_ERROR_STOP=1 -U postgres -d "$1" "${@:2}"; }
exists() { [ "$(psql_admin postgres -tAc "$1")" = 1 ]; }
exists "SELECT 1 FROM pg_roles WHERE rolname = 'uap'" ||
  psql_admin postgres -c "CREATE ROLE uap LOGIN PASSWORD 'uap_local_dev' NOSUPERUSER NOCREATEDB"
for db in uap uap_test; do
  exists "SELECT 1 FROM pg_database WHERE datname = '$db'" ||
    psql_admin postgres -c "CREATE DATABASE $db OWNER uap"
  psql_admin "$db" -c "CREATE EXTENSION IF NOT EXISTS vector"
done

export DATABASE_URL="postgresql+psycopg://uap:uap_local_dev@127.0.0.1:${db_port}/uap"
.venv/bin/alembic upgrade head
exec .venv/bin/uvicorn uap.server.app:create_app --factory --host "$host" --port "$port"
