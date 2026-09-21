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

# Quick-pick apps shown in the "Run in" submenu. "All Apps" always means no
# restriction (matches everything). Add/remove names here as you like --
# matching is a case-insensitive substring match against the frontmost
# app's name, same as lisan.py's --app flag.
APP_FILTER_CHOICES = ["All Apps", "Notes", "WhatsApp", "Messages", "Mail", "Safari"]


class LisanApp(rumps.App):
    def __init__(self):
        super().__init__("لسان", quit_button=None)

        self.listener = None

        self.toggle_item = rumps.MenuItem("Start", callback=self.toggle)

        # "Run in" submenu: pick which app lisan is restricted to, live.
        self.app_filter_item = rumps.MenuItem("Run in: All Apps", callback=lambda _: None)
        self.app_filter_option_items = {}
        for name in APP_FILTER_CHOICES:
            item = rumps.MenuItem(name, callback=self.select_app_filter)
            item.state = name == "All Apps"
            self.app_filter_option_items[name] = item
            self.app_filter_item.add(item)
        self.app_filter_item.add(None)  # separator
        self.custom_app_item = rumps.MenuItem("Custom App...", callback=self.select_custom_app)
        self.app_filter_item.add(self.custom_app_item)

        # rumps greys out / disables a MenuItem that has no callback, even
        # if it later gets a submenu attached via .add(). Give it a no-op
        # callback so it stays enabled and its "Suggestions" submenu opens
        # normally when hovered/clicked.
        self.suggestions_item = rumps.MenuItem("Suggestions (none yet)", callback=lambda _: None)
        self.quit_item = rumps.MenuItem("Quit", callback=self.quit_app)

        self.menu = [
            self.toggle_item,
            None,
            self.app_filter_item,
            None,
            self.suggestions_item,
            None,
            self.quit_item,
        ]

        lisan.set_swap_callback(self.on_swap)

    # -- Start/Stop -----------------------------------------------------

    def toggle(self, sender):
        if self.listener is None:
            self.listener = lisan.start_lisan(lisan._target_app)
            sender.title = "Stop"
            self.title = "لسان ✅"
        else:
            lisan.stop_lisan(self.listener)
            self.listener = None
            sender.title = "Start"
            self.title = "لسان"
            self._clear_suggestions()

    # -- "Run in" app filter ---------------------------------------------

    def select_app_filter(self, sender):
        name = sender.title
        target = None if name == "All Apps" else name
        self._apply_app_filter(target, label=name)

    def select_custom_app(self, sender):
        response = rumps.Window(
            message="Restrict lisan to this app (case-insensitive, matches part of the frontmost app's name). Leave blank for All Apps.",
            title="Custom App",
            default_text=lisan._target_app or "",
            ok="Set",
            cancel="Cancel",
        ).run()
        if not response.clicked:
            return
        text = response.text.strip()
        self._apply_app_filter(text or None, label=text or "All Apps")

    def _apply_app_filter(self, target, label):
        lisan.set_target_app(target)
        self.app_filter_item.title = f"Run in: {label}"
        for name, item in self.app_filter_option_items.items():
            item.state = name == label
        self.custom_app_item.state = label not in self.app_filter_option_items

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
