"""Headless UI integration tests (textual run_test pilot)."""

import json
import tempfile
import unittest
from pathlib import Path

import catflap
from catflap import (
    DevicePickerScreen,
    ExportDirScreen,
    HelpScreen,
    Input,
    Catflap,
    PickListScreen,
    QueryHighlighter,
    TextViewerScreen,
)


def make_app():
    # fully isolate from adb: no devices -> no reader/mapper activity,
    # tests feed lines straight into app.queue
    catflap.list_devices = lambda: []
    app = Catflap()
    app._auto_picked = True
    return app


def isolate_state():
    tmp = Path(tempfile.mkdtemp()) / "state.json"
    catflap.STATE_PATH = tmp
    return tmp


LINE = "06-12 10:00:00.{ms:03d}  {pid}  {pid} {lvl} {tag}: {msg}"


def line(ms=0, pid=42, lvl="D", tag="Acme", msg="hello"):
    return LINE.format(ms=ms, pid=pid, lvl=lvl, tag=tag, msg=msg)


class FilteringFlow(unittest.IsolatedAsyncioTestCase):
    async def test_filters_levels_and_operators(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.queue.put(line(0, lvl="E", tag="Crash", msg="boom in ad"))
            app.queue.put(line(1, lvl="D", msg="ad loaded"))
            app.queue.put(line(2, lvl="W", msg="slow ad"))
            await pilot.pause(0.3)
            self.assertEqual(app.shown, 3)
            app.set_min_level("E")  # errors only
            await pilot.pause(0.3)
            self.assertEqual(app.shown, 1)
            app.set_min_level("I")  # drops only the D line
            await pilot.pause(0.3)
            self.assertEqual(app.shown, 2)
            app.query_one("#query").value = "message:ad AND -message:slow"
            await pilot.pause(0.4)
            self.assertEqual(app.shown, 1)

    async def test_clear_button_inside_input(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            box = app.query_one("#query")
            clear = app.query_one("#clear-query")
            self.assertFalse(clear.display)
            box.value = "AcmeSDK"
            await pilot.pause(0.4)
            self.assertTrue(clear.display)
            await pilot.click("#clear-query")
            await pilot.pause(0.4)
            self.assertEqual(box.value, "")
            self.assertFalse(clear.display)
            self.assertIs(app.focused, box)

    async def test_inputs_have_query_highlighter(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            for box_id in ("#pkg", "#query"):
                self.assertIsInstance(
                    app.query_one(box_id).highlighter, QueryHighlighter
                )

    async def test_matched_terms_highlighted_in_log(self):
        """tag: and message: matches paint their substrings with distinct,
        field-scoped styles — and only the matched substring."""
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.pid_names = {"42": "com.x.app"}
            app.queue.put(line(0, pid=42, lvl="I", tag="AdsManager",
                               msg="contain that and fill"))
            await pilot.pause(0.3)
            app.query_one("#query").value = "tag:Ads message:fill"
            await pilot.pause(0.4)
            e = next(x for x in app.buffer if x.kind != "proc")
            text = app._render(e)
            plain = text.plain
            # collect (substring, style_str) for spans that carry a highlight
            tag_hl, msg_hl = [], []
            for span in text.spans:
                seg = plain[span.start:span.end]
                style = str(span.style)
                if style == app.tag_hl_style:
                    tag_hl.append(seg)
                elif style == app.msg_hl_style:
                    msg_hl.append(seg)
            self.assertEqual(tag_hl, ["Ads"])   # only "Ads", not the whole tag
            self.assertEqual(msg_hl, ["fill"])  # only "fill", not the whole msg
            self.assertNotEqual(app.tag_hl_style, app.msg_hl_style)  # distinct

    async def test_tab_reaches_level_chip_and_enter_opens_menu(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            # the package box is hidden and non-focusable, so Tab goes
            # query -> minlevel (no pkg)
            app.set_focus(app.query_one("#query", Input))
            await pilot.pause(0.05)
            order = [getattr(app.focused, "id", None)]
            await pilot.press("tab")
            await pilot.pause(0.05)
            order.append(getattr(app.focused, "id", None))
            self.assertEqual(order, ["query", "minlevel"])
            # Enter on the now-focused chip opens the level menu
            await pilot.press("enter")
            await pilot.pause(0.1)
            self.assertTrue(app.level_menu.display)

    async def test_hidden_package_box_does_not_pop_dropdown(self):
        """The package box is hidden — focusing it must not open a suggestion
        dropdown for an invisible field."""
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.pid_names = {"1": "com.fg.app"}
            app.foreground_pkg = "com.fg.app"
            app.set_focus(app.query_one("#pkg", Input))
            await pilot.pause(0.2)
            self.assertFalse(app.suggest_list.display)


class AutocompleteFlow(unittest.IsolatedAsyncioTestCase):
    async def test_bare_term_suggests_reserved_form(self):
        """A bare word matching a known tag is offered as tag:<value>."""
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            for tag in ("InterstitialDebug", "AcmeSDK", "WindowManager"):
                app.tag_count[tag] += 1
            box = app.query_one("#query")
            app.set_focus(box)
            box.value = "win"
            box.cursor_position = len(box.value)
            await pilot.pause(0.4)
            # the promotion replacement scopes the bare term to tag:
            self.assertIn("tag:WindowManager", app._suggest_values)
            await pilot.press("down", "enter")
            await pilot.pause(0.4)
            self.assertEqual(box.value, "tag:WindowManager")
            self.assertFalse(app.suggest_list.display)

    async def test_scoped_key_completes_field_values(self):
        """Typing tag:Win completes from the tag candidates, keeping the key."""
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            for tag in ("WindowManager", "WifiService"):
                app.tag_count[tag] += 1
            box = app.query_one("#query")
            app.set_focus(box)
            box.value = "message:foo tag:Win"
            box.cursor_position = len(box.value)
            await pilot.pause(0.4)
            self.assertEqual(app._suggest_values, ["message:foo tag:WindowManager"])
            await pilot.press("down", "enter")
            await pilot.pause(0.4)
            self.assertEqual(box.value, "message:foo tag:WindowManager")

    async def test_package_key_pins_foreground_app(self):
        """Typing package: in the query box suggests packages with the
        foreground app pinned first — same hint as the dedicated package box
        (there is no 'mine' keyword like Android Studio)."""
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.pid_names = {
                "1": "com.foreground.app",
                "2": "com.other.app",
                "3": "com.background.svc",
            }
            app.foreground_pkg = "com.foreground.app"
            box = app.query_one("#query")
            app.set_focus(box)
            box.value = "package:"
            box.cursor_position = len(box.value)
            await pilot.pause(0.4)
            self.assertTrue(app._suggest_values)
            self.assertEqual(app._suggest_values[0], "package:com.foreground.app")
            # pkg alias works too, and narrowing still filters
            box.value = "pkg:back"
            box.cursor_position = len(box.value)
            await pilot.pause(0.4)
            self.assertEqual(app._suggest_values, ["pkg:com.background.svc"])

    async def test_empty_query_focus_offers_foreground_app(self):
        """Focusing the empty query box surfaces the foreground app as a
        one-click 'package:<app>' start (clean replacement for the old picker)."""
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.pid_names = {"1": "com.foreground.app"}
            app.foreground_pkg = "com.foreground.app"
            box = app.query_one("#query")
            self.assertEqual(box.value, "")
            app.set_focus(box)
            await pilot.pause(0.3)
            self.assertEqual(app._suggest_values, ["package:com.foreground.app"])
            # accepting it fills the query box (not the hidden package box)
            app._apply_suggestion(app._suggest_values[0])
            await pilot.pause(0.2)
            self.assertEqual(box.value, "package:com.foreground.app")
            self.assertEqual(app.query_one("#pkg").value, "")

    async def test_typing_key_word_suggests_the_key(self):
        """Typing a key word (no colon yet) offers to complete the key, so
        'package' suggests 'package:' before treating it as a search term."""
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            box = app.query_one("#query")
            app.set_focus(box)
            for typed, expected_key in (("package", "package:"),
                                        ("pack", "package:"),
                                        ("tag", "tag:"),
                                        ("mess", "message:")):
                box.value = typed
                box.cursor_position = len(typed)
                await pilot.pause(0.4)
                self.assertIn(expected_key, app._suggest_values,
                              f"typing {typed!r} should suggest {expected_key}")

    async def test_enter_submits_query_as_typed(self):
        """Enter applies the filter immediately and dismisses the dropdown —
        without forcing the user to pick a suggestion."""
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.tag_count["AdsManager"] += 5  # makes the dropdown open on "Ads"
            app.queue.put(line(0, lvl="E", tag="AdsManager", msg="no fill"))
            app.queue.put(line(1, lvl="I", tag="Choreographer", msg="frame"))
            await pilot.pause(0.3)
            box = app.query_one("#query")
            app.set_focus(box)
            box.value = "Ads"
            box.cursor_position = len(box.value)
            await pilot.pause(0.4)
            self.assertTrue(app.suggest_list.display)  # suggestions are showing
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertFalse(app.suggest_list.display)  # Enter dismissed them
            self.assertEqual(box.value, "Ads")          # text kept as typed
            self.assertEqual(app.shown, 1)              # filter applied


class CrashFlow(unittest.IsolatedAsyncioTestCase):
    async def test_detect_jump_and_dead_pid_attribution(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.pid_names = {"42": "com.acme.sample"}
            app.queue.put(line(0, lvl="E", tag="AndroidRuntime", msg="FATAL EXCEPTION: main"))
            app.queue.put(line(1, lvl="E", tag="AndroidRuntime", msg="java.lang.NullPointerException"))
            await pilot.pause(0.3)
            self.assertEqual(len(app.crashes), 1)
            await pilot.press("ctrl+g")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, TextViewerScreen)
            # the package travels in the copyable body, not just the title
            body = app.screen.query_one("#viewer-scroll Static").render()
            self.assertIn("package: com.acme.sample", body.plain)
            await pilot.press("escape")
            await pilot.pause(0.2)
            # package filter still matches after the process "dies"
            # (mapper merges, never replaces — simulate a refresh without pid 42)
            merged = dict(app.pid_names)
            merged.update({"99": "com.other"})
            app.pid_names = merged
            app.query_one("#pkg").value = "acme"
            await pilot.pause(0.4)
            self.assertEqual(app.shown, 2)

    async def test_unmapped_pid_resolves_package_from_process_line(self):
        """When ps hasn't mapped the crashing pid, the package comes from the
        crash's own 'Process: <pkg>, PID:' line — never 'pid <n>'."""
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.pid_names = {}  # pid 3225 is unmapped
            app.queue.put(line(0, pid=3225, lvl="E", tag="AndroidRuntime",
                               msg="FATAL EXCEPTION: main"))
            app.queue.put(line(1, pid=3225, lvl="E", tag="AndroidRuntime",
                               msg="Process: com.google.android.odad, PID: 3225"))
            await pilot.pause(0.3)
            await pilot.press("ctrl+g")
            await pilot.pause(0.2)
            body = app.screen.query_one("#viewer-scroll Static").render().plain
            self.assertIn("package: com.google.android.odad", body)
            self.assertNotIn("package: pid 3225", body)

    async def test_no_crash_toast(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            await pilot.press("ctrl+g")
            await pilot.pause(0.2)
            self.assertNotIsInstance(app.screen, TextViewerScreen)


class PauseFlow(unittest.IsolatedAsyncioTestCase):
    async def test_pause_buffers_then_resume_renders(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.queue.put(line(0))
            await pilot.pause(0.3)
            before = app.shown
            await pilot.press("ctrl+s")
            app.queue.put(line(1, msg="while paused"))
            await pilot.pause(0.3)
            self.assertTrue(app.paused)
            self.assertEqual(app.shown, before)
            self.assertEqual(app._pending_lines, 1)
            await pilot.press("ctrl+s")
            await pilot.pause(0.3)
            self.assertFalse(app.paused)
            self.assertEqual(app.shown, before + 1)


def _procs(app):
    return [e for e in app.buffer if e.kind == "proc"]


def _visible_procs(app):
    return [e for e in _procs(app) if app._entry_visible(e)]


class ProcessBannerFlow(unittest.IsolatedAsyncioTestCase):
    async def test_started_and_ended_banners(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.pid_names = {"42": "com.x.app"}
            app.query_one("#pkg").value = "com.x"
            await pilot.pause(0.4)
            before = app.shown
            app._emit_banner("42", "com.x.app", "STARTED")
            await pilot.pause(0.1)
            procs = _procs(app)
            self.assertEqual(len(procs), 1)
            self.assertIn("PROCESS STARTED (42) for package com.x.app", procs[0].msg)
            self.assertEqual(app.shown, before + 1)
            app._emit_banner("42", "com.x.app", "ENDED")
            await pilot.pause(0.1)
            self.assertEqual(len(_procs(app)), 2)
            self.assertIn("PROCESS ENDED (42)", _procs(app)[1].msg)
            self.assertEqual(app.shown, before + 2)

    async def test_empty_filter_no_banner(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.pid_names = {"42": "com.x.app"}
            # package box empty -> the diff path produces nothing
            started, ended = catflap.banner_diff(
                {"1"}, {"1": "com.x.app", "42": "com.x.app"}, app.pid_names, app.f_pkg
            )
            self.assertEqual((started, ended), ([], []))
            self.assertEqual(_procs(app), [])

    async def test_package_filter_change_hides_and_reshows(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.pid_names = {"42": "com.x.app"}
            app.query_one("#pkg").value = "com.x"
            await pilot.pause(0.4)
            app._emit_banner("42", "com.x.app", "STARTED")
            await pilot.pause(0.1)
            self.assertEqual(len(_visible_procs(app)), 1)
            self.assertEqual(app.shown, 1)
            # switch package -> banner stays in buffer but is hidden & uncounted
            app.query_one("#pkg").value = "com.y"
            await pilot.pause(0.4)
            self.assertEqual(len(_procs(app)), 1)        # still buffered
            self.assertEqual(len(_visible_procs(app)), 0)  # not shown
            self.assertEqual(app.shown, 0)
            # switch back -> reappears and re-counts
            app.query_one("#pkg").value = "com.x"
            await pilot.pause(0.4)
            self.assertEqual(len(_visible_procs(app)), 1)
            self.assertEqual(app.shown, 1)

    async def test_pause_buffers_banner_then_resume(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.pid_names = {"42": "com.x.app"}
            app.query_one("#pkg").value = "com.x"
            await pilot.pause(0.4)
            await pilot.press("ctrl+s")  # pause
            app._emit_banner("42", "com.x.app", "STARTED")
            await pilot.pause(0.2)
            self.assertTrue(app.paused)
            self.assertEqual(app.shown, 0)
            self.assertEqual(app._pending_lines, 1)
            await pilot.press("ctrl+s")  # resume
            await pilot.pause(0.3)
            self.assertEqual(len(_visible_procs(app)), 1)
            self.assertEqual(app.shown, 1)

    async def test_export_excludes_banner(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.pid_names = {"42": "com.x.app"}
            app.query_one("#pkg").value = "com.x"
            await pilot.pause(0.4)
            app.queue.put(line(0, pid=42, tag="Foo", msg="real line"))
            await pilot.pause(0.3)
            app._emit_banner("42", "com.x.app", "STARTED")
            await pilot.pause(0.1)
            entries = app._filtered_entries_for_export()
            self.assertTrue(all(e.kind != "proc" for e in entries))
            raw = catflap.export_raw(entries)
            self.assertIn("real line", raw)
            self.assertNotIn("PROCESS STARTED", raw)

    async def test_search_finds_banner_anchor(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.pid_names = {"42": "com.x.app"}
            app.query_one("#pkg").value = "com.x"
            await pilot.pause(0.4)
            app._emit_banner("42", "com.x.app", "STARTED")
            await pilot.pause(0.1)
            await pilot.press("/")  # open search
            await pilot.pause(0.1)
            app.query_one("#searchbar", Input).value = "PROCESS"
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertTrue(app._search_matches)  # banner is a searchable anchor


class PresetsAndPersistence(unittest.IsolatedAsyncioTestCase):
    async def test_preset_roundtrip_and_state_file(self):
        tmp = isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.query_one("#query").value = "message:timeout"
            await pilot.pause(0.4)
            app._state.setdefault("presets", {})["mine"] = app._current_filters()
            catflap.save_state(app._state)
            app.query_one("#query").value = ""
            await pilot.pause(0.4)
            app._apply_filter_dict(app._state["presets"]["mine"])
            await pilot.pause(0.4)
            self.assertEqual(app.query_one("#query").value, "message:timeout")
        saved = json.loads(tmp.read_text())
        self.assertEqual(saved["presets"]["mine"]["query"], "message:timeout")
        self.assertEqual(saved["presets"]["mine"]["query"], "message:timeout")

    async def test_filters_not_restored_on_launch(self):
        """Each session starts with a clean filter — a saved query/level does
        NOT carry over (only named presets persist filters)."""
        tmp = isolate_state()
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"filters": {"query": "tag:AcmeSDK", "level": "E"}}))
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.4)
            self.assertEqual(app.query_one("#query").value, "")  # fresh
            self.assertEqual(app.min_level, "V")                  # default level

    async def test_legacy_tag_msg_preset_migrates_on_load(self):
        """Old two-box presets fold into the unified query when the preset is
        loaded (migration still applies via _apply_filter_dict)."""
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app._apply_filter_dict({"tag": "AcmeSDK", "msg": "timeout"})
            await pilot.pause(0.3)
            self.assertEqual(
                app.query_one("#query").value, "tag:AcmeSDK AND message:timeout"
            )

    async def test_theme_and_wrap_persist(self):
        tmp = isolate_state()
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"theme": "nord", "wrap": True}))
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            self.assertEqual(app.theme, "nord")
            self.assertTrue(app.query_one("#log").wrap)
        saved = json.loads(tmp.read_text())
        self.assertEqual(saved["theme"], "nord")


class LevelMenuFlow(unittest.IsolatedAsyncioTestCase):
    async def test_open_select_and_dismiss(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.toggle_level_menu()
            await pilot.pause(0.2)
            self.assertTrue(app.level_menu.display)
            await pilot.press("down", "down", "down", "enter")  # V -> W
            await pilot.pause(0.3)
            self.assertEqual(app.min_level, "W")
            self.assertFalse(app.level_menu.display)
            self.assertIn("Level ≥ W", str(app.query_one("#minlevel").render()))
            app.toggle_level_menu()
            await pilot.pause(0.2)
            await pilot.press("escape")
            await pilot.pause(0.2)
            self.assertFalse(app.level_menu.display)
            self.assertEqual(app.min_level, "W")

    async def test_clicking_chip_toggles_consistently(self):
        """Clicking the Level chip must reliably open/close — focusing the chip
        should not race with its own toggle and cancel out."""
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            chip = app.query_one("#minlevel")
            states = []
            for _ in range(6):
                await pilot.click(chip)
                await pilot.pause(0.12)
                states.append(app.level_menu.display)
            self.assertEqual(states, [True, False, True, False, True, False])

    async def test_chip_text_colored_by_level(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.set_min_level("E")
            await pilot.pause(0.1)
            rendered = app.query_one("#minlevel").render()
            self.assertIn("Level", rendered.plain)
            self.assertIn("E", rendered.plain)
            # the level part is styled (bold + the level colour), not plain
            styled = [str(s.style) for s in rendered.spans if s.style]
            self.assertTrue(any("bold" in s for s in styled))

    async def test_exact_mode_toggle(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.queue.put(line(0, lvl="I", msg="info line"))
            app.queue.put(line(1, lvl="W", msg="warn line"))
            app.queue.put(line(2, lvl="E", msg="error line"))
            await pilot.pause(0.3)
            app.set_min_level("I")  # threshold: all three
            await pilot.pause(0.3)
            self.assertEqual(app.shown, 3)
            # switch to exact via the menu's mode entry (last option)
            app.toggle_level_menu()
            await pilot.pause(0.2)
            app.level_menu.highlighted = app.level_menu.option_count - 1
            await pilot.press("enter")
            await pilot.pause(0.3)
            self.assertTrue(app.level_exact)
            self.assertEqual(app.shown, 1)  # only the I line
            self.assertIn("Level = I", str(app.query_one("#minlevel").render()))
            # back to threshold
            app.set_min_level("I", exact=False)
            await pilot.pause(0.3)
            self.assertEqual(app.shown, 3)
            chip = app.query_one("#minlevel")
            self.assertTrue(chip.has_class("levelactive"))
            app.set_min_level("V")
            await pilot.pause(0.2)
            self.assertFalse(chip.has_class("levelactive"))


class ExportFlow(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_once_then_remember(self):
        state = isolate_state()
        target = Path(tempfile.mkdtemp()) / "my-logs"  # does not exist yet
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.queue.put(line(0, msg="exported line"))
            await pilot.pause(0.3)
            await pilot.press("ctrl+e")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, PickListScreen)
            await pilot.press("down", "enter")  # Raw logcat (.log)
            await pilot.pause(0.3)
            self.assertIsInstance(app.screen, ExportDirScreen)
            box = app.screen.query_one("#exportdir-path", Input)
            self.assertTrue(box.value.endswith("/Downloads"))  # default prefill
            box.value = str(target)
            await pilot.press("enter")
            await pilot.pause(0.3)
            self.assertEqual(len(list(target.glob("*.log"))), 1)
            # second export skips the folder prompt
            await pilot.press("ctrl+e")
            await pilot.pause(0.2)
            await pilot.press("down", "enter")
            await pilot.pause(0.4)
            self.assertNotIsInstance(app.screen, ExportDirScreen)
        saved = json.loads(state.read_text())
        self.assertEqual(saved["export_dir"], str(target))

    async def test_nothing_to_export_warns(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.action_export_raw()
            await pilot.pause(0.2)
            self.assertNotIsInstance(app.screen, ExportDirScreen)


class PauseBindingFlow(unittest.IsolatedAsyncioTestCase):
    async def test_footer_label_flips(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            self.assertTrue(app.check_action("pause", ()))
            self.assertFalse(app.check_action("resume", ()))
            await pilot.press("ctrl+s")
            await pilot.pause(0.2)
            self.assertTrue(app.paused)
            self.assertFalse(app.check_action("pause", ()))
            self.assertTrue(app.check_action("resume", ()))
            await pilot.press("ctrl+s")
            await pilot.pause(0.2)
            self.assertFalse(app.paused)


class AdbMenuFlow(unittest.IsolatedAsyncioTestCase):
    async def test_requires_device(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.action_adb_menu()
            await pilot.pause(0.2)
            self.assertNotIsInstance(app.screen, PickListScreen)

    async def test_target_picker_filters_then_ops(self):
        from catflap import FilterPickScreen
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.serial = "FAKE"
            app.pid_names = {"1": "com.acme.sample", "2": "com.other.app", "3": "kworker"}
            await pilot.press("ctrl+a")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, FilterPickScreen)
            # only dotted names offered, sorted
            self.assertEqual(app.screen._all, ["com.acme.sample", "com.other.app"])
            # type to filter, enter selects the first match
            await pilot.press(*"acme")
            await pilot.pause(0.2)
            self.assertEqual(app.screen._current, ["com.acme.sample"])
            await pilot.press("enter")
            await pilot.pause(0.3)
            self.assertEqual(app._adb_target, "com.acme.sample")
            self.assertIsInstance(app.screen, PickListScreen)  # ops menu
            self.assertTrue(any("Start app" in o for o in app.screen._options))
            await pilot.press("escape")
            await pilot.pause(0.2)
            # second open goes straight to ops (target remembered)
            await pilot.press("ctrl+a")
            await pilot.pause(0.2)
            self.assertTrue(any("📦 Target" in o for o in app.screen._options))
            await pilot.press("escape")

    async def test_adb_target_inherits_package_from_query(self):
        """A package: filter in the query box scopes the ADB target — Ctrl+A
        goes straight to the ops menu for that app (regression: it used to read
        the now-hidden package box)."""
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.serial = "FAKE"
            app.pid_names = {"1": "com.example.app", "2": "com.other.app"}
            app.query_one("#query").value = "package:com.example"
            await pilot.pause(0.4)
            await pilot.press("ctrl+a")
            await pilot.pause(0.2)
            self.assertEqual(app._adb_target, "com.example.app")
            self.assertIsInstance(app.screen, PickListScreen)  # ops, not picker
            self.assertTrue(any("📦 Target: com.example.app" in o for o in app.screen._options))
            await pilot.press("escape")

    async def test_device_menu_has_screenshot_and_record(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.serial = "FAKE"
            app.action_device_menu()
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, PickListScreen)
            opts = app.screen._options
            self.assertTrue(any("📸 Screenshot" in o for o in opts))
            self.assertTrue(any("Start screen record" in o for o in opts))
            await pilot.press("escape")

    async def test_ctrl_r_toggles_recording_and_recbar(self):
        from unittest.mock import patch, MagicMock
        import tempfile
        tmp = isolate_state()
        app = make_app()
        # a saved export dir so stop runs without a folder prompt
        app_state_dir = tempfile.mkdtemp()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.serial = "FAKE"
            app._state["export_dir"] = app_state_dir
            proc = MagicMock()
            proc.poll.return_value = None
            recbar = app.query_one("#recbar")
            self.assertFalse(recbar.display)
            with patch("catflap.subprocess.Popen", return_value=proc):
                await pilot.press("ctrl+r")  # start
                await pilot.pause(0.2)
            self.assertIsNotNone(app._record_proc)
            self.assertTrue(recbar.display)  # the REC bar is shown
            self.assertIn("REC", str(recbar.render()))
            # stop: clears the handle and hides the bar
            with patch("catflap.subprocess.run", return_value=MagicMock(returncode=0)):
                await pilot.press("ctrl+r")
                await pilot.pause(0.3)
            self.assertIsNone(app._record_proc)
            self.assertFalse(recbar.display)


class Screens(unittest.IsolatedAsyncioTestCase):
    async def test_device_picker_and_quit_binding(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app._open_picker([("serial1", "Pixel 8"), ("emulator-5554", "Pixel 7 API 33")])
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, DevicePickerScreen)
            await pilot.press("enter")
            await pilot.pause(0.3)
            self.assertEqual(app.serial, "serial1")

    async def test_help_screen(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            await pilot.press("f1")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause(0.2)
            self.assertNotIsInstance(app.screen, HelpScreen)


if __name__ == "__main__":
    unittest.main()
