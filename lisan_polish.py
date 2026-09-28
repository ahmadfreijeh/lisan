"""OpenAI-backed sentence polishing and its local text buffer."""

import os
import sys
import threading

from openai import OpenAI
from pynput.keyboard import Key
from dotenv import load_dotenv

from lisan_input import TextInjector

# Load local secrets from .env when lisan starts.
load_dotenv()

# Keep the model local and static. Mini provides noticeably stronger writing
# quality than Nano while remaining fast enough for an inline rewrite.
OPENAI_MODEL = "gpt-5.4-mini"
OPENAI_TIMEOUT_SECONDS = 20
# Use plain ASCII so the loader can be injected reliably in Cursor and other
# editors, regardless of the active keyboard layout.
PLACEHOLDER = "..."


class SentencePolisher:
    """Collect the current sentence and replace it after ⌘⇧R."""

    def __init__(self, injector: TextInjector, get_frontmost_app):
        self._injector = injector
        self._get_frontmost_app = get_frontmost_app
        self._buffer: list[str] = []
        self._sentence_complete = False
        self._origin_app = None
        self._revision = 0
        self._pending = False
        self._placeholder_active = False
        self._lock = threading.Lock()
        self._status_callback = None

    def set_status_callback(self, callback) -> None:
        """Receive True while an OpenAI request is in progress."""
        self._status_callback = callback

    def reset(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._sentence_complete = False
            self._origin_app = None
            self._revision += 1
            self._placeholder_active = False

    def handle_key(self, key, char: "str | None", shortcut_pressed: bool, app_is_active: bool) -> None:
        if shortcut_pressed:
            self._submit(app_is_active)
            return

        with self._lock:
            if self._pending and self._placeholder_active:
                # Remove the inline loader before accepting new text. The
                # changed revision cancels the in-flight request safely.
                self._injector.backspaces(len(PLACEHOLDER))
                self._placeholder_active = False
            self._revision += 1
            if char is not None:
                if self._sentence_complete and not char.isspace():
                    self._buffer.clear()
                    self._sentence_complete = False
                    self._origin_app = None
                # Capture the character before the macOS app lookup. That
                # lookup can be slow enough to make the first typed letter
                # appear missing from the replacement buffer.
                self._buffer.append(char)
                self._sentence_complete = char in ".!?"
                if self._origin_app is None:
                    self._origin_app = self._get_frontmost_app()
            elif key == Key.space:
                self._buffer.append(" ")
            elif key == Key.backspace:
                if self._buffer:
                    self._buffer.pop()
                self._sentence_complete = bool(self._buffer and self._buffer[-1] in ".!?")
            else:
                self._buffer.clear()
                self._sentence_complete = False
                self._origin_app = None

    def _submit(self, app_is_active: bool) -> None:
        if not app_is_active:
            return
        current_app = self._get_frontmost_app()
        with self._lock:
            if self._pending:
                print("[lisan] polish already in progress")
                return
            if self._origin_app and current_app != self._origin_app:
                self._buffer.clear()
                self._sentence_complete = False
                self._origin_app = None
                self._revision += 1
                print("[lisan] polish skipped because the capture belongs to another app")
                return
            sentence = "".join(self._buffer).strip()
            revision = self._revision
            if not sentence:
                return
            self._pending = True
            self._placeholder_active = True
            self._injector.text(PLACEHOLDER)
        self._notify_status(True)
        threading.Thread(target=self._request, args=(sentence, revision, current_app), daemon=True).start()

    def _request(self, sentence: str, revision: int, origin_app: "str | None") -> None:
        try:
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                print("[lisan] polish skipped: OPENAI_API_KEY is not set", file=sys.stderr)
                return
            response = OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS).responses.create(
                model=OPENAI_MODEL,
                reasoning={"effort": "low"},
                instructions=(
                    "You are a careful editor. Rewrite the user's text into one polished sentence. "
                    "Fix grammar, spelling, punctuation, capitalization, wording, sentence structure, "
                    "and flow. Improve clarity and naturalness without changing the intended meaning, "
                    "language, tone, facts, or level of formality. Copy every proper noun exactly as "
                    "written, including names, brands, and places; never shorten, replace, or misspell "
                    "them. Never use an em dash; use commas, periods, or parentheses instead. Do not "
                    "add new claims or repeat words, letters, phrases, or clauses. Return only the "
                    "final rewritten text, with no quotes, explanation, label, or Markdown."
                ),
                input=sentence,
            )
            replacement = response.output_text.strip().replace("—", ",").replace(" ,", ",")
            if not replacement:
                raise ValueError("OpenAI returned no text")
        except Exception as exc:  # noqa: BLE001 - preserve original text on failure
            print(f"[lisan] polish failed: {exc}", file=sys.stderr)
            with self._lock:
                self._clear_placeholder_if_safe(origin_app)
        else:
            with self._lock:
                if revision != self._revision or (origin_app and self._get_frontmost_app() != origin_app):
                    print("[lisan] polish cancelled because typing or focus changed")
                else:
                    placeholder_length = len(PLACEHOLDER) if self._placeholder_active else 0
                    self._injector.backspaces(len(sentence) + placeholder_length)
                    self._injector.text(replacement)
                    self._buffer[:] = list(replacement)
                    self._placeholder_active = False
        finally:
            with self._lock:
                self._pending = False
            self._notify_status(False)

    def _clear_placeholder_if_safe(self, origin_app: "str | None") -> None:
        """Erase the loader only while its original text field is focused."""
        if self._placeholder_active and (not origin_app or self._get_frontmost_app() == origin_app):
            self._injector.backspaces(len(PLACEHOLDER))
            self._placeholder_active = False

    def _notify_status(self, pending: bool) -> None:
        if self._status_callback is not None:
            try:
                self._status_callback(pending)
            except Exception as exc:  # noqa: BLE001 - UI feedback is optional
                print(f"[lisan] polish status callback error: {exc}", file=sys.stderr)
