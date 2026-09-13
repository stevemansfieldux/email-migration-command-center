# Working on this repo

Internal command centre for the Klaviyo-migration venture. FastAPI + SQLModel, deployed on
Railway, Postgres in production and SQLite locally.

## The one rule

**Never copy code, schema or config from `MROSupply/command-center`.** That repo belongs to a
client Steve contracts for. This is a separate commercial venture and the boundary needs to
stay clean and visible. If a pattern from there is worth having, rewrite it from scratch.

The same applies in reverse: nothing from this repo goes anywhere near MRO's infrastructure,
worklogs or ops-hub.

## Conventions

- Every route except `/login` and `/healthz` requires a user — session or bearer key. Page
  routes redirect to `/login`; API routes return 401 JSON. Never add an unauthenticated route.
- Deleting a task archives it. Do not add a hard-delete endpoint.
- Passwords and API keys are set through `scripts/user.py`, run by the person themselves.
  Never print a password or a freshly-minted key anywhere but that terminal.
- Extraction previews by default. Anything that writes to the board takes an explicit
  `commit: true`.
- Every extracted task keeps the verbatim quote it came from. Traceability is the point; a
  board full of plausible-sounding tasks nobody agreed to is worse than an empty one.
- Secrets live in the environment, never in the repo. `.env` is gitignored.

## Deploying

Push to `main`. Railway builds and restarts. There is no other deploy path — do not edit
anything on the server.
