#!/bin/bash
# Deploy the working tree to the VM. No GitHub access needed on the box and no
# secrets anywhere: code goes up over `gcloud compute ssh` using your own gcloud auth.
#
#   ./deploy.sh            # sync + install + restart
#   ./deploy.sh --logs     # tail the service
#   ./deploy.sh --ssh      # shell on the box
set -euo pipefail
P=steve-command-center; Z=europe-west2-a; H=cc
SSH=(gcloud compute ssh "$H" --project="$P" --zone="$Z" --quiet)
cd "$(dirname "$0")"

case "${1:-}" in
  --logs) exec "${SSH[@]}" --command='sudo journalctl -u cc -n 80 --no-pager -f' ;;
  --ssh)  exec "${SSH[@]}" ;;
esac

echo "→ syncing"
COPYFILE_DISABLE=1 tar czf - --no-xattrs --exclude .git --exclude .venv --exclude '*.db' --exclude .env --exclude transcripts \
          --exclude __pycache__ --exclude .DS_Store . \
  | "${SSH[@]}" --command='rm -rf ~/cc-stage && mkdir -p ~/cc-stage && tar xzf - -C ~/cc-stage'

echo "→ installing"
"${SSH[@]}" --command='set -e
sudo rsync -a --delete --exclude .env --exclude "*.db" --exclude .venv ~/cc-stage/ /srv/cc/app/
sudo chown -R cc:cc /srv/cc/app
sudo -u cc bash -c "cd /srv/cc/app && ([ -d .venv ] || python3.12 -m venv .venv) && .venv/bin/pip install -q -r requirements.txt"
sudo install -m 644 /srv/cc/app/deploy/cc.service /etc/systemd/system/cc.service
sudo install -m 644 /srv/cc/app/deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl daemon-reload
sudo systemctl enable -q cc
sudo systemctl restart cc
sudo systemctl reload caddy || sudo systemctl restart caddy
sleep 2
systemctl is-active cc caddy'
echo "→ done: https://34.89.78.186.sslip.io"
