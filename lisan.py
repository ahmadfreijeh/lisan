#!/usr/bin/env python3
"""
lisan.py

lisan (لسان, "tongue/language") - a lightweight macOS background script that
globally monitors keyboard input,
buffers typed Latin-alphabet words, and on Space transliterates the word to
Arabic using Yamli's transliteration API, then swaps the typed word for the
Arabic result directly in the currently focused text field (no clipboard).

REQUIRED macOS PERMISSIONS
---------------------------
This script uses pynput, which relies on macOS Accessibility APIs to both
listen to global keystrokes and inject synthetic keystrokes. The process
running this script (e.g. Terminal, iTerm, or python3 itself) must be granted:

    System Settings > Privacy & Security > Accessibility
    System Settings > Privacy & Security > Input Monitoring

If you don't see a permission prompt, add the app manually under both panes
and restart the script.

USAGE
-----
    pip3 install pynput requests
    python3 lisan.py
    python3 lisan.py --app Notes   # only active while "Notes" is frontmost

Type a word (e.g. "marhaba") in any focused text field, then press Space.
The script will attempt to erase the Latin word and replace it with the
first Arabic transliteration candidate returned by Yamli.

If --app/-a is omitted, lisan is active for every app (previous behavior).
If provided, lisan only acts when the frontmost app's name contains that
string (case-insensitive), e.g. --app notes matches "Notes".

Stop the script with Ctrl+C in the terminal it's running in.

NOTES
-----
- TLS certificate verification is intentionally disabled for the Yamli
  request per project requirements (Yamli's certs are sometimes outdated).
  The corresponding urllib3 InsecureRequestWarning is suppressed.
- Password fields and other "secure input" contexts on macOS block synthetic
  keystrokes system-wide; this is a macOS-level protection this script cannot
  (and should not attempt to) bypass.
"""

import argparse
import json
import re
import subprocess
import sys
import threading

import requests
import urllib3
from pynput import keyboard
from pynput.keyboard import Key, Controller

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Yamli's real transliteration endpoint. This mirrors the endpoint used by
# Yamli's own JS widget (confirmed via the public zaini/yamli-api reference
# client), rather than a placeholder URL, so it actually returns results.
YAMLI_URL_TEMPLATE = (
    "https://api.yamli.com/transliterate.ashx"
    "?word={word}"
    "&tool=api"
    "&account_id=000006"
    "&prot=https"
    "&hostname=AliMZaini"
    "&path=yamli-api"
    "&build=5515"
)

REQUEST_TIMEOUT_SECONDS = 5

# Suppress the expected "InsecureRequestWarning" noise from verify=False.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_buffer_lock = threading.Lock()
_buffer: list = []

_kb_controller = Controller()

# Counts synthetic key events we are about to inject (backspaces + the
# replacement text + trailing space) so the listener can ignore them instead
# of treating them as real user input. Without this, injected keystrokes
# (including Arabic letters, which Python's str.isalpha() treats as alpha)
# feed back into the buffer/listener and can re-trigger process_word,
# causing a feedback loop -- especially noticeable when typing fast enough
# that swaps overlap.
_suppress_lock = threading.Lock()
_suppress_count = 0

# If set (via --app/-a), lisan only activates when this app is frontmost.
# Matching is case-insensitive substring match against the frontmost app's
# name (e.g. "notes" matches "Notes"). None/empty means "all apps".
_target_app: "str | None" = None

# Instant visual feedback typed right after Space, while the Yamli lookup is
# still in flight, so fast typists see *something* immediately instead of a
# noticeable pause. It gets erased and replaced once the real result (or a
# revert to the original word, on failure) is ready.
PLACEHOLDER = "…"

# Tracks the most recent successful swap so the user can press F2 to cycle
# through alternate Arabic candidates for that word (a lightweight stand-in
# for a real suggestions dropdown). Reset to None as soon as any other real
# key is pressed, since the cursor position assumption only holds
# immediately after a swap.
_last_swap_lock = threading.Lock()
_last_swap: "dict | None" = None

# Optional callback, set via set_swap_callback(), invoked with the ranked
# candidates list every time a successful swap happens. Used by lisan_app.py
# (the menu bar wrapper) to populate its "Suggestions" dropdown; harmless if
# left unset when running lisan.py directly from the terminal.
_swap_callback = None


def set_swap_callback(callback) -> None:
    """Register a `callback(candidates: list[str])` fired after each swap."""
    global _swap_callback
    _swap_callback = callback


# ---------------------------------------------------------------------------
# Synthetic keystroke injection helpers
# ---------------------------------------------------------------------------

def inject_backspaces(count: int) -> None:
    """Send `count` synthetic Backspace presses, suppressing their echo."""
    if count <= 0:
        return
    global _suppress_count
    with _suppress_lock:
        _suppress_count += count
    for _ in range(count):
        _kb_controller.press(Key.backspace)
        _kb_controller.release(Key.backspace)


