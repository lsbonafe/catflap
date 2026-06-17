"""The Catflap Textual application — the interactive TUI.

subprocess is imported here (not via a wrapper) so tests can patch
``catflap.subprocess.{Popen,run}`` — the shared module object — to intercept
recording. adb/state are imported as modules so test monkeypatches on
``catflap.adb.list_devices`` / ``catflap.state.STATE_PATH`` take effect."""

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
from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.command import CommandPalette
from textual.containers import Horizontal
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from catflap import adb, state
from catflap.adb import logcat_cmd, parse_permissions, parse_foreground, BUFFER_CHOICES
from catflap.crash import is_crash_start, crash_block, crash_package, banner_diff
from catflap.entry import Entry, parse_line, LEVELS, LEVEL_STYLES, level_matches
from catflap.export import (
    md_escape, export_markdown, export_filename, export_raw, ensure_dir,
)
from catflap.filtering import (
    compile_term, parse_terms, matches, parse_query, query_matches,
    highlight_patterns, FIELD_ALIASES, KEY_RE, split_query_token, parse_token,
    suggest, split_last_term, _migrate_query,
)
from catflap.state import load_state, save_state
from catflap.widgets import (
    HELP_TEXT, LEVEL_LABELS, FOOTER_ORDER,
    ClearButton, DropdownArrow, ClosableCommandPalette, QueryHighlighter,
    HelpScreen, TextViewerScreen, PickListScreen, ExportDirScreen,
    FilterPickScreen, TextPromptScreen, SavePresetScreen, LogPane, OrderedFooter,
    LevelChip, RecBar, DevicePickerScreen,
)

BUFFER_MAX = 20_000   # parsed lines kept in memory
DISPLAY_MAX = 2_000   # lines re-rendered after a filter change


