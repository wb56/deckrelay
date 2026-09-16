"""Small operator decision dialog for an interrupted automatic plan."""

from __future__ import annotations

from collections.abc import Callable

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.automatic_selection_plan import (
    AutomaticSelectionPlanRecoveryAction,
    AutomaticSelectionPlanRecoveryState,
)


class AutomaticSelectionPlanRecoveryDialog(ctk.CTkToplevel):  # type: ignore[misc]
    def __init__(
        self,
        parent: ctk.CTk,
        state: AutomaticSelectionPlanRecoveryState,
        *,
        resume: Callable[[], None],
        recalculate: Callable[[], None],
        discard: Callable[[], None],
        leave_paused: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self.title("Automatikplan wiederherstellen")
        self.geometry("620x360")
        self.minsize(420, 260)
        self.resizable(True, True)
        self.transient(parent)
        self._handled = False
        self._action_buttons: list[ctk.CTkButton] = []
        self.protocol("WM_DELETE_WINDOW", lambda: self._leave_paused(leave_paused))
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        body = ctk.CTkScrollableFrame(self)
        body.grid(row=0, column=0, sticky="nsew", padx=16, pady=(16, 8))
        body.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            body,
            text="Ein unterbrochener Automatikplan wurde gefunden.",
            font=ctk.CTkFont(size=19, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 16))
        ctk.CTkLabel(
            body,
            text=f"Verbleibende Schritte: {state.remaining_planned_steps}",
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        ctk.CTkLabel(
            body,
            text=self._reason_text(state),
            anchor="w",
            justify="left",
            wraplength=520,
        ).grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        if state.interrupted_track_id is not None:
            ctk.CTkLabel(
                body,
                text="Der beim Neustart unterbrochene Titel wird nicht automatisch wiederholt.",
                anchor="w",
                justify="left",
                wraplength=520,
            ).grid(row=3, column=0, sticky="ew", padx=8, pady=4)

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 16))
        for column in range(3):
            actions.grid_columnconfigure(column, weight=1)
        for column, (text, _action, command) in enumerate(
            (
                ("Fortsetzen", AutomaticSelectionPlanRecoveryAction.RESUME, resume),
                (
                    "Neu berechnen",
                    AutomaticSelectionPlanRecoveryAction.RECALCULATE,
                    recalculate,
                ),
                ("Verwerfen", AutomaticSelectionPlanRecoveryAction.DISCARD, discard),
            )
        ):
            button = ctk.CTkButton(
                actions,
                text=text,
                command=lambda callback=command: self._choose(callback),
            )
            button.grid(row=0, column=column, sticky="ew", padx=4)
            self._action_buttons.append(button)
        self._decision_status = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            text_color=("#92400E", "#FBBF24"),
        )
        self._decision_status.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 12))
        self.grab_set()

    def _choose(self, callback: Callable[[], None]) -> None:
        if self._handled:
            return
        self._handled = True
        for button in self._action_buttons:
            button.configure(state="disabled")
        self._decision_status.configure(text="Entscheidung wird sicher ausgeführt …")
        callback()

    def finish_decision(self, *, success: bool, message: str = "") -> None:
        if success:
            self.grab_release()
            self.destroy()
            return
        self._handled = False
        for button in self._action_buttons:
            button.configure(state="normal")
        self._decision_status.configure(
            text=message or "Die Entscheidung konnte nicht sicher ausgeführt werden."
        )

    def _leave_paused(self, callback: Callable[[], None]) -> None:
        if self._handled:
            return
        self._handled = True
        self.grab_release()
        self.destroy()
        callback()

    @staticmethod
    def _reason_text(state: AutomaticSelectionPlanRecoveryState) -> str:
        return {
            "INTERRUPTED_PLAYBACK": (
                "Beim letzten Programmlauf wurde ein geplanter Titel bereits abgespielt und "
                "durch den Neustart unterbrochen. Er wird nicht automatisch wiederholt. "
                "Bitte den verbleibenden Ablauf neu berechnen oder verwerfen."
            ),
            "CONFIGURATION_CHANGED": (
                "Die Regeln für die automatische Auswahl wurden seit der Planung geändert. "
                "Der alte Ablauf kann deshalb nicht unverändert fortgesetzt werden."
            ),
            "PREDECESSOR_CHANGED": (
                "Seit der Planung wurde ein anderer Titel tatsächlich abgespielt. Die "
                "gespeicherte Übergangskette passt deshalb nicht mehr zum aktuellen Verlauf."
            ),
            "INCONSISTENT_QUEUE_LINK": (
                "Ein bereits eingeplanter Queue-Eintrag fehlt oder besitzt einen nicht "
                "eindeutigen Zustand. Eine unveränderte Fortsetzung wäre nicht sicher."
            ),
        }.get(
            state.reason_code.value,
            (
                "Der gespeicherte Ablauf passt weiterhin zur aktuellen Session und zu den "
                "Auswahlregeln. Sie können ihn unverändert fortsetzen, mit einem neuen Seed "
                "neu berechnen oder vollständig verwerfen. Bis zu Ihrer Entscheidung bleibt "
                "die Automatik pausiert."
            ),
        )