def inject_text(text: str) -> None:
    """Type `text` synthetically, suppressing its echo (1 event per char)."""
    if not text:
        return
    global _suppress_count
    with _suppress_lock:
        _suppress_count += len(text)
    _kb_controller.type(text)


# ---------------------------------------------------------------------------
# Frontmost app detection (macOS only, via System Events)
# ---------------------------------------------------------------------------

def get_frontmost_app_name() -> "str | None":
    """Return the name of the frontmost macOS app, or None if it can't be determined."""
    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to get name of first application '
                'process whose frontmost is true',
            ],
            capture_output=True,
            text=True,
            timeout=1,
        )
        name = result.stdout.strip()
        return name or None
    except Exception as exc:  # noqa: BLE001 - never let this break the listener
        print(f"[lisan] could not determine frontmost app: {exc}", file=sys.stderr)
        return None


def is_target_app_active() -> bool:
    """True if lisan should be active for the currently focused app."""
    if not _target_app:
        return True
    frontmost = get_frontmost_app_name()
    if frontmost is None:
        # If we can't tell, fail open so lisan still works.
        return True
    return _target_app.lower() in frontmost.lower()


# ---------------------------------------------------------------------------
# Yamli network call (runs on a background thread, never blocks typing)
# ---------------------------------------------------------------------------

def fetch_transliteration(word: str) -> str:
    """Fetch the raw response body from Yamli for `word`. May raise."""
    url = YAMLI_URL_TEMPLATE.format(word=word)
    print(f"[lisan] GET {url}")
    response = requests.get(url, verify=False, timeout=REQUEST_TIMEOUT_SECONDS)
    print(f"[lisan] response status={response.status_code} body={response.text!r}")
    response.raise_for_status()
    return response.text


_ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06FF]")
_TRAILING_INDEX_RE = re.compile(r"/\d+$")


def _all_arabic_candidates(r_value: str) -> list:
    """
    Given the "r" field's pipe-delimited candidate list, return *all*
    segments that are actually Arabic text (stripping any trailing "/<n>"
    weight suffix), in ranked order, with duplicates removed.

    Yamli's "r" format varies slightly by endpoint/version, e.g.:
      - "مرحبا/0|مرحبة/1|مرهبة/1|..."             (real api.yamli.com response)
      - "0/marhaba/0|مرحبا/1|مرحباً/0"             (JSONP example from docs)
    In both cases, scanning segments in order and keeping the ones that
    contain Arabic script gives the ranked list of predictions.
    """
    seen = set()
    candidates = []
    for segment in r_value.split("|"):
        candidate = _TRAILING_INDEX_RE.sub("", segment).strip()
        if candidate and _ARABIC_CHAR_RE.search(candidate) and candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def _first_arabic_candidate(r_value: str) -> "str | None":
    """Convenience wrapper: just the top-ranked Arabic candidate, if any."""
    candidates = _all_arabic_candidates(r_value)
    return candidates[0] if candidates else None


def _extract_r_value(raw_text: str) -> "str | None":
    """Pull the raw "r" candidate string out of Yamli's JSON/JSONP response."""
    if not raw_text:
        return None

    text = raw_text.strip()

    # Strip a JSONP-style wrapper: SomeCallbackName( ... )
    jsonp_match = re.match(r"^[A-Za-z_.][A-Za-z0-9_.]*\((.*)\)\s*;?\s*$", text, re.DOTALL)
    if jsonp_match:
        text = jsonp_match.group(1)

    # Try structured JSON parsing first.
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            if "r" in payload and isinstance(payload["r"], str):
                return payload["r"]
            if "data" in payload and isinstance(payload["data"], str):
                # "data" is itself an escaped JSON string, e.g. '{"r":"..."}'
                inner = json.loads(payload["data"])
                if isinstance(inner, dict) and isinstance(inner.get("r"), str):
                    return inner["r"]
    except (json.JSONDecodeError, TypeError):
        pass

    # Fall back to treating the raw text itself as the candidate list.
    return text


def extract_arabic_candidates(raw_text: str) -> list:
    """
    Extract *all* ranked Arabic transliteration candidates from Yamli's
    response. Returns an empty list if none were found.
    """
    r_value = _extract_r_value(raw_text)
    if r_value is None:
        return []
    return _all_arabic_candidates(r_value)


def extract_arabic(raw_text: str) -> "str | None":
    """Extract just the top-ranked Arabic transliteration candidate."""
    candidates = extract_arabic_candidates(raw_text)
    return candidates[0] if candidates else None


