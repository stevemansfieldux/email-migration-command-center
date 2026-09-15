# Roadmap

A command centre for a two-person venture. Meetings are the first source; everything else
comes after the first one works. Each phase has a single acceptance test — if it does not
pass, the phase is not done, however much code exists.

---

## Phase 0 — Saturday 12 September: the first meeting becomes the first input

The whole point of tomorrow. Not a feature demo — one moment: the call is recorded, it goes
through the extractor, and the decisions Matt just made appear on a board while he can still
remember making them.

**Deliverables**

- Deployed on Railway with Postgres, `ANTHROPIC_API_KEY` and `INGEST_TOKEN` set — *Steve*
- The call recorded and transcribed. Zoom or Meet with transcription on is the easy route;
  otherwise local recording plus Whisper. **Test the route before the call, not during it** — *Steve*
- `scripts/ingest_meeting.py` run against the transcript: preview, read it, commit — *done, in repo*
- The board opened on a screen share with the tasks on it — *Steve*

**Acceptance:** the board shows Saturday's decisions as tasks, every one traceable to the words
that produced it. Zero tasks nobody agreed to.

**If Railway isn't up in time:** run it locally with SQLite (`uvicorn app.main:app`) and share
the screen. The demo is the extraction, not the hosting.

---

## Phase 1 — Week of 14 September: meetings become routine

Make ingestion something that happens rather than something Steve does.

**Deliverables**

- ~~EMCC polls EMOH on a schedule; a transcript in `meetings/` becomes suggestions with no
  click~~ — done 15 Sep (`EXTRACT_INTERVAL_MIN`)
- ~~Nightly backup of the database off the VM~~ — done 15 Sep (`cc-backup.timer` → `gs://emcc-backups`)
- ~~New task from the board~~ — done 15 Sep
- One transcript route settled and documented; a drop folder or a webhook so a finished
  recording arrives in EMOH without a manual step (Google Meet → Drive → EMOH connector)
- Re-ingesting the same meeting does not duplicate tasks (hash the transcript, match on title
  and date)
- A meeting record with participants, date and a short generated summary, linked from each task
- Task detail page — open a task, see its quote, its source, edit it, close it
- Real owners: Steve and Matt as users rather than free-text strings
- Basic auth on the board. It is currently open, which is fine for a demo and not for anything
  else

**Acceptance:** every meeting in the week is ingested without anyone calling the API by hand,
and the board is the place both of us look rather than a thing Steve maintains.

---

## Phase 2 — Late September: other sources

Meetings are where decisions get made. Slack and email are where they get quietly changed.

**Deliverables**

- Slack event handler wired to the app created from `slack/manifest.yaml`
- Daily Slack digest through the extractor, producing task candidates rather than tasks —
  a human confirms, because chat is noisier than a meeting
- Comments on tasks, so the thread of a decision lives with the task rather than in three places
- Email ingestion once Google Workspace exists — the same preview-then-commit shape

**Acceptance:** a decision made in Slack becomes a task within a day, without anyone re-typing it.

---

## Phase 3 — October: agents that help get it over the line

This is what Matt actually asked for. Everything before it is the plumbing that makes it possible.

**Deliverables**

- Meeting prep: before each call, an agent assembles the agenda from open tasks, stale tasks
  and anything blocked, and posts it to Slack. Steve stops writing the agenda by hand.
- Nudges: tasks with no movement past their due date get a Slack mention to the owner, with
  the original quote, so the nudge carries its own justification
- Weekly digest on Friday: what moved, what didn't, what is blocked on the other person
- Draft follow-ups: for a task that needs an outside party (Mark, a prospect, a vendor), an
  agent drafts the message and leaves it for a human to send

**Acceptance:** Saturday's agenda arrives in Slack on Friday without Steve producing it, and it
is right.

---

## Deliberately not on this roadmap

Goals, metrics dashboards, multi-team permissions, a mobile view, an activity feed. MRO's
command centre has these because it serves a team. This one serves two people, and every one
of those is a week of work that makes the board heavier without making a decision faster.
Revisit when there is a third person.

---

## The order matters

Phase 0 before anything else, because a plan for a system that has never ingested a meeting
is a plan for the wrong system. The first real transcript will change the extraction prompt,
the task shape and probably the board — cheaper to learn that on Saturday than in October.
