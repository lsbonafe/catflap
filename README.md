# catflap 🐈🚪

The little door your Android logs come through. A terminal UI for logcat with Android Studio-grade filtering — live, fast, and keyboard-friendly. Built with [Textual](https://textual.textualize.io/).

![](https://img.shields.io/badge/python-3.9%2B-blue) ![](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)

## Features

- **Live filters that update as you type**: package, tag, and message boxes with autocomplete suggestions drawn from the actual stream (process names, tags, frequent messages)
- **Boolean query syntax** in every box: `ad AND timeout`, `wifi OR coffee`, `NOT /Choreographer|gralloc/` — uppercase operators, `AND` binds tighter than `OR`, `/slashes/` for regex, everything else literal
- **Severity filtering**: clickable `Level ≥ W` chip with a dropdown; switchable operator — `≥` (that level and worse) or `=` (exactly that level)
- **Crash spotlight**: FATAL EXCEPTIONs trigger a toast and a persistent 💥 indicator; `Ctrl+G` opens the full stack trace in a modal, regardless of active filters
- **Device management**: auto-selects a single device, clickable picker for several (AVD names for emulators), auto-reconnect, `Ctrl+D` to switch
- **Pause/resume** (`Ctrl+S`): freeze the view to read or select text; the buffer keeps filling and renders on resume
- **Exports** (`Ctrl+E`): Markdown table or raw `.log`, respecting active filters, to a configurable folder
- **Filter presets** and full session persistence (filters, level, device, theme, wrap, export folder)
- **Theme-aware**: all colors (log levels, operators, indicators) derive from the active Textual theme — switch via the command palette
- Pid→package mapping that survives process death, so crash lines stay attributed and filterable

## Requirements

- Python ≥ 3.9
- `adb` in PATH with a device/emulator connected (USB debugging enabled)

## Install

```bash
git clone https://github.com/lbonafe/catflap.git
cd catflap
python3 -m venv .venv
.venv/bin/pip install textual
```

Optionally add an alias:

```bash
alias catflap="/path/to/catflap/catflap"
```

## Run

```bash
./catflap
```

## Keys

| Key | Action |
| --- | --- |
| `Ctrl+L` | Clear the local view |
| `Ctrl+S` | Pause / resume the stream |
| `Ctrl+G` | Jump to the last crash |
| `Ctrl+E` | Export (Markdown / raw log) |
| `Ctrl+D` | Switch device |
| `F1` | Filtering cheatsheet |
| `Ctrl+P` | Command palette (presets, wrap, device buffer, theme…) |
| `Ctrl+Q` | Quit |

## Filtering examples

```
ninja AND pirate          # both terms, any order
wifi OR coffee            # either term
pizza AND NOT pineapple   # exclude a term
/retry \d+/               # regex term (case-insensitive)
meltdown OR /ad (loaded|failed)/ AND NOT teads
```

Press `F1` inside the app for the full cheatsheet, including how to copy text from the terminal.

## Development

```bash
.venv/bin/python -m unittest discover    # 58 tests: unit + headless UI integration
```

State is persisted at `~/.config/catflap/state.json` (palette → "Restore factory defaults" wipes it).
