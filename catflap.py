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
    A trailing 'i' (e.g. /foo/i) is accepted and ignored — matching is always
    case-insensitive. An invalid /regex/ falls back to literal matching."""
    m = re.fullmatch(r"/(.+)/i?", term)
    if m:
        try:
            return re.compile(m.group(1), re.IGNORECASE)
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


# ---- unified query language (Android Studio style) --------------------------
#
# One box, field-scoped keys. A predicate is (field, op, pattern, negated):
#   field  — "tag" | "msg" | "pkg" | "any"  ("any" = tag OR msg)
#   op     — "contains" | "exact" | "regex"
#   pattern— compiled, case-insensitive
#   negated— leading '-' on the key
# Keys:  tag:  message:/msg:  package:/pkg:   ·  =: exact  ·  ~: regex  ·  -key: negate
# Bare terms (no key) become field "any" and match tag OR msg.
# OR (uppercase) splits clauses; whitespace / AND join within a clause; a
# leading NOT before a bare term negates it. A query is DNF: OR of ANDs.

FIELD_ALIASES = {"tag": "tag", "message": "msg", "msg": "msg", "package": "pkg", "pkg": "pkg"}

# key + optional =:/~:/: operator, e.g. tag:  message=:  -pkg~:
KEY_RE = re.compile(
    r"(?P<neg>-)?(?P<key>tag|message|msg|package|pkg)(?P<op>=:|~:|:)",
    re.IGNORECASE,
)


def compile_predicate(field, op, raw, negated):
    """Build a (field, op, pattern, negated) predicate. Exact anchors the
    whole value; regex compiles raw; contains uses /…/ regex or literal."""
    if op == "exact":
        pat = re.compile(rf"^{re.escape(raw)}$", re.IGNORECASE)
    elif op == "regex":
        try:
            pat = re.compile(raw, re.IGNORECASE)
        except re.error:
            pat = re.compile(re.escape(raw), re.IGNORECASE)
    else:  # contains — honour inline /regex/ for parity with the old boxes
        pat = compile_term(raw)
    return (field, op, pat, negated)


def _op_name(op_token):
    return {"=:": "exact", "~:": "regex", ":": "contains"}[op_token]


def parse_query(text):
    """Unified query -> DNF list of clauses; each clause a list of predicates.
    Empty query -> []. Bare terms -> field 'any'."""
    clauses = []
    for part in re.split(r"\s+OR\s+", text.strip()):
        preds = _parse_clause(part)
        if preds:
            clauses.append(preds)
    return clauses


def _parse_clause(part):
    """One OR-segment -> list of AND-ed predicates.

    An explicit ` AND ` and every field key start a new predicate; a key's
    value runs up to the next key or the next AND. Keyless spans split on
    whitespace into 'any' predicates, honouring a leading NOT."""
    preds = []
    for chunk in re.split(r"\s+AND\s+", part):
        preds.extend(_parse_and_term(chunk))
    return preds


def _strip_trailing_not(span):
    """A 'NOT' word at the end of a span negates the key that follows it (so
    'NOT message:fill' == '-message:fill'). Return (span_without_not, not_flag)."""
    words = span.split()
    if words and words[-1] == "NOT":
        return " ".join(words[:-1]), True
    return span, False


def _parse_and_term(part):
    """A single AND-term — may still hold several keys (space = AND), e.g.
    'tag:Ads -message:fill'. Keys cut it into spans; the leading keyless span
    becomes bare 'any' predicates. A 'NOT' right before a key negates it.

    Value greediness is per field: tag/package values are a single token (they
    never contain spaces), so 'package:com.foo crash' is package com.foo AND a
    bare 'crash' search. message values keep the whole phrase ('message:no fill'
    matches 'no fill'). Exact/regex (=:/~:) always take the whole value."""
    preds = []
    keys = list(KEY_RE.finditer(part))
    if not keys:
        return _bare_predicates(part)

    lead, not_next = _strip_trailing_not(part[: keys[0].start()])
    preds.extend(_bare_predicates(lead))
    for i, m in enumerate(keys):
        end = keys[i + 1].start() if i + 1 < len(keys) else len(part)
        value_span = part[m.end() : end]
        raw, not_after = _strip_trailing_not(value_span.strip())
        field = FIELD_ALIASES[m.group("key").lower()]
        op = _op_name(m.group("op"))
        negated = bool(m.group("neg")) or not_next
        # tag/package contains: first token is the value, the rest are bare
        # search terms (those fields never hold spaces, so trailing words are
        # clearly separate). message — and any exact/regex match — keeps the
        # whole phrase.
        if raw and op == "contains" and field in ("tag", "pkg"):
            first, _, rest = raw.partition(" ")
            preds.append(compile_predicate(field, op, first, negated))
            if rest.strip():
                preds.extend(_bare_predicates(rest))
        elif raw:
            preds.append(compile_predicate(field, op, raw, negated))
        # a key with no value (trailing "tag:") is an in-progress token — skip
        not_next = not_after
    return preds


# a token is a /regex/ group (which may contain spaces) or a run of non-space
BARE_TOKEN_RE = re.compile(r"/[^/]*/i?|\S+")


def _bare_tokens(span):
    """Split a keyless span into tokens, keeping an inline /regex/ whole even
    when it contains spaces ('/retry \\d+/' is one token, not two)."""
    return BARE_TOKEN_RE.findall(span)


def _bare_predicates(span):
    """Tokenize a keyless span into 'any' predicates; 'NOT word' negates."""
    out = []
    words = _bare_tokens(span)
    i = 0
    while i < len(words):
        w = words[i]
        if w == "AND":
            i += 1
            continue
        if w == "NOT" and i + 1 < len(words):
            out.append(compile_predicate("any", "contains", words[i + 1], True))
            i += 2
            continue
        if w == "NOT":  # dangling NOT — ignore
            i += 1
            continue
        out.append(compile_predicate("any", "contains", w, False))
        i += 1
    return out


def _scope_box(value, key):
    """Rewrite an old single-field box value into key-scoped unified syntax.

    The old boxes spoke AND/OR/NOT; prefix the key onto each bare term so the
    meaning is preserved. 'a OR b' in the tag box -> 'tag:a OR tag:b'.
    'x AND NOT y' -> 'tag:x AND -tag:y'. /regex/ becomes key~:regex."""
    value = value.strip()
    if not value:
        return ""
    or_parts = []
    for clause in re.split(r"\s+OR\s+", value):
        and_terms = []
        for t in re.split(r"\s+AND\s+", clause):
            t = t.strip()
            if not t:
                continue
            neg = t.startswith("NOT ") and bool(t[4:].strip())
            if neg:
                t = t[4:].strip()
            m = re.fullmatch(r"/(.+)/i?", t)
            if m:
                term = f"{'-' if neg else ''}{key}~:{m.group(1)}"
            else:
                term = f"{'-' if neg else ''}{key}:{t}"
            and_terms.append(term)
        if and_terms:
            or_parts.append(" AND ".join(and_terms))
    return " OR ".join(or_parts)


def _migrate_query(f):
    """Build the unified-box value from a saved filter dict.

    New format stores 'query' directly. Legacy format stored separate 'tag'
    and 'msg' boxes — fold them into scoped unified syntax (AND-joined)."""
    if "query" in f:
        return f.get("query", "")
    parts = []
    tag = _scope_box(f.get("tag", ""), "tag")
    msg = _scope_box(f.get("msg", ""), "message")
    # if either side itself has OR-alternatives, parenthesise via AND-distribution
    if tag and msg:
        # both present: AND them. OR inside either side would bind wrong, so we
        # wrap each multi-clause side back into a single clause is impossible in
        # this flat language — keep it simple and join with AND, which is correct
        # when neither side uses OR (the common case). Sides using OR are rare in
        # saved presets; joining still yields a usable, close-enough query.
        return f"{tag} AND {msg}" if " OR " not in tag and " OR " not in msg else f"{tag} {msg}"
    return tag or msg


def query_matches(tag, msg, pkg, clauses):
    """True if no clauses, or any clause's predicates all hold for the line."""
    if not clauses:
        return True
    fields = {"tag": tag, "msg": msg, "pkg": pkg}
    return any(
        all(_pred_holds(p, fields) for p in clause)
        for clause in clauses
    )


