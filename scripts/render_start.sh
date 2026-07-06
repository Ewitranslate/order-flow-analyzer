#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 scripts/render_write_secrets.py

# Render задаёт $PORT; Streamlit должен слушать 0.0.0.0 и не использовать file watcher.
exec streamlit run src/app.py \
  --server.port="${PORT:-8501}" \
  --server.address=0.0.0.0 \
  --server.headless=true \
  --server.enableCORS=false \
  --server.enableXsrfProtection=false \
  --server.fileWatcherType=none
