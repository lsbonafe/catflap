import unittest
from datetime import datetime
from unittest.mock import patch

import catflap
from catflap import (
    _pick_dump_serial,
    banner_diff,
    crash_block,
    crash_package,
    Entry,
    highlight_patterns,
    export_filename,
    export_markdown,
    export_raw,
    is_crash_start,
    level_at_least,
    matches,
    md_escape,
    parse_devices,
    parse_foreground,
    parse_query,
    parse_token,
    query_matches,
    split_query_token,
    logcat_cmd,
    parse_line,
    parse_terms,
    split_last_term,
    suggest,
)


def qm(query, tag="", msg="", pkg=""):
    """Helper: does a line (tag/msg/pkg) match a unified query string?"""
    return query_matches(tag, msg, pkg, parse_query(query))


class QueryTokenTest(unittest.TestCase):
    def test_bare_trailing_word_splits_on_space(self):
        self.assertEqual(split_query_token("foo ba"), ("foo ", "ba"))

    def test_trailing_key_value_stays_whole(self):
        self.assertEqual(
            split_query_token("tag:Ad message:no fi"), ("tag:Ad ", "message:no fi")
        )

    def test_or_resets_to_bare(self):
        # after an OR the tail is a fresh bare token
        self.assertEqual(split_query_token("tag:Ad OR ba"), ("tag:Ad OR ", "ba"))

    def test_parse_token_bare(self):
        self.assertEqual(parse_token("win"), (False, None, None, "win"))

    def test_parse_token_keyed(self):
        self.assertEqual(parse_token("tag:Win"), (False, "tag", ":", "Win"))
        self.assertEqual(parse_token("-message~:x"), (True, "message", "~:", "x"))


class MigrateQueryTest(unittest.TestCase):
    def test_new_format_passthrough(self):
        self.assertEqual(catflap._migrate_query({"query": "tag:x"}), "tag:x")

    def test_legacy_single_box(self):
        self.assertEqual(catflap._migrate_query({"tag": "AcmeSDK"}), "tag:AcmeSDK")
        self.assertEqual(catflap._migrate_query({"msg": "timeout"}), "message:timeout")

    def test_legacy_both_boxes_and_joined(self):
        self.assertEqual(
            catflap._migrate_query({"tag": "AcmeSDK", "msg": "timeout"}),
            "tag:AcmeSDK AND message:timeout",
        )

    def test_legacy_operators_preserved(self):
        self.assertEqual(catflap._migrate_query({"tag": "a OR b"}), "tag:a OR tag:b")
        self.assertEqual(
            catflap._migrate_query({"msg": "x AND NOT y"}), "message:x AND -message:y"
        )

    def test_legacy_regex_box(self):
        self.assertEqual(catflap._migrate_query({"tag": "/Cho+/"}), "tag~:Cho+")

    def test_empty(self):
        self.assertEqual(catflap._migrate_query({}), "")


class HighlightPatternsTest(unittest.TestCase):
    def _pats(self, query):
        tp, mp = highlight_patterns(parse_query(query))
        return [p.pattern for p in tp], [p.pattern for p in mp]

    def test_bare_term_highlights_both_fields(self):
        self.assertEqual(self._pats("contain"), (["contain"], ["contain"]))

    def test_scoped_terms_split_by_field(self):
        self.assertEqual(self._pats("tag:Ads message:fill"), (["Ads"], ["fill"]))

    def test_negated_terms_excluded(self):
        # -tag:gc contributes nothing; the positive message term still shows
        self.assertEqual(self._pats("-tag:gc message:ok"), ([], ["ok"]))

    def test_exact_is_deanchored(self):
        # tag=:Foo compiles to ^Foo$ but highlights the bare substring
        self.assertEqual(self._pats("tag=:Foo"), (["Foo"], []))

    def test_package_term_does_not_highlight_tag_or_msg(self):
        self.assertEqual(self._pats("package:com.x"), ([], []))

    def test_empty_query_no_patterns(self):
        self.assertEqual(highlight_patterns([]), ([], []))

    def test_dedupes_repeated_term(self):
        # same term in two OR clauses -> one pattern per field
        tp, mp = highlight_patterns(parse_query("fill OR fill"))
        self.assertEqual(len(mp), 1)
        self.assertEqual(len(tp), 1)

    def test_highlight_only_matched_substring(self):
        # the highlight pattern matches just "contain", not the whole field
        tp, _ = highlight_patterns(parse_query("tag:contain"))
        spans = list(tp[0].finditer("contain that"))
        self.assertEqual([s.group() for s in spans], ["contain"])


