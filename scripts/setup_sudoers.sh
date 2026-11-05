#!/usr/bin/env bash
# One-time setup so the harness can run `sudo -n tc ...` and `sudo -n bpftrace ...` without a password.
# !!! ROOT: writes /etc/sudoers.d/crosstrace. Dev WSL/VM only. !!!
# Limitation: this lets your user run ANY `tc` and ANY `bpftrace` program as root without a password.
# bpftrace can read arbitrary kernel memory, so this is effectively root-equivalent: do not use outside
# a throwaway dev environment.
# Idempotent. Remove with: sudo rm /etc/sudoers.d/crosstrace /etc/sudoers.d/crosstrace-tc
set -eu
TC="$(command -v tc)"
BPFTRACE="$(command -v bpftrace)"
printf '%s ALL=(root) NOPASSWD: %s, %s\n' "$USER" "$TC" "$BPFTRACE" | sudo tee /etc/sudoers.d/crosstrace >/dev/null
sudo chmod 440 /etc/sudoers.d/crosstrace
sudo rm -f /etc/sudoers.d/crosstrace-tc   # superseded by the combined file
sudo visudo -cf /etc/sudoers.d/crosstrace
sudo -k   # forget cached credentials so the checks below are honest
sudo -n tc qdisc show dev lo && echo "OK: passwordless tc works"
sudo -n bpftrace --version && echo "OK: passwordless bpftrace works"
