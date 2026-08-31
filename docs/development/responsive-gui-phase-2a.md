# DeckRelay 2.0: responsive GUI and compact mode concept

## Status and scope

This is the Phase 2A design record. It is based on the Phase 1 work-area and DPI
model, the current `MainWindow` composition, the existing view models and the manual
Windows checks performed on 2026-08-16. It deliberately makes no production-code
change. Tkinter and CustomTkinter remain the production GUI stack; no new dependency
is proposed.

The manual baseline is:

| Display | Scaling | Window geometry | Interior layout |
| --- | ---: | --- | --- |
| 1920 x 1080 | 100% | passed | passed |
| 1920 x 1080 | 125% | passed | lower deck controls clipped |
| 1366 x 768 | 125% | passed | substantial horizontal and vertical clipping |

Mode decisions must use the logical client area obtained after Phase 1 has applied the
current monitor work area, frame insets and DPI scale. The physical display sizes in
this table are test labels, not layout thresholds.

## Existing main-window inventory

Dimensions below are logical CustomTkinter units. Where Tk calculates a requested size
from text, the value is a structural estimate rather than a new layout contract.

| Area | Purpose and operating importance | Current demand and fixed elements | Responsive disposition | Input impact |
| --- | --- | --- | --- | --- |
| Header and session status | Product/session identity, global on-air summary, fullscreen, Extras and Help. On-air summary is live-critical; product/version is secondary. | Three grid columns; product font 28, on-air font 16; global buttons request about 360 units plus labels. One horizontal row competes with the center session label. | Keep on-air and workspace selector in both modes. Move Extras/Help to one overflow menu in compact mode; shorten fullscreen label with tooltip. Product/version can contract. | Preserve tab order and F11/Escape. Overflow menu must remain keyboard-openable. |
| Deck A and Deck B | Load and control players; show source, state, time, cue, gain, EQ and errors. Core live surface. | Each `DeckPanel` has 17 stacked rows, a 190 x 160 cover, metadata wrapping at 270/300, four 58-unit transport buttons, four fade/eject buttons of 72/72/90/72, 16-unit side padding, quick-start pads and volume. Practical large width is about 340; full vertical request is greater than the 125% target client height. | Use separate large and compact deck layouts over the same deck view state. Compact deck keeps state/source, title, time/progress, transport, stop/eject, fade state and volume; cover becomes optional thumbnail. Cue/gain/EQ details and Jingle pads move to a local details disclosure. Arrange compact decks as two columns when width permits and two short rows when height is the tighter constraint. | Primary buttons retain at least 32 logical units height and tooltips. F1-F4 and other shortcuts remain unchanged. Focus is restored to the corresponding action after a layout switch. |
| Crossfader and transition status | Shows both deck transition states and allows immediate transition control. Live-critical. | A and B labels each request 145 units around a stretchable slider; padding adds about 40. | Always present in Live. Compact version uses shorter deck labels but keeps slider, percentage and endpoint identity. Also render a read-only compact status strip in Preparation. | Existing keyboard focus and Ctrl+Left/Right/Home/End behavior must be retained. |
| Queue header and source | Identifies active source, page, duration and warnings. Live-critical. | Source button requests 190, paging controls and statistics share one packed row. Long warning/source text expands it. | Always show source identity, queue title and warning indicator. In compact mode use a two-row header: identity/source first, paging/stats second. Long source text ellipsizes visually but remains available by tooltip. | Source and page controls remain keyboard reachable. No horizontal scrolling. |
| Queue actions and automation | Queue policy, EQ, shuffle/clear/actions, automation status and start/stop. Live-critical in part. | One packed row combines a 48 switch, `Interpretenschutz`, label and 120-unit option menu, shuffle and right-aligned actions/status. It cannot shrink safely. | Split into a permanent live command row (automation state/action, safe stop/clear entry, overflow) and a Queue Options disclosure for repetition, duration and EQ policy. Destructive actions remain explicit and confirmed. | Automation and safety actions remain direct; secondary controls are reachable through disclosure and tooltips. |
| Queue list | Ordered live program and row actions. Highest priority in Live. | Local `SmoothScrollableFrame`; rows are virtualized/bounded (10-20 plus existing behavior), but row labels and A/B/action buttons need substantial width. Minimum grid height is currently 80. | Live workspace gives it all remaining height. Compact rows show order, title, duration, state and the immediately useful deck/action; secondary row actions move to `...`. Vertical local scrolling only. | Preserve selected queue id, visible item and keyboard focus. Existing queue view model remains the source. |
| Catalog summary, search and filters | Locate and prepare tracks. Primary in Preparation, secondary in Live. | Search row spans columns 0-7. Fixed analysis controls request 120-150 each; imports request 140/150. Search, paging and six analysis buttons occupy two dense rows. | Hide behind the Preparation workspace in compact mode. Recompose into search/paging, import, and collapsible analysis groups. Analysis progress/cancel stays visible while a job is active. | Ctrl+F switches to Preparation if necessary and focuses search. Local list scrolling remains. |
| Catalog list and row actions | Browse matches and load/edit tracks. Primary in Preparation. | Local `SmoothScrollableFrame`; catalog construction is chunked. Current row actions A, B, add and details contribute to horizontal demand. | Preparation uses all remaining height. Compact rows keep title and load/add entry; A/B and metadata actions use a row menu or details pane. | Preserve search text, page, selection and scroll position across workspace changes. |
| Catalog/Queue splitter | Shares center height between two simultaneously visible lists. Useful only in large combined work. | Fixed 34-unit bar, three 92-unit buttons; both list rows have an 80-unit minimum. | Large mode may retain the splitter. Compact mode replaces it with explicit Live/Preparation workspace selection and does not display the splitter. | Saved split ratio remains stored and is restored when returning to large mode. |
| Playlist/source toolbar | Loads, saves and inspects playlists and sets template EQ. Preparation function, source identity also matters in Live. | A hidden packed row contains long labels, option menus, switches and four icon buttons. | Playlist preparation belongs in Preparation. Live shows only active source and an Add Source action. Existing disclosure state becomes explicit presentation state. | Preserve selected playlist and menu values; shortcuts/menus need descriptive tooltips. |
| Mixer and playback options | Master volume/mute, device and playback mode; some controls are live-critical, most are settings. | Full-width panel below all three columns. It contains two-column groups, recovery/emergency controls, replacement actions, diagnostics and overlay management. It is collapsed by default, but its header still consumes a row. | Keep a compact global status/action strip. Master mute, emergency/recovery state and active overlay stop remain direct when applicable. Put device, modes and extended mixer options in a local drawer/dialog. | Ctrl+M opens the same explicit presentation state. Active emergency/recovery actions must never be hidden by a generic overflow. |
| Overlay quick-start and overlay panel | Starts/stops independent Jingles; important during some events but not core deck transport. | Three pads inside each deck plus a larger disclosed management panel. Duplicated placement adds deck height. | One central, optional Live disclosure; show a direct Stop action whenever an overlay is active. Remove the compact duplication from both deck cards, not the underlying state. | Existing Ctrl+1..6 shortcuts remain. Compact buttons receive pad-name tooltips. |
| Diagnostics and analysis details | Runtime diagnosis, delay scenario and detailed status. Secondary except active failure/recovery. | Nested inside mixer; several horizontal packed controls. Visibility currently partly inferred with `winfo_ismapped()`. | Priority C drawer/dialog. Promote only active warnings and required recovery actions into the global status strip. | Explicit presentation state replaces mapped-widget state. Background results continue through `GuiEventDispatcher`. |
| Overlays, menus and transient status | Context menus, tooltips, progress and warnings. | Positioned relative to buttons/pointer; long text can widen rows. | Anchor menus to visible compact controls and clamp pop-ups through the Phase 1 work-area service. Use local wrapping for transient messages. | Escape closes transient UI; focus returns to invoking control. No domain state lives in overlay visibility. |

