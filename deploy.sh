#!/bin/bash
# Deploy the working tree to the VM. No GitHub access needed on the box and no
# secrets anywhere: code goes up over `gcloud compute ssh` using your own gcloud auth.
#
#   ./deploy.sh            # sync + install + restart
#   ./deploy.sh --logs     # tail the service
#   ./deploy.sh --ssh      # shell on the box
#   ./deploy.sh --setup    # first run: create both users + Steve's API key (you type, on the box)
#   ./deploy.sh --env NAME # set one env var on the box (prompts, hidden) and restart
#   ./deploy.sh --backup   # run the nightly backup now and list what is in the bucket
set -euo pipefail
P=steve-command-center; Z=europe-west2-a; H=cc
SSH=(gcloud compute ssh "$H" --project="$P" --zone="$Z" --quiet)
cd "$(dirname "$0")"

case "${1:-}" in
  --logs)  exec "${SSH[@]}" --command='sudo journalctl -u cc -n 80 --no-pager -f' ;;
  --ssh)   exec "${SSH[@]}" ;;
  --backup) exec "${SSH[@]}" --command='sudo systemctl start cc-backup.service && sudo journalctl -u cc-backup -n 3 --no-pager -o cat && gcloud storage ls -l gs://emcc-backups/ | tail -n 8' ;;
  --setup) exec "${SSH[@]}" --ssh-flag=-t --command='sudo -u cc bash -c "cd /srv/cc/app && .venv/bin/python scripts/user.py setup"' ;;
  --env)
    name="${2:?usage: ./deploy.sh --env NAME}"
    read -r -s -p "$name: " value; echo
    [ -n "$value" ] || { echo "empty, nothing written"; exit 1; }
    if [ "$name" = ANTHROPIC_API_KEY ] && [[ "$value" != sk-ant-* ]]; then
      echo "that doesn't look like an Anthropic key (they start sk-ant-). A cc_ key is this app's own API key — wrong slot. Nothing written."; exit 1
    fi
    printf '%s' "$value" | "${SSH[@]}" --command="sudo -u cc /srv/cc/app/deploy/setenv.sh $name && sudo systemctl restart cc && echo 'service restarted'"
    exit ;;
esac

echo "→ checking"
.venv/bin/python scripts/check.py || { echo "not deploying a broken build"; exit 1; }

echo "→ syncing"
COPYFILE_DISABLE=1 tar czf - --no-xattrs --exclude .git --exclude .venv --exclude '*.db' --exclude .env --exclude transcripts \
          --exclude __pycache__ --exclude .DS_Store . \
  | "${SSH[@]}" --command='rm -rf ~/cc-stage && mkdir -p ~/cc-stage && tar xzf - -C ~/cc-stage'

echo "→ installing"
"${SSH[@]}" --command='set -e
sudo rsync -a --delete --exclude .env --exclude "*.db" --exclude .venv --exclude backups --exclude .gcloud ~/cc-stage/ /srv/cc/app/
sudo chown -R cc:cc /srv/cc/app
sudo -u cc bash -c "cd /srv/cc/app && ([ -d .venv ] || python3.12 -m venv .venv) && .venv/bin/pip install -q -r requirements.txt"
sudo install -m 644 /srv/cc/app/deploy/cc.service /etc/systemd/system/cc.service
sudo install -m 644 /srv/cc/app/deploy/Caddyfile /etc/caddy/Caddyfile
sudo install -m 644 /srv/cc/app/deploy/cc-backup.service /etc/systemd/system/cc-backup.service
sudo install -m 644 /srv/cc/app/deploy/cc-backup.timer /etc/systemd/system/cc-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable -q cc
sudo systemctl enable -q --now cc-backup.timer
sudo systemctl restart cc
sudo systemctl reload caddy || sudo systemctl restart caddy
sleep 2
systemctl is-active cc caddy'
echo "→ done: https://34.89.78.186.sslip.io"
