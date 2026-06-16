#!/usr/bin/env python3
"""catflap — live adb logcat viewer with dynamic filters, Android Studio style.

Two input boxes re-filter the stream as you type: a package box (process name)
and a unified query box. The query box speaks Android-Studio field keys —
tag: / message: / package: with =: (exact), ~: (regex) and a leading - to
negate — plus the boolean OR/AND/NOT operators and inline /regex/. A bare word
with no key matches the tag OR the message.
"""

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue

from rich.cells import cell_len
from rich.highlighter import Highlighter
from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.command import CommandPalette
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Footer,
    Input,
    Label,
    OptionList,
    RichLog,
    Static,
)
from textual.widgets._footer import FooterKey
from textual.widgets.option_list import Option

BUFFER_MAX = 20_000   # parsed lines kept in memory
DISPLAY_MAX = 2_000   # lines re-rendered after a filter change

from catflap.entry import (  # noqa: E402  (kept near the original location)
    LINE_RE, LEVEL_STYLES, LEVELS, Entry, parse_line,
    level_at_least, level_matches,
)


from catflap.filtering import (  # noqa: E402
    compile_term, parse_terms, matches, FIELD_ALIASES, KEY_RE, compile_predicate,
    parse_query, query_matches, highlight_patterns, _migrate_query, _scope_box,
    split_last_term, suggest, split_query_token, parse_token,
)


from catflap.crash import (  # noqa: E402
    is_crash_start, crash_block, crash_package, banner_diff,
)
from catflap.export import (  # noqa: E402
    md_escape, export_markdown, export_filename, export_raw, ensure_dir,
)
from catflap import state as state  # noqa: E402  (mutated by tests: state.STATE_PATH)
from catflap.state import STATE_PATH, load_state, save_state  # noqa: E402


from catflap import adb as adb  # noqa: E402  (mutated by tests: adb.list_devices)
from catflap.adb import (  # noqa: E402
    parse_permissions, parse_foreground, parse_devices, avd_name,
    logcat_cmd, list_devices, BUFFER_CHOICES,
)


from catflap.widgets import (  # noqa: E402
    HELP_TEXT, LEVEL_LABELS, FOOTER_ORDER,
    ClearButton, DropdownArrow, PaletteClose, ClosableCommandPalette,
    QueryHighlighter, CloseButton, OutsideClickDismiss, HelpScreen,
    TextViewerScreen, PickListScreen, ExportDirScreen, FilterPickScreen,
    TextPromptScreen, SavePresetScreen, LogPane, OrderedFooter, LevelChip,
    RecBar, DevicePickerScreen,
)


from catflap.app import Catflap, BUFFER_MAX, DISPLAY_MAX  # noqa: E402


def _resolve_pid_names(serial):
    """pid -> process name map, same source the TUI uses."""
    names = {}
    try:
        out = subprocess.run(
            ["adb", "-s", serial, "shell", "ps", "-A", "-o", "PID,NAME"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        for line in out.splitlines()[1:]:
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                names[parts[0]] = parts[1].strip()
    except Exception:
        pass
    return names


def _pick_dump_serial(requested):
    """Resolve the device to dump from; return (serial, error_message)."""
    devices = adb.list_devices()
    if requested:
        if any(s == requested for s, _ in devices):
            return requested, None
        return None, f"device '{requested}' not found"
    if not devices:
        return None, "no devices connected"
    if len(devices) > 1:
        listing = ", ".join(s for s, _ in devices)
        return None, f"multiple devices, pass --device (one of: {listing})"
    return devices[0][0], None


def run_dump(args):
    """Headless filtered logcat for scripts and AI agents. Returns an exit code."""
    serial, err = _pick_dump_serial(args.device)
    if err:
        print(f"catflap: {err}", file=sys.stderr)
        return 1

    f_pkg = parse_terms(args.package or "")
    f_tag = parse_terms(args.tag or "")
    f_msg = parse_terms(args.message or "")
    pid_names = _resolve_pid_names(serial) if args.package else {}

    cmd = logcat_cmd(serial, [args.buffer] if args.buffer else None)
    if not args.follow:
        cmd.append("-d")  # dump the buffer and exit

    def visible(e):
        return (
            level_matches(e.level, args.level, args.exact)
            and matches(pid_names.get(e.pid, ""), f_pkg)
            and matches(e.tag, f_tag)
            and matches(e.msg, f_msg)
        )

    def emit(e):
        pkg = pid_names.get(e.pid, "")
        if args.format == "jsonl":
            print(json.dumps({
                "ts": e.ts, "pid": e.pid, "tid": e.tid, "level": e.level,
                "package": pkg, "tag": e.tag, "message": e.msg,
            }))
        else:
            print(f"{e.ts} {e.level} {pkg or e.pid} {e.tag}: {e.msg}")

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, errors="replace")
    except FileNotFoundError:
        print("catflap: adb not found on PATH", file=sys.stderr)
        return 1

    count = 0
    try:
        for line in proc.stdout:
            e = parse_line(line.rstrip("\n"))
            if e is None or not visible(e):
                continue
            emit(e)
            count += 1
            if args.lines and count >= args.lines:
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            proc.kill()
        except Exception:
            pass
    return 0


def _build_parser():
    import argparse
    p = argparse.ArgumentParser(
        prog="catflap",
        description="Terminal UI for Android logcat (run with no command to open the app).",
    )
    p.add_argument("--version", action="store_true", help="print version and exit")
    sub = p.add_subparsers(dest="command")
    d = sub.add_parser(
        "dump",
        help="print filtered logcat to stdout and exit (for scripts and AI agents)",
        description="Filtered logcat using catflap's boolean/regex syntax — "
                    "no TUI. Filters accept 'a AND b', 'x OR y', 'NOT z', and /regex/.",
    )
    d.add_argument("--device", help="adb serial (required if several are connected)")
    d.add_argument("--package", help="filter on process name (boolean/regex syntax)")
    d.add_argument("--tag", help="filter on log tag (boolean/regex syntax)")
    d.add_argument("--message", help="filter on message text (boolean/regex syntax)")
    d.add_argument("--level", default="V", choices=LEVELS[:-1],
                   help="minimum level V/D/I/W/E (default V)")
    d.add_argument("--exact", action="store_true",
                   help="match the level exactly instead of 'and above'")
    d.add_argument("--buffer", choices=["main", "system", "crash", "events", "radio"],
                   help="logcat buffer (default: adb's main+system)")
    d.add_argument("--lines", type=int, default=500,
                   help="stop after N matching lines (default 500; 0 = unlimited)")
    d.add_argument("--follow", action="store_true",
                   help="keep streaming instead of dumping and exiting")
    d.add_argument("--format", choices=["jsonl", "text"], default="text",
                   help="output format (default text; jsonl = one JSON object per line)")
    return p


def main():
    parser = _build_parser()
    args = parser.parse_args()
    if args.version:
        try:
            from importlib.metadata import version
            print(f"catflap {version('catflap')}")
        except Exception:
            print("catflap (dev)")
        return
    if args.command == "dump":
        sys.exit(run_dump(args))
    Catflap().run()


if __name__ == "__main__":
    main()
