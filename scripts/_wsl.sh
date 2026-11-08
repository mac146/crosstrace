#!/usr/bin/env bash
# helper: run a command inside the repo with the project venv (dev only). usage: scripts/_wsl.sh <cmd...>
cd "$(dirname "$0")/.."
exec "$@"