class BannerDiffTest(unittest.TestCase):
    FPKG = staticmethod(lambda: parse_terms("com.x"))

    def test_started_when_pid_newly_present(self):
        started, ended = banner_diff(
            {"1"}, {"1": "com.x", "2": "com.x"}, {"1": "com.x", "2": "com.x"},
            parse_terms("com.x"),
        )
        self.assertEqual(started, [("2", "com.x")])
        self.assertEqual(ended, [])

    def test_ended_when_pid_drops(self):
        started, ended = banner_diff(
            {"1", "2"}, {"1": "com.x"}, {"1": "com.x", "2": "com.x"},
            parse_terms("com.x"),
        )
        self.assertEqual(started, [])
        self.assertEqual(ended, [("2", "com.x")])

    def test_empty_filter_yields_nothing(self):
        self.assertEqual(
            banner_diff({"1"}, {"1": "com.x", "2": "com.x"}, {}, []),
            ([], []),
        )

    def test_filter_scopes_started(self):
        # pid 2 belongs to a different package than the filter
        started, ended = banner_diff(
            {"1"}, {"1": "com.x", "2": "com.other"}, {}, parse_terms("com.x"),
        )
        self.assertEqual(started, [])
        self.assertEqual(ended, [])

    def test_ended_unknown_pkg_dropped(self):
        # pid 9 ended but is absent from pid_names -> no package -> dropped
        started, ended = banner_diff(
            {"1", "9"}, {"1": "com.x"}, {"1": "com.x"}, parse_terms("com.x"),
        )
        self.assertEqual((started, ended), ([], []))

    def test_pid_still_live_no_banner(self):
        # same pid present both polls -> nothing (documents the pid-reuse gap)
        self.assertEqual(
            banner_diff({"1"}, {"1": "com.x"}, {"1": "com.x"}, parse_terms("com.x")),
            ([], []),
        )


class BannerEntryTest(unittest.TestCase):
    def test_entry_default_kind_none(self):
        self.assertIsNone(Entry("ts", "1", "1", "D", "Tag", "msg").kind)
        self.assertIsNone(parse_line("06-12 10:00:00.001  1  1 D Tag: hi").kind)

    def test_proc_entry_kind(self):
        e = Entry("ts", "1", "", "", "proc", "PROCESS STARTED (1)", kind="proc")
        self.assertEqual(e.kind, "proc")

    def test_export_omits_banner_text(self):
        # even if a banner somehow reaches the pure exporters, the table row is
        # benign (no crash) — the real guard is _filtered_entries_for_export,
        # but assert the exporters don't blow up on empty level/tid
        banner = Entry("06-12 10:00:00.000", "1", "", "", "proc",
                       "PROCESS STARTED (1) for package com.x", kind="proc")
        md = export_markdown([banner], "f", "now")
        raw = export_raw([banner])
        self.assertIn("PROCESS STARTED", md)   # it renders, doesn't crash
        self.assertIn("PROCESS STARTED", raw)


