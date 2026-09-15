"""Turn meeting transcripts from EMOH into suggestions.

Deliberately conservative: better to miss a task than fill the board with things nobody
agreed to. Every suggestion carries the verbatim quote it came from. The extractor is shown
the existing tasks so it can flag a re-tread as dup_of instead of creating a twin — flagged,
never auto-merged; a person decides.

run() is the pathway: walk EMOH meetings/, skip anything already in the ledger, extract,
write suggestions, record the ledger. It reports progress into STATUS for the UI to poll.
"""
import os
import threading
from datetime import datetime, timezone
from typing import Any

from sqlmodel import select

from . import db, kb

MODEL = os.environ.get("EXTRACT_MODEL", "claude-opus-5")

SYSTEM = """You extract action items from a meeting between two business partners, Steve and Matt.

Return ONLY tasks that someone actually committed to, or that the conversation clearly left as
a required next step. Do not invent work, do not turn opinions into tasks, and do not create a
task for something already described as done.

You are also shown the EXISTING tasks on their board. If an action item is plainly the same
piece of work as an existing task, still return it but set dup_of to that task's id so a human
can merge or dismiss it. Do not silently drop it and do not guess — only set dup_of when it is
clearly the same work.

For each task give:
  title      an imperative phrase under 80 characters, starting with a verb
  detail     one or two sentences of context, enough to act on without the transcript
  owner      Steve, Matt, or "unassigned" if genuinely unclear
  priority   low | normal | high
  due        YYYY-MM-DD if a date was actually stated, otherwise null
  quote      the shortest verbatim span from the transcript that supports this task
  dup_of     the id of an existing task this duplicates, otherwise null

If nothing was committed to, return an empty list. That is a valid and common answer."""

TOOL = {
    "name": "record_tasks",
    "description": "Record the action items found in the transcript.",
    "input_schema": {
        "type": "object",
        "properties": {"tasks": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"}, "detail": {"type": "string"}, "owner": {"type": "string"},
                "priority": {"enum": ["low", "normal", "high"]}, "due": {"type": ["string", "null"]},
                "quote": {"type": "string"}, "dup_of": {"type": ["integer", "null"]},
            },
            "required": ["title", "detail", "owner", "priority", "quote"],
        }}},
        "required": ["tasks"],
    },
}


class NotConfigured(RuntimeError):
    """No usable Anthropic credential resolved."""


def _existing() -> list[dict]:
    with db.session() as s:
        rows = s.exec(select(db.Task).where(db.Task.archived_at.is_(None), db.Task.status.notin_(["dismissed"]))).all()
    return [{"id": t.id, "title": t.title, "status": t.status, "owner": t.owner or t.suggested_owner} for t in rows]


def extract(text: str, participants: list[str] | None = None, existing: list[dict] | None = None) -> list[dict[str, Any]]:
    import anthropic
    who = ", ".join(participants) if participants else "Steve, Matt"
    ex = existing if existing is not None else _existing()
    ex_block = "\n".join(f"  #{t['id']} [{t['status']}] {t['title']} — {t['owner']}" for t in ex) or "  (none)"
    client = anthropic.Anthropic()
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=8000, system=SYSTEM, tools=[TOOL],
            tool_choice={"type": "tool", "name": "record_tasks"},
            messages=[{"role": "user", "content": f"Participants: {who}\n\nExisting tasks:\n{ex_block}\n\nTranscript:\n\n{text}"}],
        )
    except anthropic.AuthenticationError as e:
        raise NotConfigured("The API rejected the credential — check ANTHROPIC_API_KEY") from e
    except TypeError as e:
        from .auth_status import is_no_credential
        if not is_no_credential(e):
            raise
        raise NotConfigured("No Anthropic credential resolved — set ANTHROPIC_API_KEY") from e
    for block in resp.content:
        if block.type == "tool_use" and block.name == "record_tasks":
            return block.input.get("tasks", [])
    return []


# ---------- the pathway ----------

STATUS: dict[str, Any] = {"running": False, "started": None, "finished": None, "done": 0, "total": 0, "current": None, "created": 0, "error": None, "log": [], "by": None, "next_auto": None}
_lock = threading.Lock()


def _owner_id(name: str) -> int | None:
    with db.session() as s:
        u = s.exec(select(db.User).where(db.User.name == name)).first()
        return u.id if u else None


