from dataclasses import replace
from typing import Any

from party_player.enums import QueueStatus
from party_player.models import QueueEntry, Track
from party_player.performance_monitor import PerformanceMonitor
from party_player.ui import queue_row
from party_player.ui import theme
from party_player.ui.queue_row import QueueEntryViewModel, QueueRowView


class FakeWidget:
    created = 0
    configured = 0

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        type(self).created += 1
        self.options: dict[str, object] = dict(_kwargs)

    def configure(self, **_kwargs: object) -> None:
        type(self).configured += 1
        self.options.update(_kwargs)

    def pack(self, **_kwargs: object) -> None:
        pass

    def pack_forget(self) -> None:
        pass

    def bind(self, event: str, callback: object) -> None:
        self.options[f"bind:{event}"] = callback

    def destroy(self) -> None:
        pass

    def winfo_pointerxy(self) -> tuple[int, int]:
        return (21, 43)


class FakeTooltip:
    created = 0
    cancelled = 0
    closed = 0

    def __init__(self, _widget: object, _text: str) -> None:
        type(self).created += 1
        self.text = _text

    def cancel(self) -> None:
        type(self).cancelled += 1

    def close(self) -> None:
        type(self).closed += 1

    def set_text(self, _text: str) -> None:
        self.text = _text


def callbacks() -> dict[str, Any]:
    return {
        name: lambda _queue_id: None
        for name in (
            "cue",
            "deck_a",
            "deck_b",
            "up",
            "down",
            "top",
            "end",
            "priority",
            "lock",
            "equalizer",
            "equalizer_remove",
            "played",
            "skip",
            "retry",
            "override_skip",
            "reset",
            "remove",
            "remove_prepared",
            "move_prepared_up",
            "move_prepared_down",
        )
    }


def model(queue_id: int, status: QueueStatus) -> QueueEntryViewModel:
    track = Track(queue_id, f"{queue_id}.mp3", f"Song {queue_id}", "Artist", "", 90.0)
    return QueueEntryViewModel(QueueEntry(queue_id, queue_id, queue_id, status), track)


def test_queue_rebinding_reuses_compact_widgets_and_tooltips(monkeypatch) -> None:
    FakeWidget.created = 0
    FakeTooltip.created = 0
    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    row = QueueRowView(object(), callbacks(), PerformanceMonitor())
    widgets = FakeWidget.created
    tooltips = FakeTooltip.created

    row.bind_entry(model(1, QueueStatus.WAITING))
    row.bind_entry(model(2, QueueStatus.PLAYING))

    assert FakeWidget.created == widgets
    assert FakeTooltip.created == tooltips == 4


def test_reused_commands_and_selection_target_current_entry(monkeypatch) -> None:
    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    invoked: list[tuple[str, int]] = []
    row_callbacks = callbacks()
    row_callbacks["deck_a"] = lambda queue_id: invoked.append(("deck_a", queue_id))
    row_callbacks["select"] = lambda queue_id: invoked.append(("select", queue_id))
    row = QueueRowView(object(), row_callbacks, PerformanceMonitor())
    deck_command = row._buttons["deck_a"].options["command"]
    select_command = row._frame.options["bind:<Button-1>"]

    row.bind_entry(model(1, QueueStatus.WAITING))
    row.bind_entry(model(2, QueueStatus.PLAYING))
    deck_command()
    select_command(object())

    assert invoked == [("select", 2)]
    assert row._buttons["deck_a"].options["state"] == "disabled"
    assert row._buttons["deck_b"].options["state"] == "disabled"


def test_identical_queue_rebind_performs_no_widget_configuration(monkeypatch) -> None:
    FakeWidget.created = 0
    FakeWidget.configured = 0
    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    performance = PerformanceMonitor()
    row = QueueRowView(object(), callbacks(), performance)
    entry = model(1, QueueStatus.WAITING)
    row.bind_entry(entry)
    configured = FakeWidget.configured

    assert not row.bind_entry(entry)
    assert FakeWidget.configured == configured
    assert performance.statistics()["queue_row_noop_bind_total"].total_duration_ms == 1


def test_queue_snapshot_has_visible_cue_marker_and_explanatory_tooltip(monkeypatch) -> None:
    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    row = QueueRowView(object(), callbacks(), PerformanceMonitor())
    track = Track(1, "one.mp3", "One", "Artist", "", 120.0)
    entry = QueueEntry(
        1,
        1,
        1,
        QueueStatus.WAITING,
        cue_in_override=2.0,
        cue_out_override=110.0,
        fade_duration_override=6.0,
        cue_override_source="snapshot",
    )

    row.bind_entry(QueueEntryViewModel(entry, track))

    assert row._buttons["cue"].options["text"] == "C●"
    assert row._buttons["cue"].options["fg_color"] == "#2f7d4f"
    assert row._tooltips["cue"].text == ("Eigene Veranstaltungswerte – zum Bearbeiten öffnen")


