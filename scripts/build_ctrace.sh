#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/../collectors/ctrace"
make clean >/dev/null 2>&1 || true
make 2>&1
ls -l "$HOME/.crosstrace-build/ctrace"