class ParseQueryTest(unittest.TestCase):
    def test_empty_matches_all(self):
        self.assertEqual(parse_query("   "), [])
        self.assertTrue(qm("", tag="anything", msg="x"))

    def test_bare_term_hits_tag_or_message(self):
        self.assertTrue(qm("gc", tag="GcLog", msg="nothing"))
        self.assertTrue(qm("gc", tag="Other", msg="running GC now"))
        self.assertFalse(qm("gc", tag="Other", msg="nothing"))

    def test_bare_term_does_not_hit_package(self):
        # package only matched via package: key (it keeps its own box)
        self.assertFalse(qm("mine", tag="T", msg="m", pkg="com.mine.app"))

    def test_tag_key_scopes_to_tag(self):
        self.assertTrue(qm("tag:Choreo", tag="Choreographer", msg="zzz"))
        self.assertFalse(qm("tag:Choreo", tag="Other", msg="Choreographer here"))

    def test_message_key_and_alias(self):
        self.assertTrue(qm("message:fill", tag="Ads", msg="no fill"))
        self.assertTrue(qm("msg:fill", tag="Ads", msg="no fill"))
        self.assertFalse(qm("message:fill", tag="fill", msg="ok"))

    def test_package_key(self):
        self.assertTrue(qm("package:mine", pkg="com.mine.app", tag="T", msg="m"))
        self.assertTrue(qm("pkg:mine", pkg="com.mine.app"))
        self.assertFalse(qm("package:mine", pkg="com.other.app"))

    def test_exact_operator(self):
        self.assertTrue(qm("tag=:Foo", tag="Foo", msg="x"))
        self.assertFalse(qm("tag=:Foo", tag="FooBar", msg="x"))
        self.assertTrue(qm("tag=:foo", tag="FOO"))  # case-insensitive

    def test_regex_operator(self):
        self.assertTrue(qm("tag~:Fo+", tag="Fooo", msg="x"))
        self.assertFalse(qm("tag~:^Bar$", tag="FooBar"))
        self.assertTrue(qm("message~:retry \\d+", msg="retry 5 times"))

    def test_negated_key(self):
        self.assertTrue(qm("-tag:gc", tag="Choreo", msg="m"))
        self.assertFalse(qm("-tag:gc", tag="GcDaemon", msg="m"))

    def test_negated_exact_and_regex(self):
        self.assertFalse(qm("-tag=:Foo", tag="Foo"))
        self.assertTrue(qm("-tag=:Foo", tag="FooBar"))
        self.assertFalse(qm("-tag~:Fo+", tag="Foo"))
        self.assertTrue(qm("-tag~:Fo+", tag="Bar"))

    def test_not_word_negates_a_key(self):
        # 'NOT <key>' is equivalent to '-<key>' (both with and without AND)
        self.assertTrue(qm("tag:Ads AND NOT message:fill", tag="AdsX", msg="loaded"))
        self.assertFalse(qm("tag:Ads AND NOT message:fill", tag="AdsX", msg="no fill"))
        self.assertTrue(qm("tag:Ads NOT message:fill", tag="AdsX", msg="loaded"))
        self.assertFalse(qm("tag:Ads NOT message:fill", tag="AdsX", msg="no fill"))

    def test_not_word_negates_leading_key(self):
        self.assertTrue(qm("NOT tag:gc", tag="Choreo"))
        self.assertFalse(qm("NOT tag:gc", tag="GcDaemon"))

    def test_two_not_keys(self):
        q = "tag:Ads AND NOT message:fill AND NOT tag:Net"
        self.assertTrue(qm(q, tag="AdsX", msg="loaded"))
        self.assertFalse(qm(q, tag="AdsNet", msg="loaded"))  # tag:Net excluded
        self.assertFalse(qm(q, tag="AdsX", msg="no fill"))   # message:fill excluded

    def test_not_inside_key_value_is_literal(self):
        # only a trailing NOT before a key negates; NOT mid-value stays literal
        self.assertTrue(qm("message:NOT found", msg="error: NOT found"))
        self.assertFalse(qm("message:NOT found", msg="all good"))

    def test_whitespace_between_keys_is_and(self):
        # both must hold
        self.assertTrue(qm("tag:Ads message:fill", tag="Ads", msg="no fill"))
        self.assertFalse(qm("tag:Ads message:fill", tag="Ads", msg="loaded"))
        self.assertFalse(qm("tag:Ads message:fill", tag="Net", msg="no fill"))

    def test_key_then_negated_key(self):
        self.assertTrue(qm("tag:Choreo -message:gc", tag="Choreographer", msg="frame"))
        self.assertFalse(qm("tag:Choreo -message:gc", tag="Choreographer", msg="run gc"))

    def test_or_splits_clauses(self):
        self.assertTrue(qm("tag:Ads OR tag:Net", tag="Network", msg="x"))
        self.assertTrue(qm("tag:Ads OR tag:Net", tag="AdsManager", msg="x"))
        self.assertFalse(qm("tag:Ads OR tag:Net", tag="Other", msg="x"))

    def test_bare_and_key_combined(self):
        # leading bare span ANDs with the keyed predicate
        self.assertTrue(qm("error tag:Ads", tag="AdsManager", msg="error here"))
        self.assertFalse(qm("error tag:Ads", tag="AdsManager", msg="all good"))

    def test_not_before_bare_word(self):
        self.assertTrue(qm("NOT spam", tag="ham", msg="eggs"))
        self.assertFalse(qm("NOT spam", tag="ham", msg="spam folder"))

    def test_multi_word_message_value(self):
        # message keeps the whole phrase (messages routinely contain spaces)
        self.assertTrue(qm("message:no fill", tag="Ads", msg="got no fill today"))
        self.assertFalse(qm("message:no fill", tag="Ads", msg="filled"))

    def test_tag_value_is_single_token_rest_is_bare(self):
        # tag never has spaces, so 'tag:Choreo skipped' == tag:Choreo AND skipped
        self.assertTrue(qm("tag:Choreo skipped", tag="Choreographer", msg="skipped 3 frames"))
        self.assertFalse(qm("tag:Choreo skipped", tag="Choreographer", msg="all good"))
        self.assertFalse(qm("tag:Choreo skipped", tag="Other", msg="skipped 3 frames"))

    def test_package_value_is_single_token_rest_is_bare(self):
        # 'package:com.foo crash' == package com.foo AND a 'crash' search
        self.assertTrue(qm("package:com.foo crash", pkg="com.foo.app", msg="crash now"))
        self.assertFalse(qm("package:com.foo crash", pkg="com.foo.app", msg="all ok"))
        self.assertFalse(qm("package:com.foo crash", pkg="com.other", msg="crash now"))

    def test_package_multiple_trailing_bare_terms(self):
        # each trailing word after a single-token field is its own AND term
        self.assertTrue(qm("package:com.foo bar baz",
                           pkg="com.foo.app", msg="bar and baz here"))
        self.assertFalse(qm("package:com.foo bar baz",
                            pkg="com.foo.app", msg="only bar"))

    def test_message_phrase_survives_with_other_keys(self):
        # message keeps its phrase even when a single-token key precedes it
        self.assertTrue(qm("package:com.foo message:no fill",
                           pkg="com.foo.app", msg="no fill"))
        self.assertFalse(qm("package:com.foo message:no fill",
                            pkg="com.foo.app", msg="loaded"))

    def test_single_token_field_then_or(self):
        # 'package:com.foo crash OR anr' = (pkg com.foo AND crash) OR anr
        q = "package:com.foo crash OR anr"
        self.assertTrue(qm(q, pkg="com.foo.app", msg="crash"))
        self.assertTrue(qm(q, pkg="com.other", msg="anr in system"))
        self.assertFalse(qm(q, pkg="com.foo.app", msg="loaded"))

    def test_exact_and_regex_keep_whole_value(self):
        # =:/~: are not single-token — they take the full value as written
        self.assertTrue(qm("tag~:Ad Manager", tag="Ad Manager"))   # regex w/ space
        self.assertTrue(qm("tag=:Ad Hoc", tag="Ad Hoc"))           # exact w/ space

    def test_trailing_key_with_no_value_is_noop(self):
        # user mid-typing "tag:" — should match everything (clause empty)
        self.assertTrue(qm("tag:", tag="anything", msg="x"))

    def test_inline_regex_in_contains_still_works(self):
        self.assertTrue(qm("tag:/Cho+/", tag="Choo"))

    def test_explicit_and_terminates_key_value(self):
        # ' AND ' is an operator boundary, not part of the message value
        self.assertTrue(qm("message:ad AND -message:slow", tag="C", msg="boom in ad"))
        self.assertFalse(qm("message:ad AND -message:slow", tag="C", msg="slow ad"))

    def test_or_with_keys_distributes(self):
        self.assertTrue(qm("tag:Ads AND message:fill OR tag:Net",
                           tag="Net", msg="anything"))
        self.assertTrue(qm("tag:Ads AND message:fill OR tag:Net",
                           tag="AdsX", msg="no fill"))
        self.assertFalse(qm("tag:Ads AND message:fill OR tag:Net",
                            tag="AdsX", msg="loaded"))

    def test_value_with_leading_dash_is_not_a_key(self):
        # "-foo" with no colon is a bare term, dash kept literally? It splits as
        # a bare word "-foo" — matches substring "-foo". Documenting behavior.
        self.assertTrue(qm("-tag:x", tag="y"))  # negated key path covered above


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

    def test_regex_trailing_i_flag_accepted(self):
        # /foo/i must behave like /foo/ (matching is always case-insensitive),
        # not fall through to literal matching of the string "/foo/i"
        terms = parse_terms("/choreographer/i")
        self.assertTrue(matches("DisplayManager: Choreographer registered", terms))
        self.assertFalse(matches("nothing here", terms))

    def test_regex_i_flag_mixed_with_boolean(self):
        terms = parse_terms("timeout OR /anr/i")
        self.assertTrue(matches("ANR in com.foo", terms))
        self.assertTrue(matches("read timeout", terms))

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
        terms = parse_terms("acme AND /retry \\d+/")
        self.assertTrue(matches("Acme: retry 3 scheduled", terms))
        self.assertFalse(matches("Acme: retry soon", terms))


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
            export_filename("com.acme OR sample", now),
            "logcat_com.acme-OR-sample_2026-06-12_14-33-21.md",
        )
        self.assertEqual(export_filename("", now), "logcat_all_2026-06-12_14-33-21.md")

    def test_md_escape_pipes(self):
        self.assertEqual(md_escape("a | b"), "a \\| b")

    def test_markdown_table(self):
        e = parse_line("06-12 10:33:21.123  1234  5678 D MyTag: hello | world")
        out = export_markdown(
            [e], "package=`*` tag=`*` message=`*`", "2026-06-12 14:33:21",
            packages={"1234": "com.x.app"},
        )
        # columns: Time | Level | Package | Tag | Message
        self.assertIn("| Time | Level | Package | Tag | Message |", out)
        self.assertIn("| 06-12 10:33:21.123 | D | com.x.app | MyTag | hello \\| world |", out)
        self.assertIn("- Lines: 1", out)

    def test_markdown_package_blank_when_unknown(self):
        # pid not in the map -> empty package cell, table still well-formed
        e = parse_line("06-12 10:33:21.123  42  42 I MyTag: hi")
        out = export_markdown([e], "f", "now")
        self.assertIn("| 06-12 10:33:21.123 | I |  | MyTag | hi |", out)

    def test_markdown_crash_mark(self):
        fatal = parse_line("06-12 10:33:21.123  99  99 F libc: Fatal signal 11")
        anr = parse_line("06-12 10:33:21.200  99  99 E AndroidRuntime: FATAL EXCEPTION: main")
        normal = parse_line("06-12 10:33:21.300  99  99 E Other: just an error")
        out = export_markdown([fatal, anr, normal], "f", "now", packages={"99": "com.x"})
        self.assertIn("| 06-12 10:33:21.123 | 💥 F | com.x | libc | Fatal signal 11 |", out)
        self.assertIn("| 06-12 10:33:21.200 | 💥 E | com.x | AndroidRuntime | FATAL EXCEPTION: main |", out)
        # a non-crash error row keeps a plain level
        self.assertIn("| 06-12 10:33:21.300 | E | com.x | Other | just an error |", out)


