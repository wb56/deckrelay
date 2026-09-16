"""Atomic persistence for automatic-selection plans and their queue linkage."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import sqlite3

from party_player.automatic_selection_plan import (
    AutomaticSelectionPlan,
    AutomaticSelectionPlanBundle,
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
            if session is None or str(session["status"]) not in {"active", "paused"}:
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
                   WHERE id=? AND status IN ('active','paused')
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
