"""Command centre. Meetings and Slack in, tasks out. Two people, one board, one API."""
import html
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlmodel import select
from starlette.middleware.sessions import SessionMiddleware

from . import ask as askmod, auth, auth_status, db, extract

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
STATUSES = ["open", "doing", "blocked", "done"]
PRIORITIES = ["low", "normal", "high"]


# ---------- env ----------

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


def secret_key() -> str:
    """Session signing key. Generated once and persisted if absent, so a restart
    doesn't log everyone out and a fresh clone still works."""
    k = os.environ.get("SECRET_KEY", "").strip()
    if not k:
        k = secrets.token_urlsafe(48)
        os.environ["SECRET_KEY"] = k
        write_env("SECRET_KEY", k)
    return k


load_env()


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title="Command Centre API", version="0.2", lifespan=lifespan,
              description="Tasks, comments, sources, and Ask Claude. Auth: session cookie from /login, "
                          "or `Authorization: Bearer <api key>` minted with scripts/user.py.")
app.add_middleware(SessionMiddleware, secret_key=secret_key(), same_site="lax", https_only=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
LOGOS = Path(__file__).parent / "static" / "logos"


def logo(slug: str) -> Optional[str]:
    f = LOGOS / f"{slug}.svg"
    if not f.exists():
        return None
    return f.read_text().replace("<svg ", '<svg fill="currentColor" width="18" height="18" aria-hidden="true" ', 1)


templates.env.globals["logo"] = logo


@app.exception_handler(auth.LoginRequired)
async def _login_redirect(request: Request, _: auth.LoginRequired):
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


def page(request: Request, name: str, user: Optional[db.User], **ctx):
    return templates.TemplateResponse(name, {"request": request, "user": user, **ctx})


def owners() -> list[str]:
    with db.session() as s:
        return [u.name for u in s.exec(select(db.User).order_by(db.User.id)).all()]


# ---------- helpers ----------

def split_detail(detail: str) -> tuple[str, list[str]]:
    body, quotes = [], []
    for line in (detail or "").splitlines():
        (quotes if line.startswith("> ") else body).append(line[2:] if line.startswith("> ") else line)
    return "\n".join(body).strip(), quotes


PATCHABLE = {"title", "detail", "owner", "priority", "due", "status", "tags"}


def _apply(task: db.Task, fields: dict) -> None:
    for k, v in fields.items():
        if k not in PATCHABLE:
            continue
        if k == "status" and v not in STATUSES:
            raise HTTPException(400, f"status must be one of {STATUSES}")
        if k == "priority" and v not in PRIORITIES:
            raise HTTPException(400, f"priority must be one of {PRIORITIES}")
        if k == "title" and not str(v).strip():
            raise HTTPException(400, "title cannot be empty")
        setattr(task, k, v if v != "" else (None if k == "due" else ""))
    task.updated_at = db.now()


def _get_task(s, task_id: int, allow_archived: bool = False) -> db.Task:
    t = s.get(db.Task, task_id)
    if not t or (t.archived_at and not allow_archived):
        raise HTTPException(404, "no such task")
    return t


# ---------- login ----------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: Optional[str] = None):
    if auth.resolve(request):
        return RedirectResponse(next or "/", status_code=303)
    return page(request, "login.html", None, next=next, error=error, tab=None)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/")):
    u = auth.user_by_email(email)
    if not u or not auth.check_password(password, u.password_hash):
        return page(request, "login.html", None, next=next, error="That email and password don't match.", tab=None)
    request.session["uid"] = u.id
    return RedirectResponse(next if next.startswith("/") else "/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------- board ----------

@app.get("/", response_class=HTMLResponse)
def board(request: Request, user: db.User = auth.PageUser):
    with db.session() as s:
        tasks = s.exec(select(db.Task).where(db.Task.archived_at.is_(None)).order_by(db.Task.updated_at.desc())).all()
    columns = {st: [t for t in tasks if t.status == st] for st in STATUSES}
    return page(request, "board.html", user, columns=columns, statuses=STATUSES, total=len(tasks), tab="tasks")


@app.post("/tasks/{task_id}/status")
def set_status(task_id: int, status: str = Form(...), user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id)
        _apply(t, {"status": status})
        s.add(t); s.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_page(request: Request, task_id: int, user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id, allow_archived=True)
        comments = s.exec(select(db.Comment).where(db.Comment.task_id == task_id).order_by(db.Comment.created_at)).all()
    body, quotes = split_detail(t.detail)
    return page(request, "task.html", user, t=t, body=body, quotes=quotes, comments=comments,
                statuses=STATUSES, priorities=PRIORITIES, owners=owners(), tab="tasks")


@app.post("/tasks/{task_id}")
def task_update(task_id: int, title: str = Form(...), detail: str = Form(""), owner: str = Form("unassigned"),
                priority: str = Form("normal"), due: str = Form(""), status: str = Form("open"),
                user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id)
        _, quotes = split_detail(t.detail)
        merged = detail.strip() + ("\n\n" + "\n".join("> " + q for q in quotes) if quotes else "")
        _apply(t, {"title": title, "detail": merged, "owner": owner, "priority": priority, "due": due, "status": status})
        s.add(t); s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/comments")
def add_comment_ui(task_id: int, body: str = Form(...), user: db.User = auth.PageUser):
    if body.strip():
        with db.session() as s:
            t = _get_task(s, task_id)
            s.add(db.Comment(task_id=task_id, author=user.name, body=body.strip()))
            t.updated_at = db.now(); s.add(t); s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/archive")
def archive_ui(task_id: int, user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id); t.archived_at = db.now(); t.updated_at = db.now(); s.add(t); s.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/tasks/{task_id}/restore")
def restore_ui(task_id: int, user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id, allow_archived=True); t.archived_at = None; t.updated_at = db.now(); s.add(t); s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


# ---------- API: identity ----------

@app.get("/api/me", tags=["me"])
def me(user: db.User = auth.CurrentUser):
    return {"id": user.id, "email": user.email, "name": user.name}


@app.get("/api/users", tags=["me"])
def users(user: db.User = auth.CurrentUser):
    with db.session() as s:
        return [{"id": u.id, "email": u.email, "name": u.name} for u in s.exec(select(db.User).order_by(db.User.id)).all()]


# ---------- API: tasks ----------

class TaskIn(BaseModel):
    title: str
    detail: str = ""
    owner: str = "unassigned"
    priority: str = "normal"
    due: Optional[str] = None
    tags: str = ""
    source: str = "manual"
    source_ref: str = ""


class TaskPatch(BaseModel):
    title: Optional[str] = None
    detail: Optional[str] = None
    owner: Optional[str] = None
    priority: Optional[str] = None
    due: Optional[str] = None
    status: Optional[str] = None
    tags: Optional[str] = None


@app.get("/api/tasks", tags=["tasks"])
def list_tasks(status: Optional[str] = None, owner: Optional[str] = None, include_archived: bool = False,
               user: db.User = auth.CurrentUser):
    with db.session() as s:
        q = select(db.Task)
        if not include_archived:
            q = q.where(db.Task.archived_at.is_(None))
        if status:
            q = q.where(db.Task.status == status)
        if owner:
            q = q.where(db.Task.owner == owner)
        rows = s.exec(q.order_by(db.Task.updated_at.desc())).all()
    return {"count": len(rows), "tasks": [t.model_dump() for t in rows]}


@app.post("/api/tasks", status_code=201, tags=["tasks"])
def create_task(payload: TaskIn, user: db.User = auth.CurrentUser):
    if payload.priority not in PRIORITIES:
        raise HTTPException(400, f"priority must be one of {PRIORITIES}")
    with db.session() as s:
        t = db.Task(**payload.model_dump(), created_by=user.name)
        s.add(t); s.commit(); s.refresh(t)
    return t.model_dump()


@app.get("/api/tasks/{task_id}", tags=["tasks"])
def get_task(task_id: int, user: db.User = auth.CurrentUser):
    with db.session() as s:
        t = _get_task(s, task_id, allow_archived=True)
        comments = s.exec(select(db.Comment).where(db.Comment.task_id == task_id).order_by(db.Comment.created_at)).all()
        return {**t.model_dump(), "comments": [c.model_dump() for c in comments]}


@app.patch("/api/tasks/{task_id}", tags=["tasks"])
def patch_task(task_id: int, payload: TaskPatch, user: db.User = auth.CurrentUser):
    with db.session() as s:
        t = _get_task(s, task_id)
        _apply(t, payload.model_dump(exclude_none=True))
        s.add(t); s.commit(); s.refresh(t)
    return t.model_dump()


@app.delete("/api/tasks/{task_id}", tags=["tasks"])
def delete_task(task_id: int, user: db.User = auth.CurrentUser):
    """Archives. Nothing is hard-deleted through the API; POST /restore brings it back."""
    with db.session() as s:
        t = _get_task(s, task_id); t.archived_at = db.now(); t.updated_at = db.now(); s.add(t); s.commit()
    return {"archived": task_id}


@app.post("/api/tasks/{task_id}/restore", tags=["tasks"])
def restore_task(task_id: int, user: db.User = auth.CurrentUser):
    with db.session() as s:
        t = _get_task(s, task_id, allow_archived=True); t.archived_at = None; t.updated_at = db.now(); s.add(t); s.commit()
    return {"restored": task_id}


# ---------- API: comments ----------

class CommentIn(BaseModel):
    body: str


@app.get("/api/tasks/{task_id}/comments", tags=["comments"])
def list_comments(task_id: int, user: db.User = auth.CurrentUser):
    with db.session() as s:
        _get_task(s, task_id, allow_archived=True)
        return [c.model_dump() for c in s.exec(select(db.Comment).where(db.Comment.task_id == task_id).order_by(db.Comment.created_at)).all()]


@app.post("/api/tasks/{task_id}/comments", status_code=201, tags=["comments"])
def add_comment(task_id: int, payload: CommentIn, user: db.User = auth.CurrentUser):
    if not payload.body.strip():
        raise HTTPException(400, "empty comment")
    with db.session() as s:
        t = _get_task(s, task_id)
        c = db.Comment(task_id=task_id, author=user.name, body=payload.body.strip())
        s.add(c); t.updated_at = db.now(); s.add(t); s.commit(); s.refresh(c)
    return c.model_dump()


@app.delete("/api/comments/{comment_id}", tags=["comments"])
def delete_comment(comment_id: int, user: db.User = auth.CurrentUser):
    """Comments are small and yours; this one is a real delete, limited to your own."""
    with db.session() as s:
        c = s.get(db.Comment, comment_id)
        if not c:
            raise HTTPException(404, "no such comment")
        if c.author != user.name:
            raise HTTPException(403, "you can only delete your own comments")
        s.delete(c); s.commit()
    return {"deleted": comment_id}


# ---------- API: sources + ingest ----------

@app.get("/api/sources", tags=["sources"])
def list_sources(user: db.User = auth.CurrentUser):
    with db.session() as s:
        rows = s.exec(select(db.Source).order_by(db.Source.created_at.desc())).all()
    return [{"id": r.id, "kind": r.kind, "title": r.title, "occurred_at": r.occurred_at,
             "created_at": r.created_at, "chars": len(r.body)} for r in rows]


@app.get("/api/sources/{source_id}", tags=["sources"])
def get_source(source_id: int, user: db.User = auth.CurrentUser):
    with db.session() as s:
        r = s.get(db.Source, source_id)
        if not r:
            raise HTTPException(404, "no such source")
        return r.model_dump()


def read_transcript(filename: str, data: bytes) -> str:
    if filename.lower().endswith(".docx"):
        import io
        from docx import Document
        return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs if p.text.strip())
    return data.decode("utf-8", errors="replace")


def _commit_meeting(title: str, transcript: str, occurred_at: Optional[str], found: list[dict], by: str) -> dict:
    with db.session() as s:
        src = db.Source(kind="meeting", title=title, body=transcript, occurred_at=occurred_at)
        s.add(src); s.commit(); s.refresh(src)
        for t in found:
            s.add(db.Task(
                title=t["title"][:200],
                detail=(t.get("detail", "") + "\n\n> " + t.get("quote", "")).strip(),
                owner=t.get("owner", "unassigned"), priority=t.get("priority", "normal"), due=t.get("due"),
                source="meeting", source_ref=f"{title} (source {src.id})", created_by=f"extractor via {by}",
            ))
        s.commit()
        return {"committed": True, "count": len(found), "source_id": src.id}


class MeetingIn(BaseModel):
    title: str
    transcript: str
    occurred_at: Optional[str] = None
    participants: list[str] = []
    commit: bool = False


@app.post("/api/ingest/meeting", tags=["sources"])
def ingest_meeting(payload: MeetingIn, user: db.User = auth.CurrentUser):
    """Extract tasks from a transcript. Previews unless commit=true."""
    try:
        found = extract.extract(payload.transcript, payload.participants)
    except extract.NotConfigured as e:
        raise HTTPException(503, str(e))
    if not payload.commit:
        return {"committed": False, "count": len(found), "tasks": found}
    return _commit_meeting(payload.title, payload.transcript, payload.occurred_at, found, user.name)


# ---------- ingest UI ----------

@app.get("/ingest", response_class=HTMLResponse)
def ingest_page(request: Request, user: db.User = auth.PageUser):
    return page(request, "ingest.html", user, tab="ingest", preview=None, error=None)


@app.post("/ingest", response_class=HTMLResponse)
async def ingest_upload(request: Request, title: str = Form(...), participants: str = Form(""), date: str = Form(""),
                        transcript: UploadFile = File(...), user: db.User = auth.PageUser):
    raw = read_transcript(transcript.filename or "", await transcript.read())
    who = [p.strip() for p in participants.split(",") if p.strip()]
    ctx = dict(tab="ingest", preview=None, error=None)
    if not raw.strip():
        ctx["error"] = "That file is empty."
        return page(request, "ingest.html", user, **ctx)
    try:
        found = extract.extract(raw, who)
    except extract.NotConfigured as e:
        ctx["error"] = f"{e} — see Settings. The upload was read fine; nothing was written."
        return page(request, "ingest.html", user, **ctx)
    payload = {"title": title, "transcript": raw, "occurred_at": date or None, "tasks": found}
    ctx["preview"] = {"title": title, "tasks": found, "payload_json": html.escape(json.dumps(payload), quote=True)}
    return page(request, "ingest.html", user, **ctx)


@app.post("/ingest/commit")
def ingest_commit(payload: str = Form(...), user: db.User = auth.PageUser):
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(400, "bad payload")
    _commit_meeting(data["title"], data["transcript"], data.get("occurred_at"), data.get("tasks", []), user.name)
    return RedirectResponse("/", status_code=303)


# ---------- ask (UI + API) ----------

def _thread() -> list[db.ChatMessage]:
    with db.session() as s:
        return s.exec(select(db.ChatMessage).order_by(db.ChatMessage.created_at)).all()


def _ask(text: str) -> str:
    import anthropic
    with db.session() as s:
        s.add(db.ChatMessage(role="user", content=text)); s.commit()
    history = [{"role": m.role, "content": m.content} for m in _thread()]
    try:
        reply = askmod.ask(history)
    except anthropic.AuthenticationError:
        raise HTTPException(503, "The API rejected the credential — see Settings.")
    except TypeError as e:
        if not auth_status.is_no_credential(e):
            raise
        raise HTTPException(503, "No Anthropic credential on this machine — see Settings.")
    except anthropic.APIConnectionError:
        raise HTTPException(503, "Network error reaching the API.")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"API error {e.status_code}")
    with db.session() as s:
        s.add(db.ChatMessage(role="assistant", content=reply)); s.commit()
    return reply