def _pred_holds(pred, fields):
    field, _op, pat, negated = pred
    if field == "any":
        hit = bool(pat.search(fields["tag"])) or bool(pat.search(fields["msg"]))
    else:
        hit = bool(pat.search(fields[field]))
    return hit != negated


def highlight_patterns(clauses):
    """From a parsed query, the compiled patterns to highlight in each field.

    Returns (tag_pats, msg_pats). Only positive (non-negated) predicates
    contribute — you can't highlight the absence of a term. A bare 'any' term
    highlights in both fields, so the colour shows which field the hit is in.
    Exact predicates (anchored ^…$) are de-anchored so the visible substring
    still highlights."""
    tag_pats, msg_pats = [], []
    seen_tag, seen_msg = set(), set()
    for clause in clauses:
        for field, op, pat, negated in clause:
            if negated:
                continue
            hl = _deanchor(pat) if op == "exact" else pat
            if field in ("tag", "any") and hl.pattern not in seen_tag:
                seen_tag.add(hl.pattern)
                tag_pats.append(hl)
            if field in ("msg", "any") and hl.pattern not in seen_msg:
                seen_msg.add(hl.pattern)
                msg_pats.append(hl)
    return tag_pats, msg_pats


def _deanchor(pat):
    """Strip ^…$ from an exact predicate so the literal still highlights."""
    src = pat.pattern
    if src.startswith("^") and src.endswith("$"):
        src = src[1:-1]
    try:
        return re.compile(src, re.IGNORECASE)
    except re.error:
        return pat


