#!/usr/bin/env bash
# Nightly SQLite backup → gs://emcc-backups. Run as the cc user by cc-backup.timer.
# Uses sqlite's online backup API (consistent even while the app is writing), gzips,
# uploads, and keeps the last 7 locally. The bucket's lifecycle rule deletes objects after 30 days.
#
# Restore:  gcloud storage cp gs://emcc-backups/command-center-<stamp>.db.gz /tmp/ && gunzip /tmp/command-center-<stamp>.db.gz
#           sudo systemctl stop cc && sudo -u cc cp /tmp/command-center-<stamp>.db /srv/cc/app/command-center.db && sudo systemctl start cc
set -euo pipefail
APP=/srv/cc/app
BUCKET="${BACKUP_BUCKET:-gs://emcc-backups}"
OUT=$APP/backups; mkdir -p "$OUT"
STAMP=$(date -u +%Y%m%d-%H%M)
FILE="$OUT/command-center-$STAMP.db"

"$APP/.venv/bin/python" - "$APP/command-center.db" "$FILE" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
with dst: src.backup(dst)
src.close(); dst.close()
PY
gzip -f "$FILE"
gcloud storage cp -q "$FILE.gz" "$BUCKET/"
ls -1t "$OUT"/command-center-*.db.gz | tail -n +8 | xargs -r rm -f
echo "backup ok: $FILE.gz → $BUCKET ($(stat -c %s "$FILE.gz") bytes)"
