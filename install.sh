#!/usr/bin/env bash
# Install sysmon for current user (pipx preferred, venv fallback). Adds app menu entry.
set -euo pipefail
cd "$(dirname "$0")"
if command -v pipx >/dev/null; then
  pipx install --force .
else
  python3 -m venv "$HOME/.local/share/sysmon-venv"
  "$HOME/.local/share/sysmon-venv/bin/pip" install .
  mkdir -p "$HOME/.local/bin"
  ln -sf "$HOME/.local/share/sysmon-venv/bin/sysmon" "$HOME/.local/bin/sysmon"
fi
mkdir -p "$HOME/.local/share/applications"
cp data/sysmon.desktop "$HOME/.local/share/applications/"
echo "Installed. Run: sysmon   (ensure ~/.local/bin is in PATH)"
