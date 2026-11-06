#!/usr/bin/env bash
# Installs (or removes, with --uninstall) the Ask host agent on macOS as a LaunchAgent.
# Same as Install-AskAgent.ps1, for Macs without PowerShell.
#   ./ask/agent/install.sh [--port 9101] [--mcp-config path/to/.mcp.json] [--uninstall]
set -euo pipefail
PORT=9101
UNINSTALL=0
MCP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    --mcp-config) MCP="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
[ "$(uname -s)" = "Darwin" ] || { echo "This installer is for macOS." >&2; exit 1; }

LABEL=com.vcommunity.ask-host-agent
APP_DIR="$HOME/Library/Application Support/ask-host-agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TOKEN_DIR="$HOME/.config/ask-host-agent"
TOKEN_FILE="$TOKEN_DIR/token"
LOG="$HOME/Library/Logs/ask-host-agent.log"
DOMAIN="gui/$(id -u)"
HERE="$(cd "$(dirname "$0")" && pwd)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [ "$UNINSTALL" = 1 ]; then
  rm -f "$PLIST" "$APP_DIR/ask-host-agent.py"
  echo "Removed the Ask host agent. The token is still in $TOKEN_FILE."
  exit 0
fi

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 not found: xcode-select --install" >&2; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "Python 3.9+ needed" >&2; exit 1; }

mkdir -p "$APP_DIR" "$TOKEN_DIR" "$(dirname "$PLIST")" "$(dirname "$LOG")"
cp "$HERE/ask-host-agent.py" "$APP_DIR/"
if [ ! -f "$TOKEN_FILE" ]; then
  (umask 077; openssl rand -hex 32 > "$TOKEN_FILE")
fi
if [ -n "$MCP" ]; then
  "$PY" -c 'import json,sys; json.load(open(sys.argv[1]))' "$MCP" || { echo "$MCP is not JSON" >&2; exit 1; }
  cp "$MCP" "$TOKEN_DIR/mcp.json"
  echo "MCP servers: copied $MCP to $TOKEN_DIR/mcp.json."
fi
chmod 700 "$TOKEN_DIR"; chmod 600 "$TOKEN_FILE"
TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"

esc() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(esc "$PY")</string>
    <string>$(esc "$APP_DIR/ask-host-agent.py")</string>
    <string>--port</string><string>$PORT</string>
    <string>--token-file</string><string>$(esc "$TOKEN_FILE")</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(esc "$LOG")</string>
  <key>StandardErrorPath</key><string>$(esc "$LOG")</string>
</dict>
</plist>
EOF
launchctl bootstrap "$DOMAIN" "$PLIST"
sleep 3
if curl -fsS -H "Authorization: Bearer $TOKEN" "http://localhost:$PORT/metrics" >/dev/null; then
  echo "Ask host agent is running on port $PORT."
else
  echo "The agent did not answer yet. Check $LOG and: launchctl print $DOMAIN/$LABEL" >&2
fi
echo
echo "Add this line to the .env file of vCommunity-Observability, then run: docker compose up -d vco-ask"
echo "ASK_AGENT_TOKEN=$TOKEN"
if [ -f "$TOKEN_DIR/mcp.json" ]; then
  echo
  echo "MCP servers (what Ask will offer on the MCP tools page):"
  "$PY" "$APP_DIR/ask-host-agent.py" --mcp-config "$TOKEN_DIR/mcp.json" --mcp-list || true
  echo "If a server script lives under ~/Documents or ~/Desktop and the MCP tools page says 'Operation not permitted',"
  echo "give $("$PY" -c 'import sys; print(sys.executable)') Full Disk Access (System Settings > Privacy & Security), then: launchctl kickstart -k $DOMAIN/$LABEL"
fi
command -v macmon >/dev/null || echo "Optional: brew install macmon (temperature and power)."
