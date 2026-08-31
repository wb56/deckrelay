# DeckRelay development rules

These rules apply to the complete repository and are binding for the DeckRelay 2.0
release line and future work unless a later, explicit architecture decision replaces
them.

- Keep Python and Tkinter/CustomTkinter as the production stack for DeckRelay 2.0.
- Do not replace the GUI framework independently. PySide6 may be evaluated only under
  a separate explicit assignment and must not enter production code without approval.
- Do not use PyQt. Do not migrate to C#/WinUI or a web framework without an explicit
  architecture decision.
- Never access Tkinter widgets from background threads. Route worker results through
  the existing `GuiEventDispatcher` or another existing main-thread scheduling path.
- Widgets are views and command sources, not domain-state storage. Keep player,
  catalog, queue, playlist, source and automation state outside widgets; never infer
  domain truth from widget text, color, visibility or enabled state.
- Keep the established `UI -> Controller -> Service -> Repository -> SQLite`
  boundaries and continue separating domain behavior from concrete widgets.
- Apply [the GUI guidelines](docs/development/gui-guidelines.md) to every GUI change.
- Before adding any direct or transitive runtime, build or development dependency,
  complete the necessity and license review in
  [the dependency and license policy](docs/development/dependency-license-policy.md).
- GPL, AGPL, SSPL, proprietary, ambiguously licensed or unlicensed dependencies need
  explicit approval before adoption. LGPL components require a documented distribution
  assessment. PyQt is excluded by project policy.
- Do not add dependencies merely for convenience or one small isolated requirement.
- Do not bypass, weaken or remove existing tests, formatting, type checks, quality
  gates or release checks.

The linked detailed policies are normative. `CONTRIBUTING.md` additionally defines the
normal contribution workflow and required local checks.