def test_selection_updates_only_row_style(monkeypatch) -> None:
    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    row = QueueRowView(object(), callbacks(), PerformanceMonitor())
    row.bind_entry(model(1, QueueStatus.WAITING))
    configured = FakeWidget.configured

    assert row.update_selection(True)

    assert FakeWidget.configured == configured + 1
    assert row._frame.options["fg_color"] == theme.SURFACE_HOVER
    assert row._frame.options["border_color"] == theme.DECK_ACCENTS["A"]

    assert not row.update_selection(True)
    assert FakeWidget.configured == configured + 1


def test_status_update_configures_title_style_and_deck_actions(monkeypatch) -> None:
    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    row = QueueRowView(object(), callbacks(), PerformanceMonitor())
    row.bind_entry(model(1, QueueStatus.WAITING))
    configured = FakeWidget.configured

    assert row.update_status(QueueStatus.PLAYING)

    assert FakeWidget.configured == configured + 4
    assert row._title.options["text"].endswith("● ON AIR")
    assert row._frame.options["fg_color"] == "#4c2229"
    assert row._frame.options["border_color"] == theme.ON_AIR

    assert not row.update_status(QueueStatus.PLAYING)
    assert FakeWidget.configured == configured + 4


def test_request_count_update_changes_only_title(monkeypatch) -> None:
    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    row = QueueRowView(object(), callbacks(), PerformanceMonitor())
    row.bind_entry(model(1, QueueStatus.WAITING))
    configured = FakeWidget.configured

    assert row.update_request_count(3)

    assert FakeWidget.configured == configured + 1
    assert "3 Wünsche" in str(row._title.options["text"])

    assert not row.update_request_count(3)
    assert FakeWidget.configured == configured + 1


def test_targeted_status_event_does_not_configure_other_row(monkeypatch) -> None:
    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    first = QueueRowView(object(), callbacks(), PerformanceMonitor())
    second = QueueRowView(object(), callbacks(), PerformanceMonitor())
    first.bind_entry(model(1, QueueStatus.WAITING))
    second.bind_entry(model(2, QueueStatus.WAITING))
    second_configured = second.configured_widget_count

    first.update_status(QueueStatus.PLAYING)

    assert second.configured_widget_count == second_configured


def test_rebind_reuses_tooltips_and_cancels_delayed_callbacks(monkeypatch) -> None:
    FakeTooltip.created = 0
    FakeTooltip.cancelled = 0
    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    row = QueueRowView(object(), callbacks(), PerformanceMonitor())
    row.bind_entry(model(1, QueueStatus.WAITING))

    row.bind_entry(None)
    row.bind_entry(model(2, QueueStatus.WAITING))

    assert FakeTooltip.created == 4
    assert FakeTooltip.cancelled == 4


def test_dispose_closes_all_tooltips_once(monkeypatch) -> None:
    FakeTooltip.closed = 0
    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    row = QueueRowView(object(), callbacks(), PerformanceMonitor())

    row.dispose()
    row.dispose()

    assert FakeTooltip.closed == 4


def test_priority_lock_requests_and_skip_reason_are_rendered_fieldwise(monkeypatch) -> None:
    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    row = QueueRowView(object(), callbacks(), PerformanceMonitor())
    base = model(1, QueueStatus.SKIPPED)
    entry = replace(
        base.entry,
        priority=4,
        locked=True,
        request_count=2,
        skip_reason="Doppelt",
    )

    row.bind_entry(replace(base, entry=entry, request_count=2))

    text = str(row._title.options["text"])
    assert "2 Wünsche" in text
    assert "Priorität 4" in text
    assert "Gesperrt" in text
    assert "Grund: Doppelt" in text
    assert row._frame.options["fg_color"] == "#40391f"
    assert row._frame.options["border_color"] == theme.WARNING


def test_default_playlist_priority_is_shown_as_cd_playlist(monkeypatch) -> None:
    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    row = QueueRowView(object(), callbacks(), PerformanceMonitor())
    base = model(1, QueueStatus.WAITING)
    entry = replace(base.entry, source="PLAYLIST", priority=300)

    row.bind_entry(replace(base, entry=entry))

    text = str(row._title.options["text"])
    assert "CD/Playlist" in text
    assert "Priorität 300" not in text


