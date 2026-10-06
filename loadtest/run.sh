# Load test driver. Run from anywhere:  bash loadtest/run.sh
cd "$(dirname "$0")/.." || exit 1
mkdir -p loadtest/results
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.loadtest.yml"
# always put the normal dev API back, even if something below fails
trap 'echo "== restoring the normal dev API"; docker compose up -d api >/dev/null 2>&1' EXIT

echo "== starting the API with 4 workers"
$COMPOSE up -d api >/dev/null 2>&1
for i in $(seq 1 60); do curl -sf localhost:8000/health >/dev/null && break; sleep 1; done
curl -sf localhost:8000/health >/dev/null || { echo "API did not come up"; exit 1; }

NET=$(docker inspect "$(docker compose ps -q api)" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')
echo "docker network: $NET"
{ docker info --format '{{.NCPU}} {{.MemTotal}}'; sysctl -n machdep.cpu.brand_string 2>/dev/null; } > loadtest/results/hw.txt

# level 50 is a warm-up (connection pools, caches); it is not reported
for N in 50 100 300 500; do
  echo "== level $N: preparing"
  docker compose exec -T api python -m scripts.loadprep --level $N > loadtest/data.json 2> loadtest/results/prep-$N.log || { echo "prep failed:"; tail -5 loadtest/results/prep-$N.log; exit 1; }
  SID=$(python3 -c "import json;print(json.load(open('loadtest/data.json'))['session_id'])")
  echo "== level $N: running k6 against session $SID"
  docker run --rm --network "$NET" -v "$PWD/loadtest:/loadtest:ro" -e BASE_URL=http://api:8000 grafana/k6:latest run /loadtest/checkin.js > loadtest/results/raw-$N.txt 2>&1
  grep -o 'RESULT_JSON:.*' loadtest/results/raw-$N.txt | head -1 | sed 's/^RESULT_JSON://' > loadtest/results/level-$N.json
  docker compose exec -T db sh -c "psql -tA -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -c 'SELECT count(*) FROM attendance WHERE session_id=$SID'" > loadtest/results/db-$N.txt
  if [ -s loadtest/results/level-$N.json ]; then cat loadtest/results/level-$N.json; else echo "no k6 result; last lines of raw-$N.txt:"; tail -8 loadtest/results/raw-$N.txt; fi
  echo "   attendance rows in the database for this session: $(tr -d '[:space:]' < loadtest/results/db-$N.txt)"
  sleep 3
done

python3 loadtest/make_report.py
