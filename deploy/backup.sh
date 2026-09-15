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
# Upload with the VM's own identity via the metadata server — no gcloud (the snap refuses
# to run for a user whose home is outside /home) and no key file on disk.
TOKEN=$(curl -sf -H 'Metadata-Flavor: Google' 'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' | "$APP/.venv/bin/python" -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -sf -o /dev/null -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/gzip' --data-binary @"$FILE.gz" \
  "https://storage.googleapis.com/upload/storage/v1/b/${BUCKET#gs://}/o?uploadType=media&name=$(basename "$FILE.gz")"
ls -1t "$OUT"/command-center-*.db.gz | tail -n +8 | xargs -r rm -f
echo "backup ok: $FILE.gz → $BUCKET ($(stat -c %s "$FILE.gz") bytes)"
