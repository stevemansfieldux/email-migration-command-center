# Working on this repo — EMCC

The **venture's** command centre (Steve + Matt). FastAPI + SQLModel, SQLite, on a GCP VM.

## The two rules

**1. "Command center" means MRO's unless qualified.** MROSupply has `command-center` and
`ops-hub`; the venture has **EMCC** (this) and **EMOH** (`email-migration-ops-hub`). Same shape,
different companies. If a request says "command center" or "ops hub" without saying which,
ask before touching anything.

**2. Never copy code, schema or config between the two.** Features are mirrored by rebuilding.
The MRO repos belong to a client Steve contracts for; this is a separate venture. Nothing from
here goes near MRO's infrastructure, worklogs or ops-hub, and nothing from there comes here.

## Conventions

- Every route except `/login` and `/healthz` requires a user — session or bearer key. Page
  routes redirect to `/login`; API routes return 401 JSON. Never add an unauthenticated route.
- Deleting a task archives it. Do not add a hard-delete endpoint.
- Passwords and API keys are set through `scripts/user.py`, run by the person themselves.
  Never print a password or a freshly-minted key anywhere but that terminal.
- The extractor writes **suggestions**, never tasks. A person accepts. Keep it that way.
- Every extracted task keeps the verbatim quote it came from. Traceability is the point; a
  board full of plausible-sounding tasks nobody agreed to is worse than an empty one.
- `ExtractedMeeting` is the ledger: a meeting file is processed exactly once. Don't bypass it.
- Sample or placeholder data must be unmistakable in the data itself (`source: sample`, a
  fictional participant) — never a real person's name next to invented words.
- Secrets live in the environment, never in the repo. `.env` is gitignored.

## Deploying

`./deploy.sh` from the Mac. Commit and push to `main` too, but GitHub is not the deploy path —
the box never talks to GitHub except to read EMOH. Do not edit anything on the server by hand.
