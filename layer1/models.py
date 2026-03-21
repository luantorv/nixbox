from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, Relationship, SQLModel


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"

class LogStream(str, Enum):
    stdout = "stdout"
    stderr = "stderr"

# ---------------------------------------------------------------------------
# Recurrence
# ---------------------------------------------------------------------------

class RecurrenceBase(SQLModel):
    cron_string: str
    enabled: bool = True

class Recurrence(RecurrenceBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    last_execution: Optional[datetime] = Field(default=None)
    next_execution: Optional[datetime] = Field(default=None)

    tasks: list["Task"] = Relationship(back_populates="recurrence")

class RecurrenceCreate(RecurrenceBase):
    pass

class RecurrenceRead(RecurrenceBase):
    id: int
    last_execution: Optional[datetime]
    next_execution: Optional[datetime]

# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------

class TaskBase(SQLModel):
    name: str
    sandbox_type: str
    scheduled_at: Optional[datetime] = Field(default=None)
    recurrence_id: Optional[int] = Field(default=None, foreign_key="recurrence.id")

class Task(TaskBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    status: TaskStatus = Field(default=TaskStatus.pending)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    pid: Optional[int] = Field(default=None)

    recurrence: Optional[Recurrence] = Relationship(back_populates="tasks")
    interactions: list["Interaction"] = Relationship(back_populates="task")
    log_entries: list["LogEntry"] = Relationship(back_populates="task")

class TaskCreate(TaskBase):
    initial_prompt: str

class TaskRead(TaskBase):
    id: int
    status: TaskStatus
    created_at: datetime
    pid: Optional[int]
    recurrence: Optional[RecurrenceRead]

# ---------------------------------------------------------------------------
# Interaction
# ---------------------------------------------------------------------------

class InteractionBase(SQLModel):
    role: str  # "user" | "assistant"
    content: str

class Interaction(InteractionBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    task: Optional[Task] = Relationship(back_populates="interactions")

class InteractionCreate(InteractionBase):
    pass

class InteractionRead(InteractionBase):
    id: int
    task_id: int
    created_at: datetime

# ---------------------------------------------------------------------------
# LogEntry
# ---------------------------------------------------------------------------

class LogEntryBase(SQLModel):
    stream: LogStream
    content: str

class LogEntry(LogEntryBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    task: Optional[Task] = Relationship(back_populates="log_entries")

class LogEntryRead(LogEntryBase):
    id: int
    task_id: int
    created_at: datetime