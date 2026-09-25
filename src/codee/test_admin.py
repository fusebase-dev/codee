"""Dashboard UI event tests."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from codee.admin import AdminState, _highlight_parts, _run_preview, running_panel


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


def test_running_panel_shows_unpause_in_paused_empty_state() -> None:
    panel = running_panel().render()
    header_action = panel["children"][0]["children"][-1]["children"][0]
    empty_state = panel["children"][1]["children"][0]["false_value"]["children"][0]
    paused_row = empty_state["true_value"]["children"][0]["children"]

    assert "active_jobs" in header_action["cond_state"]
    assert "paused" in header_action["cond_state"]
    assert "paused" in empty_state["cond_state"]
    assert paused_row[1]["children"][0]["contents"] == '"All work is on pause right now."'
    unpause_button = paused_row[2]
    assert unpause_button["children"][-1]["contents"] == '"Unpause"'
    assert any("toggle_pause" in prop for prop in unpause_button["props"])
    idle_row = empty_state["false_value"]["children"][0]["children"]
    assert idle_row[1]["children"][0]["contents"] == '"No sessions running right now."'


def test_clearing_skill_search_resets_query() -> None:
    state = SimpleNamespace(skill_query="alarm")

    AdminState.set_skill_query.fn(state, "")

    assert state.skill_query == ""


def test_clearing_runs_search_reloads_unfiltered_first_page() -> None:
    runs = [object()]
    state = SimpleNamespace(runs_query="alarm", runs_loading=True, runs=[],
                            _fetch_runs_page=Mock(return_value=runs))

    AdminState.search_runs.fn(state, "")

    assert state.runs_query == ""
    assert state.runs_loading is False
    state._fetch_runs_page.assert_called_once_with(0)
    assert state.runs is runs


def test_runs_highlight_all_case_insensitive_literal_occurrences() -> None:
    parts = _highlight_parts("A.B and a.b and aXb", "a.b")

    assert [(part.text, part.matched) for part in parts] == [
        ("A.B", True), (" and ", False), ("a.b", True),
        (" and aXb", False),
    ]
    assert _highlight_parts("<script>hi</script>", "hi")[1].text == "hi"
    assert _highlight_parts("some text", "  ") == []
    assert _highlight_parts("some text", "missing") == []


def test_runs_preview_shows_hit_in_response_or_later_in_prompt() -> None:
    assert _run_preview("Other prompt", "Other prompt",
                        "The LLM found an ALARM in the logs", "alarm") == (
        "LLM response: The LLM found an ALARM in the logs")
    prompt = "A" * 100 + " alarm " + "B" * 100
    preview = _run_preview(prompt, prompt, "", "alarm")
    assert preview.startswith("...")
    assert "alarm" in preview


def test_fetch_runs_page_builds_matching_segments_for_all_search_fields() -> None:
    run = dict(skill_name="skill", trigger_type="issue", status="succeeded",
               started_at="2026-01-01", ended_at="2026-01-02",
               duration_label="1s", relative_age_label="yesterday",
               session_id="thread", message="ALARM in original",
               user_message="Investigate alarm", response="Alarm resolved")
    state = SimpleNamespace(runs_query="alarm", runs_has_more=False)
    with patch("codee.admin.SERVICE.recent_runs", return_value=[run]) as recent_runs:
        records = AdminState._fetch_runs_page(state, 0)

    recent_runs.assert_called_once_with(21, 0, "alarm")
    assert len(records) == 1
    assert records[0].preview_parts[0].matched is True
    assert records[0].message_parts[0].matched is True
    assert records[0].user_message_parts[1].matched is True
    assert records[0].response_parts[0].matched is True
