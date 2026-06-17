"""UI widgets and modal screens (Textual). The Catflap app composes these;
they reach the app only through ``self.app`` (no import of the App class, so no
import cycle)."""

from rich.highlighter import Highlighter
from textual.app import ComposeResult
from textual.binding import Binding
from textual.command import CommandPalette
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, OptionList, RichLog, Static
from textual.widgets._footer import FooterKey
from textual.widgets.option_list import Option


class FilterInput(Input):
    """Input with delete-the-previous-word bound to the platform conventions.

    Textual only ships ctrl+w for that, and maps ctrl+backspace to the *wrong*
    direction (delete_right_word). Bind both conventions to delete-left-word:
      • macOS:        ⌥+Backspace  (arrives as alt+backspace / ctrl+alt+h)
      • Linux/Win:    Ctrl+Backspace
    ⌥+arrows already jump words because the terminal maps them onto ctrl+arrow.
    Whether a terminal sends a distinct alt/ctrl+backspace varies; ctrl+w is the
    universal fallback."""

    BINDINGS = [
        Binding("alt+backspace", "delete_left_word", show=False),
        Binding("ctrl+alt+h", "delete_left_word", show=False),  # some terminals
        Binding("ctrl+backspace", "delete_left_word", show=False),  # Linux/Windows
    ]