def test_recovered_queue_entry_is_visibly_marked(monkeypatch) -> None:
    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    row = QueueRowView(object(), callbacks(), PerformanceMonitor())

    row.bind_entry(replace(model(1, QueueStatus.WAITING), restored=True))

    assert "↻ Wiederhergestellt" in str(row._title.options["text"])


def test_risky_queue_cues_are_visibly_marked(monkeypatch) -> None:
    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    row = QueueRowView(object(), callbacks(), PerformanceMonitor())

    row.bind_entry(
        replace(
            model(1, QueueStatus.WAITING),
            cue_warning="Überblenddauer unterschreitet das sichere Minimum",
        )
    )

    assert "⚠ Cue:" in str(row._title.options["text"])
    assert "sichere Minimum" in str(row._title.options["text"])


def test_more_menu_offers_track_wide_equalizer_actions(monkeypatch) -> None:
    actions: list[tuple[str, int]] = []

    class FakeMenu:
        commands: list[tuple[str, Any]] = []
        popup_position: tuple[int, int] | None = None

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            type(self).commands = []

        def add_command(self, *, label: str, command: Any = None, **_kwargs: object) -> None:
            type(self).commands.append((label, command))

        def add_separator(self) -> None:
            pass

        def tk_popup(self, x: int, y: int) -> None:
            type(self).popup_position = (x, y)

    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    monkeypatch.setattr(queue_row.tk, "Menu", FakeMenu)
    row_callbacks = callbacks()
    row_callbacks["equalizer"] = lambda queue_id: actions.append(("assign", queue_id))
    row_callbacks["equalizer_remove"] = lambda queue_id: actions.append(("remove", queue_id))
    row = QueueRowView(object(), row_callbacks, PerformanceMonitor())
    row.bind_entry(model(7, QueueStatus.WAITING))

    row._show_more_actions()

    commands = dict(FakeMenu.commands)
    commands["Equalizer zuweisen…"]()
    commands["Equalizer-Zuweisung entfernen"]()
    assert actions == [("assign", 7), ("remove", 7)]
    assert FakeMenu.popup_position == (21, 43)


def test_more_menu_remains_actionable_after_live_status_update(monkeypatch) -> None:
    invoked: list[int] = []

    class FakeMenu:
        commands: list[tuple[str, Any]] = []

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            type(self).commands = []

        def add_command(self, *, label: str, command: Any = None, **_kwargs: object) -> None:
            type(self).commands.append((label, command))

        def add_separator(self) -> None:
            pass

        def tk_popup(self, _x: int, _y: int) -> None:
            pass

        def destroy(self) -> None:
            pass

    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    monkeypatch.setattr(queue_row.tk, "Menu", FakeMenu)
    row_callbacks = callbacks()
    row_callbacks["played"] = invoked.append
    row = QueueRowView(object(), row_callbacks, PerformanceMonitor())
    row.bind_entry(model(4, QueueStatus.READY))
    row.update_status(QueueStatus.PLAYING)

    row._show_more_actions()

    commands = dict(FakeMenu.commands)
    commands["Als gespielt markieren"]()
    assert invoked == [4]


def test_more_menu_offers_repetition_override_and_does_not_treat_priority_as_lock(
    monkeypatch,
) -> None:
    invoked: list[int] = []
    reset_to_waiting: list[int] = []

    class FakeMenu:
        commands: list[tuple[str, Any]] = []

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            type(self).commands = []

        def add_command(self, *, label: str, command: Any = None, **_kwargs: object) -> None:
            type(self).commands.append((label, command))

        def add_separator(self) -> None:
            pass

        def tk_popup(self, _x: int, _y: int) -> None:
            pass

    monkeypatch.setattr(queue_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(queue_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(queue_row, "Tooltip", FakeTooltip)
    monkeypatch.setattr(queue_row.tk, "Menu", FakeMenu)
    row_callbacks = callbacks()
    row_callbacks["override_skip"] = invoked.append
    row_callbacks["retry"] = reset_to_waiting.append
    row = QueueRowView(object(), row_callbacks, PerformanceMonitor())
    base = model(9, QueueStatus.SKIPPED)
    entry = replace(
        base.entry,
        source="PLAYLIST",
        priority=300,
        skip_reason="Titel wurde vor Kurzem gespielt",
        skip_code="TRACK_REPETITION",
    )
    row.bind_entry(replace(base, entry=entry))

    row._show_more_actions()

    commands = dict(FakeMenu.commands)
    assert "Sperren" in commands
    assert "Entsperren" not in commands
    commands["Wieder auf wartend setzen"]()
    commands["Trotzdem abspielen"]()
    assert reset_to_waiting == [9]
    assert invoked == [9]
