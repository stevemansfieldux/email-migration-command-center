# Command Centre

Internal tooling for the Klaviyo-migration venture. Meetings and Slack go in; tasks come out.

## What it does today

- A task board — four columns, editable status, at `/`
- A JSON API at `/api/tasks` for reading and creating tasks
- Transcript ingestion at `/api/ingest/meeting`, which extracts committed action items and
  either previews them or writes them to the board

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in ANTHROPIC_API_KEY and INGEST_TOKEN
uvicorn app.main:app --reload
```

With no `DATABASE_URL` it uses a local SQLite file, so there is nothing else to set up.

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
