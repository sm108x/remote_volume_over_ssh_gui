#!/usr/bin/env python3
"""Remote volume control for a PipeWire machine (e.g. Linux Mint 22.x) over SSH.

Keeps ONE long-lived ssh process open with a remote shell on the other end,
and sends wpctl commands down it. Reconnects automatically if the link drops.

Usage:  python3 remote_volume.py [user@host]

Requirements
  local : Python 3 with Tkinter (Mint/Ubuntu: sudo apt install python3-tk),
          OpenSSH client, key-based login to the remote (no password prompt).
  remote: PipeWire + WirePlumber (wpctl), user logged in to the desktop.
"""

import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

SINK = "@DEFAULT_AUDIO_SINK@"
SENTINEL = "__RV_DONE__"
POLL_SECONDS = 2        # how often to re-read the remote volume
RETRY_SECONDS = 5       # wait between reconnect attempts
HOST_FILE = Path.home() / ".remote_volume_host"

# Runs on the remote: make sure the PipeWire socket can be found, then become
# a plain shell that reads commands from stdin (stderr folded into stdout).
REMOTE_INIT = (
    'export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"; '
    "exec sh 2>&1"
)

VOLUME_RE = re.compile(r"Volume:\s*([\d.]+)(\s*\[MUTED\])?")


class Remote(threading.Thread):
    """Owns the persistent ssh process. All remote I/O happens on this thread."""

    def __init__(self, host, ui_queue):
        super().__init__(daemon=True)
        self.host = host
        self.ui = ui_queue
        self.cmds = queue.Queue()
        self.stopping = threading.Event()
        self.proc = None

    # -- called from the GUI thread ---------------------------------------
    def set_volume(self, percent):
        self.cmds.put(("vol", percent))

    def set_mute(self, muted):
        self.cmds.put(("mute", muted))

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
            for kind, value in items:
                if kind == "vol":
                    volume = value
                else:
                    mute = value
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
            self.ui.put(("state", percent, bool(match.group(2))))
            self.ui.put(("status", f"Connected to {self.host}", True))
        else:
            self._status(f"Remote error: {out.strip() or 'wpctl failed'}", False)

    def _status(self, text, ok):
        self.ui.put(("status", text, ok))

    def _kill(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.kill()


class App:
    def __init__(self, root, host):
        self.root = root
        self.remote = None
        self.ui_queue = queue.Queue()
        self.remote_volume = None     # last value reported by the remote
        self.dragging = False
        self.last_user_change = 0.0

        root.title("Remote Volume")
        root.resizable(False, False)
        frame = ttk.Frame(root, padding=12)
        frame.grid()

        ttk.Label(frame, text="Host:").grid(row=0, column=0, sticky="w")
        self.host_var = tk.StringVar(value=host)
        entry = ttk.Entry(frame, textvariable=self.host_var, width=28)
        entry.grid(row=0, column=1, padx=6)
        entry.bind("<Return>", lambda _e: self.connect())
        ttk.Button(frame, text="Connect", command=self.connect).grid(row=0, column=2)

        self.volume_var = tk.DoubleVar(value=0)
        self.scale = ttk.Scale(
            frame, from_=0, to=100, variable=self.volume_var,
            length=300, command=self.on_slide,
        )
        self.scale.grid(row=1, column=0, columnspan=2, pady=(14, 4), sticky="we")
        self.scale.bind("<ButtonPress-1>", lambda _e: self._drag(True))
        self.scale.bind("<ButtonRelease-1>", lambda _e: self._drag(False))
        self.percent_label = ttk.Label(frame, text="–", width=6, anchor="e")
        self.percent_label.grid(row=1, column=2, pady=(14, 4))

        self.mute_var = tk.BooleanVar(value=False)
        self.mute_box = ttk.Checkbutton(
            frame, text="Mute", variable=self.mute_var, command=self.on_mute
        )
        self.mute_box.grid(row=2, column=0, columnspan=2, sticky="w")

        self.status = ttk.Label(frame, text="Not connected", foreground="gray",
                                wraplength=380, justify="left")
        self.status.grid(row=3, column=0, columnspan=3, sticky="w", pady=(12, 0))

        self._enable(False)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.drain)
        if host:
            self.connect()

    # -- actions ------------------------------------------------------------
    def connect(self):
        host = self.host_var.get().strip()
        if not host:
            return
        if self.remote:
            self.remote.stop()
        try:
            HOST_FILE.write_text(host)
        except OSError:
            pass
        self.ui_queue = queue.Queue()   # drop messages from the old connection
        self._enable(False)
        self.remote = Remote(host, self.ui_queue)
        self.remote.start()

    def on_slide(self, _value):
        percent = round(self.volume_var.get())
        self.percent_label.config(text=f"{percent}%")
        if percent == self.remote_volume or not self.remote:
            return                      # programmatic update, nothing to send
        self.remote_volume = percent
        self.last_user_change = time.monotonic()
        self.remote.set_volume(percent)

    def on_mute(self):
        if self.remote:
            self.last_user_change = time.monotonic()
            self.remote.set_mute(self.mute_var.get())

    def _drag(self, active):
        self.dragging = active
        self.last_user_change = time.monotonic()

    # -- updates from the worker thread --------------------------------------
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
                    self.show_state(message[1], message[2])
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def show_state(self, percent, muted):
        # Don't fight the user: ignore remote readings mid-adjustment.
        if self.dragging or time.monotonic() - self.last_user_change < 1.0:
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
        if self.remote:
            self.remote.stop()
        self.root.destroy()


def main():
    if len(sys.argv) > 1:
        host = sys.argv[1]
    else:
        try:
            host = HOST_FILE.read_text().strip()
        except OSError:
            host = ""
    root = tk.Tk()
    App(root, host)
    root.mainloop()


if __name__ == "__main__":
    main()
