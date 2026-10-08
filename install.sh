#!/usr/bin/env bash
# Interactive dependency installer for remote_volume_over_ssh_gui.
#
# Debian/Ubuntu/Mint block system-wide `pip install` by default
# ("externally-managed-environment"), and pynput's Linux build pulls in
# `evdev`, which compiles a C extension against Python.h -- it fails with
# "fatal error: Python.h: No such file or directory" unless python3-dev is
# installed. apt has prebuilt packages for everything we need, so that's
# the default and avoids compiling altogether.
set -euo pipefail
cd "$(dirname "$0")"

APT_PKGS=(python3-pynput python3-pystray python3-pil python3-xlib python3-evdev libnotify-bin)

echo "Install Python dependencies (pynput, pystray, pillow) how?"
echo "  1) apt packages (recommended -- prebuilt, nothing to compile)"
echo "  2) Virtual environment in .venv, built on top of the apt packages"
echo "  3) System Python via pip, forcing --break-system-packages"
echo "  4) Cancel"
read -rp "Choice [1-4]: " choice

case "$choice" in
  1)
    sudo apt install -y "${APT_PKGS[@]}"
    echo "Done. Run: python3 remote_volume.py"
    ;;
  2)
    sudo apt install -y "${APT_PKGS[@]}"
    python3 -m venv --system-site-packages .venv
    echo "Done. Run: .venv/bin/python remote_volume.py"
    ;;
  3)
    sudo apt install -y python3-dev build-essential libnotify-bin
    pip install --break-system-packages -r requirements.txt
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