HELP_TEXT = r"""[b u $accent]Plain terms[/] — the query box

  [b]droid[/]                      a bare word matches the [b]tag[/] OR the [b]message[/]

  [b]panic (again)[/]              literal text: ( ) \[ ] match exactly as typed


[b u $accent]Field keys[/] — scope a term to one field (Android Studio style)

  [b $accent]tag:[/]Choreo                  tag [b]contains[/] Choreo

  [b $accent]message:[/]no fill             message contains "no fill" (spaces kept)

  [b $accent]package:[/]com.acme            process name contains com.acme
                              (type [b $accent]package:[/] — the foreground app is suggested)

  [b $accent]tag=:[/]Choreographer         [b]exact[/] — the whole tag equals it

  [b $accent]tag~:[/]Cho.+                  [b]regex[/] — match the field by pattern

  [b $accent]-tag:[/]gc                     [b]negate[/] — tag does NOT contain gc
                              (-tag=:  -tag~:  negate exact / regex too)

  keys combine: [b $accent]tag:[/]Ads [b $accent]-message:[/]fill   both must hold (space = AND)


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
  mix freely:  [b]meltdown [$primary]OR[/] [i $secondary]/retry \d+/[/] [$primary]AND NOT[/] noise[/]


[b u $accent]Scope[/]

  the [b]package[/] box matches the process name; the [b]query[/] box matches
  [b]tag[/] + [b]message[/] (scope to one with [b $accent]tag:[/] / [b $accent]message:[/])

  with a [b]package[/] filter set, a divider marks when that app's process
  starts or dies: [b $accent]── PROCESS STARTED (pid) … ──[/] (so you can see restarts)

  the [b]Level[/] chip ([b]F2[/]) filters by severity — [b]≥[/] shows that level and worse,
  [b]=[/] shows exactly that level (switch modes inside the chip's menu)

  in any filter box: [b]^u[/] clears to the start, [b]^k[/] to the end

  [b]^g[/] opens the last crash with its full stack trace

  [b]/[/] searches the displayed lines (plain text or [i $secondary]/regex/[/]) —
  [b]enter[/] jumps to the latest match, [b]n[/]/[b]N[/] hop older/newer, [b]esc[/] closes


[b u $accent]Copying from the terminal[/]

  the app captures the mouse — hold a modifier while dragging to select:

  [b]Ghostty[/]          hold [b]Shift[/]
  [b]kitty / Linux[/]    hold [b]Shift[/]
  [b]iTerm2[/]           hold [b]⌥ Option[/]
  [b]macOS Terminal[/]   hold [b]Fn[/]

  a repaint can wipe the selection — two ways around it:
  [b]1.[/] keep the modifier held [b]~1s[/] after the drag, then release
  [b]2.[/] [b]^e[/] exports the filtered lines to a file (the reliable way)

  pausing first ([b]^s[/]) helps by holding the lines still
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


class DropdownArrow(Static):
    """The ▼ at the right end of the package box; toggles the package dropdown."""

    def __init__(self, **kwargs):
        super().__init__("▼", **kwargs)

    def on_click(self):
        app = self.app
        box = app.query_one("#pkg", Input)
        if app.suggest_list.display and app._suggest_target is box:
            app._hide_suggest()
        else:
            app.set_focus(box)
            app._update_suggest(box)


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
    key_style = "bold cyan"

    def highlight(self, text):
        text.highlight_regex(r"(?<=\s)(?:OR|AND)(?=\s)", self.op_style)
        text.highlight_regex(r"(?:^|(?<=\s))NOT(?=\s)", self.op_style)
        text.highlight_regex(r"/[^/]+/", self.regex_style)
        # field keys in the unified box: -tag:  message=:  pkg~:  …
        text.highlight_regex(
            r"(?:^|(?<=\s))-?(?:tag|message|msg|package|pkg)(?:=:|~:|:)",
            self.key_style,
        )


class CloseButton(Static):
    """A ✕ that dismisses its screen."""

    def __init__(self, **kwargs):
        super().__init__("✕", **kwargs)

    def on_click(self):
        self.screen.dismiss()


class OutsideClickDismiss:
    """Modal mixin: a click on the dimmed backdrop (outside the dialog) closes it."""

    def on_click(self, event):
        if event.widget is self:
            self.dismiss(None)


class HelpScreen(OutsideClickDismiss, ModalScreen):
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


class TextViewerScreen(OutsideClickDismiss, ModalScreen):
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


class PickListScreen(OutsideClickDismiss, ModalScreen):
    """Generic option picker; dismisses with the chosen string or None."""

    CSS = """
    PickListScreen { align: center middle; }
    #picklist-box {
        width: 64; height: auto; max-height: 18; padding: 1 2;
        background: $surface; border: round $accent;
    }
    #picklist-title-row { height: auto; }
    #picklist-title { width: 1fr; padding-bottom: 1; text-style: bold; }
    #picklist-close { width: 3; text-align: right; color: $text 50%; }
    #picklist-close:hover { color: $text; }
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
            with Horizontal(id="picklist-title-row"):
                yield Label(self._title, id="picklist-title")
                yield CloseButton(id="picklist-close")
            yield OptionList(
                *[Option(o, id=str(i)) for i, o in enumerate(self._options)]
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.dismiss(self._options[int(event.option.id)])

    def action_cancel(self):
        self.dismiss(None)


class ExportDirScreen(OutsideClickDismiss, ModalScreen):
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


class FilterPickScreen(OutsideClickDismiss, ModalScreen):
    """Option picker with a live filter box on top (same feel as the main filters).
    Dismisses with the chosen string or None."""

    CSS = """
    FilterPickScreen { align: center middle; }
    #fpick-box {
        width: 64; height: auto; max-height: 24; padding: 1 2;
        background: $surface; border: round $accent;
    }
    #fpick-title-row { height: auto; }
    #fpick-title { width: 1fr; padding-bottom: 1; text-style: bold; }
    #fpick-close { width: 3; text-align: right; color: $text 50%; }
    #fpick-close:hover { color: $text; }
    #fpick-list { height: auto; max-height: 14; }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+q", "app.quit", "Quit"),
    ]

    def __init__(self, title, options, placeholder="type to filter…"):
        super().__init__()
        self._title = title
        self._all = options
        self._current = options
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="fpick-box"):
            with Horizontal(id="fpick-title-row"):
                yield Label(self._title, id="fpick-title")
                yield CloseButton(id="fpick-close")
            yield Input(placeholder=self._placeholder, id="fpick-input")
            yield OptionList(id="fpick-list")

    def on_mount(self):
        self.query_one("#fpick-input", Input).cursor_blink = False
        self._populate(self._all)
        self.set_focus(self.query_one("#fpick-input"))

    def _populate(self, items):
        self._current = items
        olist = self.query_one("#fpick-list", OptionList)
        olist.clear_options()
        olist.add_options([Option(o, id=str(i)) for i, o in enumerate(items)])

    def on_input_changed(self, event: Input.Changed):
        term = event.value.strip().lower()
        self._populate([o for o in self._all if term in o.lower()])

    def on_input_submitted(self, event: Input.Submitted):
        if self._current:
            self.dismiss(self._current[0])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.dismiss(self._current[int(event.option.id)])

    def on_key(self, event):
        if event.key == "down" and self.focused is self.query_one("#fpick-input"):
            olist = self.query_one("#fpick-list", OptionList)
            if olist.option_count:
                self.set_focus(olist)
                olist.highlighted = 0
            event.stop()

    def action_cancel(self):
        self.dismiss(None)


class TextPromptScreen(OutsideClickDismiss, ModalScreen):
    """Generic one-line text prompt; dismisses with the value or None."""

    CSS = """
    TextPromptScreen { align: center middle; }
    #prompt-box {
        width: 70; height: auto; padding: 1 2;
        background: $surface; border: round $accent;
    }
    #prompt-title { padding-bottom: 1; text-style: bold; }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title, initial="", placeholder=""):
        super().__init__()
        self._title = title
        self._initial = initial
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(self._title, id="prompt-title")
            yield Input(value=self._initial, placeholder=self._placeholder, id="prompt-input")

    def on_input_submitted(self, event: Input.Submitted):
        self.dismiss(event.value.strip() or None)

    def action_cancel(self):
        self.dismiss(None)


