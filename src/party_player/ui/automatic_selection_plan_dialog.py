"""Responsive, asynchronous automatic-selection plan overview."""

from __future__ import annotations

from collections.abc import Callable
from tkinter import messagebox
from typing import Any, Protocol

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.automatic_selection_plan import AutomaticSelectionPlanStatus
from party_player.automatic_selection_plan_ui import PlanOverview, PlanStepView
from party_player.ui import theme
from party_player.ui.responsive_dialog import (
    apply_responsive_dialog_geometry,
    bind_dialog_escape,
    release_dialog,
)


class PlanRequester(Protocol):
    def request_automatic_plan_overview(
        self,
        completed: Callable[[PlanOverview | None], None],
        failed: Callable[[str], None],
    ) -> bool: ...

    def request_automatic_plan_action(
        self,
        action: str,
        plan_id: str,
        revision: int,
        completed: Callable[[], None],
        failed: Callable[[str], None],
    ) -> bool: ...


def available_plan_actions(status: AutomaticSelectionPlanStatus) -> tuple[str, ...]:
    return {
        AutomaticSelectionPlanStatus.DRAFT: ("activate", "discard"),
        AutomaticSelectionPlanStatus.ACTIVE: ("recalculate", "discard"),
        AutomaticSelectionPlanStatus.PAUSED: ("resume", "recalculate", "discard"),
    }.get(status, ())


