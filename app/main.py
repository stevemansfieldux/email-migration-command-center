"""Command centre. Meetings and Slack in, tasks out."""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import html
import json

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlmodel import select

from . import ask as askmod, auth_status, db, extract

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_env() -> None:
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip())


def write_env(key: str, value: str) -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    lines = [l for l in lines if not l.startswith(f"{key}=")]
    if value:
        lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n")


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_env()
    db.init()
    yield


app = FastAPI(title="Command Centre", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
LOGOS = Path(__file__).parent / "static" / "logos"


def logo(slug: str) -> Optional[str]:
    """Inline brand mark from app/static/logos/<slug>.svg, recoloured via currentColor.
    Returns None when there's no file, so the template falls back to a monogram."""
    f = LOGOS / f"{slug}.svg"
    if not f.exists():
        return None
    svg = f.read_text()
    svg = svg.replace("<svg ", '<svg fill="currentColor" width="18" height="18" aria-hidden="true" ', 1)
    return svg


templates.env.globals["logo"] = logo

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


# ---------- helpers ----------

def split_detail(detail: str) -> tuple[str, list[str]]:
    """Separate the free-text body from the '> quoted' lines the extractor appends."""
    body, quotes = [], []
    for line in (detail or "").splitlines():
        if line.startswith("> "):
            quotes.append(line[2:])
        else:
            body.append(line)
    return "\n".join(body).strip(), quotes


PATCHABLE = {"title", "detail", "owner", "priority", "due", "status", "tags"}


def _apply(task: db.Task, fields: dict) -> None:
    for k, v in fields.items():
        if k not in PATCHABLE:
            continue
        if k == "status" and v not in STATUSES:
            raise HTTPException(400, f"status must be one of {STATUSES}")
        setattr(task, k, v if v != "" else (None if k == "due" else ""))
    task.updated_at = db.now()


# ---------- board ----------

@app.get("/", response_class=HTMLResponse)
def board(request: Request):
    with db.session() as s:
        tasks = s.exec(select(db.Task).order_by(db.Task.updated_at.desc())).all()
    columns = {st: [t for t in tasks if t.status == st] for st in STATUSES}
    return templates.TemplateResponse(
        "board.html",
        {"request": request, "columns": columns, "statuses": STATUSES, "total": len(tasks), "tab": "tasks"},
    )


@app.post("/tasks/{task_id}/status")
def set_status(task_id: int, status: str = Form(...)):
    """Select-menu fallback on the board; drag-and-drop uses PATCH /api/tasks/{id}."""
    with db.session() as s:
        task = s.get(db.Task, task_id)
        if not task:
            raise HTTPException(404, "no such task")
        _apply(task, {"status": status})
        s.add(task)
        s.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_page(request: Request, task_id: int):
    with db.session() as s:
        task = s.get(db.Task, task_id)
        if not task:
            raise HTTPException(404, "no such task")
        comments = s.exec(
            select(db.Comment).where(db.Comment.task_id == task_id).order_by(db.Comment.created_at)
        ).all()
    body, quotes = split_detail(task.detail)
    return templates.TemplateResponse("task.html", {
        "request": request, "tab": "tasks", "t": task, "body": body, "quotes": quotes,
        "comments": comments, "statuses": STATUSES,
    })


@app.post("/tasks/{task_id}")
def task_update(
    task_id: int,
    title: str = Form(...),
    detail: str = Form(""),
    owner: str = Form("unassigned"),
    priority: str = Form("normal"),
    due: str = Form(""),
    status: str = Form("open"),
):
    with db.session() as s:
        task = s.get(db.Task, task_id)
        if not task:
            raise HTTPException(404, "no such task")
        # Keep the extractor's quote lines; the form only edits the free-text body.
        _, quotes = split_detail(task.detail)
        merged = detail.strip() + ("\n\n" + "\n".join("> " + q for q in quotes) if quotes else "")
        _apply(task, {"title": title, "detail": merged, "owner": owner, "priority": priority, "due": due, "status": status})
        s.add(task)
        s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/comments")
def add_comment(task_id: int, author: str = Form(...), body: str = Form(...)):
    if not body.strip():
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)
    with db.session() as s:
        if not s.get(db.Task, task_id):
            raise HTTPException(404, "no such task")
        s.add(db.Comment(task_id=task_id, author=author.strip() or "someone", body=body.strip()))
        task = s.get(db.Task, task_id)
        task.updated_at = db.now()
        s.add(task)
        s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


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


class TaskPatch(BaseModel):
    title: Optional[str] = None
    detail: Optional[str] = None
    owner: Optional[str] = None
    priority: Optional[str] = None
    due: Optional[str] = None
    status: Optional[str] = None
    tags: Optional[str] = None


@app.patch("/api/tasks/{task_id}")
def patch_task(task_id: int, payload: TaskPatch):
    with db.session() as s:
        task = s.get(db.Task, task_id)
        if not task:
            raise HTTPException(404, "no such task")
        _apply(task, payload.model_dump(exclude_none=True))
        s.add(task)
        s.commit()
        s.refresh(task)
    return task.model_dump()


@app.post("/api/tasks", status_code=201)
def create_task(payload: TaskIn):
    with db.session() as s:
        task = db.Task(**payload.model_dump())
        s.add(task)
        s.commit()
        s.refresh(task)
    return task.model_dump()


# ---------- ingest ----------