class SavePresetScreen(OutsideClickDismiss, ModalScreen):
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
            yield Input(placeholder="e.g. my app errors", id="preset-name")

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


FOOTER_ORDER = ["Device", "ADB", "Clear", "Pause", "Resume", "Crash", "Buffer", "Export", "Palette", "Quit"]


class OrderedFooter(Footer):
    """Footer with a fixed, focus-independent ordering of the app's bindings."""

    def compose(self) -> ComposeResult:
        if not self._bindings_ready:
            return
        bindings = [
            (binding, binding.description, enabled, tooltip)
            for (_, binding, enabled, tooltip) in self.screen.active_bindings.values()
            if binding.show
        ]
        # the palette binding is show=False; surface it as a regular in-flow key
        # (skipping the -command-palette class that would dock it right)
        if self.show_command_palette and self.app.ENABLE_COMMAND_PALETTE:
            try:
                _node, binding, enabled, tooltip = self.screen.active_bindings[
                    self.app.COMMAND_PALETTE_BINDING
                ]
            except KeyError:
                pass
            else:
                bindings.append(
                    (binding, "Palette", enabled, tooltip or binding.description)
                )

        def rank(item):
            try:
                return FOOTER_ORDER.index(item[1])
            except ValueError:
                return len(FOOTER_ORDER)

        bindings.sort(key=rank)
        seen_actions = set()
        self.styles.grid_size_columns = len(bindings)
        for binding, description, enabled, tooltip in bindings:
            if binding.action in seen_actions:
                continue
            seen_actions.add(binding.action)
            yield FooterKey(
                binding.key,
                self.app.get_key_display(binding),
                description,
                binding.action,
                disabled=not enabled,
                tooltip=tooltip,
            ).data_bind(compact=Footer.compact)


class LevelChip(Static):
    """Clickable, Tab-focusable min-level selector in the status bar."""

    can_focus = True
    BINDINGS = [
        Binding("enter", "open", "Level", show=False),
        Binding("space", "open", "Level", show=False),
    ]

    def on_click(self):
        self.app.toggle_level_menu()

    def action_open(self):
        self.app.toggle_level_menu()


class RecBar(Static):
    """Fixed bottom-right recording indicator: 🔴 REC m:ss ⏹ — click to stop."""

    def on_mouse_down(self, event):
        # fire on press, not on a matched click — a per-second repaint between
        # mouse-down and -up can otherwise invalidate the click and the stop
        # gets dropped (you'd have to click several times)
        event.stop()
        self.app.action_toggle_record()  # stop & save


class DevicePickerScreen(OutsideClickDismiss, ModalScreen):
    CSS = """
    DevicePickerScreen { align: center middle; }
    #picker-box {
        width: 64; height: auto; max-height: 18; padding: 1 2;
        background: $surface; border: round $accent;
    }
    #picker-title-row { height: auto; }
    #picker-title { width: 1fr; padding-bottom: 1; text-style: bold; }
    #picker-close { width: 3; text-align: right; color: $text 50%; }
    #picker-close:hover { color: $text; }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+q", "app.quit", "Quit"),
    ]

    def __init__(self, devices, current=None):
        super().__init__()
        self.devices = devices
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            with Horizontal(id="picker-title-row"):
                yield Label("Select a device", id="picker-title")
                yield CloseButton(id="picker-close")
            yield OptionList(
                *[
                    Option(
                        ("✓ " if serial == self.current else "  ")
                        + f"{model or '(unknown model)'}  —  {serial}"
                        + ("  (streaming)" if serial == self.current else ""),
                        id=serial,
                    )
                    for serial, model in self.devices
                ]
            )

    def on_mount(self):
        serials = [s for s, _ in self.devices]
        if self.current in serials:
            self.query_one(OptionList).highlighted = serials.index(self.current)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        serial = event.option.id
        model = dict(self.devices).get(serial, "")
        self.dismiss((serial, model))

    def action_cancel(self):
        self.dismiss(None)