def process_word(word: str) -> None:
    """
    Runs on a background thread: fetch Yamli's transliteration for `word`
    and, if found, swap it into the focused field by erasing the
    placeholder + `word` and typing the Arabic result. If nothing is found
    (or the request fails), the placeholder is erased and the original
    word is restored so the user isn't left with a stray "…". Never raises
    into the caller.
    """
    if not word:
        print("[lisan] process_word called with empty word, skipping")
        return

    print(f"[lisan] processing word: '{word}'")

    try:
        raw = fetch_transliteration(word)
    except requests.RequestException as exc:
        print(f"[lisan] network error for '{word}': {exc}", file=sys.stderr)
        restore_original_word(word)
        return

    candidates = extract_arabic_candidates(raw)
    if not candidates:
        print(f"[lisan] no Arabic candidate found for '{word}'")
        restore_original_word(word)
        return

    arabic_word = candidates[0]
    print(f"[lisan] swapping '{word}' -> '{arabic_word}' ({len(candidates)} candidate(s))")
    swap_text(word, arabic_word, had_placeholder=True)
    print(f"[lisan] swap complete for '{word}'")

    global _last_swap
    with _last_swap_lock:
        _last_swap = {
            "candidates": candidates,
            "index": 0,
            "text_len": len(arabic_word) + 1,  # + trailing space
        }

    if _swap_callback is not None:
        try:
            _swap_callback(candidates)
        except Exception as exc:  # noqa: BLE001 - never let a UI bug break typing
            print(f"[lisan] swap callback error: {exc}", file=sys.stderr)


def restore_original_word(word: str) -> None:
    """Erase the placeholder and retype `word` unchanged (lookup failed)."""
    swap_text(word, word, had_placeholder=True)


# ---------------------------------------------------------------------------
# Text swap injection (direct keystroke simulation, no clipboard)
# ---------------------------------------------------------------------------

def swap_text(original_word: str, replacement_word: str, had_placeholder: bool = False) -> None:
    """
    Erase `original_word` (already typed by the user, immediately followed
    by the trigger Space -- and, if `had_placeholder`, the instant "…"
    feedback typed right after it) via synthetic Backspace keypresses, then
    type `replacement_word` followed by a space to restore the word
    boundary for whatever is typed next.
    """
    backspace_count = len(original_word) + 1  # +1 for the trigger space
    if had_placeholder:
        backspace_count += len(PLACEHOLDER)

    print(f"[lisan] sending {backspace_count} backspaces (word + trigger space + placeholder)")
    inject_backspaces(backspace_count)

    print(f"[lisan] typing replacement word + space: '{replacement_word} '")
    inject_text(replacement_word + " ")


# ---------------------------------------------------------------------------
# Global key listener
# ---------------------------------------------------------------------------

def select_candidate(index: int) -> None:
    """
    Replace the currently-shown Arabic word (from the last swap) with the
    candidate at `index`. Used by both F2 cycling and lisan_app.py's
    clickable "Suggestions" menu. No-op if there's no active swap or the
    index is out of range.
    """
    global _last_swap
    with _last_swap_lock:
        if _last_swap is None:
            print("[lisan] no active suggestion to select from")
            return
        candidates = _last_swap["candidates"]
        if not 0 <= index < len(candidates):
            print(f"[lisan] candidate index {index} out of range (0..{len(candidates) - 1})")
            return
        old_len = _last_swap["text_len"]
        new_word = candidates[index]
        _last_swap["index"] = index
        _last_swap["text_len"] = len(new_word) + 1

    print(f"[lisan] selecting candidate {index + 1}/{len(candidates)}: '{new_word}'")
    inject_backspaces(old_len)
    inject_text(new_word + " ")


def cycle_last_swap_suggestion() -> None:
    """
    Handle an F2 press: replace the currently-shown Arabic word with the
    next alternate candidate from the last swap (wrapping around).
    """
    with _last_swap_lock:
        if _last_swap is None:
            print("[lisan] F2 pressed but there's no active suggestion to cycle")
            return
        next_index = (_last_swap["index"] + 1) % len(_last_swap["candidates"])
    select_candidate(next_index)


def on_press(key) -> None:
    global _buffer

    try:
        _handle_key(key)
    except Exception as exc:  # noqa: BLE001 - never let a bad event kill the listener
        print(f"[lisan] error handling key {key!r}: {exc}", file=sys.stderr)