def _commit_meeting(title: str, transcript: str, occurred_at: Optional[str], found: list[dict]) -> dict:
    """Write a meeting source and its extracted tasks. Used by both the API and the UI."""
    with db.session() as s:
        src = db.Source(kind="meeting", title=title, body=transcript, occurred_at=occurred_at)
        s.add(src)
        s.commit()
        s.refresh(src)
        for t in found:
            s.add(db.Task(
                title=t["title"][:200],
                detail=(t.get("detail", "") + "\n\n> " + t.get("quote", "")).strip(),
                owner=t.get("owner", "unassigned"),
                priority=t.get("priority", "normal"),
                due=t.get("due"),
                source="meeting",
                source_ref=f"{title} (source {src.id})",
            ))
        s.commit()
        return {"committed": True, "count": len(found), "source_id": src.id}


@app.get("/ingest", response_class=HTMLResponse)
def ingest_page(request: Request):
    return templates.TemplateResponse("ingest.html", {"request": request, "tab": "ingest", "preview": None, "error": None})


@app.post("/ingest", response_class=HTMLResponse)
async def ingest_upload(
    request: Request,
    title: str = Form(...),
    participants: str = Form(""),
    date: str = Form(""),
    transcript: UploadFile = File(...),
):
    """Upload a transcript, run extraction, show the result. Writes nothing."""
    raw = (await transcript.read()).decode("utf-8", errors="replace")
    who = [p.strip() for p in participants.split(",") if p.strip()]
    ctx = {"request": request, "tab": "ingest", "preview": None, "error": None}
    if not raw.strip():
        ctx["error"] = "That file is empty."
        return templates.TemplateResponse("ingest.html", ctx)
    try:
        found = extract.extract(raw, who)
    except extract.NotConfigured as e:
        ctx["error"] = f"{e} — see Settings. The upload was read fine; nothing was written."
        return templates.TemplateResponse("ingest.html", ctx)
    payload = {"title": title, "transcript": raw, "occurred_at": date or None, "tasks": found}
    ctx["preview"] = {"title": title, "tasks": found, "payload_json": html.escape(json.dumps(payload), quote=True)}
    return templates.TemplateResponse("ingest.html", ctx)


@app.post("/ingest/commit")
def ingest_commit(payload: str = Form(...)):
    """Write the previewed tasks. The preview carried them here so extraction isn't re-run."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(400, "bad payload")
    _commit_meeting(data["title"], data["transcript"], data.get("occurred_at"), data.get("tasks", []))
    return RedirectResponse("/", status_code=303)


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
    return _commit_meeting(payload.title, payload.transcript, payload.occurred_at, found)


# ---------- ask ----------

@app.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request, error: Optional[str] = None):
    with db.session() as s:
        thread = s.exec(select(db.ChatMessage).order_by(db.ChatMessage.created_at)).all()
    return templates.TemplateResponse("ask.html", {"request": request, "tab": "ask", "thread": thread, "error": error, "model": askmod.MODEL})


@app.post("/ask")
def ask_send(request: Request, message: str = Form(...)):
    text = message.strip()
    if not text:
        return RedirectResponse("/ask", status_code=303)
    with db.session() as s:
        s.add(db.ChatMessage(role="user", content=text))
        s.commit()
        history = [{"role": m.role, "content": m.content}
                   for m in s.exec(select(db.ChatMessage).order_by(db.ChatMessage.created_at)).all()]
    import anthropic
    try:
        reply = askmod.ask(history)
    except anthropic.AuthenticationError:
        return RedirectResponse("/ask?error=The+API+rejected+the+credential+%E2%80%94+see+Settings.", status_code=303)
    except TypeError as e:
        if not auth_status.is_no_credential(e):
            raise
        return RedirectResponse("/ask?error=No+Anthropic+credential+on+this+machine+%E2%80%94+see+Settings.", status_code=303)
    except anthropic.APIStatusError as e:
        return RedirectResponse(f"/ask?error=API+error+{e.status_code}", status_code=303)
    except anthropic.APIConnectionError:
        return RedirectResponse("/ask?error=Network+error+reaching+the+API.", status_code=303)
    with db.session() as s:
        s.add(db.ChatMessage(role="assistant", content=reply))
        s.commit()
    return RedirectResponse("/ask", status_code=303)


@app.post("/ask/clear")
def ask_clear():
    with db.session() as s:
        for m in s.exec(select(db.ChatMessage)).all():
            s.delete(m)
        s.commit()
    return RedirectResponse("/ask", status_code=303)


# ---------- settings ----------

def _settings_ctx(request: Request, result=None) -> dict:
    return {"request": request, "tab": "settings", "st": auth_status.status(),
            "model": askmod.MODEL, "extract_model": os.environ.get("EXTRACT_MODEL", "claude-opus-5"), "result": result}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", _settings_ctx(request))


@app.post("/settings/test", response_class=HTMLResponse)
def settings_test(request: Request):
    ok, msg = auth_status.test_connection(askmod.MODEL)
    return templates.TemplateResponse("settings.html", _settings_ctx(request, {"ok": ok, "msg": msg}))


@app.post("/settings/profile")
def settings_profile(profile: str = Form("")):
    """Pick which `ant` profile this app uses. Persists to .env; takes effect immediately."""
    profile = profile.strip()
    if profile:
        os.environ["ANTHROPIC_PROFILE"] = profile
    else:
        os.environ.pop("ANTHROPIC_PROFILE", None)
    write_env("ANTHROPIC_PROFILE", profile)
    return RedirectResponse("/settings", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True}
