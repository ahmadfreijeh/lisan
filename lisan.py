#!/usr/bin/env python3
"""lisan: macOS keyboard helper for Arabic transliteration and polishing."""

import argparse
import sys

from pynput import keyboard
from pynput.keyboard import Key

from lisan_input import TextInjector, get_frontmost_app_name
from lisan_polish import SentencePolisher
from lisan_trans import Transliterator, extract_arabic, extract_arabic_candidates

TRANSLITERATE_MODE = "transliterate"
POLISH_MODE = "polish"

_injector = TextInjector()
_transliterator = Transliterator(_injector)
_polisher = SentencePolisher(_injector, get_frontmost_app_name)
_active_mode = TRANSLITERATE_MODE
_target_app: "str | None" = None
_pressed_modifiers = set()


def set_mode(mode: str) -> None:
    """Switch between Arabic transliteration and sentence polish."""
    if mode not in {TRANSLITERATE_MODE, POLISH_MODE}:
        raise ValueError(f"Unsupported lisan mode: {mode!r}")
    global _active_mode
    _active_mode = mode
    _transliterator.reset()
    _polisher.reset()
    print(f"[lisan] mode set to: {mode}")


def get_mode() -> str:
    """Return the active mode for the menu-bar UI."""
    return _active_mode


def set_swap_callback(callback) -> None:
    """Register a callback for transliteration candidates shown in the UI."""
    _transliterator.set_swap_callback(callback)


def set_polish_status_callback(callback) -> None:
    """Register a callback for the Polish loading indicator."""
    _polisher.set_status_callback(callback)


def select_candidate(index: int) -> None:
    """Replace the last transliterated word with candidate `index`."""
    _transliterator.select_candidate(index)


def set_target_app(app_name: "str | None") -> None:
    """Restrict lisan to a frontmost app name, or clear the restriction."""
    global _target_app
    _target_app = app_name or None
    print(f"[lisan] target app: {_target_app!r}" if _target_app else "[lisan] active for all apps")


def is_target_app_active() -> bool:
    """Return whether lisan is allowed to act in the focused application."""
    if not _target_app:
        return True
    frontmost = get_frontmost_app_name()
    return frontmost is None or _target_app.lower() in frontmost.lower()


def _is_modifier(key) -> bool:
    return key in {Key.cmd, Key.cmd_l, Key.cmd_r, Key.shift, Key.shift_l, Key.shift_r}


def _is_polish_shortcut(char: "str | None") -> bool:
    command = any(key in _pressed_modifiers for key in {Key.cmd, Key.cmd_l, Key.cmd_r})
    shift = any(key in _pressed_modifiers for key in {Key.shift, Key.shift_l, Key.shift_r})
    return char is not None and char.lower() == "r" and command and shift


def on_press(key) -> None:
    """Route global key presses to the selected feature."""
    try:
        if _injector.consume_synthetic_event():
            return
        if _is_modifier(key):
            _pressed_modifiers.add(key)
            return

        char = getattr(key, "char", None)
        if _active_mode == POLISH_MODE:
            shortcut = _is_polish_shortcut(char)
            if shortcut or not _pressed_modifiers:
                _polisher.handle_key(key, char, shortcut, is_target_app_active() if shortcut else True)
        else:
            _transliterator.handle_key(key, char, is_target_app_active() if key == Key.space else True)
    except Exception as exc:  # noqa: BLE001 - one event must not stop the listener
        print(f"[lisan] error handling key {key!r}: {exc}", file=sys.stderr)


def on_release(key) -> None:
    _pressed_modifiers.discard(key)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="lisan - Arabic transliteration and sentence polish.")
    parser.add_argument("-a", "--app", help="Only activate while this app is frontmost.")
    return parser.parse_args()


def start_lisan(app_filter: "str | None" = None) -> keyboard.Listener:
    """Start the non-blocking global keyboard listener."""
    set_target_app(app_filter)
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    print("[lisan] listener started")
    return listener


def stop_lisan(listener: keyboard.Listener) -> None:
    """Stop a listener created by `start_lisan`."""
    listener.stop()
    print("[lisan] listener stopped")


def main() -> None:
    args = parse_args()
    print("lisan running. Type a word + Space to transliterate it to Arabic.")
    print("Use the menu-bar app to enable Polish, then press Cmd+Shift+R.")
    print(f"Python executable: {sys.executable}")
    listener = start_lisan(args.app)
    try:
        listener.join()
    except KeyboardInterrupt:
        stop_lisan(listener)


if __name__ == "__main__":
    main()
