# GUI guidelines for DeckRelay 2.0

## Status and scope

This document is normative for all DeckRelay 2.0 GUI work. Python and
Tkinter/CustomTkinter remain the production stack. Improvements must be incremental;
a complete GUI rewrite or framework migration is outside the regular 2.0 scope.

## Objectives

The GUI must remain reliably operable on large monitors and lower-resolution laptops.
It must make the current operating state and active source unambiguous; an inactive
source must never look active. Layout work must preserve player stability and automatic
operation. Changes should improve one bounded area at a time and retain established
workflows unless a separate product decision changes them.

## Mandatory verification environments

Every material main-window or dialog change must be checked in all of these Windows
configurations:

- 1366 x 768 at 125% scaling
- 1920 x 1080 at 100% scaling
- 1920 x 1080 at 125% scaling

The usable work area—not only nominal display resolution—must be recorded, including
taskbar, window borders and title bar. Verification must cover normal window mode;
fullscreen alone is not sufficient. Tests must also include moving a saved window to a
monitor with a different work area or DPI.

## Binding layout rules

- Fixed pixel dimensions require a documented reason and a check in every mandatory
  environment.
- Flexible grid rows and columns must receive explicit, meaningful weights. Critical
  controls need usable minimum sizes, but the application must not demand a minimum
  window larger than the target work area.
- Essential status and actions must not be clipped or require horizontal scrolling.
  The main window must not become one global scroll surface.
- Secondary areas must be collapsible, switchable or locally scrollable. Long details,
  forms and settings pages must use local vertical scrolling.
- The window must not shrink below a genuinely operable layout; before that boundary,
  it must switch to the compact layout rather than merely compressing controls.
- Persisted geometry must be validated and clamped to the current monitor's work area.
  DPI and scaling changes must trigger a safe recalculation.
- Resize handling must be coalesced or debounced. It must not create an event or redraw
  storm, steal focus, or interrupt an active operator action.

## Large and compact presentation modes

DeckRelay 2.0 must provide two explicit layout classes. Switching may be automatic,
manual or combined, but an automatic switch must not interrupt dragging, text entry,
dialog interaction or another current action.

Large mode shows complete deck information, queue and active source concurrently and
uses the available space for additional information and parallel work areas.

Compact mode prioritizes both decks, automation status, active source, queue and all
immediate playback actions. It moves infrequent details into disclosures, tabs or
dedicated detail views. It is a deliberate rearrangement, not a proportional scaling
of the large interface. Primary actions remain directly reachable without horizontal
scrolling.

## Workspaces

The design distinguishes at least:

1. playback and live operation;
2. catalog, metadata and preparation.

Large screens may show these concurrently. Smaller screens may use tabs, switches,
splitters or collapsible panels. Switching workspaces must not alter playback, queue or
automation state.

## Architecture boundaries

- Deck, player, automation, queue, playlist and source state resides outside widgets.
- Widget text, color, visibility and enabled state are presentation only and must not
  be read back as domain truth.
- GUI components render state and emit commands. Controllers and services own behavior.
- Background work communicates through immutable state, safe events or the existing
  `GuiEventDispatcher`; direct Tkinter calls from worker threads are forbidden.
- GUI updates execute on the Tk main thread and use the existing bounded scheduling,
  coalescing and dispatcher mechanisms.
- Layout changes alone must not alter player, transition, automation or persistence
  behavior.

## Performance evidence

Performance work requires measurements or a reproducible test. Do not assume Python or
Tkinter is the cause. For affected views record dynamic-widget count, update frequency,
event load, memory behavior and GUI heartbeat. Prefer bounded, virtualized or reusable
rows when large collections would otherwise create disproportionate widget counts.

## Conditions for a later PySide6 evaluation

PySide6 is not prohibited, but it may be investigated or introduced only through a
separate explicit assignment. An evaluation is justified only when at least one of the
following remains after reasonable Tkinter optimization:

- large tables require disproportionate widget counts;
- scrolling, sorting or filtering remains measurably inadequate;
- required drag-and-drop cannot be implemented reliably and maintainably;
- responsive layout or DPI scaling remains uncontrollable;
- dynamic views retain excessive CPU or memory use;
- necessary standard behavior requires disproportionate custom implementation; or
- projected long-term maintenance exceeds a controlled migration effort.

Before a decision, provide a reproducible Tkinter problem, measurements or specific
maintenance evidence, attempted Tkinter solutions, an isolated PySide6 prototype
outside production paths, a feature/performance/build-size/maintenance comparison, an
LGPLv3 obligations review, the exact Qt modules and licenses, a migration and rollback
plan, and explicit approval before production changes.

## Current GUI baseline (2.0 planning audit)

### Existing strengths

