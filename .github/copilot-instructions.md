# DeckRelay Copilot Instructions

`AGENTS.md` is the shared project rule source. `CONTRIBUTING.md` defines the
contribution, testing, and PR evidence workflow. Follow their quality, security,
architecture, documentation, and testing requirements; do not duplicate those standards
here.

- Copilot Free may be used for analysis, tests, and small, explicitly scoped
  implementations.
- For the mandatory T3.5 review defined in `AGENTS.md` and `CONTRIBUTING.md`, use Ask
  mode and remain read-only. Review the complete PR diff against the actual PR base
  independently of Codex, bind the complete report to PR number, current full head SHA
  and verified base SHA, and use every required report field.
- During T3.5, do not edit files or propose yourself as the implementer. Report
  BLOCKER, MAJOR and MINOR findings clearly; BLOCKER or MAJOR findings require
  `REVIEW_BLOCKED`, and an incomplete review requires `REVIEW_INCOMPLETE`.
- Do not commit, push, create or publish pull requests, or merge unless the user
  explicitly authorizes that specific operation. Authorization to implement or test is
  not authorization for Git or GitHub changes.
- Do not edit on `main`; establish a focused feature branch first.

The Git and pull-request section in `AGENTS.md` describes Codex's existing B3 workflow
and does not grant Copilot those Git permissions.
