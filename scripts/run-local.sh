#!/bin/sh
# Start the server for a local MCP client (Claude Desktop, Claude Code), reading
# SPLITWISE_API_KEY from the project's .env so the key isn't copied into client config.
here="$(cd "$(dirname "$0")/.." && pwd)"
set -a
. "$here/.env"
set +a
exec "$here/.venv/bin/splitwise-mcp-server"
