#!/usr/bin/env bash
# Interactive dependency installer for remote_volume_over_ssh_gui.
#
# Debian/Ubuntu/Mint block system-wide `pip install` by default
# ("externally-managed-environment"), so this offers a few ways around it.
set -euo pipefail
cd "$(dirname "$0")"

echo "Install Python dependencies (pynput, pystray, pillow) how?"
echo "  1) Virtual environment in .venv (recommended)"
echo "  2) System Python, forcing pip with --break-system-packages"
echo "  3) apt packages where available, pip for the rest"
echo "  4) Cancel"
read -rp "Choice [1-4]: " choice

case "$choice" in
  1)
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
    echo "Done. Run: .venv/bin/python remote_volume.py"
    ;;
  2)
    pip install --break-system-packages -r requirements.txt
    echo "Done. Run: python3 remote_volume.py"
    ;;
  3)
    sudo apt install -y python3-pil python3-pynput
    pip install --break-system-packages pystray 2>/dev/null || pip install pystray
    echo "Done. Run: python3 remote_volume.py"
    ;;
  4)
    echo "Cancelled."
    exit 0
    ;;
  *)
    echo "Invalid choice." >&2
    exit 1
    ;;
esac
