# catflap 🐈🚪

The little door your Android logs come through. A terminal UI for logcat with Android Studio-grade filtering — live, fast, and keyboard-friendly. Built with [Textual](https://textual.textualize.io/).

![](https://img.shields.io/badge/python-3.9%2B-blue) ![](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey) ![](https://img.shields.io/badge/install-brew-orange)

![catflap screenshot](docs/screenshot.svg)

## Features

- **One unified query box, Android Studio–style**: scope a term to a field with `tag:`, `message:` or `package:`; add `=:` (exact), `~:` (regex), or a leading `-` to negate (`-tag:gc`). A bare word with no key matches the **tag OR the message**. Live autocomplete draws from the actual stream (process names, tags, frequent messages); typing `package:` pins the **foreground app** on top.
- **Boolean operators** compose with the keys: `tag:Ads AND -message:fill`, `wifi OR coffee`, `NOT /Choreographer|gralloc/` — uppercase operators, `AND` binds tighter than `OR`, `/slashes/` for regex, everything else literal
- **Match highlighting**: the terms you're filtering on are highlighted inline in the log — tag matches one colour, message matches another — so you can see *why* a line matched
- **Process banners**: with a `package:` filter set, an Android-Studio-style divider marks when that app's process starts or dies (`──── PROCESS STARTED (pid) … ────`), so restarts and crashes are obvious
- **Severity filtering**: clickable `Level` chip (or `F2`) with a dropdown; the chip text is tinted to the selected level; switchable operator — `≥` (that level and worse) or `=` (exactly that level)
- **Search the scrollback** (`/`): jump to matches across the whole buffer, not just what's on screen; `n`/`N` step between hits, same plain-text or `/regex/` syntax as the filters
- **Crash spotlight**: FATAL EXCEPTIONs trigger a toast and a persistent 💥 indicator; `Ctrl+G` opens the full stack trace in a modal (with the package resolved from the crash itself), regardless of active filters
- **Device menu** (`Ctrl+D`): switch the streaming device (AVD names for emulators, auto-reconnect), install an APK via a native file picker, or mirror the screen with [scrcpy](https://github.com/Genymobile/scrcpy) if it's installed
- **Log buffer selection** (`Ctrl+B`): stream `crash`, `events`, `radio`, or everything instead of the default `main`+`system`
- **ADB operations menu** (`Ctrl+A`): start/restart/kill the target app, simulate process death, clear data, uninstall, grant/revoke permissions, open deep links, screenshot, screen record
- **Pause/resume** (`Ctrl+S`): freeze the view to read or select text; the buffer keeps filling and renders on resume
- **Exports** (`Ctrl+E`): Markdown table (`Time · Level · Package · Tag · Message`, crashes marked 💥) or raw `.log`, respecting active filters, to a configurable folder
- **Filter presets** (save/load named filters) — each session otherwise starts with a clean filter; theme, device, buffer, wrap and export folder persist
- **Theme-aware**: all colors (log levels, operators, match highlights, indicators) derive from the active Textual theme — switch via the command palette
- Pid→package mapping that survives process death, so crash lines stay attributed and filterable
- **Headless mode for scripts & AI agents** (`catflap dump`): the same boolean/regex filtering piped to stdout (text or JSONL), no TUI — ships a [SKILL.md](SKILL.md) so coding agents can pull filtered logs in one call

## How it compares

catflap's niche is the **terminal**: live boolean filtering with package/PID resolution, ADB device actions, and screen mirroring — in one keyboard-driven TUI. No other terminal tool combines them.

| Tool | UI | Boolean filters | PID resolution | ADB actions | Screen mirror | Maintained |
| --- | --- | --- | --- | --- | --- | --- |
| **catflap** | TUI | ✅ `tag:`/`message:`/`package:` keys + `AND`/`OR`/`NOT` + regex | ✅ | ✅ install, clear, perms, deep links, screenshot/record | ✅ scrcpy | ✅ |
| [pidcat](https://github.com/JakeWharton/pidcat) | pipe | ❌ | ✅ | ❌ | ❌ | ❌ (2022) |
| [lazylogcat](https://github.com/parfenovvs/lazylogcat) | TUI | ❌ per-field, regex | — | ❌ | ❌ | ✅ (2026) |
| [purr](https://github.com/google/purr) | TUI (fzf) | ❌ fuzzy | ❌ | ✅ shell, wipe, bugreport | ❌ | ⚠️ (2023) |
| [rogcat](https://github.com/flxo/rogcat) | pipe | ❌ regex ±negate | ❌ | ⚠️ devices, clear, bugreport | ❌ | ✅ (2024) |
| `adb logcat` | pipe | ❌ tag\:level, 1 regex | ⚠️ manual `--pid` | n/a | ❌ | ✅ |

<sub>— = not documented / unconfirmed. Snapshot June 2026; check each project for current state.</sub>

**Agent/headless CLI:** catflap and lazylogcat both ship a non-interactive `dump` command with a SKILL.md for AI agents. catflap's exposes its full boolean/regex filter syntax (`--message "timeout OR /anr/"`); lazylogcat's is contains-match only. The others are interactive- or pipe-only.

## Requirements

- Python ≥ 3.9
- `adb` in PATH with a device/emulator connected (USB debugging enabled). On macOS: `brew install --cask android-platform-tools`
- _Optional:_ [`scrcpy`](https://github.com/Genymobile/scrcpy) for screen mirroring (`brew install scrcpy`)

## Install

Homebrew (macOS / Linux):

```bash
brew install lsbonafe/tap/catflap
```

Or with pipx / pip:

```bash
pipx install git+https://github.com/lsbonafe/catflap.git
```

Or from a clone:

```bash
git clone https://github.com/lsbonafe/catflap.git
cd catflap
python3 -m venv .venv      # use a working Python ≥ 3.9 (e.g. /usr/bin/python3 on macOS)
.venv/bin/pip install .
```

## Run

```bash
catflap
```

## Headless mode (scripts & AI agents)

`catflap dump` prints filtered logcat to stdout and exits — no TUI — using the
same boolean/regex syntax as the app:

```bash
catflap dump --package com.example.app --level E --format jsonl
catflap dump --message "timeout OR anr OR /fatal/i" --lines 200
catflap dump --package "com.example.app AND NOT gms" --buffer crash
```

`--format jsonl` emits one JSON object per line for easy parsing. See
[`SKILL.md`](SKILL.md) for the agent-facing reference (coding agents like Claude
Code and Cursor can install it to pull filtered logs in one call). Run
`catflap dump --help` for all flags.

## Keys

| Key | Action |
| --- | --- |
| `Ctrl+L` | Clear the local view |
| `Ctrl+S` | Pause / resume the stream |
| `Ctrl+G` | Jump to the last crash |
| `Ctrl+D` | Device menu (switch device / install APK) |
| `Ctrl+B` | Switch log buffer |
| `Ctrl+A` | ADB operations menu |
| `Ctrl+E` | Export (Markdown / raw log) |
| `/` | Search the scrollback (`n`/`N` to step) |
| `F1` | Filtering cheatsheet |
| `F2` | Level menu |
| `Ctrl+P` | Command palette (presets, wrap, theme, factory reset…) |
| `Ctrl+Q` | Quit |

Inside the query box, `Ctrl+U` clears to the start and `Ctrl+K` to the end.

## Filtering examples

```
droid                          # bare word → matches the tag OR the message
tag:Choreographer              # scope to the tag field
message:no fill                # scope to the message (spaces kept)
package:com.example crash      # logs from com.example that mention "crash"
tag=:AdsManager                # =: exact · ~: regex · - negates (-tag:gc)
ninja AND pirate               # both terms, any order
wifi OR coffee                 # either term
tag:Ads AND NOT message:fill   # combine keys with AND / OR / NOT
/retry \d+/                    # regex term (case-insensitive)
meltdown OR /ad (loaded|failed)/ AND NOT noise
```

Press `F1` inside the app for the full cheatsheet, including how to copy text from the terminal.

## Known issues

- **Mouse text selection is flaky.** Like any full-screen Textual app, catflap
  captures the mouse and repaints continuously, so the terminal's native
  drag-to-select (hold `Shift`/`Fn`/`Option` depending on the terminal) often
  gets wiped by a repaint — releasing the modifier too soon makes the selection
  vanish. Two ways around it:
  - **Hold the modifier ~1s after finishing the drag, then release.** Letting
    pending repaints flush while you're still holding makes the selection stick.
  - **Use Export (`Ctrl+E`)** to write the filtered lines to a Markdown or raw
    `.log` file — the reliable way to copy larger chunks.

## Development

```bash
.venv/bin/python -m unittest discover    # 161 tests: unit + headless UI integration
```

State is persisted at `~/.config/catflap/state.json` (palette → "Restore factory defaults" wipes it).
