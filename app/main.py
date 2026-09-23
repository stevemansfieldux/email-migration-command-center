"""EMCC — the venture's command centre. Meetings in from EMOH, suggestions out, tasks between.
Not MROSupply's command center; nothing is shared with it."""
import os
from datetime import datetime
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Optional

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
    extract.start_scheduler()
    yield


app = FastAPI(title="EMCC API", version="0.3", lifespan=lifespan,
              description="The venture command centre. Auth: session from /login, or `Authorization: Bearer <api key>`.")
app.add_middleware(SessionMiddleware, secret_key=secret_key(), same_site="lax", https_only=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def nice_date(v: str) -> str:
    """2026-09-13 → 13 Sep. Anything unparseable is shown as-is."""
    try:
        return datetime.strptime(v, "%Y-%m-%d").strftime("%-d %b")
    except (TypeError, ValueError):
        return v or ""


templates.env.filters["nice_date"] = nice_date
templates.env.globals["ref"] = db.ref
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
    return templates.TemplateResponse(name, {"request": request, "user": user, "unseen": unseen(user.id) if user else 0,
                                             "today": db.now().strftime("%Y-%m-%d"), **ctx})


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
                log_event(s, task.id, actor, "status" if k == "status" else "field", f"{k}: {old or '(none)'} to {new or '(none)'}")
            elif s is not None and task.id:
                log_event(s, task.id, actor, "field", "context edited")
            if k == "status" and s is not None and task.id:
                _sync_epic_on_status(s, task, new)
    task.updated_at = db.now()


def _task_id(task_id: str) -> int:
    """Path param: FH-0001 or 12. Anything else is a 404, not a validation error."""
    tid = db.parse_ref(task_id)
    if tid is None:
        raise HTTPException(404, "no such task")
    return tid


TaskId = Annotated[int, Depends(_task_id)]


def _get_task(s, task_id: int, allow_archived: bool = False) -> db.Task:
    t = s.get(db.Task, task_id)
    if not t or (t.archived_at and not allow_archived):
        raise HTTPException(404, "no such task")
    return t


# ---------- epics ----------
# An epic is an ordinary task that carries an epic_no (EPIC-001 ...). Members carry epic_id
# and the epic carries one mirror milestone per member (linked_task_id). Done lets a member
# out and ticks its mirror; reopening puts it back. Epics do not nest. Archiving an epic sets
# its members free. Same behaviour as the MRO board, rebuilt here; nothing is shared.

def _sync_epic_on_status(s, t: db.Task, status: str) -> None:
    if status in ("done", "dismissed"):
        if t.epic_id:
            for m in s.exec(select(db.Milestone).where(db.Milestone.task_id == t.epic_id, db.Milestone.linked_task_id == t.id)).all():
                m.done = True; s.add(m)
            epic = s.get(db.Task, t.epic_id)
            if epic:
                epic.updated_at = db.now(); s.add(epic)
            t.epic_id = None
    elif not t.epic_id:
        m = s.exec(select(db.Milestone).where(db.Milestone.linked_task_id == t.id)).first()
        if m:
            epic = s.get(db.Task, m.task_id)
            if epic and not epic.archived_at:
                m.done = False; s.add(m); t.epic_id = epic.id
                epic.updated_at = db.now(); s.add(epic)


def _is_epic(s, t: db.Task) -> bool:
    return bool(t.epic_no) or s.exec(select(db.Task).where(db.Task.epic_id == t.id)).first() is not None


def add_to_epic(s, t: db.Task, epic: db.Task, actor: str) -> str:
    """Put t inside epic. Raises 400 on the guards: itself, not an epic, nesting either way."""
    if t.id == epic.id:
        raise HTTPException(400, "a task cannot go inside itself")
    if epic.archived_at:
        raise HTTPException(400, f"{db.ref(epic.id)} is archived")
    if not epic.epic_no:
        raise HTTPException(400, f"{db.ref(epic.id)} is not an epic; drop the two cards together to create one")
    if epic.epic_id:
        raise HTTPException(400, f"{db.ref(epic.id)} is inside {db.ref(epic.epic_id)}; epics do not nest")
    if _is_epic(s, t):
        raise HTTPException(400, f"{db.ref(t.id)} is an epic itself; epics do not nest")
    label = db.epic_label(epic.epic_no)
    if t.epic_id == epic.id:
        return f"{db.ref(t.id)} is already inside {label}"
    live = t.status not in ("done", "dismissed")
    if live:
        t.epic_id = epic.id
    mirror = s.exec(select(db.Milestone).where(db.Milestone.task_id == epic.id, db.Milestone.linked_task_id == t.id)).first()
    if not mirror:
        for old in s.exec(select(db.Milestone).where(db.Milestone.linked_task_id == t.id, db.Milestone.task_id != epic.id)).all():
            s.delete(old)
        n = len(s.exec(select(db.Milestone).where(db.Milestone.task_id == epic.id)).all())
        s.add(db.Milestone(task_id=epic.id, text=f"{db.ref(t.id)} · {t.title}"[:300], sort=n, linked_task_id=t.id, done=not live))
    else:
        mirror.done = not live; s.add(mirror)
    t.updated_at = db.now(); epic.updated_at = db.now(); s.add(t); s.add(epic)
    log_event(s, t.id, actor, "field", f"epic: {label}")
    log_event(s, epic.id, actor, "milestone", f"+ {db.ref(t.id)} {t.title}"[:400])
    return f"{db.ref(t.id)} is now inside {label}"


def remove_from_epic(s, t: db.Task, actor: str) -> str:
    """Take t out of its epic by hand. Its mirror milestone goes too."""
    epic_id = t.epic_id
    if not epic_id:
        mirror = s.exec(select(db.Milestone).where(db.Milestone.linked_task_id == t.id)).first()
        if not mirror:
            raise HTTPException(400, f"{db.ref(t.id)} is not inside an epic")
        epic_id = mirror.task_id
    for m in s.exec(select(db.Milestone).where(db.Milestone.linked_task_id == t.id)).all():
        s.delete(m)
    t.epic_id = None; t.updated_at = db.now(); s.add(t)
    epic = s.get(db.Task, epic_id)
    label = db.epic_label(epic.epic_no) if epic and epic.epic_no else db.ref(epic_id)
    if epic:
        epic.updated_at = db.now(); s.add(epic)
        log_event(s, epic.id, actor, "milestone", f"- {db.ref(t.id)}")
    log_event(s, t.id, actor, "field", f"epic: {label} to (none)")
    return f"{db.ref(t.id)} taken out of {label}"


def create_epic(s, title: str, member_ids: list[int], owner: str, actor: str) -> tuple[db.Task, list[str]]:
    """A new epic task numbered after the highest epic_no ever issued, with member_ids inside."""
    title = title.strip()[:200]
    if not title:
        raise HTTPException(400, "give the epic a title")
    top = max((t.epic_no or 0 for t in s.exec(select(db.Task).where(db.Task.epic_no.is_not(None))).all()), default=0)
    epic = db.Task(title=title, owner=owner or "unassigned", source="manual", created_by=actor, epic_no=top + 1)
    s.add(epic); s.commit(); s.refresh(epic)
    log_event(s, epic.id, actor, "created", db.epic_label(epic.epic_no))
    msgs = []
    for mid in member_ids:
        m = s.get(db.Task, mid)
        if not m or m.archived_at:
            msgs.append(f"{db.ref(mid)}: no such task"); continue
        try:
            msgs.append(add_to_epic(s, m, epic, actor))
        except HTTPException as e:
            msgs.append(str(e.detail))
    s.commit(); s.refresh(epic)
    return epic, msgs


def free_members(s, epic: db.Task) -> None:
    for m in s.exec(select(db.Task).where(db.Task.epic_id == epic.id)).all():
        m.epic_id = None; m.updated_at = db.now(); s.add(m)


def epic_labels(s) -> dict[int, str]:
    return {t.id: db.epic_label(t.epic_no) for t in s.exec(select(db.Task).where(db.Task.epic_no.is_not(None))).all()}


def epic_member_counts(s) -> dict[int, int]:
    """{epic_id: live members}. A member is live while it is open, doing or blocked."""
    out: dict[int, int] = {}
    for t in s.exec(select(db.Task).where(db.Task.epic_id.is_not(None), db.Task.archived_at.is_(None), db.Task.status.in_(["open", "doing", "blocked"]))).all():
        out[t.epic_id] = out.get(t.epic_id, 0) + 1
    return out


def epics_live(s) -> list[dict]:
    """Every unarchived epic, for the drawer's picker."""
    rows = s.exec(select(db.Task).where(db.Task.epic_no.is_not(None), db.Task.archived_at.is_(None)).order_by(db.Task.epic_no)).all()
    return [{"id": t.id, "label": db.epic_label(t.epic_no), "title": t.title, "status": t.status} for t in rows]


def tags_of(s, task_id: TaskId) -> list[str]:
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


def comment_map(s, task_ids: list[int]) -> dict[int, int]:
    out = {i: 0 for i in task_ids}
    if task_ids:
        for c in s.exec(select(db.Comment).where(db.Comment.task_id.in_(task_ids))).all():
            out[c.task_id] += 1
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
def board(request: Request, tag: Optional[str] = None, task: Optional[str] = None, user: db.User = auth.PageUser):
    with db.session() as s:
        q = select(db.Task).where(db.Task.archived_at.is_(None), db.Task.status.in_(STATUSES))
        tasks = s.exec(q.order_by(db.Task.updated_at.desc())).all()
        if tag:
            ids = {r.task_id for r in s.exec(select(db.Tag).where(db.Tag.tag == tag)).all()}
            tasks = [t for t in tasks if t.id in ids]
        hidden = {r.task_id for r in s.exec(select(db.SuggestionHide).where(db.SuggestionHide.user_id == user.id)).all()}
        # Every open suggestion, minus the ones this user marked "Not mine". The board's owner
        # filter narrows them to a person; the Suggested column is where they wait.
        sugg = [t for t in s.exec(select(db.Task).where(db.Task.status == "suggested", db.Task.archived_at.is_(None)).order_by(db.Task.created_at.desc())).all()
                if t.id not in hidden]
        ids = [t.id for t in tasks] + [t.id for t in sugg]
        tags, ms, cm = tag_map(s, ids), milestone_map(s, ids), comment_map(s, ids)
        dup_titles = {t.dup_of: s.get(db.Task, t.dup_of).title for t in sugg if t.dup_of and s.get(db.Task, t.dup_of)}
        labels, counts, epics = epic_labels(s), epic_member_counts(s), epics_live(s)
    columns = {st: [t for t in tasks if t.status == st] for st in STATUSES}
    return page(request, "board.html", user, columns=columns, statuses=STATUSES, total=len(tasks), tab="tasks",
                suggestions=sugg, tags=tags, ms=ms, cm=cm, dup_titles=dup_titles, tag=tag, open_task=db.parse_ref(task), users=users_all(),
                epic_labels=labels, epic_counts=counts, epics=epics)


@app.post("/tasks/epics/new")
def task_epic_new(title: str = Form(...), ids: str = Form(""), user: db.User = auth.PageUser):
    """The board's creation point: two cards dropped together. Makes the EPIC-nnn task with both inside."""
    member_ids = [i for i in (db.parse_ref(x) for x in ids.split(",")) if i]
    with db.session() as s:
        epic, msgs = create_epic(s, title, member_ids, owner=user.name, actor=user.name)
        return {"ok": True, "id": epic.id, "ref": db.ref(epic.id), "label": db.epic_label(epic.epic_no), "messages": msgs}


@app.post("/tasks/{task_id}/epic")
def task_epic(task_id: TaskId, epic_id: str = Form(""), remove: str = Form(""), user: db.User = auth.PageUser):
    """Board and drawer: put a card inside an epic, or take it out."""
    with db.session() as s:
        t = _get_task(s, task_id)
        if remove or not epic_id:
            msg = remove_from_epic(s, t, user.name)
        else:
            eid = db.parse_ref(epic_id)
            epic = s.get(db.Task, eid) if eid else None
            if not epic:
                raise HTTPException(404, "no such epic")
            msg = add_to_epic(s, t, epic, user.name)
        s.commit(); s.refresh(t)
        return {"ok": True, "message": msg, "epic_id": t.epic_id}


@app.post("/tasks/new")
def task_new(title: str = Form(...), detail: str = Form(""), owner: str = Form("unassigned"), priority: str = Form("normal"),
             due: str = Form(""), tags: str = Form(""), user: db.User = auth.PageUser):
    """The board's + New task dialog. Same shape as POST /api/tasks, then opens the drawer on the new card."""
    if priority not in PRIORITIES or owner not in owners() + ["unassigned"]:
        raise HTTPException(400, "bad owner or priority")
    with db.session() as s:
        t = db.Task(title=title.strip()[:200], detail=detail.strip(), owner=owner, priority=priority,
                    due=due or None, source="manual", created_by=user.name)
        s.add(t); s.commit(); s.refresh(t); log_event(s, t.id, user.name, "created")
        for tg in {norm_tag(x) for x in tags.replace("#", " ").replace(",", " ").split()}:
            if tg and len(tg) >= 2:
                s.add(db.Tag(task_id=t.id, tag=tg))
                if not s.get(db.TagMeta, tg):
                    s.add(db.TagMeta(tag=tg))
        s.commit()
        return RedirectResponse(f"/?task={t.id}", status_code=303)


@app.post("/tasks/{task_id}/status")
def set_status(task_id: TaskId, status: str = Form(...), user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id); _apply(t, {"status": status}, user.name, s); s.add(t); s.commit()
    return RedirectResponse("/", status_code=303)


def _accept(s, t: db.Task, owner: Optional[str], actor: str = "") -> None:
    t.owner = owner or t.suggested_owner or "unassigned"
    t.status = "open"; t.updated_at = db.now(); s.add(t)
    log_event(s, t.id, actor, "accepted", f"owner {t.owner}")


@app.post("/suggestions/{task_id}/accept")
def suggest_accept(task_id: TaskId, owner: str = Form(""), user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id)
        if t.status != "suggested":
            raise HTTPException(400, "not a suggestion")
        _accept(s, t, owner or None, user.name); s.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/suggestions/{task_id}/dismiss")
def suggest_dismiss(task_id: TaskId, user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id); t.status = "dismissed"; t.updated_at = db.now(); s.add(t); log_event(s, t.id, user.name, "dismissed"); s.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/suggestions/{task_id}/hide")
def suggest_hide(task_id: TaskId, user: db.User = auth.PageUser):
    """'Not mine' — hidden for this user only; the other still sees it."""
    with db.session() as s:
        _get_task(s, task_id)
        if not s.get(db.SuggestionHide, (task_id, user.id)):
            s.add(db.SuggestionHide(task_id=task_id, user_id=user.id)); s.commit()
    return RedirectResponse("/", status_code=303)


# ---------- task page ----------

@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_page(request: Request, task_id: TaskId, user: db.User = auth.PageUser):
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
def task_update(task_id: TaskId, title: str = Form(...), detail: str = Form(""), owner: str = Form("unassigned"),
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
def add_comment_ui(task_id: TaskId, body: str = Form(...), user: db.User = auth.PageUser):
    if body.strip():
        with db.session() as s:
            t = _get_task(s, task_id)
            s.add(db.Comment(task_id=task_id, author=user.name, body=body.strip()))
            notify_mentions(s, user, t, body); log_event(s, t.id, user.name, "comment")
            t.updated_at = db.now(); s.add(t); s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/archive")
def archive_ui(task_id: TaskId, user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id); t.archived_at = db.now(); t.updated_at = db.now(); s.add(t); log_event(s, t.id, user.name, "archived"); free_members(s, t); s.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/tasks/{task_id}/restore")
def restore_ui(task_id: TaskId, user: db.User = auth.PageUser):
    with db.session() as s:
        t = _get_task(s, task_id, allow_archived=True); t.archived_at = None; t.updated_at = db.now(); s.add(t); s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


# ---------- tags ----------

@app.post("/tasks/{task_id}/tags")
def tag_add(task_id: TaskId, tag: str = Form(...), user: db.User = auth.PageUser):
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
def tag_remove(task_id: TaskId, tag: str = Form(...), user: db.User = auth.PageUser):
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
def milestone_add(task_id: TaskId, text: str = Form(...), user: db.User = auth.PageUser):
    if text.strip():
        with db.session() as s:
            _get_task(s, task_id)
            n = len(s.exec(select(db.Milestone).where(db.Milestone.task_id == task_id)).all())
            s.add(db.Milestone(task_id=task_id, text=text.strip(), sort=n)); s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/milestones/{mid}/toggle")
def milestone_toggle(task_id: TaskId, mid: int, user: db.User = auth.PageUser):
    with db.session() as s:
        m = s.get(db.Milestone, mid)
        if m and m.linked_task_id:
            raise HTTPException(400, "this milestone mirrors a task inside the epic; change that task's status instead")
        if m and m.task_id == task_id:
            m.done = not m.done; s.add(m)
            t = _get_task(s, task_id); t.updated_at = db.now(); s.add(t); s.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/milestones/{mid}/delete")
def milestone_delete(task_id: TaskId, mid: int, user: db.User = auth.PageUser):
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
    return page(request, "meetings.html", user, tab="meetings", meetings=meetings, error=error, status=extract.STATUS, auto_min=extract.interval_min(), source=("local " + os.environ["EMOH_PATH"]) if os.environ.get("EMOH_PATH") else kb.REPO)


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


@app.get("/plan", response_class=HTMLResponse)
def plan_page(request: Request, user: db.User = auth.PageUser):
    """The pricing model: one platform, three licences. Same page as the ROI artifact."""
    return page(request, "plan.html", user, tab="plan")


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
    # epic_id: a task id or ref puts the task inside that epic (a mirror milestone lands on the
    # epic); null takes it out. Absent means untouched.
    epic_id: Optional[int | str] = None


def _task_out(s, t: db.Task) -> dict:
    d, n = milestone_map(s, [t.id])[t.id]
    members = [m.id for m in s.exec(select(db.Task).where(db.Task.epic_id == t.id, db.Task.archived_at.is_(None))).all()] if t.epic_no else []
    parent = s.get(db.Task, t.epic_id) if t.epic_id else None
    return {**t.model_dump(), "ref": db.ref(t.id), "tags": tags_of(s, t.id), "milestones_done": d, "milestones_total": n,
            "comment_count": comment_map(s, [t.id])[t.id],
            "label": db.epic_label(t.epic_no) or db.ref(t.id), "members": members,
            "epic_label": (db.epic_label(parent.epic_no) if parent and parent.epic_no else (db.ref(parent.id) if parent else "")),
            "epic_title": parent.title if parent else ""}


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
def api_get_task(task_id: TaskId, user: db.User = auth.CurrentUser):
    with db.session() as s:
        t = _get_task(s, task_id, allow_archived=True)
        comments = s.exec(select(db.Comment).where(db.Comment.task_id == task_id).order_by(db.Comment.created_at)).all()
        miles = s.exec(select(db.Milestone).where(db.Milestone.task_id == task_id).order_by(db.Milestone.sort, db.Milestone.id)).all()
        return {**_task_out(s, t), "comments": [c.model_dump() for c in comments], "milestones": [m.model_dump() for m in miles]}


@app.get("/api/tasks/{task_id}/history", tags=["tasks"])
def api_history(task_id: TaskId, user: db.User = auth.CurrentUser):
    with db.session() as s:
        _get_task(s, task_id, allow_archived=True)
        return [e.model_dump() for e in s.exec(select(db.TaskEvent).where(db.TaskEvent.task_id == task_id).order_by(db.TaskEvent.created_at.desc())).all()]


@app.patch("/api/tasks/{task_id}", tags=["tasks"])
def api_patch_task(task_id: TaskId, payload: TaskPatch, user: db.User = auth.CurrentUser):
    with db.session() as s:
        t = _get_task(s, task_id)
        epic_msg = ""
        if "epic_id" in payload.model_fields_set:
            raw = payload.epic_id
            if raw in (None, "", 0, "0", "null"):
                epic_msg = remove_from_epic(s, t, user.name)
            else:
                eid = db.parse_ref(raw)
                epic = s.get(db.Task, eid) if eid else None
                if not epic:
                    raise HTTPException(400, "epic_id must be a task id or ref, or null to take it out")
                epic_msg = add_to_epic(s, t, epic, user.name)
        _apply(t, payload.model_dump(exclude_none=True, exclude={"epic_id"}), user.name, s); s.add(t); s.commit(); s.refresh(t)
        out = _task_out(s, t)
        if epic_msg:
            out["epic"] = epic_msg
        return out


@app.delete("/api/tasks/{task_id}", tags=["tasks"])
def api_delete_task(task_id: TaskId, user: db.User = auth.CurrentUser):
    """Archives. Nothing is hard-deleted; POST /restore brings it back. Archiving an epic frees its members."""
    with db.session() as s:
        t = _get_task(s, task_id); t.archived_at = db.now(); t.updated_at = db.now(); s.add(t); log_event(s, t.id, user.name, "archived"); free_members(s, t); s.commit()
    return {"archived": task_id}


# epics
class EpicIn(BaseModel):
    title: str
    members: list[int | str] = []
    owner: Optional[str] = None


@app.get("/api/epics", tags=["epics"])
def api_epics(user: db.User = auth.CurrentUser):
    """Every unarchived epic with its live member count."""
    with db.session() as s:
        counts = epic_member_counts(s)
        return [{**e, "members": counts.get(e["id"], 0)} for e in epics_live(s)]


@app.post("/api/epics", status_code=201, tags=["epics"])
def api_epic_create(payload: EpicIn, user: db.User = auth.CurrentUser):
    """Create an epic from existing tasks: {title, members: [ids or refs], owner?}. The epic is a
    real task numbered EPIC-nnn; each member gets a mirror milestone on it. Done on a member ticks
    it and lets the member out; reopening puts it back. Epics do not nest. To add a task to an
    existing epic, PATCH the task with epic_id."""
    member_ids = [i for i in (db.parse_ref(x) for x in payload.members) if i]
    with db.session() as s:
        epic, msgs = create_epic(s, payload.title, member_ids, owner=payload.owner or user.name, actor=user.name)
        miles = s.exec(select(db.Milestone).where(db.Milestone.task_id == epic.id).order_by(db.Milestone.sort)).all()
        return {**_task_out(s, epic), "messages": msgs, "milestones": [m.model_dump() for m in miles]}


@app.post("/api/tasks/{task_id}/restore", tags=["tasks"])
def api_restore_task(task_id: TaskId, user: db.User = auth.CurrentUser):
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
def api_accept(task_id: TaskId, payload: AcceptIn = AcceptIn(), user: db.User = auth.CurrentUser):
    with db.session() as s:
        t = _get_task(s, task_id)
        if t.status != "suggested":
            raise HTTPException(400, "not a suggestion")
        _accept(s, t, payload.owner, user.name); s.commit(); s.refresh(t)
        return _task_out(s, t)


@app.post("/api/suggestions/{task_id}/dismiss", tags=["suggestions"])
def api_dismiss(task_id: TaskId, user: db.User = auth.CurrentUser):
    with db.session() as s:
        t = _get_task(s, task_id); t.status = "dismissed"; t.updated_at = db.now(); s.add(t); log_event(s, t.id, user.name, "dismissed"); s.commit()
    return {"dismissed": task_id}


@app.post("/api/suggestions/{task_id}/hide", tags=["suggestions"])
def api_hide(task_id: TaskId, user: db.User = auth.CurrentUser):
    with db.session() as s:
        _get_task(s, task_id)
        if not s.get(db.SuggestionHide, (task_id, user.id)):
            s.add(db.SuggestionHide(task_id=task_id, user_id=user.id)); s.commit()
    return {"hidden_for": user.name}


# comments
class CommentIn(BaseModel):
    body: str


@app.get("/api/tasks/{task_id}/comments", tags=["comments"])
def api_list_comments(task_id: TaskId, user: db.User = auth.CurrentUser):
    with db.session() as s:
        _get_task(s, task_id, allow_archived=True)
        return [c.model_dump() for c in s.exec(select(db.Comment).where(db.Comment.task_id == task_id).order_by(db.Comment.created_at)).all()]


@app.post("/api/tasks/{task_id}/comments", status_code=201, tags=["comments"])
def api_add_comment(task_id: TaskId, payload: CommentIn, user: db.User = auth.CurrentUser):
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
def api_tag_add(task_id: TaskId, payload: TagIn, user: db.User = auth.CurrentUser):
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
def api_tag_remove(task_id: TaskId, tag: str, user: db.User = auth.CurrentUser):
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
def api_milestone_add(task_id: TaskId, payload: MilestoneIn, user: db.User = auth.CurrentUser):
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
        if m.linked_task_id:
            raise HTTPException(400, "this milestone mirrors a task inside the epic; change that task's status instead")
        m.done = not m.done; s.add(m); log_event(s, m.task_id, user.name, "milestone", f"{'✓' if m.done else '○'} {m.text}"); s.commit(); s.refresh(m)
        return m.model_dump()


@app.delete("/api/milestones/{mid}", tags=["milestones"])
def api_milestone_delete(mid: int, user: db.User = auth.CurrentUser):
    with db.session() as s:
        m = s.get(db.Milestone, mid)
        if not m:
            raise HTTPException(404, "no such milestone")
        if m.linked_task_id:
            member = s.get(db.Task, m.linked_task_id)
            if member:
                remove_from_epic(s, member, user.name); s.commit()
                return {"deleted": mid, "removed_from_epic": member.id}
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
