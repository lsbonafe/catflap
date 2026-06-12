import unittest
from datetime import datetime

from logcat_tui import (
    crash_block,
    export_filename,
    export_markdown,
    export_raw,
    is_crash_start,
    level_at_least,
    matches,
    md_escape,
    parse_devices,
    parse_line,
    parse_terms,
    split_last_term,
    suggest,
)


class ParseTermsTest(unittest.TestCase):
    def test_single_term(self):
        self.assertEqual(len(parse_terms("toto")), 1)

    def test_uppercase_or_splits(self):
        self.assertEqual(len(parse_terms("a OR b OR c")), 3)

    def test_lowercase_or_is_literal(self):
        clauses = parse_terms("true or false")
        self.assertEqual(len(clauses), 1)
        self.assertTrue(matches("it was TRUE OR FALSE here", clauses))
        self.assertFalse(matches("true and false", clauses))

    def test_empty(self):
        self.assertEqual(parse_terms("   "), [])


class MatchesTest(unittest.TestCase):
    def test_empty_filter_matches_all(self):
        self.assertTrue(matches("anything", []))

    def test_substring_case_insensitive(self):
        self.assertTrue(matches("InterstitialDebug", parse_terms("interstitial")))

    def test_or_any_term_with_spaces(self):
        terms = parse_terms("timeout OR connection failed")
        self.assertTrue(matches("Connection FAILED after 3 retries", terms))
        self.assertTrue(matches("read timeout", terms))
        self.assertFalse(matches("all good", terms))

    def test_plain_terms_treat_regex_chars_literally(self):
        self.assertTrue(matches("ad (loaded) [ok]", parse_terms("(loaded) [ok]")))
        self.assertFalse(matches("ad loaded ok", parse_terms("(loaded) [ok]")))

    def test_regex_term(self):
        terms = parse_terms("/ad (loaded|failed)/")
        self.assertTrue(matches("Ad LOADED fine", terms))
        self.assertTrue(matches("ad failed: no fill", terms))
        self.assertFalse(matches("ad requested", terms))

    def test_regex_mixed_with_plain_or(self):
        terms = parse_terms("toto OR /time(out|r)s?/")
        self.assertTrue(matches("read timeouts everywhere", terms))
        self.assertTrue(matches("TOTO here", terms))
        self.assertFalse(matches("nothing", terms))

    def test_invalid_regex_falls_back_to_literal(self):
        terms = parse_terms("/[/")
        self.assertTrue(matches("weird /[/ literal", terms))
        self.assertFalse(matches("clean line", terms))

    def test_and_requires_all_terms(self):
        terms = parse_terms("ad AND timeout")
        self.assertTrue(matches("timeout while loading ad", terms))
        self.assertFalse(matches("ad loaded fine", terms))

    def test_and_binds_tighter_than_or(self):
        terms = parse_terms("toto AND tata OR momo")
        self.assertTrue(matches("tata then toto", terms))
        self.assertTrue(matches("just momo", terms))
        self.assertFalse(matches("only toto here", terms))

    def test_and_with_regex_term(self):
        terms = parse_terms("teads AND /retry \\d+/")
        self.assertTrue(matches("Teads: retry 3 scheduled", terms))
        self.assertFalse(matches("Teads: retry soon", terms))


class ParseLineTest(unittest.TestCase):
    def test_threadtime_line(self):
        e = parse_line(
            "06-12 10:33:21.123  1234  5678 D InterstitialDebug: ad loaded ok"
        )
        self.assertIsNotNone(e)
        self.assertEqual(e.pid, "1234")
        self.assertEqual(e.level, "D")
        self.assertEqual(e.tag, "InterstitialDebug")
        self.assertEqual(e.msg, "ad loaded ok")

    def test_tag_with_spaces_padding(self):
        e = parse_line("06-12 10:33:21.123   123   456 W MyTag  : hello: world")
        self.assertEqual(e.tag, "MyTag")
        self.assertEqual(e.msg, "hello: world")

    def test_non_log_line(self):
        self.assertIsNone(parse_line("--------- beginning of main"))


class ExportTest(unittest.TestCase):
    def test_filename_convention(self):
        now = datetime(2026, 6, 12, 14, 33, 21)
        self.assertEqual(
            export_filename("com.teads OR sample", now),
            "logcat_com.teads-OR-sample_2026-06-12_14-33-21.md",
        )
        self.assertEqual(export_filename("", now), "logcat_all_2026-06-12_14-33-21.md")

    def test_md_escape_pipes(self):
        self.assertEqual(md_escape("a | b"), "a \\| b")

    def test_markdown_table(self):
        e = parse_line("06-12 10:33:21.123  1234  5678 D MyTag: hello | world")
        out = export_markdown([e], "package=`*` tag=`*` message=`*`", "2026-06-12 14:33:21")
        self.assertIn("| Time | Tag | Message |", out)
        self.assertIn("| 06-12 10:33:21.123 | MyTag | hello \\| world |", out)
        self.assertIn("- Lines: 1", out)


