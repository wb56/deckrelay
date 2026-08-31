"""Rate-limited Python thread dumps for critical GUI heartbeat delays."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
import sys
from threading import enumerate as enumerate_threads
from time import monotonic
import traceback

from party_player.diagnostic_retention import retain_latest
from party_player.product import PRODUCT_NAME, PRODUCT_SLUG


class ThreadDumpWriter:
    """Persist diagnostic Python stacks without flooding the diagnostics directory.

    The writer is called by the independent GUI watchdog while Tk remains blocked.
    It captures the current frame map synchronously so the MainThread stack still
    identifies the active blocking callback.
    """

    def __init__(
        self,
        directory: Path = Path("diagnostics"),
        *,
        rate_limit_seconds: float = 60.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        """Configure the output directory and monotonic rate-limit clock."""
        self._directory = directory
        self._rate_limit = rate_limit_seconds
        self._clock = clock
        self._last_dump_at = float("-inf")

    def write(
        self,
        delay_ms: float,
        test_context: str,
        playback_state: str,
        dispatcher_state: str,
        callback_snapshot: object | None = None,
    ) -> Path | None:
        """Write all current Python stacks, or return ``None`` while rate-limited.

        The monotonic clock controls the 60-second rate limit. Wall-clock time is
        used only for the human-readable timestamp and filename.
        """
        now = self._clock()
        if now - self._last_dump_at < self._rate_limit:
            return None
        self._last_dump_at = now
        self._directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone()
        target = self._directory / f"{PRODUCT_SLUG}-thread-dump-{timestamp:%Y%m%d-%H%M%S}.txt"
        sequence = 1
        while target.exists():
            target = self._directory / (
                f"{PRODUCT_SLUG}-thread-dump-{timestamp:%Y%m%d-%H%M%S}-{sequence}.txt"
            )
            sequence += 1
        # Capture the frame map once so all thread stacks describe approximately
        # the same instant and the dump stays internally consistent.
        frames = sys._current_frames()
        lines = [
            f"{PRODUCT_NAME} critical GUI heartbeat thread dump",
            f"Timestamp: {timestamp.isoformat(timespec='seconds')}",
            f"Heartbeat delay: {delay_ms:.1f} ms",
            f"Test context: {test_context}",
            f"Playback state: {playback_state}",
            f"Dispatcher state: {dispatcher_state}",
            "Heartbeat/callback state:",
            "  last_heartbeat_monotonic: "
            f"{getattr(callback_snapshot, 'last_heartbeat_monotonic', 'unknown')}",
            f"  active_gui_callback: {getattr(callback_snapshot, 'active_gui_callback', None)}",
            "  active_gui_callback_started_at: "
            f"{getattr(callback_snapshot, 'active_gui_callback_started_at', None)}",
            "  last_started_gui_callback: "
            f"{getattr(callback_snapshot, 'last_started_gui_callback', None)}",
            "  last_completed_gui_callback: "
            f"{getattr(callback_snapshot, 'last_completed_gui_callback', None)}",
            "  milliseconds_since_last_callback_completion: "
            f"{self._milliseconds_since_completion(now, callback_snapshot)}",
            f"  active_catalog_render: {getattr(callback_snapshot, 'active_catalog_render', None)}",
            f"  active_queue_render: {getattr(callback_snapshot, 'active_queue_render', None)}",
            "  pending_layout_refreshes: "
            f"{getattr(callback_snapshot, 'pending_layout_refreshes', 0)}",
            "  pending_focus_request: "
            f"{getattr(callback_snapshot, 'pending_focus_request', False)}",
            f"  pending_catalog_chunks: {getattr(callback_snapshot, 'pending_catalog_chunks', 0)}",
            f"  pending_queue_chunks: {getattr(callback_snapshot, 'pending_queue_chunks', 0)}",
            f"  catalog_rows_created: {getattr(callback_snapshot, 'catalog_rows_created', 0)}",
            f"  queue_rows_created: {getattr(callback_snapshot, 'queue_rows_created', 0)}",
            "",
        ]
        for thread in enumerate_threads():
            heading = "MainThread" if thread.name == "MainThread" else thread.name
            lines.append(f"Thread {heading} (ident={thread.ident}, daemon={thread.daemon}):")
            frame = frames.get(thread.ident) if thread.ident is not None else None
            lines.extend(traceback.format_stack(frame) if frame is not None else ["  <no stack>\n"])
            lines.append("")
        target.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        retain_latest(self._directory, "thread-dump-*.txt", 500)
        return target

    @staticmethod
    def _milliseconds_since_completion(now: float, snapshot: object | None) -> str:
        completed_at = getattr(snapshot, "last_completed_gui_callback_at", None)
        if completed_at is None:
            return "unknown"
        return f"{max(0.0, (now - float(completed_at)) * 1000.0):.1f}"
