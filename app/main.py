"""Command centre. Meetings and Slack in, tasks out."""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlmodel import select

from . import db, extract

@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title="Command Centre", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

STATUSES = ["open", "doing", "blocked", "done"]


def require_token(x_ingest_token: str = Header(default="")) -> None:
    """Ingest endpoints accept writes from outside, so they carry a shared secret.

    If INGEST_TOKEN is unset the endpoints refuse rather than running open — an
    unauthenticated write path is worse than a broken one.
    """
    expected = os.environ.get("INGEST_TOKEN", "").strip()
    if not expected:
        raise HTTPException(503, "INGEST_TOKEN is not configured on the server")
    if x_ingest_token != expected:
        raise HTTPException(401, "bad or missing X-Ingest-Token")


# ---------- board ----------

@app.get("/", response_class=HTMLResponse)
def board(request: Request):
    with db.session() as s:
        tasks = s.exec(select(db.Task).order_by(db.Task.updated_at.desc())).all()
    columns = {st: [t for t in tasks if t.status == st] for st in STATUSES}
    return templates.TemplateResponse(
        "board.html",
        {"request": request, "columns": columns, "statuses": STATUSES, "total": len(tasks)},
    )


@app.post("/tasks/{task_id}/status")
def set_status(task_id: int, status: str = Form(...)):
    if status not in STATUSES:
        raise HTTPException(400, f"status must be one of {STATUSES}")
    with db.session() as s:
        task = s.get(db.Task, task_id)
        if not task:
            raise HTTPException(404, "no such task")
        task.status = status
        task.updated_at = db.now()
        s.add(task)
        s.commit()
    return RedirectResponse("/", status_code=303)


# ---------- api ----------

class TaskIn(BaseModel):
    title: str
    detail: str = ""
    owner: str = "unassigned"
    priority: str = "normal"
    due: Optional[str] = None
    tags: str = ""
    source: str = "manual"
    source_ref: str = ""


@app.get("/api/tasks")
def list_tasks(status: Optional[str] = None, owner: Optional[str] = None):
    with db.session() as s:
        q = select(db.Task)
        if status:
            q = q.where(db.Task.status == status)
        if owner:
            q = q.where(db.Task.owner == owner)
        tasks = s.exec(q.order_by(db.Task.updated_at.desc())).all()
    return {"count": len(tasks), "tasks": [t.model_dump() for t in tasks]}


@app.post("/api/tasks", status_code=201)
def create_task(payload: TaskIn):
    with db.session() as s:
        task = db.Task(**payload.model_dump())
        s.add(task)
        s.commit()
        s.refresh(task)
    return task.model_dump()


# ---------- ingest ----------

class MeetingIn(BaseModel):
    title: str
    transcript: str
    occurred_at: Optional[str] = None
    participants: list[str] = []
    commit: bool = False  # False previews the tasks; True writes them to the board


@app.post("/api/ingest/meeting", dependencies=[Depends(require_token)])
def ingest_meeting(payload: MeetingIn):
    """Extract tasks from a transcript.

    Defaults to a preview so a bad extraction never silently pollutes the board —
    call again with commit=true once the output looks right.
    """
    try:
        found = extract.extract(payload.transcript, payload.participants)
    except extract.NotConfigured as e:
        raise HTTPException(503, str(e))

    if not payload.commit:
        return {"committed": False, "count": len(found), "tasks": found}

    with db.session() as s:
        src = db.Source(
            kind="meeting",
            title=payload.title,
            body=payload.transcript,
            occurred_at=payload.occurred_at,
        )
        s.add(src)
        s.commit()
        s.refresh(src)

        written = []
        for t in found:
            task = db.Task(
                title=t["title"][:200],
                detail=(t.get("detail", "") + "\n\n> " + t.get("quote", "")).strip(),
                owner=t.get("owner", "unassigned"),
                priority=t.get("priority", "normal"),
                due=t.get("due"),
                source="meeting",
                source_ref=f"{payload.title} (source {src.id})",
            )
            s.add(task)
            written.append(task)
        s.commit()
        return {"committed": True, "count": len(written), "source_id": src.id}


@app.get("/healthz")
def healthz():
    return {"ok": True}
