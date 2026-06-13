---
name: catflap
description: Read filtered Android logcat from a connected device or emulator using catflap's boolean/regex filter syntax. Use when an agent needs app logs, crash traces, or errors from an Android device via adb — especially when filtering by package, level, or a boolean/regex expression.
---

# catflap — logcat for agents

catflap is a terminal logcat viewer. Running bare `catflap` opens a full-screen
interactive UI that **blocks** and cannot be driven by an agent. **Never run bare
`catflap`.** Use the `catflap dump` subcommand, which prints filtered logcat to
stdout and exits.

## When to use

- The user asks why their app crashed, hung, or misbehaved and logs would help.
- You need errors, a stack trace, or specific log lines from a connected device.
- Plain `adb logcat | grep` is awkward because the filter needs boolean logic,
  regex, package-name filtering, or structured output.

Requires `adb` on PATH with a device/emulator connected.

## Command

```
catflap dump [--device SERIAL] [--package EXPR] [--tag EXPR] [--message EXPR]
             [--level V|D|I|W|E] [--exact] [--buffer main|system|crash|events|radio]
             [--lines N] [--follow] [--format text|jsonl]
```

- `--device` — adb serial. Optional with one device; **required** if several are
  connected (the command errors and lists them otherwise).
- `--package` / `--tag` / `--message` — filters on process name / log tag /
  message text. All accept the **filter syntax** below. `--package` resolves the
  process name from the device, so it keeps working across app restarts.
- `--level` — minimum severity (default `V`). `--exact` matches only that level.
- `--lines` — stop after N matching lines (default 500; `0` = unlimited). Without
  `--follow` it dumps the existing buffer and exits.
- `--follow` — keep streaming (until killed) instead of dumping and exiting.
- `--format` — `text` (default) or `jsonl` (one JSON object per line:
  `ts pid tid level package tag message`). **Prefer `jsonl` for parsing.**

## Filter syntax (the reason to use this over `adb | grep`)

Each filter expression supports:

- **AND / OR / NOT** (uppercase only): `a AND b`, `x OR y`, `NOT z`.
  `AND` binds tighter than `OR`: `lost AND found OR stolen` = `(lost AND found) OR stolen`.
- **/regex/** — wrap a term in slashes: `/retry \d+/`, `/anr|timeout/`. All
  matching is **case-insensitive**, so no flag is needed; a trailing `i`
  (`/anr/i`) is accepted but redundant.
- Everything else is a case-insensitive substring; lowercase `or`/`and` are literal.

## Examples

```bash
# Errors from one app, structured output:
catflap dump --package com.example.app --level E --format jsonl

# Boolean message filter, last 200 lines:
catflap dump --message "timeout OR anr OR /fatal/i" --lines 200

# Exclude noise while watching an app:
catflap dump --package "com.example.app AND NOT gms" --level W

# Crash buffer only:
catflap dump --buffer crash --lines 100

# Pick a device when several are attached:
catflap dump --device emulator-5554 --tag ActivityManager
```

## Notes

- The output is plain stdout — pipe, redirect, or parse it directly.
- `text` format is `TIMESTAMP LEVEL PACKAGE_OR_PID TAG: MESSAGE`.
- In `jsonl`, `package` is `""` for system/server logs whose PID maps to no app
  (e.g. `system_server`); it's filled in for real app processes.
- If nothing matches, the command prints nothing and exits 0. Empty output means
  "no matching lines" — if unsure your filter is right, first run it with a term
  you know is present (e.g. a common tag) to confirm it returns lines.
- Errors (no device, ambiguous device, adb missing) go to stderr with exit 1.