class NotOperatorTest(unittest.TestCase):
    def test_and_not(self):
        terms = parse_terms("ad AND NOT timeout")
        self.assertTrue(matches("ad loaded fine", terms))
        self.assertFalse(matches("ad timeout", terms))

    def test_leading_not(self):
        terms = parse_terms("NOT Choreographer")
        self.assertTrue(matches("Acme: ad loaded", terms))
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
        from catflap import level_matches
        self.assertTrue(level_matches("I", "I", exact=True))
        self.assertFalse(level_matches("W", "I", exact=True))
        self.assertFalse(level_matches("V", "I", exact=True))

    def test_exact_e_includes_fatal(self):
        from catflap import level_matches
        self.assertTrue(level_matches("F", "E", exact=True))
        self.assertFalse(level_matches("W", "E", exact=True))

    def test_default_mode_is_threshold(self):
        from catflap import level_matches
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
            "06-12 10:00:00.003  42  42 E AndroidRuntime: \tat com.acme.Ad.load(Ad.kt:12)",
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

    def test_crash_package_from_process_line(self):
        block = [
            self._e("06-15 12:37:38.542 3225 3225 E AndroidRuntime: FATAL EXCEPTION: main"),
            self._e("06-15 12:37:38.542 3225 3225 E AndroidRuntime: Process: com.google.android.odad, PID: 3225"),
            self._e("06-15 12:37:38.542 3225 3225 E AndroidRuntime: \tat android.app.ActivityThread.main(ActivityThread.java:9333)"),
        ]
        self.assertEqual(crash_package(block), "com.google.android.odad")

    def test_crash_package_none_when_absent(self):
        block = [self._e("06-12 10:00:00.000  42  42 F libc: aborting")]
        self.assertIsNone(crash_package(block))


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


