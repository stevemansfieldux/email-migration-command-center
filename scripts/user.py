#!/usr/bin/env python3
"""Manage users. Run this yourself — passwords are typed here, never sent anywhere else.

    scripts/user.py add <email> --name "Steve Mansfield"     # prompts for a password
    scripts/user.py password <email>                          # reset it
    scripts/user.py key <email>                               # mint an API key (shown once)
    scripts/user.py list
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import auth, db  # noqa: E402
from sqlmodel import select  # noqa: E402


def _prompt_password() -> str:
    while True:
        a = getpass.getpass("password: ")
        b = getpass.getpass("again: ")
        if a and a == b:
            return a
        print("didn't match, try again")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("email"); a.add_argument("--name", required=True)
    p = sub.add_parser("password"); p.add_argument("email")
    k = sub.add_parser("key"); k.add_argument("email")
    sub.add_parser("list")
    args = ap.parse_args()

    db.init()
    with db.session() as s:
        if args.cmd == "list":
            for u in s.exec(select(db.User).order_by(db.User.id)).all():
                print(f"{u.id:>3}  {u.email:<32} {u.name:<20} login={'yes' if u.password_hash else 'no'}  key={'yes' if u.api_key_hash else 'no'}")
            return 0

        email = args.email.strip().lower()
        u = s.exec(select(db.User).where(db.User.email == email)).first()

        if args.cmd == "add":
            if u:
                print(f"{email} already exists"); return 1
            u = db.User(email=email, name=args.name, password_hash=auth.hash_password(_prompt_password()))
            s.add(u); s.commit()
            print(f"added {email}")
            return 0

        if not u:
            print(f"no user {email}"); return 1

        if args.cmd == "password":
            u.password_hash = auth.hash_password(_prompt_password())
            s.add(u); s.commit(); print("password updated"); return 0

        if args.cmd == "key":
            key, h = auth.new_api_key()
            u.api_key_hash = h
            s.add(u); s.commit()
            print("API key (shown once, store it now):\n")
            print(f"  {key}\n")
            print(f'  curl -H "Authorization: Bearer {key}" http://localhost:8000/api/me')
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