### Priority classification

Priority A, always directly visible in Live, is: global on-air state; both deck identities,
sources, states, time/progress and transport/stop actions; crossfader and transition
state; active queue source; queue list and essential row actions; automation status and
central action; active emergency/recovery action; and active overlay stop. Volume must
remain directly reachable, either per compact deck or in its always-visible command
row.

Priority B is directly visible in its workspace: catalog search and paging, catalog
rows, import, playlist/source preparation, metadata and EQ editing entry points, and
the principal cue/loudness analysis actions. These do not need to occupy Live space.

Priority C may be disclosed or moved to a detail view: full cue/gain/EQ text, covers,
Jingle-pad assignment, duplicate/duration policies, advanced analysis variants,
diagnostic scenarios, device details and secondary statistics. A warning or active
recovery state is promoted from C to A for as long as operator action is required.

## Causes of current clipping

Horizontal demand is dominated by the fixed three-column `1:2:1` composition. Two deck
cards each need roughly 340 logical units while the center toolbar and queue rows are
designed for substantially more than 700. At 1920 x 1080 and 125%, a nominal 1920-pixel
width represents only about 1536 logical units before frame insets. At 1366 x 768 and
125%, it represents only about 1093 logical units. Shrinking padding at the existing
1350 threshold cannot make three independently wide work surfaces fit.

Vertical demand is dominated by the 17-row deck stack (cover, metadata, cue/gain/EQ,
transport, fades, Jingle pads and volume), while the center simultaneously reserves
space for catalog, crossfader, splitter, queue controls and queue. At 125%, the logical
work height is the physical work height divided by 1.25, so the tested 1920 x 1080
environment has roughly 840 logical work-area units when its physical work area is
1050 pixels; window borders and the title bar reduce the client area further. The
1366 x 768 target is smaller again. Fixed covers and cumulative padding consume space
without creating an alternate hierarchy.