def run(limit: int | None = None, by: str = "system") -> dict:
    """Process every unextracted meeting. Safe to call repeatedly; the ledger makes it idempotent."""
    if not _lock.acquire(blocking=False):
        return {"started": False, "reason": "already running"}
    try:
        STATUS.update(running=True, started=datetime.now(timezone.utc).isoformat(), finished=None, done=0, created=0, current=None, error=None, log=[], by=by)
        meetings = kb.list_meetings()
        with db.session() as s:
            seen = {r.path for r in s.exec(select(db.ExtractedMeeting)).all()}
        todo = [m for m in meetings if m["path"] not in seen]
        if limit:
            todo = todo[:limit]
        STATUS["total"] = len(todo)
        for m in todo:
            STATUS["current"] = m["path"]
            text = kb.read(m["path"])
            attendees = [a.replace("-", " ").title().split()[0] for a in (m.get("attendees") or [])]
            found = extract(text, attendees or None)
            created = 0
            with db.session() as s:
                for t in found:
                    task = db.Task(
                        title=t["title"][:200],
                        detail=(t.get("detail", "") + "\n\n> " + t.get("quote", "")).strip(),
                        owner="", suggested_owner=t.get("owner") or "unassigned",
                        priority=t.get("priority", "normal"), due=t.get("due"),
                        status="suggested", source="meeting", source_ref=m["title"],
                        source_meeting=m["path"], meeting_date=m.get("date"),
                        dup_of=t.get("dup_of"), created_by=f"extractor via {by}",
                    )
                    s.add(task); s.commit(); s.refresh(task)
                    s.add(db.TaskEvent(task_id=task.id, actor="extractor", kind="created", detail=f"suggested from {m['path']}"))
                    created += 1
                    if (uid := _owner_id(task.suggested_owner)):
                        s.add(db.Notification(recipient_id=uid, actor="extractor", kind="suggestion", task_id=task.id,
                                              text=f"Suggested for you from {m['title']}: {task.title}"))
                s.add(db.ExtractedMeeting(path=m["path"], task_count=created))
                s.commit()
            STATUS["done"] += 1; STATUS["created"] += created
            STATUS["log"].append(f"{m['path']}: {created} suggestion(s)")
        STATUS["current"] = None
        return {"started": True, "meetings": len(todo), "created": STATUS["created"]}
    except Exception as e:  # noqa: BLE001 — recorded for the UI, then re-raised for logs
        STATUS["error"] = str(e)
        raise
    finally:
        STATUS.update(running=False, finished=datetime.now(timezone.utc).isoformat())
        _lock.release()


def run_in_background(by: str) -> bool:
    if STATUS["running"]:
        return False
    threading.Thread(target=run, kwargs={"by": by}, daemon=True).start()
    return True


# ---------- scheduler ----------
# Meetings land in EMOH on their own schedule; the board should notice without anyone clicking
# "Fetch & extract". A daemon thread polls every EXTRACT_INTERVAL_MIN minutes (default 60,
# 0 disables). The ledger makes a poll that finds nothing new cost one GitHub tree read.

def configured() -> bool:
    """True when both halves of the pathway have credentials: EMOH to read, Anthropic to extract."""
    return bool(os.environ.get("EMOH_PATH") or os.environ.get("GITHUB_TOKEN")) and bool(os.environ.get("ANTHROPIC_API_KEY"))


def interval_min() -> int:
    try:
        return max(0, int(os.environ.get("EXTRACT_INTERVAL_MIN", "60")))
    except ValueError:
        return 60


def _scheduler_loop(minutes: int) -> None:
    import logging, time
    log = logging.getLogger("uvicorn.error")
    while True:
        STATUS["next_auto"] = datetime.fromtimestamp(time.time() + minutes * 60, timezone.utc).isoformat()
        time.sleep(minutes * 60)
        if not configured():
            continue
        try:
            kb.clear_cache()
            r = run(by="scheduler")
            if r.get("started") and r.get("meetings"):
                log.info("extract scheduler: %s meeting(s), %s suggestion(s)", r["meetings"], r["created"])
        except Exception as e:  # noqa: BLE001 — a bad poll must not kill the scheduler
            log.warning("extract scheduler: %s", e)


def start_scheduler() -> bool:
    minutes = interval_min()
    if not minutes:
        return False
    threading.Thread(target=_scheduler_loop, args=(minutes,), daemon=True, name="extract-scheduler").start()
    return True