class Catflap(App):
    TITLE = "catflap"

    CSS = """
    Screen { layers: base overlay; }
    #filters { height: 3; }
    .inputwrap {
        width: 1fr; height: 3;
        border: tall $border-blurred; background: $boost;
    }
    /* package box hidden for now — filter by package: in the query box.
       kept in the DOM (not removed) so its filter/foreground/preset wiring
       still works and it can be brought back by dropping this display rule. */
    #wrap-pkg { display: none; }
    #wrap-query { width: 1fr; }
    .inputwrap:focus-within { border: tall $accent; }
    .inputwrap Input {
        width: 1fr; min-width: 16; height: 1; border: none; padding: 0 1;
        background: transparent;
    }
    ClearButton { width: 3; height: 1; content-align: center middle; color: $text 50%; }
    ClearButton:hover { color: $text; }
    DropdownArrow { width: 3; height: 1; content-align: center middle; color: $text 50%; }
    DropdownArrow:hover { color: $text; }
    #statusbar { height: 1; }
    #status { width: 1fr; height: 1; color: $text 60%; padding: 0 1; }
    #searchrow { height: 1; display: none; }
    #search-slash { width: 2; content-align: center middle; color: $accent; text-style: bold; }
    #searchbar { width: 1fr; height: 1; border: none; padding: 0 1; background: transparent; }
    #search-count { width: auto; height: 1; padding: 0 1; color: $text 60%; }
    #brand { width: auto; height: 1; padding: 0 1; color: $accent; text-style: bold; }
    #minlevel {
        width: auto; height: 3; padding: 0 2; color: $text 60%;
        content-align: center middle;
        border: tall $border-blurred; background: $boost;
    }
    #minlevel:hover { color: $text; }
    #minlevel:focus { border: tall $accent; color: $text; text-style: bold; }
    #minlevel.levelactive { text-style: bold; }
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
    #recbar {
        layer: overlay;
        display: none;
        width: auto; height: 1;
        padding: 0 1;
        background: $error; color: $text; text-style: bold;
    }
    #recbar:hover { background: $error 80%; }
    """

    BINDINGS = [
        Binding("ctrl+l", "clear_log", "Clear", priority=True),
        Binding("ctrl+s", "pause", "Pause", priority=True),
        Binding("ctrl+s", "resume", "Resume", priority=True),
        Binding("ctrl+g", "jump_crash", "Crash", priority=True),
        Binding("ctrl+e", "export_menu", "Export", priority=True),
        Binding("ctrl+d", "device_menu", "Device", priority=True),
        Binding("ctrl+b", "pick_buffer", "Buffer", priority=True),
        Binding("ctrl+a", "adb_menu", "ADB", priority=True),
        Binding("f1", "help", "Filtering", show=False, priority=True),
        Binding("f2", "level_menu", "Level", show=False, priority=True),
        # F3/F4, not Ctrl+R: terminals/shells often grab Ctrl+R for reverse-search
        # so it never reaches the app — F-keys fit F1/F2 and aren't intercepted
        Binding("f3", "toggle_record", "Record", show=False, priority=True),
        Binding("f4", "device_screenshot", "Screenshot", show=False, priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def get_system_commands(self, screen):
        commands = [
            SystemCommand(
                "❓ Filtering help (F1)",
                "Cheatsheet: AND/OR/NOT operators and /regex/ syntax",
                self.action_help,
            ),
            # device
            SystemCommand(
                "📱 Switch device (^d)",
                "Pick which connected device to stream logcat from",
                self.action_pick_device,
            ),
            SystemCommand(
                "📱 Clear device buffer",
                "adb logcat -c on the device, plus the local view",
                self.action_clear_device,
            ),
            SystemCommand(
                "📦 Install APK",
                "Pick an .apk file to install on the current device (adb install -r)",
                self._install_apk_flow,
            ),
            SystemCommand(
                "🖥  Mirror screen (scrcpy)",
                "Open the device screen in a controllable window (requires scrcpy)",
                self._mirror_screen,
            ),
            SystemCommand(
                "📸 Device screenshot (F4)",
                "Capture the device screen (PNG) to the export folder",
                self.action_device_screenshot,
            ),
            SystemCommand(
                "🎬 Device screen record (F3)",
                "Start, or stop & save, a recording of the device screen",
                self.action_toggle_record,
            ),
            SystemCommand(
                "📚 Switch log buffer (^b)",
                "Stream crash, events, radio, or the default main+system buffers",
                self.action_pick_buffer,
            ),
            SystemCommand(
                "📱 Filter on foreground app",
                "Set the package filter to the app currently on screen",
                self.adopt_foreground,
            ),
            SystemCommand(
                "🤖 ADB operations (^a)",
                "Start/kill/clear/uninstall the target app, permissions, deep links, screenshots",
                self.action_adb_menu,
            ),
            SystemCommand(
                "🎚 Set minimum level (F2)",
                "Open the level menu: V/D/I/W/E threshold or exact mode",
                self.action_level_menu,
            ),
            # debugging
            SystemCommand(
                "💥 Jump to last crash (^g)",
                "Open the most recent FATAL EXCEPTION with its stack trace",
                self.action_jump_crash,
            ),
            SystemCommand(
                "🔍 Search displayed lines (/)",
                "Find and jump to matches in the filtered scrollback",
                self.action_search,
            ),
            # exports
            SystemCommand(
                "📤 Export as Markdown (^e)",
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
                "♻️ Restore factory defaults",
                "Clear filters, presets, and all saved settings",
                self.action_factory_reset,
            ),
        ]
        # built-ins: emoji for each; Keys/Maximize dropped (footer + F1 cover them); Quit last
        quit_cmd = None
        for cmd in super().get_system_commands(screen):
            low = cmd.title.lower()
            if "keys" in low or "maximize" in low:
                continue
            elif "quit" in low:
                quit_cmd = SystemCommand(f"🚪 {cmd.title}", cmd.help, cmd.callback, cmd.discover)
            elif "theme" in low:
                commands.append(SystemCommand(f"🎨 {cmd.title}", cmd.help, cmd.callback, cmd.discover))
            elif "screenshot" in low:
                # Textual's built-in saves an SVG of catflap's own UI — rename it
                # so it isn't confused with the device screenshot (ADB/Device menu)
                commands.append(SystemCommand(
                    "🖼  Save catflap UI snapshot (SVG)",
                    "Save an SVG image of catflap's own terminal window (not the device screen)",
                    cmd.callback, cmd.discover,
                ))
            else:
                commands.append(SystemCommand(f"⚙️ {cmd.title}", cmd.help, cmd.callback, cmd.discover))
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

    def action_level_menu(self):
        self.toggle_level_menu()

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
        self._live_pids = set()  # live pids from the previous ps poll (mapper thread)
        self.f_pkg = []
        self.f_query = []
        self._hl_patterns = ([], [])  # (tag_patterns, msg_patterns) for log highlights
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
        self._adb_target = None
        self._record_proc = None
        self._record_start = 0.0   # monotonic time the recording began
        self._rec_timer = None     # interval that ticks the bottom-right REC bar
        self._last_deeplink = ""
        self.min_level = "V"
        self.level_exact = False
        self.foreground_pkg = None
        self.log_buffers = None  # None = adb default (main+system)
        self.buffer_label = ""
        self._search_active = False
        self._search_autopause = False
        self._search_entries = []
        self._search_matches = []
        self._search_pos = -1
        self._preferred_serial = None
        self._state = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="filters"):
            with Horizontal(classes="inputwrap", id="wrap-pkg"):
                # hidden box — non-focusable from the start so Textual's
                # auto-focus never lands on it and pops a dropdown for an
                # invisible field (see #wrap-pkg display:none in the CSS)
                pkg = Input(placeholder="package", id="pkg")
                pkg.can_focus = False
                yield pkg
                yield ClearButton("pkg", id="clear-pkg")
                yield DropdownArrow(id="pkg-arrow")
            with Horizontal(classes="inputwrap", id="wrap-query"):
                yield Input(placeholder="package:  tag:  message:  /regex/  — or just type", id="query")
                yield ClearButton("query", id="clear-query")
            yield LevelChip("Level ≥ V", id="minlevel")
        yield LogPane(highlight=False, markup=False, wrap=False, max_lines=DISPLAY_MAX, id="log")
        with Horizontal(id="statusbar"):
            yield Static("starting…", id="status")
            yield Static("🐈 𝒸𝒶𝓉𝒻𝓁𝒶𝓅", id="brand")
        with Horizontal(id="searchrow"):
            yield Static("/", id="search-slash")
            yield Input(placeholder="search — enter: jump · n/N: older/newer · esc: close", id="searchbar")
            yield Static("", id="search-count")
        yield OrderedFooter()
        yield OptionList(id="suggest")
        yield OptionList(id="levelmenu")
        yield RecBar(id="recbar")

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
        QueryHighlighter.key_style = f"bold {v.get('accent', 'cyan')}"
        # filter-match highlights in the log: theme colors used with `reverse`,
        # which paints the term as a colored background with auto-contrasting
        # text (readable on light or dark themes). accent vs primary are the two
        # most visually distinct palette colors not already used as backgrounds.
        self.tag_hl_style = f"reverse bold {v.get('accent', 'cyan')}"
        self.msg_hl_style = f"reverse bold {v.get('primary', 'magenta')}"

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
        # start every session with a clean filter — last session's query/level
        # should not silently carry over (theme, presets, export dir etc. still
        # persist). Saved filters live on only via named presets.
        self._apply_filter_dict({})
        self.set_min_level(self.min_level, self.level_exact)  # paint the chip colour
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
        threading.Thread(target=self._foreground_watcher, daemon=True).start()
        self.set_interval(0.1, self._drain)
        self.set_interval(1.0, self._update_status)

    # ---- background threads -------------------------------------------------

    def _device_watcher(self):
        """Auto-select a single device; open the picker when several are online."""
        while not self._stop.is_set():
            if self.serial is None and not self._picker_open:
                devices = adb.list_devices()
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
            buffers = self.log_buffers
            if serial is None:
                time.sleep(0.5)
                continue
            try:
                proc = subprocess.Popen(
                    logcat_cmd(serial, buffers, tail=True),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    errors="replace",
                )
                self._proc = proc
                self._device_ok = True
                for line in proc.stdout:
                    if (
                        self._stop.is_set()
                        or self.serial != serial
                        or self.log_buffers != buffers
                    ):
                        break
                    self.queue.put(line.rstrip("\n"))
            except Exception:
                pass
            self._device_ok = False
            if not self._stop.is_set() and self.serial == serial and self.log_buffers == buffers:
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
                    # diff against the previous poll for process STARTED/ENDED
                    # banners — but only once we have a baseline (skip first poll,
                    # else the whole running process table floods as STARTED)
                    if self._live_pids:
                        started, ended = banner_diff(
                            self._live_pids, names, self.pid_names, self.f_pkg
                        )
                        for pid, pkg in started:
                            self.call_from_thread(self._emit_banner, pid, pkg, "STARTED")
                        for pid, pkg in ended:
                            self.call_from_thread(self._emit_banner, pid, pkg, "ENDED")
                    self._live_pids = set(names)
            except Exception:
                pass
            time.sleep(3)

    def _foreground_watcher(self):
        while not self._stop.is_set():
            serial = self.serial
            if serial is None:
                time.sleep(0.5)
                continue
            try:
                out = subprocess.run(
                    ["adb", "-s", serial, "shell", "dumpsys", "activity", "activities"],
                    capture_output=True, text=True, timeout=10,
                ).stdout
                pkg = parse_foreground(out)
                if pkg != self.foreground_pkg and self.serial == serial:
                    self.foreground_pkg = pkg
                    self.call_from_thread(self._on_foreground_change)
            except Exception:
                pass
            time.sleep(2)

    def _on_foreground_change(self):
        # surface the foreground app in the query box's dropdown once it's known
        # (the clean replacement for the old startup package picker), and keep a
        # package: completion in sync if one is open
        q = self.query_one("#query", Input)
        if q.has_focus and (not q.value or q.value.lower().startswith("package:")):
            self._update_suggest(q)

    def adopt_foreground(self):
        pkg = self.foreground_pkg
        if not pkg:
            self.notify("No foreground app detected yet.", severity="warning")
            return
        # the package box is hidden — scope via the visible query box so the
        # active filter is shown. Append package: if a query is already there.
        box = self.query_one("#query", Input)
        token = f"package:{pkg}"
        existing = box.value.strip()
        if token in existing:
            return
        box.value = f"{existing} {token}".strip() if existing else token
        box.cursor_position = len(box.value)
        self.set_focus(box)

    # ---- filtering -----------------------------------------------------------

    def _entry_visible(self, e):
        if e.kind == "proc":
            # a process banner shows only while its package passes the package
            # filter — re-evaluated every refresh, so it hides/reappears with it
            return bool(self.f_pkg) and matches(self.pid_names.get(e.pid, ""), self.f_pkg)
        pkg = self.pid_names.get(e.pid, "")
        return (
            level_matches(e.level, self.min_level, self.level_exact)
            and matches(pkg, self.f_pkg)
            and query_matches(e.tag, e.msg, pkg, self.f_query)
        )

    def _render(self, e, highlight=False):
        if e.kind == "proc":
            w = self.log_widget.size.width or 80
            body = f" {e.msg} "
            pad = max(0, (w - len(body)) // 2)
            text = Text(
                "─" * pad + body + "─" * max(0, w - pad - len(body)),
                style=f"bold {self.theme_variables.get('accent', 'cyan')}",
            )
            if highlight:
                text.stylize("reverse")
            return text
        style = self.level_styles.get(e.level, "")
        pkg = self.pid_names.get(e.pid, e.pid)
        if len(pkg) > 28:
            pkg = "…" + pkg[-27:]
        # build tag/msg as their own fragments so filter-match highlighting is
        # scoped to the right field (and only the matched substring, not the
        # whole field) before assembling the line
        tag_frag = Text(e.tag, style="bold")
        msg_frag = Text(e.msg, style=style if e.level in ("E", "F", "W") else "")
        tag_pats, msg_pats = self._hl_patterns
        for p in tag_pats:
            tag_frag.highlight_regex(p, self.tag_hl_style)
        for p in msg_pats:
            msg_frag.highlight_regex(p, self.msg_hl_style)
        text = Text.assemble(
            (e.ts, "dim"),
            "  ",
            (pkg.ljust(28), "bright_black"),
            " ",
            (e.level, style or "bold"),
            "  ",
            tag_frag,
            ": ",
            msg_frag,
        )
        if highlight:
            text.stylize("reverse")
        return text

    def _emit_banner(self, pid, pkg, which):
        """Append a synthetic PROCESS STARTED/ENDED banner. Main thread only
        (the mapper hands it over via call_from_thread). Mirrors _drain's tail
        so pause / shown / status behave exactly like a real line."""
        ms = int((time.time() % 1) * 1000)
        ts = time.strftime("%m-%d %H:%M:%S") + f".{ms:03d}"
        e = Entry(
            ts=ts, pid=pid, tid="", level="", tag="proc",
            msg=f"PROCESS {which} ({pid}) for package {pkg}", kind="proc",
        )
        self.buffer.append(e)
        if not self._entry_visible(e):
            return  # filter changed between the poll and this callback
        if self.paused:
            self._pending_lines += 1
        else:
            self.log_widget.write(self._render(e))
            self.shown += 1
            self._update_status()

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
        self.foreground_pkg = None
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

        self.push_screen(DevicePickerScreen(devices, current=self.serial), done)

    def action_pick_buffer(self):
        if self.serial is None:
            self.notify("No device selected.", severity="warning")
            return
        labels = [
            ("✓ " if buffers == self.log_buffers else "  ") + label
            for label, buffers in BUFFER_CHOICES
        ]

        def done(choice):
            if not choice:
                return
            label, buffers = BUFFER_CHOICES[labels.index(choice)]
            self._set_buffers(buffers, label)

        self.push_screen(PickListScreen("Log buffer", labels), done)

    def _set_buffers(self, buffers, label):
        if buffers == self.log_buffers:
            return
        self.log_buffers = buffers
        self.buffer_label = label.split(" — ")[0].split(" (")[0]
        self.action_clear_log()  # different stream — do not mix
        if self._proc:
            try:
                self._proc.kill()  # reader respawns with the new -b flags
            except Exception:
                pass
        self.notify(f"Streaming buffer: {self.buffer_label}")
        self._update_status()

    def action_pick_device(self):
        devices = adb.list_devices()
        if not devices:
            self.notify("No devices connected.", severity="warning")
            return
        self._open_picker(devices)

    def action_device_menu(self):
        name = self.device_model or self.serial
        title = f"Device — {name}" if self.serial else "No devices connected"
        recording = self._record_proc is not None

        def done(choice):
            if not choice:
                return
            if choice.startswith("🔄"):
                self.action_pick_device()
            elif choice.startswith("📦"):
                self._install_apk_flow()
            elif choice.startswith("🖥"):
                self._mirror_screen()
            elif choice.startswith("📸"):
                self._export_dir_or_prompt(self._take_screenshot)
            elif choice.startswith("🎬") or choice.startswith("⏹"):
                self.action_toggle_record()

        self.push_screen(
            PickListScreen(
                title,
                [
                    "🔄 Switch streaming device",
                    "📦 Install APK…",
                    "🖥  Mirror screen (scrcpy)",
                    "📸 Screenshot (F4)",
                    "⏹ Stop recording & save (F3)" if recording else "🎬 Start screen record (F3)",
                ],
            ),
            done,
        )

    def _mirror_screen(self):
        """Open scrcpy on the current device in its own window; control stays with it."""
        if self.serial is None:
            self.notify("No devices connected.", severity="warning")
            return
        if shutil.which("scrcpy") is None:
            self.notify("scrcpy not found — install with: brew install scrcpy", severity="warning")
            return
        try:
            subprocess.Popen(
                ["scrcpy", "-s", self.serial],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.notify(f"Mirroring {self.device_model or self.serial}…")
        except Exception as ex:
            self.notify(f"scrcpy failed: {ex}", severity="error")

    # ---- install apk -------------------------------------------------------------

    def _install_apk_flow(self):
        """Installs onto the streaming device; switch device first to target another."""
        if self.serial is None:
            self.notify("No devices connected.", severity="warning")
            return
        self._pick_apk_then_install(self.serial, self.device_model)

    def _pick_apk_then_install(self, serial, model):
        if sys.platform == "darwin":
            def work():
                try:
                    r = subprocess.run(
                        ["osascript", "-e",
                         'POSIX path of (choose file with prompt "Choose an APK to install")'],
                        capture_output=True, text=True, timeout=300,
                    )
                    path = r.stdout.strip()
                    if r.returncode == 0 and path:
                        self.call_from_thread(self._install_apk, serial, model, path)
                except Exception as ex:
                    self.call_from_thread(self.notify, f"File dialog failed: {ex}", severity="error")

            threading.Thread(target=work, daemon=True).start()
            return

        def done(path):
            if path:
                self._install_apk(serial, model, path)

        self.push_screen(TextPromptScreen("APK path", "", "/path/to/app.apk"), done)

    def _install_apk(self, serial, model, path):
        p = Path(path).expanduser()
        if not p.is_file() or p.suffix.lower() != ".apk":
            self.notify(f"Not an APK: {p.name or path}", severity="error")
            return
        name = model or serial
        self.notify(f"Installing {p.name} on {name}…")

        def work():
            try:
                r = subprocess.run(
                    ["adb", "-s", serial, "install", "-r", str(p)],
                    capture_output=True, text=True, timeout=300,
                )
                out = f"{r.stdout}\n{r.stderr}"
                ok = r.returncode == 0 and "Success" in out
                msg = (
                    f"Installed {p.name} on {name}"
                    if ok
                    else (out.strip().splitlines() or ["install failed"])[-1]
                )
                self.call_from_thread(
                    self.notify, msg, severity="information" if ok else "error"
                )
            except Exception as ex:
                self.call_from_thread(self.notify, f"adb failed: {ex}", severity="error")

        threading.Thread(target=work, daemon=True).start()

    def _update_status(self):
        if self.serial is None:
            device = "device: waiting…"
        else:
            name = self.device_model or self.serial
            device = f"device: {name}" if self._device_ok else f"device: {name} (offline)"
        if self.serial is not None:
            device += f"   |   buffer: {self.buffer_label or 'main+system'}"
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
        if event.input.id not in ("pkg", "query"):
            return
        self.query_one(f"#clear-{event.input.id}").display = bool(event.value)
        # debounce: refilter once typing pauses, not on every keystroke
        self._pending_input = event.input
        if self._debounce_timer is not None:
            self._debounce_timer.stop()
        self._debounce_timer = self.set_timer(0.15, self._apply_filter_change)

    def _apply_filters_now(self):
        """Parse both boxes and re-render. Shared by the debounce and Enter."""
        self.f_pkg = parse_terms(self.query_one("#pkg", Input).value)
        self.f_query = parse_query(self.query_one("#query", Input).value)
        self._hl_patterns = highlight_patterns(self.f_query)
        self._refresh_view()

    def _apply_filter_change(self):
        self._debounce_timer = None
        self._apply_filters_now()
        self._update_suggest(self._pending_input)

    # ---- autocomplete ---------------------------------------------------------

    def _candidates_for(self, kind):
        # sorting 30k messages per keystroke is wasteful — cache for 1s
        cached = self._cand_cache.get(kind)
        if cached and time.monotonic() - cached[0] < 1.0:
            return cached[1]
        if kind == "pkg":
            values = sorted(set(self.pid_names.values()))
        elif kind == "tag":
            values = [t for t, _ in self.tag_count.most_common()]
        else:  # msg
            values = [m for m, _ in self.msg_count.most_common()]
        self._cand_cache[kind] = (time.monotonic(), values)
        return values

    def _update_suggest(self, input_widget):
        if input_widget is None or not input_widget.has_focus:
            self._hide_suggest()
            return
        if input_widget.id == "query":
            items = self._query_suggestions(input_widget.value)
        else:
            items = self._pkg_suggestions(input_widget.value)
        if not items:
            self._hide_suggest()
            return
        self._suggest_target = input_widget
        # each item: (display_text_or_Text, replacement_value)
        self._suggest_values = [repl for _disp, repl in items]
        self.suggest_list.clear_options()
        self.suggest_list.add_options(
            [Option(disp, id=str(i)) for i, (disp, _repl) in enumerate(items)]
        )
        region = input_widget.parent.region
        width = max(region.width, 28)
        self.suggest_list.styles.offset = (max(0, min(region.x, self.size.width - width)), 3)
        self.suggest_list.styles.width = width
        self.suggest_list.display = True

    def _pkg_suggestions(self, value):
        """(display, replacement) pairs for the package box — same as before,
        with the foreground app pinned on top. Replacement preserves any
        OR/NOT prefix the user already typed."""
        prefix, term = split_last_term(value)
        if term.startswith("NOT "):
            prefix += "NOT "
            term = term[4:]
        values = suggest(self._candidates_for("pkg"), term)
        fg = self.foreground_pkg
        t = term.strip().lower()
        if fg and (not t or t in fg.lower()) and t != fg.lower():
            values = ([fg] + [v for v in values if v != fg])[:8]
        out = []
        for v in values:
            if v == fg:
                disp = Text.assemble("📱 ", v, ("  foreground", "dim italic"))
            else:
                disp = Text(v)
            out.append((disp, prefix + v))
        return out

    def _query_suggestions(self, value):
        """(display, replacement) pairs for the unified query box.

        - After a key (tag:/message:/package:): complete that field's values.
        - For a bare term: offer matching tags & messages, each promoted to its
          reserved form (tag:… / message:…) so accepting it scopes the term."""
        prefix, token = split_query_token(value)
        _negated, key, _op_token, partial = parse_token(token)
        partial = partial.strip()
        # rich Text can't resolve $-theme vars; pull a concrete key color
        key_color = f"bold {self.theme_variables.get('accent', 'cyan')}"
        out = []
        if key:  # completing a scoped value — suggest that field's candidates
            cand_kind = FIELD_ALIASES[key]  # already 'tag' | 'msg' | 'pkg'
            keytext = token[: KEY_RE.match(token).end()]  # e.g. "-tag~:"
            values = suggest(self._candidates_for(cand_kind), partial)
            # for package:, pin the device's foreground app on top with its
            # label — same hint the dedicated package box gives (there is no
            # 'mine' here, so the foreground app is the natural starting point)
            fg = self.foreground_pkg if cand_kind == "pkg" else None
            t = partial.strip().lower()
            if fg and (not t or t in fg.lower()) and t != fg.lower():
                values = ([fg] + [v for v in values if v != fg])[:8]
            for v in values:
                if v == fg:
                    disp = Text.assemble("📱 ", v, ("  foreground", "dim italic"))
                else:
                    disp = Text(v)
                out.append((disp, prefix + keytext + v))
            return out
        # bare term — promote to reserved forms across tag + message
        bare = partial
        if bare == "" and not prefix and self.foreground_pkg:
            # empty box: offer the device's foreground app as a one-click start
            fg = self.foreground_pkg
            return [(
                Text.assemble("📱 ", ("package:", key_color), fg, ("  foreground", "dim italic")),
                "package:" + fg,
            )]
        if bare in ("", "NOT") or bare.startswith("/"):
            return []  # nothing useful to promote yet (or an inline regex)
        # if the word is the start of a reserved key, offer to complete the key
        # itself (so typing "package" suggests "package:" before treating it as
        # a literal search term)
        low = bare.lower()
        for kw in ("tag", "message", "package"):
            if kw.startswith(low):
                hint = " — then pick the app" if kw == "package" else " — scope to that field"
                out.append((
                    Text.assemble((kw + ":", key_color), (hint, "dim italic")),
                    prefix + kw + ":",
                ))
        for v in suggest(self._candidates_for("tag"), bare, limit=5):
            disp = Text.assemble(("tag:", key_color), v)
            out.append((disp, prefix + "tag:" + v))
        for v in suggest(self._candidates_for("msg"), bare, limit=5):
            label = v if len(v) <= 60 else v[:59] + "…"
            disp = Text.assemble(("message:", key_color), label)
            out.append((disp, prefix + "message:" + v))
        return out[:8]

    def _hide_suggest(self):
        self.suggest_list.display = False
        self._suggest_values = []

    def _apply_suggestion(self, value):
        # `value` is the full replacement string for the box (prefix already included)
        target = self._suggest_target
        self._hide_suggest()
        if target is None:
            return
        target.value = value
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
        # close the level menu when focus moves away — but NOT when it lands on
        # the chip itself, whose click is about to toggle the menu (otherwise
        # the focus-close and the toggle race and cancel out, so the menu
        # appears not to open)
        if w is not self.level_menu and not isinstance(w, LevelChip):
            self.level_menu.display = False
        if isinstance(w, Input) and w.id == "pkg" and not self._pkg_hidden():
            self._update_suggest(w)  # the package box drops down on focus
        # focusing an empty query box surfaces the foreground app as a one-click
        # start (the clean replacement for the old startup package picker)
        if isinstance(w, Input) and w.id == "query" and not w.value and self.foreground_pkg:
            self._update_suggest(w)

    def _pkg_hidden(self):
        return self.query_one("#wrap-pkg").styles.display == "none"

    def on_click(self, event):
        # reopen on click even when the box already has focus (e.g. after escape)
        w = event.widget
        if (isinstance(w, Input) and w.id == "pkg"
                and not self._pkg_hidden() and not self.suggest_list.display):
            self._update_suggest(w)

    def on_key(self, event):
        focused = self.focused
        if focused is self._suggest_target and self.suggest_list.display:
            if event.key == "down":
                self.set_focus(self.suggest_list)
                self.suggest_list.highlighted = 0
                event.prevent_default()  # keep the list's own ↓ binding from skipping to item 1
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
        elif event.character == "/" and (focused is self.log_widget or focused is None):
            self.action_search()
            event.stop()
        elif self._search_active and focused is self.log_widget and event.key in ("n", "N"):
            if self._search_matches:
                step = -1 if event.key == "n" else 1
                self._search_pos = (self._search_pos + step) % len(self._search_matches)
                self._jump_to_match()
            event.stop()
        elif self._search_active and event.key == "escape" and (
            focused is self.log_widget or (focused is not None and focused.id == "searchbar")
        ):
            self._close_search()
            event.stop()

    # ---- search in scrollback ----------------------------------------------------

    def action_search(self):
        if not self._search_active:
            self._search_active = True
            self._search_autopause = not self.paused
            if not self.paused:
                self.action_pause()  # lines must hold still while navigating
            self._search_entries = [e for e in self.buffer if self._entry_visible(e)]
            self.query_one("#statusbar").display = False
            self.query_one("#searchrow").display = True
        self.set_focus(self.query_one("#searchbar", Input))

    def _close_search(self):
        if not self._search_active:
            return
        self._search_active = False
        self.query_one("#searchrow").display = False
        self.query_one("#statusbar").display = True
        self.query_one("#searchbar", Input).value = ""
        self.query_one("#search-count", Static).update("")
        self._search_entries = []
        self._search_matches = []
        self.set_focus(self.log_widget)
        if self._search_autopause and self.paused:
            self.action_resume()  # re-renders the live tail, dropping the highlight
        else:
            self._refresh_view()

    def _run_search(self, term):
        pattern = compile_term(term)
        self._search_matches = [
            i for i, e in enumerate(self._search_entries)
            if pattern.search(e.msg) or pattern.search(e.tag)
            or pattern.search(self.pid_names.get(e.pid, ""))
        ]
        if not self._search_matches:
            self.query_one("#search-count", Static).update("no matches")
            return
        self._search_pos = len(self._search_matches) - 1  # most recent first
        self._jump_to_match()
        self.set_focus(self.log_widget)  # so n/N navigate right away

    def _jump_to_match(self):
        idx = self._search_matches[self._search_pos]
        start = max(0, idx - DISPLAY_MAX // 2)
        window = self._search_entries[start:start + DISPLAY_MAX]
        self.log_widget.clear()
        for j, e in enumerate(window, start=start):
            self.log_widget.write(self._render(e, highlight=(j == idx)), scroll_end=False)
        line = idx - start
        self.log_widget.scroll_to(
            y=max(0, line - self.log_widget.size.height // 2), animate=False
        )
        self.query_one("#search-count", Static).update(
            f"{self._search_pos + 1}/{len(self._search_matches)}"
        )

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == "searchbar" and event.value.strip():
            self._run_search(event.value.strip())
        elif event.input.id in ("pkg", "query"):
            # Enter submits the query as typed — apply now, don't wait for the
            # debounce, and dismiss the suggestions (no forced pick)
            if self._debounce_timer is not None:
                self._debounce_timer.stop()
                self._debounce_timer = None
            self._hide_suggest()
            self._apply_filters_now()

    def _filtered_entries_for_export(self):
        # exclude synthetic process banners — their empty level/tid would render
        # as garbage in the markdown table and the raw "ts pid tid level tag:" line
        entries = [e for e in self.buffer if e.kind != "proc" and self._entry_visible(e)]
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
        query = self.query_one("#query", Input).value.strip()
        filters_desc = f"package=`{pkg or '*'}` query=`{query or '*'}`"
        now = datetime.now()
        path = d / export_filename(pkg, now)
        path.write_text(
            export_markdown(
                entries, filters_desc, now.strftime("%Y-%m-%d %H:%M:%S"),
                packages=self.pid_names,
            ),
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
        # prefer the live pid->name map; fall back to the crash's own
        # "Process: <pkg>, PID:" line (present in every FATAL EXCEPTION) so the
        # package shows even when ps hasn't mapped the pid yet
        pkg = self.pid_names.get(start.pid) or crash_package(block) or "(unknown)"
        err_style = self.level_styles.get("E", "red")
        # lead with a package/pid header so it travels with the copied text
        header = Text.assemble(
            ("package: ", "dim"), (pkg, "bold"),
            ("   pid ", "dim"), (str(start.pid), ""),
            ("   tag ", "dim"), (start.tag, "bold"),
        )
        lines = [header, Text("")]
        lines.extend(
            Text.assemble((e.ts, "dim"), " ", (e.msg, err_style if e.level in ("E", "F") else ""))
            for e in block
        )
        body = Text("\n").join(lines)
        self.push_screen(TextViewerScreen(f"💥 {pkg} — {start.tag} @ {start.ts}", body))

    def set_min_level(self, level, exact=None):
        if level not in ("V", "D", "I", "W", "E"):
            return
        self.min_level = level
        if exact is not None:
            self.level_exact = exact
        sign = "=" if self.level_exact else "≥"
        chip = self.query_one("#minlevel", Static)
        # tint the chip text with the selected level's colour (matches the log)
        style = self.level_styles.get(level, "") or "dim"
        chip.update(Text.assemble(("Level ", "dim"), (f"{sign} {level}", f"bold {style}")))
        chip.set_class(level != "V" or self.level_exact, "levelactive")
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

    # ---- adb operations ---------------------------------------------------------

    def _adb_async(self, args, success_msg, then=None, timeout=30):
        """Run an adb command off the UI thread; toast the outcome."""
        serial = self.serial

        def work():
            try:
                r = subprocess.run(
                    ["adb", "-s", serial, *args],
                    capture_output=True, text=True, timeout=timeout,
                )
                out = f"{r.stdout}\n{r.stderr}"
                ok = r.returncode == 0 and "Failure" not in out and "Error" not in out
                msg = success_msg if ok else (out.strip().splitlines() or ["adb failed"])[-1]
                self.call_from_thread(self._adb_done, ok, msg, then)
            except Exception as ex:
                self.call_from_thread(self.notify, f"adb failed: {ex}", severity="error")

        threading.Thread(target=work, daemon=True).start()

    def _adb_done(self, ok, msg, then):
        self.notify(msg, severity="information" if ok else "error")
        if ok and then:
            then()

    def _package_predicates(self):
        """Positive package: predicates from the query box (and the legacy pkg
        box, if anything ever sets it). Each is a compiled pattern."""
        pats = []
        for clause in self.f_query:
            for field, _op, pat, negated in clause:
                if field == "pkg" and not negated:
                    pats.append(pat)
        for pat, negated in self.f_pkg:  # legacy package box (normally empty)
            if not negated:
                pats.append(pat)
        return pats

    def _default_adb_target(self):
        """The app the package filter unambiguously points at, if any."""
        pats = self._package_predicates()
        if not pats:
            return None
        names = sorted({n for n in self.pid_names.values() if "." in n})
        matching = [n for n in names if all(p.search(n) for p in pats)]
        if len(matching) == 1:
            return matching[0]
        # several match — prefer an exact process-name hit (package:com.x.app)
        exact = [n for n in matching if any(p.pattern == re.escape(n) for p in pats)]
        return exact[0] if len(exact) == 1 else None

    def action_adb_menu(self):
        if self.serial is None:
            self.notify("No device selected.", severity="warning")
            return
        if self._adb_target is None:
            self._adb_target = self._default_adb_target()
        if self._adb_target is None:
            self._pick_adb_target()
        else:
            self._open_adb_ops()

    def _pick_adb_target(self):
        candidates = sorted({n for n in self.pid_names.values() if "." in n})
        # float packages matching the current package: filter to the top, then
        # the foreground app, so the likely target is first
        pats = self._package_predicates()
        ordered = []
        if pats:
            matching = [c for c in candidates if all(p.search(c) for p in pats)]
            ordered += matching
        if self.foreground_pkg and self.foreground_pkg in candidates:
            if self.foreground_pkg not in ordered:
                ordered.append(self.foreground_pkg)
        candidates = ordered + [c for c in candidates if c not in ordered]
        if not candidates:
            self.notify("No processes mapped yet — wait a moment.", severity="warning")
            return

        def done(pkg):
            if pkg:
                self._adb_target = pkg
                self._open_adb_ops()

        self.push_screen(FilterPickScreen("Target app", candidates, "filter packages…"), done)

    def _open_adb_ops(self):
        pkg = self._adb_target
        recording = self._record_proc is not None
        ops = [
            f"📦 Target: {pkg}  (change…)",
            "▶ Start app",
            "🔄 Restart app",
            "⛔ Kill app (force-stop)",
            "💀 Simulate process death",
            "🧹 Clear app data",
            "🧹 Clear app data & restart",
            "🗑 Uninstall app",
            "🔓 Grant permission…",
            "🔒 Revoke permission…",
            "♻️ Reset all permissions (all apps)",
            "🔗 Open deep link…",
            "⏹ Stop recording & save" if recording else "🎬 Start screen record",
        ]
        self.push_screen(PickListScreen(f"ADB — {pkg}", ops), self._run_adb_op)

    def _run_adb_op(self, choice):
        if not choice:
            return
        pkg = self._adb_target
        start_cmd = ["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"]
        if choice.startswith("📦"):
            self._adb_target = None
            self._pick_adb_target()
        elif choice == "▶ Start app":
            self._adb_async(start_cmd, f"Started {pkg}")
        elif choice == "🔄 Restart app":
            self._adb_async(
                ["shell", "am", "force-stop", pkg], f"Restarted {pkg}",
                then=lambda: self._adb_async(start_cmd, f"Started {pkg}"),
            )
        elif choice == "⛔ Kill app (force-stop)":
            self._adb_async(["shell", "am", "force-stop", pkg], f"Killed {pkg}")
        elif choice == "💀 Simulate process death":
            self._adb_async(["shell", "am", "kill", pkg], f"Background-killed {pkg}")
        elif choice == "🧹 Clear app data":
            self._confirm_adb(f"Clear all data of {pkg}?", ["shell", "pm", "clear", pkg], f"Cleared {pkg} data")
        elif choice == "🧹 Clear app data & restart":
            self._confirm_adb(
                f"Clear all data of {pkg} and restart?",
                ["shell", "pm", "clear", pkg], f"Cleared {pkg} data",
                then=lambda: self._adb_async(start_cmd, f"Started {pkg}"),
            )
        elif choice == "🗑 Uninstall app":
            self._confirm_adb(f"Uninstall {pkg}?", ["uninstall", pkg], f"Uninstalled {pkg}")
        elif choice == "🔓 Grant permission…":
            self._pick_permission(granted=False)
        elif choice == "🔒 Revoke permission…":
            self._pick_permission(granted=True)
        elif choice.startswith("♻️"):
            self._confirm_adb(
                "Reset runtime permissions of ALL apps?",
                ["shell", "pm", "reset-permissions"], "All permissions reset",
            )
        elif choice == "🔗 Open deep link…":
            def done(url):
                if url:
                    self._last_deeplink = url
                    self._adb_async(
                        ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url],
                        f"Opened {url}",
                    )
            self.push_screen(
                TextPromptScreen("Deep link URL", self._last_deeplink, "scheme://host/path"), done
            )
        elif choice == "🎬 Start screen record":
            self._start_recording()
        elif choice.startswith("⏹"):
            self._export_dir_or_prompt(self._stop_recording)

    REC_LIMIT = 180  # screenrecord --time-limit (the device caps at 3 min)

    def _start_recording(self):
        """Begin a device screen recording (no app target needed)."""
        if self.serial is None:
            self.notify("No devices connected.", severity="warning")
            return
        if self._record_proc is not None:
            return  # already recording
        self._record_proc = subprocess.Popen(
            ["adb", "-s", self.serial, "shell", "screenrecord",
             "--time-limit", str(self.REC_LIMIT), "/sdcard/catflap_rec.mp4"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._record_start = time.monotonic()
        bar = self.query_one("#recbar", RecBar)
        bar.display = True
        self._tick_recbar()        # sets _rec_text
        self._position_recbar()    # …which the position measurement needs
        self._rec_timer = self.set_interval(1.0, self._tick_recbar)

    def on_resize(self, event):
        if self._record_proc is not None:
            self._position_recbar()

    def _tick_recbar(self):
        if self._record_proc is None:
            return
        elapsed = int(time.monotonic() - self._record_start)
        rc = self._record_proc.poll()
        if rc is not None and elapsed < self.REC_LIMIT - 1:
            # screenrecord exited early — almost always an error (screen locked,
            # secure/DRM surface, adb dropped). Don't claim success or pull a
            # file that was never written; abandon the recording.
            self._abort_recording()
            self.notify(
                "Recording stopped early — is the screen locked? (no file saved)",
                severity="warning",
            )
            return
        # the device auto-stops at the limit — finalise & save when it does
        if elapsed >= self.REC_LIMIT or rc is not None:
            self.notify("Recording reached the 3 min limit — saving.")
            self._export_dir_or_prompt(self._stop_recording)
            return
        remaining = self.REC_LIMIT - elapsed
        text = f"🔴 REC  {elapsed // 60}:{elapsed % 60:02d}  / {remaining // 60}:{remaining % 60:02d} left   ⏹ stop"
        # only the text changes each tick; the position is set once on show /
        # resize (re-applying the offset every second re-layouts the overlay and
        # can drop a click landing in that frame — see _position_recbar)
        self._rec_text = text
        self.query_one("#recbar", RecBar).update(text)

    def _position_recbar(self):
        # anchor flush against the right edge, one row above the footer. measure
        # the real display width (cell_len counts the wide 🔴/⏹ glyphs as 2) and
        # add the box's 0 1 left/right padding so it sits exactly at the edge
        bar = self.query_one("#recbar", RecBar)
        width = cell_len(getattr(self, "_rec_text", "")) + 2  # +2 = padding 0 1
        bar.styles.offset = (max(0, self.size.width - width), self.size.height - 2)

    def _hide_recbar(self):
        if self._rec_timer is not None:
            self._rec_timer.stop()
            self._rec_timer = None
        self.query_one("#recbar", RecBar).display = False

    def _abort_recording(self):
        """Recording failed/exited early — drop it without saving and tidy the
        (possibly empty) device file in the background."""
        proc, self._record_proc = self._record_proc, None
        self._hide_recbar()
        serial = self.serial
        if proc is None or serial is None:
            return

        def work():
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            try:
                subprocess.run(
                    ["adb", "-s", serial, "shell", "rm", "-f", "/sdcard/catflap_rec.mp4"],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def action_device_screenshot(self):
        """F4 — capture the device screen (PNG) to the export folder."""
        if self.serial is None:
            self.notify("No devices connected.", severity="warning")
            return
        self._export_dir_or_prompt(self._take_screenshot)

    def action_toggle_record(self):
        """F3 — start a recording, or stop & save the one in progress."""
        if self.serial is None:
            self.notify("No devices connected.", severity="warning")
            return
        if self._record_proc is not None:
            self._export_dir_or_prompt(self._stop_recording)
        else:
            self._start_recording()

    def _confirm_adb(self, question, args, success_msg, then=None):
        def done(answer):
            if answer == "Yes":
                self._adb_async(args, success_msg, then=then)

        self.push_screen(PickListScreen(question, ["Yes", "Cancel"]), done)

    def _pick_permission(self, granted):
        pkg = self._adb_target
        serial = self.serial

        def work():
            try:
                out = subprocess.run(
                    ["adb", "-s", serial, "shell", "dumpsys", "package", pkg],
                    capture_output=True, text=True, timeout=15,
                ).stdout
                perms = sorted(p for p, g in parse_permissions(out).items() if g == granted)
                self.call_from_thread(self._show_permissions, perms, granted)
            except Exception as ex:
                self.call_from_thread(self.notify, f"adb failed: {ex}", severity="error")

        threading.Thread(target=work, daemon=True).start()

    def _show_permissions(self, perms, granted):
        verb = "revoke" if granted else "grant"
        if not perms:
            self.notify(f"Nothing to {verb}.", severity="warning")
            return

        def done(perm):
            if perm:
                self._adb_async(
                    ["shell", "pm", verb, self._adb_target, perm],
                    f"{verb.capitalize()}ed {perm.rsplit('.', 1)[-1]}",
                )

        self.push_screen(PickListScreen(f"Permission to {verb}", perms), done)

    def _take_screenshot(self, d):
        serial = self.serial

        def work():
            try:
                r = subprocess.run(
                    ["adb", "-s", serial, "exec-out", "screencap", "-p"],
                    capture_output=True, timeout=20,
                )
                if r.returncode == 0 and r.stdout.startswith(b"\x89PNG"):
                    path = d / f"screenshot_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.png"
                    path.write_bytes(r.stdout)
                    self.call_from_thread(self.notify, f"Saved {path.name}")
                else:
                    self.call_from_thread(self.notify, "Screenshot failed.", severity="error")
            except Exception as ex:
                self.call_from_thread(self.notify, f"adb failed: {ex}", severity="error")

        threading.Thread(target=work, daemon=True).start()

    def _stop_recording(self, d):
        serial = self.serial
        proc, self._record_proc = self._record_proc, None
        self._hide_recbar()

        def work():
            try:
                subprocess.run(
                    ["adb", "-s", serial, "shell", "killall", "-INT", "screenrecord"],
                    capture_output=True, timeout=10,
                )
                if proc:
                    proc.wait(timeout=10)
                time.sleep(1)  # let the device finalize the mp4
                path = d / f"screenrecord_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.mp4"
                r = subprocess.run(
                    ["adb", "-s", serial, "pull", "/sdcard/catflap_rec.mp4", str(path)],
                    capture_output=True, text=True, timeout=60,
                )
                subprocess.run(
                    ["adb", "-s", serial, "shell", "rm", "-f", "/sdcard/catflap_rec.mp4"],
                    capture_output=True, timeout=10,
                )
                if r.returncode == 0:
                    self.call_from_thread(self.notify, f"Saved {path.name}")
                else:
                    self.call_from_thread(self.notify, "Could not pull the recording.", severity="error")
            except Exception as ex:
                self.call_from_thread(self.notify, f"adb failed: {ex}", severity="error")

        threading.Thread(target=work, daemon=True).start()

    # ---- presets & persistence -------------------------------------------------

    def _current_filters(self):
        return {
            "pkg": self.query_one("#pkg", Input).value,
            "query": self.query_one("#query", Input).value,
            "level": self.min_level,
            "level_exact": self.level_exact,
        }

    def _apply_filter_dict(self, f):
        self.query_one("#pkg", Input).value = f.get("pkg", "")
        self.query_one("#query", Input).value = _migrate_query(f)
        level = f.get("level", "V")
        if f.get("errors") and LEVELS.index(level) < LEVELS.index("E"):
            level = "E"  # legacy "errors only" checkbox state
        exact = f.get("level_exact", False)
        if level != self.min_level or exact != self.level_exact:
            self.set_min_level(level, exact)

    def _do_factory_reset(self):
        self._state = {}
        try:
            state.STATE_PATH.unlink()  # via module so tests' redirect applies
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


