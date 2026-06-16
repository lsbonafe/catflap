"""Filtered-log exports (Markdown table / raw .log) and the export-dir helper."""

import re
from pathlib import Path

from catflap.crash import is_crash_start


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
