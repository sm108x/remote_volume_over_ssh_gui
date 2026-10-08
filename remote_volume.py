#!/usr/bin/env python3
"""Remote volume control for a PipeWire machine (e.g. Linux Mint 22.x) over SSH.

Keeps ONE long-lived ssh process open with a remote shell on the other end,
and sends wpctl commands down it. Reconnects automatically if the link drops.

Usage:  python3 remote_volume.py [user@host]

Features
  - Global keyboard shortcuts (configurable) to nudge volume and toggle mute
    regardless of which window has focus.
  - Minimises to a system tray icon on close; right-click it to show the
    window again or exit the app, left-click or scroll over it to adjust
    volume directly.
  - Remembers previously used hosts in a dropdown for quick reconnection.
  - Optional autostart on login, starting with the window hidden by
    default; connection failures show an on-screen indicator (when hidden)
    and a desktop notification.

Requirements
  local : Python 3 with Tkinter (Mint/Ubuntu: sudo apt install python3-tk),
          OpenSSH client, key-based login to the remote (no password prompt),
          pynput + pystray + Pillow (pip install pynput pystray pillow),
          a running X server (global hotkeys and the tray icon need one),
          notify-send (libnotify-bin) for desktop notifications, optional.
  remote: PipeWire + WirePlumber (wpctl), user logged in to the desktop.
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from pynput import keyboard, mouse

# gtk is the most reliable backend against Cinnamon's legacy XEmbed tray
# ("System tray" applet) -- appindicator and the hand-rolled xorg backend
# were both unresponsive to clicks there. setdefault() so an explicit
# PYSTRAY_BACKEND still overrides this for testing other backends.
os.environ.setdefault("PYSTRAY_BACKEND", "gtk")
import pystray
from PIL import Image, ImageDraw

SINK = "@DEFAULT_AUDIO_SINK@"
SENTINEL = "__RV_DONE__"
POLL_SECONDS = 2        # how often to re-read the remote volume
RETRY_SECONDS = 5       # fast retry, once a connection has succeeded at least once
DEFAULT_RETRY_MINUTES = 2.0  # slow retry while it has never yet connected (e.g. at login)
VOLUME_STEP = 5          # percent per global-hotkey nudge
MAX_HOST_HISTORY = 15

CONFIG_DIR = Path.home() / ".config" / "remote_volume_over_ssh_gui"
CONFIG_FILE = CONFIG_DIR / "config.json"
AUTOSTART_DIR = Path.home() / ".config" / "autostart"
AUTOSTART_FILE = AUTOSTART_DIR / "remote-volume-over-ssh-gui.desktop"

# Any Ctrl+Alt(+Shift)+arrow combo collides with Cinnamon/GNOME's default
# workspace-switching ("switch-to-workspace-up/down") and window-moving
# ("move-to-workspace-up/down") shortcuts -- adding modifiers doesn't help,
# since both are bound by default. The window-manager's key grab wins the
# race and silently swallows the keypress before pynput's listener sees
# it. Letter/number keys aren't bound to anything by default, so they're
# used here instead of arrows.
DEFAULT_HOTKEYS = {
    "volume_up": "<ctrl>+<alt>+<shift>+u",
    "volume_down": "<ctrl>+<alt>+<shift>+d",
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
    config = {
        "hosts": [], "last_host": "", "hotkeys": dict(DEFAULT_HOTKEYS),
        "start_hidden": True, "retry_minutes": DEFAULT_RETRY_MINUTES,
    }
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
    if isinstance(data.get("start_hidden"), bool):
        config["start_hidden"] = data["start_hidden"]
    retry_minutes = data.get("retry_minutes")
    if isinstance(retry_minutes, (int, float)) and retry_minutes > 0:
        config["retry_minutes"] = retry_minutes
    return config


def save_config(config):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(config, indent=2))
    except OSError:
        pass


def _desktop_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def is_autostart_enabled():
    return AUTOSTART_FILE.exists()


def set_autostart(enabled):
    """Writes/removes an XDG autostart .desktop entry (no root needed).

    The file's existence is the single source of truth -- not mirrored into
    config.json -- so it can't drift out of sync with what's actually
    registered, e.g. if removed by hand outside the app.
    """
    if not enabled:
        try:
            AUTOSTART_FILE.unlink()
        except FileNotFoundError:
            pass
        return
    AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    exec_line = f"{_desktop_quote(sys.executable)} {_desktop_quote(script)}"
    AUTOSTART_FILE.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Remote Volume\n"
        f"Exec={exec_line}\n"
        "X-GNOME-Autostart-enabled=true\n"
        "Terminal=false\n"
    )


def make_tray_image():
    """A white speaker glyph on a flat, fully opaque square.

    Fully opaque on purpose: legacy XEmbed tray hosts (e.g. Cinnamon's
    "System tray" applet) often don't composite alpha at all and render any
    transparent pixel as solid black, turning a nicer transparent icon into
    an unrecognisable black square.
    """
    size = 64
    img = Image.new("RGBA", (size, size), (51, 102, 204, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((20, 26, 28, 38), fill=(255, 255, 255, 255))
    draw.polygon([(28, 26), (40, 16), (40, 48), (28, 38)], fill=(255, 255, 255, 255))
    return img


class Remote(threading.Thread):
    """Owns the persistent ssh process. All remote I/O happens on this thread."""

    def __init__(self, host, ui_queue, slow_retry_seconds=RETRY_SECONDS):
        super().__init__(daemon=True)
        self.host = host
        self.ui = ui_queue
        self.cmds = queue.Queue()
        self.stopping = threading.Event()
        self.proc = None
        self.last_seq = 0   # sequence number of the last command actually applied
        self.slow_retry_seconds = slow_retry_seconds
        self.ever_connected = False

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
            wait = RETRY_SECONDS
            try:
                self._status(f"Connecting to {self.host}…", False)
                self._connect()
                self.ever_connected = True
                self._status(f"Connected to {self.host}", True)
                self._refresh()
                self._loop()
            except ConnectionError as exc:
                if self.stopping.is_set():
                    break
                if not self.ever_connected:
                    # Hasn't connected even once yet -- likely just after
                    # login, before the remote machine or its desktop
                    # session is up. Don't hammer it every few seconds.
                    wait = self.slow_retry_seconds
                detail = str(exc) or "connection closed"
                self._status(
                    f"Disconnected: {detail} (retrying in {wait:.0f}s)", False
                )
            finally:
                self._kill()
            self.stopping.wait(wait)

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
                "Modifiers: <ctrl> <alt> <shift> <cmd>  e.g. <ctrl>+<alt>+<shift>+u\n"
                "Avoid arrow keys: Cinnamon/GNOME bind every Ctrl+Alt(+Shift)+arrow\n"
                "combo by default (workspace switching / moving windows between\n"
                "workspaces) -- that grab wins and the shortcut here silently\n"
                "never fires. Letter/number keys are unclaimed and safe to use."
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


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.title("Settings")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        frame = ttk.Frame(self, padding=12)
        frame.grid()

        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        ttk.Checkbutton(
            frame, text="Start automatically on login", variable=self.autostart_var
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        self.start_hidden_var = tk.BooleanVar(
            value=app.config.get("start_hidden", True)
        )
        ttk.Checkbutton(
            frame, text="Start with the main window hidden",
            variable=self.start_hidden_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        retry_row = ttk.Frame(frame)
        retry_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(retry_row, text="Retry every").grid(row=0, column=0)
        self.retry_var = tk.StringVar(
            value=str(app.config.get("retry_minutes", DEFAULT_RETRY_MINUTES))
        )
        ttk.Entry(retry_row, textvariable=self.retry_var, width=6).grid(
            row=0, column=1, padx=4
        )
        ttk.Label(retry_row, text="minutes if never yet connected").grid(
            row=0, column=2
        )

        ttk.Label(
            frame,
            text=(
                "Once connected at least once, reconnects after a drop stay\n"
                "fast (5s). The interval above only applies before that first\n"
                "success -- e.g. right after login, before the remote machine\n"
                "or its desktop session is up."
            ),
            foreground="gray",
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 10))

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=0, padx=4)
        ttk.Button(buttons, text="Save", command=self.save).grid(row=0, column=1)

    def save(self):
        try:
            retry_minutes = float(self.retry_var.get())
            if retry_minutes <= 0:
                raise ValueError("must be greater than 0")
        except ValueError as exc:
            messagebox.showerror("Invalid value", f"Retry interval: {exc}", parent=self)
            return
        try:
            set_autostart(self.autostart_var.get())
        except OSError as exc:
            messagebox.showerror("Autostart", str(exc), parent=self)
            return
        self.app.apply_settings(retry_minutes, self.start_hidden_var.get())
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
        self.volume_popup = None
        self.volume_popup_trace = None
        self.popup_click_listener = None
        self.volume_osd = None
        self.volume_osd_label = None
        self.volume_osd_hide_job = None
        self.remote_volume = None     # last value reported by the remote
        self.request_seq = 0          # bumped on every command we send; lets
                                       # show_state ignore readings that predate it
        self.last_status_ok = None    # None = unknown yet; edge-triggers failure feedback

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
        ttk.Button(frame, text="Settings…", command=self.open_settings_dialog).grid(
            row=2, column=1
        )
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

        if self.config.get("start_hidden", True):
            self.root.withdraw()

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
        self.last_status_ok = None      # fresh connection, re-arm failure feedback
        self._enable(False)
        slow_retry = self.config.get("retry_minutes", DEFAULT_RETRY_MINUTES) * 60
        self.remote = Remote(host, self.ui_queue, slow_retry)
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
        self.send_volume(percent)

    def send_volume(self, percent):
        # Called both from the Scale's -command (user drags) and directly
        # from nudge_volume (hotkeys) -- a Scale's -command is not guaranteed
        # to fire from a bare external var.set(), so hotkeys can't rely on it.
        if percent == self.remote_volume or not self.remote:
            return
        self.remote_volume = percent
        self.request_seq += 1
        self.remote.set_volume(percent, self.request_seq)

    def on_mute(self):
        self.send_mute(self.mute_var.get())

    def send_mute(self, muted):
        if self.remote:
            self.request_seq += 1
            self.remote.set_mute(muted, self.request_seq)

    def nudge_volume(self, delta):
        if not self.remote:
            return
        current = round(self.volume_var.get())
        percent = max(0, min(100, current + delta))
        self.volume_var.set(percent)
        self.percent_label.config(text=f"{percent}%")
        self.send_volume(percent)
        self.show_volume_osd()

    def toggle_mute(self):
        if not self.remote:
            return
        muted = not self.mute_var.get()
        self.mute_var.set(muted)
        self.send_mute(muted)
        self.show_volume_osd()

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
                "already binds that combo and its window-manager grab wins "
                "before this listener sees the keypress. Arrow keys are "
                "especially prone to this on Cinnamon/GNOME -- Ctrl+Alt+arrow "
                "and Ctrl+Alt+Shift+arrow are both bound by default (workspace "
                "switching and moving a window between workspaces), so prefer "
                "letter/number keys. Check System Settings > Keyboard > "
                "Shortcuts, or pick a different combo via the Shortcuts… "
                "button."
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

    def open_settings_dialog(self):
        SettingsDialog(self.root, self)

    def apply_settings(self, retry_minutes, start_hidden):
        self.config["retry_minutes"] = retry_minutes
        self.config["start_hidden"] = start_hidden
        save_config(self.config)
        if self.remote:
            self.remote.slow_retry_seconds = retry_minutes * 60

    # -- system tray ------------------------------------------------------------
    def init_tray(self):
        try:
            icon = pystray.Icon(
                "remote_volume",
                make_tray_image(),
                "Remote Volume",
                menu=pystray.Menu(
                    pystray.MenuItem(
                        "Volume",
                        lambda icon, item: self.action_queue.put(("popup_volume",)),
                        default=True,
                        visible=False,
                    ),
                    pystray.MenuItem("Show", lambda icon, item: self.action_queue.put(("show",))),
                    pystray.MenuItem("Exit", lambda icon, item: self.action_queue.put(("exit",))),
                ),
            )
            threading.Thread(target=icon.run, daemon=True).start()
            self.tray_icon = icon
            print(
                f"Tray icon backend: {type(icon).__module__}.{type(icon).__name__}\n"
                "If you don't see it, this backend's icon has nothing to render "
                "into on this desktop -- on Cinnamon that usually means no "
                "tray/status applet is on the panel (right-click the panel > "
                "Applets > add 'XApp Status Applet' or similar), or the "
                "StatusNotifierItem watcher service isn't running (try: sudo "
                "apt install ayatana-indicator-application). You can also force "
                "a different backend to test, e.g.: "
                "PYSTRAY_BACKEND=xorg python3 remote_volume.py "
                "(other values: appindicator, gtk)."
            )
            if self._bind_tray_scroll(icon):
                print("Scroll over the tray icon to adjust volume.")
            else:
                print(
                    "Scroll-to-adjust-volume needs the gtk tray backend; not "
                    "available on this one."
                )
        except Exception as exc:
            self.tray_icon = None
            print(f"Tray icon unavailable: {exc}", file=sys.stderr)

    def _bind_tray_scroll(self, icon):
        # pystray has no cross-backend scroll API; gtk's underlying
        # Gtk.StatusIcon supports it directly, so reach into it when present.
        status_icon = getattr(icon, "_status_icon", None)
        if status_icon is None:
            return False
        try:
            from gi.repository import Gdk
        except ImportError:
            return False

        def on_scroll(_status_icon, event):
            if event.direction == Gdk.ScrollDirection.UP:
                self.action_queue.put(("vol_up",))
            elif event.direction == Gdk.ScrollDirection.DOWN:
                self.action_queue.put(("vol_down",))
            elif event.direction == Gdk.ScrollDirection.SMOOTH:
                if event.delta_y < 0:
                    self.action_queue.put(("vol_up",))
                elif event.delta_y > 0:
                    self.action_queue.put(("vol_down",))

        status_icon.connect("scroll-event", on_scroll)
        return True

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def toggle_volume_popup(self):
        if not self.remote:
            return
        if self.volume_popup:
            self.close_volume_popup()
            return

        popup = tk.Toplevel(self.root)
        self.volume_popup = popup
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        frame = ttk.Frame(popup, padding=10, relief="raised", borderwidth=1)
        frame.grid()

        percent_var = tk.StringVar(value=f"{round(self.volume_var.get())}%")
        self.volume_popup_trace = self.volume_var.trace_add(
            "write", lambda *_a: percent_var.set(f"{round(self.volume_var.get())}%")
        )
        ttk.Label(frame, textvariable=percent_var).grid(row=0, column=0, pady=(0, 4))
        ttk.Scale(
            frame, from_=100, to=0, orient="vertical", variable=self.volume_var,
            length=120, command=self.on_slide,
        ).grid(row=1, column=0)
        ttk.Checkbutton(
            frame, text="Mute", variable=self.mute_var, command=self.on_mute
        ).grid(row=2, column=0, pady=(6, 0))

        popup.bind("<FocusOut>", lambda _e: self.close_volume_popup())
        popup.bind("<Escape>", lambda _e: self.close_volume_popup())
        self._position_near_pointer(popup, gap=8)

        popup.lift()
        popup.update()          # ensure the window is actually mapped first --
        popup.focus_force()     # override-redirect windows often won't take
        # focus otherwise, so <FocusOut> alone can miss it. Delay the first
        # check: focus transfer is asynchronous and may not have landed yet.
        self.root.after(200, self._poll_popup_focus)

        # Focus alone isn't enough either: clicking a Cinnamon panel applet
        # doesn't necessarily take keyboard focus away from an
        # overrideredirect window, so that click wouldn't trip the poll
        # above. Passively watch for ANY click anywhere (via pynput, the
        # same mechanism the global hotkeys already use) and close if it
        # landed outside the popup -- this doesn't swallow the click, it
        # still reaches its real target too.
        self.popup_click_listener = mouse.Listener(on_click=self._on_global_click)
        self.popup_click_listener.start()

    def _on_global_click(self, x, y, button, pressed):
        if pressed:
            self.action_queue.put(("popup_click_check", x, y))

    def _check_popup_click(self, x, y):
        popup = self.volume_popup
        if not popup:
            return
        left, top = popup.winfo_rootx(), popup.winfo_rooty()
        right, bottom = left + popup.winfo_width(), top + popup.winfo_height()
        if not (left <= x <= right and top <= y <= bottom):
            self.close_volume_popup()

    def _poll_popup_focus(self):
        if not self.volume_popup:
            return
        focused = self.root.focus_get()
        if focused is None or not str(focused).startswith(str(self.volume_popup)):
            self.close_volume_popup()
            return
        self.root.after(200, self._poll_popup_focus)

    def close_volume_popup(self):
        if not self.volume_popup:
            return
        if self.popup_click_listener:
            self.popup_click_listener.stop()
            self.popup_click_listener = None
        self.volume_var.trace_remove("write", self.volume_popup_trace)
        self.volume_popup.destroy()
        self.volume_popup = None
        self.volume_popup_trace = None

    def _position_near_pointer(self, window, gap):
        window.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        pointer_x = self.root.winfo_pointerx()
        pointer_y = self.root.winfo_pointery()
        # Push toward screen center, not just off the icon's edge: a tray
        # near the top gets the window below it, and vice versa.
        if pointer_y < screen_h / 2:
            y = pointer_y + gap
        else:
            y = pointer_y - window.winfo_height() - gap
        x = pointer_x - window.winfo_width() // 2
        x = max(0, min(x, screen_w - window.winfo_width()))
        y = max(0, min(y, screen_h - window.winfo_height()))
        window.geometry(f"+{x}+{y}")

    def show_volume_osd(self):
        # Brief, auto-dismissing indicator for scroll/hotkey-triggered
        # changes, so there's feedback even when the main window is hidden.
        percent = round(self.volume_var.get())
        text = f"{percent}%" + ("  (muted)" if self.mute_var.get() else "")
        self._show_osd(text, duration_ms=1200)

    def show_connection_osd(self, text):
        # Same mechanism, for connection failures -- only called when the
        # main window is hidden, so there's still *some* visible feedback.
        self._show_osd(text, duration_ms=4000)

    def _show_osd(self, text, duration_ms):
        if self.volume_osd is None:
            osd = tk.Toplevel(self.root)
            osd.overrideredirect(True)
            osd.attributes("-topmost", True)
            frame = ttk.Frame(osd, padding=(14, 6), relief="raised", borderwidth=1)
            frame.grid()
            self.volume_osd_label = ttk.Label(
                frame, font=("TkDefaultFont", 12, "bold"),
                wraplength=260, justify="center",
            )
            self.volume_osd_label.grid()
            self.volume_osd = osd

        self.volume_osd_label.config(text=text)
        self.volume_osd.deiconify()
        self._position_near_pointer(self.volume_osd, gap=8)
        self.volume_osd.lift()

        if self.volume_osd_hide_job:
            self.root.after_cancel(self.volume_osd_hide_job)
        self.volume_osd_hide_job = self.root.after(duration_ms, self._hide_volume_osd)

    def _hide_volume_osd(self):
        if self.volume_osd:
            self.volume_osd.withdraw()
        self.volume_osd_hide_job = None

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
        if self.popup_click_listener:
            self.popup_click_listener.stop()
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
                    self.handle_status_change(text, ok)
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
        elif kind == "popup_volume":
            self.toggle_volume_popup()
        elif kind == "popup_click_check":
            self._check_popup_click(action[1], action[2])

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

    def handle_status_change(self, text, ok):
        # "Connecting to ..." is transient (sent at the start of every retry,
        # successful or not) and ok=False only because it isn't a success
        # yet -- not itself a failure, so it must not trip the edge below.
        if not ok and text.startswith("Connecting to"):
            return
        newly_failed = (not ok) and (self.last_status_ok is not False)
        self.last_status_ok = ok
        if newly_failed:
            if not self.root.winfo_viewable():
                self.show_connection_osd(text)
            self.notify_connection_issue(text)

    def notify_connection_issue(self, text):
        # Off the GUI thread: a hung notification daemon/D-Bus call must not
        # freeze the window, matching why all SSH I/O already runs in Remote
        # rather than here.
        threading.Thread(target=self._send_notification, args=(text,), daemon=True).start()

    def _send_notification(self, text):
        try:
            subprocess.run(
                ["notify-send", "-a", "Remote Volume", "Remote Volume", text],
                check=False, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"Notification failed: {exc}", file=sys.stderr)

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
