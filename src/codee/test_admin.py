"""Dashboard UI event tests."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from codee.admin import (EFFORT_DEFAULT, AdminState, ModelOption, PeepStep,
                         _context_summary, _highlight_parts, _run_preview,
                         running_panel)


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


def test_kill_asks_about_the_session_it_was_opened_for() -> None:
    state = SimpleNamespace(kill_job_id=0, kill_job_label="")

    AdminState.confirm_kill_job.fn(state, 7, "/nightly")

    assert (state.kill_job_id, state.kill_job_label) == (7, "/nightly")


def test_closing_the_kill_dialog_forgets_the_session() -> None:
    state = SimpleNamespace(kill_job_id=7, kill_job_label="/nightly")

    AdminState.set_kill_dialog_open.fn(state, False)

    assert (state.kill_job_id, state.kill_job_label) == (0, "")


def test_confirming_the_kill_stops_that_session() -> None:
    state = SimpleNamespace(kill_job_id=7, kill_job_label="/nightly",
                            _refresh_dashboard=Mock())
    with patch("codee.admin.SERVICE.kill_job", return_value=True) as kill, \
            patch("codee.admin.rx.toast.success") as toast:
        result = AdminState.kill_job.fn(state)

    kill.assert_called_once_with(7)
    state._refresh_dashboard.assert_called_once_with()
    toast.assert_called_once_with("Killed /nightly")
    assert result is toast.return_value
    assert state.kill_job_id == 0


def test_killing_a_session_that_already_finished_says_so() -> None:
    state = SimpleNamespace(kill_job_id=7, kill_job_label="/nightly",
                            _refresh_dashboard=Mock())
    with patch("codee.admin.SERVICE.kill_job", return_value=False), \
            patch("codee.admin.rx.toast.info") as toast:
        AdminState.kill_job.fn(state)

    toast.assert_called_once_with("That session had already finished.")


def _peep_state(**fields) -> SimpleNamespace:
    state = SimpleNamespace(peep_job_id=0, peep_job_label="", peep_steps=[],
                            peep_note="", peep_context="")
    state.__dict__.update(fields)
    state._load_peep = lambda: AdminState._load_peep(state)
    return state


def test_peep_shows_the_sessions_last_steps() -> None:
    state = _peep_state()
    with patch("codee.admin.SERVICE.peep_job", return_value={
            "agent": "Claude Code",
            "entries": [{"kind": "tool", "text": "Bash: ls",
                         "timestamp": "2026-10-01T10:00:00Z"}],
            "context": {"tokens": 41245, "window": 128000,
                        "compactions": 1}}) as peep:
        AdminState.open_peep.fn(state, 7, "/nightly")

    peep.assert_called_once_with(7, 10)
    assert (state.peep_job_id, state.peep_job_label) == (7, "/nightly")
    assert [(step.kind, step.text) for step in state.peep_steps] == [
        ("tool", "Bash: ls")]
    assert state.peep_note == ""
    assert state.peep_context == (
        "Context: 41,245 / 128,000 tokens (32%) · 1 compaction")


def test_the_context_line_says_only_what_the_agent_wrote_down() -> None:
    assert _context_summary(None) == ""
    assert _context_summary({"tokens": 41245, "window": 0,
                             "compactions": 0}) == (
        "Context: 41,245 tokens · 0 compactions")
    assert _context_summary({"tokens": 0, "window": 0,
                             "compactions": 3}) == "Context: 3 compactions"


def test_peep_says_when_there_is_nothing_yet() -> None:
    state = _peep_state()
    with patch("codee.admin.SERVICE.peep_job",
               return_value={"agent": "Codex", "entries": []}):
        AdminState.open_peep.fn(state, 7, "/nightly")

    assert state.peep_steps == []
    assert state.peep_note.startswith("Nothing to show yet.")


def test_peep_says_which_agents_it_cannot_read() -> None:
    state = _peep_state()
    with patch("codee.admin.SERVICE.peep_job",
               return_value={"agent": "OpenCode", "entries": None}):
        AdminState.open_peep.fn(state, 7, "/nightly")

    assert state.peep_note == "Peep isn't available for OpenCode sessions yet."


def test_refreshing_a_finished_session_keeps_what_was_on_screen() -> None:
    step = PeepStep(kind="text", text="Done.")
    state = _peep_state(peep_job_id=7, peep_steps=[step])
    with patch("codee.admin.SERVICE.peep_job", return_value=None):
        AdminState.refresh_peep.fn(state)

    assert state.peep_steps == [step]
    assert state.peep_note.startswith("This session has finished.")


def test_closing_the_peep_dialog_forgets_the_session() -> None:
    state = _peep_state(peep_job_id=7, peep_job_label="/nightly",
                        peep_steps=[PeepStep(kind="text", text="x")],
                        peep_note="note", peep_context="Context: 1 compaction")

    AdminState.set_peep_dialog_open.fn(state, False)

    assert (state.peep_job_id, state.peep_job_label, state.peep_steps,
            state.peep_note, state.peep_context) == (0, "", [], "", "")


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


CATALOG = [
    ModelOption(id="claude-opus-5-5", name="Claude Opus 5.5",
                efforts=["low", "medium", "high", "xhigh", "max"],
                default_effort="medium"),
    ModelOption(id="claude-opus-4-6", name="Claude Opus 4.6",
                efforts=["low", "medium", "high", "max"]),
    ModelOption(id="claude-haiku-4-5", name="Claude Haiku 4.5"),
    ModelOption(id="claude-sonnet-5.5", name="Claude Sonnet 5.5",
                efforts=["low", "medium", "high"], default_effort="medium",
                context_tiers=[["default", "200K"], ["long_context", "1M"]]),
    ModelOption(id="auto", name="Auto",
                efforts=["efficiency", "balance", "intelligence"],
                default_effort="balance", effort_label="Tier"),
]


def _editor(model: str = "", effort: str = "",
            context_tier: str = "") -> SimpleNamespace:
    return SimpleNamespace(agent_models=CATALOG, skill_model=model,
                           skill_effort=effort, skill_context_tier=context_tier,
                           model_query="")


def _var(name: str, state: SimpleNamespace):
    return AdminState.computed_vars[name].fget(state)


def test_the_effort_picker_offers_the_picked_models_levels() -> None:
    state = _editor("claude-opus-5-5")

    assert _var("skill_effort_options", state) == [
        "low", "medium", "high", "xhigh", "max"]
    assert _var("skill_effort_default_label", state) == "Model default (medium)"
    assert _var("skill_effort_value", state) == EFFORT_DEFAULT


def test_a_reasoning_effort_picker_keeps_its_label_and_model_default() -> None:
    state = _editor("claude-opus-5-5")

    assert _var("skill_effort_label", state) == "Reasoning effort"
    assert _var("skill_effort_has_default", state) is True
    assert _var("skill_effort_choices", state)[3] == ["xhigh", "xhigh"]


def test_copilot_auto_offers_tiers_with_balance_preselected() -> None:
    state = _editor("auto")

    assert _var("skill_effort_label", state) == "Tier"
    assert _var("skill_effort_choices", state) == [
        ["efficiency", "Efficiency"], ["balance", "Balance"],
        ["intelligence", "Intelligence"]]
    assert _var("skill_effort_has_default", state) is False
    assert _var("skill_effort_value", state) == "balance"
    assert _var("skill_effort_value",
                _editor("auto", "intelligence")) == "intelligence"


def test_switching_to_auto_drops_a_reasoning_effort() -> None:
    state = _editor("claude-opus-5-5", "high")

    AdminState.choose_model.fn(state, "auto")

    assert state.skill_effort == ""


def test_a_model_without_effort_control_hides_the_picker() -> None:
    assert _var("skill_effort_options", _editor("claude-haiku-4-5")) == []
    # Nothing says what the agent's default or a typed id takes, either.
    assert _var("skill_effort_options", _editor("")) == []
    assert _var("skill_effort_options", _editor("my-custom-model")) == []


def test_an_effort_written_by_hand_stays_visible_and_is_flagged() -> None:
    state = _editor("my-custom-model", "ultra")

    assert _var("skill_effort_options", state) == ["ultra"]
    assert _var("skill_effort_unlisted", state) is True
    assert _var("skill_effort_unlisted",
                _editor("claude-opus-5-5", "max")) is False


def test_picking_a_model_that_lacks_the_effort_clears_it() -> None:
    state = _editor("claude-opus-5-5", "xhigh")

    AdminState.choose_model.fn(state, "claude-opus-4-6")

    assert state.skill_model == "claude-opus-4-6"
    assert state.skill_effort == ""


def test_picking_a_model_that_takes_the_effort_keeps_it() -> None:
    state = _editor("claude-opus-5-5", "max")

    AdminState.choose_model.fn(state, "claude-opus-4-6")

    assert state.skill_effort == "max"


def test_picking_a_model_the_catalog_lacks_keeps_the_effort() -> None:
    state = _editor("claude-opus-5-5", "high")

    AdminState.choose_model.fn(state, "claude-next")

    assert state.skill_effort == "high"


def test_choosing_model_default_effort_clears_it() -> None:
    state = _editor("claude-opus-5-5", "high")

    AdminState.set_skill_effort.fn(state, EFFORT_DEFAULT)
    assert state.skill_effort == ""
    AdminState.set_skill_effort.fn(state, "low")
    assert state.skill_effort == "low"


def test_the_context_picker_offers_the_models_windows() -> None:
    state = _editor("claude-sonnet-5.5")

    assert _var("skill_context_choices", state) == [
        ["default", "200K"], ["long_context", "1M"]]
    assert _var("skill_context_value", state) == "default"
    assert _var("skill_context_value",
                _editor("claude-sonnet-5.5", context_tier="long_context")
                ) == "long_context"


def test_a_model_without_a_choice_of_window_hides_the_context_picker() -> None:
    assert _var("skill_context_choices", _editor("claude-opus-5-5")) == []
    assert _var("skill_context_choices", _editor("")) == []


def test_picking_the_default_window_stores_nothing() -> None:
    state = _editor("claude-sonnet-5.5")

    AdminState.set_skill_context_tier.fn(state, "long_context")
    assert state.skill_context_tier == "long_context"
    AdminState.set_skill_context_tier.fn(state, "default")
    assert state.skill_context_tier == ""


def test_picking_a_model_without_long_context_clears_it() -> None:
    state = _editor("claude-sonnet-5.5", context_tier="long_context")

    AdminState.choose_model.fn(state, "claude-opus-5-5")

    assert state.skill_context_tier == ""


def test_switching_agent_resets_model_and_effort() -> None:
    state = SimpleNamespace(skill_agent="", skill_model="claude-opus-5-5",
                            skill_effort="high",
                            skill_context_tier="long_context", model_query="")

    AdminState.set_skill_agent.fn(state, "Codex")

    assert (state.skill_agent, state.skill_model, state.skill_effort,
            state.skill_context_tier) == ("codex", "", "", "")
