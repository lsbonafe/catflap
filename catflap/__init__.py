#!/usr/bin/env python3
"""catflap — live adb logcat viewer with dynamic filters, Android Studio style.

A package split out of the original single-file module. This ``__init__``
re-exports the public surface so ``catflap.parse_query`` / ``catflap.Catflap``
etc. keep working, and exposes the submodules tests monkeypatch
(``catflap.adb``, ``catflap.state``, ``catflap.subprocess``).
"""

# subprocess as a package attribute so tests can patch catflap.subprocess.{Popen,run}
import subprocess  # noqa: F401

# Textual's Input is re-exported (the old monolith had it in scope; tests use it)
from textual.widgets import Input  # noqa: F401

# submodules as attributes — monkeypatch homes (catflap.adb.list_devices,
# catflap.state.STATE_PATH) and general access
from catflap import adb, app, cli, crash, entry, export, filtering, state, widgets  # noqa: F401

# --- pure functions / data ---
from catflap.filtering import (  # noqa: F401
    compile_term, parse_terms, matches, FIELD_ALIASES, KEY_RE, compile_predicate,
    parse_query, query_matches, highlight_patterns, _migrate_query, _scope_box,
    split_last_term, suggest, split_query_token, parse_token,
)
from catflap.entry import (  # noqa: F401
    Entry, parse_line, level_at_least, level_matches, LINE_RE, LEVEL_STYLES, LEVELS,
)
from catflap.crash import is_crash_start, crash_block, crash_package, banner_diff  # noqa: F401
from catflap.export import (  # noqa: F401
    md_escape, export_markdown, export_filename, export_raw, ensure_dir,
)
from catflap.state import STATE_PATH, load_state, save_state  # noqa: F401
from catflap.adb import (  # noqa: F401
    parse_permissions, parse_foreground, parse_devices, avd_name,
    logcat_cmd, list_devices, BUFFER_CHOICES,
)

# --- widgets / screens ---
from catflap.widgets import (  # noqa: F401
    HELP_TEXT, LEVEL_LABELS, FOOTER_ORDER,
    ClearButton, DropdownArrow, PaletteClose, ClosableCommandPalette,
    QueryHighlighter, CloseButton, OutsideClickDismiss, HelpScreen,
    TextViewerScreen, PickListScreen, ExportDirScreen, FilterPickScreen,
    TextPromptScreen, SavePresetScreen, LogPane, OrderedFooter, LevelChip,
    RecBar, DevicePickerScreen,
)

# --- app + cli ---
from catflap.app import Catflap, BUFFER_MAX, DISPLAY_MAX  # noqa: F401
from catflap.cli import (  # noqa: F401
    main, run_dump, _build_parser, _resolve_pid_names, _pick_dump_serial,
)
