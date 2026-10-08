#!/usr/bin/env bash
set -Eeuo pipefail

python -m scripts.bootstrap_demo
python -m scripts.seed
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}"
