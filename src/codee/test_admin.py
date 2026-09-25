"""Dashboard UI event tests."""

from types import SimpleNamespace
from unittest.mock import patch

from codee.admin import AdminState


def test_pause_with_running_agents_explains_they_will_finish() -> None:
    state = SimpleNamespace(paused=False, active_jobs=[object()])
    with patch("codee.admin.SERVICE.set_paused", return_value=True) as set_paused, \
            patch("codee.admin.rx.toast.info") as toast:
        result = AdminState.toggle_pause.fn(state)

    set_paused.assert_called_once_with(True)
    toast.assert_called_once_with(
        "New agents won't be spawned. Currently running agents will continue until completion."
    )
    assert result is toast.return_value
    assert state.paused is True


def test_pause_without_running_agents_only_mentions_new_work() -> None:
    state = SimpleNamespace(paused=False, active_jobs=[])
    with patch("codee.admin.SERVICE.set_paused", return_value=True), \
            patch("codee.admin.rx.toast.info") as toast:
        AdminState.toggle_pause.fn(state)

    toast.assert_called_once_with("New agents won't be spawned.")


def test_unpause_does_not_show_pause_toast() -> None:
    state = SimpleNamespace(paused=True, active_jobs=[object()])
    with patch("codee.admin.SERVICE.set_paused", return_value=False) as set_paused, \
            patch("codee.admin.rx.toast.info") as toast:
        result = AdminState.toggle_pause.fn(state)

    set_paused.assert_called_once_with(False)
    toast.assert_not_called()
    assert result is None
    assert state.paused is False