The correct response is therefore recomposition and disclosure, not smaller fonts or
global scrolling.

## Proposed presentations and workspaces

### Large presentation

Large mode keeps the proven three-column view and simultaneous catalog/queue split.
The header gains a stable Live/Preparation selector, but switching is optional because
both lists may remain visible. Secondary mixer and diagnostics stay disclosed.

```text
+--------------------------------------------------------------------------+
| DeckRelay | session | [Live] [Preparation] | ON AIR | Extras | Help      |
+------------------+------------------------------------+------------------+
|                  | Catalog search/import/analysis     |                  |
|     DECK A       | Catalog (local scroll)             |     DECK B       |
| full information |------------------------------------| full information |
| and controls     | A state -- crossfader -- B state  | and controls     |
|                  |------------------------------------|                  |
|                  | Queue source / automation / queue  |                  |
|                  | Queue (local scroll)               |                  |
+------------------+------------------------------------+------------------+
| Mixer / overlays / diagnostics disclosure                                |
+--------------------------------------------------------------------------+
```

### Compact Live workspace

Compact mode uses the width for two concise deck cards and the height for the queue.
If the available width cannot support two cards with their defined minimum action
sizes, the cards become two shallow stacked rows. This is a layout variant of the same
compact presentation, not a third user-visible mode.

```text
+----------------------------------------------------------------+
| [Live] [Preparation] | ON AIR: A | source | emergency | menu    |
+-------------------------------+--------------------------------+
| DECK A: title/state/time       | DECK B: title/state/time       |
| progress | play/pause | STOP   | progress | play/pause | STOP   |
| volume | fade | eject | details| volume | fade | eject | details|
+-------------------------------+--------------------------------+
| A state -------- crossfader / transition -------- B state       |
+----------------------------------------------------------------+
| Active source | AUTOMATIC STATUS + ACTION | queue actions       |
| Queue (local vertical scroll; compact rows and row overflow)    |
|                                                                |
+----------------------------------------------------------------+
| [Jingles/details]                    active overlay STOP if any  |
+----------------------------------------------------------------+
```

### Compact Preparation workspace

Playback never disappears. A persistent live strip shows both deck states, on-air
source, transition/automation status and direct stop/safety entries while preparation
gets the principal area.

```text
+----------------------------------------------------------------+
| [Live] [Preparation] | ON AIR/source | A state | B state | STOP |
+----------------------------------------------------------------+
| Search ............................................. | Search   |
| page | Import | [Analysis tools v] | active progress/cancel     |
+----------------------------------------------------------------+
| Catalog (local vertical scroll; row action overflow)            |
|                                                                |
+----------------------------------------------------------------+
| Playlist/source preparation [v] | metadata/details [v]          |
+----------------------------------------------------------------+
| transition + automation status | Return to Live                 |
+----------------------------------------------------------------+
```

The two workspaces render existing external state. They do not own or duplicate deck,
queue, playlist, catalog or automation state. Critical commands call the same existing
controller methods from either view.

## Mode-selection policy

Use automatic preselection with a persisted manual override:

- `AUTO` chooses from the current logical client width and height after frame/DPI
  conversion. It uses measured layout requirements, not physical display labels.
- `LARGE` and `COMPACT` are explicit user overrides. If an override cannot keep the
  defined A-priority minimums reachable, the application temporarily applies Compact
  and records `manual-large-does-not-fit`; it retains the preference for a later larger
  monitor.
- The initial implementation should derive provisional enter/leave thresholds from
  the requested sizes of representative large and compact containers, then freeze
  documented conservative constants after the three-environment acceptance run.
- Use separate enter and leave bounds (recommended starting hysteresis: 48 logical
  width units and 32 height units) and require a stable size for 250 ms. These values
  are implementation starting points to validate, not physical-resolution rules.
- A monitor/DPI/work-area change schedules one coalesced reevaluation. Maximize and
  restore do the same. Ordinary configure events do not rebuild widgets.
- Defer switching while a menu/dialog is grabbed, a pointer drag is active, text is
  being edited, or a destructive confirmation is open. Apply the pending mode after
  the interaction completes.
- During playback, layout switching changes presentation only. The persistent live
  strip is installed before any old presentation is hidden, avoiding a period without
  visible state or safety controls.

Persist the preference (`AUTO`, `LARGE`, `COMPACT`) separately from the currently
resolved mode and workspace (`LIVE`, `PREPARATION`). Remember the last workspace, but
startup during active/restored playback should default to Live unless a later product
decision says otherwise.

## Architecture and incremental migration

Introduce a GUI-independent presentation module with immutable, testable values:

- `PresentationPreference`: auto, large or compact;
- `ResolvedPresentation`: large or compact;
- `Workspace`: live or preparation;
- `LogicalClientSize` and `LayoutCapabilities`;
- `PresentationState`: preference, resolved mode, workspace, disclosures and pending
  transition reason;
- `LayoutPolicy.resolve(...)`: pure automatic selection and hysteresis;
- `VisibleRegions.for_state(...)`: central description of visible, compact and
  disclosed regions.

A `MainWindowPresentationCoordinator` owns presentation state, debounces resize/work
area events and tells views to apply a layout. It is presentation-only and runs on the
Tk thread. Controller and service boundaries do not change.

Create bounded view/presenter units incrementally:

1. `GlobalStatusView` for on-air/source/automation and promoted safety state.
2. `DeckViewState` rendered by the existing large `DeckPanel` and a new compact deck
   composition. Initially keep both widget trees alive and grid only the selected one;
   once stable, shared subviews may reduce duplication. Commands remain shared.
3. `LiveWorkspaceView` for crossfader, queue and live disclosures.
4. `PreparationWorkspaceView` for catalog, analysis and playlist preparation.

Existing queue/catalog view models, bounded row pools, dirty schedulers and
`GuiEventDispatcher` paths are reused. A layout switch must not recreate row widgets.
Move each local presentation cache to its bounded presenter only when that view is
migrated; do not split all of `MainWindow` at once.

Replace `winfo_ismapped()` as the mixer and saved-toolbar state source with explicit
booleans/enums in `PresentationState`. `winfo_ismapped()` may still be used in tests or
as a rendering assertion, never as authoritative presentation or domain state.

Before switching, capture focus as a semantic control id plus workspace, selected
queue/catalog ids and each local scroll anchor. After applying `grid`/`grid_remove`,
restore the equivalent visible control. If it is intentionally hidden, focus the
workspace selector or its containing disclosure. Queue position remains in its
existing view state; no controller mutation is allowed.

## Dialog priorities

| Priority | Dialogs | Phase 2 treatment |
| --- | --- | --- |
| 1 | Backup/restore (`680 x 960`) | Include in Phase 2B. Clamp/center with the Phase 1 work-area model, make resizable, put long content in a local vertical scroll area and keep primary backup/restore/cancel actions in a fixed footer. This is known to exceed the 1366/125% logical height. |
| 1 | Any modal confirmation or editor whose action footer can leave the work area; track editor (`780 x 760`) and system diagnostics (`820 x 700`) | Audit requested size and action reachability in all targets. Clamp position/size; locally scroll body while footer remains fixed. |
| 2 | External programs (`900 x 680`), overlay management (`1050 x 680`), first run (`760 x 680`), EQ editor (`560 x 680`) | Include work-area clamping in Phase 2B if the shared helper makes it bounded. Add local body scrolling only where the acceptance probe demonstrates clipping. Overlay management already has structured content suitable for local scrolling/paging. |
| 3 | Database backup (`620 x 430`/`620 x 360`), playlist (`820 x 560`), loudness/cue and small manager dialogs (`420-650` ranges) | Apply the shared dialog placement helper later, unless manual Phase 2B testing finds an unreachable primary action. Small non-resizable confirmations can remain fixed only when proven to fit all targets. |

Tooltips must also be clamped to the current work area, but this is lower priority than
modal action reachability.

## Incremental implementation plan

1. Add the pure presentation state/policy and settings fields, with boundary,
   hysteresis, override and DPI/work-area transition tests.
2. Add the persistent global live-status strip and workspace selector without changing
   the large layout. Prove workspace switching causes no controller command.
3. Extract/reuse deck rendering state and add compact deck views. Keep all critical
   commands and shortcuts mapped to existing callbacks.
4. Add compact Live composition around the existing queue row pool and crossfader.
   Preserve selection, visible row and focus.
5. Add compact Preparation composition around the existing catalog pool, with analysis
   and playlist disclosures.
6. Replace mapped-widget disclosure state and add the coalesced mode coordinator.
7. Adapt priority-1 dialogs, then apply the shared placement helper to bounded
   priority-2 dialogs.
8. Run the complete automated and manual matrix, measure heartbeat/configure rate and
   adjust documented logical thresholds from evidence.

Each step is independently reversible and must keep the large presentation usable.

## Test and acceptance matrix

Automated pure tests must cover auto selection on both sides of width and height
boundaries, hysteresis, manual overrides, impossible Large override, work-area and DPI
changes, maximize/restore, and malformed persisted preference. Presentation tests must
cover region priority and promoted warnings.

GUI tests must cover:

- workspace and layout switching without controller/domain commands;
- preservation of queue/catalog selection, local scroll anchors, search text and
  focus;
