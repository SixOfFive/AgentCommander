#!/usr/bin/env bash
# AgentCommander launcher (Linux/macOS)
# Runs the CLI without requiring a global pip install — uses the source tree directly.

set -euo pipefail
AC_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
export PYTHONPATH="$AC_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

if command -v python3 >/dev/null 2>&1; then
  exec python3 -m agentcommander "$@"
fi
if command -v python >/dev/null 2>&1; then
  exec python -m agentcommander "$@"
fi
echo "Could not find python3/python on PATH. Install Python 3.10+ and retry." >&2
exit 1
