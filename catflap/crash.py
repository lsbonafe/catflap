"""Crash detection, crash-block extraction, and process-banner diffing."""

import re

from catflap.filtering import matches


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
