#!/usr/bin/env python3
"""Remote volume control for a PipeWire machine (e.g. Linux Mint 22.x) over SSH.

Keeps ONE long-lived ssh process open with a remote shell on the other end,
and sends wpctl commands down it. Reconnects automatically if the link drops.

Usage:  python3 remote_volume.py [user@host]

Features
  - Global keyboard shortcuts (configurable) to nudge volume and toggle mute
    regardless of which window has focus.
  - Minimises to a system tray icon on close; right-click it to show the
    window again or exit the app.
  - Remembers previously used hosts in a dropdown for quick reconnection.

Requirements
  local : Python 3 with Tkinter (Mint/Ubuntu: sudo apt install python3-tk),
          OpenSSH client, key-based login to the remote (no password prompt),
          pynput + pystray + Pillow (pip install pynput pystray pillow),
          a running X server (global hotkeys and the tray icon need one).
  remote: PipeWire + WirePlumber (wpctl), user logged in to the desktop.
"""

import json
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from pynput import keyboard
import pystray
from PIL import Image, ImageDraw

SINK = "@DEFAULT_AUDIO_SINK@"
SENTINEL = "__RV_DONE__"
POLL_SECONDS = 2        # how often to re-read the remote volume
RETRY_SECONDS = 5       # wait between reconnect attempts
VOLUME_STEP = 5          # percent per global-hotkey nudge
MAX_HOST_HISTORY = 15

CONFIG_DIR = Path.home() / ".config" / "remote_volume_over_ssh_gui"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Ctrl+Alt+Up/Down/M would collide with Cinnamon/GNOME's default
# workspace-switching shortcuts, whose window-manager-level key grab wins
# the race and silently swallows the keypress before pynput's listener
# sees it. A triple-modifier combo is far less likely to already be bound.
DEFAULT_HOTKEYS = {
    "volume_up": "<ctrl>+<alt>+<shift>+<up>",
    "volume_down": "<ctrl>+<alt>+<shift>+<down>",
    "mute_toggle": "<ctrl>+<alt>+<shift>+m",
}

# Runs on the remote: make sure the PipeWire socket can be found, then become
# a plain shell that reads commands from stdin (stderr folded into stdout).
REMOTE_INIT = (
    'export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"; '
    "exec sh 2>&1"
)

VOLUME_RE = re.compile(r"Volume:\s*([\d.]+)(\s*\[MUTED\])?")


def load_config():
    config = {"hosts": [], "last_host": "", "hotkeys": dict(DEFAULT_HOTKEYS)}
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return config
    config["hosts"] = [h for h in data.get("hosts", []) if isinstance(h, str) and h]
    config["last_host"] = data.get("last_host") or ""
    hotkeys = dict(DEFAULT_HOTKEYS)
    hotkeys.update(
        {k: v for k, v in data.get("hotkeys", {}).items() if k in DEFAULT_HOTKEYS and v}
    )
    config["hotkeys"] = hotkeys
    return config


def save_config(config):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(config, indent=2))
    except OSError:
        pass


