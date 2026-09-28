"""macOS keyboard injection and frontmost-app helpers."""

import subprocess
import sys
import threading

from pynput.keyboard import Controller, Key


class TextInjector:
    """Inject text while allowing the listener to ignore its own events."""

    def __init__(self):
        self._controller = Controller()
        self._lock = threading.Lock()
        self._pending_events = 0

    def consume_synthetic_event(self) -> bool:
        with self._lock:
            if not self._pending_events:
                return False
            self._pending_events -= 1
            return True

    def backspaces(self, count: int) -> None:
        if count <= 0:
            return
        with self._lock:
            self._pending_events += count
        for _ in range(count):
            self._controller.press(Key.backspace)
            self._controller.release(Key.backspace)

    def text(self, value: str) -> None:
        if not value:
            return
        with self._lock:
            self._pending_events += len(value)
        self._controller.type(value)


def get_frontmost_app_name() -> "str | None":
    """Return the frontmost macOS app name, or None when unavailable."""
    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to get name of first application '
                "process whose frontmost is true",
            ],
            capture_output=True,
            text=True,
            timeout=1,
        )
        return result.stdout.strip() or None
    except Exception as exc:  # noqa: BLE001 - listener must stay alive
        print(f"[lisan] could not determine frontmost app: {exc}", file=sys.stderr)
        return None
