# DeckRelay development rules

These repository-wide rules and the linked documents are binding.

- Keep Python and Tkinter/CustomTkinter as the production stack. Framework changes
  require an explicit architecture decision; PyQt is prohibited.
- Access Tkinter widgets only on the main thread; route worker results through the
  existing `GuiEventDispatcher` or another established main-thread path.
- Keep domain state outside widgets; never infer it from widget text, color, visibility
  or enabled state.
- Preserve `UI -> Controller -> Service -> Repository -> SQLite`.
- Follow the [GUI guidelines](docs/development/gui-guidelines.md) for GUI changes.
- Follow the [dependency and license policy](docs/development/dependency-license-policy.md)
  for dependency changes.
- Never weaken or bypass existing tests, formatting, type checks, quality gates or
  release checks.
- Follow `CONTRIBUTING.md` for the binding development and contribution workflow.
