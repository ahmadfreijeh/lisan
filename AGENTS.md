# Repository Guidelines

## Project Structure

This is a small macOS Python utility, not a packaged application. Keep the core keyboard/transliteration behavior in `lisan.py`; it provides the command-line entry point and public helpers used by the UI. Keep menu-bar behavior in `lisan_app.py`, which imports `lisan` and uses `rumps`. User-facing documentation and demo media live in `README.md` and `docs/` (`docs/index.html`, `demo.gif`, and `demo.mp4`). Dependencies are listed in `requirements.txt`.

## Setup, Run, and Checks

Use Python 3.9+ and preferably a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip3 install -r requirements.txt
python3 lisan.py --app Notes
python3 lisan_app.py
python3 -m py_compile lisan.py lisan_app.py
```

The first command path runs the background listener, optionally limited to a frontmost app; the second launches the menu-bar wrapper. `py_compile` is the current lightweight syntax check. Both runtime modes require macOS Accessibility and Input Monitoring permissions for the Python executable.

## Coding Style

Follow the existing Python style: four-space indentation, `snake_case` functions and variables, `UPPER_SNAKE_CASE` module constants, and concise docstrings on public functions. Keep the listener responsive: network work belongs on background threads, and injected keyboard events must remain suppression-safe. Handle external/API and UI failures defensively so a bad event never terminates the listener. Use comments to explain macOS or keystroke-synchronization constraints, not obvious code.

## Testing Guidelines

There is no automated test suite yet. For parser changes, exercise pure helpers from a Python shell (for example, `extract_arabic_candidates(...)`) and run the compile check above. For listener or UI changes, manually verify typing, fallback behavior, F2 candidate cycling, Start/Stop, and the app filter in a non-sensitive text field. Do not test in password fields.

## Commits and Pull Requests

Recent history uses short imperative subjects, e.g. `Add GitHub Pages landing page` and `Simplify landing page`. Keep commits focused and describe the observable change. PRs should state the behavior changed, list manual verification, link relevant issues, and include a screenshot or recording for `docs/` or menu-bar UI changes. Call out permission, network, or Yamli API implications explicitly.
