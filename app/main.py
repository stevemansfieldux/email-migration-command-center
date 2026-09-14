"""EMCC — the venture's command centre. Meetings in from EMOH, suggestions out, tasks between.
Not MROSupply's command center; nothing is shared with it."""
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlmodel import select
from starlette.middleware.sessions import SessionMiddleware

from . import ask as askmod, auth, auth_status, db, extract, kb

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
STATUSES = ["open", "doing", "blocked", "done"]           # board columns
ALL_STATUSES = ["suggested", *STATUSES, "dismissed"]
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
    k = os.environ.get("SECRET_KEY", "").strip()
    if not k:
        k = secrets.token_urlsafe(48); os.environ["SECRET_KEY"] = k; write_env("SECRET_KEY", k)
    return k


load_env()


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title="EMCC API", version="0.3", lifespan=lifespan,
              description="The venture command centre. Auth: session from /login, or `Authorization: Bearer <api key>`.")
app.add_middleware(SessionMiddleware, secret_key=secret_key(), same_site="lax", https_only=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.exception_handler(auth.LoginRequired)
async def _login_redirect(request: Request, _: auth.LoginRequired):
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


# ---------- helpers ----------

def unseen(user_id: int) -> int:
    with db.session() as s:
        n = len(s.exec(select(db.Notification).where(db.Notification.recipient_id == user_id, db.Notification.seen.is_(False))).all())
        m = len(s.exec(select(db.Message).where(db.Message.to_id == user_id, db.Message.seen.is_(False))).all())
    return n + m


def page(request: Request, name: str, user: Optional[db.User], **ctx):
    return templates.TemplateResponse(name, {"request": request, "user": user, "unseen": unseen(user.id) if user else 0, **ctx})


def users_all() -> list[db.User]:
    with db.session() as s:
        return s.exec(select(db.User).order_by(db.User.id)).all()


def owners() -> list[str]:
    return [u.name for u in users_all()]


def split_detail(detail: str) -> tuple[str, list[str]]:
    body, quotes = [], []
    for line in (detail or "").splitlines():
        (quotes if line.startswith("> ") else body).append(line[2:] if line.startswith("> ") else line)
    return "\n".join(body).strip(), quotes


PATCHABLE = {"title", "detail", "owner", "priority", "due", "status", "tags"}


def log_event(s, task_id: int, actor: str, kind: str, detail: str = "") -> None:
    s.add(db.TaskEvent(task_id=task_id, actor=actor, kind=kind, detail=detail[:400]))


def _apply(task: db.Task, fields: dict, actor: str = "", s=None) -> None:
    for k, v in fields.items():
        if k not in PATCHABLE:
            continue
        if k == "detail":
            # The drawer edits the context body; the extractor's quote lines stay attached.
            _, quotes = split_detail(task.detail)
            if quotes and not any(l.startswith("> ") for l in str(v).splitlines()):
                v = str(v).strip() + "\n\n" + "\n".join("> " + q for q in quotes)
        if k == "status" and v not in ALL_STATUSES:
            raise HTTPException(400, f"status must be one of {ALL_STATUSES}")
        if k == "priority" and v not in PRIORITIES:
            raise HTTPException(400, f"priority must be one of {PRIORITIES}")
        if k == "title" and not str(v).strip():
            raise HTTPException(400, "title cannot be empty")
        old = getattr(task, k)
        new = v if v != "" else (None if k == "due" else "")
        if old != new:
            setattr(task, k, new)
            if s is not None and task.id and k != "detail":
                log_event(s, task.id, actor, "status" if k == "status" else "field", f"{k}: {old or '—'} → {new or '—'}")
            elif s is not None and task.id:
                log_event(s, task.id, actor, "field", "context edited")
    task.updated_at = db.now()


def _get_task(s, task_id: int, allow_archived: bool = False) -> db.Task:
    t = s.get(db.Task, task_id)
    if not t or (t.archived_at and not allow_archived):
        raise HTTPException(404, "no such task")
    return t


def tags_of(s, task_id: int) -> list[str]:
    return sorted(r.tag for r in s.exec(select(db.Tag).where(db.Tag.task_id == task_id)).all())


def tag_map(s, task_ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {i: [] for i in task_ids}
    if task_ids:
        for r in s.exec(select(db.Tag).where(db.Tag.task_id.in_(task_ids))).all():
            out[r.task_id].append(r.tag)
    return {k: sorted(v) for k, v in out.items()}


def milestone_map(s, task_ids: list[int]) -> dict[int, tuple[int, int]]:
    out: dict[int, tuple[int, int]] = {i: (0, 0) for i in task_ids}
    if task_ids:
        for m in s.exec(select(db.Milestone).where(db.Milestone.task_id.in_(task_ids))).all():
            d, n = out[m.task_id]; out[m.task_id] = (d + (1 if m.done else 0), n + 1)
    return out


def norm_tag(t: str) -> str:
    t = re.sub(r"[^a-z0-9-]+", "-", t.strip().lower()).strip("-")
    return t[:40]


MENTION = re.compile(r"@(\w+)")


def notify_mentions(s, actor: db.User, task: db.Task, body: str) -> None:
    names = {u.name.lower(): u for u in users_all()}
    for m in set(MENTION.findall(body)):
        u = names.get(m.lower())
        if u and u.id != actor.id:
            s.add(db.Notification(recipient_id=u.id, actor=actor.name, kind="mention", task_id=task.id,
                                  text=f"{actor.name} mentioned you on #{task.id} {task.title}"))


# ---------- login / account ----------

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


@app.get("/account", response_class=HTMLResponse)
def account(request: Request, msg: Optional[str] = None, user: db.User = auth.PageUser):
    with db.session() as s:
        mine = s.exec(select(db.Task).where(db.Task.owner == user.name, db.Task.archived_at.is_(None), db.Task.status.in_(STATUSES)).order_by(db.Task.updated_at.desc())).all()
        fresh = s.get(db.User, user.id)
    return page(request, "account.html", user, tab=None, mine=mine, me=fresh, msg=msg, new_key=request.session.pop("new_key", None))


@app.post("/account/name")
def account_name(name: str = Form(...), user: db.User = auth.PageUser):
    with db.session() as s:
        u = s.get(db.User, user.id); u.name = name.strip() or u.name; s.add(u); s.commit()
    return RedirectResponse("/account?msg=Name+updated", status_code=303)


@app.post("/account/password")
def account_password(current: str = Form(...), new: str = Form(...), again: str = Form(...), user: db.User = auth.PageUser):
    with db.session() as s:
        u = s.get(db.User, user.id)
        if not auth.check_password(current, u.password_hash):
            return RedirectResponse("/account?msg=Current+password+is+wrong", status_code=303)
        if not new or new != again:
            return RedirectResponse("/account?msg=New+passwords+don%27t+match", status_code=303)
        u.password_hash = auth.hash_password(new); s.add(u); s.commit()
    return RedirectResponse("/account?msg=Password+changed", status_code=303)


@app.post("/account/key")
def account_key(request: Request, user: db.User = auth.PageUser):
    """Rotate: the old key stops working now; the new one is shown once on the next page."""
    key, h = auth.new_api_key()
    with db.session() as s:
        u = s.get(db.User, user.id); u.api_key_hash = h; s.add(u); s.commit()
    request.session["new_key"] = key
    return RedirectResponse("/account", status_code=303)


# ---------- board + suggestions ----------

@app.get("/", response_class=HTMLResponse)
def board(request: Request, tag: Optional[str] = None, task: Optional[int] = None, user: db.User = auth.PageUser):
    with db.session() as s:
        q = select(db.Task).where(db.Task.archived_at.is_(None), db.Task.status.in_(STATUSES))
        tasks = s.exec(q.order_by(db.Task.updated_at.desc())).all()
        if tag:
            ids = {r.task_id for r in s.exec(select(db.Tag).where(db.Tag.tag == tag)).all()}
            tasks = [t for t in tasks if t.id in ids]
        hidden = {r.task_id for r in s.exec(select(db.SuggestionHide).where(db.SuggestionHide.user_id == user.id)).all()}
        sugg = [t for t in s.exec(select(db.Task).where(db.Task.status == "suggested", db.Task.archived_at.is_(None)).order_by(db.Task.created_at.desc())).all()
                if t.id not in hidden and t.suggested_owner in (user.name, "unassigned", "")]
        ids = [t.id for t in tasks] + [t.id for t in sugg]
        tags, ms = tag_map(s, ids), milestone_map(s, ids)
        dup_titles = {t.dup_of: s.get(db.Task, t.dup_of).title for t in sugg if t.dup_of and s.get(db.Task, t.dup_of)}
    columns = {st: [t for t in tasks if t.status == st] for st in STATUSES}
    return page(request, "board.html", user, columns=columns, statuses=STATUSES, total=len(tasks), tab="tasks",
                suggestions=sugg, tags=tags, ms=ms, dup_titles=dup_titles, tag=tag, open_task=task, users=users_all())


@app.post("/tasks/{task_id}/status")
def set_status(task_id: int, status: str = Form(...), user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id); _apply(t, {"status": status}, user.name, s); s.add(t); s.commit()
    return RedirectResponse("/", status_code=303)


def _accept(s, t: db.Task, owner: Optional[str], actor: str = "") -> None:
    t.owner = owner or t.suggested_owner or "unassigned"
    t.status = "open"; t.updated_at = db.now(); s.add(t)
    log_event(s, t.id, actor, "accepted", f"owner {t.owner}")


@app.post("/suggestions/{task_id}/accept")
def suggest_accept(task_id: int, owner: str = Form(""), user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id)
        if t.status != "suggested":
            raise HTTPException(400, "not a suggestion")
        _accept(s, t, owner or None, user.name); s.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/suggestions/{task_id}/dismiss")
def suggest_dismiss(task_id: int, user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id); t.status = "dismissed"; t.updated_at = db.now(); s.add(t); log_event(s, t.id, user.name, "dismissed"); s.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/suggestions/{task_id}/hide")
def suggest_hide(task_id: int, user: db.User = auth.PageUser):
    """'Not mine' — hidden for this user only; the other still sees it."""
    with db.session() as s:
        _get_task(s, task_id)
        if not s.get(db.SuggestionHide, (task_id, user.id)):
            s.add(db.SuggestionHide(task_id=task_id, user_id=user.id)); s.commit()
    return RedirectResponse("/", status_code=303)


# ---------- task page ----------

@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_page(request: Request, task_id: int, user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id, allow_archived=True)
        comments = s.exec(select(db.Comment).where(db.Comment.task_id == task_id).order_by(db.Comment.created_at)).all()
        tags = tags_of(s, task_id)
        miles = s.exec(select(db.Milestone).where(db.Milestone.task_id == task_id).order_by(db.Milestone.sort, db.Milestone.id)).all()
        dup = s.get(db.Task, t.dup_of) if t.dup_of else None
        all_tags = sorted({r.tag for r in s.exec(select(db.Tag)).all()})
    body, quotes = split_detail(t.detail)
    return page(request, "task.html", user, t=t, body=body, quotes=quotes, comments=comments, tags=tags, all_tags=all_tags,
                miles=miles, dup=dup, statuses=ALL_STATUSES, priorities=PRIORITIES, owners=owners(), tab="tasks")


@app.post("/tasks/{task_id}")
def task_update(task_id: int, title: str = Form(...), detail: str = Form(""), owner: str = Form("unassigned"),
                priority: str = Form("normal"), due: str = Form(""), status: str = Form("open"), user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id)
        _, quotes = split_detail(t.detail)
        merged = detail.strip() + ("\n\n" + "\n".join("> " + q for q in quotes) if quotes else "")
        prev_owner = t.owner
        _apply(t, {"title": title, "detail": merged, "owner": owner, "priority": priority, "due": due, "status": status}, user.name, s)
        if owner != prev_owner and owner not in ("", "unassigned", user.name):
            if (u := next((x for x in users_all() if x.name == owner), None)):
                s.add(db.Notification(recipient_id=u.id, actor=user.name, kind="assigned", task_id=t.id, text=f"{user.name} assigned you #{t.id} {t.title}"))
        s.add(t); s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/comments")
def add_comment_ui(task_id: int, body: str = Form(...), user: db.User = auth.PageUser):
    if body.strip():
        with db.session() as s:
            t = _get_task(s, task_id)
            s.add(db.Comment(task_id=task_id, author=user.name, body=body.strip()))
            notify_mentions(s, user, t, body); log_event(s, t.id, user.name, "comment")
            t.updated_at = db.now(); s.add(t); s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/archive")
def archive_ui(task_id: int, user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id); t.archived_at = db.now(); t.updated_at = db.now(); s.add(t); log_event(s, t.id, user.name, "archived"); s.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/tasks/{task_id}/restore")
def restore_ui(task_id: int, user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id, allow_archived=True); t.archived_at = None; t.updated_at = db.now(); s.add(t); s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


# ---------- tags ----------

@app.post("/tasks/{task_id}/tags")
def tag_add(task_id: int, tag: str = Form(...), user: db.User = auth.PageUser):
    tag = norm_tag(tag)
    if len(tag) >= 2:
        with db.session() as s:
            _get_task(s, task_id)
            if not s.get(db.Tag, (task_id, tag)):
                s.add(db.Tag(task_id=task_id, tag=tag))
            if not s.get(db.TagMeta, tag):
                s.add(db.TagMeta(tag=tag))
            s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/tags/remove")
def tag_remove(task_id: int, tag: str = Form(...), user: db.User = auth.PageUser):
    with db.session() as s:
        if (r := s.get(db.Tag, (task_id, tag))):
            s.delete(r); s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.get("/tags", response_class=HTMLResponse)
def tags_index(request: Request, user: db.User = auth.PageUser):
    with db.session() as s:
        rows = s.exec(select(db.Tag)).all()
        counts: dict[str, int] = {}
        for r in rows:
            counts[r.tag] = counts.get(r.tag, 0) + 1
        meta = {m.tag: m for m in s.exec(select(db.TagMeta)).all()}
    items = [{"tag": t, "count": counts.get(t, 0), "description": (meta[t].description if t in meta else "")} for t in sorted(set(counts) | set(meta))]
    return page(request, "tags.html", user, tab="tags", items=items)


@app.post("/tags/{tag}/description")
def tag_description(tag: str, description: str = Form(""), user: db.User = auth.PageUser):
    with db.session() as s:
        m = s.get(db.TagMeta, tag) or db.TagMeta(tag=tag)
        m.description = description.strip(); m.updated_at = db.now(); s.add(m); s.commit()
    return RedirectResponse("/tags", status_code=303)


# ---------- milestones ----------

@app.post("/tasks/{task_id}/milestones")
def milestone_add(task_id: int, text: str = Form(...), user: db.User = auth.PageUser):
    if text.strip():
        with db.session() as s:
            _get_task(s, task_id)
            n = len(s.exec(select(db.Milestone).where(db.Milestone.task_id == task_id)).all())
            s.add(db.Milestone(task_id=task_id, text=text.strip(), sort=n)); s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/milestones/{mid}/toggle")
def milestone_toggle(task_id: int, mid: int, user: db.User = auth.PageUser):
    with db.session() as s:
        m = s.get(db.Milestone, mid)
        if m and m.task_id == task_id:
            m.done = not m.done; s.add(m)
            t = _get_task(s, task_id); t.updated_at = db.now(); s.add(t); s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/milestones/{mid}/delete")
def milestone_delete(task_id: int, mid: int, user: db.User = auth.PageUser):
    with db.session() as s:
        m = s.get(db.Milestone, mid)
        if m and m.task_id == task_id:
            s.delete(m); s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


# ---------- meetings (EMOH) ----------

@app.get("/meetings", response_class=HTMLResponse)
def meetings_page(request: Request, user: db.User = auth.PageUser):
    error = None
    try:
        meetings = kb.list_meetings()
    except kb.NotConfigured as e:
        meetings, error = [], str(e)
    except Exception as e:  # noqa: BLE001
        meetings, error = [], f"Could not read EMOH: {e}"
    with db.session() as s:
        ledger = {r.path: r for r in s.exec(select(db.ExtractedMeeting)).all()}
    for m in meetings:
        m["extracted"] = ledger.get(m["path"])
    return page(request, "meetings.html", user, tab="meetings", meetings=meetings, error=error, status=extract.STATUS, source=("local " + os.environ["EMOH_PATH"]) if os.environ.get("EMOH_PATH") else kb.REPO)


@app.get("/meetings/view/{path:path}", response_class=HTMLResponse)
def meeting_view(request: Request, path: str, user: db.User = auth.PageUser):
    if not path.startswith("meetings/") or ".." in path:
        raise HTTPException(404)
    try:
        text = kb.read(path)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(404, str(e))
    return page(request, "meeting.html", user, tab="meetings", path=path, text=text)


@app.post("/meetings/extract")
def meetings_extract(user: db.User = auth.PageUser):
    extract.run_in_background(by=user.name)
    return RedirectResponse("/meetings", status_code=303)


@app.post("/meetings/refresh")
def meetings_refresh(user: db.User = auth.PageUser):
    kb.clear_cache()
    return RedirectResponse("/meetings", status_code=303)


# ---------- inbox + messages ----------

@app.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, user: db.User = auth.PageUser):
    with db.session() as s:
        notes = s.exec(select(db.Notification).where(db.Notification.recipient_id == user.id).order_by(db.Notification.created_at.desc())).all()[:100]
        for n in notes:
            if not n.seen:
                n.seen = True; s.add(n)
        s.commit()
    return page(request, "inbox.html", user, tab="inbox", notes=notes)


@app.get("/messages", response_class=HTMLResponse)
def messages(request: Request, user: db.User = auth.PageUser):
    other = next((u for u in users_all() if u.id != user.id), None)
    with db.session() as s:
        thread = s.exec(select(db.Message).order_by(db.Message.created_at)).all()[-200:]
        for m in thread:
            if m.to_id == user.id and not m.seen:
                m.seen = True; s.add(m)
        s.commit()
    return page(request, "messages.html", user, tab="inbox", thread=thread, other=other)


@app.post("/messages")
def message_send(body: str = Form(...), user: db.User = auth.PageUser):
    other = next((u for u in users_all() if u.id != user.id), None)
    if other and body.strip():
        with db.session() as s:
            s.add(db.Message(from_id=user.id, to_id=other.id, body=body.strip())); s.commit()
    return RedirectResponse("/messages", status_code=303)


# ---------- ask ----------

def _thread():
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
    if message.strip():
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


# ---------- settings ----------

def _settings_ctx(result=None) -> dict:
    return {"tab": "settings", "st": auth_status.status(), "model": askmod.MODEL, "extract_model": extract.MODEL, "result": result,
            "gh_token": bool(os.environ.get("GITHUB_TOKEN")), "emoh_path": os.environ.get("EMOH_PATH") or None, "emoh_repo": kb.REPO}


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


# ======================================================================
# API
# ======================================================================

@app.get("/api/me", tags=["me"])
def api_me(user: db.User = auth.CurrentUser):
    return {"id": user.id, "email": user.email, "name": user.name, "unseen": unseen(user.id)}


@app.get("/api/users", tags=["me"])
def api_users(user: db.User = auth.CurrentUser):
    return [{"id": u.id, "email": u.email, "name": u.name} for u in users_all()]


class TaskIn(BaseModel):
    title: str; detail: str = ""; owner: str = "unassigned"; priority: str = "normal"
    due: Optional[str] = None; tags: list[str] = []; source: str = "manual"; source_ref: str = ""


class TaskPatch(BaseModel):
    title: Optional[str] = None; detail: Optional[str] = None; owner: Optional[str] = None; priority: Optional[str] = None
    due: Optional[str] = None; status: Optional[str] = None


def _task_out(s, t: db.Task) -> dict:
    d, n = milestone_map(s, [t.id])[t.id]
    return {**t.model_dump(), "tags": tags_of(s, t.id), "milestones_done": d, "milestones_total": n}


@app.get("/api/tasks", tags=["tasks"])
def api_list_tasks(status: Optional[str] = None, owner: Optional[str] = None, tag: Optional[str] = None,
                   include_archived: bool = False, user: db.User = auth.CurrentUser):
    with db.session() as s:
        q = select(db.Task)
        if not include_archived:
            q = q.where(db.Task.archived_at.is_(None))
        q = q.where(db.Task.status == status) if status else q.where(db.Task.status.in_(STATUSES))
        if owner:
            q = q.where(db.Task.owner == owner)
        rows = s.exec(q.order_by(db.Task.updated_at.desc())).all()
        if tag:
            ids = {r.task_id for r in s.exec(select(db.Tag).where(db.Tag.tag == tag)).all()}
            rows = [t for t in rows if t.id in ids]
        return {"count": len(rows), "tasks": [_task_out(s, t) for t in rows]}


@app.post("/api/tasks", status_code=201, tags=["tasks"])
def api_create_task(payload: TaskIn, user: db.User = auth.CurrentUser):
    if payload.priority not in PRIORITIES:
        raise HTTPException(400, f"priority must be one of {PRIORITIES}")
    with db.session() as s:
        t = db.Task(**payload.model_dump(exclude={"tags"}), created_by=user.name)
        s.add(t); s.commit(); s.refresh(t); log_event(s, t.id, user.name, "created")
        for tg in payload.tags:
            if (tg := norm_tag(tg)) and len(tg) >= 2:
                s.add(db.Tag(task_id=t.id, tag=tg))
                if not s.get(db.TagMeta, tg):
                    s.add(db.TagMeta(tag=tg))
        s.commit()
        return _task_out(s, t)


@app.get("/api/tasks/{task_id}", tags=["tasks"])
def api_get_task(task_id: int, user: db.User = auth.CurrentUser):
    with db.session() as s:
        t = _get_task(s, task_id, allow_archived=True)
        comments = s.exec(select(db.Comment).where(db.Comment.task_id == task_id).order_by(db.Comment.created_at)).all()
        miles = s.exec(select(db.Milestone).where(db.Milestone.task_id == task_id).order_by(db.Milestone.sort, db.Milestone.id)).all()
        return {**_task_out(s, t), "comments": [c.model_dump() for c in comments], "milestones": [m.model_dump() for m in miles]}


@app.get("/api/tasks/{task_id}/history", tags=["tasks"])
def api_history(task_id: int, user: db.User = auth.CurrentUser):
    with db.session() as s:
        _get_task(s, task_id, allow_archived=True)
        return [e.model_dump() for e in s.exec(select(db.TaskEvent).where(db.TaskEvent.task_id == task_id).order_by(db.TaskEvent.created_at.desc())).all()]


@app.patch("/api/tasks/{task_id}", tags=["tasks"])
def api_patch_task(task_id: int, payload: TaskPatch, user: db.User = auth.CurrentUser):
    with db.session() as s:
        t = _get_task(s, task_id); _apply(t, payload.model_dump(exclude_none=True), user.name, s); s.add(t); s.commit(); s.refresh(t)
        return _task_out(s, t)


@app.delete("/api/tasks/{task_id}", tags=["tasks"])
def api_delete_task(task_id: int, user: db.User = auth.CurrentUser):
    """Archives. Nothing is hard-deleted; POST /restore brings it back."""
    with db.session() as s:
        t = _get_task(s, task_id); t.archived_at = db.now(); t.updated_at = db.now(); s.add(t); log_event(s, t.id, user.name, "archived"); s.commit()
    return {"archived": task_id}


@app.post("/api/tasks/{task_id}/restore", tags=["tasks"])
def api_restore_task(task_id: int, user: db.User = auth.CurrentUser):
    with db.session() as s:
        t = _get_task(s, task_id, allow_archived=True); t.archived_at = None; t.updated_at = db.now(); s.add(t); s.commit()
    return {"restored": task_id}


# suggestions
@app.get("/api/suggestions", tags=["suggestions"])
def api_suggestions(user: db.User = auth.CurrentUser):
    with db.session() as s:
        hidden = {r.task_id for r in s.exec(select(db.SuggestionHide).where(db.SuggestionHide.user_id == user.id)).all()}
        rows = [t for t in s.exec(select(db.Task).where(db.Task.status == "suggested", db.Task.archived_at.is_(None)).order_by(db.Task.created_at.desc())).all() if t.id not in hidden]
        return [_task_out(s, t) for t in rows]


class AcceptIn(BaseModel):
    owner: Optional[str] = None


@app.post("/api/suggestions/{task_id}/accept", tags=["suggestions"])
def api_accept(task_id: int, payload: AcceptIn = AcceptIn(), user: db.User = auth.CurrentUser):
    with db.session() as s:
        t = _get_task(s, task_id)
        if t.status != "suggested":
            raise HTTPException(400, "not a suggestion")
        _accept(s, t, payload.owner, user.name); s.commit(); s.refresh(t)
        return _task_out(s, t)


@app.post("/api/suggestions/{task_id}/dismiss", tags=["suggestions"])
def api_dismiss(task_id: int, user: db.User = auth.CurrentUser):
    with db.session() as s:
        t = _get_task(s, task_id); t.status = "dismissed"; t.updated_at = db.now(); s.add(t); log_event(s, t.id, user.name, "dismissed"); s.commit()
    return {"dismissed": task_id}


@app.post("/api/suggestions/{task_id}/hide", tags=["suggestions"])
def api_hide(task_id: int, user: db.User = auth.CurrentUser):
    with db.session() as s:
        _get_task(s, task_id)
        if not s.get(db.SuggestionHide, (task_id, user.id)):
            s.add(db.SuggestionHide(task_id=task_id, user_id=user.id)); s.commit()
    return {"hidden_for": user.name}


# comments
class CommentIn(BaseModel):
    body: str


@app.get("/api/tasks/{task_id}/comments", tags=["comments"])
def api_list_comments(task_id: int, user: db.User = auth.CurrentUser):
    with db.session() as s:
        _get_task(s, task_id, allow_archived=True)
        return [c.model_dump() for c in s.exec(select(db.Comment).where(db.Comment.task_id == task_id).order_by(db.Comment.created_at)).all()]


@app.post("/api/tasks/{task_id}/comments", status_code=201, tags=["comments"])
def api_add_comment(task_id: int, payload: CommentIn, user: db.User = auth.CurrentUser):
    if not payload.body.strip():
        raise HTTPException(400, "empty comment")
    with db.session() as s:
        t = _get_task(s, task_id)
        c = db.Comment(task_id=task_id, author=user.name, body=payload.body.strip()); s.add(c)
        notify_mentions(s, user, t, payload.body); log_event(s, t.id, user.name, "comment")
        t.updated_at = db.now(); s.add(t); s.commit(); s.refresh(c)
        return c.model_dump()


@app.delete("/api/comments/{comment_id}", tags=["comments"])
def api_delete_comment(comment_id: int, user: db.User = auth.CurrentUser):
    with db.session() as s:
        c = s.get(db.Comment, comment_id)
        if not c:
            raise HTTPException(404, "no such comment")
        if c.author != user.name:
            raise HTTPException(403, "you can only delete your own comments")
        s.delete(c); s.commit()
    return {"deleted": comment_id}


# tags
class TagIn(BaseModel):
    tag: str


@app.get("/api/tags", tags=["tags"])
def api_tags(user: db.User = auth.CurrentUser):
    with db.session() as s:
        counts: dict[str, int] = {}
        for r in s.exec(select(db.Tag)).all():
            counts[r.tag] = counts.get(r.tag, 0) + 1
        meta = {m.tag: m.description for m in s.exec(select(db.TagMeta)).all()}
    return [{"tag": t, "count": counts.get(t, 0), "description": meta.get(t, "")} for t in sorted(set(counts) | set(meta))]


@app.post("/api/tasks/{task_id}/tags", status_code=201, tags=["tags"])
def api_tag_add(task_id: int, payload: TagIn, user: db.User = auth.CurrentUser):
    tag = norm_tag(payload.tag)
    if len(tag) < 2:
        raise HTTPException(400, "tag too short")
    with db.session() as s:
        _get_task(s, task_id)
        if not s.get(db.Tag, (task_id, tag)):
            s.add(db.Tag(task_id=task_id, tag=tag)); log_event(s, task_id, user.name, "tag", f"+{tag}")
        if not s.get(db.TagMeta, tag):
            s.add(db.TagMeta(tag=tag))
        s.commit()
        return {"task_id": task_id, "tags": tags_of(s, task_id)}


@app.delete("/api/tasks/{task_id}/tags/{tag}", tags=["tags"])
def api_tag_remove(task_id: int, tag: str, user: db.User = auth.CurrentUser):
    with db.session() as s:
        if (r := s.get(db.Tag, (task_id, tag))):
            s.delete(r); log_event(s, task_id, user.name, "tag", f"-{tag}"); s.commit()
        return {"task_id": task_id, "tags": tags_of(s, task_id)}


class TagMetaIn(BaseModel):
    description: str


@app.patch("/api/tags/{tag}", tags=["tags"])
def api_tag_meta(tag: str, payload: TagMetaIn, user: db.User = auth.CurrentUser):
    with db.session() as s:
        m = s.get(db.TagMeta, tag) or db.TagMeta(tag=tag)
        m.description = payload.description.strip(); m.updated_at = db.now(); s.add(m); s.commit()
        return {"tag": tag, "description": m.description}


# milestones
class MilestoneIn(BaseModel):
    text: str


@app.post("/api/tasks/{task_id}/milestones", status_code=201, tags=["milestones"])
def api_milestone_add(task_id: int, payload: MilestoneIn, user: db.User = auth.CurrentUser):
    if not payload.text.strip():
        raise HTTPException(400, "empty milestone")
    with db.session() as s:
        _get_task(s, task_id)
        n = len(s.exec(select(db.Milestone).where(db.Milestone.task_id == task_id)).all())
        m = db.Milestone(task_id=task_id, text=payload.text.strip(), sort=n); s.add(m); log_event(s, task_id, user.name, "milestone", f"+ {m.text}"); s.commit(); s.refresh(m)
        return m.model_dump()


@app.post("/api/milestones/{mid}/toggle", tags=["milestones"])
def api_milestone_toggle(mid: int, user: db.User = auth.CurrentUser):
    with db.session() as s:
        m = s.get(db.Milestone, mid)
        if not m:
            raise HTTPException(404, "no such milestone")
        m.done = not m.done; s.add(m); log_event(s, m.task_id, user.name, "milestone", f"{'✓' if m.done else '○'} {m.text}"); s.commit(); s.refresh(m)
        return m.model_dump()


@app.delete("/api/milestones/{mid}", tags=["milestones"])
def api_milestone_delete(mid: int, user: db.User = auth.CurrentUser):
    with db.session() as s:
        m = s.get(db.Milestone, mid)
        if not m:
            raise HTTPException(404, "no such milestone")
        s.delete(m); s.commit()
    return {"deleted": mid}


# meetings / extract
@app.get("/api/meetings", tags=["meetings"])
def api_meetings(user: db.User = auth.CurrentUser):
    try:
        meetings = kb.list_meetings()
    except kb.NotConfigured as e:
        raise HTTPException(503, str(e))
    with db.session() as s:
        ledger = {r.path: r.model_dump() for r in s.exec(select(db.ExtractedMeeting)).all()}
    return [{**m, "extracted": ledger.get(m["path"])} for m in meetings]


@app.get("/api/meetings/{path:path}", tags=["meetings"])
def api_meeting(path: str, user: db.User = auth.CurrentUser):
    if not path.startswith("meetings/") or ".." in path:
        raise HTTPException(404)
    return {"path": path, "text": kb.read(path)}


@app.post("/api/extract/run", tags=["meetings"])
def api_extract_run(user: db.User = auth.CurrentUser):
    """Start the extractor in the background. Poll /api/extract/status."""
    started = extract.run_in_background(by=user.name)
    return {"started": started, "status": extract.STATUS}


@app.get("/api/extract/status", tags=["meetings"])
def api_extract_status(user: db.User = auth.CurrentUser):
    return extract.STATUS


# inbox + messages
@app.get("/api/inbox", tags=["inbox"])
def api_inbox(unseen_only: bool = False, user: db.User = auth.CurrentUser):
    with db.session() as s:
        q = select(db.Notification).where(db.Notification.recipient_id == user.id)
        if unseen_only:
            q = q.where(db.Notification.seen.is_(False))
        return [n.model_dump() for n in s.exec(q.order_by(db.Notification.created_at.desc())).all()[:100]]


@app.post("/api/inbox/seen", tags=["inbox"])
def api_inbox_seen(user: db.User = auth.CurrentUser):
    with db.session() as s:
        n = 0
        for x in s.exec(select(db.Notification).where(db.Notification.recipient_id == user.id, db.Notification.seen.is_(False))).all():
            x.seen = True; s.add(x); n += 1
        s.commit()
    return {"marked": n}


class MessageIn(BaseModel):
    body: str


@app.get("/api/messages", tags=["inbox"])
def api_messages(user: db.User = auth.CurrentUser):
    with db.session() as s:
        return [m.model_dump() for m in s.exec(select(db.Message).order_by(db.Message.created_at)).all()[-200:]]


@app.post("/api/messages", status_code=201, tags=["inbox"])
def api_message_send(payload: MessageIn, user: db.User = auth.CurrentUser):
    other = next((u for u in users_all() if u.id != user.id), None)
    if not other:
        raise HTTPException(400, "no one to message")
    if not payload.body.strip():
        raise HTTPException(400, "empty message")
    with db.session() as s:
        m = db.Message(from_id=user.id, to_id=other.id, body=payload.body.strip()); s.add(m); s.commit(); s.refresh(m)
        return m.model_dump()


# ask
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


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}
