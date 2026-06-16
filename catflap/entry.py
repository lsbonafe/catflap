"""Parsed logcat line model and severity-level helpers."""

import re

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

LEVELS = ["V", "D", "I", "W", "E", "F"]


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
