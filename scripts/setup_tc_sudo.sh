#!/usr/bin/env bash
# One-time setup so the fault harness can run `sudo -n tc ...` without a password.
# !!! ROOT: writes /etc/sudoers.d/crosstrace-tc. Dev WSL/VM only. !!!
# Limitation: this lets your user run ANY `tc` command as root without a password.
# Remove with: sudo rm /etc/sudoers.d/crosstrace-tc
set -eu
TC="$(command -v tc)"
RULE="$USER ALL=(root) NOPASSWD: $TC"
echo "$RULE" | sudo tee /etc/sudoers.d/crosstrace-tc >/dev/null
sudo chmod 440 /etc/sudoers.d/crosstrace-tc
sudo visudo -cf /etc/sudoers.d/crosstrace-tc
echo "installed: $RULE"
sudo -k          # forget cached credentials so the test below is honest
sudo -n tc qdisc show dev lo && echo "OK: passwordless tc works"
