"""adb plumbing: device listing, dumpsys parsing, and the logcat command line.

list_devices is monkeypatched in tests via ``catflap.adb.list_devices`` and is
called by the app as ``adb.list_devices()`` so the patch takes effect."""

import re
import subprocess


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