class AutomaticSelectionPlanDialog(ctk.CTkToplevel):  # type: ignore[misc]
    def __init__(self, parent: Any, requester: PlanRequester) -> None:
        super().__init__(parent)
        self._parent = parent
        self._requester = requester
        self._overview: PlanOverview | None = None
        self._running = False
        self._closed = False
        self._generation = 0
        self._step_buttons: list[Any] = []
        self._action_buttons: list[Any] = []
        compact = bool(getattr(parent, "_compact_layout_active", False))
        self.title("Fortlaufende Automatik")
        apply_responsive_dialog_geometry(
            self,
            parent,
            preferred_size=(980, 720) if not compact else (720, 680),
            minimum_size=(720, 500) if not compact else (480, 420),
        )
        self.transient(parent)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            self,
            text="Fortlaufende Automatik",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, padx=20, pady=(18, 4), sticky="w")
        self._header = ctk.CTkLabel(
            self, text="Plan wird geladen …", anchor="w", justify="left", wraplength=900
        )
        self._header.grid(row=1, column=0, padx=20, pady=6, sticky="ew")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, padx=20, pady=6, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)
        if compact:
            body.grid_rowconfigure(1, weight=1)
            list_grid = dict(row=0, column=0, sticky="nsew", pady=(0, 5))
            detail_grid = dict(row=1, column=0, sticky="nsew", pady=(5, 0))
        else:
            body.grid_columnconfigure(1, weight=1)
            list_grid = dict(row=0, column=0, sticky="nsew", padx=(0, 5))
            detail_grid = dict(row=0, column=1, sticky="nsew", padx=(5, 0))
        self._list = ctk.CTkScrollableFrame(body, label_text="Geplante Schritte")
        self._list.grid(**list_grid)
        self._list.grid_columnconfigure(0, weight=1)
        detail = ctk.CTkScrollableFrame(body, label_text="Schrittdetails")
        detail.grid(**detail_grid)
        detail.grid_columnconfigure(0, weight=1)
        self._detail = ctk.CTkLabel(
            detail,
            text="Einen Planschritt auswählen.",
            anchor="nw",
            justify="left",
            wraplength=430 if compact else 400,
        )
        self._detail.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

        self._status = ctk.CTkLabel(self, text="", anchor="w", wraplength=900)
        self._status.grid(row=3, column=0, padx=20, pady=(2, 4), sticky="ew")
        self._actions = ctk.CTkFrame(self, fg_color="transparent")
        self._actions.grid(row=4, column=0, padx=20, pady=(4, 18), sticky="ew")
        self._actions.grid_columnconfigure(4, weight=1)
        ctk.CTkButton(self._actions, text="Schließen", command=self._close).grid(
            row=0, column=5, padx=(8, 0), sticky="e"
        )
        self.protocol("WM_DELETE_WINDOW", self._close)
        bind_dialog_escape(self, self._close)
        self._load()

    def _load(self) -> None:
        if self._closed or self._running:
            return
        self._running = True
        self._generation += 1
        generation = self._generation
        self._set_actions_enabled(False)
        self._status.configure(text="Plan wird geladen …", text_color=theme.TEXT_MUTED)
        started = self._requester.request_automatic_plan_overview(
            lambda overview: self._accept_overview(generation, overview),
            lambda message: self._accept_error(generation, message),
        )
        if not started:
            self._accept_error(generation, "Der Ladevorgang konnte nicht gestartet werden.")

    def _accept_overview(self, generation: int, overview: PlanOverview | None) -> None:
        if self._closed or generation != self._generation:
            return
        self._running = False
        self._overview = overview
        self._clear_steps()
        if overview is None:
            self._header.configure(text="Kein Automatikplan für die aktuelle Session vorhanden.")
            self._detail.configure(
                text="Eine Vorschau kann als neuer Automatikplan übernommen werden."
            )
            self._status.configure(text="Kein aktiver Plan", text_color=theme.TEXT_MUTED)
            self._render_actions(())
            self._publish_summary("Kein aktiver Plan")
            return
        self._header.configure(
            text=(
                f"Status: {overview.status_text} · Session {overview.session_id}\n"
                f"Erstellt: {overview.created_at:%d.%m.%Y %H:%M} · "
                f"{overview.total} Titel · {overview.open_count} offen · "
                f"{overview.queued_count} in Queue · {overview.played_count} gespielt · "
                f"{overview.terminal_count} beendet"
                + (f"\nHinweis: {overview.reason}" if overview.reason else "")
                + "\nQueue, manuelle Titel, Gastwünsche und Notfallaktionen behalten Vorrang."
            )
        )
        for row, step in enumerate(overview.steps):
            button = ctk.CTkButton(
                self._list,
                text=(
                    f"{step.position}. {step.artist or 'Unbekannter Interpret'} – {step.title}\n"
                    f"{step.plan_status} · {step.execution_status} · {step.reason}"
                ),
                anchor="w",
                command=lambda item=step: self._show_step(item),
            )
            button.grid(row=row, column=0, padx=6, pady=4, sticky="ew")
            self._step_buttons.append(button)
        if overview.steps:
            self._show_step(overview.steps[0])
        self._status.configure(text="Planübersicht ist aktuell.", text_color=theme.TEXT_MUTED)
        self._render_actions(available_plan_actions(overview.status))
        self._publish_summary(
            f"Plan {overview.status_text.lower()} · {overview.open_count} von {overview.total} offen"
        )

    def _show_step(self, step: PlanStepView) -> None:
        primary = (
            str(step.primary_play_count)
            if step.primary_play_count is not None
            else "Abspielregel nicht aktiv"
        )
        self._detail.configure(
            text=(
                f"Titel: {step.artist or 'Unbekannter Interpret'} – {step.title}\n"
                f"Planstatus: {step.plan_status}\nAusführung: {step.execution_status}\n"
                f"Primäre Abspielzahl: {primary}\nSekundärscore: {step.secondary_score:+g}\n"
                f"Auswahlgrund: {step.reason}\nVorgänger: {step.previous_title}\n"
                f"Lockerungsstufe: {step.relaxation}\nTie-Break: {step.tie_text}"
            )
        )

    def _render_actions(self, actions: tuple[str, ...]) -> None:
        for button in self._action_buttons:
            button.destroy()
        self._action_buttons.clear()
        labels = {
            "activate": "Fortlaufende Automatik starten",
            "resume": "Fortsetzen",
            "recalculate": "Neu berechnen",
            "discard": "Verwerfen",
        }
        for column, action in enumerate(actions):
            button = ctk.CTkButton(
                self._actions,
                text=labels[action],
                command=lambda value=action: self._confirm_action(value),
            )
            button.grid(row=0, column=column, padx=(0, 6), pady=3)
            self._action_buttons.append(button)

    def _confirm_action(self, action: str) -> None:
        overview = self._overview
        if self._running or overview is None:
            return
        if action in {"discard", "recalculate"} and not messagebox.askyesno(
            "Automatikplan ändern",
            (
                "Der verbleibende Automatikplan wird verändert. Bereits gespielte Titel und "
                "die Historie bleiben erhalten; manuelle und andere Queue-Einträge bleiben "
                "unverändert. Fortfahren?"
            ),
            parent=self,
        ):
            return
        self._running = True
        self._set_actions_enabled(False)
        self._status.configure(text="Wird verarbeitet …", text_color=theme.TEXT_MUTED)
        started = self._requester.request_automatic_plan_action(
            action,
            overview.plan_id,
            overview.revision,
            self._action_complete,
            self._action_failed,
        )
        if not started:
            self._action_failed("Die Aktion konnte nicht gestartet werden.")

    def _action_complete(self) -> None:
        self._running = False
        self._load()

    def _action_failed(self, message: str) -> None:
        if self._closed:
            return
        self._running = False
        self._status.configure(text=message, text_color=theme.ERROR)
        self._set_actions_enabled(True)

    def _accept_error(self, generation: int, message: str) -> None:
        if self._closed or generation != self._generation:
            return
        self._action_failed(message)

    def _set_actions_enabled(self, enabled: bool) -> None:
        for button in self._action_buttons:
            button.configure(state="normal" if enabled else "disabled")

    def _clear_steps(self) -> None:
        for button in self._step_buttons:
            button.destroy()
        self._step_buttons.clear()

    def _publish_summary(self, text: str) -> None:
        callback = getattr(self._parent, "show_automatic_plan_status", None)
        if callable(callback):
            callback(text)

    def _close(self) -> None:
        self._closed = True
        self._generation += 1
        release_dialog(self)
        self.destroy()
