"""LibVLC implementation of the audio contract."""

import logging
import os
import sys
from ctypes import windll
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

from party_player.equalizer import EqualizerService, ResolvedEqualizerPreset


class VlcAudioBackend:
    """One media player per deck backed by one shared LibVLC instance."""

    MAXIMUM_VOLUME_FACTOR = 2.0
    _shared_instance: Any = None
    _shared_installation_directory: Path | None = None
    _active_players = 0
    _active_players_lock = Lock()

    def __init__(
        self,
        output_device: str = "",
        worker_name: str = "vlc-volume",
        installation_directory: str | Path | None = None,
    ) -> None:
        self._dll_directory: Any = None
        configured_directory = (
            Path(installation_directory).resolve() if installation_directory else None
        )
        if sys.platform == "win32" and (
            configured_directory is not None or getattr(sys, "frozen", False)
        ):
            runtime_dir = Path(sys.executable).resolve().parent
            bundle_dir = configured_directory or Path(getattr(sys, "_MEIPASS", runtime_dir))
            plugin_dir = bundle_dir / "plugins"
            os.environ["VLC_PLUGIN_PATH"] = str(plugin_dir)
            self._dll_directory = os.add_dll_directory(str(bundle_dir))
        try:
            import vlc  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError("python-vlc ist nicht installiert") from exc
        self._vlc: Any = vlc
        if VlcAudioBackend._shared_instance is None:
            options = ["--no-video", "--quiet"]
            if sys.platform == "win32":
                options.append("--aout=directsound")
            VlcAudioBackend._shared_instance = vlc.Instance(*options)
            VlcAudioBackend._shared_installation_directory = configured_directory
        elif VlcAudioBackend._shared_installation_directory != configured_directory:
            raise RuntimeError(
                "Die laufende VLC-Instanz verwendet ein anderes Installationsverzeichnis"
            )
        self._instance: Any = VlcAudioBackend._shared_instance
        self._player: Any = self._instance.media_player_new()
        self._media: Any = None
        with type(self)._active_players_lock:
            type(self)._active_players += 1
        self._output_device = output_device.strip()
        self._worker_name = worker_name
        if self._output_device:
            self._player.audio_output_device_set(None, self._output_device)
        self._paused = False
        self._requested_seek_seconds: float | None = None
        self._volume_percent: int | None = None
        self._requested_volume_percent: int | None = None
        self._volume_call_lock = Lock()
        self._lifecycle_lock = Lock()
        self._volume_changed = Event()
        self._volume_stop = Event()
        self._volume_thread: Thread | None = None
        self._closed = False
        self._equalizer: Any = None
        self._equalizer_snapshot: ResolvedEqualizerPreset | None = None
        self._logger = logging.getLogger(__name__)

    def list_output_devices(self) -> list[tuple[str, str]]:
        devices: list[tuple[str, str]] = []
        device_list = self._player.audio_output_device_enum()
        current = device_list
        try:
            while current:
                entry = current.contents
                device_id = entry.device.decode(errors="replace") if entry.device else ""
                description = (
                    entry.description.decode(errors="replace") if entry.description else device_id
                )
                if device_id:
                    devices.append((device_id, description))
                current = entry.next
        finally:
            if device_list:
                self._vlc.libvlc_audio_output_device_list_release(device_list)
        return devices

    def maximum_volume_factor(self) -> float:
        """Return VLC's practical 200% software-volume ceiling."""
        return self.MAXIMUM_VOLUME_FACTOR

    def supports_runtime_clip_protection(self) -> bool:
        """VLC exposes no portable, true-peak-aware real-time limiter control."""
        return False

    def set_runtime_clip_protection(self, enabled: bool, ceiling_dbfs: float) -> bool:
        """Report unsupported capability; LoudnessService supplies the safe fallback."""
        del enabled, ceiling_dbfs
        return False

    def set_output_device(self, device_id: str) -> None:
        self._output_device = device_id.strip()
        self._player.audio_output_device_set(None, self._output_device or None)

    def equalizer_band_frequencies(self) -> tuple[float, ...]:
        count = int(self._vlc.libvlc_audio_equalizer_get_band_count())
        return tuple(
            float(self._vlc.libvlc_audio_equalizer_get_band_frequency(index))
            for index in range(count)
        )

    def apply_equalizer(self, preset: ResolvedEqualizerPreset) -> bool:
        """Apply one immutable snapshot, retaining VLC's equalizer handle per deck."""
        if preset == self._equalizer_snapshot:
            return False
        EqualizerService().validate_resolved(preset)
        if not preset.enabled:
            result = self._player.set_equalizer(None)
            if result != 0:
                raise RuntimeError("VLC konnte den Equalizer nicht deaktivieren")
            self._equalizer = None
            self._equalizer_snapshot = preset
            return True
        frequencies = self.equalizer_band_frequencies()
        if frequencies != preset.band_frequencies_hz:
            raise ValueError("Equalizer-Snapshot passt nicht zur VLC-Bandstruktur")
        self._logger.info(
            "equalizer.create preset=%s preamp_db=%.1f bands=%d",
            preset.name,
            preset.preamp_db,
            len(preset.band_gains_db),
        )
        equalizer = self._vlc.AudioEqualizer()
        if equalizer.set_preamp(preset.preamp_db) != 0:
            raise RuntimeError("VLC konnte den Equalizer-Preamp nicht setzen")
        for index, gain_db in enumerate(preset.band_gains_db):
            if equalizer.set_amp_at_index(gain_db, index) != 0:
                raise RuntimeError(f"VLC konnte Equalizer-Band {index} nicht setzen")
        if self._player.set_equalizer(equalizer) != 0:
            raise RuntimeError("VLC konnte den Equalizer nicht anwenden")
        self._equalizer = equalizer
        self._equalizer_snapshot = preset
        return True

    def load(self, file_path: Path) -> None:
        self.load_prepared(file_path, self.prepare(file_path))

    def prepare(self, file_path: Path) -> object:
        network_path = self._is_network_path(file_path)
        if not network_path:
            self._validate_audio_file(file_path)
        media = self._instance.media_new(str(file_path))
        if network_path:
            media.add_option(":network-caching=1500")
            self._logger.info("VLC-Netzwerkpuffer für %s aktiviert", file_path)
        parse_flag = (
            self._vlc.MediaParseFlag.network if network_path else self._vlc.MediaParseFlag.local
        )
        media.parse_with_options(parse_flag, 5000)
        return media

    def load_prepared(self, file_path: Path, prepared: object) -> None:
        media = prepared
        self._player.set_media(media)
        previous_media, self._media = self._media, media
        if previous_media is not None and previous_media is not media:
            self.release_prepared(previous_media)
        self._paused = False
        self._requested_seek_seconds = None

    def release_prepared(self, prepared: object) -> None:
        """Release a VLC media handle that is replaced or never adopted."""
        try:
            release = getattr(prepared, "release", None)
            if callable(release):
                release()
        except OSError as exc:
            self._logger.warning("VLC-Medium konnte nicht freigegeben werden: %s", exc)

    def play(self) -> None:
        if self._player.play() == -1:
            raise RuntimeError("VLC konnte die Wiedergabe nicht starten")
        self._paused = False
        if self._requested_seek_seconds is not None:
            self._player.set_time(round(self._requested_seek_seconds * 1000))
        if self._output_device:
            self._player.audio_output_device_set(None, self._output_device)
        # A newly started VLC output may not retain the value accepted for the
        # previous/stopped medium.  Invalidate the applied-value cache so the
        # requested effective deck volume is always sent after ``play()``.
        self._volume_percent = None
        self._apply_pending_volume()

    def pause(self) -> None:
        self._player.set_pause(1)
        self._paused = True

    def resume(self) -> None:
        self._player.set_pause(0)
        self._paused = False

    def stop(self) -> None:
        self._player.stop()
        self._paused = False

    def seek(self, position_seconds: float) -> None:
        self._requested_seek_seconds = max(0.0, position_seconds)
        self._player.set_time(round(self._requested_seek_seconds * 1000))

    def set_volume(self, volume: float) -> None:
        # LibVLC accepts amplification above 100%; LoudnessService has already
        # applied gain and peak-safety limits before this resolved value arrives.
        volume_percent = round(max(0.0, min(volume, self.MAXIMUM_VOLUME_FACTOR)) * 100)
        self._requested_volume_percent = volume_percent
        if self._requested_volume_percent == self._volume_percent:
            return
        self._start_volume_worker()

    def get_position(self) -> float:
        position = float(max(0.0, self._player.get_time() / 1000.0))
        requested = getattr(self, "_requested_seek_seconds", None)
        if requested is not None:
            if abs(position - requested) <= 0.25:
                self._requested_seek_seconds = None
            elif self._player.is_playing():
                # VLC may discard a seek issued before its output is running.
                self._player.set_time(round(requested * 1000))
        return position

    def get_duration(self) -> float:
        return float(max(0.0, self._player.get_length() / 1000.0))

    def is_playing(self) -> bool:
        return bool(self._player.is_playing())

    def is_paused(self) -> bool:
        return self._paused

    def is_finished(self) -> bool:
        return bool(self._player.get_state() == self._vlc.State.Ended)

    def playback_state(self) -> str:
        """Expose the current VLC state for the existing status/diagnostic sample."""
        state = self._player.get_state()
        name = getattr(state, "name", None)
        return str(name or state).rsplit(".", 1)[-1].upper()

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._volume_stop.set()
            self._volume_changed.set()
            thread = getattr(self, "_volume_thread", None)
            if thread is not None and thread.is_alive():
                thread.join(1.0)
            with self._volume_call_lock:
                try:
                    self._player.stop()
                except OSError as exc:
                    self._logger.warning("VLC-Player konnte beim Schließen nicht stoppen: %s", exc)
                try:
                    self._player.release()
                except OSError as exc:
                    self._logger.warning("VLC-Player konnte nicht freigegeben werden: %s", exc)
            media = getattr(self, "_media", None)
            if media is not None:
                self.release_prepared(media)
            self._media = None
            self._equalizer = None
            self._equalizer_snapshot = None
            self._player = None
            self._volume_thread = None
            with type(self)._active_players_lock:
                type(self)._active_players = max(0, type(self)._active_players - 1)

    @classmethod
    def active_player_count(cls) -> int:
        with cls._active_players_lock:
            return cls._active_players

    @classmethod
    def shared_instance_identity(cls) -> int | None:
        """Expose identity for lifecycle assertions without leaking the instance."""
        return id(cls._shared_instance) if cls._shared_instance is not None else None

    @classmethod
    def release_shared_instance(cls) -> bool:
        """Release the process resource only when no player can still reference it."""
        with cls._active_players_lock:
            if cls._active_players:
                return False
            instance, cls._shared_instance = cls._shared_instance, None
            cls._shared_installation_directory = None
        if instance is not None:
            try:
                instance.release()
            except OSError:
                logging.getLogger(__name__).warning(
                    "Gemeinsame VLC-Instanz konnte nicht freigegeben werden"
                )
        return True

    def _apply_pending_volume(self) -> None:
        """Apply one volume synchronously at playback startup."""
        requested = self._requested_volume_percent
        if requested is None or requested == self._volume_percent:
            return
        with self._volume_call_lock:
            if self._player.audio_set_volume(requested) == 0:
                self._volume_percent = requested
            else:
                self._logger.debug(
                    "VLC-Audioausgang noch nicht bereit; Lautstärke wird erneut gesetzt"
                )

    def _start_volume_worker(self) -> None:
        """Wake one persistent coalescing worker without taking a fade-thread lock."""
        # Tests may construct the adapter via __new__; initialize worker state lazily.
        if not hasattr(self, "_volume_changed"):
            self._volume_call_lock = Lock()
            self._volume_changed = Event()
            self._volume_stop = Event()
            self._closed = False
            self._volume_thread = None
        if self._closed:
            return
        if self._volume_thread is None or not self._volume_thread.is_alive():
            self._volume_thread = Thread(
                target=self._volume_worker,
                name=getattr(self, "_worker_name", "vlc-volume"),
                daemon=True,
            )
            self._volume_thread.start()
        self._volume_changed.set()

    def _volume_worker(self) -> None:
        while not self._volume_stop.is_set():
            self._volume_changed.wait()
            self._volume_changed.clear()
            failed_attempts = 0
            while not self._volume_stop.is_set():
                requested = self._requested_volume_percent
                if requested is None or requested == self._volume_percent:
                    break
                with self._volume_call_lock:
                    result = self._player.audio_set_volume(requested)
                if result == 0:
                    self._volume_percent = requested
                    failed_attempts = 0
                else:
                    failed_attempts += 1
                    if failed_attempts >= 2:
                        self._logger.debug("VLC-Audioausgang noch nicht bereit")
                        break

    @staticmethod
    def _is_network_path(file_path: Path) -> bool:
        path_text = str(file_path)
        if path_text.startswith(("\\\\", "//")):
            return True
        if sys.platform != "win32" or not file_path.drive:
            return False
        root = f"{file_path.drive}\\"
        drive_remote = 4
        try:
            return bool(windll.kernel32.GetDriveTypeW(root) == drive_remote)
        except (AttributeError, OSError):
            return False

    @staticmethod
    def _validate_audio_file(file_path: Path) -> None:
        """Reject clearly invalid containers before handing them to LibVLC."""
        try:
            with file_path.open("rb") as audio_file:
                header = audio_file.read(10)
                if file_path.suffix.lower() == ".flac":
                    metadata = audio_file.read(32)
                    valid = (
                        header.startswith(b"fLaC")
                        and len(header) + len(metadata) >= 42
                        and header[4] & 0x7F == 0
                        and int.from_bytes(header[5:8], "big") == 34
                    )
                else:
                    if header.startswith(b"ID3") and len(header) == 10:
                        tag_size = sum(
                            value << shift
                            for value, shift in zip(header[6:10], (21, 14, 7, 0), strict=True)
                        )
                        audio_file.seek(10 + tag_size)
                        sample = audio_file.read(65536)
                    else:
                        sample = header + audio_file.read(65526)
                    valid = any(
                        sample[index] == 0xFF and sample[index + 1] & 0xE0 == 0xE0
                        for index in range(max(0, len(sample) - 1))
                    )
        except OSError as exc:
            raise RuntimeError("Die Audiodatei ist nicht erreichbar oder nicht lesbar") from exc
        if not valid:
            raise ValueError("Die Audiodatei ist beschädigt oder enthält keine lesbare Audiospur")
