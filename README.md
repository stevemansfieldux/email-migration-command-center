# Command Centre

Internal tooling for the Klaviyo-migration venture. Meetings and Slack go in; tasks come out.

## What it does today

- **Ingest** — upload a transcript, preview the extracted tasks with their quotes, commit
- **Tasks** — four columns, drag between them, click through to edit and comment
- **Ask Claude** — an assistant with tools over the board: list, read, search the transcripts;
  create, update and comment only when asked, always attributed to Claude
- **Settings** — which credential the SDK will use, a connection test, a profile picker
- A JSON API at `/api/tasks` (list, create, PATCH) and `/api/ingest/meeting`

## Run it locally

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # set INGEST_TOKEN; leave ANTHROPIC_API_KEY empty to use a profile
uvicorn app.main:app --reload
```

With no `DATABASE_URL` it uses a local SQLite file, so there is nothing else to set up.

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

Preview first — this is the default, and it exists so a bad extraction never quietly fills
the board:

```bash
curl -s https://HOST/api/ingest/meeting \
  -H "X-Ingest-Token: $INGEST_TOKEN" -H 'Content-Type: application/json' \
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

No authentication on the board — it is unlisted, not secure, and should not hold anything
sensitive until that changes. No agent fleet. No email ingestion. No Slack event handler.
Those come after the first working demo.

## House rule

Nothing from `MROSupply/command-center` gets copied into this repo. That is a client's
codebase and this is a separate venture. Build fresh or bring in Steve's own tooling.