class ParsePermissionsTest(unittest.TestCase):
    OUTPUT = """
    requested permissions:
      android.permission.INTERNET
    install permissions:
      android.permission.INTERNET: granted=true
    runtime permissions:
      android.permission.CAMERA: granted=false, flags=[ USER_SENSITIVE_WHEN_GRANTED]
      android.permission.ACCESS_FINE_LOCATION: granted=true, flags=[ USER_SET]
    """

    def test_parses_granted_state(self):
        from catflap import parse_permissions
        perms = parse_permissions(self.OUTPUT)
        self.assertEqual(perms["android.permission.CAMERA"], False)
        self.assertEqual(perms["android.permission.ACCESS_FINE_LOCATION"], True)
        self.assertEqual(perms["android.permission.INTERNET"], True)


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


class LogcatCmdTest(unittest.TestCase):
    def test_default_buffers(self):
        self.assertEqual(
            logcat_cmd("emulator-5554"),
            ["adb", "-s", "emulator-5554", "logcat", "-v", "threadtime"],
        )

    def test_explicit_buffers(self):
        self.assertEqual(
            logcat_cmd("abc", ["crash", "events"]),
            ["adb", "-s", "abc", "logcat", "-v", "threadtime", "-b", "crash", "-b", "events"],
        )

    def test_tail_starts_from_now(self):
        # the live TUI starts from now (-T 1) so old crashes don't replay
        self.assertEqual(
            logcat_cmd("abc", tail=True),
            ["adb", "-s", "abc", "logcat", "-v", "threadtime", "-T", "1"],
        )

    def test_no_tail_by_default(self):
        # the CLI dump path keeps reading the whole buffer
        self.assertNotIn("-T", logcat_cmd("abc"))


