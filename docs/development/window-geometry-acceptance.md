# Windows work-area and DPI acceptance for DeckRelay 2.0

## Coordinate model

Windows monitor bounds and work areas are read with `EnumDisplayMonitors` and
`GetMonitorInfoW` in physical pixels. The work area therefore excludes taskbars.
Per-monitor effective DPI comes from `GetDpiForMonitor` and is recorded as
`dpi / 96`.

Stored client width and height are logical CustomTkinter window units. Stored x/y
coordinates and Windows work-area rectangles are physical desktop coordinates. The
saved DPI scale makes the old outer rectangle comparable with a changed monitor layout.
Window-frame insets are measured from the native window and included when clamping.

CustomTkinter remains the single owner of widget and window scaling. DeckRelay does not
call `set_widget_scaling` or `set_window_scaling` and does not multiply sizes before
passing them to `CTk.geometry`; this avoids double scaling. CustomTkinter already polls
per-monitor DPI. DeckRelay independently polls only the monitor/work-area fingerprint
and revalidates placement after a work-area change.

## Automated coverage

`tests/test_window_geometry.py` covers valid geometry, oversized geometry, negative and
invalid dimensions, malformed data, missing data, off-screen position, removed monitor,
negative-coordinate secondary monitor, very small and large work areas, preservation of
valid values and 125% physical-to-logical conversion. Settings persistence is covered in
`tests/test_settings_service.py`.

## Manual Windows matrix

For each row, record the monitor bounds and work area emitted by the
`Fenstergeometrie` startup log, the detected DPI scale, stored geometry, applied geometry
and correction reasons. Do not record monitor names or user paths.

| Display | Scaling | Reported bounds | Reported work area | DPI scale | Result |
| --- | ---: | --- | --- | ---: | --- |
| 1366 x 768 | 125% | not retained in this early provider table | not retained in this early provider table | 1.25 target | later responsive workspace/dialog acceptance passed |
| 1920 x 1080 | 100% | `0,0–1920,1080` | `0,0–1920,1050` | 1.0 | provider probe passed; safe default `1500x950+202+30` |
| 1920 x 1080 | 125% | not retained in this early provider table | not retained in this early provider table | 1.25 target | later responsive workspace/dialog acceptance passed |

For every row verify startup without saved geometry, restoration of a valid geometry,
restoration after moving/removing a monitor, taskbar exclusion, access to all window
edges and retention of valid user size/position. This phase does not claim that all
interior controls form a compact layout; that remains a separate DeckRelay 2.0 phase.

The recorded 100% probe ran on a three-monitor Windows arrangement; all three monitors
reported 1920 x 1080 bounds, 1920 x 1050 work areas and DPI scale 1.0. No monitor names
or other user-identifying values were logged.

This table preserves the early geometry-provider evidence and does not invent missing
coordinates retrospectively. The later practical acceptance for all three target
display/scaling combinations is recorded in
[Responsive GUI phase 2A](responsive-gui-phase-2a.md). It covers the responsive
workspaces and the migrated priority dialogs; historical intermediate findings in that
development record remain identifiable as such.
