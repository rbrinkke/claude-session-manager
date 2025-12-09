"""
HTTP API for Claude Session Manager.

FastAPI-based REST API for external control of Claude sessions.
Runs alongside the daemon process.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

logger = logging.getLogger("claude-session-manager.api")


# ============================================================================
# Request/Response Models
# ============================================================================

class StartSessionRequest(BaseModel):
    """Request to start a new Claude session."""
    name: str = Field(..., min_length=1, max_length=100)
    user_id: str = Field(..., description="UUID of the user")
    conversation_id: Optional[str] = Field(None, description="UUID for chat integration")
    access_token: Optional[str] = Field(None, description="JWT for chat API")
    task: Optional[str] = Field(None, description="Task description")
    working_directory: str = Field("/home/rob", description="Working directory")
    system_prompt: Optional[str] = Field(None, description="System prompt for Claude")


class StartTestSessionRequest(BaseModel):
    """Request to start a test runner session."""
    scenario_id: str = Field(..., min_length=1, max_length=100)
    triggered_by: str = Field("manual", description="manual, scheduled, or ci")
    environment: str = Field("dev", description="dev, staging, or prod")
    user_id: Optional[str] = Field(None, description="Override user ID")
    wait_for_completion: bool = Field(False, description="Block until test completes")
    timeout_seconds: int = Field(300, ge=10, le=3600, description="Max wait time")


class SendMessageRequest(BaseModel):
    """Request to send a message to a session."""
    content: str = Field(..., min_length=1, max_length=50000)


class SessionResponse(BaseModel):
    """Session information response."""
    id: str
    name: str
    status: str
    user_id: str
    pid: Optional[int] = None
    message_count: int = 0
    tool_calls: int = 0
    total_cost_usd: float = 0.0
    current_task: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    last_activity_at: Optional[str] = None


class TestSessionResponse(BaseModel):
    """Test session response with additional test metadata."""
    session_id: str
    scenario_id: str
    status: str
    triggered_by: str
    environment: str
    started_at: str
    run_id: Optional[str] = None  # test-mcp run ID once available


class APIStats(BaseModel):
    """API statistics."""
    total_sessions: int
    running_sessions: int
    stopped_sessions: int
    error_sessions: int
    max_allowed: int
    uptime_seconds: float


# ============================================================================
# Test Runner System Prompt
# ============================================================================

TEST_RUNNER_SYSTEM_PROMPT = '''# Autonomous Test Executor

You are an autonomous test executor running in a headless Claude Code session.
Your ONLY purpose is to execute test scenarios using the test-mcp framework.

## Your Mission
Execute the test scenario "{scenario_id}" completely and autonomously.

## Available MCP Servers
You have access to these MCP servers:
- **test-mcp**: For test execution (understand_scenario, begin_test, record_observation, conclude_test)
- **auth-mcp**: For authentication (login_as, create_test_user)
- **chat-mcp**: For messaging (set_session, chat, get_chat_history)
- **social-api**: For social features (get_nearby_users, search_users, send_friend_request)
- **activity-api**: For activities (create_activity, join_activity, etc.)
- **notification-mcp**: For notifications (list_notifications, get_unread_count)

## Execution Protocol

### Step 1: Understand
First, call `understand_scenario("{scenario_id}")` to get:
- The goal (what must be proven)
- Success criteria (what defines success)
- Failure patterns (common issues and how to diagnose)
- Approach hints (guidance for execution)

### Step 2: Begin
Call `begin_test("{scenario_id}", "{triggered_by}", "{environment}")` to:
- Start the test timer
- Get the run_id for recording observations

### Step 3: Execute & Observe
Execute the test using available MCP tools. For EVERY action you take:
- Call the appropriate MCP tool
- IMMEDIATELY record an observation with `record_observation()`:
  - action: What you did (e.g., "Login as Abbas")
  - reasoning: Why you did it (e.g., "Need authenticated user to test chat")
  - mcp_server: Which server (e.g., "auth-mcp")
  - mcp_tool: Which tool (e.g., "login_as")
  - input_data: What you passed
  - output_data: What you received
  - duration_ms: Estimate if not measured
  - passed: Whether this step succeeded
  - assessment: Brief assessment of the result

### Step 4: Conclude
After completing all test steps, call `conclude_test()` with:
- passed: Your overall assessment (true/false)
- assessment: Detailed explanation of what happened
- confidence: How confident you are (0.0-1.0)
- error_type: If failed, categorize the error
- error_message: If failed, the error details

## Critical Rules

1. **ALWAYS record observations** - Every MCP tool call must be recorded
2. **Be thorough** - Follow the success criteria exactly
3. **Handle errors gracefully** - If something fails, still conclude the test
4. **Be efficient** - Don't take unnecessary steps
5. **Stay focused** - Only execute this test, nothing else
6. **Exit when done** - After conclude_test(), your job is complete

## Example Flow

```
1. understand_scenario("chat_flow")
   → Get goal, criteria, patterns

2. begin_test("chat_flow", "manual", "dev")
   → Get run_id

3. login_as("abbas")
   → record_observation(action="Login as Abbas", mcp_server="auth-mcp", ...)

4. set_session(token)
   → record_observation(action="Set chat session", mcp_server="chat-mcp", ...)

5. chat(to="emma", message="Hi!")
   → record_observation(action="Send test message", mcp_server="chat-mcp", ...)

6. get_chat_history(with_user="emma")
   → record_observation(action="Verify message delivered", ...)

7. conclude_test(passed=True, assessment="Chat flow works correctly...")
```

Now execute the test. Begin with understand_scenario("{scenario_id}").
'''


# ============================================================================
# API Application
# ============================================================================

def create_api(manager) -> FastAPI:
    """
    Create the FastAPI application with the given session manager.

    Args:
        manager: SessionManager instance from daemon

    Returns:
        Configured FastAPI app
    """

    start_time = datetime.utcnow()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("API server starting")
        yield
        logger.info("API server stopping")

    app = FastAPI(
        title="Claude Session Manager API",
        description="REST API for managing Claude Code sessions and test execution",
        version="1.0.0",
        lifespan=lifespan,
    )

    # ========================================================================
    # Health & Stats
    # ========================================================================

    @app.get("/health")
    async def health_check():
        """Health check endpoint."""
        return {
            "status": "healthy",
            "service": "claude-session-manager",
            "timestamp": datetime.utcnow().isoformat(),
        }

    @app.get("/api/stats", response_model=APIStats)
    async def get_stats():
        """Get API and session statistics."""
        sessions = await manager.list_sessions()

        running = sum(1 for s in sessions if s.get("status") == "running")
        stopped = sum(1 for s in sessions if s.get("status") == "stopped")
        error = sum(1 for s in sessions if s.get("status") == "error")

        uptime = (datetime.utcnow() - start_time).total_seconds()

        return APIStats(
            total_sessions=len(sessions),
            running_sessions=running,
            stopped_sessions=stopped,
            error_sessions=error,
            max_allowed=manager.max_sessions if hasattr(manager, 'max_sessions') else 10,
            uptime_seconds=uptime,
        )

    # ========================================================================
    # Session Management
    # ========================================================================

    @app.get("/api/sessions", response_model=list[SessionResponse])
    async def list_sessions(status: Optional[str] = None, limit: int = 20):
        """List all sessions with optional filtering."""
        sessions = await manager.list_sessions()

        if status:
            sessions = [s for s in sessions if s.get("status") == status]

        sessions = sessions[:limit]

        return [
            SessionResponse(
                id=s["id"],
                name=s["name"],
                status=s["status"],
                user_id=s["user_id"],
                pid=s.get("pid"),
                message_count=s.get("message_count", 0),
                tool_calls=s.get("tool_calls", 0),
                total_cost_usd=s.get("total_cost_usd", 0.0),
                current_task=s.get("current_task"),
                created_at=s.get("created_at"),
                started_at=s.get("started_at"),
                last_activity_at=s.get("last_activity_at"),
            )
            for s in sessions
        ]

    @app.get("/api/sessions/{session_id}", response_model=SessionResponse)
    async def get_session(session_id: str):
        """Get details of a specific session."""
        session = await manager.get_session(UUID(session_id))

        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        return SessionResponse(
            id=session["id"],
            name=session["name"],
            status=session["status"],
            user_id=session["user_id"],
            pid=session.get("pid"),
            message_count=session.get("message_count", 0),
            tool_calls=session.get("tool_calls", 0),
            total_cost_usd=session.get("total_cost_usd", 0.0),
            current_task=session.get("current_task"),
            created_at=session.get("created_at"),
            started_at=session.get("started_at"),
            last_activity_at=session.get("last_activity_at"),
        )

    @app.post("/api/sessions", response_model=SessionResponse, status_code=201)
    async def start_session(request: StartSessionRequest):
        """Start a new Claude session."""
        session_id = await manager.start_session(
            name=request.name,
            user_id=UUID(request.user_id),
            conversation_id=UUID(request.conversation_id) if request.conversation_id else None,
            access_token=request.access_token,
            task=request.task,
            working_directory=request.working_directory,
            system_prompt=request.system_prompt,
        )

        if not session_id:
            raise HTTPException(
                status_code=503,
                detail="Failed to start session. Max sessions may be reached."
            )

        session = await manager.get_session(session_id)

        return SessionResponse(
            id=session["id"],
            name=session["name"],
            status=session["status"],
            user_id=session["user_id"],
            pid=session.get("pid"),
            current_task=session.get("current_task"),
            created_at=session.get("created_at"),
            started_at=session.get("started_at"),
        )

    @app.delete("/api/sessions/{session_id}")
    async def stop_session(session_id: str):
        """Stop a running session."""
        success = await manager.stop_session(UUID(session_id))

        if not success:
            raise HTTPException(status_code=404, detail="Session not found or already stopped")

        return {"status": "stopped", "session_id": session_id}

    @app.post("/api/sessions/{session_id}/message")
    async def send_message(session_id: str, request: SendMessageRequest):
        """Send a message to a running session."""
        success = await manager.send_to_session(UUID(session_id), request.content)

        if not success:
            raise HTTPException(
                status_code=400,
                detail="Failed to send message. Session may not be running."
            )

        return {"status": "sent", "session_id": session_id}

    # ========================================================================
    # Test Execution
    # ========================================================================

    @app.post("/api/test/run", response_model=TestSessionResponse, status_code=201)
    async def run_test_scenario(
        request: StartTestSessionRequest,
        background_tasks: BackgroundTasks
    ):
        """
        Start a test runner session for a scenario.

        This spawns a new Claude session with the test executor system prompt.
        The session will autonomously:
        1. Understand the scenario via test-mcp
        2. Execute the test using available MCP servers
        3. Record observations for each step
        4. Conclude with pass/fail assessment
        """
        from config import MONITOR_ORG_ID

        # Build system prompt
        system_prompt = TEST_RUNNER_SYSTEM_PROMPT.format(
            scenario_id=request.scenario_id,
            triggered_by=request.triggered_by,
            environment=request.environment,
        )

        # User ID for the session
        user_id = UUID(request.user_id) if request.user_id else UUID(MONITOR_ORG_ID)

        # Start the session
        session_id = await manager.start_session(
            name=f"test-{request.scenario_id}-{datetime.utcnow().strftime('%H%M%S')}",
            user_id=user_id,
            task=f"Execute test scenario: {request.scenario_id}",
            system_prompt=system_prompt,
        )

        if not session_id:
            raise HTTPException(
                status_code=503,
                detail="Failed to start test session. Max sessions may be reached."
            )

        # Send the initial prompt to kick off execution
        initial_prompt = f"""Execute test scenario "{request.scenario_id}" now.