class NotOperatorTest(unittest.TestCase):
    def test_and_not(self):
        terms = parse_terms("ad AND NOT timeout")
        self.assertTrue(matches("ad loaded fine", terms))
        self.assertFalse(matches("ad timeout", terms))

    def test_leading_not(self):
        terms = parse_terms("NOT Choreographer")
        self.assertTrue(matches("Teads: ad loaded", terms))
        self.assertFalse(matches("Choreographer: skipped frames", terms))

    def test_not_with_regex(self):
        terms = parse_terms("NOT /Choreographer|gralloc/")
        self.assertFalse(matches("gralloc4: buffer", terms))
        self.assertTrue(matches("clean", terms))

    def test_lowercase_not_is_literal(self):
        terms = parse_terms("not today")
        self.assertTrue(matches("definitely NOT TODAY", terms))

    def test_not_or_combination(self):
        terms = parse_terms("crash OR NOT verbose")
        self.assertTrue(matches("a crash in verbose mode", terms))
        self.assertTrue(matches("quiet line", terms))
        self.assertFalse(matches("verbose chatter", terms))


class LevelTest(unittest.TestCase):
    def test_threshold(self):
        self.assertTrue(level_at_least("E", "W"))
        self.assertTrue(level_at_least("W", "W"))
        self.assertFalse(level_at_least("D", "W"))

    def test_unknown_level_passes(self):
        self.assertTrue(level_at_least("S", "E"))

    def test_exact_mode(self):
        from logcat_tui import level_matches
        self.assertTrue(level_matches("I", "I", exact=True))
        self.assertFalse(level_matches("W", "I", exact=True))
        self.assertFalse(level_matches("V", "I", exact=True))

    def test_exact_e_includes_fatal(self):
        from logcat_tui import level_matches
        self.assertTrue(level_matches("F", "E", exact=True))
        self.assertFalse(level_matches("W", "E", exact=True))

    def test_default_mode_is_threshold(self):
        from logcat_tui import level_matches
        self.assertTrue(level_matches("W", "I"))
        self.assertFalse(level_matches("D", "I"))


class CrashTest(unittest.TestCase):
    def _e(self, line):
        return parse_line(line)

    def test_is_crash_start(self):
        fatal = self._e("06-12 10:00:00.000  42  42 E AndroidRuntime: FATAL EXCEPTION: main")
        plain = self._e("06-12 10:00:00.000  42  42 E AndroidRuntime: some error")
        f_level = self._e("06-12 10:00:00.000  42  42 F libc: aborting")
        self.assertTrue(is_crash_start(fatal))
        self.assertFalse(is_crash_start(plain))
        self.assertTrue(is_crash_start(f_level))

    def test_crash_block_collects_trace_and_stops(self):
        lines = [
            "06-12 10:00:00.000  42  42 E AndroidRuntime: FATAL EXCEPTION: main",
            "06-12 10:00:00.001  99  99 D Other: interleaved noise",
            "06-12 10:00:00.002  42  42 E AndroidRuntime: java.lang.NullPointerException",
            "06-12 10:00:00.003  42  42 E AndroidRuntime: \tat com.teads.Ad.load(Ad.kt:12)",
            "06-12 10:00:00.004  42  42 I Process: Sending signal",
            "06-12 10:00:00.005  42  42 E AndroidRuntime: not part of the block anymore",
        ]
        entries = [self._e(l) for l in lines]
        block = crash_block(entries, entries[0])
        self.assertEqual(len(block), 3)
        self.assertIn("NullPointerException", block[1].msg)

    def test_crash_block_evicted_start(self):
        e = self._e("06-12 10:00:00.000  42  42 E AndroidRuntime: FATAL EXCEPTION: main")
        self.assertEqual(crash_block([], e), [])


class ExportRawTest(unittest.TestCase):
    def test_round_trips_line(self):
        line = "06-12 10:33:21.123 1234 5678 D MyTag: hello world"
        e = parse_line(line)
        self.assertEqual(export_raw([e]).strip(), line)

    def test_log_extension(self):
        from datetime import datetime as dt
        self.assertTrue(
            export_filename("", dt(2026, 6, 12, 1, 2, 3), "log").endswith(".log")
        )


class ParseDevicesTest(unittest.TestCase):
    OUTPUT = """List of devices attached
R3CX10ABCDE            device usb:1-1 product:e3qxxx model:SM_S928B device:e3q transport_id:1
emulator-5554          device product:sdk_gphone64_arm64 model:sdk_gphone64_arm64 device:emu64a transport_id:2
0123456789ABCDEF       offline usb:1-2 transport_id:3
"""

    def test_parses_online_devices_with_model(self):
        self.assertEqual(
            parse_devices(self.OUTPUT),
            [
                ("R3CX10ABCDE", "SM S928B"),
                ("emulator-5554", "sdk gphone64 arm64"),
            ],
        )

    def test_skips_offline_and_empty(self):
        self.assertEqual(parse_devices("List of devices attached\n\n"), [])


class SuggestTest(unittest.TestCase):
    def test_split_last_term(self):
        self.assertEqual(split_last_term("toto OR pix"), ("toto OR ", "pix"))
        self.assertEqual(split_last_term("pix"), ("", "pix"))
        self.assertEqual(split_last_term("a or b OR "), ("a or b OR ", ""))

    def test_suggest_substring_case_insensitive(self):
        cands = ["InterstitialDebug", "TeadsSDK", "WindowManager"]
        self.assertEqual(suggest(cands, "teads"), ["TeadsSDK"])

    def test_suggest_empty_term_returns_top(self):
        self.assertEqual(suggest(["a", "b", "c"], "", limit=2), ["a", "b"])

    def test_suggest_excludes_exact_match(self):
        self.assertEqual(suggest(["TeadsSDK"], "teadssdk"), [])


if __name__ == "__main__":
    unittest.main()