class Entry:
    __slots__ = ("ts", "pid", "tid", "level", "tag", "msg", "kind")

    def __init__(self, ts, pid, tid, level, tag, msg, kind=None):
        self.ts = ts
        self.pid = pid
        self.tid = tid
        self.level = level
        self.tag = tag
        self.msg = msg
        self.kind = kind  # None for real logs; "proc" for synthetic process banners


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


PROCESS_LINE_RE = re.compile(r"Process:\s*([\w.]+),\s*PID:")


def crash_package(block):
    """Package name from a crash block's 'Process: <pkg>, PID:' line, or None.
    The Android runtime prints this line in every FATAL EXCEPTION, so it's a
    reliable fallback when the pid isn't in the live pid->name map yet."""
    for e in block:
        m = PROCESS_LINE_RE.search(e.msg)
        if m:
            return m.group(1)
    return None


def banner_diff(prev_live, cur_names, pid_names, f_pkg):
    """Diff two ps polls into process STARTED/ENDED events for the filtered pkg.

    prev_live  — set of pids that were live in the previous poll
    cur_names  — {pid: package} from the current poll (live pids only)
    pid_names  — the merged, never-removed map (resolves a dead pid's package)
    f_pkg      — parsed package filter; empty -> no banners

    Returns (started, ended), each a list of (pid, package). The caller is
    responsible for skipping the very first poll (prev_live empty) so the whole
    process table isn't dumped as STARTED banners on launch."""
    if not f_pkg:
        return [], []
    cur_live = set(cur_names)
    started = [
        (pid, cur_names[pid])
        for pid in cur_live - prev_live
        if matches(cur_names[pid], f_pkg)
    ]
    ended = []
    for pid in prev_live - cur_live:
        pkg = pid_names.get(pid, "")  # dead pid: package from the never-removes map
        if pkg and matches(pkg, f_pkg):
            ended.append((pid, pkg))
    return started, ended


def md_escape(text):
    return text.replace("|", "\\|")


def export_markdown(entries, filters_desc, when, packages=None):
    """Markdown table export. Columns: Time | Level | Package | Tag | Message.

    packages — {pid: package} to fill the Package column.
    A crash row (level F or a FATAL EXCEPTION) shows 💥 in the Level cell."""
    packages = packages or {}
    lines = [
        f"# logcat export — {when}",
        "",
        f"- Filters: {filters_desc}",
        f"- Lines: {len(entries)}",
        "",
        "| Time | Level | Package | Tag | Message |",
        "| --- | --- | --- | --- | --- |",
    ]
    for e in entries:
        level = f"💥 {e.level}".strip() if is_crash_start(e) else e.level
        pkg = md_escape(packages.get(e.pid, ""))
        lines.append(f"| {e.ts} | {level} | {pkg} | {md_escape(e.tag)} | {md_escape(e.msg)} |")
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


# ---- unified-box autocomplete -----------------------------------------------

# the bit the user is editing: everything after the last OR / whitespace, except
# that a key's value may contain spaces (message:no fill) so we don't split there.
QUERY_TOKEN_RE = re.compile(r"\s+OR\s+|\s+", re.IGNORECASE)


