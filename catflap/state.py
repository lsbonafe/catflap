"""Persisted settings at ~/.config/catflap/state.json.

STATE_PATH is a module global read at call time by load_state/save_state, so
tests redirect persistence by setting ``catflap.state.STATE_PATH``."""

import json
from pathlib import Path

STATE_PATH = Path.home() / ".config" / "catflap" / "state.json"


def load_state():
    if not STATE_PATH.exists() and STATE_PATH.parent.name == "catflap":
        legacy = Path.home() / ".config" / "logcat-tui" / "state.json"
        if legacy.exists():
            try:
                STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                STATE_PATH.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                pass
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass
