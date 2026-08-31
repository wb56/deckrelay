"""Snapshot-bound caching for technical audio information from the shared backend."""

from collections import OrderedDict
from concurrent.futures import Future
from pathlib import Path
from threading import Lock

from party_player.analysis import AudioAnalysisBackend, AudioFileInfo
from party_player.metadata_analysis_contracts import FileSnapshot


class TechnicalAudioFileChangedError(RuntimeError):
    """Raised when a source changes while FFprobe is reading it."""


class TechnicalAudioInfoService:
    """Deduplicate probes and cache results only for an unchanged file snapshot."""

    def __init__(self, backend: AudioAnalysisBackend, *, maximum_entries: int = 128) -> None:
        if maximum_entries <= 0:
            raise ValueError("maximum_entries muss positiv sein")
        self._backend = backend
        self._maximum_entries = maximum_entries
        self._cache: OrderedDict[str, tuple[FileSnapshot, AudioFileInfo]] = OrderedDict()
        self._in_flight: dict[FileSnapshot, Future[AudioFileInfo]] = {}
        self._lock = Lock()

    def probe(self, file_path: Path) -> AudioFileInfo:
        snapshot = FileSnapshot.capture(str(file_path))
        with self._lock:
            cached = self._cache.get(snapshot.normalized_path)
            if cached is not None and cached[0] == snapshot:
                self._cache.move_to_end(snapshot.normalized_path)
                return cached[1]
            if cached is not None:
                self._cache.pop(snapshot.normalized_path, None)
            shared = self._in_flight.get(snapshot)
            if shared is None:
                shared = Future()
                self._in_flight[snapshot] = shared
                owner = True
            else:
                owner = False
        if not owner:
            return shared.result(timeout=35.0)
        try:
            info = self._backend.probe(Path(snapshot.normalized_path))
            if not snapshot.matches_file():
                raise TechnicalAudioFileChangedError(
                    "Die Audiodatei wurde während der technischen Ermittlung geändert."
                )
        except Exception as exc:
            shared.set_exception(exc)
            raise
        else:
            with self._lock:
                self._cache[snapshot.normalized_path] = (snapshot, info)
                self._cache.move_to_end(snapshot.normalized_path)
                while len(self._cache) > self._maximum_entries:
                    self._cache.popitem(last=False)
            shared.set_result(info)
            return info
        finally:
            with self._lock:
                self._in_flight.pop(snapshot, None)

    @property
    def cached_entry_count(self) -> int:
        with self._lock:
            return len(self._cache)