Triggered by: {request.triggered_by}
Environment: {request.environment}

Start with: understand_scenario("{request.scenario_id}")
"""

        await manager.send_to_session(session_id, initial_prompt)

        response = TestSessionResponse(
            session_id=str(session_id),
            scenario_id=request.scenario_id,
            status="running",
            triggered_by=request.triggered_by,
            environment=request.environment,
            started_at=datetime.utcnow().isoformat(),
        )

        # If wait requested, wait for completion
        if request.wait_for_completion:
            try:
                await wait_for_session_completion(
                    manager,
                    session_id,
                    request.timeout_seconds
                )
                session = await manager.get_session(session_id)
                response.status = session.get("status", "unknown")
            except asyncio.TimeoutError:
                response.status = "timeout"

        return response

    @app.get("/api/test/sessions", response_model=list[SessionResponse])
    async def list_test_sessions(limit: int = 20):
        """List all test sessions (sessions with name starting with 'test-')."""
        sessions = await manager.list_sessions()

        test_sessions = [
            s for s in sessions
            if s.get("name", "").startswith("test-")
        ][:limit]

        return [
            SessionResponse(
                id=s["id"],
                name=s["name"],
                status=s["status"],
                user_id=s["user_id"],
                pid=s.get("pid"),
                message_count=s.get("message_count", 0),
                tool_calls=s.get("tool_calls", 0),
                total_cost_usd=s.get("total_cost_usd", 0.0),
                current_task=s.get("current_task"),
                created_at=s.get("created_at"),
                started_at=s.get("started_at"),
                last_activity_at=s.get("last_activity_at"),
            )
            for s in test_sessions
        ]

    return app


async def wait_for_session_completion(manager, session_id: UUID, timeout: int):
    """Wait for a session to complete (stop or error)."""
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        session = await manager.get_session(session_id)

        if not session:
            return

        status = session.get("status")
        if status in ("stopped", "error"):
            return

        await asyncio.sleep(2)

    raise asyncio.TimeoutError(f"Session {session_id} did not complete within {timeout}s")
