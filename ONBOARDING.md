# Onboarding — Matt (and Matt's Claude)

You have the same access Steve has. This is everything needed to go from a blank Mac to
deploying EMCC, setting secrets on the box, and driving it with your own Claude Code session.
Give this file to your Claude; it's written to be followed by one.

## 0. Which command centre

There are two. **EMCC** is the venture's (this repo). MRO's `command-center` is a different
company's app that happens to look the same. If anything says "command center" without
qualifying, ask which. Never copy code between them. Full rule: `CLAUDE.md`.

## 1. Tools

```bash
brew install gh google-cloud-sdk python@3.12
gh auth login                 # GitHub — browser flow
gcloud auth login             # Google — use the account Steve granted IAM to
gcloud config set project steve-command-center
```

## 2. The repos

```bash
cd ~
gh repo clone stevemansfieldux/email-migration-command-center    # EMCC — the app (this)
gh repo clone stevemansfieldux/email-migration-ops-hub           # EMOH — meetings, people, projects
gh repo clone stevemansfieldux/email-migration-service           # the product; empty until the platform decision
```

## 3. Local run (optional, but useful for your Claude to test against)

```bash
cd ~/email-migration-command-center
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
echo "EMOH_PATH=$HOME/email-migration-ops-hub" >> .env     # read the local checkout, no token needed
.venv/bin/python scripts/user.py setup                       # creates local users (this is a separate local db)
.venv/bin/uvicorn app.main:app --reload                      # http://127.0.0.1:8000
```

## 4. Deploying to the live box

The live app is on a GCP VM (`cc`, project `steve-command-center`). You already have IAM.

```bash
cd ~/email-migration-command-center
./deploy.sh              # check → sync working tree → install → restart. ~40s.
./deploy.sh --logs       # tail the service
./deploy.sh --ssh        # a shell on the box
```

There is no GitHub deploy; the box never talks to GitHub except to read EMOH. Commit and push
to `main` too, so the repo matches what's live — but pushing alone deploys nothing.

`deploy.sh` runs `scripts/check.py` first (every template compiles, the app imports) and
refuses to sync if it fails. If you see "not deploying a broken build", that's it working.

## 5. Secrets on the box

Three, all in `/srv/cc/app/.env` on the VM, all set the same way — hidden prompt, value goes
over SSH, never in a command line or a log:

```bash
./deploy.sh --env ANTHROPIC_API_KEY    # from console.anthropic.com — starts sk-ant-; the script refuses anything else
./deploy.sh --env GITHUB_TOKEN         # fine-grained PAT, Contents: read, only the EMOH repo
./deploy.sh --env EXTRACT_MODEL        # optional; defaults to claude-opus-5
```

`SECRET_KEY` (session signing) is generated once and stays. Don't set it by hand.
The Settings tab in the app shows which credentials are present and has a Test button.

## 6. Your account in the app

You have a login (Steve set the first password — change it at `/account`). Mint your own API
key there too; it's shown once. For the API from a script:

```bash
curl -H "Authorization: Bearer $EMCC_KEY" https://34.89.78.186.sslip.io/api/me
```

Docs at `/docs`. Everything the UI does, the API does.

## 7. The loop

1. A meeting happens. Put the transcript in `EMOH/meetings/YYYY-MM-DD-topic.md` with
   `type: meeting` frontmatter (template in EMOH's README). Push.
2. In EMCC → Meetings → **Fetch & extract**. Each new file is processed once.
3. Suggestions appear on the board for whoever they're for. Accept / Not mine / Dismiss.
   Nothing lands without a person saying yes.

## 8. What you can't do yet — and why

The GCP project, the Anthropic console, and the GitHub repos are all on Steve's accounts. You
have full *operational* access — deploy, secrets, SSH, admin on the repos — but not
*ownership*. That's not a trust thing; it's that there's no venture entity yet to own them.
When there is, they move. Until then, if Steve vanished the lights would stay on but the bills
would land on him.
