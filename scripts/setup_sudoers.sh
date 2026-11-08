#!/usr/bin/env bash
# One-time setup so the harness can run `sudo -n tc ...`, `sudo -n bpftrace ...` and the compiled collector without a password.
# !!! ROOT: writes /etc/sudoers.d/crosstrace and installs /usr/local/libexec/crosstrace/ctrace. Dev WSL/VM only. !!!
# Limitation: this lets your user run ANY `tc` and ANY `bpftrace` program as root without a password.
# bpftrace can read arbitrary kernel memory, so this is effectively root-equivalent: do not use outside
# a throwaway dev environment.
# The compiled collector (collectors/ctrace, build with `make -C collectors/ctrace`) is copied to a ROOT-OWNED path before it is
# allowed: a binary in a user-writable folder could be swapped for anything and then run as root. Re-run this script after
# rebuilding it to install the new copy.
# Idempotent. Remove with: sudo rm -r /etc/sudoers.d/crosstrace /etc/sudoers.d/crosstrace-tc /usr/local/libexec/crosstrace
set -eu
TC="$(command -v tc)"
BPFTRACE="$(command -v bpftrace)"
BUILT="$HOME/.crosstrace-build/ctrace"
DEST=/usr/local/libexec/crosstrace/ctrace
RULE="$USER ALL=(root) NOPASSWD: $TC, $BPFTRACE"
if [ -x "$BUILT" ]; then
  sudo install -d -o root -g root -m 755 /usr/local/libexec/crosstrace
  sudo install -o root -g root -m 755 "$BUILT" "$DEST"
  RULE="$RULE, $DEST"
else
  echo "note: $BUILT not built yet (make -C collectors/ctrace); the compiled collector is not enabled"
fi
printf '%s\n' "$RULE" | sudo tee /etc/sudoers.d/crosstrace >/dev/null
sudo chmod 440 /etc/sudoers.d/crosstrace
sudo rm -f /etc/sudoers.d/crosstrace-tc   # superseded by the combined file
sudo visudo -cf /etc/sudoers.d/crosstrace
sudo -k   # forget cached credentials so the checks below are honest
sudo -n tc qdisc show dev lo && echo "OK: passwordless tc works"
sudo -n bpftrace --version && echo "OK: passwordless bpftrace works"
if [ -x "$DEST" ]; then sudo -n "$DEST" --bad-flag 2>/dev/null; [ $? -eq 2 ] && echo "OK: passwordless ctrace works"; fi
