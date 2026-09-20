# EMCC — the venture's command centre

Internal tooling for the email-migration venture (Steve + Matt). Meetings come in from EMOH,
the ops hub; suggestions come out; tasks live in between.

**Not MROSupply's Command Center.** Same shape, different company, nothing shared. If "command
center" is said without qualifying, it means MRO's — ask.

Live at https://34.89.78.186.sslip.io · API docs at `/docs`.

## What it does

- **Meetings** — lists `type: meeting` files in EMOH's `meetings/`. A scheduler polls EMOH every
  `EXTRACT_INTERVAL_MIN` minutes (default 60) and extracts anything not yet in the ledger, so a
  transcript committed to EMOH becomes suggestions on its own; *Fetch & extract* does the same
  now. Each file is processed exactly once.
- **Suggestions** — every action item the extractor finds lands as a *suggestion* for its owner,
  with the verbatim quote. A strip at the top of the board shows yours: Accept, Not mine (hidden
  for you only), or Dismiss. Nothing reaches the board without a person saying yes. If the
  extractor thinks an item re-treads an existing task it flags `dup_of` — never auto-merged.
- **Tasks** — four columns, drag between them, *+ New task* to add one, click a card to open the
  drawer. Tags with descriptions,
  per-task milestones with progress on the card, comments with `@Steve` / `@Matt` mentions.
- **Plan** — the pricing model (one platform, three licences): client savings, payback and our
  margin per list size, with editable assumptions. Same page as the ROI artifact.
- **Inbox** — mentions, assignments, and new suggestions for you. Plus a direct-message thread.
- **Ask Claude** — an assistant with tools over the board and the transcripts.
- **Account** — name, password, API key rotation, your open tasks.
- **Ticket numbers** — every task is `FH-0001` style (derived from its id). `/?task=FH-0007` opens
  the drawer on it, `/tasks/FH-0007` is its page, and every `/api/tasks/{id}` route takes the
  ref or the bare number.
- **An API** for all of it, documented live at `/docs`.

## EMOH — the ops hub

`stevemansfieldux/email-migration-ops-hub`. Markdown with YAML frontmatter. Transcripts go in
`meetings/` verbatim as `type: meeting`; Gemini's notes sit beside them as `type: notes` and are
skipped. EMCC reads it over the GitHub API with a read-only token (`GITHUB_TOKEN`), or from a
local checkout when `EMOH_PATH` is set.

## Users and the API

Session from `/login`, or `Authorization: Bearer <key>`. Everything except `/login` and `/healthz`
requires one. First run on a fresh database: `./deploy.sh --setup` (prompts you for both
passwords, mints Steve's key). Rotate a key any time from `/account`.

`DELETE /api/tasks/{id}` archives; `/restore` brings it back. Nothing is hard-deleted.
Every write records who did it.

## Run it locally

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set EMOH_PATH=~/email-migration-ops-hub to read the local checkout
.venv/bin/python scripts/user.py setup
uvicorn app.main:app --reload
```

SQLite file, nothing else to set up.

## Deploy

GCP VM `cc` in project `steve-command-center`. `./deploy.sh` syncs the working tree over
`gcloud compute ssh` and restarts — no GitHub access on the box, no deploy key anywhere.
`./deploy.sh --env NAME` sets a variable in the box's `.env` (hidden input);
`--logs`, `--ssh`, `--setup`, `--backup`.

**Backups.** `cc-backup.timer` runs `deploy/backup.sh` nightly at 03:15 UTC: an online SQLite
copy, gzipped, uploaded to `gs://emcc-backups` (30-day lifecycle; the VM's service account has
`objectAdmin` on that bucket only). `./deploy.sh --backup` runs one now and lists the bucket.
Restore steps are at the top of `deploy/backup.sh`.

Secrets on the box: `SECRET_KEY` (generated), `ANTHROPIC_API_KEY` (from console.anthropic.com,
starts `sk-ant-`), `GITHUB_TOKEN` (fine-grained, Contents: read, EMOH only).

## Not here yet

Slack, Google Meet and Klaviyo connectors. Agents that build the agenda or nudge stale tasks.
Those are on the roadmap behind the first month of real use.
