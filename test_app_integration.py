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


def line(ms=0, pid=42, lvl="D", tag="Teads", msg="hello"):
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
            app.query_one("#msg").value = "ad AND NOT slow"
            await pilot.pause(0.4)
            self.assertEqual(app.shown, 1)

    async def test_clear_button_inside_input(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            box = app.query_one("#tag")
            clear = app.query_one("#clear-tag")
            self.assertFalse(clear.display)
            box.value = "TeadsSDK"
            await pilot.pause(0.4)
            self.assertTrue(clear.display)
            await pilot.click("#clear-tag")
            await pilot.pause(0.4)
            self.assertEqual(box.value, "")
            self.assertFalse(clear.display)
            self.assertIs(app.focused, box)

    async def test_inputs_have_query_highlighter(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            for box_id in ("#pkg", "#tag", "#msg"):
                self.assertIsInstance(
                    app.query_one(box_id).highlighter, QueryHighlighter
                )


class AutocompleteFlow(unittest.IsolatedAsyncioTestCase):
    async def test_suggest_select_and_or_segment(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            for tag in ("InterstitialDebug", "TeadsSDK", "WindowManager"):
                app.tag_count[tag] += 1
            box = app.query_one("#tag")
            app.set_focus(box)
            box.value = "TeadsSDK OR NOT win"
            box.cursor_position = len(box.value)
            await pilot.pause(0.4)
            self.assertEqual(app._suggest_values, ["WindowManager"])
            await pilot.press("down", "enter")
            await pilot.pause(0.4)
            self.assertEqual(box.value, "TeadsSDK OR NOT WindowManager")
            self.assertFalse(app.suggest_list.display)


class CrashFlow(unittest.IsolatedAsyncioTestCase):
    async def test_detect_jump_and_dead_pid_attribution(self):
        isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.pid_names = {"42": "com.teads.sample"}
            app.queue.put(line(0, lvl="E", tag="AndroidRuntime", msg="FATAL EXCEPTION: main"))
            app.queue.put(line(1, lvl="E", tag="AndroidRuntime", msg="java.lang.NullPointerException"))
            await pilot.pause(0.3)
            self.assertEqual(len(app.crashes), 1)
            await pilot.press("ctrl+g")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, TextViewerScreen)
            await pilot.press("escape")
            await pilot.pause(0.2)
            # package filter still matches after the process "dies"
            # (mapper merges, never replaces — simulate a refresh without pid 42)
            merged = dict(app.pid_names)
            merged.update({"99": "com.other"})
            app.pid_names = merged
            app.query_one("#pkg").value = "teads"
            await pilot.pause(0.4)
            self.assertEqual(app.shown, 2)

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


class PresetsAndPersistence(unittest.IsolatedAsyncioTestCase):
    async def test_preset_roundtrip_and_state_file(self):
        tmp = isolate_state()
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            app.query_one("#msg").value = "timeout"
            await pilot.pause(0.4)
            app._state.setdefault("presets", {})["mine"] = app._current_filters()
            app.query_one("#msg").value = ""
            await pilot.pause(0.4)
            app._apply_filter_dict(app._state["presets"]["mine"])
            await pilot.pause(0.4)
            self.assertEqual(app.query_one("#msg").value, "timeout")
        saved = json.loads(tmp.read_text())
        self.assertEqual(saved["filters"]["msg"], "timeout")
        self.assertEqual(saved["presets"]["mine"]["msg"], "timeout")

    async def test_filters_restored_on_launch(self):
        tmp = isolate_state()
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"filters": {"tag": "TeadsSDK", "errors": True}}))
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause(0.4)
            self.assertEqual(app.query_one("#tag").value, "TeadsSDK")
            # legacy "errors only" state maps onto the level selector
            self.assertEqual(app.min_level, "E")

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
            app.pid_names = {"1": "com.teads.sample", "2": "com.other.app", "3": "kworker"}
            await pilot.press("ctrl+a")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, FilterPickScreen)
            # only dotted names offered, sorted
            self.assertEqual(app.screen._all, ["com.other.app", "com.teads.sample"])
            # type to filter, enter selects the first match
            await pilot.press(*"teads")
            await pilot.pause(0.2)
            self.assertEqual(app.screen._current, ["com.teads.sample"])
            await pilot.press("enter")
            await pilot.pause(0.3)
            self.assertEqual(app._adb_target, "com.teads.sample")
            self.assertIsInstance(app.screen, PickListScreen)  # ops menu
            self.assertTrue(any("Start app" in o for o in app.screen._options))
            await pilot.press("escape")
            await pilot.pause(0.2)
            # second open goes straight to ops (target remembered)
            await pilot.press("ctrl+a")
            await pilot.pause(0.2)
            self.assertTrue(any("📦 Target" in o for o in app.screen._options))
            await pilot.press("escape")


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
