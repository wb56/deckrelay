"""Atomic persistence for automatic-selection plans and their queue linkage."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import sqlite3

from party_player.automatic_selection_plan import (
    AutomaticSelectionPlan,
    AutomaticSelectionPlanBundle,
    AutomaticSelectionPlanRecoveryAction,
    AutomaticSelectionPlanRecoveryReason,
    AutomaticSelectionPlanRecoveryState,
    AutomaticSelectionPlanStatus,
    AutomaticSelectionPlanStep,
    AutomaticSelectionPlanStepStatus,
)
from party_player.database.connection import Database
from party_player.enums import QueueSource, QueueStatus
from party_player.models import QueueEntry
from party_player.repository import PartyPlayerRepository


class AutomaticSelectionPlanNotFoundError(LookupError):
    pass


class AutomaticSelectionPlanConflictError(RuntimeError):
    pass


_TRANSITIONS = {
    AutomaticSelectionPlanStatus.DRAFT: {
        AutomaticSelectionPlanStatus.ACTIVE,
        AutomaticSelectionPlanStatus.DISCARDED,
    },
    AutomaticSelectionPlanStatus.ACTIVE: {
        AutomaticSelectionPlanStatus.PAUSED,
        AutomaticSelectionPlanStatus.COMPLETED,
        AutomaticSelectionPlanStatus.INVALIDATED,
        AutomaticSelectionPlanStatus.DISCARDED,
    },
    AutomaticSelectionPlanStatus.PAUSED: {
        AutomaticSelectionPlanStatus.ACTIVE,
        AutomaticSelectionPlanStatus.INVALIDATED,
        AutomaticSelectionPlanStatus.DISCARDED,
    },
}


class AutomaticSelectionPlanRepository:
    def __init__(self, database: Database, party_repository: PartyPlayerRepository) -> None:
        self._database = database
        self._party_repository = party_repository

    def create(self, bundle: AutomaticSelectionPlanBundle) -> AutomaticSelectionPlanBundle:
        with self._database.transaction() as connection:
            self._insert_plan(connection, bundle.plan)
            connection.executemany(
                """INSERT INTO automatic_selection_plan_steps
                   (step_id, plan_id, position, track_id, status, planned_at,
                    previous_track_id, relaxation_stage_code, primary_play_count,
                    secondary_score, tie_candidate_count, tie_break_method_code,
                    selection_reason_code, executed_queue_entry_id, invalid_reason_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [self._step_values(step) for step in bundle.steps],
            )
        return bundle

    def create_replacing_resumable(
        self,
        bundle: AutomaticSelectionPlanBundle,
        old_plan_id: str,
        expected_revision: int,
        current_configuration_digest: str,
    ) -> AutomaticSelectionPlanBundle:
        """Create one draft and replace one resumable plan in a single transaction."""
        with self._database.transaction() as connection:
            old = self._required_plan(connection, old_plan_id)
            self._require_revision(old, expected_revision)
            if old.status not in {
                AutomaticSelectionPlanStatus.ACTIVE,
                AutomaticSelectionPlanStatus.PAUSED,
            }:
                raise ValueError("only a resumable plan can be replaced")
            if bundle.plan.session_id != old.session_id:
                raise ValueError("replacement session differs")
            if bundle.plan.rule_configuration_digest != current_configuration_digest:
                raise ValueError("replacement configuration changed")
            self._insert_plan(connection, bundle.plan)
            connection.executemany(
                """INSERT INTO automatic_selection_plan_steps
                   (step_id, plan_id, position, track_id, status, planned_at,
                    previous_track_id, relaxation_stage_code, primary_play_count,
                    secondary_score, tie_candidate_count, tie_break_method_code,
                    selection_reason_code, executed_queue_entry_id, invalid_reason_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [self._step_values(step) for step in bundle.steps],
            )
            connection.execute(
                """UPDATE party_queue SET status='removed', loaded_deck=NULL,
                          updated_at=CURRENT_TIMESTAMP
                   WHERE id IN (SELECT executed_queue_entry_id
                                FROM automatic_selection_plan_steps WHERE plan_id=?)
                     AND source='AUTOMATIC' AND status='waiting'""",
                (old_plan_id,),
            )
            connection.execute(
                """UPDATE automatic_selection_plan_steps
                   SET status='INVALIDATED', invalid_reason_code='PLAN_RECALCULATED'
                   WHERE plan_id=? AND status='PLANNED'""",
                (old_plan_id,),
            )
            retired = replace(
                old,
                status=AutomaticSelectionPlanStatus.INVALIDATED,
                updated_at=datetime.utcnow(),
                revision=old.revision + 1,
                invalid_reason_code="PLAN_RECALCULATED",
            )
            self._update_plan_cas(connection, retired, expected_revision)
            active = replace(
                bundle.plan,
                status=AutomaticSelectionPlanStatus.ACTIVE,
                updated_at=datetime.utcnow(),
                revision=bundle.plan.revision + 1,
            )
            self._update_plan_cas(connection, active, bundle.plan.revision)
            return self._load_steps(connection, active)

    def get(self, plan_id: str) -> AutomaticSelectionPlanBundle | None:
        with self._database.connect() as connection:
            return self._load_bundle(connection, plan_id)

    def get_resumable_for_session(self, session_id: int) -> AutomaticSelectionPlanBundle | None:
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM automatic_selection_plans
                   WHERE session_id=? AND status IN ('ACTIVE','PAUSED')""",
                (session_id,),
            ).fetchall()
            if len(rows) > 1:
                raise RuntimeError("multiple resumable plans violate persistence invariants")
            if not rows:
                return None
            return self._load_steps(connection, self._plan_from_row(rows[0]))

    def active_queue_track_ids(self, session_id: int) -> frozenset[int]:
        """Return tracks already occupying the active queue or a deck."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT track_id FROM party_queue
                   WHERE session_id=?
                     AND status IN ('waiting','preparing','ready','playing')""",
                (session_id,),
            ).fetchall()
        return frozenset(int(row["track_id"]) for row in rows)

    def automatic_session_track_ids(self, session_id: int) -> frozenset[int]:
        """Return every title already materialized automatically in this session."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT track_id FROM party_queue
                   WHERE session_id=? AND source='AUTOMATIC'""",
                (session_id,),
            ).fetchall()
        return frozenset(int(row["track_id"]) for row in rows)

    def automatic_buffer_count(self, session_id: int) -> int:
        """Count non-playing automatic rows that currently satisfy the buffer."""
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS count FROM party_queue
                   WHERE session_id=? AND source='AUTOMATIC'
                     AND status IN ('waiting','preparing','ready')""",
                (session_id,),
            ).fetchone()
        return int(row["count"])

    def relaxation_for_queue_entry(self, session_id: int, queue_id: int) -> str | None:
        """Resolve the reviewed relaxation even after its source plan completed."""
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT s.relaxation_stage_code
                   FROM automatic_selection_plan_steps s
                   JOIN automatic_selection_plans p ON p.plan_id=s.plan_id
                   WHERE p.session_id=? AND s.executed_queue_entry_id=?
                   ORDER BY p.created_at DESC LIMIT 1""",
                (session_id, queue_id),
            ).fetchone()
        return None if row is None else str(row["relaxation_stage_code"])

    def get_latest_for_session(self, session_id: int) -> AutomaticSelectionPlanBundle | None:
        """Load the newest plan and all steps in two queries."""
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT * FROM automatic_selection_plans
                   WHERE session_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
            return None if row is None else self._load_steps(connection, self._plan_from_row(row))

    def recovery_state(
        self, session_id: int, current_configuration_digest: str
    ) -> AutomaticSelectionPlanRecoveryState | None:
        """Pause and describe one resumable plan in one short transaction."""
        with self._database.transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM automatic_selection_plans
                   WHERE session_id=? AND status IN ('ACTIVE','PAUSED')""",
                (session_id,),
            ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise RuntimeError("multiple resumable plans")
            plan = self._plan_from_row(rows[0])
            if plan.status is AutomaticSelectionPlanStatus.ACTIVE:
                paused = replace(
                    plan,
                    status=AutomaticSelectionPlanStatus.PAUSED,
                    updated_at=datetime.utcnow(),
                    revision=plan.revision + 1,
                )
                self._update_plan_cas(connection, paused, plan.revision)
                plan = paused
            step_rows = connection.execute(
                """SELECT s.step_id, s.track_id, s.status, s.previous_track_id,
                          s.executed_queue_entry_id,
                          q.status AS queue_status, q.skip_code
                   FROM automatic_selection_plan_steps s
                   LEFT JOIN party_queue q ON q.id=s.executed_queue_entry_id
                   WHERE s.plan_id=? ORDER BY s.position""",
                (plan.plan_id,),
            ).fetchall()
            interrupted = next(
                (row for row in step_rows if row["skip_code"] == "RECOVERY_INTERRUPTED_PLAYBACK"),
                None,
            )
            missing = any(
                row["status"] == "QUEUED" and row["queue_status"] is None for row in step_rows
            )
            predecessor_row = connection.execute(
                """SELECT track_id FROM (
                       SELECT track_id, COALESCE(played_at, updated_at) AS occurred, id
                       FROM party_queue WHERE session_id=? AND status='playing'
                       UNION ALL
                       SELECT track_id, finished_at AS occurred, id FROM play_history
                       WHERE completion_status='PLAYED'
                   ) ORDER BY occurred DESC, id DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
            actual_predecessor = (
                int(interrupted["track_id"])
                if interrupted is not None
                else int(predecessor_row["track_id"]) if predecessor_row is not None else None
            )
            configuration_matches = plan.rule_configuration_digest == current_configuration_digest
            first_remaining = next(
                (
                    row
                    for row in step_rows
                    if row["status"] == "PLANNED"
                    or row["queue_status"] in {"waiting", "preparing", "ready", "playing"}
                ),
                None,
            )
            stored_previous = None
            if first_remaining is not None:
                stored_previous = (
                    int(first_remaining["previous_track_id"])
                    if first_remaining["previous_track_id"] is not None
                    else None
                )
            predecessor_changed = (
                first_remaining is not None and stored_previous != actual_predecessor
            )
            reason = (
                AutomaticSelectionPlanRecoveryReason.INTERRUPTED_PLAYBACK
                if interrupted is not None
                else (
                    AutomaticSelectionPlanRecoveryReason.INCONSISTENT_QUEUE_LINK
                    if missing
                    else (
                        AutomaticSelectionPlanRecoveryReason.CONFIGURATION_CHANGED
                        if not configuration_matches
                        else (
                            AutomaticSelectionPlanRecoveryReason.PREDECESSOR_CHANGED
                            if predecessor_changed
                            else AutomaticSelectionPlanRecoveryReason.RECOVERY_REQUIRED
                        )
                    )
                )
            )
            return AutomaticSelectionPlanRecoveryState(
                plan.plan_id,
                session_id,
                plan.status,
                plan.revision,
                sum(row["status"] == "PLANNED" for row in step_rows),
                sum(
                    row["queue_status"] in {"waiting", "preparing", "ready", "playing"}
                    for row in step_rows
                ),
                str(interrupted["step_id"]) if interrupted is not None else None,
                int(interrupted["track_id"]) if interrupted is not None else None,
                actual_predecessor,
                configuration_matches,
                (
                    (AutomaticSelectionPlanRecoveryAction.RESUME,)
                    if configuration_matches
                    and interrupted is None
                    and not missing
                    and not predecessor_changed
                    else ()
                )
                + (
                    AutomaticSelectionPlanRecoveryAction.RECALCULATE,
                    AutomaticSelectionPlanRecoveryAction.DISCARD,
                ),
                reason,
            )

    def resume_recovered_plan(
        self, plan_id: str, expected_revision: int
    ) -> AutomaticSelectionPlanBundle:
        return self.change_status(plan_id, expected_revision, AutomaticSelectionPlanStatus.ACTIVE)

    def pause_for_foreign_playback(self, session_id: int, queue_id: int) -> bool:
        """Pause and invalidate the unmaterialized tail after actual foreign playback."""
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM automatic_selection_plans
                   WHERE session_id=? AND status='ACTIVE'""",
                (session_id,),
            ).fetchone()
            if row is None:
                return False
            plan = self._plan_from_row(row)
            linked = connection.execute(
                """SELECT 1 FROM automatic_selection_plan_steps s
                   JOIN automatic_selection_plans p ON p.plan_id=s.plan_id
                   WHERE p.session_id=? AND s.executed_queue_entry_id=?""",
                (session_id, queue_id),
            ).fetchone()
            if linked is not None:
                return False
            connection.execute(
                """UPDATE automatic_selection_plan_steps
                   SET status='INVALIDATED', invalid_reason_code='FOREIGN_PLAYBACK'
                   WHERE plan_id=? AND status='PLANNED'""",
                (plan.plan_id,),
            )
            candidate = replace(
                plan,
                status=AutomaticSelectionPlanStatus.PAUSED,
                updated_at=datetime.utcnow(),
                revision=plan.revision + 1,
            )
            self._update_plan_cas(connection, candidate, plan.revision)
            return True

    def discard_for_finished_session(self, session_id: int) -> None:
        """Prevent a resumable plan from crossing its owning session boundary."""
        with self._database.transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM automatic_selection_plans
                   WHERE session_id=? AND status IN ('ACTIVE','PAUSED')""",
                (session_id,),
            ).fetchall()
            for row in rows:
                plan = self._plan_from_row(row)
                connection.execute(
                    """UPDATE automatic_selection_plan_steps
                       SET status='INVALIDATED', invalid_reason_code='SESSION_FINISHED'
                       WHERE plan_id=? AND status='PLANNED'""",
                    (plan.plan_id,),
                )
                discarded = replace(
                    plan,
                    status=AutomaticSelectionPlanStatus.DISCARDED,
                    updated_at=datetime.utcnow(),
                    revision=plan.revision + 1,
                    invalid_reason_code="SESSION_FINISHED",
                )
                self._update_plan_cas(connection, discarded, plan.revision)

    def discard_recovered_plan(
        self, plan_id: str, expected_revision: int
    ) -> AutomaticSelectionPlanBundle:
        """Discard remaining work and only remove safe waiting automatic links."""
        with self._database.transaction() as connection:
            plan = self._required_plan(connection, plan_id)
            if plan.status is AutomaticSelectionPlanStatus.DISCARDED:
                return self._load_steps(connection, plan)
            self._require_revision(plan, expected_revision)
            playing = connection.execute(
                """SELECT 1 FROM automatic_selection_plan_steps s
                   JOIN party_queue q ON q.id=s.executed_queue_entry_id
                   WHERE s.plan_id=? AND q.status='playing' LIMIT 1""",
                (plan_id,),
            ).fetchone()
            if playing is not None:
                raise ValueError("playing plan entry cannot be discarded")
            connection.execute(
                """UPDATE party_queue SET status='removed', loaded_deck=NULL,
                          updated_at=CURRENT_TIMESTAMP
                   WHERE id IN (SELECT executed_queue_entry_id
                                FROM automatic_selection_plan_steps WHERE plan_id=?)
                     AND source='AUTOMATIC' AND status='waiting'""",
                (plan_id,),
            )
            connection.execute(
                """UPDATE automatic_selection_plan_steps
                   SET status='INVALIDATED', invalid_reason_code='PLAN_DISCARDED'
                   WHERE plan_id=? AND status='PLANNED'""",
                (plan_id,),
            )
            discarded = replace(
                plan,
                status=AutomaticSelectionPlanStatus.DISCARDED,
                updated_at=datetime.utcnow(),
                revision=plan.revision + 1,
                invalid_reason_code="PLAN_DISCARDED",
            )
            self._update_plan_cas(connection, discarded, expected_revision)
            return self._load_steps(connection, discarded)

    def replace_recovered_plan(
        self,
        old_plan_id: str,
        expected_revision: int,
        new_plan_id: str,
        current_configuration_digest: str,
    ) -> AutomaticSelectionPlanBundle:
        """Atomically retire the old remainder and activate a completed draft."""
        with self._database.transaction() as connection:
            old = self._required_plan(connection, old_plan_id)
            self._require_revision(old, expected_revision)
            new = self._required_plan(connection, new_plan_id)
            if new.status is not AutomaticSelectionPlanStatus.DRAFT:
                raise ValueError("replacement must be a draft")
            if new.session_id != old.session_id:
                raise ValueError("replacement session differs")
            if new.rule_configuration_digest != current_configuration_digest:
                raise ValueError("replacement configuration changed")
            connection.execute(
                """UPDATE party_queue SET status='removed', loaded_deck=NULL,
                          updated_at=CURRENT_TIMESTAMP
                   WHERE id IN (SELECT executed_queue_entry_id
                                FROM automatic_selection_plan_steps WHERE plan_id=?)
                     AND source='AUTOMATIC' AND status='waiting'""",
                (old_plan_id,),
            )
            connection.execute(
                """UPDATE automatic_selection_plan_steps
                   SET status='INVALIDATED', invalid_reason_code='PLAN_RECALCULATED'
                   WHERE plan_id=? AND status='PLANNED'""",
                (old_plan_id,),
            )
            retired = replace(
                old,
                status=AutomaticSelectionPlanStatus.INVALIDATED,
                updated_at=datetime.utcnow(),
                revision=old.revision + 1,
                invalid_reason_code="PLAN_RECALCULATED",
            )
            self._update_plan_cas(connection, retired, expected_revision)
            active = replace(
                new,
                status=AutomaticSelectionPlanStatus.ACTIVE,
                updated_at=datetime.utcnow(),
                revision=new.revision + 1,
            )
            self._update_plan_cas(connection, active, new.revision)
            return self._load_steps(connection, active)

    def activate_draft_plan(
        self,
        plan_id: str,
        expected_revision: int,
        current_configuration_digest: str,
    ) -> AutomaticSelectionPlanBundle:
        """Activate one eligible draft with all invariants checked atomically."""
        with self._database.transaction() as connection:
            plan = self._required_plan(connection, plan_id)
            self._require_revision(plan, expected_revision)
            if plan.status is not AutomaticSelectionPlanStatus.DRAFT:
                raise ValueError("only draft plans can be activated")
            if plan.rule_configuration_digest != current_configuration_digest:
                raise ValueError("plan configuration changed")
            session = connection.execute(
                "SELECT status FROM party_sessions WHERE id=?", (plan.session_id,)
            ).fetchone()
            if session is None or str(session["status"]) not in {
                "active",
                "paused",
                "recovered",
            }:
                raise ValueError("session is missing or not eligible for planning")
            planned = connection.execute(
                """SELECT COUNT(*) FROM automatic_selection_plan_steps
                   WHERE plan_id=? AND status='PLANNED'""",
                (plan_id,),
            ).fetchone()[0]
            if int(planned) < 1:
                raise ValueError("draft plan has no planned steps")
            conflict = connection.execute(
                """SELECT 1 FROM automatic_selection_plans
                   WHERE session_id=? AND plan_id<>?
                     AND status IN ('ACTIVE','PAUSED') LIMIT 1""",
                (plan.session_id, plan_id),
            ).fetchone()
            if conflict is not None:
                raise AutomaticSelectionPlanConflictError("another resumable plan already exists")
            candidate = replace(
                plan,
                status=AutomaticSelectionPlanStatus.ACTIVE,
                updated_at=datetime.utcnow(),
                revision=plan.revision + 1,
            )
            self._update_plan_cas(connection, candidate, expected_revision)
            return self._load_steps(connection, candidate)

    def pause_invalid_step(
        self,
        plan_id: str,
        position: int,
        expected_revision: int,
        reason_code: str,
    ) -> AutomaticSelectionPlanBundle:
        """Invalidate one unsafe step and pause its plan in the same transaction."""
        with self._database.transaction() as connection:
            plan = self._required_plan(connection, plan_id)
            self._require_revision(plan, expected_revision)
            if plan.status is not AutomaticSelectionPlanStatus.ACTIVE:
                raise ValueError("only active plans can be paused for invalid steps")
            cursor = connection.execute(
                """UPDATE automatic_selection_plan_steps
                   SET status='INVALIDATED', invalid_reason_code=?
                   WHERE plan_id=? AND position=? AND status='PLANNED'""",
                (reason_code, plan_id, position),
            )
            if cursor.rowcount != 1:
                raise AutomaticSelectionPlanConflictError("plan step changed concurrently")
            candidate = replace(
                plan,
                status=AutomaticSelectionPlanStatus.PAUSED,
                updated_at=datetime.utcnow(),
                revision=plan.revision + 1,
            )
            self._update_plan_cas(connection, candidate, expected_revision)
            return self._load_steps(connection, candidate)

    def change_status(
        self,
        plan_id: str,
        expected_revision: int,
        target: AutomaticSelectionPlanStatus,
        *,
        reason_code: str | None = None,
    ) -> AutomaticSelectionPlanBundle:
        with self._database.transaction() as connection:
            plan = self._required_plan(connection, plan_id)
            self._require_revision(plan, expected_revision)
            if target not in _TRANSITIONS.get(plan.status, set()):
                raise ValueError(f"invalid plan transition: {plan.status} -> {target}")
            now = datetime.utcnow()
            candidate = replace(
                plan,
                status=target,
                updated_at=now,
                revision=plan.revision + 1,
                invalid_reason_code=reason_code,
            )
            if target in {
                AutomaticSelectionPlanStatus.INVALIDATED,
                AutomaticSelectionPlanStatus.DISCARDED,
            }:
                connection.execute(
                    """UPDATE automatic_selection_plan_steps
                       SET status='INVALIDATED', invalid_reason_code=?
                       WHERE plan_id=? AND status='PLANNED'""",
                    (reason_code, plan_id),
                )
            self._update_plan_cas(connection, candidate, expected_revision)
            return self._load_steps(connection, candidate)

    def invalidate_remaining(
        self,
        plan_id: str,
        expected_revision: int,
        first_position: int,
        reason_code: str,
    ) -> AutomaticSelectionPlanBundle:
        if first_position < 1:
            raise ValueError("first_position must be positive")
        with self._database.transaction() as connection:
            plan = self._required_plan(connection, plan_id)
            self._require_revision(plan, expected_revision)
            connection.execute(
                """UPDATE automatic_selection_plan_steps
                   SET status='INVALIDATED', invalid_reason_code=?
                   WHERE plan_id=? AND position>=? AND status='PLANNED'""",
                (reason_code, plan_id, first_position),
            )
            candidate = replace(plan, updated_at=datetime.utcnow(), revision=plan.revision + 1)
            self._update_plan_cas(connection, candidate, expected_revision)
            return self._load_steps(connection, candidate)

    def materialize_step(
        self, plan_id: str, position: int, expected_revision: int
    ) -> tuple[AutomaticSelectionPlanBundle, QueueEntry]:
        with self._database.transaction() as connection:
            plan = self._required_plan(connection, plan_id)
            row = connection.execute(
                """SELECT status, track_id, executed_queue_entry_id
                   FROM automatic_selection_plan_steps WHERE plan_id=? AND position=?""",
                (plan_id, position),
            ).fetchone()
            if row is None:
                raise AutomaticSelectionPlanNotFoundError("plan step not found")
            if str(row["status"]) == AutomaticSelectionPlanStepStatus.QUEUED:
                entry = self._party_repository.get_queue_entry(int(row["executed_queue_entry_id"]))
                if entry is None:
                    raise RuntimeError("linked queue entry is missing")
                return self._load_steps(connection, plan), entry
            self._require_revision(plan, expected_revision)
            if plan.status is not AutomaticSelectionPlanStatus.ACTIVE:
                raise ValueError("only active plans can materialize steps")
            if str(row["status"]) != AutomaticSelectionPlanStepStatus.PLANNED:
                raise ValueError("only planned steps can be materialized")
            entry = self._party_repository.add_queue_entry(
                plan.session_id, int(row["track_id"]), QueueSource.AUTOMATIC
            )
            cursor = connection.execute(
                """UPDATE automatic_selection_plan_steps
                   SET status='QUEUED', executed_queue_entry_id=?
                   WHERE plan_id=? AND position=? AND status='PLANNED'""",
                (entry.queue_id, plan_id, position),
            )
            if cursor.rowcount != 1:
                raise AutomaticSelectionPlanConflictError("plan step changed concurrently")
            candidate = replace(plan, updated_at=datetime.utcnow(), revision=plan.revision + 1)
            self._update_plan_cas(connection, candidate, expected_revision)
            return self._load_steps(connection, candidate), entry

    @staticmethod
    def _insert_plan(connection: sqlite3.Connection, plan: AutomaticSelectionPlan) -> None:
        cursor = connection.execute(
            """INSERT INTO automatic_selection_plans
               (plan_id, session_id, status, created_at, updated_at, planning_seed,
                rule_configuration_version, rule_configuration_digest,
                rationale_schema_version, source_context_code, planned_depth, revision,
                invalid_reason_code)
               SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
               WHERE EXISTS (
                   SELECT 1 FROM party_sessions
                   WHERE id=? AND status IN ('active','paused','recovered')
               )""",
            (
                plan.plan_id,
                plan.session_id,
                plan.status,
                plan.created_at.isoformat(),
                plan.updated_at.isoformat(),
                plan.planning_seed,
                plan.rule_configuration_version,
                plan.rule_configuration_digest,
                plan.rationale_schema_version,
                plan.source_context_code,
                plan.planned_depth,
                plan.revision,
                plan.invalid_reason_code,
                plan.session_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("session is missing or not eligible for planning")

    @staticmethod
    def _step_values(step: AutomaticSelectionPlanStep) -> tuple[object, ...]:
        return (
            step.step_id,
            step.plan_id,
            step.position,
            step.track_id,
            step.status,
            step.planned_at.isoformat(),
            step.previous_track_id,
            step.relaxation_stage_code,
            step.primary_play_count,
            step.secondary_score,
            step.tie_candidate_count,
            step.tie_break_method_code,
            step.selection_reason_code,
            step.executed_queue_entry_id,
            step.invalid_reason_code,
        )

    def _required_plan(
        self, connection: sqlite3.Connection, plan_id: str
    ) -> AutomaticSelectionPlan:
        row = connection.execute(
            "SELECT * FROM automatic_selection_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise AutomaticSelectionPlanNotFoundError("plan not found")
        return self._plan_from_row(row)

    def _load_bundle(
        self, connection: sqlite3.Connection, plan_id: str
    ) -> AutomaticSelectionPlanBundle | None:
        row = connection.execute(
            "SELECT * FROM automatic_selection_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        return None if row is None else self._load_steps(connection, self._plan_from_row(row))

    def _load_steps(
        self, connection: sqlite3.Connection, plan: AutomaticSelectionPlan
    ) -> AutomaticSelectionPlanBundle:
        rows = connection.execute(
            """SELECT s.*, q.status AS executed_queue_status
               FROM automatic_selection_plan_steps s
               LEFT JOIN party_queue q ON q.id=s.executed_queue_entry_id
               WHERE s.plan_id=? ORDER BY s.position""",
            (plan.plan_id,),
        ).fetchall()
        return AutomaticSelectionPlanBundle(plan, tuple(self._step_from_row(row) for row in rows))

    @staticmethod
    def _plan_from_row(row: sqlite3.Row) -> AutomaticSelectionPlan:
        return AutomaticSelectionPlan(
            plan_id=str(row["plan_id"]),
            session_id=int(row["session_id"]),
            status=AutomaticSelectionPlanStatus(str(row["status"])),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            planning_seed=(int(row["planning_seed"]) if row["planning_seed"] is not None else None),
            rule_configuration_version=int(row["rule_configuration_version"]),
            rule_configuration_digest=str(row["rule_configuration_digest"]),
            rationale_schema_version=int(row["rationale_schema_version"]),
            source_context_code=str(row["source_context_code"]),
            planned_depth=int(row["planned_depth"]),
            revision=int(row["revision"]),
            invalid_reason_code=(
                str(row["invalid_reason_code"]) if row["invalid_reason_code"] else None
            ),
        )

    @staticmethod
    def _step_from_row(row: sqlite3.Row) -> AutomaticSelectionPlanStep:
        return AutomaticSelectionPlanStep(
            step_id=str(row["step_id"]),
            plan_id=str(row["plan_id"]),
            position=int(row["position"]),
            track_id=int(row["track_id"]),
            status=AutomaticSelectionPlanStepStatus(str(row["status"])),
            planned_at=datetime.fromisoformat(str(row["planned_at"])),
            previous_track_id=(
                int(row["previous_track_id"]) if row["previous_track_id"] is not None else None
            ),
            relaxation_stage_code=str(row["relaxation_stage_code"]),
            primary_play_count=(
                int(row["primary_play_count"]) if row["primary_play_count"] is not None else None
            ),
            secondary_score=float(row["secondary_score"]),
            tie_candidate_count=int(row["tie_candidate_count"]),
            tie_break_method_code=str(row["tie_break_method_code"]),
            selection_reason_code=str(row["selection_reason_code"]),
            executed_queue_entry_id=(
                int(row["executed_queue_entry_id"])
                if row["executed_queue_entry_id"] is not None
                else None
            ),
            executed_queue_status=(
                QueueStatus(str(row["executed_queue_status"]))
                if row["executed_queue_status"] is not None
                else None
            ),
            invalid_reason_code=(
                str(row["invalid_reason_code"]) if row["invalid_reason_code"] else None
            ),
        )

    @staticmethod
    def _require_revision(plan: AutomaticSelectionPlan, expected_revision: int) -> None:
        if plan.revision != expected_revision:
            raise AutomaticSelectionPlanConflictError("plan revision changed concurrently")

    @staticmethod
    def _update_plan_cas(
        connection: sqlite3.Connection,
        plan: AutomaticSelectionPlan,
        expected_revision: int,
    ) -> None:
        cursor = connection.execute(
            """UPDATE automatic_selection_plans
               SET status=?, updated_at=?, revision=?, invalid_reason_code=?
               WHERE plan_id=? AND revision=?""",
            (
                plan.status,
                plan.updated_at.isoformat(),
                plan.revision,
                plan.invalid_reason_code,
                plan.plan_id,
                expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise AutomaticSelectionPlanConflictError("plan revision changed concurrently")
