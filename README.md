# Claude Session Manager

Beheert meerdere Claude Code sessies met bidirectionele chat-integratie.

## Architectuur

```
┌─────────────────────────────────────────────────────────────────┐
│                    Claude Session Manager                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐       │
│  │   Daemon     │    │  MCP Server  │    │  Chat Poller │       │
│  │  (systemd)   │    │ (session-mcp)│    │              │       │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘       │
│         │                   │                   │                │
│         ▼                   ▼                   ▼                │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              Claude Code Subprocess                      │    │
│  │   stdin (stream-json) ◄──► stdout (stream-json)         │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │     monitoringdb       │
              │  (PostgreSQL:5441)     │
              ├────────────────────────┤
              │ - claude_sessions      │
              │ - claude_session_logs  │
              └────────────────────────┘
```

## Locaties

| Component | Pad |
|-----------|-----|
| Service code | `/opt/goamet/services/claude-session-manager/` |
| Systemd unit | `/etc/systemd/system/claude-session-manager.service` |
| Log directory | `/var/log/claude/` |
| Database | `monitoringdb` (PostgreSQL container, port 5441) |
| MCP config | `~/.claude.json` (session-mcp) |

## Bestanden

```
/opt/goamet/services/claude-session-manager/
├── daemon.py           # Hoofd daemon - SessionManager & ManagedSession
├── session.py          # ClaudeProcess - subprocess met JSON streaming
├── chat_poller.py      # ChatPoller & ChatSender voor chat-api integratie
├── models.py           # SQLAlchemy models (ClaudeSession, ClaudeSessionLog)
├── config.py           # Configuratie uit .env
├── mcp_server.py       # MCP server voor sessie beheer
├── .env                # Environment configuratie
├── requirements.txt    # Python dependencies
├── install.sh          # Installatie script
├── venv/               # Python virtual environment
└── README.md           # Deze documentatie
```

## Database Tabellen (monitoringdb)

### claude_sessions
- `id` - UUID primary key
- `name` - Sessie naam (uniek voor actieve sessies)
- `user_id` - UUID van eigenaar
- `conversation_id` - UUID voor chat-api integratie
- `status` - stopped/starting/running/error/waiting
- `current_task` - Huidige taak beschrijving
- `pid` - Process ID
- `log_path` - Pad naar log bestand
- `working_directory` - Werkdirectory voor Claude
- `system_prompt` - Custom system prompt
- `total_cost_usd` - Totale kosten
- `message_count` - Aantal berichten
- `tool_calls` - Aantal tool calls

### claude_session_logs
- `id` - Serial primary key
- `session_id` - Foreign key naar sessions
- `timestamp` - Tijdstempel
- `level` - debug/info/warn/error
- `source` - stdin/stdout/stderr/system/chat
- `content` - Log inhoud
- `tool_name` - Tool naam (indien van toepassing)
- `cost_usd` - Kosten per entry

## Commando's

```bash
# Service beheer
sudo systemctl start claude-session-manager
sudo systemctl stop claude-session-manager
sudo systemctl restart claude-session-manager
sudo systemctl status claude-session-manager

# Logs bekijken
journalctl -u claude-session-manager -f

# Session logs
tail -f /var/log/claude/session-*.log
```

## MCP Server

De MCP server staat in een aparte repo:
- **Locatie**: `/opt/goamet/mcp-servers/session-mcp/`
- **Repo**: `rbrinkke/mcp-servers`

Tools beschikbaar via `session-mcp`:
| Tool | Beschrijving |
|------|--------------|
| `list_sessions` | Lijst alle Claude sessies |
| `get_session` | Details van een sessie |
| `get_session_logs` | Bekijk sessie logs |
| `session_stats` | Statistieken overzicht |
| `delete_session` | Verwijder gestopte sessie |

## Configuratie (.env)

```env
# Database
DATABASE_URL=postgresql+asyncpg://postgres:...@localhost:5441/monitoringdb

# API URLs (k3d cluster)
CHAT_API_URL=http://localhost:30001
AUTH_API_URL=http://localhost:30012
SOCIAL_API_URL=http://localhost:30005

# Limieten
MAX_SESSIONS=10
DEFAULT_MAX_COST_USD=10.0

# Polling intervals (seconden)
CHAT_POLL_INTERVAL=2.0
HEARTBEAT_INTERVAL=30.0
LOG_CLEANUP_INTERVAL=3600.0

# Paden
LOG_DIR=/var/log/claude
CLAUDE_BIN=/home/rob/.local/bin/claude
```

## Integratie met Activity App

De Session Manager integreert met:

1. **monitoringdb** - Zelfde database als test-mcp voor monitoring data
2. **chat-api** - Bidirectionele communicatie via chat polling
3. **auth-service** - JWT tokens voor API authenticatie
4. **MCP ecosystem** - session-mcp server voor beheer

## Claude Code Streaming

Gebruikt `claude --input-format stream-json --output-format stream-json --verbose`:

**Input formaat:**
```json
{"type": "user", "message": {"role": "user", "content": "Say hello"}}
```

**Output formaat:**
```json
{"type": "assistant", "message": {...}}
{"type": "result", "result": "Hello world", "total_cost_usd": 0.35}
```

## Logs Retentie

- Session logs worden 7 dagen bewaard
- Cleanup functie draait elk uur
- File logs in `/var/log/claude/` (handmatig beheren)
