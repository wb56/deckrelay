# DeckRelay

**Music library management and reliable dual-deck playback for Windows**

DeckRelay is a free, open-source Windows application for organizing music collections, preparing playlists and queues, and running continuous automatic playback with two audio decks and smooth crossfades.

It is designed for private parties, small events, clubs, associations, and anyone who wants more control than a simple playlist player provides—without requiring a full professional DJ system.

[Download DeckRelay 1.0.0 for Windows](https://github.com/wb56/deckrelay/releases/download/v1.0.0/DeckRelay-portable-1.0.0.zip) ·[Release notes](https://github.com/wb56/deckrelay/releases/tag/v1.0.0) ·
[SHA-256 checksums](https://github.com/wb56/deckrelay/releases/download/v1.0.0/SHA256SUMS.txt) · [View documentation](#getting-started) · [Report a problem](https://github.com/wb56/deckrelay/issues)

> DeckRelay 1.0.0 is the first stable release. It is available as a portable Windows application and does not require a traditional installation.


<img width="1472" height="807" alt="grafik" src="https://github.com/user-attachments/assets/4c4bd92b-d479-46c7-8ad7-71ec432e098f" />



## What DeckRelay offers

* **Music library management**
  Organize and search your local music collection and maintain track information in one central catalog.

* **Dual-deck playback**
  Two independent playback decks provide continuous music and prepare the next track in advance.

* **Automatic transitions**
  DeckRelay handles track changes and smooth crossfades automatically.

* **Playlists and live queue**
  Prepare playlists in advance or adjust the upcoming tracks during playback.

* **Cue points and track-specific settings**
  Store playback positions and other settings for individual tracks without changing the original audio files.

* **Volume and loudness control**
  Track-specific gain, loudness analysis, and clipping protection help produce more consistent playback levels.

* **Local and independent**
  Your music files and catalog remain on your computer. DeckRelay does not require a cloud service or user account.

## Typical uses

DeckRelay is suitable for:

* private parties and celebrations;
* background music at events;
* club and association events;
* unattended or semi-automatic music playback;
* managing larger local MP3 and FLAC collections.

DeckRelay is not intended to replace performance-oriented DJ software. Its focus is on preparation, reliable automation, and easy control of continuous music playback.

## Getting started

Before starting DeckRelay, make sure that VLC is installed. FFmpeg and
FFprobe are additionally required if you want DeckRelay to analyze new
audio files.

1. Download `DeckRelay-portable-1.0.0.zip` from the [latest release](https://github.com/wb56/deckrelay/releases/latest).
2. Extract the complete ZIP archive into a folder of your choice.
3. Start `DeckRelay.exe`.
4. Add a folder containing your music files to the catalog.
5. Add tracks to the queue and start playback.

No separate installation is required. Do not start DeckRelay directly from within the ZIP archive.

> **Windows SmartScreen:** Because DeckRelay is currently not digitally signed, Windows may display a security warning when it is started for the first time. The source code is publicly available, and every official release is tested automatically before publication.

## Supported system

* Windows 10 or Windows 11
* 64-bit system
* local MP3 and FLAC files
* sufficient free disk space for the catalog and application data
* VLC media player with libVLC for audio playback
* FFmpeg and FFprobe for cue and loudness analysis

## Stable release

DeckRelay 1.0.0 passed the complete release quality gate:

* 1,119 automated tests passed;
* Ruff, Black, and MyPy checks passed;
* three real FFmpeg format tests passed;
* portable release archive verified;
* release built from the published source commit.

For technical details and downloadable files, see the [DeckRelay 1.0.0 release](https://github.com/wb56/deckrelay/releases/tag/v1.0.0).

## Feedback and contributions

DeckRelay is a new open-source project. Reports from real-world use are especially valuable.

* [Report a bug](https://github.com/wb56/deckrelay/issues/new)
* [Suggest an improvement](https://github.com/wb56/deckrelay/issues/new)
* [View the source code](https://github.com/wb56/deckrelay)

When reporting a playback problem, please include the DeckRelay version, the Windows version, the audio format involved, and—if available—the diagnostic report created by DeckRelay.