- `MainWindow` uses a weighted three-column grid for Deck A, center workspace and Deck
  B. Catalog and queue rows have weighted minimums and a persisted vertical split.
- Catalog and queue are local `CTkScrollableFrame` instances. Queue rendering uses a
  bounded reusable pool (10 minimum, 20 maximum plus defined overscan behavior), and
  catalog creation is chunked. Resize updates are coalesced before applying spacing.
- Overlay management and several detail views already use local scrolling or paging.
- Worker-heavy controller paths generally publish through `GuiEventDispatcher`; GUI
  heartbeat, render counters and layout timings already provide useful evidence.
- View models exist for queue, catalog, overlays and several dialogs, reducing some
  direct widget/domain coupling.

### Concrete limitations

- The main window starts at 1500 x 950 and enforces 1180 x 800. A 1366 x 768 display
  has less than 768 pixels of usable height after taskbar and frame; therefore the
  current 800-pixel minimum cannot fit even at 100%, and 125% scaling further reduces
  the effective work area. This target environment is currently unsupported.
- The current responsive policy changes only padding at a width threshold of 1350.
  It does not rearrange the three work columns or provide a compact workspace.
- Both decks contain fixed cover, badge, button, wrapping and control sizes. The center
  search/analysis toolbar places many fixed-width controls in two grid rows. At laptop
  widths these compete for space rather than moving to a compact command surface.
- Several dialogs use fixed, sometimes non-resizable geometry. The backup/restore
  dialog requests 680 x 960; other examples include 900 x 680 external-program and
  1050 x 680 overlay-management windows. They are not all clamped to the work area.
- No repository-level monitor/work-area geometry service or explicit DPI-change policy
  was found. Main-window position and size are not restored and clamped as one tested
  geometry model.
- The main window correctly avoids global scrolling, but large noncritical mixer,
  diagnostics, emergency and preparation controls rely on disclosure and a dense fixed
  layout rather than a complete compact-mode hierarchy.

### Couplings to reduce

- `MainWindow` is a very large composition and presentation class that retains queue,
  catalog, overlay, selection, render-cache and layout state alongside command wiring.
  These are mostly presentation caches, but their concentration makes layout changes
  risky and should be split into workspace-specific presenters/view models.
- Visibility is queried with `winfo_ismapped()` for mixer/toolbar disclosure state.
  These cases should use explicit presentation state so visibility remains an output.
- Some option-menu values are read with `cget()` to avoid redraws. This is a rendering
  optimization, not domain state, but it should remain isolated in view code.
- Dialog entries are necessarily read for user input. Parsed values must continue to be
  validated and transferred to controller-owned state rather than becoming persistent
  truth in the widgets.

### Existing tests and missing evidence

`tests/test_gui_layout_policy.py` covers spacing classes, bounded row pools, workspace
split behavior and disposal. Row, dialog, overlay, dispatcher, heartbeat and rendering
tests cover important isolated behavior. There is no automated or recorded acceptance
matrix for the three mandatory resolution/scaling combinations, no clipping/reachability
assertion for primary controls, no cross-monitor geometry test and no compact-mode test.

## Prioritized DeckRelay 2.0 implementation proposal

1. Introduce a display/work-area model and clamp startup/restored geometry. Add pure
   tests for all mandatory work areas and DPI transitions.
2. Define stable large/compact layout state outside widgets, including a guarded switch
   policy. Add tests proving that switching does not change playback or queue state.
3. Recompose the center area into live-operation and preparation workspaces. Keep decks,
   active source, automation and queue immediately accessible in compact mode.
4. Make oversized dialogs work-area-aware and locally scroll long forms; begin with
   backup/restore, external programs and overlay management.
5. Split `MainWindow` presentation responsibilities into bounded presenters/view models
   without changing controller, player, automation or database behavior.
6. Add Windows visual acceptance evidence for all three environments, keyboard access,
   focus retention, clipping, active-source clarity and resize/heartbeat performance.

These measures belong in 2.0 as incremental GUI and architecture work. A framework
prototype, database migration, or player/transition redesign does not.

## Acceptance criteria for 2.0 GUI work

- All primary actions and critical status are visible and keyboard-reachable in each
  mandatory environment without horizontal scrolling.
- No essential control is outside the usable work area; dialogs fit or scroll locally.
- Large/compact switching preserves playback, automation, queue, selection and focus.
- Worker paths make no Tk calls and dispatcher/heartbeat limits remain satisfied.
- Large lists retain bounded widget counts and existing render/timing tests pass.
- Ruff, Black, MyPy, full Pytest and the Windows quality gate pass unchanged.

Current evidence does not justify a PySide6 evaluation. The main problems are known
layout composition, fixed geometry and missing work-area/DPI handling; reasonable
Tkinter solutions have not yet been exhausted.
