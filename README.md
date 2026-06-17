# catflap 🐈🚪

The little door your Android logs come through. A terminal UI for logcat with Android Studio-grade filtering — live, fast, and keyboard-friendly. Built with [Textual](https://textual.textualize.io/).

![](https://img.shields.io/badge/python-3.9%2B-blue) ![](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey) ![](https://img.shields.io/badge/install-brew-orange)

![catflap screenshot](docs/screenshot.svg)

## How it compares

catflap's niche is the **terminal**: live boolean + field-scoped filtering with
PID→package resolution, ADB device actions, and live screen mirroring — in one
keyboard-driven TUI. No other terminal tool combines them.

| Tool | UI | Filtering | PID resolution | ADB actions | Screen mirror | Headless/agent | Maintained |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **catflap** | TUI | ✅ `tag:`/`message:`/`package:` keys + `AND`/`OR`/`NOT` + regex + negate | ✅ survives process death | ✅ install, clear, perms, deep links, screenshot/record | ✅ scrcpy | ✅ `dump` text/JSONL + SKILL.md | ✅ 2026 |
| [DroidTUI](https://github.com/sorinirimies/droidtui) | TUI + JSONL | ⚠️ regex, exclude, tag/PID/level — no boolean | ❌ PID-number filter only | ⚠️ 40+ typed cmds (no mirror) | ❌ | ✅ `--query` JSONL (no SKILL.md) | ✅ 2026 |
| [adb-tui](https://github.com/alanisme/adb-tui) | TUI + CLI + MCP | ⚠️ level/tag/search — no boolean/regex | ⚠️ process↔name, not logcat¹ | ✅ install, shell, push/pull, screenshot/record | ❌ capture only | ✅ MCP (120+ tools, JSON-RPC) | ✅ 2026 |
| [FadCat](https://github.com/anonfaded/fadcat) | GUI + CLI + MCP | ⚠️ regex, fuzzy, grep — no boolean | ⚠️ package picker, not formal¹ | ✅ bundled adb, multi-device | ❌ roadmap only | ✅ MCP (FastMCP stdio) | ✅ 2026 |
| [lazylogcat](https://github.com/parfenovvs/lazylogcat) | TUI + web UI | ❌ per-field contains² — no regex/boolean | — | ❌ log viewing only | ❌ | ⚠️ agent skill, **no** dump/JSON | ✅ 2026 |
| [rogcat](https://github.com/flxo/rogcat) | pipe | ⚠️ regex on tag/PID/msg + `!` negate — no AND/OR | ❌ | ⚠️ devices, clear, bugreport | ❌ | ✅ JSON/CSV/raw stdout | ✅ 2025 |
| [purr](https://github.com/google/purr) | TUI (fzf) | ⚠️ fuzzy + tag/severity — no boolean/regex | ❌ | ✅ shell, wipe, bugreport | ❌ | ❌ | ✅ 2026 |
| [pidcat](https://github.com/JakeWharton/pidcat) | pipe | ❌ package/tag only | ✅ tracks PID across deploys | ❌ | ❌ | ❌ | ❌ 2022 |
| [Aya](https://github.com/liriliri/aya) | GUI (Electron) | ⚠️ viewer³ — depth undocumented | ❌ | ✅ file/app/process mgmt | ✅ screen mirror | ❌ | ✅ 2025 |

<sub>✅ full · ⚠️ partial/qualified · ❌ absent · — undocumented. Snapshot June 2026; verify per project.</sub>
<sub>¹ Has process↔name correlation but no documented logcat PID→package resolution. ² Repo README/config show contains-match only. ³ Aya lists "logcat viewer" with no filtering depth documented.</sub>

**Where catflap stands out:** it's the only **terminal** tool that combines boolean +
field-scoped filtering (`tag:Ads AND -message:fill`), PID→package resolution that survives
process death, broad ADB actions, **live scrcpy mirror**, and a headless `dump` with real
filter syntax + a SKILL.md. Boolean AND/OR/NOT with field keys is unmatched across the
verified terminal tools (rogcat has `!` negation only). Live mirror + logcat together exists
elsewhere only in Aya (a GUI).

**Where others go further — honestly:** for *agent integration*, [adb-tui](https://github.com/alanisme/adb-tui)
and [FadCat](https://github.com/anonfaded/fadcat) expose full **MCP servers** (JSON-RPC,
120+ tools); catflap's headless story is a one-shot `dump`, not a live MCP surface. They
also ship broader raw ADB (push/pull, port-forward, file/app management). And
[pidcat](https://github.com/JakeWharton/pidcat) still does the cleanest single-package PID
tracking if that's all you need.

## Features

- **One unified query box, Android Studio–style**: scope a term to a field with `tag:`, `message:` or `package:`; add `=:` (exact), `~:` (regex), or a leading `-` to negate (`-tag:gc`). A bare word with no key matches the **tag OR the message**. Live autocomplete draws from the actual stream (process names, tags, frequent messages); typing `package:` pins the **foreground app** on top.
- **Boolean operators** compose with the keys: `tag:Ads AND -message:fill`, `wifi OR coffee`, `NOT /Choreographer|gralloc/` — uppercase operators, `AND` binds tighter than `OR`, `/slashes/` for regex, everything else literal
- **Match highlighting**: the terms you're filtering on are highlighted inline in the log — tag matches one colour, message matches another — so you can see *why* a line matched
- **Process banners**: with a `package:` filter set, an Android-Studio-style divider marks when that app's process starts or dies (`──── PROCESS STARTED (pid) … ────`), so restarts and crashes are obvious
- **Severity filtering**: clickable `Level` chip (or `F2`) with a dropdown; the chip text is tinted to the selected level; switchable operator — `≥` (that level and worse) or `=` (exactly that level)
- **Search the scrollback** (`/`): jump to matches across the whole buffer, not just what's on screen; `n`/`N` step between hits, same plain-text or `/regex/` syntax as the filters
- **Crash spotlight**: FATAL EXCEPTIONs trigger a toast and a persistent 💥 indicator; `Ctrl+G` opens the full stack trace in a modal (with the package resolved from the crash itself), regardless of active filters
- **Device menu** (`Ctrl+D`): switch the streaming device (AVD names for emulators, auto-reconnect), install an APK via a native file picker, mirror the screen with [scrcpy](https://github.com/Genymobile/scrcpy), or grab a **screenshot (`F4`) / screen recording (`F3` toggles)**
- **Log buffer selection** (`Ctrl+B`): stream `crash`, `events`, `radio`, or everything instead of the default `main`+`system`
- **ADB operations menu** (`Ctrl+A`): start/restart/kill the target app, simulate process death, clear data, uninstall, grant/revoke permissions, open deep links
- **Pause/resume** (`Ctrl+S`): freeze the view to read or select text; the buffer keeps filling and renders on resume
- **Exports** (`Ctrl+E`): Markdown table (`Time · Level · Package · Tag · Message`, crashes marked 💥) or raw `.log`, respecting active filters, to a configurable folder
- **Filter presets** (save/load named filters) — each session otherwise starts with a clean filter; theme, device, buffer, wrap and export folder persist
- **Theme-aware**: all colors (log levels, operators, match highlights, indicators) derive from the active Textual theme — switch via the command palette
- Pid→package mapping that survives process death, so crash lines stay attributed and filterable
- **Headless mode for scripts & AI agents** (`catflap dump`): the same boolean/regex filtering piped to stdout (text or JSONL), no TUI — ships a [SKILL.md](SKILL.md) so coding agents can pull filtered logs in one call

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
| `Ctrl+D` | Device menu (switch device, install APK, mirror, screenshot/record) |
| `Ctrl+B` | Switch log buffer |
| `Ctrl+A` | ADB operations menu |
| `F3` | Start / stop screen recording |
| `F4` | Device screenshot |
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
.venv/bin/python -m unittest discover    # 165 tests: unit + headless UI integration
```

State is persisted at `~/.config/catflap/state.json` (palette → "Restore factory defaults" wipes it).
