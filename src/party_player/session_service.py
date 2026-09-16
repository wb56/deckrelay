"""Party session lifecycle and safe recovery."""

import json
from collections.abc import Callable

from party_player.enums import SessionStatus
from party_player.models import PartySession
from party_player.queue_service import QueueService
from party_player.repository import PartyPlayerRepository


class PartySessionService:
    def __init__(self, repository: PartyPlayerRepository) -> None:
        self._repository = repository
        self._discard_automatic_plan: Callable[[int], None] | None = None

    def bind_automatic_plan_discard(self, callback: Callable[[int], None]) -> None:
        self._discard_automatic_plan = callback

    def start(self, name: str = "Party") -> PartySession:
        return self._repository.create_session(name)

    def restore_or_start(self, restore: bool = True) -> PartySession:
        previous = self._repository.latest_unfinished_session() if restore else None
        if previous is None:
            fresh = self.start()
            if restore:
                finished = self._repository.latest_finished_session_with_pending_queue()
                if finished is not None:
                    copied = QueueService.copy_persisted_pending_queue(
                        self._repository,
                        finished.session_id,
                        fresh.session_id,
                    )
                    if copied:
                        self._repository.set_session_status(
                            fresh.session_id, SessionStatus.RECOVERED
                        )
                        return PartySession(
                            session_id=fresh.session_id,
                            name=fresh.name,
                            started_at=fresh.started_at,
                            status=SessionStatus.RECOVERED,
                            settings_snapshot=fresh.settings_snapshot,
                        )
            return fresh
        QueueService.recover_persisted_session(
            self._repository,
            previous.session_id,
        )
        self._repository.set_session_status(previous.session_id, SessionStatus.RECOVERED)
        return PartySession(
            session_id=previous.session_id,
            name=previous.name,
            started_at=previous.started_at,
            ended_at=None,
            status=SessionStatus.RECOVERED,
            selected_playlist=previous.selected_playlist,
            settings_snapshot=previous.settings_snapshot,
        )

    def finish(self, session_id: int) -> None:
        if self._discard_automatic_plan is not None:
            self._discard_automatic_plan(session_id)
        self._repository.set_session_status(session_id, SessionStatus.FINISHED)

    def select_playlist(self, session_id: int, saved_queue_id: int | None) -> None:
        self._repository.set_selected_playlist(session_id, saved_queue_id)

    @staticmethod
    def settings_snapshot(master_volume: float, crossfader_position: float, mode: str) -> str:
        return json.dumps(
            {
                "master_volume": master_volume,
                "crossfader_position": crossfader_position,
                "mode": mode,
            }
        )
