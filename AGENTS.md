# DeckRelay development rules

These rules apply to the complete DeckRelay repository and complement the globally
installed Codex rules and standards. Global rules govern general development, safety,
Git, testing, Python architecture and documentation; this file adds DeckRelay-specific
requirements. Read the applicable central standards before the work they cover. Report
conflicts between global and project rules instead of resolving them silently. The linked
project policies and `CONTRIBUTING.md` are normative.

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
- Place new behavior in the existing module or package that owns its domain
  responsibility. Keep `main.py` limited to application entry and initialization.
- Do not grow monolithic modules or place new modules in the package root by default.
  Inspect the existing package structure before adding a subpackage, and preserve clear
  responsibilities, import boundaries and the established dependency direction.
- Do not reorganize packages or architecture outside the explicit assignment.

## Binding project policies

- Apply [the GUI guidelines](docs/development/gui-guidelines.md) to every GUI change.
- Before adopting a direct or transitive runtime, build or development dependency,
  complete the review required by
  [the dependency and license policy](docs/development/dependency-license-policy.md).
  GPL, AGPL, SSPL, proprietary, ambiguously licensed or unlicensed dependencies need
  explicit approval. LGPL components require a documented distribution assessment.
- Follow `CONTRIBUTING.md` for the project contribution workflow and PR evidence.

## Documentation

- Apply the central documentation standard to every relevant change. Update affected
  docstrings, public contracts and architecture documentation together with behavior.
- Keep state transitions and error behavior for playback, queue, selection and recovery
  traceable where those areas change.
- Extend existing documentation where practical; do not create unrelated or speculative
  documents.

## Test levels

Prefer `scripts/Invoke-DevTests.ps1` and use the project interpreter
`.venv\Scripts\python.exe`; do not assume a global Python installation. Read available
regression groups dynamically from `scripts/test-groups.psd1`; do not duplicate group
names in rules.

### T1

- Run the immediately affected tests with the `quick` profile and the narrowest useful
  paths, node IDs or keyword expression.
- If a direct Pytest call is necessary, include
  `--tb=short -x --no-header --no-summary`.

### T2

- Run the relevant affected-area tests, using a declared `regression` group when one
  matches the change.
- Run `.\.venv\Scripts\python.exe -m ruff check src tests`.
- Run `.\.venv\Scripts\python.exe -m black --check src tests`.
- Run `.\.venv\Scripts\python.exe -m mypy src\party_player`.
- Perform the resolution/DPI acceptance required by the GUI guidelines only for
  relevant GUI changes.
- Perform the dependency-license review only for dependency changes.
- Do not run the full suite unless the change risk or a project gate requires it.

### T3

- The GitHub workflow `Windows quality gates` is the binding and complete PR evidence.
- Do not rerun the full local suite merely to duplicate an equivalent successful CI
  gate.

### T4

- After integration or a push to `main`, the `Windows quality gates` workflow is the
  main-integrity evidence.

## Git and pull-request workflow

- Prefer `scripts/Invoke-DevPr.ps1` for its supported actions: `status`, `validate`,
  `commit`, `publish`, `gates`, `merge` and `cleanup`. Do not claim that it performs
  unsupported work.
- Preserve Git autonomy B3 and work on an unambiguous feature branch, preferably in an
  isolated worktree. Protect unrelated worktrees, branches and local changes.
- Bind technical review and explicit owner approval separately to the PR number and full
  current head SHA. Recheck both after every head change; neither replaces formal GitHub
  review requirements.
- Merge only after all required security and quality gates succeed. After merge, verify
  the main-integrity gate for the merge commit.
- The script's `cleanup` action deletes only an eligible local `feature/*` branch and
  refuses attached worktrees. After a successful T4, or an explicitly documented `n/a`,
  remove the verified clean worktree and remote branch manually when required, without
  force and without touching unrelated worktrees or branches.

## Hardware-dependent audio acceptance

For hardware-dependent audio changes, perform separate real VLC, audio-device or
listening acceptance when automated tests do not cover actual device interaction.
These manual checks are not a blanket part of every T2, T3 or T4.
