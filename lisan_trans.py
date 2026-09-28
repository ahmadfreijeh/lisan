"""Yamli API integration and word-by-word Arabic transliteration."""

import json
import re
import sys
import threading

import requests
import urllib3
from pynput.keyboard import Key

from lisan_input import TextInjector

YAMLI_URL_TEMPLATE = (
    "https://api.yamli.com/transliterate.ashx"
    "?word={word}&tool=api&account_id=000006&prot=https"
    "&hostname=AliMZaini&path=yamli-api&build=5515"
)
REQUEST_TIMEOUT_SECONDS = 5
PLACEHOLDER = "…"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06FF]")
_TRAILING_INDEX_RE = re.compile(r"/\d+$")


def extract_arabic_candidates(raw_text: str) -> list[str]:
    """Return ranked, duplicate-free Arabic candidates from a Yamli response."""
    if not raw_text:
        return []
    text = raw_text.strip()
    jsonp_match = re.match(r"^[A-Za-z_.][A-Za-z0-9_.]*\((.*)\)\s*;?\s*$", text, re.DOTALL)
    if jsonp_match:
        text = jsonp_match.group(1)
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            candidate_text = payload.get("r") or payload.get("data")
            if isinstance(candidate_text, str):
                text = candidate_text
                if text.startswith("{"):
                    inner = json.loads(text)
                    if isinstance(inner, dict) and isinstance(inner.get("r"), str):
                        text = inner["r"]
    except (json.JSONDecodeError, TypeError):
        pass

    candidates = []
    for segment in text.split("|"):
        candidate = _TRAILING_INDEX_RE.sub("", segment).strip()
        if candidate and _ARABIC_CHAR_RE.search(candidate) and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def extract_arabic(raw_text: str) -> "str | None":
    """Return Yamli's top Arabic candidate, if one exists."""
    candidates = extract_arabic_candidates(raw_text)
    return candidates[0] if candidates else None


class Transliterator:
    """Track one Latin word and replace it with Yamli's top result."""

    def __init__(self, injector: TextInjector):
        self._injector = injector
        self._buffer: list[str] = []
        self._last_swap = None
        self._lock = threading.Lock()
        self._swap_callback = None

    def set_swap_callback(self, callback) -> None:
        self._swap_callback = callback

    def reset(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._last_swap = None

    def handle_key(self, key, char: "str | None", app_is_active: bool) -> None:
        if key == Key.f2:
            self.cycle_candidate()
            return

        with self._lock:
            self._last_swap = None
            if char is not None and char.isalpha():
                self._buffer.append(char)
                return
            if key == Key.backspace:
                if self._buffer:
                    self._buffer.pop()
                return
            if key == Key.space:
                word = "".join(self._buffer)
                self._buffer.clear()
            else:
                self._buffer.clear()
                return

        if word and app_is_active:
            self._injector.text(PLACEHOLDER)
            threading.Thread(target=self._process_word, args=(word,), daemon=True).start()

    def select_candidate(self, index: int) -> None:
        with self._lock:
            if self._last_swap is None:
                return
            candidates = self._last_swap["candidates"]
            if not 0 <= index < len(candidates):
                return
            old_length = self._last_swap["text_length"]
            replacement = candidates[index]
            self._last_swap["index"] = index
            self._last_swap["text_length"] = len(replacement) + 1
        self._injector.backspaces(old_length)
        self._injector.text(replacement + " ")

    def cycle_candidate(self) -> None:
        with self._lock:
            if self._last_swap is None:
                return
            index = (self._last_swap["index"] + 1) % len(self._last_swap["candidates"])
        self.select_candidate(index)

    def _process_word(self, word: str) -> None:
        try:
            response = requests.get(
                YAMLI_URL_TEMPLATE.format(word=word), verify=False, timeout=REQUEST_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            candidates = extract_arabic_candidates(response.text)
        except requests.RequestException as exc:
            print(f"[lisan] Yamli request failed: {exc}", file=sys.stderr)
            candidates = []

        replacement = candidates[0] if candidates else word
        self._injector.backspaces(len(word) + 1 + len(PLACEHOLDER))
        self._injector.text(replacement + " ")
        if not candidates:
            return

        with self._lock:
            self._last_swap = {
                "candidates": candidates,
                "index": 0,
                "text_length": len(replacement) + 1,
            }
        if self._swap_callback is not None:
            try:
                self._swap_callback(candidates)
            except Exception as exc:  # noqa: BLE001 - UI must not stop typing
                print(f"[lisan] swap callback error: {exc}", file=sys.stderr)
