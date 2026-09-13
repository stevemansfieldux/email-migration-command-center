# Command Centre

Internal tooling for the Klaviyo-migration venture. Meetings and Slack go in; tasks come out.

## What it does today

- **Ingest** — upload a transcript, preview the extracted tasks with their quotes, commit
- **Tasks** — four columns, drag between them, click through to edit and comment
- **Ask Claude** — an assistant with tools over the board: list, read, search the transcripts;
  create, update and comment only when asked, always attributed to Claude
- **Settings** — which credential the SDK will use, a connection test, a profile picker
- **An API** covering all of it, documented live at `/docs`

## Run it locally

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # leave ANTHROPIC_API_KEY empty to use a profile
uvicorn app.main:app --reload
```

With no `DATABASE_URL` it uses a local SQLite file, so there is nothing else to set up.

## Users and the API

Two ways in, both resolving to the same user: a session from `/login`, or an API key as
`Authorization: Bearer <key>`. Everything except `/login` and `/healthz` requires one.

```bash
scripts/user.py add design@stevenmansfield.com --name Steve   # prompts for a password
scripts/user.py add mmenashe@mrosupply.com --name Matt
scripts/user.py key design@stevenmansfield.com                 # mints an API key, shown once
scripts/user.py list
```

Run those yourself — passwords are typed at the prompt and keys print to your terminal only.

The API is documented at `/docs` (interactive) and `/openapi.json`. Tasks: list, create,
get, patch (title, detail, owner, priority, due, status, tags), delete, restore. Comments:
list, add, delete your own. Sources: list, get, ingest. Ask: thread, send, clear. Me: who am I,
who else is here.

**Delete archives.** `DELETE /api/tasks/{id}` hides a task from the board and sets
`archived_at`; `POST /api/tasks/{id}/restore` brings it back; `?include_archived=true` lists
them. Nothing is hard-deleted through the API. Comments are the one exception — small, yours,
and a real delete, limited to your own.

Every write records who did it: `created_by` on tasks, `author` on comments, and tasks the
extractor creates say `extractor via <user>`.

## Signing in with your local Claude

The app holds no key. The Anthropic SDK resolves a credential on its own — `ANTHROPIC_API_KEY`,
then `ANTHROPIC_AUTH_TOKEN`, then an OAuth profile from the Anthropic CLI — and for local use
the profile is the intended route:

```bash
brew install anthropics/tap/ant
xattr -d com.apple.quarantine "$(brew --prefix)/bin/ant"
ant auth login          # opens a browser, stores a profile under ~/.config/anthropic/
ant auth status         # shows which org and workspace it's bound to
```

The Settings tab reports which source is in use and has a Test button. Two traps: a set
`ANTHROPIC_API_KEY` silently wins over any profile, and Claude Code may warn about an auth
conflict with its own login — keep one.

## Deploy

Railway. Point it at this repo, add a Postgres service, and set `ANTHROPIC_API_KEY` and
`INGEST_TOKEN` in the environment. `DATABASE_URL` is injected automatically. `railway.json`
already carries the start command.

## Ingesting a meeting

The Ingest tab takes a `.txt`, `.vtt`, `.srt` or Gemini/Google Docs `.docx`. Preview first —
this is the default, and it exists so a bad extraction never quietly fills the board. Same
over the API:

```bash
curl -s https://HOST/api/ingest/meeting \
  -H "Authorization: Bearer $CC_KEY" -H 'Content-Type: application/json' \
  -d '{"title":"Sat 12 Sep planning","participants":["Steve","Matt"],
       "transcript":"...", "commit":false}' | jq
```

Add `"commit":true` once the output looks right.

Every extracted task carries the verbatim quote it came from, appended to its detail, so a
task can always be traced back to the thing somebody actually said.

## Slack

`slack/manifest.yaml` creates the app. Install it to the workspace, set `SLACK_BOT_TOKEN` and
`SLACK_SIGNING_SECRET`, and update the manifest's `request_url` to the deployed host. The
event handler is not written yet.

## What is deliberately not here

No agent fleet. No email ingestion. No Slack event handler. Those come after the first
working demo.

## House rule

Nothing from `MROSupply/command-center` gets copied into this repo. That is a client's
codebase and this is a separate venture. Build fresh or bring in Steve's own tooling.
