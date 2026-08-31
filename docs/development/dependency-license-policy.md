# Dependency and license policy for DeckRelay 2.0

## Status and responsibility

This policy is normative for every new direct or transitive runtime, build or
development dependency and for every DeckRelay release. License compatibility is a
selection, build and release gate. The review is an engineering record, not legal
advice; unclear cases require explicit project approval and, where necessary, legal
review.

## Required review before adoption

Record all of the following before changing dependency declarations or build inputs:

- package name and exact proposed version;
- canonical source and download origin;
- concrete purpose and technically viable alternatives;
- whether it is direct or transitive and runtime, build or development only;
- license from a primary source and compatibility with DeckRelay's
  `GPL-3.0-or-later` license;
- whether source, build environment, portable ZIP, EXE, installer or GitHub release
  contains it;
- required copyright notices and license texts;
- source-code, relinking, replacement or installation-information obligations;
- consequences for portable ZIP, EXE, a future installer and GitHub assets;
- maintenance activity, vulnerability exposure and update strategy; and
- the decision, reviewer and date.

A dependency may not be added merely for convenience or one small isolated feature.
The review must include transitive packages and actual build output, not only the top
level declaration.

## License decision rules

MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0 and comparable permissive licenses are
preferred, subject to their notice obligations.

GPL, AGPL, SSPL, proprietary components, unclear multiple licensing, components with
no discoverable license and unreviewed Qt modules are not automatically allowed and
require explicit approval. PyQt is excluded by project policy because its GPL or
commercial licensing model is not selected for DeckRelay.

LGPL components are not prohibited. Their concrete distribution, linking or loading
model, replaceability, notices, license texts and source availability must be documented
before adoption or release.

## PySide6 and Qt

PySide6 Community Edition is generally offered under LGPLv3/GPLv3, but not every Qt
module is necessarily LGPL. A separate evaluation must list every required Qt module
and its license, assess dynamic integration and PyInstaller packaging, preserve user
replacement rights where applicable, and include required license texts and source
information in release artifacts. A commercial Qt license would be a separate business
and architecture decision. PySide6 must not enter production code without the explicit
approval required by the GUI guidelines.

## Build and release requirements

Every release must provide:

- a reproducible inventory of components actually shipped;
- `THIRD_PARTY_NOTICES.txt` or an equivalent shipped notice file;
- all required full license texts;
- a comparison of the inventory against the built ZIP/EXE/installer;
- manually verified license conclusions rather than blind acceptance of scanner output;
- pinned versions or a reproducible, reviewable dependency resolution; and
- a completed license step in the release checklist.

External VLC/libVLC and FFmpeg/FFprobe installations must remain clearly distinguished
from bundled components. Any proposal to bundle them requires a new distribution and
license review.

## Current inventory and gaps (2.0 planning audit)

DeckRelay itself is `GPL-3.0-or-later`. Current declared direct runtime dependencies are:

| Component | Declared range / observed 1.0 build | Role | Current license evidence | Distribution |
| --- | --- | --- | --- | --- |
| CustomTkinter | `>=5.2,<6` / 5.2.2 | GUI | canonical upstream LICENSE is MIT; installed metadata reports CC0 and must be recorded as a metadata discrepancy | bundled |
| python-vlc | `>=3.0.21203,<4` / 3.0.21203 | libVLC binding | LGPL-2.1-or-later metadata | Python binding bundled; VLC runtime excluded |
| TinyTag | `>=2.1,<3` / 2.2.1 | media metadata | canonical upstream LICENSE is MIT; installed metadata has no license expression | bundled |
| Pillow | `>=11,<13` / 12.3.0 | image handling | MIT-CMU expression | bundled |

CustomTkinter directly brings `darkdetect` (observed BSD-3-Clause) and `packaging`
(observed Apache-2.0 OR BSD-2-Clause). These transitives must appear in the generated
release inventory if shipped.

The build uses setuptools (`>=75`) and PyInstaller 6.21.0. PyInstaller reports
GPL-2.0-or-later with its bundling exception and brings build dependencies including
altgraph, packaging, pefile, pyinstaller-hooks-contrib and pywin32-ctypes. Development
gates pin Black 25.1.0, MyPy 2.3.0, Pytest 9.1.1 and Ruff 0.15.22; their transitives are
not locked in the repository.

VLC/libVLC and FFmpeg/FFprobe are external prerequisites and are intentionally filtered
from the portable build. They are not part of the current ZIP.

Current deviations to close before a 2.0 release:

1. Runtime dependency declarations use ranges and there is no committed lock or resolved
   release inventory; development tools are pinned but their transitives are not.
2. `THIRD_PARTY_LICENSES.md` is a useful maintainer inventory, but no generated
   artifact-specific `THIRD_PARTY_NOTICES.txt` and complete license-text set is currently
   required inside the portable ZIP.
3. CustomTkinter's conflicting package metadata and TinyTag's missing installed license
   expression must be reconciled in generated reports with the recorded canonical MIT
   license sources rather than accepted blindly.
4. The current inventory does not enumerate every transitive component actually bundled
   by PyInstaller.
5. Security and maintenance status is not recorded per dependency at release time.

## Acceptance criteria for the 2.0 dependency gate

- A clean environment resolves to a recorded component/version graph.
- The built artifact inventory matches that graph and identifies bundled versus external
  components.
- Every shipped component has a reviewed primary-source license, required notice and
  license text.
- `THIRD_PARTY_NOTICES.txt` and required texts are present in the release artifact and
  checked by an automated artifact test plus manual review.
- No prohibited or unapproved license appears, and all LGPL distribution obligations are
  documented.
- Existing Ruff, Black, MyPy, Pytest, artifact and Windows quality gates remain intact.

For 2.0, first implement reproducible inventory generation and artifact verification;
do not add a new dependency merely to generate that inventory until this policy's own
necessity and license review is complete.