@app.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request, error: Optional[str] = None, user: db.User = auth.PageUser):
    return page(request, "ask.html", user, tab="ask", thread=_thread(), error=error, model=askmod.MODEL)


@app.post("/ask")
def ask_send(message: str = Form(...), user: db.User = auth.PageUser):
    if not message.strip():
        return RedirectResponse("/ask", status_code=303)
    try:
        _ask(message.strip())
    except HTTPException as e:
        from urllib.parse import quote
        return RedirectResponse(f"/ask?error={quote(str(e.detail))}", status_code=303)
    return RedirectResponse("/ask", status_code=303)


@app.post("/ask/clear")
def ask_clear(user: db.User = auth.PageUser):
    with db.session() as s:
        for m in s.exec(select(db.ChatMessage)).all():
            s.delete(m)
        s.commit()
    return RedirectResponse("/ask", status_code=303)


class AskIn(BaseModel):
    message: str


@app.get("/api/ask", tags=["ask"])
def api_ask_thread(user: db.User = auth.CurrentUser):
    return [m.model_dump() for m in _thread()]


@app.post("/api/ask", tags=["ask"])
def api_ask(payload: AskIn, user: db.User = auth.CurrentUser):
    if not payload.message.strip():
        raise HTTPException(400, "empty message")
    return {"reply": _ask(payload.message.strip())}