def _handle_key(key) -> None:
    global _buffer, _suppress_count

    # Ignore key events that we ourselves injected (synthetic backspaces /
    # replacement text from swap_text). Without this, injected keystrokes
    # loop back into the listener and can re-trigger process_word, causing
    # a runaway feedback loop -- most visible when typing fast enough that
    # a swap is still injecting while the next word is already underway.
    with _suppress_lock:
        if _suppress_count > 0:
            _suppress_count -= 1
            return

    char = getattr(key, "char", None)
    print(f"[lisan] key event: {key!r} (char={char!r})")

    # F2: cycle to the next alternate Arabic candidate for the word that was
    # just swapped (a lightweight suggestions "dropdown" substitute). Only
    # works immediately after a swap -- any other real key press below
    # invalidates it, since the cursor position assumption no longer holds.
    if key == Key.f2:
        cycle_last_swap_suggestion()
        return

    # Any other real key press invalidates the F2 cycling target.
    global _last_swap
    with _last_swap_lock:
        _last_swap = None

    # Alphabetic character keys.
    if char is not None and char.isalpha():
        with _buffer_lock:
            _buffer.append(char)
            print(f"[lisan] buffer -> {''.join(_buffer)!r}")
        return

    # Backspace: remove last char from the buffer.
    if key == Key.backspace:
        with _buffer_lock:
            if _buffer:
                _buffer.pop()
            print(f"[lisan] buffer (after backspace) -> {''.join(_buffer)!r}")
        return

    # Space: lock/snapshot the buffer, reset it, and trigger the async
    # Yamli lookup + swap on a separate thread so typing is never blocked.
    if key == Key.space:
        with _buffer_lock:
            word = "".join(_buffer)
            _buffer.clear()

        if word and not is_target_app_active():
            print(
                f"[lisan] space pressed for {word!r}, but frontmost app doesn't "
                f"match target app {_target_app!r}, skipping"
            )
            return

        print(f"[lisan] space pressed, triggering lookup for {word!r}")
        if word:
            # Instant feedback so fast typists don't feel the network delay:
            # type a placeholder right away, then let process_word() swap in
            # the real result (or revert to `word`) once it's ready.
            inject_text(PLACEHOLDER)
            threading.Thread(target=process_word, args=(word,), daemon=True).start()
        return

    # Any other key (punctuation, enter, arrows, etc.) breaks the current
    # word boundary — reset the buffer.
    with _buffer_lock:
        if _buffer:
            print(f"[lisan] non-alpha key, clearing buffer (was {''.join(_buffer)!r})")
        _buffer.clear()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="lisan - transliterate typed Latin words to Arabic on Space."
    )
    parser.add_argument(
        "-a",
        "--app",
        default=None,
        help=(
            "Only activate lisan when this app is frontmost (case-insensitive "
            "substring match, e.g. 'notes' matches 'Notes'). If omitted, lisan "
            "is active for every app."
        ),
    )
    return parser.parse_args()


def start_lisan(app_filter: "str | None" = None) -> keyboard.Listener:
    """
    Start the global key listener on a background thread and return it
    immediately (non-blocking). Used by lisan_app.py (the menu bar wrapper)
    to toggle lisan on/off; `python3 lisan.py` also uses this internally but
    then blocks on listener.join() to stay alive as a foreground script.
    """
    global _target_app
    _target_app = app_filter

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    print("[lisan] listener started" + (f" (restricted to {app_filter!r})" if app_filter else ""))
    return listener


def stop_lisan(listener: keyboard.Listener) -> None:
    """Stop a listener previously returned by start_lisan()."""
    listener.stop()
    print("[lisan] listener stopped")


def main() -> None:
    args = parse_args()

    print("lisan running.")
    if args.app:
        print(f"[lisan] restricted to app matching: {args.app!r}")
    else:
        print("[lisan] active for all apps")
    print(f"Python executable: {sys.executable}")
    print(
        "IMPORTANT: macOS grants Accessibility/Input Monitoring permission "
        "to the *exact binary path* above, not to 'Terminal' in general. "
        "If typed text isn't being replaced, add THIS EXACT PATH under both:\n"
        "  System Settings > Privacy & Security > Accessibility\n"
        "  System Settings > Privacy & Security > Input Monitoring\n"
        "(Use Cmd+Shift+G in the add-file dialog to paste the path directly, "
        "since pyenv/venv binaries are usually hidden from the normal picker.)\n"
        "After adding/toggling permissions, fully quit (Ctrl+C) and restart "
        "this script -- macOS caches trust at process launch."
    )
    print("Type a word and press Space to transliterate it to Arabic.")
    print("Press F2 right after a swap to cycle alternate candidates.")
    print("Press Ctrl+C in this terminal to stop.")
    print(
        "If you see NO '[lisan] key event' lines below as you type, the "
        "listener itself isn't receiving events (Input Monitoring not granted "
        "for the path above). If key events/network/parsing all log fine but "
        "nothing changes on screen, and you see 'This process is not trusted!' "
        "above, that means Accessibility (not Input Monitoring) is missing for "
        "the path above -- injection is silently failing."
    )

    listener = start_lisan(args.app)
    try:
        listener.join()
    except KeyboardInterrupt:
        print("\nStopping lisan.")
        stop_lisan(listener)


if __name__ == "__main__":
    main()
