#!/usr/bin/env python3
"""
lisan_app.py

A macOS menu bar wrapper around lisan.py. Instead of running lisan.py from
a terminal, this puts a small icon in your menu bar with:

  - A Start/Stop toggle, so lisan only runs while you want it to.
  - A "Suggestions" submenu showing the alternate Arabic candidates for the
    last word you swapped -- click one to pick it instead of the default.

REQUIREMENTS
------------
    pip3 install -r requirements.txt   # includes rumps, pynput, requests

USAGE
-----
    python3 lisan_app.py

The same macOS Accessibility / Input Monitoring permissions described in
lisan.py's docstring / the README are required -- grant them to whichever
Python binary runs this script.
"""

import sys

import rumps

import lisan


class LisanApp(rumps.App):
    def __init__(self):
        super().__init__("لسان", quit_button=None)

        self.listener = None

        self.toggle_item = rumps.MenuItem("Start", callback=self.toggle)
        self.suggestions_item = rumps.MenuItem("Suggestions (none yet)")
        self.quit_item = rumps.MenuItem("Quit", callback=self.quit_app)

        self.menu = [self.toggle_item, None, self.suggestions_item, None, self.quit_item]

        lisan.set_swap_callback(self.on_swap)

    # -- Start/Stop -----------------------------------------------------

    def toggle(self, sender):
        if self.listener is None:
            self.listener = lisan.start_lisan()
            sender.title = "Stop"
            self.title = "لسان ✅"
        else:
            lisan.stop_lisan(self.listener)
            self.listener = None
            sender.title = "Start"
            self.title = "لسان"
            self._clear_suggestions()

    # -- Suggestions dropdown --------------------------------------------

    def on_swap(self, candidates):
        # Called from the background listener thread; rebuilding the menu
        # here is a common (if not strictly documented) pattern with rumps,
        # since it's ultimately just PyObjC/NSMenu calls under the hood.
        if not candidates:
            self._clear_suggestions()
            return

        self.suggestions_item.title = f"Suggestions ({len(candidates)})"
        # Clear previous entries, then repopulate.
        for key in list(self.suggestions_item.keys()):
            del self.suggestions_item[key]
        for i, word in enumerate(candidates):
            label = f"{i + 1}. {word}"
            self.suggestions_item.add(rumps.MenuItem(label, callback=self._make_picker(i)))

    def _make_picker(self, index):
        def _pick(sender):
            lisan.select_candidate(index)

        return _pick

    def _clear_suggestions(self):
        self.suggestions_item.title = "Suggestions (none yet)"
        for key in list(self.suggestions_item.keys()):
            del self.suggestions_item[key]

    # -- Quit -------------------------------------------------------------

    def quit_app(self, sender):
        if self.listener is not None:
            lisan.stop_lisan(self.listener)
        rumps.quit_application()


def main() -> None:
    print("lisan menu bar app starting.")
    print(f"Python executable: {sys.executable}")
    print(
        "Grant Accessibility + Input Monitoring permissions to the exact "
        "path above under System Settings > Privacy & Security, then "
        "restart this app if lisan doesn't seem to react to typing."
    )
    LisanApp().run()


if __name__ == "__main__":
    main()
