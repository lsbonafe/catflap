#!/usr/bin/env python3
"""logcat-tui — live adb logcat viewer with dynamic filters, Android Studio style.

Three input boxes (package, tag, message) re-filter the stream as you type.
Filter syntax: case-insensitive substring; alternatives joined with " OR ",
e.g. message box: "toto OR bla bla".
"""

import json
import re
import subprocess
import threading
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue

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

LINE_RE = re.compile(
    r"^(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"(?P<pid>\d+)\s+(?P<tid>\d+)\s+"
    r"(?P<level>[VDIWEFS])\s+"
    r"(?P<tag>.*?)\s*: (?P<msg>.*)$"
)

LEVEL_STYLES = {
    "V": "dim",
    "D": "cyan",
    "I": "green",
    "W": "yellow",
    "E": "red",
    "F": "bold red",
    "S": "dim",
}


def compile_term(term):
    """Plain text -> escaped substring pattern; /…/ -> regex. Both case-insensitive.
    An invalid /regex/ falls back to literal matching."""
    if len(term) > 2 and term.startswith("/") and term.endswith("/"):
        try:
            return re.compile(term[1:-1], re.IGNORECASE)
        except re.error:
            pass
    return re.compile(re.escape(term), re.IGNORECASE)


def parse_terms(text):
    """Boolean query -> DNF clause list of (pattern, negated) pairs.
    Operators are uppercase-only words; AND binds tighter than OR.
    'a AND NOT b OR c' == (a AND NOT b) OR c."""
    clauses = []
    for part in re.split(r"\s+OR\s+", text):
        patterns = []
        for t in re.split(r"\s+AND\s+", part):
            t = t.strip()
            if not t:
                continue
            negated = t.startswith("NOT ") and bool(t[4:].strip())
            if negated:
                t = t[4:].strip()
            patterns.append((compile_term(t), negated))
        if patterns:
            clauses.append(patterns)
    return clauses


def matches(value, clauses):
    """True if no clauses (filter empty) or any clause is fully satisfied."""
    return not clauses or any(
        all(bool(p.search(value)) != negated for p, negated in patterns)
        for patterns in clauses
    )


class Entry:
    __slots__ = ("ts", "pid", "tid", "level", "tag", "msg")

    def __init__(self, ts, pid, tid, level, tag, msg):
        self.ts = ts
        self.pid = pid
        self.tid = tid
        self.level = level
        self.tag = tag
        self.msg = msg


def parse_line(line):
    m = LINE_RE.match(line)
    if not m:
        return None
    return Entry(m["ts"], m["pid"], m["tid"], m["level"], m["tag"], m["msg"])


LEVELS = ["V", "D", "I", "W", "E", "F"]


def level_at_least(level, minimum):
    """True if level ranks >= minimum; unknown levels always pass."""
    try:
        return LEVELS.index(level) >= LEVELS.index(minimum)
    except ValueError:
        return True


def level_matches(level, minimum, exact=False):
    """Threshold match by default; exact match when exact=True.
    Exact E still includes F — both are errors."""
    if not exact:
        return level_at_least(level, minimum)
    if minimum == "E":
        return level in ("E", "F")
    return level == minimum


def is_crash_start(e):
    return e.level == "F" or (e.tag == "AndroidRuntime" and "FATAL EXCEPTION" in e.msg)


def crash_block(entries, start_entry, limit=400):
    """The crash line plus its contiguous same-pid/same-tag follow-up (stack trace).
    Returns [] if start_entry is no longer in entries (evicted from buffer)."""
    block = []
    for e in entries:
        if not block:
            if e is start_entry:
                block.append(e)
            continue
        if e.pid == start_entry.pid and e.tag == start_entry.tag:
            block.append(e)
            if len(block) >= limit:
                break
        elif e.pid == start_entry.pid:
            break  # same process moved on to another tag — trace over
    return block


def md_escape(text):
    return text.replace("|", "\\|")


def export_markdown(entries, filters_desc, when):
    lines = [
        f"# logcat export — {when}",
        "",
        f"- Filters: {filters_desc}",
        f"- Lines: {len(entries)}",
        "",
        "| Time | Tag | Message |",
        "| --- | --- | --- |",
    ]
    for e in entries:
        lines.append(f"| {e.ts} | {md_escape(e.tag)} | {md_escape(e.msg)} |")
    return "\n".join(lines) + "\n"


def export_filename(pkg_filter, now, ext="md"):
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", pkg_filter).strip("-") or "all"
    return f"logcat_{slug}_{now.strftime('%Y-%m-%d_%H-%M-%S')}.{ext}"


def export_raw(entries):
    return "\n".join(
        f"{e.ts} {e.pid} {e.tid} {e.level} {e.tag}: {e.msg}" for e in entries
    ) + "\n"


def ensure_dir(path_str):
    """Expand and create the folder; None if it cannot be created."""
    try:
        d = Path(path_str).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return None


STATE_PATH = Path.home() / ".config" / "logcat-tui" / "state.json"


def load_state():
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


OR_SPLIT_RE = re.compile(r"\s+(?:OR|AND)\s+")


def split_last_term(text):
    """'toto OR pix' -> ('toto OR ', 'pix'); 'pix' -> ('', 'pix')."""
    last_end = 0
    for m in OR_SPLIT_RE.finditer(text):
        last_end = m.end()
    return text[:last_end], text[last_end:]


def suggest(candidates, current_term, limit=8):
    """Frequency-ordered candidates containing current_term (excluding exact match)."""
    term = current_term.strip().lower()
    out = []
    for c in candidates:
        cl = c.lower()
        if term and (term not in cl or term == cl):
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


