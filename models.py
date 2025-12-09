"""
Database models for Claude Session Manager.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    Column, String, Integer, Boolean, Text, DateTime,
    Numeric, ForeignKey, Index, CheckConstraint
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy import text

from config import DATABASE_URL

# Create async engine
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()


class SessionStatus(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"
    WAITING = "waiting"


class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class LogSource(str, Enum):
    STDIN = "stdin"
    STDOUT = "stdout"
    STDERR = "stderr"
    SYSTEM = "system"
    CHAT = "chat"


class ClaudeSession(Base):
    """Represents a Claude Code session."""
    __tablename__ = "claude_sessions"
    __table_args__ = {"schema": "public"}

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))

    # Identification
    name = Column(String(100), nullable=False)
    user_id = Column(PGUUID(as_uuid=True), nullable=False)
    conversation_id = Column(PGUUID(as_uuid=True), nullable=True)

    # Status
    status = Column(String(20), nullable=False, default=SessionStatus.STOPPED.value)
    current_task = Column(Text, nullable=True)
    last_error = Column(Text, nullable=True)

    # Process info
    pid = Column(Integer, nullable=True)
    log_path = Column(String(255), nullable=True)

    # Configuration
    working_directory = Column(String(255), default="/home/rob")
    system_prompt = Column(Text, nullable=True)
    auto_restart = Column(Boolean, default=False)
    max_cost_usd = Column(Numeric(10, 4), default=10.0)

    # Metrics
    total_cost_usd = Column(Numeric(10, 6), default=0)
    message_count = Column(Integer, default=0)
    tool_calls = Column(Integer, default=0)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    started_at = Column(DateTime(timezone=True), nullable=True)
    last_activity_at = Column(DateTime(timezone=True), nullable=True)
    stopped_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    logs = relationship("ClaudeSessionLog", back_populates="session", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<ClaudeSession {self.name} ({self.status})>"

    def to_dict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "user_id": str(self.user_id),
            "conversation_id": str(self.conversation_id) if self.conversation_id else None,
            "status": self.status,
            "current_task": self.current_task,
            "pid": self.pid,
            "log_path": self.log_path,
            "total_cost_usd": float(self.total_cost_usd) if self.total_cost_usd else 0,
            "message_count": self.message_count,
            "tool_calls": self.tool_calls,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_activity_at": self.last_activity_at.isoformat() if self.last_activity_at else None,
        }


class ClaudeSessionLog(Base):
    """Log entry for a Claude session."""
    __tablename__ = "claude_session_logs"
    __table_args__ = {"schema": "public"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(PGUUID(as_uuid=True), ForeignKey("public.claude_sessions.id", ondelete="CASCADE"), nullable=False)

    # Log entry
    timestamp = Column(DateTime(timezone=True), server_default=text("NOW()"))
    level = Column(String(10), nullable=False)
    source = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)

    # Metadata
    tool_name = Column(String(100), nullable=True)
    tool_input = Column(JSONB, nullable=True)
    tool_output = Column(JSONB, nullable=True)
    cost_usd = Column(Numeric(10, 6), nullable=True)
    duration_ms = Column(Integer, nullable=True)

    # Partitioning
    created_date = Column(DateTime, server_default=text("CURRENT_DATE"))

    # Relationships
    session = relationship("ClaudeSession", back_populates="logs")

    def __repr__(self):
        return f"<ClaudeSessionLog {self.level}: {self.content[:50]}>"


async def init_db():
    """Initialize database connection."""
    async with engine.begin() as conn:
        # Verify connection
        await conn.execute(text("SELECT 1"))
    return True


async def get_session() -> AsyncSession:
    """Get a database session."""
    async with async_session() as session:
        yield session
