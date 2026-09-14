#!/bin/bash
# Set one variable in /srv/cc/app/.env. Value arrives on stdin so it never touches argv or logs.
#   printf '%s' "$value" | setenv.sh NAME
set -euo pipefail
name="${1:?usage: setenv.sh NAME < value}"
[[ "$name" =~ ^[A-Z_][A-Z0-9_]*$ ]] || { echo "bad variable name: $name" >&2; exit 2; }
value="$(cat)"
[ -n "$value" ] || { echo "empty value, nothing written" >&2; exit 1; }
f=/srv/cc/app/.env
touch "$f"
{ grep -v "^${name}=" "$f" || true; printf '%s=%s\n' "$name" "$value"; } > "$f.tmp"
mv "$f.tmp" "$f"
chmod 600 "$f"
echo "$name set"
