# DeckRelay development rules

These rules apply to the complete DeckRelay repository and complement the global
Codex rules. The linked policies and `CONTRIBUTING.md` are normative.

## Stack and architecture

- Use Python 3.11+ with Tkinter/CustomTkinter as the production stack. PySide6 may be
  evaluated only in a separate explicit assignment and requires approval before it
  enters production. Do not use PyQt or migrate to C#/WinUI or a web framework without
  an explicit architecture decision.
- Preserve the `UI -> Controller -> Service -> Repository -> SQLite` boundaries and
  keep domain behavior separate from concrete widgets.
- Keep player, catalog, queue, playlist, source and automation state outside widgets.
  Widget text, color, visibility and enabled state are presentation, not domain truth.
- Access Tkinter widgets only on the main thread. Route worker results through the
  existing `GuiEventDispatcher` or another existing main-thread scheduling path.
- Run blocking file, database and audio work outside the GUI thread.
- Implement database changes only through forward-compatible SQLite migrations.
- Never modify, move, rename or bundle users' music or other media files.
- Keep new audio behavior testable with a fake backend.

## Binding project policies

- Apply [the GUI guidelines](docs/development/gui-guidelines.md) to every GUI change.
- Before adopting a direct or transitive runtime, build or development dependency,
  complete the review required by
  [the dependency and license policy](docs/development/dependency-license-policy.md).
  GPL, AGPL, SSPL, proprietary, ambiguously licensed or unlicensed dependencies need
  explicit approval. LGPL components require a documented distribution assessment.
- Follow `CONTRIBUTING.md` for the project contribution workflow and PR evidence.

## Test levels

Use the project tools and the project interpreter `.venv\Scripts\python.exe`; do not
assume a global Python installation.

### T1

- Run the immediately affected tests with targeted pytest; fail-fast is allowed.
- Typical command:
  `.\.venv\Scripts\python.exe -m pytest -q <test-file> -k "<test-name>" -x`

### T2

- Run the affected tests.
- Run `.\.venv\Scripts\python.exe -m ruff check src tests`.
- Run `.\.venv\Scripts\python.exe -m black --check src tests`.
- Run `.\.venv\Scripts\python.exe -m mypy src\party_player`.
- Perform the resolution/DPI acceptance required by the GUI guidelines only for
  relevant GUI changes.
- Perform the dependency-license review only for dependency changes.

### T3

- The GitHub workflow `Windows quality gates` is the binding and complete PR evidence.
- Do not rerun the full local suite merely to duplicate an equivalent successful CI
  gate.

### T4

- After integration or a push to `main`, the `Windows quality gates` workflow is the
  main-integrity evidence.

## Hardware-dependent audio acceptance

For hardware-dependent audio changes, perform separate real VLC, audio-device or
listening acceptance when automated tests do not cover actual device interaction.
These manual checks are not a blanket part of every T2, T3 or T4.
