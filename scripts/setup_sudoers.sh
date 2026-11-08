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
# Optional: the real OpenTelemetry eBPF Instrumentation (OBI) v1.0.0 for the Stage 15 comparison. Installed only if the extracted binary
# is byte-identical to the one whose hash was recorded (release tarball checksum verified, signature NOT verified: no cosign).
OBI_BUILT="$HOME/obi-dl/x/obi"
OBI_SHA=b239d223f41c5bd4577bb0a040dbc2621cdd7911b1936e3f2a36971de1704462
OBI_DEST=/usr/local/libexec/crosstrace/obi
if [ -f "$OBI_BUILT" ] && [ "$(sha256sum "$OBI_BUILT" | cut -d' ' -f1)" = "$OBI_SHA" ]; then
  sudo install -d -o root -g root -m 755 /usr/local/libexec/crosstrace
  sudo install -o root -g root -m 755 "$OBI_BUILT" "$OBI_DEST"
  RULE="$RULE, $OBI_DEST"
else
  echo "note: OBI not installed (missing or hash mismatch at $OBI_BUILT)"
fi
printf '%s\n' "$RULE" | sudo tee /etc/sudoers.d/crosstrace >/dev/null
sudo chmod 440 /etc/sudoers.d/crosstrace
sudo rm -f /etc/sudoers.d/crosstrace-tc   # superseded by the combined file
sudo visudo -cf /etc/sudoers.d/crosstrace
sudo -k   # forget cached credentials so the checks below are honest
sudo -n tc qdisc show dev lo && echo "OK: passwordless tc works"
sudo -n bpftrace --version && echo "OK: passwordless bpftrace works"
if [ -x "$DEST" ]; then
  rc=0; sudo -n "$DEST" --bad-flag 2>/dev/null || rc=$?   # usage error (status 2) proves it ran without a password
  [ "$rc" -eq 2 ] && echo "OK: passwordless ctrace works"
fi
if [ -x "$OBI_DEST" ]; then
  if sudo -n "$OBI_DEST" --version >/dev/null 2>&1; then echo "OK: passwordless obi runs"; else echo "note: obi installed but --version did not succeed"; fi
fi
