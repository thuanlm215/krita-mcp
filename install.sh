#!/usr/bin/env bash
# Install the Krita MCP bridge plugin on Linux or macOS.
#
# Copies the bundled plugin into Krita's resource folder and enables it in
# kritarc. Safe to re-run. Close Krita first: it rewrites kritarc on exit.
# Pass --force to write the flag anyway.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$here/mcp_server.py" install "$@"
