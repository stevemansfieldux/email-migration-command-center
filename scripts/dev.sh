#!/bin/sh
# Run the app locally against a throwaway copy of the database (never the live one).
# Usage: scripts/dev.sh [db-file] [port]   (defaults: local-prod.db, 8765)
cd "$(dirname "$0")/.."
export DATABASE_URL="sqlite:///./${1:-local-prod.db}"
export EXTRACT_INTERVAL_MIN=0
exec .venv/bin/uvicorn app.main:app --port "${2:-8765}" --reload