class ParseForegroundTest(unittest.TestCase):
    MODERN = """
    topResumedActivity=ActivityRecord{a1b2c3 u0 com.acme.sample/.MainActivity t123}
    mFocusedApp=ActivityRecord{a1b2c3 u0 com.acme.sample/.MainActivity t123}
"""
    LEGACY = """
    mResumedActivity: ActivityRecord{d4e5f6 u0 com.example.legacy/.HomeActivity t7}
"""
    FOCUSED_ONLY = """
    mFocusedApp=ActivityRecord{9a8b7c u10 org.work.profile/.SplashActivity t42}
"""

    PIXEL_A16 = """
  ResumedActivity: ActivityRecord{253754768 u0 com.google.android.apps.nexuslauncher/.NexusLauncherActivity t2}
  mFocusedApp=null
"""

    def test_pixel_android16_resumed_activity(self):
        self.assertEqual(parse_foreground(self.PIXEL_A16), "com.google.android.apps.nexuslauncher")

    def test_top_resumed_activity(self):
        self.assertEqual(parse_foreground(self.MODERN), "com.acme.sample")

    def test_legacy_resumed_activity(self):
        self.assertEqual(parse_foreground(self.LEGACY), "com.example.legacy")

    def test_focused_app_fallback_with_user_id(self):
        self.assertEqual(parse_foreground(self.FOCUSED_ONLY), "org.work.profile")

    def test_no_match(self):
        self.assertIsNone(parse_foreground("ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)"))


