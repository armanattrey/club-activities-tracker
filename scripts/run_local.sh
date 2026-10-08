#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Reuse ordinary project settings but replace the Docker-only database address.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

PG_BIN="${PG_BIN:-}"
if [[ -z "$PG_BIN" ]]; then
  for candidate in \
    "$(brew --prefix postgresql@16 2>/dev/null || true)/bin" \
    /opt/homebrew/opt/postgresql@16/bin \
    /usr/local/opt/postgresql@16/bin; do
    if [[ -x "$candidate/initdb" ]]; then PG_BIN="$candidate"; break; fi
  done
fi
if [[ -z "$PG_BIN" ]]; then
  for tool in initdb pg_ctl psql createdb; do
    command -v "$tool" >/dev/null 2>&1 || {
      echo "PostgreSQL 16 tools are required. Install them with: brew install postgresql@16" >&2
      echo "If they are installed outside PATH, set PG_BIN to their bin directory." >&2
      exit 1
    }
  done
  PG_BIN="$(dirname "$(command -v initdb)")"
fi
for tool in initdb pg_ctl psql createdb; do
  [[ -x "$PG_BIN/$tool" ]] || { echo "Missing $PG_BIN/$tool" >&2; exit 1; }
done

LOCAL_DATA_DIR="${LOCAL_DATA_DIR:-$ROOT/.local}"
PG_DATA="$LOCAL_DATA_DIR/postgres"
PG_PORT="${LOCAL_PG_PORT:-55432}"
DB_USER="${POSTGRES_USER:-club}"
DB_NAME="${POSTGRES_DB:-clubtracker}"
mkdir -p "$LOCAL_DATA_DIR"

if [[ ! -s "$PG_DATA/PG_VERSION" ]]; then
  mkdir -p "$PG_DATA"
  "$PG_BIN/initdb" -D "$PG_DATA" -U "$DB_USER" --auth-local=trust --auth-host=trust >/dev/null
fi
if ! "$PG_BIN/pg_ctl" -D "$PG_DATA" status >/dev/null 2>&1; then
  "$PG_BIN/pg_ctl" -D "$PG_DATA" -l "$LOCAL_DATA_DIR/postgres.log" \
    -o "-h 127.0.0.1 -p $PG_PORT" start >/dev/null
fi

export APP_MODE=local
export DATABASE_URL="postgresql://$DB_USER@127.0.0.1:$PG_PORT/$DB_NAME"
export REDIS_URL=""
export UPLOAD_DIR="${LOCAL_UPLOAD_DIR:-$LOCAL_DATA_DIR/uploads}"
if [[ -z "${JWT_SECRET:-}" ]]; then
  if [[ ! -s "$LOCAL_DATA_DIR/jwt_secret" ]]; then
    "$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(32))' > "$LOCAL_DATA_DIR/jwt_secret"
    chmod 600 "$LOCAL_DATA_DIR/jwt_secret"
  fi
  JWT_SECRET="$(cat "$LOCAL_DATA_DIR/jwt_secret")"
fi
export JWT_SECRET

if ! "$PG_BIN/psql" "$DATABASE_URL" -Atqc 'SELECT 1' >/dev/null 2>&1; then
  "$PG_BIN/createdb" -h 127.0.0.1 -p "$PG_PORT" -U "$DB_USER" "$DB_NAME"
fi

schema="$("$PG_BIN/psql" "$DATABASE_URL" -Atqc "SELECT to_regclass('public.users')")"
if [[ -z "$schema" ]]; then
  echo "Initializing the local database..."
  migrations="$LOCAL_DATA_DIR/migrations.sql"
  : > "$migrations"
  for migration in "$ROOT"/backend/migrations/*.sql; do
    cat "$migration" >> "$migrations"
  done
  "$PG_BIN/psql" "$DATABASE_URL" --single-transaction -v ON_ERROR_STOP=1 -f "$migrations" >/dev/null
  touch "$LOCAL_DATA_DIR/schema-v10"
elif [[ ! -f "$LOCAL_DATA_DIR/schema-v10" ]]; then
  echo "Existing local database found; automatic migrations were skipped."
  echo "Back it up, then apply any new migration files manually as documented in README.md."
fi

if [[ "${1:-}" == "--demo" ]]; then
  (cd "$ROOT/backend" && "$PYTHON_BIN" -m scripts.seed)
fi

API_HOST="${API_HOST:-0.0.0.0}"
API_PORT="${API_PORT:-8000}"
WEB_HOST="${WEB_HOST:-0.0.0.0}"
WEB_PORT="${WEB_PORT:-5500}"
(cd "$ROOT/backend" && "$PYTHON_BIN" -m uvicorn app.main:app --host "$API_HOST" --port "$API_PORT" --reload) &
API_PID=$!
(cd "$ROOT/frontend" && "$PYTHON_BIN" -m http.server "$WEB_PORT" --bind "$WEB_HOST") &
WEB_PID=$!
cleanup() {
  kill "$API_PID" "$WEB_PID" 2>/dev/null || true
  wait "$API_PID" "$WEB_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Club Activities Tracker is starting in local mode."
echo "Frontend: http://127.0.0.1:$WEB_PORT"
echo "API docs: http://127.0.0.1:$API_PORT/docs"
LAN_IP=""
for interface in en0 en1; do
  LAN_IP="$(ipconfig getifaddr "$interface" 2>/dev/null || true)"
  [[ -n "$LAN_IP" ]] && break
done
if [[ -z "$LAN_IP" ]]; then
  LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
fi
if [[ -n "$LAN_IP" ]]; then
  echo "On this network: http://$LAN_IP:$WEB_PORT (API: http://$LAN_IP:$API_PORT)"
else
  echo "To open on another device, use this computer's LAN IP with ports $WEB_PORT (frontend) and $API_PORT (API)."
fi
echo "PostgreSQL data: $PG_DATA (kept between runs)"
echo "Press Ctrl+C to stop the app. PostgreSQL stays available for the next run."
wait "$API_PID" "$WEB_PID"