- no queue/catalog row-pool recreation on configure or workspace changes;
- resize coalescing and a bounded number of layout applications;
- both deck states, active source, transition and automation state in each workspace;
- direct reachability of stop/safety actions and active overlay stop;
- keyboard traversal, Ctrl+F, Ctrl+M, F1-F4 and crossfader shortcuts;
- no Tk calls from worker threads and unchanged dispatcher behavior;
- dialog body scrolling and fixed action footer at small work areas;
- unchanged GUI heartbeat and existing timing limits.

Manual Windows acceptance records actual monitor bounds, work area, DPI scale, logical
client area, resolved preference/mode and correction/switch reason:

| Environment | Expected initial mode | Required evidence |
| --- | --- | --- |
| 1920 x 1080, 100% | Large | Current spacious layout retained; all actions reachable; Live and Preparation switch without state loss. |
| 1920 x 1080, 125% | Compact unless measured client area proves Large fits | No lower clipping; both decks, queue, source, automation and transition usable. |
| 1366 x 768, 125% | Compact | No horizontal scrolling to primary actions; no vertical clipping; priority-1 dialog actions reachable. |

For every row, test normal startup, saved geometry, maximize/restore, monitor move,
DPI change, both workspaces, an active playback session, active automation, a queue
selection and a priority-1 modal dialog. The large mode must remain unchanged in the
first environment.

## Risks and open decisions

- Jingle pads are confirmed as priority B in compact mode. They will be provided once
  in a central Live area and remain available through their existing shortcuts. An
  active overlay and its Stop action are priority A. Phase 2B-1 does not move them.
- Presentation preference is confirmed as one global `AUTO` (default), `LARGE` or
  `COMPACT` value, not a monitor-specific setting. A temporary Compact fallback must
  not overwrite a stored Large preference.
- Startup is confirmed to select Live whenever playback, automation, an error,
  emergency or recovery is active. Without an active operating state, the last
  workspace may be restored. If startup cannot establish that state yet, it starts
  safely in Live and changes only presentation focus after existing state arrives.
- Two persistent deck widget trees simplify safe migration but increase widget count.
  Measure memory and configure traffic before retaining that design permanently.
- CustomTkinter requested sizes may settle after initial mapping and DPI polling. Mode
  resolution must wait for a stable client size and avoid oscillation.
- Row action consolidation can harm discoverability. Every compact overflow needs a
  clear `...` affordance, tooltip and keyboard path.
- Emergency and recovery controls are currently mixed with a large disclosed panel.
  Their promoted compact representation must be enumerated from existing states before
  implementation; no safety action may silently remain hidden.

No current finding requires a new dependency, native Windows component beyond Phase 1,
new GUI framework, Player/Queue/Automation/Audio/Database change, or complete
`MainWindow` rewrite.

## Recommended Phase 2B scope

Phase 2B should implement the presentation state/policy, automatic/manual mode choice,
the persistent global status strip, workspace selector, compact deck cards and compact
Live workspace using the existing queue and controller paths. It should also adapt the
backup/restore dialog and provide the shared work-area-aware dialog shell.

Defer the full Preparation workspace recomposition, broad dialog migration and deeper
`MainWindow` presenter extraction to Phase 2C. Phase 2B is successful only when the
three target environments have no clipped A-priority action, state remains unchanged
during presentation switches, resize activity is coalesced, and the large 100% layout
does not regress.

## Phase 2B-1 implementation acceptance

Phase 2B-1 was implemented and manually accepted on Windows on 2026-08-16. It adds the
pure presentation state and policy, global `AUTO`/`LARGE`/`COMPACT` preference,
persisted Live/Preparation workspace, coalesced main-thread coordinator, explicit
workspace focus and a compact global status strip. It does not implement compact deck,
queue or catalog content; the UI labels a resolved Compact mode as
`KOMPAKT (Inhalte ab 2B-2)`.

| Target environment | Result |
| --- | --- |
| 1920 x 1080 at 100% | Large presentation retained; header, both decks, catalog and queue remained usable. |
| 1920 x 1080 at 125% | Compact mode resolved and labelled; Live/Preparation, preference and required global status remained reachable. Existing lower-content clipping remains for Phase 2B-2. |
| 1366 x 768 at 125% | Compact mode resolved and labelled; the workspace and preference controls plus required status remained readable after removing secondary automation detail. Existing content clipping remains for Phase 2B-2. |

Manual checks also confirmed that workspace switching preserves playback, queue,
catalog and search state; Live focuses the queue automation action and Preparation
focuses catalog search. A stored Large preference safely resolved to Compact on the
small work area without overwriting that preference, and switching back to Auto was
persisted.

### Real Windows diagnostic evidence

The existing work-area log and the one-shot presentation diagnostic recorded this
representative run on the primary 1920 x 1080 monitor at 125% scaling:

```text
monitor bounds: 0,0-1920,1080
work area: 0,0-1920,1042
dpi scale: 1.25
logical client: 1521 x 796
stored preference: auto
resolved presentation: compact
workspace: live
last reason: configure:auto-large-height-insufficient
resize events received: 2
policy evaluations: 2
presentation changes applied: 1
pending change: none
```

