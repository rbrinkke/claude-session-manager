#!/usr/bin/env python3
"""
Claude Session Manager Daemon

Manages multiple Claude Code sessions with:
- stdin/stdout JSON streaming
- Chat polling for incoming messages
- Log file writing
- Database state tracking
"""
import asyncio
import json
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from uuid import UUID

from dotenv import load_dotenv

# Load .env file
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    MAX_SESSIONS, HEARTBEAT_INTERVAL, LOG_CLEANUP_INTERVAL,
    LOG_LEVEL, LOG_DIR
)
from models import (
    engine, async_session, init_db,
    ClaudeSession, ClaudeSessionLog, SessionStatus, LogLevel, LogSource
)
from session import ClaudeProcess
from chat_poller import ChatPoller, ChatSender

# Configure logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(LOG_DIR / "daemon.log"),
    ]
)
logger = logging.getLogger("claude-session-manager")


class SessionManager:
    """
    Manages all Claude Code sessions.
    """

    def __init__(self):
        self.sessions: Dict[UUID, ManagedSession] = {}
        self.running = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the session manager."""
        logger.info("Starting Claude Session Manager")

        # Initialize database
        await init_db()

        # Reset any sessions that were running (from previous crash)
        await self._reset_orphaned_sessions()

        self.running = True

        # Start background tasks
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        logger.info("Session manager started")

    async def stop(self):
        """Stop the session manager and all sessions."""
        logger.info("Stopping Session Manager")
        self.running = False

        # Stop all sessions
        for session_id in list(self.sessions.keys()):
            await self.stop_session(session_id)

        # Cancel background tasks
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._cleanup_task:
            self._cleanup_task.cancel()

        logger.info("Session manager stopped")

    async def start_session(
        self,
        name: str,
        user_id: UUID,
        conversation_id: Optional[UUID] = None,
        access_token: Optional[str] = None,
        task: Optional[str] = None,
        working_directory: str = "/home/rob",
        system_prompt: Optional[str] = None,
    ) -> Optional[UUID]:
        """Start a new Claude session."""

        # Check limits
        active_count = len([s for s in self.sessions.values() if s.is_running])
        if active_count >= MAX_SESSIONS:
            logger.error(f"Max sessions ({MAX_SESSIONS}) reached")
            return None

        async with async_session() as db:
            # Create database record
            session = ClaudeSession(
                name=name,
                user_id=user_id,
                conversation_id=conversation_id,
                status=SessionStatus.STARTING.value,
                current_task=task,
                working_directory=working_directory,
                system_prompt=system_prompt,
            )
            db.add(session)
            await db.commit()
            await db.refresh(session)

            session_id = session.id

            # Create managed session
            managed = ManagedSession(
                session_id=session_id,
                name=name,
                user_id=user_id,
                conversation_id=conversation_id,
                access_token=access_token,
                working_directory=working_directory,
                system_prompt=system_prompt,
            )

            # Start it
            success = await managed.start()

            if success:
                self.sessions[session_id] = managed

                # Update database
                await db.execute(
                    update(ClaudeSession)
                    .where(ClaudeSession.id == session_id)
                    .values(
                        status=SessionStatus.RUNNING.value,
                        pid=managed.process.pid,
                        log_path=managed.process.log_path,
                        started_at=datetime.utcnow(),
                    )
                )
                await db.commit()

                logger.info(f"Session {name} started (ID: {session_id})")
                return session_id
            else:
                # Update database with error
                await db.execute(
                    update(ClaudeSession)
                    .where(ClaudeSession.id == session_id)
                    .values(
                        status=SessionStatus.ERROR.value,
                        last_error="Failed to start process",
                    )
                )
                await db.commit()

                logger.error(f"Failed to start session {name}")
                return None

    async def stop_session(self, session_id: UUID) -> bool:
        """Stop a session."""
        managed = self.sessions.get(session_id)
        if not managed:
            logger.warning(f"Session {session_id} not found")
            return False

        await managed.stop()
        del self.sessions[session_id]

        # Update database
        async with async_session() as db:
            await db.execute(
                update(ClaudeSession)
                .where(ClaudeSession.id == session_id)
                .values(
                    status=SessionStatus.STOPPED.value,
                    stopped_at=datetime.utcnow(),
                    pid=None,
                )
            )
            await db.commit()

        logger.info(f"Session {session_id} stopped")
        return True

    async def send_to_session(self, session_id: UUID, message: str) -> bool:
        """Send a message to a session."""
        managed = self.sessions.get(session_id)
        if not managed or not managed.is_running:
            return False

        return await managed.send_message(message)

    async def get_session(self, session_id: UUID) -> Optional[dict]:
        """Get session info."""
        async with async_session() as db:
            result = await db.execute(
                select(ClaudeSession).where(ClaudeSession.id == session_id)
            )
            session = result.scalar_one_or_none()
            return session.to_dict() if session else None

    async def list_sessions(self) -> list:
        """List all sessions."""
        async with async_session() as db:
            result = await db.execute(select(ClaudeSession))
            sessions = result.scalars().all()
            return [s.to_dict() for s in sessions]

    async def _reset_orphaned_sessions(self):
        """Reset sessions that were running before daemon restart."""
        async with async_session() as db:
            await db.execute(
                update(ClaudeSession)
                .where(ClaudeSession.status.in_([
                    SessionStatus.RUNNING.value,
                    SessionStatus.STARTING.value,
                ]))
                .values(
                    status=SessionStatus.STOPPED.value,
                    last_error="Daemon restarted",
                    pid=None,
                )
            )
            await db.commit()

    async def _heartbeat_loop(self):
        """Update session activity timestamps."""
        while self.running:
            try:
                for session_id, managed in list(self.sessions.items()):
                    if managed.is_running:
                        async with async_session() as db:
                            await db.execute(
                                update(ClaudeSession)
                                .where(ClaudeSession.id == session_id)
                                .values(last_activity_at=datetime.utcnow())
                            )
                            await db.commit()
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")

            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _cleanup_loop(self):
        """Cleanup old logs periodically."""
        while self.running:
            try:
                async with async_session() as db:
                    # Call cleanup function
                    from sqlalchemy import text
                    await db.execute(text("SELECT cleanup_old_session_logs()"))
                    await db.commit()
                    logger.info("Cleaned up old session logs")
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

            await asyncio.sleep(LOG_CLEANUP_INTERVAL)


class ManagedSession:
    """
    A managed Claude session with chat integration.
    """

    def __init__(
        self,
        session_id: UUID,
        name: str,
        user_id: UUID,
        conversation_id: Optional[UUID] = None,
        access_token: Optional[str] = None,
        working_directory: str = "/home/rob",
        system_prompt: Optional[str] = None,
    ):
        self.session_id = session_id
        self.name = name
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.access_token = access_token

        # Claude process
        self.process = ClaudeProcess(
            session_id=session_id,
            session_name=name,
            working_directory=working_directory,
            system_prompt=system_prompt,
            on_output=self._on_claude_output,
            on_error=self._on_claude_error,
            on_exit=self._on_claude_exit,
        )

        # Chat poller (if conversation configured)
        self.poller: Optional[ChatPoller] = None
        self.chat_sender: Optional[ChatSender] = None

        if conversation_id and access_token:
            self.poller = ChatPoller(
                session_id=session_id,
                conversation_id=conversation_id,
                session_user_id=user_id,
                access_token=access_token,
                on_message=self._on_chat_message,
            )
            self.chat_sender = ChatSender(
                conversation_id=conversation_id,
                access_token=access_token,
            )

    @property
    def is_running(self) -> bool:
        return self.process.running

    async def start(self) -> bool:
        """Start the session."""
        success = await self.process.start()

        if success and self.poller:
            await self.poller.start()

        return success

    async def stop(self):
        """Stop the session."""
        if self.poller:
            await self.poller.stop()

        await self.process.stop()

    async def send_message(self, content: str) -> bool:
        """Send message to Claude."""
        return await self.process.send_message(content)

    async def _on_claude_output(self, data: dict):
        """Handle output from Claude."""
        msg_type = data.get("type")

        # Log to database
        await self._log_to_db(
            level=LogLevel.INFO.value,
            source=LogSource.STDOUT.value,
            content=json.dumps(data),
            tool_name=data.get("tool") if msg_type == "tool_use" else None,
        )

        # Send results to chat
        if msg_type == "result" and self.chat_sender:
            result_text = data.get("result", "")
            if result_text:
                # Truncate if too long
                if len(result_text) > 2000:
                    result_text = result_text[:2000] + "... [truncated]"

                await self.chat_sender.send(f"[{self.name}] {result_text}")

        # Send assistant messages to chat
        elif msg_type == "assistant" and self.chat_sender:
            content = data.get("content", "")
            if content:
                await self.chat_sender.send(f"[{self.name}] {content}")

    async def _on_claude_error(self, error: str):
        """Handle errors from Claude."""
        await self._log_to_db(
            level=LogLevel.ERROR.value,
            source=LogSource.STDERR.value,
            content=error,
        )

        # Send error to chat
        if self.chat_sender and "error" in error.lower():
            await self.chat_sender.send(f"[{self.name}] ERROR: {error[:500]}")

    async def _on_claude_exit(self, return_code: int):
        """Handle Claude process exit."""
        await self._log_to_db(
            level=LogLevel.INFO.value,
            source=LogSource.SYSTEM.value,
            content=f"Process exited with code {return_code}",
        )

        # Notify via chat
        if self.chat_sender:
            await self.chat_sender.send(f"[{self.name}] Session ended (code: {return_code})")

    async def _on_chat_message(self, sender: str, content: str):
        """Handle incoming chat message."""
        await self._log_to_db(
            level=LogLevel.INFO.value,
            source=LogSource.CHAT.value,
            content=f"{sender}: {content}",
        )

        # Forward to Claude
        await self.process.send_message(f"{sender}: {content}")

    async def _log_to_db(
        self,
        level: str,
        source: str,
        content: str,
        tool_name: Optional[str] = None,
        cost_usd: Optional[float] = None,
    ):
        """Log entry to database."""
        try:
            async with async_session() as db:
                log = ClaudeSessionLog(
                    session_id=self.session_id,
                    level=level,
                    source=source,
                    content=content[:10000],  # Truncate if too long
                    tool_name=tool_name,
                    cost_usd=cost_usd,
                )
                db.add(log)
                await db.commit()
        except Exception as e:
            logger.error(f"Failed to log to database: {e}")


# Global manager instance
manager = SessionManager()


async def main():
    """Main entry point."""
    # Setup signal handlers
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("Received shutdown signal")
        asyncio.create_task(manager.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    # Start manager
    await manager.start()

    # Keep running
    try:
        while manager.running:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass

    logger.info("Daemon shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