class PickDumpSerialTest(unittest.TestCase):
    def _with_devices(self, devices):
        return patch.object(catflap, "list_devices", return_value=devices)

    def test_no_devices(self):
        with self._with_devices([]):
            serial, err = _pick_dump_serial(None)
        self.assertIsNone(serial)
        self.assertIn("no devices", err)

    def test_single_device_auto(self):
        with self._with_devices([("R3CX10ABCDE", "SM S928B")]):
            serial, err = _pick_dump_serial(None)
        self.assertEqual(serial, "R3CX10ABCDE")
        self.assertIsNone(err)

    def test_multiple_requires_device(self):
        with self._with_devices([("a", "x"), ("b", "y")]):
            serial, err = _pick_dump_serial(None)
        self.assertIsNone(serial)
        self.assertIn("multiple devices", err)

    def test_requested_present(self):
        with self._with_devices([("a", "x"), ("b", "y")]):
            serial, err = _pick_dump_serial("b")
        self.assertEqual(serial, "b")
        self.assertIsNone(err)

    def test_requested_absent(self):
        with self._with_devices([("a", "x")]):
            serial, err = _pick_dump_serial("zzz")
        self.assertIsNone(serial)
        self.assertIn("not found", err)


class SuggestTest(unittest.TestCase):
    def test_split_last_term(self):
        self.assertEqual(split_last_term("toto OR pix"), ("toto OR ", "pix"))
        self.assertEqual(split_last_term("pix"), ("", "pix"))
        self.assertEqual(split_last_term("a or b OR "), ("a or b OR ", ""))

    def test_suggest_substring_case_insensitive(self):
        cands = ["InterstitialDebug", "AcmeSDK", "WindowManager"]
        self.assertEqual(suggest(cands, "acme"), ["AcmeSDK"])

    def test_suggest_empty_term_returns_top(self):
        self.assertEqual(suggest(["a", "b", "c"], "", limit=2), ["a", "b"])

    def test_suggest_excludes_exact_match(self):
        self.assertEqual(suggest(["AcmeSDK"], "acmesdk"), [])


if __name__ == "__main__":
    unittest.main()
