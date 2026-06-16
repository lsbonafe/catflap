"""Command-line entry point: `catflap` opens the TUI, `catflap dump` prints
filtered logcat to stdout for scripts and AI agents."""

import json
import subprocess
import sys

from catflap import adb
from catflap.adb import logcat_cmd
from catflap.app import Catflap
from catflap.entry import LEVELS, level_matches, parse_line
from catflap.filtering import parse_terms, matches


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
    devices = adb.list_devices()
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
