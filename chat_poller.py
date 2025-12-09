"""
Chat Poller - Polls chat-api for new messages and forwards to Claude sessions.
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Callable
from uuid import UUID

import httpx

from config import CHAT_API_URL, SOCIAL_API_URL, CHAT_POLL_INTERVAL

logger = logging.getLogger(__name__)


class ChatPoller:
    """
    Polls chat-api for new messages directed at a Claude session.

    Flow:
    1. Get messages from conversation since last_seen_at
    2. Filter out messages from self (session user)
    3. Forward new messages to callback
    """

    def __init__(
        self,
        session_id: UUID,
        conversation_id: UUID,
        session_user_id: UUID,
        access_token: str,
        on_message: Callable[[str, str], Any],  # (sender_name, content)
        poll_interval: float = CHAT_POLL_INTERVAL,
    ):
        self.session_id = session_id
        self.conversation_id = conversation_id
        self.session_user_id = session_user_id
        self.access_token = access_token
        self.on_message = on_message
        self.poll_interval = poll_interval

        # State
        self.running = False
        self.last_seen_at: Optional[datetime] = None
        self.last_message_id: Optional[str] = None

        # HTTP client
        self._client: Optional[httpx.AsyncClient] = None
        self._poll_task: Optional[asyncio.Task] = None

        # User cache (user_id -> display_name)
        self._user_cache: Dict[str, str] = {}

    @property
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def start(self):
        """Start polling for messages."""
        if self.running:
            return

        self._client = httpx.AsyncClient(timeout=30.0)
        self.running = True
        self._poll_task = asyncio.create_task(self._poll_loop())

        logger.info(f"Chat poller started for session {self.session_id}")

    async def stop(self):
        """Stop polling."""
        self.running = False

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if self._client:
            await self._client.aclose()
            self._client = None

        logger.info(f"Chat poller stopped for session {self.session_id}")

    async def _poll_loop(self):
        """Main polling loop."""
        while self.running:
            try:
                await self._check_messages()
            except Exception as e:
                logger.error(f"Error polling messages: {e}")

            await asyncio.sleep(self.poll_interval)

    async def _check_messages(self):
        """Check for new messages in the conversation."""
        if not self._client:
            return

        try:
            # Get recent messages
            response = await self._client.get(
                f"{CHAT_API_URL}/api/chat/conversations/{self.conversation_id}/messages",
                params={"page": 1, "page_size": 20},
                headers=self._headers,
            )

            if response.status_code != 200:
                logger.warning(f"Failed to get messages: {response.status_code}")
                return

            data = response.json()
            messages = data.get("messages", [])

            # Process new messages (oldest first)
            for msg in reversed(messages):
                msg_id = msg.get("id")
                sender_id = msg.get("authorId") or msg.get("sender_id")
                content = msg.get("text") or msg.get("content", "")
                created_at = msg.get("createdAt")

                # Skip if already seen
                if self.last_message_id and msg_id == self.last_message_id:
                    continue

                # Skip messages from self
                if str(sender_id) == str(self.session_user_id):
                    continue

                # Skip if before last seen
                if self.last_seen_at and created_at:
                    msg_time = self._parse_timestamp(created_at)
                    if msg_time and msg_time <= self.last_seen_at:
                        continue

                # Get sender name
                sender_name = await self._get_user_name(sender_id)

                logger.info(f"New message from {sender_name}: {content[:50]}...")

                # Forward to callback
                try:
                    result = self.on_message(sender_name, content)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.error(f"Error in message callback: {e}")

                # Update last seen
                self.last_message_id = msg_id
                if created_at:
                    self.last_seen_at = self._parse_timestamp(created_at)

        except httpx.RequestError as e:
            logger.error(f"Request error: {e}")

    async def _get_user_name(self, user_id: str) -> str:
        """Get display name for user (with caching)."""
        if user_id in self._user_cache:
            return self._user_cache[user_id]

        try:
            response = await self._client.get(
                f"{SOCIAL_API_URL}/api/v1/profiles/{user_id}",
                headers=self._headers,
            )

            if response.status_code == 200:
                data = response.json()
                name = data.get("display_name", f"User-{user_id[:8]}")
                self._user_cache[user_id] = name
                return name

        except Exception as e:
            logger.error(f"Failed to get user name: {e}")

        return f"User-{user_id[:8]}"

    def _parse_timestamp(self, ts: Any) -> Optional[datetime]:
        """Parse timestamp from various formats."""
        if isinstance(ts, datetime):
            return ts
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts)
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                pass
        return None


class ChatSender:
    """
    Sends messages to chat-api on behalf of a Claude session.
    """

    def __init__(
        self,
        conversation_id: UUID,
        access_token: str,
    ):
        self.conversation_id = conversation_id
        self.access_token = access_token
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def send(self, content: str) -> bool:
        """Send a message to the conversation."""
        if not self._client:
            self._client = httpx.AsyncClient(timeout=30.0)

        try:
            response = await self._client.post(
                f"{CHAT_API_URL}/api/chat/conversations/{self.conversation_id}/messages",
                json={"type": "text", "content": content},
                headers={"Authorization": f"Bearer {self.access_token}"},
            )

            if response.status_code in (200, 201):
                logger.debug(f"Message sent: {content[:50]}...")
                return True

            logger.error(f"Failed to send message: {response.status_code} - {response.text}")
            return False

        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return False
