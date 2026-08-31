"""Thread-safe state for bounded, explicitly enabled diagnostic scenarios."""

from dataclasses import dataclass, replace
from datetime import datetime
from threading import Condition
from time import sleep


@dataclass(frozen=True, slots=True)
class DiagnosticScenarioSnapshot:
    name: str
    started_at: datetime
    ended_at: datetime | None
    injected_database_delay_ms: int
    statistics_reset_at_start: bool
    transitions_completed: int
    persistence_jobs_submitted: int
    persistence_jobs_completed: int
    persistence_jobs_failed: int
    active: bool

    @property
    def is_meaningful_database_test(self) -> bool:
        return self.transitions_completed > 0 and self.persistence_jobs_submitted > 0


class DiagnosticScenario:
    """Own scenario counters and inject delay only when explicitly active."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._snapshot: DiagnosticScenarioSnapshot | None = None

    def begin(self, name: str, database_delay_ms: int = 0) -> None:
        with self._condition:
            self._snapshot = DiagnosticScenarioSnapshot(
                name,
                datetime.now().astimezone(),
                None,
                max(0, database_delay_ms) if name == "database_delay" else 0,
                True,
                0,
                0,
                0,
                0,
                True,
            )

    def end(self) -> None:
        with self._condition:
            current = self._snapshot
            if current is not None:
                self._snapshot = replace(
                    current, ended_at=datetime.now().astimezone(), active=False
                )
            self._condition.notify_all()

    def snapshot(self) -> DiagnosticScenarioSnapshot | None:
        with self._condition:
            return self._snapshot

    def inject_database_delay(self) -> int:
        with self._condition:
            current = self._snapshot
            delay_ms = (
                current.injected_database_delay_ms
                if current is not None and current.active and current.name == "database_delay"
                else 0
            )
        if delay_ms:
            sleep(delay_ms / 1000.0)
        return delay_ms

    def transition_completed(self) -> None:
        self._increment("transitions_completed")

    def persistence_submitted(self) -> None:
        self._increment("persistence_jobs_submitted")

    def persistence_completed(self) -> None:
        self._increment("persistence_jobs_completed")

    def persistence_failed(self) -> None:
        self._increment("persistence_jobs_failed")

    def wait_for_persistence(self, timeout: float = 30.0) -> bool:
        """Wait off the GUI thread until all scenario persistence jobs finish."""
        with self._condition:
            return self._condition.wait_for(
                lambda: (
                    self._snapshot is None
                    or self._snapshot.persistence_jobs_completed
                    + self._snapshot.persistence_jobs_failed
                    >= self._snapshot.persistence_jobs_submitted
                ),
                timeout,
            )

    def _increment(self, field: str) -> None:
        with self._condition:
            current = self._snapshot
            if current is None or not current.active:
                return
            self._snapshot = replace(current, **{field: getattr(current, field) + 1})
            self._condition.notify_all()