The two Configure events did not cause two full presentation changes. The coordinator
coalesced them into one applied mode change. Diagnostic counters are also exposed by
`widget_diagnostics()` without reading widget text or creating a background thread.

### FFmpeg validation

The three real format tests (128 kbit/s MP3, FLAC and VBR MP3) passed with the existing
project test tools at
`.tools/ffmpeg/ffmpeg-8.1.2-essentials_build/bin`. Both executables identify themselves
as `8.1.2-essentials_build-www.gyan.dev`. The local directory was prepended to `PATH`
for the test process only; no installation or dependency change was made. The Windows
quality workflow independently installs the pinned Chocolatey package `ffmpeg 9.0.1`
and verifies both executable versions before running the same three tests.

No new runtime, build or development dependency was introduced by Phase 1, Phase 2A
or Phase 2B-1.

## Phase 2B-2a compact Live implementation

Phase 2B-2a adds a compact Live composition without changing player, queue,
automation, audio or database rules. Two compact deck views are created once and
receive the same existing `Deck` state updates and controller commands as the large
views. A presentation change only shows or hides prebuilt widgets. It does not issue a
deck command, recreate the queue row pool, add a timer or add a subscription.

Compact Live places the global status and workspace selector first, followed by both
compact decks, the existing crossfader, queue source and automation controls, the
existing virtualized queue and one central Jingle disclosure. An active Jingle is
always identified next to a direct Stop action. The six existing favorite commands
remain available through the disclosure and Ctrl+1 through Ctrl+6.
The existing Mixer disclosure remains visible at the bottom in Compact and opens the
same mixer controls; no parallel audio state or command path is introduced.
On short work areas, expanding the compact Jingle favorites temporarily uses the
Mixer disclosure's footer space. Collapsing the favorites restores the Mixer
disclosure immediately, so both sets of controls remain reachable without overlap.
The Jingle disclosure itself is placed above the flexible Queue viewport so it
cannot be pushed behind the Mixer footer on a 768-pixel-high Windows work area.

Compact Preparation is intentionally not presented as complete. It displays an
explicit Phase-2C notice and a keyboard-focusable return to Live. Large Preparation
and all catalog, analysis and playlist behavior remain unchanged.

### Widget and lifecycle evidence

The same empty-window diagnostic probe was run before and after the implementation:

| Gauge | Before | After |
| --- | ---: | ---: |
| Total Tk widgets | 638 | 778 |
| Current Tooltip instances | 30 | 52 |
| Compact deck widgets | 0 | 98 |
| Compact deck trees created | 0 | 2 |

The increase is fixed at construction time. Repeated layout decisions are ignored by
the stored presentation signature; the compact deck creation counter remains two.
Only the currently visible deck representation is rendered during periodic status
updates. The hidden representation receives one catch-up render when the presentation
changes.

### Windows acceptance matrix

| Target environment | Phase 2B-2a result |
| --- | --- |
| 1920 x 1080 at 100% | Passed: Large remained complete. Forced Compact exposed both decks, Crossfader, queue/automation and Jingle disclosure. Large/Compact round trips restored Deck B and the full header after regression fixes. Compact Preparation showed the Phase-2C transition and returned to Live without losing Live state. |
| 1920 x 1080 at 125% | Passed: Compact Live exposed both decks, Crossfader, queue/automation and the Jingle and Mixer disclosures without clipping primary controls. |
| 1366 x 768 at 125% | Passed: Compact Live kept both decks, Crossfader, queue/automation and the Jingle and Mixer disclosures reachable without horizontal scrolling or vertical overlap. |

No new dependency or license change was introduced.

## Phase 2B-2b responsive dialogs

Phase 2B-2b adds a shared dialog foundation that resolves each dialog against the
existing `window_geometry` work-area model. Dialogs are centered on the monitor of
their parent, DPI-scaled dimensions are clamped to the real Windows work area, and
minimum sizes adapt when the target work area is smaller than the preferred size.
The foundation also provides consistent Escape handling and defensive modal-grab
release without adding a dependency or changing domain behavior.

The Database/Backup dialog, Track Editor and System Diagnostics now keep their primary
actions in a fixed footer while long content remains locally scrollable. External
Programs and First-run Setup use the same pattern. Overlay Management already had
local list and form scrolling and now uses the shared placement and Escape behavior.
The embedded Equalizer editor was deliberately left unchanged because adapting that
anonymous MainWindow-owned dialog would require a larger extraction beyond this
bounded dialog pass.

The manual Windows acceptance passed for 1920 x 1080 at 100%, 1920 x 1080 at 125%,
and 1366 x 768 at 125%. In all three environments the three priority dialogs remained
inside the usable work area, long content scrolled locally, action bars stayed
reachable, keyboard focus/Tab/Escape and resize retention behaved correctly, and
closing left no visible residual state. The additional adapted dialogs each opened
and closed normally. No destructive backup, restore or maintenance action was run.

