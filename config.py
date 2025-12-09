"""
Configuration for Claude Session Manager.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env file
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

# Database
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5441/activitydb"
)

# Chat API
CHAT_API_URL = os.getenv("CHAT_API_URL", "http://localhost:8001")
AUTH_API_URL = os.getenv("AUTH_API_URL", "http://localhost:8012")
SOCIAL_API_URL = os.getenv("SOCIAL_API_URL", "http://localhost:8005")

# Claude Monitor Organization ID (created in migration)
MONITOR_ORG_ID = os.getenv("MONITOR_ORG_ID", "019b02c5-fb08-71d5-8041-ffe4fe40896c")

# Session limits
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "10"))
DEFAULT_MAX_COST_USD = float(os.getenv("DEFAULT_MAX_COST_USD", "10.0"))

# Polling intervals (seconds)
CHAT_POLL_INTERVAL = float(os.getenv("CHAT_POLL_INTERVAL", "2.0"))
HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL", "30.0"))
LOG_CLEANUP_INTERVAL = float(os.getenv("LOG_CLEANUP_INTERVAL", "3600.0"))  # 1 hour

# Paths
LOG_DIR = Path(os.getenv("LOG_DIR", "/var/log/claude"))
WORKING_DIR = Path(os.getenv("WORKING_DIR", "/home/rob"))

# Claude binary
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")

# Service token for internal API calls
SERVICE_TOKEN = os.getenv("SERVICE_TOKEN", "")

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
