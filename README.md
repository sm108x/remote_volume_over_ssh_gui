# remote_volume_over_ssh_gui

GUI for controlling audio volume on a remote PipeWire machine over SSH.

## Features

- Persistent SSH connection with automatic reconnect.
- Global keyboard shortcuts (configurable) to nudge volume up/down and
  toggle mute, regardless of which window has focus.
- Minimises to a system tray icon on close; right-click the tray icon to
  show the window again or exit.
- Remembers previously used hosts in a dropdown for quick reconnection.

## Requirements

- Local: Python 3 with Tkinter (`sudo apt install python3-tk`), the OpenSSH
  client, and key-based login to the remote host (no password prompt).
- Local: a running X server — global shortcuts and the tray icon both need
  one (this does not work headless or under Wayland-only sessions).
- Remote: PipeWire + WirePlumber (`wpctl`), with the target user logged in
  to a desktop session.

Install the Python dependencies by running `./install.sh` -- it offers a
choice of apt packages (recommended: prebuilt, nothing to compile), a
virtual environment built on top of those apt packages, or plain
`pip install -r requirements.txt` forced with `--break-system-packages`.
Plain `pip install` on Debian/Ubuntu/Mint either refuses outright
("externally-managed-environment") or ends up compiling `evdev`, which
fails unless `python3-dev` and `build-essential` are installed;
`./install.sh` handles both cases for you.

## Usage

```sh
python3 remote_volume.py [user@host]
```

If no host is given, the last-used host is loaded automatically. Previously
used hosts are kept in a dropdown; config and connection history are stored
in `~/.config/remote_volume_over_ssh_gui/config.json`.

Click "Shortcuts…" in the window to change the global hotkeys (defaults:
`Ctrl+Alt+Shift+U`, `Ctrl+Alt+Shift+D`, `Ctrl+Alt+Shift+M` for volume
up/down/mute). Avoid arrow keys for these: Cinnamon/GNOME bind every
`Ctrl+Alt(+Shift)+arrow` combo by default (workspace switching and moving
windows between workspaces), and that window-manager grab silently wins
over this app's listener.

### Tray icon not visible

The app prints which `pystray` backend it picked on startup. If the icon
doesn't show up anywhere, that backend likely has nothing on your desktop
to render into -- no error is raised, it's just invisible. On Cinnamon:

- Make sure a tray/status applet is actually on the panel: right-click the
  panel > Applets > add "XApp Status Applet" (or similar). Ubuntu's Cinnamon
  build doesn't always ship one by default the way Linux Mint does.
- Install the StatusNotifierItem watcher service if it's missing:
  `sudo apt install ayatana-indicator-application`.
- Force a different backend to test: `PYSTRAY_BACKEND=xorg python3
  remote_volume.py` (other values: `appindicator`, `gtk`).