@app.delete("/api/ask", tags=["ask"])
def api_ask_clear(user: db.User = auth.CurrentUser):
    with db.session() as s:
        n = 0
        for m in s.exec(select(db.ChatMessage)).all():
            s.delete(m); n += 1
        s.commit()
    return {"cleared": n}


# ---------- settings ----------

def _settings_ctx(result=None) -> dict:
    return {"tab": "settings", "st": auth_status.status(), "model": askmod.MODEL,
            "extract_model": os.environ.get("EXTRACT_MODEL", "claude-opus-5"), "result": result}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: db.User = auth.PageUser):
    return page(request, "settings.html", user, **_settings_ctx())


@app.post("/settings/test", response_class=HTMLResponse)
def settings_test(request: Request, user: db.User = auth.PageUser):
    ok, msg = auth_status.test_connection(askmod.MODEL)
    return page(request, "settings.html", user, **_settings_ctx({"ok": ok, "msg": msg}))


@app.post("/settings/profile")
def settings_profile(profile: str = Form(""), user: db.User = auth.PageUser):
    profile = profile.strip()
    if profile:
        os.environ["ANTHROPIC_PROFILE"] = profile
    else:
        os.environ.pop("ANTHROPIC_PROFILE", None)
    write_env("ANTHROPIC_PROFILE", profile)
    return RedirectResponse("/settings", status_code=303)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}
