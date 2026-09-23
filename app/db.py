"""Storage. Postgres on Railway via DATABASE_URL, SQLite locally when it is unset."""
import os
import re
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel, Session, create_engine, select  # noqa: F401


def _url() -> str:
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        return "sqlite:///./command-center.db"
    # Railway and Heroku still hand out the legacy postgres:// scheme.
    if raw.startswith("postgres://"):
        raw = raw.replace("postgres://", "postgresql+psycopg://", 1)
    elif raw.startswith("postgresql://"):
        raw = raw.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw


REF_PREFIX = "FH"


def ref(task_id: int) -> str:
    """Ticket number shown to people: FH-0001. Derived from the row id, so it never collides."""
    return f"{REF_PREFIX}-{task_id:04d}"


def parse_ref(value) -> Optional[int]:
    """Accepts 'FH-0001', 'fh-1', '#12' or '12'. None when it is none of those."""
    if value is None:
        return None
    m = re.fullmatch(rf"\s*(?:#|(?:{REF_PREFIX}-?))?0*(\d+)\s*", str(value), re.I)
    return int(m.group(1)) if m else None


def now() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    name: str
    password_hash: Optional[str] = None   # None = cannot log in to the UI yet
    api_key_hash: Optional[str] = None    # sha256 of the key; the key itself is shown once
    created_at: datetime = Field(default_factory=now)


class Task(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    detail: str = ""
    owner: str = "unassigned"
    status: str = "open"          # suggested | open | doing | blocked | done | dismissed
    priority: str = "normal"      # low | normal | high
    due: Optional[str] = None     # YYYY-MM-DD
    tags: str = ""                # comma separated
    source: str = "manual"        # manual | meeting | slack
    source_ref: str = ""          # meeting title, slack permalink, etc.
    created_by: str = ""          # user name, "extractor", or "claude"
    suggested_owner: str = ""     # extractor's guess; becomes owner on accept
    source_meeting: str = ""      # EMOH path, e.g. meetings/2026-09-12-steve-matt-planning.md
    meeting_date: Optional[str] = None
    dup_of: Optional[int] = None  # extractor thinks this re-treads an existing task
    # Epics: a task dropped inside another one. The member stays an ordinary task; the epic
    # gets a mirror milestone for it. epic_no is set once, at creation: this task IS an epic
    # (EPIC-001, EPIC-002 ...) and the number is never reused or cleared.
    epic_id: Optional[int] = Field(default=None, index=True)
    epic_no: Optional[int] = None
    archived_at: Optional[datetime] = None   # soft delete; nothing is hard-deleted via the API
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class ExtractedMeeting(SQLModel, table=True):
    """Ledger: which EMOH meeting files have been through the extractor. Keyed by path,
    so a file is considered exactly once however many times the extractor runs."""
    path: str = Field(primary_key=True)
    extracted_at: datetime = Field(default_factory=now)
    task_count: int = 0


class SuggestionHide(SQLModel, table=True):
    """'Not mine' — this user has passed on this suggestion; it stays visible to the other."""
    task_id: int = Field(primary_key=True)
    user_id: int = Field(primary_key=True)
    hidden_at: datetime = Field(default_factory=now)


class Tag(SQLModel, table=True):
    task_id: int = Field(primary_key=True, foreign_key="task.id")
    tag: str = Field(primary_key=True)


class TagMeta(SQLModel, table=True):
    tag: str = Field(primary_key=True)
    description: str = ""
    updated_at: datetime = Field(default_factory=now)


class Milestone(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", index=True)
    text: str
    done: bool = False
    sort: int = 0
    linked_task_id: Optional[int] = None   # the epic member this milestone mirrors
    created_at: datetime = Field(default_factory=now)


def epic_label(no) -> str:
    """EPIC-001. Empty when the task is not an epic."""
    return f"EPIC-{int(no):03d}" if no else ""


class Notification(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    recipient_id: int = Field(index=True)
    actor: str = ""
    kind: str = "mention"         # mention | suggestion | message | assigned
    task_id: Optional[int] = None
    text: str = ""
    seen: bool = False
    created_at: datetime = Field(default_factory=now)


class Message(SQLModel, table=True):
    """Direct messages. Two people, so one thread; the model still records who sent it."""
    id: Optional[int] = Field(default=None, primary_key=True)
    from_id: int
    to_id: int
    body: str
    seen: bool = False
    created_at: datetime = Field(default_factory=now)


class TaskEvent(SQLModel, table=True):
    """History: who changed what. Written by the app on every mutation; never edited."""
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", index=True)
    actor: str
    kind: str                     # created | status | field | comment | milestone | tag | accepted | dismissed | archived | restored
    detail: str = ""
    created_at: datetime = Field(default_factory=now)


class Comment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", index=True)
    author: str
    body: str
    created_at: datetime = Field(default_factory=now)


class ChatMessage(SQLModel, table=True):
    """The Ask Claude thread. One thread for now; clear it to start over."""
    id: Optional[int] = Field(default=None, primary_key=True)
    role: str                     # user | assistant
    content: str
    created_at: datetime = Field(default_factory=now)


class Source(SQLModel, table=True):
    """Raw material we ingested, kept so a task can be traced back to what was said."""
    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str                     # meeting | slack
    title: str
    body: str
    occurred_at: Optional[str] = None
    created_at: datetime = Field(default_factory=now)


engine = create_engine(_url(), echo=False, pool_pre_ping=True)


def init() -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """create_all never alters existing tables. Add any column the models have
    that the live table lacks — enough for a small app without a migration tool."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as conn:
        for model in (User, Task, Comment, ChatMessage, Source, ExtractedMeeting, SuggestionHide, Tag, TagMeta, Milestone, Notification, Message, TaskEvent):
            table = model.__tablename__
            have = {c["name"] for c in insp.get_columns(table)}
            for col in model.__table__.columns:
                if col.name in have:
                    continue
                ctype = col.type.compile(engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{col.name}" {ctype}'))


def session() -> Session:
    return Session(engine, expire_on_commit=False)