The reduced lifecycle probe opened and closed the Database/Backup dialog ten times.
Root widget count remained 1/1, Toplevel count 0/0, no active grab remained and GUI
callbacks changed from 2 to 1 rather than accumulating. The compact Preparation
visual recheck also exposed a stale delayed LIVE-layout callback; its presentation-only
reassertion now reads the current workspace and no longer leaves LIVE/Queue/Mixer
content over Compact Preparation. The targeted combined dialog, geometry and layout
run passed 91 tests. The complete project suite was intentionally not run under the
commissioned risk-based verification strategy.

Ursache war, dass ein verzögerter LIVE-Layout-Callback nach einem Wechsel zu
VORBEREITUNG noch ausgeführt werden konnte. Dadurch konnten Deck-, Queue- oder
Mixerbereiche die kompakte Vorbereitung überlagern. Die Korrektur verhindert, dass
veraltete oder nicht mehr zum aktuellen Präsentationszustand passende
Layoutanwendungen wirksam werden. Player-, Queue-, Automatik-, Audio- und
Datenbanklogik wurden nicht verändert.

Die vollständige Projektsuite wurde entsprechend der beauftragten risikobasierten
Prüfstrategie bewusst nicht ausgeführt.

## Phase 2C compact Preparation implementation

Phase 2C replaces the compact Preparation placeholder with a production catalog and
preparation composition. It re-grids the existing search, paging, import, analysis,
catalog and saved-playlist widgets; no catalog query, controller command or widget-tree
rebuild is issued by a presentation or workspace change. The existing bounded catalog
row pool remains the only catalog view and therefore retains its bound rows and local
scroll position while it is hidden.

The compact composition places the explicit live-status projection first, including
both deck states, active queue source, automation, transition and promoted warning
state. A direct Stop action is shown only while one or both decks are explicitly on
air and delegates to the existing deck Stop commands. Search, reset and paging remain
directly reachable. Import actions stay on the next row. Analysis and saved-playlist
preparation use independent local disclosures whose state survives workspace and
presentation changes. An active analysis opens its disclosure and also renders a
separate compact progress/cancel row. The disclosure remains closable while this
active row keeps progress and cancellation visible, without adding a subscription,
timer or worker-thread GUI access.

Playlist selection remains preparation state only. The live-status source continues
to change exclusively through the existing active queue-origin update, so opening a
playlist for editing cannot present it as the active playback source. Ctrl+F changes
from Compact Live to Compact Preparation before focusing the existing search entry.
Large presentation restores the existing simultaneous catalog/queue composition.

### Phase 2C lifecycle and performance evidence

The structural audit adds six fixed widgets compared with the Phase 2B-2a tree: a
search-reset button, the net replacement of the placeholder with the compact live
strip and three-button preparation toolbar, plus the active-analysis status and cancel
controls. No tooltip, periodic timer, subscription
or catalog row is added. Diagnostics now expose
`compact_preparation_specific_widget_count` alongside the existing total-widget,
tooltip, row-pool, creation and layout-application gauges.

The automated empty-window probe could not be executed in the agent process because
that Windows execution identity could not initialize the locally installed Tcl/Tk
runtime, even with explicit library paths and a temporary readable copy. A real
interactive before/after diagnostic measurement therefore remains part of the manual
Windows acceptance; no runtime widget or heartbeat value is inferred from the static
audit.

The later approved unsandboxed Tk probe succeeded. Before and after ten complete
Preparation/Live/Preparation round trips, total Tk widgets remained 744/744,
`compact_preparation_specific_widget_count` remained 22/22 and current Tooltip
instances remained 36/36. Presentation layout applications advanced exactly from 1
to 21 (one application per workspace change), while policy evaluations stayed at 1
and resize events at 0. The ten round trips took 7.081–8.758 ms in the first gauge
run (7.894 ms mean). In the heartbeat run the first five round trips averaged 9.270 ms
and the last five 8.562 ms, so no increasing delay was observed. Published heartbeat
gaps were at most 16 ms; afterward there was no active GUI callback and no pending
layout refresh, Catalog chunk or Queue chunk. The hidden probe retained one pending
focus request because an intentionally withdrawn window cannot accept real keyboard
focus; this is a probe artifact rather than accumulating layout work.

### Phase 2C Windows acceptance matrix

