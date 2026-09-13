"""Storage. Postgres on Railway via DATABASE_URL, SQLite locally when it is unset."""
import os
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
    status: str = "open"          # open | doing | blocked | done
    priority: str = "normal"      # low | normal | high
    due: Optional[str] = None     # YYYY-MM-DD
    tags: str = ""                # comma separated
    source: str = "manual"        # manual | meeting | slack
    source_ref: str = ""          # meeting title, slack permalink, etc.
    created_by: str = ""          # user name, "extractor", or "claude"
    archived_at: Optional[datetime] = None   # soft delete; nothing is hard-deleted via the API
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


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
        for model in (User, Task, Comment, ChatMessage, Source):
            table = model.__tablename__
            have = {c["name"] for c in insp.get_columns(table)}
            for col in model.__table__.columns:
                if col.name in have:
                    continue
                ctype = col.type.compile(engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{col.name}" {ctype}'))


def session() -> Session:
    return Session(engine)
