# Third-Party Licenses and Runtime Dependencies

This document summarizes third-party components used by DeckRelay and their
licensing context for source and release distribution.

## Core Python Runtime Dependencies

- customtkinter (5.2.2)
  - Canonical upstream LICENSE: MIT.
  - Installed package metadata reports CC0; retain this mismatch in the release review
    and use the canonical upstream license file as the recorded primary-source evidence.
- python-vlc (3.0.21203)
  - License: LGPL-2.1-or-later.
- tinytag (2.2.1)
  - Canonical upstream LICENSE: MIT.
- Pillow (12.3.0)
  - License expression metadata: MIT-CMU.

## Build Tooling

- PyInstaller (6.21.0)
  - GPL-2.0-or-later with PyInstaller exception for bundled applications.

## External Runtime Components (Not Bundled)

The following components are required but are not shipped inside DeckRelay
release ZIP files:

- VLC (LibVLC runtime)
  - Required for playback.
  - Installed and licensed separately by the end user.
  - Detected from an explicit validated directory, common Windows installation
    directories, or PATH.
  - Upstream installation typically provides COPYING and AUTHORS files.
- FFmpeg / FFprobe
  - Required for automatic cue analysis and loudness analysis features.
  - Installed and licensed separately by the end user.
  - Both executables must come from the same validated bin directory; PATH is only
    one supported discovery source.

## Distribution Policy

- DeckRelay source code is licensed under GPL-3.0-or-later (see LICENSE).
- VLC executables, libVLC, VLC plugins, FFmpeg, and FFprobe are intentionally not
  included in release ZIPs.
- End users must install VLC and, for analysis features, FFmpeg/FFprobe before use.
- DeckRelay never downloads or installs these external runtimes automatically.
- The release build filters hook-discovered runtime files and fails its post-build
  scan if a forbidden VLC/FFmpeg artifact remains.

## Maintainer Checklist Before Release

- Confirm dependency versions in pyproject.toml and lock environment.
- Re-check upstream licenses for customtkinter, python-vlc, TinyTag, and Pillow.
- Verify runtime prerequisite notes in README.md and LAUFZEIT-README.txt.
- Confirm release artifacts do not include third-party binary bundles by accident.
- Run `scripts/check_release_artifact.py dist/DeckRelay` and require success.