def parse_devices(adb_devices_output):
    """Parse `adb devices -l` output -> [(serial, model)] for online devices."""
    devices = []
    for line in adb_devices_output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device":
            continue
        model = ""
        for p in parts[2:]:
            if p.startswith("model:"):
                model = p.split(":", 1)[1].replace("_", " ")
        devices.append((parts[0], model))
    return devices


def avd_name(serial):
    """Readable AVD name for an emulator serial, e.g. 'Pixel 7 API 34'."""
    try:
        out = subprocess.run(
            ["adb", "-s", serial, "emu", "avd", "name"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        first = out.strip().splitlines()[0].strip()
        if first and first != "OK":
            return first.replace("_", " ")
    except Exception:
        pass
    return ""


def list_devices():
    try:
        out = subprocess.run(
            ["adb", "devices", "-l"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return []
    devices = parse_devices(out)
    return [
        (serial, (avd_name(serial) or model) if serial.startswith("emulator-") else model)
        for serial, model in devices
    ]


HELP_TEXT = r"""[b u $accent]Plain terms[/]

  [b]droid[/]                      lines containing droid (case-insensitive)

  [b]panic (again)[/]              literal text: ( ) \[ ] match exactly as typed


[b u $accent]Operators[/] — UPPERCASE only · [b $primary]AND[/] binds tighter than [b $primary]OR[/]

  [b]wifi [$primary]OR[/] coffee[/]             either term

  [b]ninja [$primary]AND[/] pirate[/]           both terms, any order

  [b]pizza [$primary]AND NOT[/] pineapple[/]    exclude a term (also at start: [b $primary]NOT[/] spam)

  [b]lost [$primary]AND[/] found [$primary]OR[/] stolen[/]   = (lost AND found) OR stolen

  lowercase stays literal: [b]"trick or treat"[/] is just text


[b u $accent]Regex[/] — wrap the term in slashes

  [i $secondary]/unicorn (seen|missing)/[/]   [b]|[/] either word

  [i $secondary]/retry \d+/[/]                [b]\d[/] digit · [b]+[/] one or more

  [i $secondary]/colou?r/[/]                  [b]?[/] previous char optional

  [i $secondary]/^Zygote/[/]                  [b]^[/] starts with · [b]$[/] ends with

  [i $secondary]/\bbugs?\b/[/]                [b]\b[/] word boundary

  case-insensitive · an invalid regex falls back to literal text
  mix freely:  [b]meltdown [$primary]OR[/] [i $secondary]/retry \d+/[/] [$primary]AND NOT[/] teads[/]


[b u $accent]Scope[/]

  [b]package[/] matches the process name; [b]tag[/] and [b]message[/] their own field

  the [b]Level[/] chip filters by severity — [b]≥[/] shows that level and worse,
  [b]=[/] shows exactly that level (switch modes inside the chip's menu)

  [b]^g[/] opens the last crash with its full stack trace


[b u $accent]Copying from the terminal[/]

  the app captures the mouse — hold a modifier while dragging to select:

  iTerm2: [b]⌥ Option[/]   ·   macOS Terminal: [b]Fn[/]   ·   kitty/Linux: [b]Shift[/]

  tip: pause first ([b]^s[/]) so lines stop moving; [b]^e[/] exports bigger chunks
"""


class ClearButton(Static):
    """The ✕ inside an input wrapper; shown only while its input has text."""

    def __init__(self, target_id, **kwargs):
        super().__init__("✕", **kwargs)
        self.target_id = target_id
        self.display = False

    def on_click(self):
        box = self.app.query_one(f"#{self.target_id}", Input)
        box.value = ""
        self.app.set_focus(box)


class PaletteClose(Static):
    """✕ in the palette's input row; exits via the palette's own escape action."""

    def on_click(self):
        self.screen._action_escape()


class ClosableCommandPalette(CommandPalette):
    CSS = """
    PaletteClose { width: 3; content-align: center middle; color: $text 50%; }
    PaletteClose:hover { color: $text; }
    """

    def on_mount(self):
        self.query_one("#--input").mount(PaletteClose("✕"))


class QueryHighlighter(Highlighter):
    """Colors operators and /regex/ terms inside the filter inputs.
    Styles are class-level so the app can re-skin them on theme change."""

    op_style = "bold magenta"
    regex_style = "italic cyan"

    def highlight(self, text):
        text.highlight_regex(r"(?<=\s)(?:OR|AND)(?=\s)", self.op_style)
        text.highlight_regex(r"(?:^|(?<=\s))NOT(?=\s)", self.op_style)
        text.highlight_regex(r"/[^/]+/", self.regex_style)


class CloseButton(Static):
    """A ✕ that dismisses its screen."""

    def __init__(self, **kwargs):
        super().__init__("✕", **kwargs)

    def on_click(self):
        self.screen.dismiss()


class HelpScreen(ModalScreen):
    CSS = """
    HelpScreen { align: center middle; }
    #help-box {
        width: 84; max-width: 95%; height: auto; max-height: 90%;
        padding: 1 3;
        background: $surface; border: round $accent;
    }
    #help-title-row { height: 1; margin-bottom: 1; }
    #help-title { width: 1fr; text-style: bold; }
    #help-close { width: 3; text-align: right; color: $text 50%; }
    #help-close:hover { color: $text; }
    #help-scroll { height: auto; max-height: 100%; }
    """

    BINDINGS = [
        ("escape", "close", "Close"),
        ("f1", "close", "Close"),
        ("ctrl+q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            with Horizontal(id="help-title-row"):
                yield Label("Filtering", id="help-title")
                yield CloseButton(id="help-close")
            with VerticalScroll(id="help-scroll"):
                yield Static(HELP_TEXT)

    def action_close(self):
        self.dismiss()


class TextViewerScreen(ModalScreen):
    """Generic wrapped-text modal (crash blocks, full-line view)."""

    CSS = """
    TextViewerScreen { align: center middle; }
    #viewer-box {
        width: 90%; height: auto; max-height: 90%; padding: 1 2;
        background: $surface; border: round $accent;
    }
    #viewer-title { text-style: bold; padding-bottom: 1; }
    #viewer-scroll { height: auto; max-height: 100%; }
    """

    BINDINGS = [
        ("escape", "close", "Close"),
        ("ctrl+q", "app.quit", "Quit"),
    ]

    def __init__(self, title, body):
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="viewer-box"):
            yield Label(self._title, id="viewer-title")
            with VerticalScroll(id="viewer-scroll"):
                yield Static(self._body)

    def action_close(self):
        self.dismiss()


class PickListScreen(ModalScreen):
    """Generic option picker; dismisses with the chosen string or None."""

    CSS = """
    PickListScreen { align: center middle; }
    #picklist-box {
        width: 64; height: auto; max-height: 18; padding: 1 2;
        background: $surface; border: round $accent;
    }
    #picklist-title { padding-bottom: 1; text-style: bold; }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+q", "app.quit", "Quit"),
    ]

    def __init__(self, title, options):
        super().__init__()
        self._title = title
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="picklist-box"):
            yield Label(self._title, id="picklist-title")
            yield OptionList(
                *[Option(o, id=str(i)) for i, o in enumerate(self._options)]
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.dismiss(self._options[int(event.option.id)])

    def action_cancel(self):
        self.dismiss(None)


class ExportDirScreen(ModalScreen):
    """Asks for the export folder; dismisses with the path string or None."""

    CSS = """
    ExportDirScreen { align: center middle; }
    #exportdir-box {
        width: 70; height: auto; padding: 1 2;
        background: $surface; border: round $accent;
    }
    #exportdir-title { padding-bottom: 1; text-style: bold; }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, current):
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="exportdir-box"):
            yield Label("Export folder (Enter to confirm)", id="exportdir-title")
            yield Input(value=self._current, id="exportdir-path")

    def on_input_submitted(self, event: Input.Submitted):
        self.dismiss(event.value.strip() or None)

    def action_cancel(self):
        self.dismiss(None)


class SavePresetScreen(ModalScreen):
    """Asks for a preset name; dismisses with it or None."""

    CSS = """
    SavePresetScreen { align: center middle; }
    #preset-box {
        width: 50; height: auto; padding: 1 2;
        background: $surface; border: round $accent;
    }
    #preset-title { padding-bottom: 1; text-style: bold; }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="preset-box"):
            yield Label("Preset name", id="preset-title")
            yield Input(placeholder="e.g. teads interstitial", id="preset-name")

    def on_input_submitted(self, event: Input.Submitted):
        self.dismiss(event.value.strip() or None)

    def action_cancel(self):
        self.dismiss(None)


LEVEL_LABELS = [
    ("V", "V — verbose"),
    ("D", "D — debug"),
    ("I", "I — info"),
    ("W", "W — warn"),
    ("E", "E — error"),
]


class LogPane(RichLog):
    """RichLog with working text extraction for mouse selection.
    (RichLog renders the selection highlight but does not implement
    get_selection, so copying would yield nothing.)"""

    def get_selection(self, selection):
        text = "\n".join(strip.text for strip in self.lines)
        return selection.extract(text), "\n"


FOOTER_ORDER = ["Clear", "Pause", "Resume", "Crash", "Export", "Device", "Filtering", "Quit"]


class OrderedFooter(Footer):
    """Footer with a fixed, focus-independent ordering of the app's bindings."""

    def compose(self) -> ComposeResult:
        if not self._bindings_ready:
            return
        bindings = [
            (binding, enabled, tooltip)
            for (_, binding, enabled, tooltip) in self.screen.active_bindings.values()
            if binding.show
        ]

        def rank(item):
            try:
                return FOOTER_ORDER.index(item[0].description)
            except ValueError:
                return len(FOOTER_ORDER)

        bindings.sort(key=rank)
        seen_actions = set()
        self.styles.grid_size_columns = len(bindings)
        for binding, enabled, tooltip in bindings:
            if binding.action in seen_actions:
                continue
            seen_actions.add(binding.action)
            yield FooterKey(
                binding.key,
                self.app.get_key_display(binding),
                binding.description,
                binding.action,
                disabled=not enabled,
                tooltip=tooltip,
            ).data_bind(compact=Footer.compact)
        if self.show_command_palette and self.app.ENABLE_COMMAND_PALETTE:
            try:
                _node, binding, enabled, tooltip = self.screen.active_bindings[
                    self.app.COMMAND_PALETTE_BINDING
                ]
            except KeyError:
                pass
            else:
                yield FooterKey(
                    binding.key,
                    self.app.get_key_display(binding),
                    binding.description,
                    binding.action,
                    classes="-command-palette",
                    disabled=not enabled,
                    tooltip=binding.tooltip or binding.description,
                )


class LevelChip(Static):
    """Clickable min-level selector in the status bar."""

    def on_click(self):
        self.app.toggle_level_menu()


class DevicePickerScreen(ModalScreen):
    CSS = """
    DevicePickerScreen { align: center middle; }
    #picker-box {
        width: 64; height: auto; max-height: 18; padding: 1 2;
        background: $surface; border: round $accent;
    }
    #picker-title { padding-bottom: 1; text-style: bold; }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+q", "app.quit", "Quit"),
    ]

    def __init__(self, devices):
        super().__init__()
        self.devices = devices

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Label("Select a device", id="picker-title")
            yield OptionList(
                *[
                    Option(f"{model or '(unknown model)'}  —  {serial}", id=serial)
                    for serial, model in self.devices
                ]
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        serial = event.option.id
        model = dict(self.devices).get(serial, "")
        self.dismiss((serial, model))

    def action_cancel(self):
        self.dismiss(None)


class LogcatTUI(App):
    TITLE = "logcat-tui"

    CSS = """
    Screen { layers: base overlay; }
    #filters { height: 3; }
    .inputwrap {
        width: 1fr; height: 3;
        border: tall $border-blurred; background: $boost;
    }
    .inputwrap:focus-within { border: tall $accent; }
    .inputwrap Input {
        width: 1fr; height: 1; border: none; padding: 0 1;
        background: transparent;
    }
    ClearButton { width: 3; height: 1; content-align: center middle; color: $text 50%; }
    ClearButton:hover { color: $text; }
    #statusbar { height: 1; }
    #status { width: 1fr; height: 1; color: $text 60%; padding: 0 1; }
    #minlevel { width: auto; height: 3; padding: 1 2; color: $text 60%; }
    #minlevel:hover { color: $text; }
    #minlevel.levelactive { color: $accent; text-style: bold; }
    Toast { width: 44; }
    RichLog { scrollbar-size-horizontal: 0; }
    #suggest {
        layer: overlay;
        display: none;
        height: auto; max-height: 10;
        background: $surface; border: round $accent;
    }
    #levelmenu {
        layer: overlay;
        display: none;
        width: 28; height: auto; max-height: 10;
        background: $surface; border: round $accent;
    }
    """

    BINDINGS = [
        Binding("ctrl+l", "clear_log", "Clear", priority=True),
        Binding("ctrl+s", "pause", "Pause", priority=True),
        Binding("ctrl+s", "resume", "Resume", priority=True),
        Binding("ctrl+g", "jump_crash", "Crash", priority=True),
        Binding("ctrl+e", "export_menu", "Export", priority=True),
        Binding("ctrl+d", "pick_device", "Device", priority=True),
        Binding("f1", "help", "Filtering", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def get_system_commands(self, screen):
        commands = [
            # device
            SystemCommand(
                "📱 Switch device",
                "Pick which connected device to stream logcat from",
                self.action_pick_device,
            ),
            SystemCommand(
                "📱 Clear device buffer",
                "adb logcat -c on the device, plus the local view",
                self.action_clear_device,
            ),
            # debugging
            SystemCommand(
                "💥 Jump to last crash",
                "Open the most recent FATAL EXCEPTION with its stack trace",
                self.action_jump_crash,
            ),
            # exports
            SystemCommand(
                "📤 Export as Markdown",
                "Save the currently filtered lines as a Markdown table",
                self.action_export_md,
            ),
            SystemCommand(
                "📤 Export raw .log",
                "Save the currently filtered lines in plain logcat format",
                self.action_export_raw,
            ),
            SystemCommand(
                "📁 Change export folder",
                "Pick where exports are saved (remembered across sessions)",
                self.action_change_export_dir,
            ),
            # filter presets
            SystemCommand(
                "💾 Save filter preset",
                "Store the current filters under a name",
                self.action_save_preset,
            ),
            SystemCommand(
                "💾 Load filter preset",
                "Apply a previously saved filter set",
                self.action_load_preset,
            ),
            SystemCommand(
                "🗑️ Delete filter preset",
                "Remove a saved filter set",
                self.action_delete_preset,
            ),
            # view
            SystemCommand(
                "📜 Toggle line wrap",
                "Wrap long log lines instead of clipping them",
                self.action_toggle_wrap,
            ),
            SystemCommand(
                "❓ Filtering help",
                "Cheatsheet: AND/OR/NOT operators and /regex/ syntax",
                self.action_help,
            ),
            SystemCommand(
                "♻️ Restore factory defaults",
                "Clear filters, presets, and all saved settings",
                self.action_factory_reset,
            ),
        ]
        # built-ins: emoji for each; Advanced (Keys/Maximize) just above Quit, Quit last
        advanced, quit_cmd = [], None
        for cmd in super().get_system_commands(screen):
            low = cmd.title.lower()
            if "keys" in low or "maximize" in low:
                advanced.append(
                    SystemCommand(
                        f"⚙️ Advanced — {cmd.title}", cmd.help, cmd.callback, cmd.discover
                    )
                )
            elif "quit" in low:
                quit_cmd = SystemCommand(f"🚪 {cmd.title}", cmd.help, cmd.callback, cmd.discover)
            elif "theme" in low:
                commands.append(SystemCommand(f"🎨 {cmd.title}", cmd.help, cmd.callback, cmd.discover))
            elif "screenshot" in low:
                commands.append(SystemCommand(f"📸 {cmd.title}", cmd.help, cmd.callback, cmd.discover))
            else:
                commands.append(SystemCommand(f"⚙️ {cmd.title}", cmd.help, cmd.callback, cmd.discover))
        commands.extend(advanced)
        if quit_cmd:
            commands.append(quit_cmd)
        # the palette sorts discovery hits alphabetically by title; an invisible
        # zero-width-space prefix (longer = earlier) pins our semantic order
        n = len(commands)
        for i, cmd in enumerate(commands):
            yield SystemCommand(
                "​" * (n - i) + cmd.title, cmd.help, cmd.callback, cmd.discover
            )

    def action_help(self):
        self.push_screen(HelpScreen())

    def action_command_palette(self):
        if self.use_command_palette and not CommandPalette.is_open(self):
            self.push_screen(ClosableCommandPalette(id="--command-palette"))

    def notify(self, message, **kwargs):
        # toasts dismiss on click; a right-aligned ✕ makes that discoverable.
        # Toast width is fixed at 44 (padding 2 each side -> 40 content cols);
        # pad so the ✕ sits at the right edge of the text's own line.
        kwargs.setdefault("markup", True)
        if len(message) <= 38:
            body = f"{escape(message)}{' ' * (39 - len(message))}✕"
        else:
            body = f"{escape(message)}\n{' ' * 39}✕"
        return super().notify(body, **kwargs)

    def __init__(self):
        super().__init__()
        self.buffer = deque(maxlen=BUFFER_MAX)
        self.queue = Queue()
        self.pid_names = {}
        self.f_pkg = []
        self.f_tag = []
        self.f_msg = []
        self.shown = 0
        self._stop = threading.Event()
        self._proc = None
        self._device_ok = False
        self.serial = None
        self.device_model = ""
        self._picker_open = False
        self._auto_picked = False
        self.tag_count = Counter()
        self.msg_count = Counter()
        self._suggest_target = None
        self._suggest_values = []
        self._debounce_timer = None
        self._cand_cache = {}
        self.paused = False
        self._pending_lines = 0
        self.crashes = []
        self.min_level = "V"
        self.level_exact = False
        self._preferred_serial = None
        self._state = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="filters"):
            for box_id, placeholder in (
                ("pkg", "package"),
                ("tag", "tag"),
                ("msg", "message"),
            ):
                with Horizontal(classes="inputwrap"):
                    yield Input(placeholder=placeholder, id=box_id)
                    yield ClearButton(box_id, id=f"clear-{box_id}")
            yield LevelChip("Level ≥ V", id="minlevel")
        yield LogPane(highlight=False, markup=False, wrap=False, max_lines=DISPLAY_MAX, id="log")
        with Horizontal(id="statusbar"):
            yield Static("starting…", id="status")
        yield OrderedFooter()
        yield OptionList(id="suggest")
        yield OptionList(id="levelmenu")

    def _rebuild_theme_styles(self):
        """Derive log/operator colors from the active theme's palette."""
        v = self.theme_variables
        self.level_styles = {
            "V": "dim",
            "D": v.get("accent", "cyan"),
            "I": v.get("success", "green"),
            "W": v.get("warning", "yellow"),
            "E": v.get("error", "red"),
            "F": f"bold {v.get('error', 'red')}",
            "S": "dim",
        }
        QueryHighlighter.op_style = f"bold {v.get('primary', 'magenta')}"
        QueryHighlighter.regex_style = f"italic {v.get('secondary', 'cyan')}"

    def _on_theme_change(self, _theme):
        self._rebuild_theme_styles()
        if hasattr(self, "log_widget"):
            self._refresh_view()
        for box in self.query("#filters Input").results(Input):
            box.refresh()  # re-run the highlighter with the new palette

    def on_mount(self):
        self.log_widget = self.query_one("#log", RichLog)
        self.status = self.query_one("#status", Static)
        self._rebuild_theme_styles()
        self.theme_changed_signal.subscribe(self, self._on_theme_change)
        self.suggest_list = self.query_one("#suggest", OptionList)
        self.level_menu = self.query_one("#levelmenu", OptionList)
        for box in self.query("#filters Input").results(Input):
            box.highlighter = QueryHighlighter()
        for box in self.query("#filters Input").results(Input):
            box.cursor_blink = False  # each blink repaints and wipes the terminal's native selection
        self._state = load_state()
        self._preferred_serial = self._state.get("last_device")
        self._apply_filter_dict(self._state.get("filters", {}))
        if self._state.get("theme"):
            try:
                self.theme = self._state["theme"]
            except Exception:
                pass
        if self._state.get("wrap"):
            self.log_widget.wrap = True
        threading.Thread(target=self._logcat_reader, daemon=True).start()
        threading.Thread(target=self._pid_mapper, daemon=True).start()
        threading.Thread(target=self._device_watcher, daemon=True).start()
        self.set_interval(0.1, self._drain)
        self.set_interval(1.0, self._update_status)

    # ---- background threads -------------------------------------------------

    def _device_watcher(self):
        """Auto-select a single device; open the picker when several are online."""
        while not self._stop.is_set():
            if self.serial is None and not self._picker_open:
                devices = list_devices()
                preferred = [d for d in devices if d[0] == self._preferred_serial]
                if len(devices) == 1:
                    self.call_from_thread(self._select_device, *devices[0])
                elif preferred and not self._auto_picked:
                    self._auto_picked = True
                    self.call_from_thread(self._select_device, *preferred[0])
                elif len(devices) > 1 and not self._auto_picked:
                    self._auto_picked = True
                    self.call_from_thread(self._open_picker, devices)
            time.sleep(2)

    def _logcat_reader(self):
        while not self._stop.is_set():
            serial = self.serial
            if serial is None:
                time.sleep(0.5)
                continue
            try:
                proc = subprocess.Popen(
                    ["adb", "-s", serial, "logcat", "-v", "threadtime"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    errors="replace",
                )
                self._proc = proc
                self._device_ok = True
                for line in proc.stdout:
                    if self._stop.is_set() or self.serial != serial:
                        break
                    self.queue.put(line.rstrip("\n"))
            except Exception:
                pass
            self._device_ok = False
            if not self._stop.is_set() and self.serial == serial:
                time.sleep(2)  # device gone — retry

    def _pid_mapper(self):
        while not self._stop.is_set():
            serial = self.serial
            if serial is None:
                time.sleep(0.5)
                continue
            try:
                out = subprocess.run(
                    ["adb", "-s", serial, "shell", "ps", "-A", "-o", "PID,NAME"],
                    capture_output=True, text=True, timeout=10,
                ).stdout
                names = {}
                for line in out.splitlines()[1:]:
                    parts = line.split(None, 1)
                    if len(parts) == 2 and parts[0].isdigit():
                        names[parts[0]] = parts[1].strip()
                if names:
                    # merge, never replace: dead pids keep their last known
                    # package so crash lines stay attributed and filterable
                    merged = dict(self.pid_names)
                    merged.update(names)
                    self.pid_names = merged
            except Exception:
                pass
            time.sleep(3)

    # ---- filtering -----------------------------------------------------------

    def _entry_visible(self, e):
        return (
            level_matches(e.level, self.min_level, self.level_exact)
            and matches(self.pid_names.get(e.pid, ""), self.f_pkg)
            and matches(e.tag, self.f_tag)
            and matches(e.msg, self.f_msg)
        )

    def _render(self, e):
        style = self.level_styles.get(e.level, "")
        pkg = self.pid_names.get(e.pid, e.pid)
        if len(pkg) > 28:
            pkg = "…" + pkg[-27:]
        return Text.assemble(
            (e.ts, "dim"),
            "  ",
            (pkg.ljust(28), "bright_black"),
            " ",
            (e.level, style or "bold"),
            "  ",
            (e.tag, "bold"),
            ": ",
            (e.msg, style if e.level in ("E", "F", "W") else ""),
        )

    def _drain(self):
        visible = []
        for _ in range(5000):
            try:
                line = self.queue.get_nowait()
            except Empty:
                break
            e = parse_line(line)
            if e is None:
                continue
            self.buffer.append(e)
            self.tag_count[e.tag] += 1
            self.msg_count[e.msg[:120]] += 1
            if len(self.msg_count) > 30_000:
                self.msg_count = Counter(dict(self.msg_count.most_common(15_000)))
            if is_crash_start(e) and not (self.crashes and self.crashes[-1].pid == e.pid):
                self.crashes.append(e)
                del self.crashes[:-20]
                pkg = self.pid_names.get(e.pid, f"pid {e.pid}")
                self.notify(f"💥 {pkg} crashed", severity="error")
            if self._entry_visible(e):
                visible.append(e)
        if not visible:
            return
        if self.paused:
            self._pending_lines += len(visible)
            return  # no repaint at all while frozen
        # render at most DISPLAY_MAX — older lines would be trimmed instantly anyway
        for e in visible[-DISPLAY_MAX:]:
            self.log_widget.write(self._render(e))
        self.shown += len(visible)
        self._update_status()

    def _refresh_view(self):
        visible = [e for e in self.buffer if self._entry_visible(e)]
        tail = visible[-DISPLAY_MAX:]
        self.log_widget.clear()
        for e in tail:
            self.log_widget.write(self._render(e))
        self.shown = len(visible)
        self._pending_lines = 0
        self._update_status()

    def _select_device(self, serial, model):
        if serial == self.serial:
            return
        old_proc = self._proc
        switching = self.serial is not None
        self.serial = serial
        self.device_model = model
        self.pid_names = {}
        if switching:
            self.action_clear_log()  # do not mix lines from two devices
        if old_proc:
            try:
                old_proc.kill()  # unblocks the reader so it respawns with the new serial
            except Exception:
                pass
        self.notify(f"Streaming from {model or serial}")
        self._update_status()

    def _open_picker(self, devices):
        self._picker_open = True

        def done(result):
            self._picker_open = False
            if result:
                self._select_device(*result)

        self.push_screen(DevicePickerScreen(devices), done)

    def action_pick_device(self):
        devices = list_devices()
        if not devices:
            self.notify("No devices connected.", severity="warning")
            return
        self._open_picker(devices)

    def _update_status(self):
        if self.serial is None:
            device = "device: waiting…"
        else:
            name = self.device_model or self.serial
            device = f"device: {name}" if self._device_ok else f"device: {name} (offline)"
        clipped = f" (last {DISPLAY_MAX} displayed)" if self.shown > DISPLAY_MAX else ""
        if self.paused:
            # no live counters while paused: a static screen is what lets the
            # terminal's native (shift-drag) selection survive
            parts = [
                Text(f"{device}   |   {self.shown} matching"),
                Text(
                    "   ⏸ paused — ^s resumes",
                    f"bold {self.theme_variables.get('warning', 'yellow')}",
                ),
            ]
        else:
            parts = [
                Text(f"{device}   |   {self.shown} matching / {len(self.buffer)} buffered{clipped}")
            ]
        if self.crashes:
            n = len(self.crashes)
            parts.append(
                Text(
                    f"   💥 {n} crash{'es' if n > 1 else ''} — ^g",
                    f"bold {self.theme_variables.get('error', 'red')}",
                )
            )
        text = Text.assemble(*parts)
        if text.plain == getattr(self, "_last_status", None):
            return  # identical content — skip the repaint
        self._last_status = text.plain
        self.status.update(text)

    # ---- events ----------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed):
        if event.input.id not in ("pkg", "tag", "msg"):
            return
        self.query_one(f"#clear-{event.input.id}").display = bool(event.value)
        # debounce: refilter once typing pauses, not on every keystroke
        self._pending_input = event.input
        if self._debounce_timer is not None:
            self._debounce_timer.stop()
        self._debounce_timer = self.set_timer(0.15, self._apply_filter_change)

    def _apply_filter_change(self):
        self._debounce_timer = None
        self.f_pkg = parse_terms(self.query_one("#pkg", Input).value)
        self.f_tag = parse_terms(self.query_one("#tag", Input).value)
        self.f_msg = parse_terms(self.query_one("#msg", Input).value)
        self._state["filters"] = self._current_filters()
        self._refresh_view()
        self._update_suggest(self._pending_input)

    # ---- autocomplete ---------------------------------------------------------

    def _candidates_for(self, input_id):
        # sorting 30k messages per keystroke is wasteful — cache for 1s
        cached = self._cand_cache.get(input_id)
        if cached and time.monotonic() - cached[0] < 1.0:
            return cached[1]
        if input_id == "pkg":
            values = sorted(set(self.pid_names.values()))
        elif input_id == "tag":
            values = [t for t, _ in self.tag_count.most_common()]
        else:
            values = [m for m, _ in self.msg_count.most_common()]
        self._cand_cache[input_id] = (time.monotonic(), values)
        return values

    def _update_suggest(self, input_widget):
        _, term = split_last_term(input_widget.value)
        if term.startswith("NOT "):
            term = term[4:]
        values = suggest(self._candidates_for(input_widget.id), term)
        if not values or not input_widget.has_focus:
            self._hide_suggest()
            return
        self._suggest_target = input_widget
        self._suggest_values = values
        self.suggest_list.clear_options()
        self.suggest_list.add_options(
            [Option(v, id=str(i)) for i, v in enumerate(values)]
        )
        region = input_widget.region
        self.suggest_list.styles.offset = (region.x, 3)
        self.suggest_list.styles.width = region.width
        self.suggest_list.display = True

    def _hide_suggest(self):
        self.suggest_list.display = False
        self._suggest_values = []

    def _apply_suggestion(self, value):
        target = self._suggest_target
        self._hide_suggest()
        if target is None:
            return
        prefix, term = split_last_term(target.value)
        if term.startswith("NOT "):
            prefix += "NOT "
        target.value = prefix + value
        target.cursor_position = len(target.value)
        self.set_focus(target)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        if event.option_list.id == "suggest":
            self._apply_suggestion(self._suggest_values[int(event.option.id)])
        elif event.option_list.id == "levelmenu":
            self.level_menu.display = False
            if event.option.id == "mode-toggle":
                self.set_min_level(self.min_level, exact=not self.level_exact)
            else:
                self.set_min_level(event.option.id)

    def on_descendant_focus(self, event):
        if not hasattr(self, "suggest_list"):
            return
        w = event.widget
        if w is not self.suggest_list and w is not self._suggest_target:
            self._hide_suggest()
        if w is not self.level_menu:
            self.level_menu.display = False

    def on_key(self, event):
        focused = self.focused
        if focused is self._suggest_target and self.suggest_list.display:
            if event.key == "down":
                self.set_focus(self.suggest_list)
                self.suggest_list.highlighted = 0
                event.stop()
            elif event.key == "escape":
                self._hide_suggest()
                event.stop()
        elif focused is self.suggest_list and event.key == "escape":
            target = self._suggest_target
            self._hide_suggest()
            if target:
                self.set_focus(target)
            event.stop()
        elif focused is self.level_menu and event.key == "escape":
            self.level_menu.display = False
            event.stop()

    def _filtered_entries_for_export(self):
        entries = [e for e in self.buffer if self._entry_visible(e)]
        if not entries:
            self.notify("Nothing to export (no matching lines).", severity="warning")
        return entries

    def _export_dir_or_prompt(self, proceed):
        """Run proceed(dir) with the saved export folder, prompting first if unset."""
        saved = self._state.get("export_dir")
        if saved:
            d = ensure_dir(saved)
            if d:
                proceed(d)
                return
            self.notify(f"Cannot use {saved} — pick another folder.", severity="warning")

        def done(value):
            if not value:
                return
            d = ensure_dir(value)
            if d is None:
                self.notify(f"Cannot create {value}.", severity="error")
                return
            self._state["export_dir"] = str(d)
            save_state(self._state)
            proceed(d)

        current = saved or str(Path.home() / "Downloads")
        self.push_screen(ExportDirScreen(current), done)

    def action_change_export_dir(self):
        current = self._state.get("export_dir") or str(Path.home() / "Downloads")

        def done(value):
            if not value:
                return
            d = ensure_dir(value)
            if d is None:
                self.notify(f"Cannot create {value}.", severity="error")
                return
            self._state["export_dir"] = str(d)
            save_state(self._state)
            self.notify(f"Exports now save to {d}")

        self.push_screen(ExportDirScreen(current), done)

    def action_export_menu(self):
        def done(choice):
            if choice == "Markdown table (.md)":
                self.action_export_md()
            elif choice == "Raw logcat (.log)":
                self.action_export_raw()

        self.push_screen(
            PickListScreen("Export", ["Markdown table (.md)", "Raw logcat (.log)"]),
            done,
        )

    def action_export_md(self):
        entries = self._filtered_entries_for_export()
        if not entries:
            return
        self._export_dir_or_prompt(lambda d: self._write_md_export(d, entries))

    def _write_md_export(self, d, entries):
        pkg = self.query_one("#pkg", Input).value.strip()
        tag = self.query_one("#tag", Input).value.strip()
        msg = self.query_one("#msg", Input).value.strip()
        filters_desc = (
            f"package=`{pkg or '*'}` tag=`{tag or '*'}` message=`{msg or '*'}`"
        )
        now = datetime.now()
        path = d / export_filename(pkg, now)
        path.write_text(
            export_markdown(entries, filters_desc, now.strftime("%Y-%m-%d %H:%M:%S")),
            encoding="utf-8",
        )
        self.notify(f"Exported {len(entries)} lines → {path}")

    def action_clear_log(self):
        self.buffer.clear()
        self.log_widget.clear()
        self.crashes = []
        self.shown = 0
        self._pending_lines = 0
        self._update_status()

    def action_pause(self):
        self.paused = True
        self._update_status()
        self.refresh_bindings()

    def action_resume(self):
        self.paused = False
        self._refresh_view()  # show what accumulated while frozen
        self._update_status()
        self.refresh_bindings()

    def check_action(self, action, parameters):
        # only one of Pause/Resume is active (and shown in the footer) at a time
        if action == "pause":
            return not self.paused
        if action == "resume":
            return self.paused
        return True

    def action_jump_crash(self):
        if not self.crashes:
            self.notify("No crash detected this session.", severity="warning")
            return
        start = self.crashes[-1]
        block = crash_block(self.buffer, start)
        if not block:
            self.notify("Last crash scrolled out of the buffer.", severity="warning")
            return
        pkg = self.pid_names.get(start.pid, f"pid {start.pid}")
        err_style = self.level_styles.get("E", "red")
        body = Text("\n").join(
            Text.assemble((e.ts, "dim"), " ", (e.msg, err_style if e.level in ("E", "F") else ""))
            for e in block
        )
        self.push_screen(TextViewerScreen(f"💥 {pkg} — {start.tag} @ {start.ts}", body))

    def set_min_level(self, level, exact=None):
        if level not in ("V", "D", "I", "W", "E"):
            return
        self.min_level = level
        if exact is not None:
            self.level_exact = exact
        sign = "=" if self.level_exact else "≥"
        chip = self.query_one("#minlevel", Static)
        chip.update(f"Level {sign} {level}")
        chip.set_class(level != "V" or self.level_exact, "levelactive")
        self._state["filters"] = self._current_filters()
        self._refresh_view()

    def toggle_level_menu(self):
        if self.level_menu.display:
            self.level_menu.display = False
            return
        chip = self.query_one("#minlevel")
        self.level_menu.clear_options()
        options = [
            Option(
                Text(
                    ("✓ " if lvl == self.min_level else "  ") + label,
                    style=self.level_styles.get(lvl, ""),
                ),
                id=lvl,
            )
            for lvl, label in LEVEL_LABELS
        ]
        mode_label = "= exactly" if self.level_exact else "≥ and above"
        options.append(
            Option(
                Text(f"  mode: {mode_label} ⇄", style=QueryHighlighter.op_style),
                id="mode-toggle",
            )
        )
        self.level_menu.add_options(options)
        # drop down just below the filter row, hugging the chip
        self.level_menu.styles.offset = (
            max(0, min(chip.region.x, self.size.width - 29)),
            3,
        )
        self.level_menu.display = True
        self.set_focus(self.level_menu)
        self.level_menu.highlighted = [l for l, _ in LEVEL_LABELS].index(self.min_level)

    def action_export_raw(self):
        entries = self._filtered_entries_for_export()
        if not entries:
            return
        self._export_dir_or_prompt(lambda d: self._write_raw_export(d, entries))

    def _write_raw_export(self, d, entries):
        pkg = self.query_one("#pkg", Input).value.strip()
        path = d / export_filename(pkg, datetime.now(), "log")
        path.write_text(export_raw(entries), encoding="utf-8")
        self.notify(f"Exported {len(entries)} lines → {path}")

    def action_clear_device(self):
        if self.serial is None:
            self.notify("No device selected.", severity="warning")
            return
        try:
            subprocess.run(
                ["adb", "-s", self.serial, "logcat", "-c"],
                capture_output=True, timeout=10,
            )
        except Exception:
            self.notify("adb logcat -c failed.", severity="error")
            return
        self.action_clear_log()
        self.notify("Device log buffer cleared.")

    def action_toggle_wrap(self):
        self.log_widget.wrap = not self.log_widget.wrap
        self._state["wrap"] = self.log_widget.wrap
        self._refresh_view()
        self.notify(f"Line wrap {'on' if self.log_widget.wrap else 'off'}.")

    # ---- presets & persistence -------------------------------------------------

    def _current_filters(self):
        return {
            "pkg": self.query_one("#pkg", Input).value,
            "tag": self.query_one("#tag", Input).value,
            "msg": self.query_one("#msg", Input).value,
            "level": self.min_level,
            "level_exact": self.level_exact,
        }

    def _apply_filter_dict(self, f):
        self.query_one("#pkg", Input).value = f.get("pkg", "")
        self.query_one("#tag", Input).value = f.get("tag", "")
        self.query_one("#msg", Input).value = f.get("msg", "")
        level = f.get("level", "V")
        if f.get("errors") and LEVELS.index(level) < LEVELS.index("E"):
            level = "E"  # legacy "errors only" checkbox state
        exact = f.get("level_exact", False)
        if level != self.min_level or exact != self.level_exact:
            self.set_min_level(level, exact)

    def _do_factory_reset(self):
        self._state = {}
        try:
            STATE_PATH.unlink()
        except Exception:
            pass
        self._preferred_serial = None
        self._apply_filter_dict({})  # empty boxes, Level ≥ V
        if self.log_widget.wrap:
            self.log_widget.wrap = False
        self.theme = "textual-dark"
        self._refresh_view()
        self.notify("Factory defaults restored.")

    def action_factory_reset(self):
        def done(choice):
            if choice == "Yes, reset everything":
                self._do_factory_reset()

        self.push_screen(
            PickListScreen(
                "Restore factory defaults? Presets and settings will be lost.",
                ["Yes, reset everything", "Cancel"],
            ),
            done,
        )

    def action_save_preset(self):
        def done(name):
            if not name:
                return
            self._state.setdefault("presets", {})[name] = self._current_filters()
            save_state(self._state)
            self.notify(f"Preset “{name}” saved.")

        self.push_screen(SavePresetScreen(), done)

    def _pick_preset(self, title, callback):
        names = sorted(self._state.get("presets", {}))
        if not names:
            self.notify("No presets saved yet.", severity="warning")
            return
        self.push_screen(PickListScreen(title, names), callback)

    def action_load_preset(self):
        def done(name):
            if name:
                self._apply_filter_dict(self._state["presets"][name])
                self.notify(f"Preset “{name}” applied.")

        self._pick_preset("Load preset", done)

    def action_delete_preset(self):
        def done(name):
            if name:
                del self._state["presets"][name]
                save_state(self._state)
                self.notify(f"Preset “{name}” deleted.")

        self._pick_preset("Delete preset", done)

    def on_unmount(self):
        self._stop.set()
        if self.serial:
            self._state["last_device"] = self.serial
        self._state["theme"] = self.theme
        save_state(self._state)
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    LogcatTUI().run()
