"""Ask Claude — an assistant with tools over the board.

Reads are free. Writes (create, update, comment) land immediately but are attributed
to Claude in the data, so a task it created is never mistakable for one a person
committed to. The system prompt tells it to write only when asked.
"""
import os
from typing import Optional

import anthropic
from anthropic import beta_tool
from sqlmodel import select

from . import db

MODEL = os.environ.get("ASK_MODEL", "claude-opus-5")
STATUSES = ["open", "doing", "blocked", "done"]

SYSTEM = """You are the assistant inside a two-person venture's command centre. The people
are Steve and Matt. The board holds tasks extracted from their meetings, each carrying the
verbatim quote that produced it.

Answer from the board, not from guesswork — use the tools to look before you speak. Be direct
and brief; this is a working tool, not a chat.

Only create, update or comment on a task when the user plainly asks you to. If they're
thinking out loud, propose the change and stop. When you do write, say exactly what you wrote.
Never invent a quote or attribute words to a person; if a task needs a source it has one."""


# ---------- tools ----------

@beta_tool
def list_tasks(status: Optional[str] = None, owner: Optional[str] = None) -> str:
    """List tasks on the board, optionally filtered.

    Args:
        status: one of open, doing, blocked, done. Omit for all.
        owner: filter to an owner name, e.g. Steve or Matt. Omit for all.
    """
    with db.session() as s:
        q = select(db.Task)
        if status:
            q = q.where(db.Task.status == status)
        if owner:
            q = q.where(db.Task.owner == owner)
        rows = s.exec(q.order_by(db.Task.updated_at.desc())).all()
    if not rows:
        return "No tasks match."
    return "\n".join(
        f"#{t.id} [{t.status}] {t.title} — {t.owner}, {t.priority}"
        + (f", due {t.due}" if t.due else "") + f" (source: {t.source})"
        for t in rows
    )


@beta_tool
def get_task(task_id: int) -> str:
    """Read one task in full, including its detail, quote, and comments.

    Args:
        task_id: the task's numeric id.
    """
    with db.session() as s:
        t = s.get(db.Task, task_id)
        if not t:
            return f"No task #{task_id}."
        comments = s.exec(
            select(db.Comment).where(db.Comment.task_id == task_id).order_by(db.Comment.created_at)
        ).all()
    out = [f"#{t.id} {t.title}", f"status: {t.status} · owner: {t.owner} · priority: {t.priority}"
           + (f" · due {t.due}" if t.due else ""), f"source: {t.source} {t.source_ref}".strip(), "", t.detail or "(no detail)"]
    if comments:
        out += ["", "comments:"] + [f"- {c.author} ({c.created_at:%d %b %H:%M}): {c.body}" for c in comments]
    return "\n".join(out)


@beta_tool
def search_sources(query: str) -> str:
    """Search the ingested meeting transcripts for a word or phrase and return matching lines with context.

    Args:
        query: text to look for, case-insensitive.
    """
    q = query.lower()
    hits = []
    with db.session() as s:
        for src in s.exec(select(db.Source)).all():
            lines = src.body.splitlines()
            for i, line in enumerate(lines):
                if q in line.lower():
                    ctx = " / ".join(l.strip() for l in lines[max(0, i - 1): i + 2] if l.strip())
                    hits.append(f"[{src.title}] {ctx}")
                    if len(hits) >= 12:
                        break
            if len(hits) >= 12:
                break
    return "\n".join(hits) if hits else f"Nothing in the transcripts mentions '{query}'."


@beta_tool
def create_task(title: str, detail: str = "", owner: str = "unassigned", priority: str = "normal", due: Optional[str] = None) -> str:
    """Create a task on the board. Only use this when the user has clearly asked for a task to be created.

    Args:
        title: imperative phrase under 80 characters.
        detail: one or two sentences of context.
        owner: Steve, Matt, or unassigned.
        priority: low, normal, or high.
        due: YYYY-MM-DD, or omit.
    """
    with db.session() as s:
        t = db.Task(title=title[:200], detail=detail, owner=owner, priority=priority, due=due, source="claude")
        s.add(t)
        s.commit()
        s.refresh(t)
    return f"Created #{t.id} {t.title}"


@beta_tool
def update_task(task_id: int, status: Optional[str] = None, owner: Optional[str] = None,
                priority: Optional[str] = None, due: Optional[str] = None, title: Optional[str] = None) -> str:
    """Change fields on an existing task. Only use this when the user has clearly asked for the change.

    Args:
        task_id: the task's numeric id.
        status: open, doing, blocked, or done.
        owner: new owner.
        priority: low, normal, or high.
        due: YYYY-MM-DD, or an empty string to clear.
        title: new title.
    """
    if status and status not in STATUSES:
        return f"status must be one of {STATUSES}"
    with db.session() as s:
        t = s.get(db.Task, task_id)
        if not t:
            return f"No task #{task_id}."
        changed = []
        for k, v in (("status", status), ("owner", owner), ("priority", priority), ("title", title)):
            if v is not None:
                setattr(t, k, v); changed.append(f"{k}={v}")
        if due is not None:
            t.due = due or None; changed.append(f"due={due or 'cleared'}")
        t.updated_at = db.now()
        s.add(t)
        s.commit()
    return f"Updated #{task_id}: " + (", ".join(changed) or "nothing changed")


@beta_tool
def add_comment(task_id: int, body: str) -> str:
    """Add a comment to a task, attributed to Claude. Only use this when the user has clearly asked.

    Args:
        task_id: the task's numeric id.
        body: the comment text.
    """
    with db.session() as s:
        if not s.get(db.Task, task_id):
            return f"No task #{task_id}."
        s.add(db.Comment(task_id=task_id, author="Claude", body=body.strip()))
        s.commit()
    return f"Commented on #{task_id}"


TOOLS = [list_tasks, get_task, search_sources, create_task, update_task, add_comment]


# ---------- run ----------

def ask(history: list[dict]) -> str:
    """Run one turn. `history` is the prior conversation as role/content dicts, last one the new user message."""
    client = anthropic.Anthropic()
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        tools=TOOLS,
        messages=history,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    last = None
    for message in runner:
        last = message
    if last is None:
        return "(no response)"
    if last.stop_reason == "refusal":
        return "(the request was declined)"
    return "".join(b.text for b in last.content if b.type == "text").strip() or "(done)"