def make_tray_image():
    """A small speaker glyph on a flat circle -- no external icon asset needed."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, size - 2, size - 2), fill=(51, 102, 204, 255))
    draw.rectangle((20, 26, 28, 38), fill=(255, 255, 255, 255))
    draw.polygon([(28, 26), (40, 16), (40, 48), (28, 38)], fill=(255, 255, 255, 255))
    return img


class Remote(threading.Thread):
    """Owns the persistent ssh process. All remote I/O happens on this thread."""

    def __init__(self, host, ui_queue):
        super().__init__(daemon=True)
        self.host = host
        self.ui = ui_queue
        self.cmds = queue.Queue()
        self.stopping = threading.Event()
        self.proc = None
        self.last_seq = 0   # sequence number of the last command actually applied

    # -- called from the GUI thread ---------------------------------------
    def set_volume(self, percent, seq):
        self.cmds.put(("vol", percent, seq))

    def set_mute(self, muted, seq):
        self.cmds.put(("mute", muted, seq))

    def stop(self):
        self.stopping.set()
        self._kill()

    # -- worker thread ------------------------------------------------------
    def run(self):
        while not self.stopping.is_set():
            try:
                self._status(f"Connecting to {self.host}…", False)
                self._connect()
                self._status(f"Connected to {self.host}", True)
                self._refresh()
                self._loop()
            except ConnectionError as exc:
                if self.stopping.is_set():
                    break
                detail = str(exc) or "connection closed"
                self._status(
                    f"Disconnected: {detail} (retrying in {RETRY_SECONDS}s)", False
                )
            finally:
                self._kill()
            self.stopping.wait(RETRY_SECONDS)

    def _connect(self):
        try:
            self.proc = subprocess.Popen(
                [
                    "ssh", "-T",
                    "-o", "BatchMode=yes",
                    "-o", "ConnectTimeout=10",
                    "-o", "ServerAliveInterval=15",
                    "-o", "ServerAliveCountMax=3",
                    self.host, REMOTE_INIT,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            raise ConnectionError("ssh client not found")
        self._run("true")   # blocks until the remote shell answers

    def _run(self, command):
        """Run one command remotely; return (exit_code, output)."""
        proc = self.proc
        try:
            proc.stdin.write(f"{command}; echo {SENTINEL}$?\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            raise ConnectionError("connection lost")
        lines = []
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith(SENTINEL):
                return int(line[len(SENTINEL):] or 1), "\n".join(lines)
            lines.append(line)
        # EOF: ssh exited. Whatever it printed is the reason.
        raise ConnectionError(" ".join(lines).strip())

    def _loop(self):
        while not self.stopping.is_set():
            try:
                items = [self.cmds.get(timeout=POLL_SECONDS)]
            except queue.Empty:
                self._refresh()
                continue
            # Drain the queue so a slider drag only sends its latest position.
            while True:
                try:
                    items.append(self.cmds.get_nowait())
                except queue.Empty:
                    break
            volume = None
            mute = None
            for kind, value, seq in items:
                if kind == "vol":
                    volume = value
                else:
                    mute = value
                self.last_seq = max(self.last_seq, seq)
            if mute is not None:
                self._run(f"wpctl set-mute {SINK} {1 if mute else 0}")
            if volume is not None:
                self._run(f"wpctl set-volume {SINK} {volume / 100:.2f}")
            self._refresh()

    def _refresh(self):
        code, out = self._run(f"wpctl get-volume {SINK}")
        match = VOLUME_RE.search(out)
        if code == 0 and match:
            percent = round(float(match.group(1)) * 100)
            self.ui.put(("state", percent, bool(match.group(2)), self.last_seq))
            self.ui.put(("status", f"Connected to {self.host}", True))
        else:
            self._status(f"Remote error: {out.strip() or 'wpctl failed'}", False)

    def _status(self, text, ok):
        self.ui.put(("status", text, ok))

    def _kill(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.kill()


class ShortcutsDialog(tk.Toplevel):
    FIELDS = [
        ("volume_up", "Volume up"),
        ("volume_down", "Volume down"),
        ("mute_toggle", "Toggle mute"),
    ]

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.title("Global Shortcuts")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        frame = ttk.Frame(self, padding=12)
        frame.grid()

        self.vars = {}
        for row, (key, label) in enumerate(self.FIELDS):
            ttk.Label(frame, text=f"{label}:").grid(row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar(value=app.config["hotkeys"].get(key, DEFAULT_HOTKEYS[key]))
            ttk.Entry(frame, textvariable=var, width=24).grid(row=row, column=1, padx=6)
            self.vars[key] = var

        ttk.Label(
            frame,
            text=(
                "Modifiers: <ctrl> <alt> <shift> <cmd>  e.g. <ctrl>+<alt>+<up>\n"
                "Avoid combos your desktop environment already binds (e.g.\n"
                "Cinnamon/GNOME's Ctrl+Alt+arrows for workspace switching) --\n"
                "its grab wins and the shortcut here will silently never fire."
            ),
            foreground="gray",
            justify="left",
        ).grid(row=len(self.FIELDS), column=0, columnspan=2, sticky="w", pady=(6, 10))

        buttons = ttk.Frame(frame)
        buttons.grid(row=len(self.FIELDS) + 1, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=0, padx=4)
        ttk.Button(buttons, text="Save", command=self.save).grid(row=0, column=1)

    def save(self):
        new_hotkeys = {key: var.get().strip() for key, var in self.vars.items()}
        try:
            self.app.apply_hotkeys(new_hotkeys)
        except Exception as exc:
            messagebox.showerror("Invalid shortcut", str(exc), parent=self)
            return
        self.destroy()


class App:
    def __init__(self, root, config, initial_host):
        self.root = root
        self.config = config
        self.remote = None
        self.ui_queue = queue.Queue()
        self.action_queue = queue.Queue()
        self.hotkey_listener = None
        self.tray_icon = None
        self.remote_volume = None     # last value reported by the remote
        self.request_seq = 0          # bumped on every command we send; lets
                                       # show_state ignore readings that predate it

        root.title("Remote Volume")
        root.resizable(False, False)
        frame = ttk.Frame(root, padding=12)
        frame.grid()

        ttk.Label(frame, text="Host:").grid(row=0, column=0, sticky="w")
        self.host_var = tk.StringVar(value=initial_host)
        self.host_combo = ttk.Combobox(
            frame, textvariable=self.host_var, values=self.config["hosts"], width=26
        )
        self.host_combo.grid(row=0, column=1, padx=6)
        self.host_combo.bind("<Return>", lambda _e: self.connect())
        self.host_combo.bind("<<ComboboxSelected>>", lambda _e: self.connect())
        ttk.Button(frame, text="Connect", command=self.connect).grid(row=0, column=2)

        self.volume_var = tk.DoubleVar(value=0)
        self.scale = ttk.Scale(
            frame, from_=0, to=100, variable=self.volume_var,
            length=300, command=self.on_slide,
        )
        self.scale.grid(row=1, column=0, columnspan=2, pady=(14, 4), sticky="we")
        self.percent_label = ttk.Label(frame, text="–", width=6, anchor="e")
        self.percent_label.grid(row=1, column=2, pady=(14, 4))

        self.mute_var = tk.BooleanVar(value=False)
        self.mute_box = ttk.Checkbutton(
            frame, text="Mute", variable=self.mute_var, command=self.on_mute
        )
        self.mute_box.grid(row=2, column=0, sticky="w")
        ttk.Button(frame, text="Shortcuts…", command=self.open_shortcuts_dialog).grid(
            row=2, column=2, sticky="e"
        )

        self.status = ttk.Label(frame, text="Not connected", foreground="gray",
                                wraplength=380, justify="left")
        self.status.grid(row=3, column=0, columnspan=3, sticky="w", pady=(12, 0))

        self._enable(False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.drain)

        self.init_tray()
        self.init_hotkeys()

        if initial_host:
            self.connect()

    # -- connections & history -----------------------------------------------
    def connect(self):
        host = self.host_var.get().strip()
        if not host:
            return
        if self.remote:
            self.remote.stop()
        self.remember_host(host)
        self.ui_queue = queue.Queue()   # drop messages from the old connection
        self.request_seq = 0            # fresh Remote starts its own seq at 0 too
        self._enable(False)
        self.remote = Remote(host, self.ui_queue)
        self.remote.start()

    def remember_host(self, host):
        hosts = [h for h in self.config["hosts"] if h != host]
        hosts.insert(0, host)
        self.config["hosts"] = hosts[:MAX_HOST_HISTORY]
        self.config["last_host"] = host
        save_config(self.config)
        self.host_combo.configure(values=self.config["hosts"])

    # -- volume & mute actions ------------------------------------------------
    def on_slide(self, _value):
        percent = round(self.volume_var.get())
        self.percent_label.config(text=f"{percent}%")
        if percent == self.remote_volume or not self.remote:
            return                      # programmatic update, nothing to send
        self.remote_volume = percent
        self.request_seq += 1
        self.remote.set_volume(percent, self.request_seq)

    def on_mute(self):
        if self.remote:
            self.request_seq += 1
            self.remote.set_mute(self.mute_var.get(), self.request_seq)

    def nudge_volume(self, delta):
        if not self.remote:
            return
        current = round(self.volume_var.get())
        self.volume_var.set(max(0, min(100, current + delta)))

    def toggle_mute(self):
        if not self.remote:
            return
        self.mute_var.set(not self.mute_var.get())
        self.on_mute()

    # -- global shortcuts -----------------------------------------------------
    def init_hotkeys(self):
        try:
            self.apply_hotkeys(self.config["hotkeys"])
        except Exception as exc:
            self.hotkey_listener = None
            print(f"Global shortcuts unavailable: {exc}", file=sys.stderr)
        else:
            combos = ", ".join(self.config["hotkeys"].values())
            print(
                f"Global shortcuts active: {combos}\n"
                "If a shortcut doesn't fire, your desktop environment likely "
                "already binds that combo (e.g. Cinnamon/GNOME bind "
                "Ctrl+Alt+Up/Down to workspace switching) and its window-manager "
                "grab wins before this listener sees the keypress -- check "
                "System Settings > Keyboard > Shortcuts, or pick a different "
                "combo via the Shortcuts… button."
            )

    def apply_hotkeys(self, hotkeys):
        parsed = {}
        for key, combo in hotkeys.items():
            try:
                parsed[key] = frozenset(keyboard.HotKey.parse(combo))
            except ValueError as exc:
                raise ValueError(f"{key.replace('_', ' ')}: {exc}") from exc
        if len({frozenset(v) for v in parsed.values()}) != len(parsed):
            raise ValueError("Shortcuts must be different from each other")

        mapping = {
            hotkeys["volume_up"]: lambda: self.action_queue.put(("vol_up",)),
            hotkeys["volume_down"]: lambda: self.action_queue.put(("vol_down",)),
            hotkeys["mute_toggle"]: lambda: self.action_queue.put(("mute_toggle",)),
        }
        listener = keyboard.GlobalHotKeys(mapping)
        listener.start()

        if self.hotkey_listener:
            self.hotkey_listener.stop()
        self.hotkey_listener = listener
        self.config["hotkeys"] = dict(hotkeys)
        save_config(self.config)

    def open_shortcuts_dialog(self):
        ShortcutsDialog(self.root, self)

    # -- system tray ------------------------------------------------------------
    def init_tray(self):
        try:
            icon = pystray.Icon(
                "remote_volume",
                make_tray_image(),
                "Remote Volume",
                menu=pystray.Menu(
                    pystray.MenuItem("Show", lambda icon, item: self.action_queue.put(("show",))),
                    pystray.MenuItem("Exit", lambda icon, item: self.action_queue.put(("exit",))),
                ),
            )
            threading.Thread(target=icon.run, daemon=True).start()
            self.tray_icon = icon
        except Exception as exc:
            self.tray_icon = None
            print(f"Tray icon unavailable: {exc}", file=sys.stderr)

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        if self.tray_icon:
            self.root.withdraw()
        else:
            self.quit_app()

    def quit_app(self):
        if self.remote:
            self.remote.stop()
        if self.hotkey_listener:
            self.hotkey_listener.stop()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.destroy()

    # -- updates from worker threads --------------------------------------------
    def drain(self):
        try:
            while True:
                message = self.ui_queue.get_nowait()
                if message[0] == "status":
                    _, text, ok = message
                    self.status.config(text=text,
                                       foreground="dark green" if ok else "firebrick")
                    self._enable(ok)
                elif message[0] == "state":
                    self.show_state(message[1], message[2], message[3])
        except queue.Empty:
            pass
        try:
            while True:
                self.handle_action(self.action_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def handle_action(self, action):
        kind = action[0]
        if kind == "vol_up":
            self.nudge_volume(VOLUME_STEP)
        elif kind == "vol_down":
            self.nudge_volume(-VOLUME_STEP)
        elif kind == "mute_toggle":
            self.toggle_mute()
        elif kind == "show":
            self.show_window()
        elif kind == "exit":
            self.quit_app()

    def show_state(self, percent, muted, seq):
        # Ignore readings that predate our latest command (e.g. an idle poll
        # that was already in flight when the user acted) rather than guessing
        # from elapsed time, which a slow link can outrace.
        if seq < self.request_seq:
            return
        self.remote_volume = min(percent, 100)
        self.volume_var.set(self.remote_volume)
        self.percent_label.config(text=f"{percent}%")
        self.mute_var.set(muted)

    def _enable(self, on):
        state = ["!disabled"] if on else ["disabled"]
        self.scale.state(state)
        self.mute_box.state(state)

    def close(self):
        self.quit_app()


def main():
    config = load_config()
    host = sys.argv[1] if len(sys.argv) > 1 else config.get("last_host", "")
    root = tk.Tk()
    App(root, config, host)
    root.mainloop()


if __name__ == "__main__":
    main()
