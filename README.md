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

Install the Python dependencies:

```sh
pip install -r requirements.txt
```

## Usage

```sh
python3 remote_volume.py [user@host]
```

If no host is given, the last-used host is loaded automatically. Previously
used hosts are kept in a dropdown; config and connection history are stored
in `~/.config/remote_volume_over_ssh_gui/config.json`.

Click "Shortcuts…" in the window to change the global hotkeys (defaults:
`Ctrl+Alt+Up`, `Ctrl+Alt+Down`, `Ctrl+Alt+M` for volume up/down/mute).
