#!/bin/bash
# Claude Session Manager Installation Script

set -e

SERVICE_DIR="/opt/goamet/services/claude-session-manager"
LOG_DIR="/var/log/claude"

echo "=== Claude Session Manager Installation ==="

# Create log directory
echo "Creating log directory..."
sudo mkdir -p "$LOG_DIR"
sudo chown rob:rob "$LOG_DIR"
chmod 755 "$LOG_DIR"

# Create venv and install Python dependencies
echo "Installing Python dependencies..."
cd "$SERVICE_DIR"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install -r requirements.txt

# Install systemd service
echo "Installing systemd service..."
sudo cp "$SERVICE_DIR/claude-session-manager.service" /etc/systemd/system/
sudo systemctl daemon-reload

# Enable service (but don't start yet)
echo "Enabling service..."
sudo systemctl enable claude-session-manager

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Commands:"
echo "  Start:   sudo systemctl start claude-session-manager"
echo "  Stop:    sudo systemctl stop claude-session-manager"
echo "  Status:  sudo systemctl status claude-session-manager"
echo "  Logs:    journalctl -u claude-session-manager -f"
echo ""
echo "MCP Server can be added to Claude config:"
echo "  claude mcp add session-mcp -- $SERVICE_DIR/venv/bin/python $SERVICE_DIR/mcp_server.py"
echo ""