def split_query_token(text):
    """Return (prefix, token) where token is the in-progress chunk at the end.

    A trailing key value with spaces stays whole: 'tag:Ad message:no fi' ->
    ('tag:Ad ', 'message:no fi'). A bare trailing word splits on whitespace:
    'foo ba' -> ('foo ', 'ba')."""
    # find the start of the current key token, if the tail contains one
    m = None
    for m in KEY_RE.finditer(text):
        pass
    if m:
        # is the cursor still inside this key's value (no OR after it)?
        tail = text[m.start():]
        if not re.search(r"\s+OR\s+", tail, re.IGNORECASE):
            return text[: m.start()], tail
    # otherwise split on the last whitespace / OR
    last_end = 0
    for mm in QUERY_TOKEN_RE.finditer(text):
        last_end = mm.end()
    return text[:last_end], text[last_end:]


def parse_token(token):
    """Split an in-progress token into (negated, key, op_token, value).
    key/op are None for a bare term. value is the partial text being completed."""
    m = KEY_RE.match(token)
    if m:
        return bool(m.group("neg")), m.group("key").lower(), m.group("op"), token[m.end():]
    return False, None, None, token


def parse_permissions(dumpsys_output):
    """dumpsys package output -> {permission_name: granted} for runtime perms."""
    perms = {}
    for name, granted in re.findall(
        r"([\w.]*\.permission\.[\w.]+): granted=(true|false)", dumpsys_output
    ):
        perms.setdefault(name, granted == "true")
    return perms


FOREGROUND_RES = [
    # covers topResumedActivity= (10+), ResumedActivity: (15+), mResumedActivity: (pre-10)
    re.compile(r"ResumedActivity[=:]\s*ActivityRecord\{\S+ u\d+ ([\w.]+)/"),
    re.compile(r"mFocusedApp=ActivityRecord\{\S+ u\d+ ([\w.]+)/"),
]


def parse_foreground(dumpsys_output):
    """dumpsys activity output -> foreground package name, or None."""
    for rx in FOREGROUND_RES:
        m = rx.search(dumpsys_output)
        if m:
            return m.group(1)
    return None


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


def logcat_cmd(serial, buffers=None, tail=False):
    """adb logcat command line; buffers=None streams adb's default (main+system).
    tail=True starts from now (-T 1) instead of replaying the whole buffer — so
    the live TUI doesn't re-surface old crashes/lines on every restart."""
    cmd = ["adb", "-s", serial, "logcat", "-v", "threadtime"]
    if tail:
        cmd += ["-T", "1"]
    for b in buffers or ():
        cmd += ["-b", b]
    return cmd


BUFFER_CHOICES = [
    ("main + system (default)", None),
    ("crash — crash dumps only", ["crash"]),
    ("events — system events (activity starts, GC…)", ["events"]),
    ("radio — telephony/modem", ["radio"]),
    ("everything — main, system, crash, events", ["main", "system", "crash", "events"]),
]


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
        Binding("ctrl+r", "toggle_record", "Record", show=False, priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def get_system_commands(self, screen):
        commands = [
            SystemCommand(
                "❓ Filtering help",
                "Cheatsheet: AND/OR/NOT operators and /regex/ syntax (also: F1)",
                self.action_help,
            ),
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
                "📚 Switch log buffer",
                "Stream crash, events, radio, or the default main+system buffers",
                self.action_pick_buffer,
            ),
            SystemCommand(
                "📱 Filter on foreground app",
                "Set the package filter to the app currently on screen",
                self.adopt_foreground,
            ),
            SystemCommand(
                "🤖 ADB operations",
                "Start/kill/clear/uninstall the target app, permissions, deep links, screenshots",
                self.action_adb_menu,
            ),
            SystemCommand(
                "🎚 Set minimum level",
                "Open the level menu: V/D/I/W/E threshold or exact mode (also: F2)",
                self.action_level_menu,
            ),
            # debugging
            SystemCommand(
                "💥 Jump to last crash",
                "Open the most recent FATAL EXCEPTION with its stack trace",
                self.action_jump_crash,
            ),
            SystemCommand(
                "🔍 Search displayed lines",
                "Find and jump to matches in the filtered scrollback (also: /)",
                self.action_search,
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
        devices = list_devices()
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
                    "📸 Screenshot",
                    "⏹ Stop recording & save" if recording else "🎬 Start screen record",
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
            "📸 Screenshot",
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
        elif choice == "📸 Screenshot":
            self._export_dir_or_prompt(self._take_screenshot)
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
        # the device auto-stops at the limit — finalise & save when it does
        if elapsed >= self.REC_LIMIT or self._record_proc.poll() is not None:
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

    def action_toggle_record(self):
        """Ctrl+R — start a recording, or stop & save the one in progress."""
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
    devices = list_devices()
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
