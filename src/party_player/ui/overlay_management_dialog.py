"""Single-instance non-modal editor for jingles and effects."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog
from typing import Any

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.overlay import OverlayDefinition, OverlayRecord
from party_player.overlay_service import OverlayService
from party_player.ui import theme
from party_player.ui.dialogs import ask_silent_yes_no, ask_silent_yes_no_cancel
from party_player.ui.overlay_presentation import (
    advanced_cue_visible,
    ducking_switch_text,
    favorite_shortcut_text,
    format_cue_time,
    parse_cue_time,
)
from party_player.ui.responsive_dialog import apply_responsive_dialog_geometry, bind_dialog_escape


class OverlayManagementDialog(ctk.CTkToplevel):  # type: ignore[misc]
    """Edit overlay records while playback continues in the main window."""

    PAGE_SIZE = 25

    def __init__(
        self,
        master: Any,
        service: OverlayService,
        *,
        on_changed: Callable[[], None],
        on_preview: Callable[[OverlayRecord], None],
        active_overlay_id: Callable[[], int | None],
    ) -> None:
        super().__init__(master)
        self._service = service
        self._on_changed = on_changed
        self._on_preview = on_preview
        self._active_overlay_id = active_overlay_id
        self._records: tuple[OverlayRecord, ...] = ()
        self._selected: OverlayRecord | None = None
        self._page = 0
        self._dirty = False
        self._loading = False
        self._pending_favorite_position: int | None = None
        self.title("Jingles und Effekte verwalten")
        apply_responsive_dialog_geometry(
            self, master, preferred_size=(1050, 680), minimum_size=(720, 480)
        )
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._request_close)
        bind_dialog_escape(self, self._request_close)
        self.grid_columnconfigure(0, weight=2)
        self.grid_columnconfigure(1, weight=3)
        self.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(self)
        left.grid(row=0, column=0, padx=(14, 7), pady=14, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(left, text="OVERLAYS", font=(theme.FONT_FAMILY, 15, "bold")).grid(
            row=0, column=0, padx=12, pady=(12, 6), sticky="w"
        )
        self._search = ctk.CTkEntry(left, placeholder_text="Name oder Kategorie filtern")
        self._search.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="ew")
        self._search.bind("<KeyRelease>", lambda _event: self._refresh(reset_page=True))
        self._list = ctk.CTkScrollableFrame(left)
        self._list.grid(row=2, column=0, padx=8, pady=4, sticky="nsew")
        self._list.grid_columnconfigure(0, weight=1)
        navigation = ctk.CTkFrame(left, fg_color="transparent")
        navigation.grid(row=3, column=0, padx=12, pady=8, sticky="ew")
        self._previous = ctk.CTkButton(
            navigation, text="←", width=45, command=lambda: self._change_page(-1)
        )
        self._previous.pack(side="left")
        self._page_label = ctk.CTkLabel(navigation, text="Seite 1")
        self._page_label.pack(side="left", fill="x", expand=True)
        self._next = ctk.CTkButton(
            navigation, text="→", width=45, command=lambda: self._change_page(1)
        )
        self._next.pack(side="right")
        ctk.CTkButton(left, text="+ Neues Overlay", command=self._new).grid(
            row=4, column=0, padx=12, pady=(0, 12), sticky="ew"
        )

        form = ctk.CTkScrollableFrame(self, label_text="Einstellungen")
        form.grid(row=0, column=1, padx=(7, 14), pady=14, sticky="nsew")
        form.grid_columnconfigure(1, weight=1)
        self._fields: dict[str, ctk.CTkEntry] = {}
        self._advanced_widgets: list[Any] = []
        self._advanced_visible = False
        row = 0
        for key, label, unit in (
            ("name", "Name", ""),
            ("file_path", "Datei", ""),
            ("category", "Kategorie", ""),
            ("volume", "Lautstärke", "%"),
            ("fade_in", "Fade-in", "ms"),
            ("fade_out", "Fade-out", "ms"),
            ("cue_in", "Cue-In", "mm:ss"),
            ("cue_out", "Cue-Out", "mm:ss"),
        ):
            field_widgets: list[Any] = []
            field_label = ctk.CTkLabel(form, text=label)
            field_label.grid(row=row, column=0, padx=(12, 8), pady=5, sticky="w")
            field_widgets.append(field_label)
            entry = ctk.CTkEntry(form)
            entry.grid(row=row, column=1, padx=4, pady=5, sticky="ew")
            entry.bind("<KeyRelease>", self._mark_dirty)
            self._fields[key] = entry
            field_widgets.append(entry)
            if unit:
                unit_label = ctk.CTkLabel(form, text=unit, width=36)
                unit_label.grid(row=row, column=2, padx=(4, 12), pady=5)
                field_widgets.append(unit_label)
            elif key == "file_path":
                choose_button = ctk.CTkButton(form, text="…", width=36, command=self._choose_file)
                choose_button.grid(row=row, column=2, padx=(4, 12), pady=5)
                field_widgets.append(choose_button)
            if key in {"cue_in", "cue_out"}:
                self._advanced_widgets.extend(field_widgets)
                for widget in field_widgets:
                    widget.grid_remove()
            row += 1

        self._advanced_button = ctk.CTkButton(
            form,
            text="Weitere Einstellungen ▸",
            fg_color="transparent",
            anchor="w",
            command=self._toggle_advanced,
        )
        self._advanced_button.grid(
            row=row, column=0, columnspan=3, padx=12, pady=(4, 2), sticky="ew"
        )
        row += 1
        self._enabled = ctk.CTkSwitch(form, text="Aktiv", command=self._mark_dirty)
        self._enabled.grid(row=row, column=0, padx=12, pady=8, sticky="w")
        self._ducking = ctk.CTkSwitch(
            form,
            text=ducking_switch_text(True),
            command=self._ducking_changed,
        )
        self._ducking.grid(row=row, column=1, padx=4, pady=8, sticky="w")
        row += 1
        self._ducking_frame = ctk.CTkFrame(form, fg_color="transparent")
        self._ducking_frame.grid(row=row, column=0, columnspan=3, sticky="ew")
        self._ducking_frame.grid_columnconfigure(1, weight=1)
        for inner_row, (key, label, unit) in enumerate(
            (
                ("ducking_db", "Absenkung", "dB"),
                ("attack", "Attack", "ms"),
                ("release", "Release", "ms"),
            )
        ):
            ctk.CTkLabel(self._ducking_frame, text=label).grid(
                row=inner_row, column=0, padx=(12, 8), pady=5, sticky="w"
            )
            entry = ctk.CTkEntry(self._ducking_frame)
            entry.grid(row=inner_row, column=1, padx=4, pady=5, sticky="ew")
            entry.bind("<KeyRelease>", self._mark_dirty)
            self._fields[key] = entry
            ctk.CTkLabel(self._ducking_frame, text=unit, width=36).grid(
                row=inner_row, column=2, padx=(4, 12), pady=5
            )
        row += 1
        ctk.CTkLabel(form, text="Favorit").grid(row=row, column=0, padx=(12, 8), pady=5, sticky="w")
        self._favorite = ctk.CTkOptionMenu(
            form,
            values=["Keiner", "1", "2", "3", "4", "5", "6"],
            command=self._favorite_changed,
        )
        self._favorite.grid(row=row, column=1, padx=4, pady=5, sticky="ew")
        row += 1
        ctk.CTkLabel(form, text="Shortcut").grid(
            row=row, column=0, padx=(12, 8), pady=5, sticky="w"
        )
        self._shortcut = ctk.CTkLabel(
            form,
            text="—",
            text_color=theme.TEXT_MUTED,
            anchor="w",
        )
        self._shortcut.grid(row=row, column=1, padx=4, pady=5, sticky="ew")
        row += 1
        self._error = ctk.CTkLabel(
            form, text="", text_color=theme.ERROR, wraplength=520, anchor="w"
        )
        self._error.grid(row=row, column=0, columnspan=3, padx=12, pady=8, sticky="ew")
        row += 1
        self._warning = ctk.CTkLabel(
            form, text="", text_color=theme.WARNING, wraplength=520, anchor="w"
        )
        self._warning.grid(row=row, column=0, columnspan=3, padx=12, pady=(0, 8), sticky="ew")
        row += 1
        actions = ctk.CTkFrame(form, fg_color="transparent")
        actions.grid(row=row, column=0, columnspan=3, padx=12, pady=12, sticky="ew")
        ctk.CTkButton(actions, text="▶ Vorhören", command=self._preview).pack(
            side="left", padx=(0, 6)
        )
        ctk.CTkButton(actions, text="Speichern", command=self._save).pack(side="left", padx=6)
        ctk.CTkButton(actions, text="Verwerfen", command=self._discard).pack(side="left", padx=6)
        self._secondary_actions = ctk.CTkOptionMenu(
            actions,
            values=["Weitere Aktionen…", "Deaktivieren", "Entfernen"],
            command=self._secondary_action,
            width=165,
        )
        self._secondary_actions.pack(side="right")
        self._refresh(reset_page=True)
        # Start with a complete, valid definition.  Previously the form was
        # empty until an existing overlay was selected or "+ Neues Overlay"
        # was clicked, although the hidden cue and ducking fields are required
        # by the form parser as well.
        self._new()

    def focus_existing(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def focus_record(self, overlay_id: int) -> None:
        """Open the requested existing record in this single dialog instance."""

        self.focus_existing()
        record = next(
            (
                item
                for item in self._service.snapshot(enabled_only=False).records
                if item.definition.overlay_id == overlay_id
            ),
            None,
        )
        if record is not None:
            self._select(record)

    def assign_favorite(self, position: int) -> None:
        """Wait for an existing list selection and assign it to one soundboard pad."""

        self.focus_existing()
        if not self._resolve_dirty():
            return
        self._pending_favorite_position = position
        self._error.configure(
            text=(
                f"Links ein vorhandenes Overlay für Favoritenplatz {position} auswählen. "
                "Danach Speichern klicken."
            )
        )
        self._search.focus_set()

    def _refresh(self, *, reset_page: bool = False) -> None:
        snapshot = self._service.snapshot(enabled_only=False)
        query = self._search.get().strip().casefold()
        self._records = tuple(
            record
            for record in snapshot.records
            if not query
            or query in record.definition.name.casefold()
            or query in record.definition.category.casefold()
        )
        if reset_page:
            self._page = 0
        maximum_page = max(0, (len(self._records) - 1) // self.PAGE_SIZE)
        self._page = min(self._page, maximum_page)
        for child in self._list.winfo_children():
            child.destroy()
        start = self._page * self.PAGE_SIZE
        for record in self._records[start : start + self.PAGE_SIZE]:
            warning = " ⚠" if not Path(record.definition.file_path).is_file() else ""
            favorite = f" · F{record.favorite_position}" if record.favorite_position else ""
            state = "" if record.enabled else " · aus"
            ctk.CTkButton(
                self._list,
                text=f"{record.definition.name}{favorite}{state}{warning}",
                anchor="w",
                fg_color="transparent",
                command=lambda item=record: self._select(item),
            ).pack(fill="x", pady=2)
        self._page_label.configure(text=f"Seite {self._page + 1} / {maximum_page + 1}")
        self._previous.configure(state="normal" if self._page else "disabled")
        self._next.configure(state="normal" if self._page < maximum_page else "disabled")

    def _select(self, record: OverlayRecord) -> None:
        if not self._resolve_dirty():
            return
        self._selected = record
        self._load(record)
        if self._pending_favorite_position is not None:
            position = self._pending_favorite_position
            self._pending_favorite_position = None
            self._favorite.set(str(position))
            self._mark_dirty()
            self._error.configure(
                text=f"Favoritenplatz {position} ist vorbereitet – jetzt Speichern klicken."
            )

    def _new(self) -> None:
        if not self._resolve_dirty():
            return
        self._pending_favorite_position = None
        self._selected = None
        self._load(OverlayRecord(OverlayDefinition(0, "", "")))
        self._fields["name"].focus_set()

    def _load(self, record: OverlayRecord) -> None:
        self._loading = True
        definition = record.definition
        values = {
            "name": definition.name,
            "file_path": definition.file_path,
            "category": definition.category,
            "volume": str(definition.volume_percent),
            "fade_in": str(definition.fade_in_ms),
            "fade_out": str(definition.fade_out_ms),
            "cue_in": format_cue_time(definition.cue_in_ms),
            "cue_out": format_cue_time(definition.cue_out_ms),
            "ducking_db": str(definition.ducking_db),
            "attack": str(definition.ducking_attack_ms),
            "release": str(definition.ducking_release_ms),
        }
        for key, value in values.items():
            entry = self._fields[key]
            entry.delete(0, "end")
            entry.insert(0, value)
        self._enabled.select() if record.enabled else self._enabled.deselect()
        self._ducking.select() if definition.ducking_enabled else self._ducking.deselect()
        self._ducking.configure(text=ducking_switch_text(definition.ducking_enabled))
        self._favorite.set(
            str(record.favorite_position) if record.favorite_position is not None else "Keiner"
        )
        self._update_shortcut()
        (
            self._ducking_frame.grid()
            if definition.ducking_enabled
            else self._ducking_frame.grid_remove()
        )
        self._set_advanced_visible(
            advanced_cue_visible(definition.cue_in_ms, definition.cue_out_ms)
        )
        self._error.configure(text="")
        self._warning.configure(text=OverlayService.safety_warning(record))
        self._dirty = False
        self._loading = False

    def _record_from_form(self) -> OverlayRecord:
        favorite_text = self._favorite.get()
        favorite = int(favorite_text) if favorite_text != "Keiner" else None
        try:
            cue_in_ms = parse_cue_time(self._fields["cue_in"].get())
        except ValueError as exc:
            raise ValueError(f"Cue-In: {exc}") from exc
        try:
            cue_out_ms = parse_cue_time(self._fields["cue_out"].get(), optional=True)
        except ValueError as exc:
            raise ValueError(f"Cue-Out: {exc}") from exc
        assert cue_in_ms is not None
        volume = self._parse_int_field("volume", "Lautstärke")
        fade_in = self._parse_int_field("fade_in", "Fade-in")
        fade_out = self._parse_int_field("fade_out", "Fade-out")
        ducking_db = self._parse_float_field("ducking_db", "Ducking")
        attack = self._parse_int_field("attack", "Attack")
        release = self._parse_int_field("release", "Release")
        definition = OverlayDefinition(
            self._selected.definition.overlay_id if self._selected is not None else 0,
            self._fields["name"].get(),
            self._fields["file_path"].get(),
            self._fields["category"].get(),
            volume,
            fade_in,
            fade_out,
            cue_in_ms,
            cue_out_ms,
            bool(self._ducking.get()),
            ducking_db,
            attack,
            release,
        )
        return OverlayRecord(
            definition,
            bool(self._enabled.get()),
            favorite,
            f"Ctrl+{favorite}" if favorite is not None else None,
        )

    def _parse_int_field(self, key: str, label: str) -> int:
        value = self._fields[key].get().strip()
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"{label}: Bitte eine ganze Zahl eingeben") from exc

    def _parse_float_field(self, key: str, label: str) -> float:
        value = self._fields[key].get().strip().replace(",", ".")
        try:
            return float(value)
        except ValueError as exc:
            raise ValueError(f"{label}: Bitte eine Zahl eingeben") from exc

    def _save(self) -> bool:
        self._clear_field_errors()
        try:
            saved = self._service.save(self._record_from_form())
        except (ValueError, KeyError) as exc:
            self._highlight_error_field(str(exc))
            self._error.configure(text=str(exc))
            return False
        self._selected = saved
        self._dirty = False
        self._error.configure(text="")
        self._warning.configure(text=OverlayService.safety_warning(saved))
        self._refresh()
        self._on_changed()
        return True

    def _preview(self) -> None:
        self._clear_field_errors()
        try:
            record = self._record_from_form()
            self._service.validate(record)
        except ValueError as exc:
            self._highlight_error_field(str(exc))
            self._error.configure(text=str(exc))
            return
        self._on_preview(record)

    def _discard(self) -> None:
        if self._selected is not None:
            self._load(self._selected)
        else:
            self._new()

    def _secondary_action(self, action: str) -> None:
        self.after_idle(lambda: self._secondary_actions.set("Weitere Aktionen…"))
        if action == "Deaktivieren":
            self._deactivate()
        elif action == "Entfernen":
            self._delete()

    def _deactivate(self) -> None:
        if self._selected is None or not self._resolve_dirty():
            return
        try:
            self._selected = self._service.set_enabled(
                self._selected.definition.overlay_id,
                False,
            )
        except KeyError as exc:
            self._error.configure(text=str(exc))
            return
        self._load(self._selected)
        self._refresh()
        self._on_changed()

    def _delete(self) -> None:
        if self._selected is None:
            return
        if not ask_silent_yes_no(
            self,
            "Overlay entfernen?",
            f"„{self._selected.definition.name}“ wirklich entfernen?",
        ):
            return
        try:
            self._service.delete(
                self._selected.definition.overlay_id,
                active_overlay_id=self._active_overlay_id(),
            )
        except ValueError as exc:
            self._error.configure(text=str(exc))
            return
        self._selected = None
        self._dirty = False
        self._refresh()
        self._on_changed()

    def _choose_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Jingle oder Effekt auswählen",
            filetypes=(("MP3 und FLAC", "*.mp3 *.flac"), ("Alle Dateien", "*.*")),
        )
        if path:
            entry = self._fields["file_path"]
            entry.delete(0, "end")
            entry.insert(0, path)
            self._mark_dirty()

    def _ducking_changed(self) -> None:
        enabled = bool(self._ducking.get())
        self._ducking.configure(text=ducking_switch_text(enabled))
        if enabled:
            self._ducking_frame.grid()
        else:
            self._ducking_frame.grid_remove()
        self._mark_dirty()

    def _toggle_advanced(self) -> None:
        self._set_advanced_visible(not self._advanced_visible)

    def _set_advanced_visible(self, visible: bool) -> None:
        self._advanced_visible = visible
        for widget in self._advanced_widgets:
            widget.grid() if visible else widget.grid_remove()
        self._advanced_button.configure(
            text="Weitere Einstellungen ▾" if visible else "Weitere Einstellungen ▸"
        )

    def _favorite_changed(self, _value: str) -> None:
        self._update_shortcut()
        self._mark_dirty()

    def _update_shortcut(self) -> None:
        self._shortcut.configure(text=favorite_shortcut_text(self._favorite.get()))

    def _mark_dirty(self, _event: object | None = None) -> None:
        if not self._loading:
            self._dirty = True
            try:
                warning = OverlayService.safety_warning(self._record_from_form())
            except (ValueError, KeyError):
                warning = ""
            self._warning.configure(text=warning)

    def _clear_field_errors(self) -> None:
        for entry in self._fields.values():
            entry.configure(border_color=theme.BORDER)

    def _highlight_error_field(self, message: str) -> None:
        field = next(
            (
                key
                for marker, key in (
                    ("Name", "name"),
                    ("MP3", "file_path"),
                    ("Lautstärke", "volume"),
                    ("Fade-in", "fade_in"),
                    ("Fade-out", "fade_out"),
                    ("Cue-In", "cue_in"),
                    ("Cue-Out", "cue_out"),
                    ("Zeit", "cue_in"),
                    ("Sekunden", "cue_in"),
                    ("Ducking", "ducking_db"),
                    ("Attack", "attack"),
                    ("Release", "release"),
                    ("Favoritenposition", "favorite"),
                )
                if marker in message
            ),
            None,
        )
        if field in self._fields:
            self._fields[field].configure(border_color=theme.ERROR)

    def _resolve_dirty(self) -> bool:
        if not self._dirty:
            return True
        choice = ask_silent_yes_no_cancel(
            self,
            "Ungespeicherte Änderungen",
            "Änderungen speichern?\n\nJa = Speichern · Nein = Verwerfen",
        )
        if choice is None:
            return False
        if choice:
            return self._save()
        self._dirty = False
        return True

    def _change_page(self, direction: int) -> None:
        self._page = max(0, self._page + direction)
        self._refresh()

    def _request_close(self) -> None:
        if self._resolve_dirty():
            self.withdraw()