| Target environment | Phase 2C result |
| --- | --- |
| 1920 x 1080 at 100% | Passed: Large retained the three-column presentation with Catalog and Queue visible together, both decks fully operable and no unexpected hidden regions or visible regression. Live/Preparation changed only the working focus, and Ctrl+F focused Catalog search. |
| 1920 x 1080 at 125% | Passed: Compact Preparation kept the complete live strip, both deck states, on-air identity, active source, automation/transition status, search/reset, paging, import, Analysis and Playlist/Source disclosures, usable Catalog height and direct return to Live reachable without clipping or horizontal scrolling. |
| 1366 x 768 at 125% | Passed after regression fix: title and global status remained visible; search/reset, paging and imports fit; the locally scrollable Catalog retained usable rows; Analysis and Playlist/Source disclosures opened and closed without permanently displacing essential actions; no global scrolling, overlap or clipped primary action was observed; return to Live remained reachable. |

The practical matrix must also cover playback and automation during search, an active
analysis and cancellation, long titles and source names, a playlist selected for
editing while Directory or Queue remains the active source, repeated workspace and
presentation changes, and maximize/restore. No new dependency or license change was
introduced.

The cross-workspace state-preservation scenario passed in Compact presentation. With
Directory or Queue playback active, selecting a Playlist only for editing left the
active source unchanged. Search text, Catalog page, selection and local scroll position
survived Live/Preparation round trips. Large/Compact and maximize/restore changes did
not issue a new search, player command or Queue action.

A subsequent Compact Live check found that the first Queue row could not open its
extended-actions menu after an event-driven status update, while menus on later rows
could be displayed but were not reliably actionable. `QueueRowView` had incorrectly
treated its invalidated render cache as evidence that no entry was bound. Menu
eligibility now uses the stable entry id and explicit row fields, and each bounded row
retains at most one active menu until replacement or disposal. A regression test
executes a menu command after a live status update. The practical menu recheck passed.
The same review exposed that skipped entries had no generic UI path back to Waiting,
although the existing Queue transition and retry command already support it. Their
menu now offers `Wieder auf wartend setzen`; repetition-protection skips additionally
retain `Trotzdem abspielen` for the explicit rule override.

During the 1366 x 768 run, pressing the Catalog row action `B` stopped automatic
operation and a later restart reported an empty Queue. This is the established manual
deck-load behavior: `B` loads the Catalog title directly into Deck B, which the
controller records as a manual override and therefore ends automatic operation. With
no waiting Queue entry, restart is correctly rejected. The `+` row action is the
non-overriding path for adding a Catalog title to the Queue; the finding was not caused
by scrolling, workspace switching or compact layout.

Two findings were recorded during the 1920 x 1080 at 125% run. A transient silent
output report could not be reproduced; Deck B subsequently reported On Air and output
remained audible, so no audio/player change was made as presentation work. The
Database and Backup dialog exceeded the available height without a local scrollbar;
that dialog migration remains in the explicitly excluded Phase 2B-2b scope and does
not change the Phase 2C workspace acceptance.

### Phase 2C automated quality evidence

The focused presentation, catalog-row, coordinator, controller, dispatcher,
dirty-row and heartbeat run passed 244 tests. The final repository checks passed Ruff,
Black for all 270 Python files and MyPy for 145 source files. The complete test suite
ran with the existing local FFmpeg 8.1.2 `bin` directory prepended to `PATH` and
completed with 1171 passed and zero skipped tests. The three real MP3, FLAC and VBR-MP3
FFmpeg/FFprobe cases therefore executed rather than skipping.

### Formatter diagnostic

After an initially non-terminating Black run, `main_window.py` was checked in
isolation with `--check --verbose` and a fresh temporary `BLACK_CACHE_DIR`. The final
controlled run completed with exit code 0 in 2.635 seconds. Two delegated Python
processes were observed, with 2.453 CPU seconds and a maximum observed working set of
70.4 MiB. Black reported the file as already well formatted. A subsequent full check
completed successfully for 270 files. The temporary cache directories and their
diagnostic output were retained; no cache or project file was deleted.

## Confirmed product decisions for Phase 2B

### Jingle and overlay controls

Jingle quick-start pads are Priority B in Compact presentation. They are
available through one central Live-workspace disclosure and through the
existing keyboard shortcuts. They are not duplicated permanently inside
both compact deck cards.

Whenever an overlay is active, its identity and a direct Stop action are
promoted to Priority A and remain visible.

### Presentation preference

Presentation preference is stored globally rather than per monitor.

The supported preferences are:

- AUTO, which is the default;
- LARGE;
- COMPACT.

If LARGE cannot satisfy the defined Priority-A minimums, DeckRelay
temporarily resolves the presentation to COMPACT. The stored LARGE
preference is retained and may become effective again on a larger work
area.

### Startup workspace

When playback, automation, recovery or an operational warning is active,
DeckRelay starts in the Live workspace.

When no operational session is active, the last selected workspace may be
restored.

### Workspace selector in Large presentation

In Compact presentation, Live and Preparation are mutually exclusive main
workspaces.

In Large presentation, both existing work surfaces remain visible. The
selector identifies and focuses the current work context but does not hide
the other surface. Switching context must not alter domain state.
