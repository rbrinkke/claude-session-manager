"""
Claude Code Session - Subprocess management with stdin/stdout streaming.
"""
import asyncio
import json
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Any
from uuid import UUID

from config import CLAUDE_BIN, LOG_DIR

logger = logging.getLogger(__name__)


class ClaudeProcess:
    """
    Manages a Claude Code subprocess with JSON streaming.

    Uses: claude --input-format stream-json --output-format stream-json
    """

    def __init__(
        self,
        session_id: UUID,
        session_name: str,
        working_directory: str = "/home/rob",
        system_prompt: Optional[str] = None,
        on_output: Optional[Callable[[dict], Any]] = None,
        on_error: Optional[Callable[[str], Any]] = None,
        on_exit: Optional[Callable[[int], Any]] = None,
    ):
        self.session_id = session_id
        self.session_name = session_name
        self.working_directory = working_directory
        self.system_prompt = system_prompt

        # Callbacks
        self.on_output = on_output
        self.on_error = on_error
        self.on_exit = on_exit

        # Process state
        self.process: Optional[asyncio.subprocess.Process] = None
        self.pid: Optional[int] = None
        self.running = False
        self.log_file: Optional[Path] = None

        # Tasks
        self._stdout_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None

    @property
    def log_path(self) -> str:
        """Get path to log file."""
        return str(LOG_DIR / f"session-{self.session_id}.log")

    async def start(self) -> bool:
        """Start the Claude subprocess."""
        if self.running:
            logger.warning(f"Session {self.session_name} already running")
            return False

        try:
            # Build command
            cmd = [
                CLAUDE_BIN,
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--verbose",
                "--dangerously-skip-permissions",  # For automated use
            ]

            if self.system_prompt:
                cmd.extend(["--system-prompt", self.system_prompt])

            logger.info(f"Starting Claude: {' '.join(cmd)}")

            # Open log file
            self.log_file = Path(self.log_path)
            self.log_file.parent.mkdir(parents=True, exist_ok=True)

            # Start process
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_directory,
            )

            self.pid = self.process.pid
            self.running = True

            # Start output readers
            self._stdout_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._read_stderr())

            logger.info(f"Session {self.session_name} started (PID: {self.pid})")

            # Write startup to log
            self._log_to_file("system", f"Session started (PID: {self.pid})")

            return True

        except Exception as e:
            logger.error(f"Failed to start session {self.session_name}: {e}")
            self.running = False
            if self.on_error:
                await self._safe_callback(self.on_error, str(e))
            return False

    async def stop(self, timeout: float = 10.0) -> bool:
        """Stop the Claude subprocess gracefully."""
        if not self.running or not self.process:
            return True

        try:
            logger.info(f"Stopping session {self.session_name}")

            # Send SIGTERM
            self.process.terminate()

            try:
                await asyncio.wait_for(self.process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"Session {self.session_name} didn't stop gracefully, killing")
                self.process.kill()
                await self.process.wait()

            self.running = False
            return_code = self.process.returncode

            # Cancel reader tasks
            if self._stdout_task:
                self._stdout_task.cancel()
            if self._stderr_task:
                self._stderr_task.cancel()

            self._log_to_file("system", f"Session stopped (return code: {return_code})")

            if self.on_exit:
                await self._safe_callback(self.on_exit, return_code)

            logger.info(f"Session {self.session_name} stopped (return code: {return_code})")
            return True

        except Exception as e:
            logger.error(f"Error stopping session {self.session_name}: {e}")
            return False

    async def send_message(self, content: str) -> bool:
        """Send a message to Claude via stdin."""
        if not self.running or not self.process or not self.process.stdin:
            logger.error(f"Cannot send message: session {self.session_name} not running")
            return False

        try:
            # Format as JSON user message (stream-json format)
            message = {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": content
                }
            }

            json_line = json.dumps(message) + "\n"

            self._log_to_file("stdin", content)

            self.process.stdin.write(json_line.encode())
            await self.process.stdin.drain()

            logger.debug(f"Sent message to {self.session_name}: {content[:100]}...")
            return True

        except Exception as e:
            logger.error(f"Failed to send message to {self.session_name}: {e}")
            return False

    async def _read_stdout(self):
        """Read and process stdout from Claude."""
        if not self.process or not self.process.stdout:
            return

        try:
            while self.running:
                line = await self.process.stdout.readline()
                if not line:
                    break

                try:
                    data = json.loads(line.decode().strip())
                    self._log_to_file("stdout", json.dumps(data))

                    if self.on_output:
                        await self._safe_callback(self.on_output, data)

                except json.JSONDecodeError:
                    # Non-JSON output
                    text = line.decode().strip()
                    if text:
                        self._log_to_file("stdout", text)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error reading stdout: {e}")

    async def _read_stderr(self):
        """Read and process stderr from Claude."""
        if not self.process or not self.process.stderr:
            return

        try:
            while self.running:
                line = await self.process.stderr.readline()
                if not line:
                    break

                text = line.decode().strip()
                if text:
                    self._log_to_file("stderr", text)

                    if self.on_error:
                        await self._safe_callback(self.on_error, text)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error reading stderr: {e}")

    def _log_to_file(self, source: str, content: str):
        """Write to log file."""
        if not self.log_file:
            return

        try:
            timestamp = datetime.now().isoformat()
            with open(self.log_file, "a") as f:
                f.write(f"[{timestamp}] [{source.upper()}] {content}\n")
        except Exception as e:
            logger.error(f"Failed to write to log file: {e}")

    async def _safe_callback(self, callback: Callable, *args):
        """Safely execute callback (sync or async)."""
        try:
            result = callback(*args)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.error(f"Callback error: {e}")

    def is_alive(self) -> bool:
        """Check if process is still running."""
        if not self.process:
            return False
        return self.process.returncode is None
