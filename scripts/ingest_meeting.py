#!/usr/bin/env python3
"""Ingest a meeting transcript. Previews first; commits only when you say so.

    scripts/ingest_meeting.py transcript.txt --title "Sat 12 Sep planning" --who Steve Matt
    scripts/ingest_meeting.py transcript.txt --title "..." --who Steve Matt --commit

Reads HOST and CC_KEY (an API key from scripts/user.py key) from the environment (or a .env alongside this repo).
"""
import argparse
import json
import os
import sys
from pathlib import Path

import httpx


def load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transcript", type=Path)
    ap.add_argument("--title", required=True)
    ap.add_argument("--who", nargs="+", default=[], help="participant names")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD")
    ap.add_argument("--commit", action="store_true", help="write to the board (default is preview)")
    ap.add_argument("--host", default=os.environ.get("HOST", "http://127.0.0.1:8000"))
    a = ap.parse_args()

    token = os.environ.get("CC_KEY", "")
    if not token:
        print("CC_KEY is not set — mint one with scripts/user.py key <email>", file=sys.stderr)
        return 2

    body = {
        "title": a.title,
        "transcript": a.transcript.read_text(),
        "participants": a.who,
        "occurred_at": a.date,
        "commit": a.commit,
    }
    r = httpx.post(
        f"{a.host.rstrip('/')}/api/ingest/meeting",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=180,
    )
    if r.status_code != 200:
        print(f"{r.status_code}: {r.text}", file=sys.stderr)
        return 1

    out = r.json()
    if out.get("committed"):
        print(f"committed {out['count']} task(s) — source {out['source_id']}")
        return 0

    tasks = out.get("tasks", [])
    print(f"preview — {len(tasks)} task(s) found, nothing written\n")
    for i, t in enumerate(tasks, 1):
        due = f"  due {t['due']}" if t.get("due") else ""
        print(f"{i:>2}. [{t['priority']}] {t['title']}  — {t['owner']}{due}")
        print(f"    {t['detail']}")
        print(f"    > {t['quote']}\n")
    print("re-run with --commit to write these to the board")
    return 0


if __name__ == "__main__":
    sys.exit(main())
