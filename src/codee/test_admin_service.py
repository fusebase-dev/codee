import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from codee_agent_abstract.provider import AgentModel
from codee_agent_claude_code.provider import ClaudeCodeAgent
from codee_agent_codex.provider import CodexAgent
from codee_agent_github_copilot.provider import GitHubCopilotAgent

from codee.admin_service import (
    MCP_CHECK, TASKS_CHECK, USAGE_CACHE_SECONDS,
    USAGE_RATE_LIMIT_BACKOFF_SECONDS, WORKFLOW_CACHE_VERSION,
    WORKFLOW_HUMAN_EDGE_COLOR, AdminService, WorkflowGeneration,
    _remove_redundant_skill_transitions, azure_oauth, issue_prompt_task,
    normalize_work_items, parse_skill, repository_name)
from codee.lib import runs_db
from codee_agent_claude_code import oauth as claude_oauth
from codee_agent_claude_code.account import AccountUnavailable
from codee_agent_claude_code.usage import (
    Usage, UsageRateLimited, UsageUnavailable)
from codee_database import claude_code_accounts
from codee_main_context.context import (
    CodeeMainContext, CodingAgent, Settings, TasksProvider, load_settings,
    save_settings)


def _empty_workflow() -> dict:
    return {issue_type: {"nodes": [], "edges": [], "warnings": []}
            for issue_type in ("story", "task")}


def _generating_service() -> AdminService:
    """A service with only what the shared workflow run needs."""
    service = AdminService.__new__(AdminService)
    service._workflow_run = WorkflowGeneration()
    service._workflow_run_lock = threading.Lock()
    service._workflow_run_force = False
    service._workflow_force_pending = False
    return service


def _write_issue_skill(root: Path) -> Path:
    """Create one issue-trigger skill, the input the workflow cache is keyed on."""
    skills_dir = root / ".claude" / "skills"
    skill_dir = skills_dir / "develop"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: develop\ndisable-model-invocation: true\n"
        "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
        "x-codee-issue-type: story\n---\n"
        "After implementation, move the issue to Review.\n"
    )
    return skills_dir


class NormalizeWorkItemsTest(unittest.TestCase):
    """What the settings form's mapping rows have to satisfy before they save."""

    def test_rows_become_a_lower_cased_mapping(self) -> None:
        mapping, queries, error = normalize_work_items(
            [("Story", ["User Story"], ""), ("task", [" Task "], ""),
             ("Bug", ["Bug"], "")])

        self.assertEqual(error, "")
        self.assertEqual(mapping, {"story": ["User Story"], "task": ["Task"],
                                   "bug": ["Bug"]})
        self.assertEqual(queries, {})

    def test_one_work_item_can_name_several_provider_types(self) -> None:
        # A Codee task that covers the backend's Task and Bug both is one work
        # item with one set of skills, not two.
        mapping, _, error = normalize_work_items(
            [("story", ["Story"], ""), ("task", ["Task", "Bug"], "")])

        self.assertEqual(error, "")
        self.assertEqual(mapping, {"story": ["Story"], "task": ["Task", "Bug"]})

    def test_a_type_listed_twice_in_one_row_is_kept_once(self) -> None:
        mapping, _, error = normalize_work_items(
            [("story", ["Story"], ""), ("task", ["Task", " task ", "Bug"], "")])

        self.assertEqual(error, "")
        self.assertEqual(mapping["task"], ["Task", "Bug"])

    def test_a_type_mapped_to_two_work_items_is_refused(self) -> None:
        # Whichever of them an incoming Bug was reported as, the other work
        # item's skills would never see it — and nothing would say why.
        _, _, error = normalize_work_items(
            [("story", ["Story"], ""), ("task", ["Task", "Bug"], ""),
             ("defect", ["bug"], "")])

        self.assertEqual(
            error, "Work item type 'bug' is mapped to both 'task' and 'defect'")

    def test_a_query_is_stored_beside_the_types_it_replaces(self) -> None:
        # The types stay so switching the row back offers them again; the
        # stored query is what says the query is what selects it.
        mapping, queries, error = normalize_work_items(
            [("story", ["Story"], ""),
             ("task", ["Task"], '  labels = "codee"  ')])

        self.assertEqual(error, "")
        self.assertEqual(mapping["task"], ["Task"])
        self.assertEqual(queries, {"task": 'labels = "codee"'})

    def test_a_work_item_with_only_a_query_is_accepted(self) -> None:
        _, queries, error = normalize_work_items(
            [("story", ["Story"], ""), ("task", ["Task"], ""),
             ("bug", [], "issuetype = Bug")])

        self.assertEqual(error, "")
        self.assertEqual(queries, {"bug": "issuetype = Bug"})

    def test_a_queried_work_item_may_reuse_a_mapped_type(self) -> None:
        # Its types are not what polls it, so there is nothing to collide with.
        _, _, error = normalize_work_items(
            [("story", ["Story"], ""), ("task", ["Task"], ""),
             ("bug", ["Task"], 'labels = "codee"')])

        self.assertEqual(error, "")

    def test_the_mandatory_work_items_cannot_be_removed(self) -> None:
        # Losing them would leave every story skill matching nothing, silently.
        _, _, error = normalize_work_items(
            [("story", ["Story"], ""), ("bug", ["Bug"], "")])

        self.assertEqual(error, "Work items task cannot be removed")

    def test_a_row_without_a_name_is_refused(self) -> None:
        _, _, error = normalize_work_items(
            [("story", ["Story"], ""), ("task", ["Task"], ""),
             ("  ", ["Bug"], "")])

        self.assertEqual(error, "Give every work item a name")

    def test_a_row_with_neither_a_type_nor_a_query_is_refused(self) -> None:
        _, _, error = normalize_work_items(
            [("story", ["Story"], ""), ("task", ["Task"], ""), ("bug", [], "")])

        self.assertEqual(error,
                         "Choose a provider work item type for 'bug'")

    def test_a_row_whose_only_type_is_blank_is_refused(self) -> None:
        _, _, error = normalize_work_items(
            [("story", ["Story"], ""), ("task", ["Task"], ""),
             ("bug", ["  "], "")])

        self.assertEqual(error,
                         "Choose a provider work item type for 'bug'")

    def test_two_rows_with_the_same_name_are_refused(self) -> None:
        # They differ only in case, so one would silently overwrite the other.
        _, _, error = normalize_work_items(
            [("story", ["Story"], ""), ("task", ["Task"], ""),
             ("Bug", ["Bug"], ""), ("bug", ["Defect"], "")])

        self.assertEqual(error, "'bug' is listed twice")


class AdminServiceWorkItemsTest(unittest.TestCase):
    def _service(self, directory: Path) -> AdminService:
        service = AdminService.__new__(AdminService)
        service.data_dir = directory
        service.context = CodeeMainContext(data_dir=directory)
        service.context.settings = load_settings(directory)
        return service

    def test_saving_keeps_the_other_provider_s_mapping(self) -> None:
        # Switching provider and back must not reset what was configured.
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            service = self._service(directory)

            service.save_settings("jira", "claude_code", 3, {},
                                  {"story": ["Epic"], "task": ["Task"]})
            service.save_settings("azure_devops", "claude_code", 3, {},
                                  {"story": ["User Story"],
                                   "task": ["Task", "Bug"]})

            stored = load_settings(directory).work_item_types
            self.assertEqual(stored["jira"],
                             {"story": ["Epic"], "task": ["Task"]})
            self.assertEqual(stored["azure_devops"],
                             {"story": ["User Story"], "task": ["Task", "Bug"]})

    def test_saving_keeps_the_other_provider_s_task_filter(self) -> None:
        # A JQL clause means nothing to Azure DevOps, so each provider keeps
        # its own and switching between them loses neither.
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            service = self._service(directory)

            service.save_settings("jira", "claude_code", 3, {}, None, None,
                                  'labels = "codee"')
            service.save_settings("azure_devops", "claude_code", 3, {}, None,
                                  None, "[System.Tags] CONTAINS 'codee'")

            stored = load_settings(directory).task_filters
            self.assertEqual(stored["jira"], 'labels = "codee"')
            self.assertEqual(stored["azure_devops"],
                             "[System.Tags] CONTAINS 'codee'")

    def test_an_unset_task_filter_saves_as_no_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            service = self._service(directory)

            service.save_settings("jira", "claude_code", 3, {},
                                  {"story": ["Story"], "task": ["Task"]})

            self.assertEqual(load_settings(directory).task_filters["jira"], "")

    def test_saving_keeps_the_other_provider_s_work_item_queries(self) -> None:
        # A JQL condition means nothing to Azure DevOps, so each provider keeps
        # its own and switching between them loses neither.
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            service = self._service(directory)

            service.save_settings("jira", "claude_code", 3, {},
                                  {"story": ["Story"], "task": ["Task"]},
                                  {"task": 'labels = "codee"'})
            service.save_settings("azure_devops", "claude_code", 3, {},
                                  {"story": ["User Story"], "task": ["Task"]},
                                  {"task": "[System.Tags] CONTAINS 'codee'"})

            stored = load_settings(directory).work_item_queries
            self.assertEqual(stored["jira"], {"task": 'labels = "codee"'})
            self.assertEqual(stored["azure_devops"],
                             {"task": "[System.Tags] CONTAINS 'codee'"})

    def test_a_work_item_selected_by_a_query_is_one_skills_may_declare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            service = self._service(directory)

            service.save_settings("jira", "claude_code", 3, {},
                                  {"story": ["Story"], "task": ["Task"],
                                   "bug": []},
                                  {"bug": 'labels = "codee-bug"'})

            self.assertEqual(service.issue_types(), ("story", "task", "bug"))
            self.assertTrue(service.work_item_mappings("jira")[2].is_query)

    def test_the_saved_work_items_are_what_skills_may_declare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            service = self._service(directory)

            service.save_settings("jira", "claude_code", 3, {},
                                  {"story": ["Story"], "task": ["Task"],
                                   "bug": ["Bug"]})

            self.assertEqual(service.issue_types(), ("story", "task", "bug"))


class AdminServiceClaudeCodeAccountsTest(unittest.TestCase):
    """Connecting, listing and disconnecting the accounts rotation runs on."""

    TOKENS = claude_oauth.Tokens(
        access_token="access-1", refresh_token="refresh-1",
        expires_at=1789300823639, refresh_expires_at=1791303745639,
        scopes=("user:profile", "user:inference"), subscription_type="max")

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.directory = Path(self._temporary.name)
        self.service = AdminService.__new__(AdminService)
        self.service.data_dir = self.directory
        self.service.context = CodeeMainContext(data_dir=self.directory)
        self.service.context.settings = Settings(claude_code_rotate_keys=True)
        # Built by hand rather than through __init__, like the other service
        # tests, so the usage cache has to be set up by hand too.
        self.service._usage_cache = {}
        self.service._usage_lock = threading.Lock()

    def _connect(self, label: str = "one@example.com",
                 tokens: claude_oauth.Tokens | None = None) -> tuple[bool, str]:
        authorization = self.service.start_claude_code_authorization()
        with patch("codee.admin_service.claude_oauth.exchange_code",
                   return_value=tokens or self.TOKENS), \
                patch("codee.admin_service.fetch_account", return_value=label):
            return self.service.complete_claude_code_authorization(
                f"the-code#{authorization.state}", authorization)

    def test_a_completed_sign_in_becomes_a_named_account(self) -> None:
        # Naming it is most of the point: a list of accounts nobody can tell
        # apart is no better than a list of masked keys.
        connected, message = self._connect("one@example.com")

        self.assertTrue(connected)
        self.assertIn("one@example.com", message)
        accounts = self.service.claude_code_accounts()
        self.assertEqual([account.label for account in accounts],
                         ["one@example.com"])
        self.assertEqual(accounts[0].subscription, "max")

    def test_the_first_account_connected_is_the_one_in_use(self) -> None:
        self._connect("one@example.com")
        self._connect("two@example.com")

        accounts = self.service.claude_code_accounts()

        self.assertEqual([account.in_use for account in accounts],
                         [True, False])

    def test_accounts_are_listed_in_the_order_they_were_connected(self) -> None:
        # That order is the order rotation works through them, so what the user
        # reads top to bottom is what will actually happen.
        self._connect("one@example.com")
        self._connect("two@example.com")
        self._connect("three@example.com")

        self.assertEqual(
            [account.label for account in self.service.claude_code_accounts()],
            ["one@example.com", "two@example.com", "three@example.com"])

    def test_the_tokens_are_stored_and_never_reach_the_page(self) -> None:
        # The page model carries no token at all: it only has to say which
        # account this is and whether it is live.
        self._connect()

        listed = self.service.claude_code_accounts()[0]
        stored = claude_code_accounts.accounts(self.service.context)[0]

        self.assertFalse(hasattr(listed, "access_token"))
        self.assertEqual(stored.access_token, "access-1")
        self.assertEqual(stored.refresh_token, "refresh-1")
        self.assertEqual(stored.expires_at, 1789300823639)
        # Kept so the page can say when an account has to be signed in again,
        # and so the executor knows to stop trying to renew it.
        self.assertEqual(stored.refresh_expires_at, 1791303745639)

    def test_a_code_from_a_different_sign_in_is_refused(self) -> None:
        # The verifier about to redeem it belongs to this sign-in; a code from
        # another one would be redeemed against the wrong challenge.
        authorization = self.service.start_claude_code_authorization()

        with patch("codee.admin_service.claude_oauth.exchange_code") as exchange:
            connected, message = self.service.complete_claude_code_authorization(
                "the-code#some-other-state", authorization)

        exchange.assert_not_called()
        self.assertFalse(connected)
        self.assertIn("different sign-in", message)

    def test_a_bare_code_without_the_state_is_accepted(self) -> None:
        # The page prints code and state joined, but a user who copied only the
        # first half has still copied a usable code.
        authorization = self.service.start_claude_code_authorization()

        with patch("codee.admin_service.claude_oauth.exchange_code",
                   return_value=self.TOKENS), \
                patch("codee.admin_service.fetch_account",
                      return_value="one@example.com"):
            connected, _ = self.service.complete_claude_code_authorization(
                "the-code", authorization)

        self.assertTrue(connected)

    def test_nothing_pasted_is_refused_before_any_round_trip(self) -> None:
        authorization = self.service.start_claude_code_authorization()

        with patch("codee.admin_service.claude_oauth.exchange_code") as exchange:
            connected, message = self.service.complete_claude_code_authorization(
                "   ", authorization)

        exchange.assert_not_called()
        self.assertFalse(connected)
        self.assertIn("Paste", message)

    def test_a_refused_code_says_so_and_connects_nothing(self) -> None:
        authorization = self.service.start_claude_code_authorization()

        with patch("codee.admin_service.claude_oauth.exchange_code",
                   side_effect=claude_oauth.OAuthApiError("code was refused")):
            connected, message = self.service.complete_claude_code_authorization(
                f"the-code#{authorization.state}", authorization)

        self.assertFalse(connected)
        self.assertIn("code was refused", message)
        self.assertEqual(self.service.claude_code_accounts(), [])

    def test_an_account_whose_profile_cannot_be_read_is_still_connected(self) -> None:
        # A name is worth having but it is not the credential; refusing the
        # sign-in over it would throw away a working account.
        authorization = self.service.start_claude_code_authorization()

        with patch("codee.admin_service.claude_oauth.exchange_code",
                   return_value=self.TOKENS), \
                patch("codee.admin_service.fetch_account",
                      side_effect=AccountUnavailable("no route")):
            connected, _ = self.service.complete_claude_code_authorization(
                f"the-code#{authorization.state}", authorization)

        self.assertTrue(connected)
        self.assertEqual(len(self.service.claude_code_accounts()), 1)

    def test_disconnecting_the_account_in_use_repoints_to_the_first(self) -> None:
        self._connect("one@example.com")
        self._connect("two@example.com")
        second = self.service.claude_code_accounts()[1]
        claude_code_accounts.set_current_account(second.id, self.service.context)

        self.service.disconnect_claude_code_account(second.id)

        accounts = self.service.claude_code_accounts()
        self.assertEqual([account.label for account in accounts],
                         ["one@example.com"])
        self.assertTrue(accounts[0].in_use)

    def test_disconnecting_an_account_forgets_its_tokens(self) -> None:
        self._connect()
        account = self.service.claude_code_accounts()[0]

        self.service.disconnect_claude_code_account(account.id)

        self.assertEqual(claude_code_accounts.accounts(self.service.context), [])

    def test_switching_rotation_off_forgets_which_account_is_in_use(self) -> None:
        # So switching it back on later starts from the top rather than from
        # whatever a run before the change left behind.
        self._connect("one@example.com")
        self._connect("two@example.com")
        second = self.service.claude_code_accounts()[1]
        claude_code_accounts.set_current_account(second.id, self.service.context)

        self.service.save_settings("jira", "claude_code", 3, {}, None, None,
                                   "", False)

        self.assertEqual(
            claude_code_accounts.current_account_id(self.service.context), 0)

    def test_switching_rotation_on_picks_the_first_account(self) -> None:
        self.service.context.settings = Settings(claude_code_rotate_keys=False)
        self._connect("one@example.com")

        self.service.save_settings("jira", "claude_code", 3, {}, None, None,
                                   "", True)

        self.assertTrue(self.service.claude_code_accounts()[0].in_use)

    def test_an_account_past_its_refresh_window_is_flagged_on_the_page(self) -> None:
        # Nothing else on the page would ever tell the user: rotation just
        # skips it, silently, on the day they need it most.
        expired = claude_oauth.Tokens(
            "access-1", "refresh-1", expires_at=1, refresh_expires_at=1)
        self._connect("one@example.com", tokens=expired)

        listed = self.service.claude_code_accounts()[0]

        self.assertTrue(listed.needs_reconnect)

    def test_a_healthy_account_is_not_flagged(self) -> None:
        far_future = int((time.time() + 30 * 24 * 3600) * 1000)
        self._connect("one@example.com", tokens=claude_oauth.Tokens(
            "access-1", "refresh-1", expires_at=far_future,
            refresh_expires_at=far_future))

        self.assertFalse(self.service.claude_code_accounts()[0].needs_reconnect)

    def test_an_account_with_no_known_window_is_not_flagged(self) -> None:
        # Zero means the API never said, which is not the same as expired.
        self._connect("one@example.com", tokens=claude_oauth.Tokens(
            "access-1", "refresh-1", expires_at=1, refresh_expires_at=0))

        self.assertFalse(self.service.claude_code_accounts()[0].needs_reconnect)

    def test_each_account_reports_both_of_its_windows(self) -> None:
        self._connect("one@example.com")
        usage = Usage(limited=False, windows={"five_hour": 12.0,
                                              "seven_day": 70.0},
                      resets_at={"five_hour": "2026-09-13T14:10:00+00:00",
                                 "seven_day": "2026-09-16T09:00:00+00:00"})

        with patch("codee.admin_service.ensure_fresh", side_effect=lambda a, c: a), \
                patch("codee.admin_service.fetch_usage", return_value=usage):
            measured = self.service.claude_code_account_usage()

        self.assertEqual(measured[0].session_percent, 12.0)
        self.assertEqual(measured[0].weekly_percent, 70.0)
        self.assertEqual(measured[0].weekly_resets, "2026-09-16T09:00:00+00:00")
        self.assertEqual(measured[0].usage_error, "")

    def test_the_reading_is_cached_rather_than_taken_every_redraw(self) -> None:
        # The dashboard redraws every second; without this that would be a
        # request per account per second for a number that moves over hours.
        self._connect("one@example.com")
        usage = Usage(limited=False, windows={"five_hour": 1.0}, resets_at={})

        with patch("codee.admin_service.ensure_fresh", side_effect=lambda a, c: a), \
                patch("codee.admin_service.fetch_usage",
                      return_value=usage) as fetch:
            self.service.claude_code_account_usage()
            self.service.claude_code_account_usage()

        fetch.assert_called_once()

    def test_an_account_connected_just_now_is_on_the_next_reading(self) -> None:
        # What the cache used to hold was the whole list, so an account
        # connected a minute after the last reading stayed off the dashboard
        # until the accounts already on it were due to be asked about again.
        self._connect("one@example.com")
        usage = Usage(limited=False, windows={"five_hour": 3.0}, resets_at={})

        with patch("codee.admin_service.ensure_fresh", side_effect=lambda a, c: a), \
                patch("codee.admin_service.fetch_usage",
                      return_value=usage) as fetch:
            self.service.claude_code_account_usage()
            self._connect("two@example.com")
            measured = self.service.claude_code_account_usage()

        self.assertEqual([account.label for account in measured],
                         ["one@example.com", "two@example.com"])
        # Only the new account was asked about: the first one's reading still
        # stands, which is what the cache is for.
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(measured[1].session_percent, 3.0)

    def test_a_disconnected_account_leaves_the_reading_at_once(self) -> None:
        self._connect("one@example.com")
        self._connect("two@example.com")
        usage = Usage(limited=False, windows={"five_hour": 3.0}, resets_at={})

        with patch("codee.admin_service.ensure_fresh", side_effect=lambda a, c: a), \
                patch("codee.admin_service.fetch_usage", return_value=usage):
            first = self.service.claude_code_account_usage()
            self.service.disconnect_claude_code_account(first[1].id)
            measured = self.service.claude_code_account_usage()

        self.assertEqual([account.label for account in measured],
                         ["one@example.com"])
        self.assertEqual(list(self.service._usage_cache), [first[0].id])

    def test_a_cached_reading_does_not_hold_a_stale_account_in_use(self) -> None:
        # The meters are what goes out of date over minutes; which account is
        # live changes the moment rotation moves, and the widget's one job is
        # to say which that is.
        self._connect("one@example.com")
        self._connect("two@example.com")
        usage = Usage(limited=False, windows={"five_hour": 3.0}, resets_at={})

        with patch("codee.admin_service.ensure_fresh", side_effect=lambda a, c: a), \
                patch("codee.admin_service.fetch_usage", return_value=usage):
            measured = self.service.claude_code_account_usage()
            claude_code_accounts.set_current_account(
                measured[1].id, self.service.context)
            again = self.service.claude_code_account_usage()

        self.assertEqual([account.in_use for account in measured], [True, False])
        self.assertEqual([account.in_use for account in again], [False, True])
        self.assertEqual(again[1].session_percent, 3.0)

    def test_a_rate_limited_reading_is_not_retried_on_the_usual_cadence(self) -> None:
        # Asking too often is what produced the 429, so the cache holds for
        # much longer than usual rather than turning the refusal into a loop.
        self._connect("one@example.com")

        with patch("codee.admin_service.ensure_fresh", side_effect=lambda a, c: a), \
                patch("codee.admin_service.fetch_usage",
                      side_effect=UsageRateLimited("HTTP 429")):
            self.service.claude_code_account_usage()

        stands_for = [entry[2] for entry in self.service._usage_cache.values()]
        self.assertEqual(stands_for, [USAGE_RATE_LIMIT_BACKOFF_SECONDS])

    def test_a_rate_limited_account_keeps_the_numbers_it_last_reported(self) -> None:
        # The meters move over hours: a reading a few minutes old is a far
        # better answer than an error where the meters were.
        self._connect("one@example.com")
        usage = Usage(limited=False, windows={"five_hour": 12.0,
                                              "seven_day": 70.0},
                      resets_at={"five_hour": "2026-09-13T14:10:00+00:00"})

        with patch("codee.admin_service.ensure_fresh", side_effect=lambda a, c: a), \
                patch("codee.admin_service.fetch_usage", return_value=usage):
            self.service.claude_code_account_usage()
        # Expire the cache so the next call asks, and is refused.
        self.service._usage_cache = {
            account: (row, 0.0, USAGE_CACHE_SECONDS)
            for account, (row, _, _) in self.service._usage_cache.items()}
        with patch("codee.admin_service.ensure_fresh", side_effect=lambda a, c: a), \
                patch("codee.admin_service.fetch_usage",
                      side_effect=UsageRateLimited("HTTP 429")):
            measured = self.service.claude_code_account_usage()

        self.assertEqual(measured[0].session_percent, 12.0)
        self.assertEqual(measured[0].weekly_percent, 70.0)
        self.assertEqual(measured[0].session_resets, "2026-09-13T14:10:00+00:00")
        self.assertEqual(measured[0].usage_error, "")

    def test_a_rate_limit_with_nothing_to_fall_back_on_says_so(self) -> None:
        # Nothing was ever read, so there is no older number to show and the
        # row has to admit it rather than draw an empty meter as zero.
        self._connect("one@example.com")

        with patch("codee.admin_service.ensure_fresh", side_effect=lambda a, c: a), \
                patch("codee.admin_service.fetch_usage",
                      side_effect=UsageRateLimited("HTTP 429")):
            measured = self.service.claude_code_account_usage()

        self.assertIn("429", measured[0].usage_error)
        self.assertEqual(measured[0].session_percent, -1.0)

    def test_a_token_that_has_aged_out_is_renewed_before_it_is_asked(self) -> None:
        # An account waiting its turn holds an expired token and would report
        # itself rejected rather than report its allowance.
        self._connect("one@example.com")

        with patch("codee.admin_service.ensure_fresh",
                   side_effect=lambda a, c: a) as renew, \
                patch("codee.admin_service.fetch_usage",
                      return_value=Usage(limited=False, windows={}, resets_at={})):
            self.service.claude_code_account_usage()

        renew.assert_called_once()

    def test_an_account_whose_usage_cannot_be_read_says_so_on_its_own_row(self) -> None:
        # And the others still draw: one unreachable account must not blank the
        # whole widget.
        self._connect("one@example.com")
        self._connect("two@example.com")
        good = Usage(limited=False, windows={"five_hour": 5.0}, resets_at={})

        with patch("codee.admin_service.ensure_fresh", side_effect=lambda a, c: a), \
                patch("codee.admin_service.fetch_usage",
                      side_effect=[UsageUnavailable("connection reset"), good]):
            measured = self.service.claude_code_account_usage()

        self.assertIn("connection reset", measured[0].usage_error)
        self.assertEqual(measured[0].session_percent, -1.0)
        self.assertEqual(measured[1].session_percent, 5.0)

    def test_an_account_needing_a_new_sign_in_is_not_asked_at_all(self) -> None:
        # There is nothing to ask with, and the row already says what to do.
        self._connect("one@example.com", tokens=claude_oauth.Tokens(
            "access-1", "refresh-1", expires_at=1, refresh_expires_at=1))

        with patch("codee.admin_service.fetch_usage") as fetch:
            measured = self.service.claude_code_account_usage()

        fetch.assert_not_called()
        self.assertTrue(measured[0].needs_reconnect)
        self.assertIn("connected again", measured[0].usage_error)

    def test_the_account_in_use_is_marked_in_the_reading(self) -> None:
        # What the widget highlights; without it the list says nothing about
        # which subscription is actually doing the work.
        self._connect("one@example.com")
        self._connect("two@example.com")

        with patch("codee.admin_service.ensure_fresh", side_effect=lambda a, c: a), \
                patch("codee.admin_service.fetch_usage",
                      return_value=Usage(limited=False, windows={}, resets_at={})):
            measured = self.service.claude_code_account_usage()

        self.assertEqual([account.in_use for account in measured], [True, False])

    def test_no_accounts_means_no_requests(self) -> None:
        with patch("codee.admin_service.fetch_usage") as fetch:
            self.assertEqual(self.service.claude_code_account_usage(), [])

        fetch.assert_not_called()

    def test_the_sign_in_url_is_the_claude_code_oauth_client(self) -> None:
        # Same client and same scopes as `claude /login`, which is what makes
        # the resulting token able to answer whose account it is.
        url = self.service.start_claude_code_authorization().url

        self.assertIn(claude_oauth.CLIENT_ID, url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("user%3Aprofile", url)


class AdminServiceWorkItemTypesTest(unittest.TestCase):
    """The listing the settings dropdowns are filled from."""

    def _service(self, directory: Path) -> AdminService:
        service = AdminService.__new__(AdminService)
        service.data_dir = directory
        service.root = directory
        service.context = CodeeMainContext(data_dir=directory)
        service.context.settings = load_settings(directory)
        return service

    def test_it_reports_the_names_and_where_they_came_from(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            response = Mock(status_code=200)
            response.json.return_value = {"issueTypes": [
                {"name": "Task"}, {"name": "Bug"}]}

            with patch("codee_tasks_jira.provider.requests.get",
                       return_value=response):
                types, scope, error = service.list_work_item_types("jira", {
                    "base_url": "https://acme.atlassian.net",
                    "account_email": "agent@example.com",
                    "api_token": "token", "project": "CORE"})

            self.assertEqual(types, ["Bug", "Task"])
            self.assertEqual(scope, "project CORE")
            self.assertEqual(error, "")

    def test_a_refused_listing_reports_the_message_and_no_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            types, scope, error = service.list_work_item_types("jira", {
                "base_url": "", "account_email": "", "api_token": "",
                "project": ""})

            self.assertEqual((types, scope), ([], ""))
            self.assertIn("Fill in", error)


class AdminServiceIssueTriggerTest(unittest.TestCase):
    def test_list_skills_includes_issue_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            skill_dir = skills_dir / "triage"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: triage\ndescription: Triage issues\n"
                "x-codee-trigger: issue\n"
                "x-codee-issue-status: [Ready, In progress]\n"
                "x-codee-issue-type: story\n---\nBody\n"
            )
            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir

            skills = service.list_skills()

            self.assertEqual(skills[0]["issue_status"], "Ready, In progress")
            self.assertEqual(skills[0]["issue_type"], "story")

    def test_list_skills_includes_the_agent_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            for slug, frontmatter in (
                ("nightly", "name: nightly\nx-codee-agent: Codex\n"
                            "model: gpt-6-astra\n"),
                ("plain", "name: plain\n"),
            ):
                (skills_dir / slug).mkdir()
                (skills_dir / slug / "SKILL.md").write_text(
                    f"---\n{frontmatter}---\nBody\n")
            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir

            rows = {skill["slug"]: (skill["agent"], skill["model"])
                    for skill in service.list_skills()}

            self.assertEqual(rows, {"nightly": ("codex", "gpt-6-astra"),
                                    "plain": ("", "")})

    def test_save_issue_trigger_writes_required_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            skill_dir = skills_dir / "triage"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: triage\nx-codee-issue-type: story\n---\nBody\n"
            )
            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir
            # The work items a skill may declare come from the settings file.
            service.data_dir = skills_dir

            with patch.object(service, "_write_and_push",
                              return_value=(True, True, "saved")) as write:
                ok, _, _, slug = service.save_skill({
                    "slug": "triage",
                    "name": "triage",
                    "description": "Triage matching issues",
                    "type": "issue trigger",
                    "issue_status": "Ready, In progress",
                    "issue_type": "task",
                    "body": "Body",
                })

            frontmatter, _ = parse_skill(write.call_args.args[1])
            self.assertTrue(ok)
            self.assertEqual(slug, "triage")
            self.assertIs(frontmatter["disable-model-invocation"], True)
            self.assertEqual(frontmatter["x-codee-trigger"], "issue")
            self.assertEqual(
                frontmatter["x-codee-issue-status"], ["Ready", "In progress"])
            self.assertEqual(frontmatter["x-codee-issue-type"], "task")

    def test_save_reports_saved_when_git_push_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "triage"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: triage\n---\nOld\n")
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir

            with patch.object(service, "_git_push", return_value=(False, "no upstream")):
                saved, pushed, message, slug = service.save_skill({
                    "slug": "triage",
                    "name": "triage",
                    "description": "Triage matching issues",
                    "type": "knowledge",
                    "issue_status": "",
                    "issue_type": "",
                    "body": "New body",
                })

            self.assertTrue(saved)
            self.assertFalse(pushed)
            self.assertIn("Git push failed", message)
            self.assertEqual(slug, "triage")
            self.assertIn("New body", (skill_dir / "SKILL.md").read_text())
            self.assertEqual(
                service.list_skills()[0]["description"], "Triage matching issues")

    def test_load_issue_trigger_includes_issue_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            skill_dir = skills_dir / "triage"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: triage\nx-codee-trigger: issue\n"
                "x-codee-issue-type: story\n---\nBody\n"
            )
            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir

            skill = service.load_skill("triage")

            self.assertEqual(skill["issue_type"], "story")

    def test_delete_skill_removes_directory_and_pushes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            skill_dir = skills_dir / "triage"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: triage\n---\nBody\n")
            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir

            with patch.object(service, "_git_push", return_value=(True, "")) as push:
                deleted, pushed, message = service.delete_skill("triage")

            self.assertTrue(deleted)
            self.assertTrue(pushed)
            self.assertEqual(message, "Deleted triage")
            self.assertFalse(skill_dir.exists())
            self.assertEqual(push.call_args.args[0], "skill: delete triage")

    def test_delete_skill_reports_deleted_when_git_push_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            skill_dir = skills_dir / "triage"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: triage\n---\nBody\n")
            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir

            with patch.object(service, "_git_push", return_value=(False, "no upstream")):
                deleted, pushed, message = service.delete_skill("triage")

            self.assertTrue(deleted)
            self.assertFalse(pushed)
            self.assertIn("Git push failed", message)
            self.assertFalse(skill_dir.exists())

    def test_delete_skill_rejects_unknown_slug(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = AdminService.__new__(AdminService)
            service.skills_dir = Path(temporary_directory)

            with patch.object(service, "_git_push") as push:
                deleted, pushed, message = service.delete_skill("missing")

            self.assertFalse(deleted)
            self.assertFalse(pushed)
            self.assertEqual(message, "missing does not exist")
            push.assert_not_called()

    def test_force_run_skill_queues_the_skill_for_the_next_tick(self) -> None:
        # The button on the skill editor goes through here, so this calls the
        # real trigger module rather than a mock: an import that names the
        # wrong object breaks only at this call.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            (skills_dir / "nightly").mkdir(parents=True)
            (skills_dir / "nightly" / "SKILL.md").write_text(
                "---\nname: nightly\nx-codee-trigger: cron\n"
                "x-codee-cron: 0 0 * * *\n---\nBody\n")
            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir
            service.context = CodeeMainContext(data_dir=root / ".codee")
            service.context.data_dir.mkdir()

            service.force_run_skill("nightly")

            queued = json.loads(
                (service.context.data_dir / "cron_skill_force.json").read_text())
            self.assertEqual(
                [Path(key).name for key in queued], ["SKILL.md"])
            self.assertIn("nightly", queued[0])

    def test_resolve_skill_slug_matches_name_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            skill_dir = skills_dir / "task-developer"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Task Developer\ndescription: Build tasks\n---\nBody\n"
            )
            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir

            self.assertEqual(
                service.resolve_skill_slug("Task Developer"), "task-developer")
            self.assertEqual(
                service.resolve_skill_slug(" task-developer "), "task-developer")
            self.assertEqual(service.resolve_skill_slug("missing"), "")
            self.assertEqual(service.resolve_skill_slug(""), "")

    def test_generate_workflow_asks_the_agent_for_its_best_model(self) -> None:
        # Inference has no skill frontmatter behind it, so without an explicit
        # model it would land on whatever default the CLI happens to resolve.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "develop"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "After implementation, move the issue to Review.\n"
            )
            agent = Mock()
            agent.best_model.return_value = "claude-opus-5"
            agent.run.return_value = (
                '{"statuses":["Ready","Review"],"transitions":['
                '{"source":"Ready","target":"Review","label":"develop",'
                '"evidence":"After implementation, move the issue to Review."}],'
                '"final_statuses":["Review"]}'
            )
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(settings=Settings(
                coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                service.generate_workflow()

            self.assertEqual(agent.run.call_args.args[2], "claude-opus-5")

    def test_generate_workflow_builds_react_flow_graph_from_issue_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "develop"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "After implementation, move the issue to Review.\n"
            )
            task_develop_dir = skills_dir / "task-develop"
            task_develop_dir.mkdir()
            (task_develop_dir / "SKILL.md").write_text(
                "---\nname: task-develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: task\n---\n"
                "When implementation is complete, move the task to Review.\n"
            )
            review_dir = skills_dir / "review"
            review_dir.mkdir()
            (review_dir / "SKILL.md").write_text(
                "---\nname: review\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Review]\n"
                "x-codee-issue-type: story\n---\n"
                "After approval, move the issue to Done.\n"
                "When fixes are needed, move the issue to Ready.\n"
            )
            agent = Mock()
            agent.run.side_effect = [
                '```json\n{"statuses":["Ready","Review","Done"],'
                '"transitions":[{"source":"Ready","target":"Review",'
                '"label":"develop","evidence":"After implementation, move the issue to Review."},'
                '{"source":"Review","target":"Done","label":"review",'
                '"evidence":"After approval, move the issue to Done."},'
                '{"source":"Review","target":"Ready","label":"review",'
                '"evidence":"When fixes are needed, move the issue to Ready."}],'
                '"final_statuses":["Done"]}\n```',
                '{"statuses":["Ready","Review"],"transitions":['
                '{"source":"Ready","target":"Review","label":"task-develop",'
                '"evidence":"When implementation is complete, move the task to Review."}],'
                '"final_statuses":["Review"]}',
            ]
            agent_type = Mock(return_value=agent)
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(settings=Settings(
                coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: agent_type,
            }):
                workflows = service.generate_workflow()

            workflow = workflows["story"]

            self.assertEqual(
                [
                    node["data"]["label"] for node in workflow["nodes"]
                    if node["data"]["label"]
                ],
                ["Ready", "Review", "Done"],
            )
            self.assertEqual(len(workflow["edges"]), 4)
            self.assertEqual(
                workflow["edges"][0]["data"]["skills"],
                ["develop"],
            )
            self.assertEqual(
                workflow["edges"][0]["data"]["reasons"],
                ["After implementation, move the issue to Review."],
            )
            self.assertEqual(
                workflow["edges"][0]["label"],
                "develop",
            )
            self.assertEqual(
                workflow["edges"][0]["ariaLabel"],
                "Ready to Review via develop",
            )
            self.assertEqual(workflow["nodes"][1]
                             ["position"], {"x": 440, "y": 0})
            self.assertEqual(workflow["nodes"][1]["sourcePosition"], "right")
            route_node = workflow["nodes"][3]
            self.assertEqual(route_node["id"], "return-route-0")
            self.assertGreater(route_node["position"]["y"], 0)
            self.assertEqual(route_node["style"]["opacity"], 1)
            self.assertIn("workflow-route-node--return",
                          route_node["className"])
            self.assertIs(workflow["edges"][2]["animated"], True)
            self.assertEqual(
                workflow["edges"][2]["className"],
                "workflow-edge workflow-edge--return workflow-edge--g2",
            )
            # Both halves of the return detour share the hover group, so
            # hovering either one lights the whole arrow.
            self.assertIn("workflow-edge--g2",
                          workflow["edges"][3]["className"])
            self.assertEqual(
                workflow["edges"][2]["style"]["strokeDasharray"], "8 6")
            self.assertEqual(workflow["edges"][2]["target"], route_node["id"])
            self.assertEqual(workflow["edges"][2]["label"], "review")
            self.assertEqual(
                workflow["edges"][2]["data"]["reasons"],
                ["When fixes are needed, move the issue to Ready."],
            )
            self.assertEqual(workflow["edges"][3]["source"], route_node["id"])
            # Both halves of a routed transition explain themselves: the
            # pointer can land on either one.
            self.assertEqual(
                workflow["edges"][3]["data"]["reasons"],
                workflow["edges"][2]["data"]["reasons"],
            )
            self.assertEqual(workflow["edges"][3]["target"], "status-0")
            self.assertNotIn("label", workflow["edges"][3])
            self.assertNotIn("markerEnd", workflow["edges"][2])
            self.assertEqual(workflow["warnings"], [])
            story_prompt = agent.run.call_args_list[0].args[0]
            task_prompt = agent.run.call_args_list[1].args[0]
            self.assertIn("Build the story workflow", story_prompt)
            self.assertIn("move the issue to Review", story_prompt)
            self.assertNotIn("task-develop", story_prompt)
            self.assertIn("Build the task workflow", task_prompt)
            self.assertIn("task-develop", task_prompt)
            self.assertNotIn("## Skill: develop", task_prompt)
            self.assertEqual(
                workflows["task"]["edges"][0]["data"]["skills"],
                ["task-develop"],
            )

    def test_generate_workflow_warns_when_final_status_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "develop"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "Keep the issue Ready while work remains.\n"
            )
            agent = Mock()
            agent.run.return_value = (
                '{"statuses":["Ready"],"transitions":['
                '{"source":"Ready","target":"Ready","label":"develop",'
                '"evidence":"Keep the issue Ready while work remains."}],'
                '"final_statuses":[]}'
            )
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                workflow = service.generate_workflow()["story"]

            self.assertEqual(workflow["warnings"], [
                "No final human-handoff status is defined in the issue skill workflow.",
            ])

    def test_generate_workflow_removes_skill_edge_bypassing_intermediate_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "story-developer"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: story-developer\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\n"
                "x-codee-issue-status: ['[AI] Ready for development', '[AI] In Progress']\n"
                "x-codee-issue-type: story\n"
                "---\nMake sure story in [AI] In Progress status before doing any work.\n"
                "After task is complete move it to [AI] CR Needed.\n"
            )
            agent = Mock()
            agent.run.return_value = (
                '{"statuses":["[AI] Ready for development","[AI] In Progress",'
                '"[AI] CR Needed"],"transitions":['
                '{"source":"[AI] Ready for development","target":"[AI] In Progress",'
                '"label":"story-developer","evidence":"Make sure story in [AI] In Progress status before doing any work."},'
                '{"source":"[AI] In Progress","target":"[AI] CR Needed",'
                '"label":"story-developer","evidence":"After task is complete move it to [AI] CR Needed."},'
                '{"source":"[AI] Ready for development","target":"[AI] CR Needed",'
                '"label":"story-developer","evidence":"After task is complete move it to [AI] CR Needed."}],'
                '"final_statuses":["[AI] CR Needed"]}'
            )
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                workflow = service.generate_workflow()["story"]

            self.assertEqual(
                [(edge["source"], edge["target"])
                 for edge in workflow["edges"]],
                [("status-0", "status-1"), ("status-1", "status-2")],
            )
            self.assertIn(
                "do not emit a direct transition that bypasses it",
                agent.run.call_args.args[0],
            )

    def test_generate_workflow_rejects_a_transition_that_moves_a_subtask(self) -> None:
        """A story skill moves its subtasks too, and those are not story statuses."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "story-developer"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: story-developer\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "Move each subtask to Subtask Review once its pull request is open.\n"
                "Move the story to CR Needed when every subtask is implemented.\n"
            )
            agent = Mock()
            agent.run.side_effect = [
                '{"statuses":["Ready","Subtask Review","CR Needed"],'
                '"transitions":[{"source":"Ready","target":"Subtask Review",'
                '"label":"story-developer","evidence":"Move each subtask to '
                'Subtask Review once its pull request is open."}],'
                '"final_statuses":["CR Needed"]}',
                '{"statuses":["Ready","CR Needed"],'
                '"transitions":[{"source":"Ready","target":"CR Needed",'
                '"label":"story-developer","evidence":"Move the story to CR '
                'Needed when every subtask is implemented."}],'
                '"final_statuses":["CR Needed"]}',
            ]
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                workflow = service.generate_workflow()["story"]

            self.assertEqual(agent.run.call_count, 2)
            self.assertIn(
                "moves the subtask rather than the story",
                agent.run.call_args.args[0],
            )
            self.assertIn(
                "The graph is the lifecycle of the story work item alone",
                agent.run.call_args_list[0].args[0],
            )
            self.assertEqual(
                [node["data"]["label"] for node in workflow["nodes"]],
                ["Ready", "CR Needed"],
            )

    def test_generate_workflow_drops_a_status_only_a_subtask_is_moved_to(self) -> None:
        """A status no story sentence moves is left to the subtask's own graph."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "story-developer"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: story-developer\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "Move each subtask to Subtask Review once its pull request is open.\n"
                "Move the story to CR Needed when every subtask is implemented.\n"
            )
            agent = Mock()
            agent.run.return_value = (
                '{"statuses":["Ready","Subtask Review","CR Needed"],'
                '"transitions":[{"source":"Ready","target":"CR Needed",'
                '"label":"story-developer","evidence":"Move the story to CR '
                'Needed when every subtask is implemented."}],'
                '"final_statuses":["CR Needed"],'
                '"human_actions":[{"status":"Subtask Review",'
                '"action":"Review the pull request."}]}'
            )
            progress: list[str] = []
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                workflow = service.generate_workflow(
                    report=progress.append)["story"]

            self.assertEqual(agent.run.call_count, 1)
            self.assertEqual(
                [node["data"]["label"] for node in workflow["nodes"]],
                ["Ready", "CR Needed"],
            )
            self.assertIn(
                "Left 1 status off the Story workflow, moved on another "
                "work item: Subtask Review.",
                progress,
            )

    def test_generate_workflow_retries_unsupported_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "story-planner"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: story-planner\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\n"
                "x-codee-issue-status: ['[AI] Decomposition needed']\n"
                "x-codee-issue-type: story\n---\n"
                "After planning, move the story to [AI] Ready for human review.\n"
            )
            agent = Mock()
            agent.run.side_effect = [
                '{"statuses":["[AI] Decomposition needed",'
                '"[AI] Ready for development","[AI] Ready for human review"],'
                '"transitions":[{"source":"[AI] Decomposition needed",'
                '"target":"[AI] Ready for development","label":"",'
                '"evidence":""}],"final_statuses":[]}',
                '{"statuses":["[AI] Decomposition needed",'
                '"[AI] Ready for development","[AI] Ready for human review"],'
                '"transitions":[{'
                '"source":"[AI] Decomposition needed",'
                '"target":"[AI] Ready for human review",'
                '"label":"story-planner",'
                '"evidence":"After planning, move the story to [AI] Ready for human review."}],'
                '"final_statuses":["[AI] Ready for human review"]}',
            ]
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                workflow = service.generate_workflow()["story"]

            self.assertEqual(agent.run.call_count, 2)
            retry_prompt = agent.run.call_args.args[0]
            self.assertIn(
                'transition 1 ("[AI] Decomposition needed" -> '
                '"[AI] Ready for development", label "") is labelled with a '
                'skill that was not supplied',
                retry_prompt,
            )
            self.assertIn('the label must be one of "story-planner"',
                          retry_prompt)
            route_out, route_in = workflow["nodes"][3:]
            self.assertEqual(route_out["id"], "forward-route-0-out")
            self.assertEqual(route_in["id"], "forward-route-0-in")
            self.assertIn("workflow-route-node--forward",
                          route_out["className"])
            self.assertEqual(route_out["sourcePosition"], "right")
            self.assertEqual(route_out["targetPosition"], "left")
            self.assertEqual(route_in["sourcePosition"], "right")
            self.assertEqual(route_in["targetPosition"], "left")
            self.assertEqual(route_out["style"]["width"], 1)
            self.assertEqual(route_out["style"]["height"], 1)
            self.assertLess(route_out["position"]["y"], 0)
            self.assertEqual(
                route_out["position"]["y"], route_in["position"]["y"])
            self.assertEqual(workflow["edges"][0]["label"], "story-planner")
            self.assertEqual(workflow["edges"][0]["target"], route_out["id"])
            self.assertEqual(workflow["edges"][1]["source"], route_out["id"])
            self.assertEqual(workflow["edges"][1]["target"], route_in["id"])
            self.assertNotIn("label", workflow["edges"][1])
            self.assertEqual(workflow["edges"][2]["source"], route_in["id"])
            self.assertEqual(workflow["edges"][2]["target"], "status-2")

    def test_generate_workflow_accepts_a_chained_transition(self) -> None:
        """A skill that hands the issue on through a status it does not own.

        The bounce-back rules in the real skills read "move to X first, then
        to Y", so the second hop starts from a status that is not one of the
        skill's entry statuses. Rejecting it burned the single retry.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "develop"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "Move the issue to Human Review, then to CR Needed.\n"
            )
            agent = Mock()
            agent.run.return_value = (
                '{"statuses":["Ready","Human Review","CR Needed"],'
                '"transitions":[{"source":"Ready","target":"Human Review",'
                '"label":"develop","evidence":"Move the issue to Human Review, '
                'then to CR Needed."},'
                '{"source":"Human Review","target":"CR Needed",'
                '"label":"develop","evidence":"Move the issue to Human Review, '
                'then to CR Needed."}],"final_statuses":["CR Needed"]}'
            )
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                workflow = service.generate_workflow()["story"]

            self.assertEqual(agent.run.call_count, 1)
            self.assertEqual(
                [(edge["source"], edge["target"])
                 for edge in workflow["edges"]],
                [("status-0", "status-1"), ("status-1", "status-2")],
            )

    def test_generate_workflow_reports_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "develop"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "Move the issue to Review when work is done.\n"
            )
            agent = Mock()
            agent.run.side_effect = [
                '{"statuses":["Ready","Review"],"transitions":['
                '{"source":"Ready","target":"Review","label":"develop",'
                '"evidence":"Work the issue until it is done."}],'
                '"final_statuses":["Review"]}',
                '{"statuses":["Ready","Review"],"transitions":['
                '{"source":"Ready","target":"Review","label":"develop",'
                '"evidence":"Move the issue to Review when work is done."}],'
                '"final_statuses":["Review"]}',
            ]
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))
            progress: list[str] = []

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                service.generate_workflow(report=progress.append)
                service.generate_workflow(report=progress.append)

            self.assertEqual(progress[0], (
                "Generating workflow for work item Story from "
                "1 issue-trigger skill..."
            ))
            self.assertEqual(progress[1], (
                "Detected error in the workflow: transition evidence is not "
                "an exact quote from develop, asking agent to fix..."
            ))
            self.assertEqual(
                progress[2], "Story workflow: 2 statuses and 1 transition.")
            self.assertEqual(
                progress[3],
                "No issue-trigger skills for work item Task.")
            self.assertEqual(
                progress[-1], "Skills are unchanged: showing the stored workflow.")

    def test_generate_workflow_reports_every_problem_in_one_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "develop"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "Move the issue to Review when work is done.\n"
            )
            agent = Mock()
            agent.run.side_effect = [
                '{"statuses":["Ready","Review"],"transitions":['
                '{"source":"Blocked","target":"Review","label":"develop",'
                '"evidence":"Move the issue to Review when work is done."},'
                '{"source":"Ready","target":"Review","label":"develop",'
                '"evidence":"Work the issue until it is done."}],'
                '"final_statuses":["Done"]}',
                '{"statuses":["Ready","Review"],"transitions":['
                '{"source":"Ready","target":"Review","label":"develop",'
                '"evidence":"Move the issue to Review when work is done."}],'
                '"final_statuses":["Review"]}',
            ]
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                service.generate_workflow()

            retry_prompt = agent.run.call_args.args[0]
            self.assertIn('transition 1 ("Blocked" -> "Review", label '
                          '"develop") uses "Blocked", which is missing from '
                          'statuses "Ready", "Review"', retry_prompt)
            self.assertIn('transition 2 ("Ready" -> "Review", label "develop") '
                          'quotes "Work the issue until it is done.", which '
                          'does not appear in "develop"', retry_prompt)
            self.assertIn('final_statuses contains "Done", missing from '
                          'statuses "Ready", "Review"', retry_prompt)

    def test_generate_workflow_names_the_entry_statuses_a_source_missed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "review"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: review\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Review]\n"
                "x-codee-issue-type: story\n---\n"
                "Move the issue to Done after approval.\n"
            )
            agent = Mock()
            agent.run.side_effect = [
                '{"statuses":["Ready","Review","Done"],"transitions":['
                '{"source":"Ready","target":"Done","label":"review",'
                '"evidence":"Move the issue to Done after approval."}],'
                '"final_statuses":["Done"]}',
                '{"statuses":["Ready","Review","Done"],"transitions":['
                '{"source":"Review","target":"Done","label":"review",'
                '"evidence":"Move the issue to Done after approval."}],'
                '"final_statuses":["Done"]}',
            ]
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                service.generate_workflow()

            self.assertIn(
                'transition 1 ("Ready" -> "Done", label "review") starts from '
                'a status "review" never has the issue in: its entry statuses '
                'are "Review" and no other transition of that skill moves the '
                'issue to "Ready".',
                agent.run.call_args.args[0],
            )

    def test_generate_workflow_warns_when_statuses_have_no_edges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "develop"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\n"
                "x-codee-issue-status: [Ready, In Progress, Review]\n"
                "x-codee-issue-type: story\n---\n"
                "Work on issues in the configured statuses.\n"
            )
            agent = Mock()
            agent.run.return_value = (
                '{"statuses":["Ready","In Progress","Review"],'
                '"transitions":[],"final_statuses":["Review"]}'
            )
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                workflow = service.generate_workflow()["story"]

            self.assertEqual(workflow["warnings"], [
                "Workflow statuses are disconnected: no status transitions were found.",
            ])
            self.assertEqual(workflow["edges"], [])
            self.assertTrue(all(
                "workflow-node--disconnected" in node["className"]
                for node in workflow["nodes"]
            ))

    def test_generate_workflow_marks_a_human_status_on_its_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "develop"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "Move the issue to In Progress when work starts.\n"
            )
            agent = Mock()
            agent.run.return_value = (
                '{"statuses":["Ready","In Progress","Done"],"transitions":['
                '{"source":"Ready","target":"In Progress","label":"develop",'
                '"evidence":"Move the issue to In Progress when work starts."}],'
                '"final_statuses":["Done"]}'
            )
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                workflow = service.generate_workflow()["story"]

            self.assertEqual(workflow["warnings"], [])
            flagged = [
                node["data"]["label"] for node in workflow["nodes"]
                if "workflow-node--human" in node.get("className", "")
            ]
            self.assertEqual(flagged, ["In Progress"])

    def test_generate_workflow_carries_the_human_action_to_the_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "develop"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "Move the issue to In Progress when work starts.\n"
            )
            agent = Mock()
            agent.run.return_value = (
                '{"statuses":["Ready","In Progress","Done"],"transitions":['
                '{"source":"Ready","target":"In Progress","label":"develop",'
                '"evidence":"Move the issue to In Progress when work starts."}],'
                '"final_statuses":["Done"],"human_actions":[{'
                '"status":"In Progress","action":"Finish the developer\'s '
                'work and move the story on."}]}'
            )
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                workflow = service.generate_workflow()["story"]

            node = next(
                node for node in workflow["nodes"]
                if node["data"]["label"] == "In Progress"
            )
            self.assertEqual(
                node["data"]["humanAction"],
                "Finish the developer's work and move the story on.",
            )
            # Quoted for CSS, with the apostrophe escaped so it cannot end the
            # string the tooltip rule reads.
            self.assertEqual(
                node["style"]["--codee-human-action"],
                "'Finish the developer\\'s work and move the story on.'",
            )
            # Done is worked by nobody the graph can name: no skill picks it
            # up, and a final status is not waiting on a person either.
            done = next(node for node in workflow["nodes"]
                        if node["data"]["label"] == "Done")
            self.assertNotIn("style", done)

    def test_generate_workflow_draws_a_human_transition_in_yellow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "planner"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: planner\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Planning]\n"
                "x-codee-issue-type: story\n---\n"
                "Move the story to Plan review when the plan is posted.\n"
                "Never move the story to Ready for development yourself. "
                "Approving the plan is a human decision.\n"
            )
            agent = Mock()
            agent.run.return_value = (
                '{"statuses":["Planning","Plan review","Ready for development"],'
                '"transitions":[{"source":"Planning","target":"Plan review",'
                '"label":"planner","evidence":"Move the story to Plan review '
                'when the plan is posted."}],"final_statuses":[],'
                '"human_actions":[{"status":"Plan review",'
                '"action":"Approve the plan."}],'
                '"human_transitions":[{"source":"Plan review",'
                '"target":"Ready for development","evidence":"Never move the '
                'story to Ready for development yourself."}]}'
            )
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                workflow = service.generate_workflow()["story"]

            labels = {node["id"]: node["data"]["label"]
                      for node in workflow["nodes"]}
            by_pair = {
                (labels[edge["source"]], labels[edge["target"]]): edge
                for edge in workflow["edges"]
            }
            human = by_pair[("Plan review", "Ready for development")]
            self.assertEqual(
                human["style"]["stroke"], WORKFLOW_HUMAN_EDGE_COLOR)
            self.assertEqual(
                human["markerEnd"]["color"], WORKFLOW_HUMAN_EDGE_COLOR)
            self.assertIn("workflow-edge--human", human["className"])
            # A person's arrow names no skill, so the click menu stays empty,
            # but the quote behind it still explains the move on hover.
            self.assertEqual(human["data"]["skills"], [])
            self.assertEqual(
                human["data"]["reasons"],
                ["Never move the story to Ready for development yourself."],
            )
            skill_edge = by_pair[("Planning", "Plan review")]
            self.assertEqual(skill_edge["style"]["stroke"], "#167d5a")
            self.assertNotIn("workflow-edge--human", skill_edge["className"])
            self.assertEqual(workflow["warnings"], [
                "No final human-handoff status is defined in the issue skill workflow.",
            ])

    def test_generate_workflow_rejects_a_human_transition_a_skill_makes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "develop"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "Move the issue to In Progress when work starts.\n"
            )
            agent = Mock()
            agent.run.side_effect = [
                '{"statuses":["Ready","In Progress"],"transitions":[],'
                '"final_statuses":[],"human_actions":[],'
                '"human_transitions":[{"source":"Ready","target":"In Progress",'
                '"evidence":"Move the issue to In Progress when work starts."}]}',
                '{"statuses":["Ready","In Progress"],"transitions":[],'
                '"final_statuses":[],"human_actions":[],'
                '"human_transitions":[]}',
            ]
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                service.generate_workflow()

            self.assertEqual(agent.run.call_count, 2)
            self.assertIn(
                "human transition source Ready is an entry status of a skill",
                agent.run.call_args.args[0],
            )

    def test_generate_workflow_rejects_a_human_action_for_an_unknown_status(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "develop"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "Move the issue to In Progress when work starts.\n"
            )
            agent = Mock()
            agent.run.side_effect = [
                '{"statuses":["Ready","In Progress"],"transitions":[],'
                '"final_statuses":[],"human_actions":[{"status":"Blocked",'
                '"action":"Unblock the story."}]}',
                '{"statuses":["Ready","In Progress"],"transitions":[],'
                '"final_statuses":[],"human_actions":[]}',
            ]
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service.CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                service.generate_workflow()

            self.assertEqual(agent.run.call_count, 2)
            self.assertIn(
                "each human action must name a declared status",
                agent.run.call_args.args[0],
            )

    def test_generate_workflow_returns_empty_without_issue_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = AdminService.__new__(AdminService)
            service.skills_dir = root
            service.data_dir = root / ".codee"

            self.assertEqual(service.generate_workflow(), {
                "story": {"nodes": [], "edges": [], "warnings": []},
                "task": {"nodes": [], "edges": [], "warnings": []},
            })

    def test_generate_workflow_is_cached_until_forced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = AdminService.__new__(AdminService)
            service.skills_dir = root / ".claude" / "skills"
            service.data_dir = root / ".codee"
            generated = _empty_workflow()

            with patch.object(service, "_generate_workflow",
                              return_value=generated) as generate:
                # Equal rather than identical: the graph handed to the page is
                # the cached one plus what the page reads off the skills.
                self.assertEqual(service.generate_workflow(), generated)
                self.assertEqual(service.generate_workflow(), generated)
                self.assertEqual(
                    service.generate_workflow(force=True), generated)

            self.assertEqual(generate.call_count, 2)

    def test_generate_workflow_reuses_graph_stored_by_an_earlier_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = _write_issue_skill(root)
            generated = _empty_workflow()
            generated["story"]["warnings"] = ["stored"]

            first = AdminService.__new__(AdminService)
            first.skills_dir = skills_dir
            first.data_dir = root / ".codee"
            with patch.object(first, "_generate_workflow", return_value=generated):
                first.generate_workflow()

            # A restart starts from an empty in-memory cache, so only the file
            # written above can spare it another coding-agent run.
            restarted = AdminService.__new__(AdminService)
            restarted.skills_dir = skills_dir
            restarted.data_dir = root / ".codee"
            with patch.object(restarted, "_generate_workflow") as generate:
                workflow = restarted.generate_workflow()

            generate.assert_not_called()
            self.assertEqual(workflow["story"]["warnings"], ["stored"])

    def test_generate_workflow_ignores_stored_graph_after_a_skill_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = _write_issue_skill(root)

            first = AdminService.__new__(AdminService)
            first.skills_dir = skills_dir
            first.data_dir = root / ".codee"
            with patch.object(first, "_generate_workflow",
                              return_value=_empty_workflow()):
                first.generate_workflow()

            (skills_dir / "develop" / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "After implementation, move the issue to Done.\n"
            )
            regenerated = _empty_workflow()
            regenerated["story"]["warnings"] = ["regenerated"]
            restarted = AdminService.__new__(AdminService)
            restarted.skills_dir = skills_dir
            restarted.data_dir = root / ".codee"
            with patch.object(restarted, "_generate_workflow",
                              return_value=regenerated) as generate:
                workflow = restarted.generate_workflow()

            self.assertEqual(generate.call_count, 1)
            self.assertEqual(workflow["story"]["warnings"], ["regenerated"])

    def test_generate_workflow_ignores_a_corrupt_stored_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = _write_issue_skill(root)
            data_dir = root / ".codee"
            data_dir.mkdir()
            (data_dir / "workflow.json").write_text("{ not json")

            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir
            service.data_dir = data_dir
            generated = _empty_workflow()
            with patch.object(service, "_generate_workflow",
                              return_value=generated) as generate:
                self.assertEqual(service.generate_workflow(), generated)

            self.assertEqual(generate.call_count, 1)


    def test_generate_workflow_regenerates_a_graph_from_an_older_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = _write_issue_skill(root)
            data_dir = root / ".codee"
            data_dir.mkdir()
            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir
            service.data_dir = data_dir
            with patch.object(service, "_generate_workflow",
                              return_value=_empty_workflow()):
                service.generate_workflow()
            stored = json.loads((data_dir / "workflow.json").read_text())
            self.assertEqual(stored["version"], WORKFLOW_CACHE_VERSION)
            stored["version"] = WORKFLOW_CACHE_VERSION - 1
            (data_dir / "workflow.json").write_text(json.dumps(stored))

            regenerated = _empty_workflow()
            restarted = AdminService.__new__(AdminService)
            restarted.skills_dir = skills_dir
            restarted.data_dir = data_dir
            with patch.object(restarted, "_generate_workflow",
                              return_value=regenerated) as generate:
                self.assertEqual(restarted.generate_workflow(), regenerated)

            self.assertEqual(generate.call_count, 1)


class AdminServiceWorkflowAgentTest(unittest.TestCase):
    """What the graph says about the agent working each status."""

    def _service(self, root: Path, extra_frontmatter: str = "") -> AdminService:
        skills_dir = root / ".claude" / "skills"
        skill_dir = skills_dir / "develop"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: develop\ndisable-model-invocation: true\n"
            "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
            f"x-codee-issue-type: story\n{extra_frontmatter}---\n"
            "After implementation, move the issue to Review.\n"
        )
        service = AdminService.__new__(AdminService)
        service.root = root
        service.skills_dir = skills_dir
        service.data_dir = root / ".codee"
        service.context = Mock(
            settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))
        return service

    def _story_nodes(self, service: AdminService) -> dict[str, dict]:
        agent = Mock()
        agent.run.return_value = (
            '{"statuses":["Ready","Review"],"transitions":['
            '{"source":"Ready","target":"Review","label":"develop",'
            '"evidence":"After implementation, move the issue to Review."}],'
            '"final_statuses":["Review"]}'
        )
        with patch.dict("codee.admin_service.CODING_AGENTS", {
            CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
        }):
            workflow = service.generate_workflow()
        return {node["data"]["label"]: node
                for node in workflow["story"]["nodes"]}

    def test_a_status_a_skill_picks_up_names_its_agent_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(
                Path(temporary_directory),
                "model: claude-opus-5\nx-codee-agent: codex\n")

            nodes = self._story_nodes(service)

            self.assertIn("workflow-node--agent", nodes["Ready"]["className"])
            # One CSS string with `\A` breaks in it: the tooltip is a single
            # `::after` whose text the node carries as a custom property.
            self.assertEqual(
                nodes["Ready"]["style"]["--codee-agent-run"],
                "'AI agent: Codex\\A Model: claude-opus-5\\A Skill: develop'",
            )

    def test_the_model_is_written_on_the_node_under_the_status_name(
            self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(
                Path(temporary_directory),
                "model: claude-opus-5\nx-codee-agent: codex\n")

            nodes = self._story_nodes(service)

            self.assertEqual(
                nodes["Ready"]["style"]["--codee-node-model"],
                "'claude-opus-5'",
            )

    def test_a_skill_that_names_neither_falls_back_to_what_runs_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = self._service(root)
            (root / ".codee").mkdir()
            save_settings(root / ".codee",
                          Settings(coding_agent=CodingAgent.CODEX))

            nodes = self._story_nodes(service)

            # The reading the executor takes: the agent Settings selects, and
            # a model left to that agent's CLI.
            self.assertEqual(
                nodes["Ready"]["style"]["--codee-agent-run"],
                "'AI agent: Codex\\A Model: agent default\\A Skill: develop'",
            )

    def test_a_status_no_skill_picks_up_says_nothing_about_an_agent(
            self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            nodes = self._story_nodes(service)

            self.assertNotIn("workflow-node--agent", nodes["Review"]["className"])
            self.assertNotIn("style", nodes["Review"])

    def test_changing_the_agent_needs_no_second_inference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = self._service(root)
            self._story_nodes(service)
            save_settings(root / ".codee",
                          Settings(coding_agent=CodingAgent.CODEX))

            # Which agent works a status is not something the graph was
            # inferred from, so picking another one must not cost the minutes
            # a fresh inference run takes.
            with patch.object(service, "_generate_workflow") as generate:
                workflow = service.generate_workflow()

            generate.assert_not_called()
            ready = next(node for node in workflow["story"]["nodes"]
                         if node["data"]["label"] == "Ready")
            self.assertIn("AI agent: Codex",
                          ready["style"]["--codee-agent-run"])


class AdminServiceWorkflowGenerationTest(unittest.TestCase):
    """The generation run the Workflow page attaches to."""

    @staticmethod
    def _await(service: AdminService) -> WorkflowGeneration:
        """Poll until the run is over, as the page's watcher does."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = service.workflow_generation_status()
            if not status.running:
                return status
            time.sleep(0.01)
        raise AssertionError("workflow generation never finished")

    def test_a_second_visit_attaches_to_the_run_already_going(self) -> None:
        service = _generating_service()
        release = threading.Event()
        generated = _empty_workflow()

        def generate(force: bool, report) -> dict:
            report("working")
            release.wait(5)
            return generated

        with patch.object(service, "generate_workflow",
                          side_effect=generate) as generating:
            service.start_workflow_generation()
            while not service.workflow_generation_status().progress:
                time.sleep(0.01)
            # The visit that comes back mid-generation sees the same run,
            # with the progress it has made so far.
            attached = service.start_workflow_generation()
            self.assertTrue(attached.running)
            self.assertEqual(attached.progress, ("working",))
            release.set()
            finished = self._await(service)

        self.assertEqual(generating.call_count, 1)
        self.assertIs(finished.workflow, generated)
        self.assertEqual(finished.error, "")

    def test_the_finished_graph_is_there_for_the_next_visit(self) -> None:
        service = _generating_service()
        generated = _empty_workflow()

        with patch.object(service, "generate_workflow",
                          return_value=generated):
            service.start_workflow_generation()
            self._await(service)
            # Coming back after it finished shows the graph straight away
            # rather than an empty page behind a spinner.
            self.assertIs(
                service.workflow_generation_status().workflow, generated)

    def test_regenerating_takes_the_old_graph_off_the_page(self) -> None:
        service = _generating_service()
        release = threading.Event()

        with patch.object(service, "generate_workflow",
                          return_value=_empty_workflow()):
            service.start_workflow_generation()
            self._await(service)

        with patch.object(service, "generate_workflow",
                          side_effect=lambda force, report: release.wait(5)):
            started = service.start_workflow_generation(force=True)
            self.assertTrue(started.running)
            self.assertIsNone(started.workflow)
            release.set()
            self._await(service)

    def test_a_failed_run_ends_and_keeps_the_graph_it_could_not_replace(
            self) -> None:
        service = _generating_service()
        generated = _empty_workflow()

        with patch.object(service, "generate_workflow",
                          return_value=generated):
            service.start_workflow_generation()
            self._await(service)

        with patch.object(service, "generate_workflow",
                          side_effect=RuntimeError("agent said no")):
            service.start_workflow_generation()
            failed = self._await(service)

        # A run that ends without saying so would leave every later visit
        # watching a generation that is not happening.
        self.assertFalse(failed.running)
        self.assertEqual(failed.error, "agent said no")
        self.assertIs(failed.workflow, generated)

    def test_regenerating_during_a_run_is_not_dropped(self) -> None:
        service = _generating_service()
        gates = [threading.Event(), threading.Event()]
        generated = _empty_workflow()
        forced = []

        def generate(force: bool, report) -> dict:
            forced.append(force)
            gates[len(forced) - 1].wait(5)
            return generated

        with patch.object(service, "generate_workflow", side_effect=generate):
            # The page visit's own run answers from the cache, so a
            # Regenerate arriving while it is going has to run afterwards
            # rather than be swallowed by a run it cannot influence.
            service.start_workflow_generation()
            while not forced:
                time.sleep(0.01)
            service.start_workflow_generation(force=True)
            gates[0].set()
            deadline = time.monotonic() + 5
            while len(forced) < 2 and time.monotonic() < deadline:
                # The queued run takes over without the page ever seeing the
                # run in flight stop, so it keeps watching.
                self.assertTrue(service.workflow_generation_status().running)
                time.sleep(0.01)
            gates[1].set()
            self._await(service)

        self.assertEqual(forced, [False, True])

    def test_regenerating_during_a_regeneration_does_not_run_twice(
            self) -> None:
        service = _generating_service()
        release = threading.Event()

        def generate(force: bool, report) -> dict:
            report("working")
            release.wait(5)
            return _empty_workflow()

        with patch.object(service, "generate_workflow",
                          side_effect=generate) as generating:
            service.start_workflow_generation(force=True)
            while not service.workflow_generation_status().progress:
                time.sleep(0.01)
            # The fresh graph being built is already what the second click
            # asks for; queueing another would cost a second agent run.
            service.start_workflow_generation(force=True)
            release.set()
            self._await(service)

        self.assertEqual(generating.call_count, 1)

    def test_a_run_that_failed_can_be_started_again(self) -> None:
        service = _generating_service()
        generated = _empty_workflow()

        with patch.object(service, "generate_workflow",
                          side_effect=RuntimeError("agent said no")):
            service.start_workflow_generation()
            self._await(service)

        with patch.object(service, "generate_workflow",
                          return_value=generated):
            service.start_workflow_generation()
            retried = self._await(service)

        self.assertEqual(retried.error, "")
        self.assertIs(retried.workflow, generated)


class AdminServiceSkillModelTest(unittest.TestCase):
    def _service(self, skills_dir: Path, frontmatter: str) -> AdminService:
        skill_dir = skills_dir / "nightly"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}---\nBody\n")
        service = AdminService.__new__(AdminService)
        service.skills_dir = skills_dir
        return service

    def test_save_writes_the_model_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            service = self._service(skills_dir, "name: nightly\n")

            with patch.object(service, "_write_and_push",
                              return_value=(True, True, "saved")) as write:
                service.save_skill({
                    "slug": "nightly", "name": "nightly", "description": "",
                    "type": "knowledge", "model": "claude-opus-5",
                    "body": "Body",
                })

            frontmatter, _ = parse_skill(write.call_args.args[1])
            self.assertEqual(frontmatter["model"], "claude-opus-5")

    def test_an_empty_model_is_left_out_of_the_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            service = self._service(skills_dir, "name: nightly\n")

            with patch.object(service, "_write_and_push",
                              return_value=(True, True, "saved")) as write:
                service.save_skill({
                    "slug": "nightly", "name": "nightly", "description": "",
                    "type": "knowledge", "model": "  ", "body": "Body",
                })

            frontmatter, _ = parse_skill(write.call_args.args[1])
            self.assertNotIn("model", frontmatter)

    def test_load_returns_the_model_and_keeps_it_out_of_preserved_extras(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            service = self._service(
                skills_dir, "name: nightly\nmodel: claude-opus-5\nlicense: MIT\n")

            skill = service.load_skill("nightly")

            self.assertEqual(skill["model"], "claude-opus-5")
            # Managed keys are rewritten on save, so only `license` is carried over.
            self.assertEqual(skill["extra"], "license: MIT\n")

    def test_load_reports_no_model_when_the_skill_declares_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            service = self._service(skills_dir, "name: nightly\n")

            self.assertEqual(service.load_skill("nightly")["model"], "")

    def test_save_writes_the_agent_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            service = self._service(skills_dir, "name: nightly\n")

            with patch.object(service, "_write_and_push",
                              return_value=(True, True, "saved")) as write:
                service.save_skill({
                    "slug": "nightly", "name": "nightly", "description": "",
                    "type": "knowledge", "model": "", "agent": "codex",
                    "body": "Body",
                })

            frontmatter, _ = parse_skill(write.call_args.args[1])
            self.assertEqual(frontmatter["x-codee-agent"], "codex")

    def test_an_empty_agent_is_left_out_of_the_frontmatter(self) -> None:
        # No agent means the default one from Settings, not an empty code.
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            service = self._service(skills_dir, "name: nightly\n")

            with patch.object(service, "_write_and_push",
                              return_value=(True, True, "saved")) as write:
                service.save_skill({
                    "slug": "nightly", "name": "nightly", "description": "",
                    "type": "knowledge", "model": "", "agent": "  ",
                    "body": "Body",
                })

            frontmatter, _ = parse_skill(write.call_args.args[1])
            self.assertNotIn("x-codee-agent", frontmatter)

    def test_an_agent_codee_cannot_run_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            service = self._service(skills_dir, "name: nightly\n")

            with patch.object(service, "_write_and_push") as write:
                saved, _, message, _ = service.save_skill({
                    "slug": "nightly", "name": "nightly", "description": "",
                    "type": "knowledge", "model": "", "agent": "cursor",
                    "body": "Body",
                })

            self.assertFalse(saved)
            self.assertIn("cursor", message)
            write.assert_not_called()

    def test_load_returns_the_agent_and_keeps_it_out_of_extras(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            service = self._service(
                skills_dir, "name: nightly\nx-codee-agent: Claude Code\n")

            skill = service.load_skill("nightly")

            # Normalized to the stored code, so the editor's picker can show it.
            self.assertEqual(skill["agent"], "claude_code")
            self.assertEqual(skill["extra"], "")

    def test_load_reports_no_agent_when_the_skill_names_an_unknown_one(self) -> None:
        # What the executor does with it too: run the skill on the default agent.
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            service = self._service(
                skills_dir, "name: nightly\nx-codee-agent: cursor\n")

            self.assertEqual(service.load_skill("nightly")["agent"], "")


class AdminServiceSkillExtraFrontmatterTest(unittest.TestCase):
    def _service(self, skills_dir: Path, frontmatter: str) -> AdminService:
        skill_dir = skills_dir / "nightly"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}---\nBody\n")
        service = AdminService.__new__(AdminService)
        service.skills_dir = skills_dir
        return service

    def _save(self, service: AdminService, extra: str | None) -> tuple[Mock, tuple]:
        skill: dict[str, str] = {
            "slug": "nightly", "name": "nightly", "description": "",
            "type": "knowledge", "model": "", "body": "Body",
        }
        if extra is not None:
            skill["extra"] = extra
        with patch.object(service, "_write_and_push",
                          return_value=(True, True, "saved")) as write:
            result = service.save_skill(skill)
        return write, result

    def test_save_writes_the_extra_fields_into_the_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(
                Path(temporary_directory), "name: nightly\n")

            write, _ = self._save(
                service, "allowed-tools: Bash\ncompatibility: Claude Code\n")

            frontmatter, _ = parse_skill(write.call_args.args[1])
            self.assertEqual(frontmatter["allowed-tools"], "Bash")
            self.assertEqual(frontmatter["compatibility"], "Claude Code")

    def test_empty_extra_fields_drop_what_the_file_carried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(
                Path(temporary_directory), "name: nightly\nlicense: MIT\n")

            write, _ = self._save(service, "")

            frontmatter, _ = parse_skill(write.call_args.args[1])
            self.assertNotIn("license", frontmatter)

    def test_a_caller_that_omits_extra_keeps_the_fields_on_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(
                Path(temporary_directory), "name: nightly\nlicense: MIT\n")

            write, _ = self._save(service, None)

            frontmatter, _ = parse_skill(write.call_args.args[1])
            self.assertEqual(frontmatter["license"], "MIT")

    def test_invalid_yaml_is_reported_and_nothing_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(
                Path(temporary_directory), "name: nightly\n")

            write, (saved, _, message, slug) = self._save(
                service, "allowed-tools")

            write.assert_not_called()
            self.assertFalse(saved)
            self.assertIn("key: value", message)
            self.assertEqual(slug, "nightly")

    def test_a_field_that_has_its_own_control_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(
                Path(temporary_directory), "name: nightly\n")

            write, (saved, _, message, _) = self._save(
                service, "model: claude-opus-5\n")

            write.assert_not_called()
            self.assertFalse(saved)
            self.assertIn("model", message)

    def test_load_returns_the_extra_fields_as_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(
                Path(temporary_directory),
                "name: nightly\nallowed-tools: Bash\nlicense: MIT\n")

            self.assertEqual(service.load_skill("nightly")["extra"],
                             "allowed-tools: Bash\nlicense: MIT\n")

    def test_load_returns_an_empty_string_when_there_are_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(
                Path(temporary_directory), "name: nightly\n")

            self.assertEqual(service.load_skill("nightly")["extra"], "")


class AdminServiceAgentModelsTest(unittest.TestCase):
    def _service(self, agent: CodingAgent) -> AdminService:
        service = AdminService.__new__(AdminService)
        service.context = CodeeMainContext(
            data_dir=Path("/tmp"), settings=Settings(coding_agent=agent))
        service._models_cache = {}
        service._models_lock = threading.Lock()
        return service

    def test_lists_the_configured_agents_models_once_and_caches_them(self) -> None:
        service = self._service(CodingAgent.CLAUDE_CODE)

        with patch.object(ClaudeCodeAgent, "list_models",
                          return_value=[AgentModel(
                              "claude-opus-5", "Claude Opus 5")]
                          ) as list_models:
            first = service.list_agent_models()
            second = service.list_agent_models()

        self.assertEqual(
            first, [{"id": "claude-opus-5", "name": "Claude Opus 5"}])
        self.assertEqual(second, first)
        list_models.assert_called_once()

    def test_a_skills_own_agent_is_asked_instead_of_the_default(self) -> None:
        service = self._service(CodingAgent.CLAUDE_CODE)

        with patch.object(CodexAgent, "list_models",
                          return_value=[AgentModel("gpt-6-astra", "GPT-6 Astra")]), \
                patch.object(ClaudeCodeAgent, "list_models") as claude_models:
            models = service.list_agent_models("codex")

        self.assertEqual(models, [{"id": "gpt-6-astra", "name": "GPT-6 Astra"}])
        claude_models.assert_not_called()

    def test_an_agent_codee_cannot_run_falls_back_to_the_default(self) -> None:
        service = self._service(CodingAgent.CLAUDE_CODE)

        with patch.object(ClaudeCodeAgent, "list_models",
                          return_value=[AgentModel("claude-opus-5", "Claude Opus 5")]):
            self.assertEqual(service.list_agent_models("cursor"),
                             [{"id": "claude-opus-5", "name": "Claude Opus 5"}])

    def test_every_agent_codee_can_run_is_offered(self) -> None:
        service = self._service(CodingAgent.CLAUDE_CODE)

        self.assertEqual(service.list_agents(), [
            {"code": "claude_code", "name": "Claude Code"},
            {"code": "github_copilot", "name": "GitHub Copilot"},
            {"code": "codex", "name": "Codex"},
        ])

    def test_an_agent_that_cannot_be_asked_yields_an_empty_list(self) -> None:
        service = self._service(CodingAgent.GITHUB_COPILOT)

        with patch.object(GitHubCopilotAgent, "list_models",
                          side_effect=RuntimeError("copilot is not logged in")):
            self.assertEqual(service.list_agent_models(), [])


class AdminServiceAgentsFileTest(unittest.TestCase):
    def _service(self, root: Path) -> AdminService:
        service = AdminService.__new__(AdminService)
        service.root = root
        service.skills_dir = root / ".claude" / "skills"
        service.memory_dir = root / "memory"
        service.agents_file = root / "AGENTS.md"
        return service

    def test_load_agents_returns_file_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "AGENTS.md").write_text("---\nnot: frontmatter\n---\nBody\n")
            service = self._service(root)

            self.assertEqual(service.load_agents(),
                             "---\nnot: frontmatter\n---\nBody\n")

    def test_load_agents_returns_empty_string_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            self.assertEqual(service.load_agents(), "")

    def test_save_agents_writes_text_and_pushes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "AGENTS.md").write_text("Old\n")
            service = self._service(root)

            with patch.object(service, "_git_push", return_value=(True, "")) as push:
                saved, pushed, message = service.save_agents("New rules\n")

            self.assertTrue(saved)
            self.assertTrue(pushed)
            self.assertIn("AGENTS.md", message)
            self.assertEqual((root / "AGENTS.md").read_text(), "New rules\n")
            push.assert_called_once_with("agents: update AGENTS.md")

    def test_git_push_stages_agents_file_only_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = self._service(root)

            with patch("codee.admin_service.subprocess.run") as run:
                run.return_value = Mock(returncode=0, stdout="", stderr="")
                service._git_push("skill: update triage")
                without_agents = run.call_args_list[0].args[0]

                (root / "AGENTS.md").write_text("Rules\n")
                run.reset_mock()
                service._git_push("agents: update AGENTS.md")
                with_agents = run.call_args_list[0].args[0]

            self.assertNotIn(str(root / "AGENTS.md"), without_agents)
            self.assertIn(str(root / "AGENTS.md"), with_agents)


class AdminServiceRepositoriesTest(unittest.TestCase):
    """Clones run for real against a local source repo: no network needed."""

    def _service(self, root: Path) -> AdminService:
        service = AdminService.__new__(AdminService)
        service.root = root
        service.repositories_dir = root / "repositories"
        return service

    def _source_repository(self, path: Path, branch: str) -> Path:
        """A one-commit repository whose default branch is ``branch``."""
        path.mkdir(parents=True)
        self._git(path, "init", "-b", branch)
        (path / "README.md").write_text("source\n")
        self._git(path, "add", "README.md")
        self._git(path, "-c", "user.email=codee@example.com",
                  "-c", "user.name=Codee", "-c", "commit.gpgsign=false",
                  "commit", "-m", "initial")
        return path

    def _git(self, cwd: Path, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(cwd), *arguments],
                                capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def test_name_is_taken_from_every_url_form(self) -> None:
        self.assertEqual(repository_name(
            "git@github.com:org/codee.git"), "codee")
        self.assertEqual(repository_name("ssh://git@github.com/org/codee.git"),
                         "codee")
        self.assertEqual(repository_name(
            "https://github.com/org/codee/"), "codee")
        self.assertEqual(repository_name("  "), "")

    def test_add_builds_the_bare_and_worktree_layout(self) -> None:
        for branch in ("main", "master"):
            with self.subTest(branch=branch), \
                    tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                source = self._source_repository(root / "source", branch)
                service = self._service(root)

                added, message, name = service.add_repository(str(source))

                repository = root / "repositories" / "source"
                self.assertTrue(added, message)
                self.assertEqual(name, "source")
                self.assertIn(branch, message)
                self.assertTrue((repository / ".bare").is_dir())
                self.assertEqual((repository / ".git").read_text(),
                                 "gitdir: ./.bare\n")
                # The branch is checked out beside `.bare`, as its own worktree.
                self.assertTrue((repository / branch / "README.md").is_file())
                self.assertEqual(
                    self._git(repository / branch, "rev-parse",
                              "--abbrev-ref", "HEAD"),
                    branch)

    def test_add_fetches_remote_tracking_refs_for_later_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = self._source_repository(root / "source", "main")
            service = self._service(root)

            service.add_repository(str(source))

            repository = root / "repositories" / "source"
            self.assertTrue(self._git(repository, "rev-parse", "origin/main"))

    def test_add_refuses_a_repository_that_is_already_there(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "repositories" / "source").mkdir(parents=True)
            service = self._service(root)

            added, message, _ = service.add_repository(
                "git@github.com:org/source.git")

            self.assertFalse(added)
            self.assertIn("already exists", message)

    def test_a_failed_clone_leaves_no_directory_behind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = self._service(root)

            added, message, _ = service.add_repository(
                str(root / "missing-repository.git"))

            self.assertFalse(added)
            self.assertIn("Could not clone", message)
            self.assertFalse((root / "repositories" / "missing-repository")
                             .exists())

    def test_list_reports_the_clone_and_skips_everything_else(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = self._source_repository(root / "source", "main")
            service = self._service(root)
            service.add_repository(str(source))
            (root / "repositories" / "scratch").mkdir()

            repositories = service.list_repositories()

            self.assertEqual([repository["name"] for repository in repositories],
                             ["source"])
            self.assertEqual(repositories[0]["url"], str(source))
            self.assertEqual(repositories[0]["default_branch"], "main")

    def test_list_is_empty_before_anything_is_cloned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            self.assertEqual(service.list_repositories(), [])


class AzureDevOpsOAuthTest(unittest.TestCase):
    """The admin side of the flow: build the URL, then handle the callback."""

    AZURE_CREDENTIALS = {
        "organization_url": "https://dev.azure.com/acme",
        "tenant_id": "tenant-1",
        "client_id": "client-1",
        "client_secret": "secret-1",
    }

    def _service(self, data_dir: Path, credentials: dict | None = None) -> AdminService:
        service = AdminService.__new__(AdminService)
        service.data_dir = data_dir
        settings = Settings(
            tasks_provider=TasksProvider.AZURE_DEVOPS,
            credentials={"azure_devops": credentials
                         if credentials is not None else self.AZURE_CREDENTIALS})
        service.context = CodeeMainContext(
            data_dir=data_dir, settings=settings)
        save_settings(data_dir, settings)
        return service

    def test_redirect_uri_follows_the_admin_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            with patch.dict(os.environ, {"REFLEX_API_URL": "http://127.0.0.1:9100",
                                         "CODEE_ADMIN_BASE_URL": ""}, clear=False):
                self.assertEqual(service.azure_redirect_uri(),
                                 "http://localhost:9100/api/oauth/azure-devops/callback")

    def test_redirect_uri_honours_an_explicit_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            with patch.dict(os.environ,
                            {"CODEE_ADMIN_BASE_URL": "https://codee.example.com/"},
                            clear=False):
                self.assertEqual(
                    service.azure_redirect_uri(),
                    "https://codee.example.com/api/oauth/azure-devops/callback")

    def test_authorization_refuses_an_incomplete_app_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory),
                                    credentials={"organization_url": "https://dev.azure.com/acme"})

            started, message = service.start_azure_authorization()

            self.assertFalse(started)
            self.assertIn("client secret", message)

    def test_callback_stores_the_tokens_for_the_state_it_issued(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            started, url = service.start_azure_authorization()
            state = parse_qs(urlparse(url).query)["state"][0]
            tokens = {"access_token": "at", "refresh_token": "rt",
                      "expires_at": "2026-08-05T12:00:00+00:00", "scope": "s"}

            with patch.object(azure_oauth, "exchange_code", return_value=tokens) as exchange, \
                    patch.object(azure_oauth, "fetch_account", return_value="dev@acme.com"):
                connected, message = service.complete_azure_authorization(
                    "code-1", state)

            self.assertTrue(started)
            self.assertTrue(connected)
            self.assertIn("dev@acme.com", message)
            # The exchange must reuse the redirect URI the authorization was issued
            # with — Entra rejects the code otherwise.
            self.assertEqual(
                exchange.call_args.args[1], service.azure_redirect_uri())
            self.assertTrue(service.azure_connection()["connected"])

    def test_callback_with_a_forged_state_stores_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            service.start_azure_authorization()

            with patch.object(azure_oauth, "exchange_code") as exchange:
                connected, message = service.complete_azure_authorization(
                    "code-1", "forged-state")

            exchange.assert_not_called()
            self.assertFalse(connected)
            self.assertIn("again", message)
            self.assertFalse(service.azure_connection()["connected"])

    def test_failed_exchange_reports_the_entra_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            _, url = service.start_azure_authorization()
            state = parse_qs(urlparse(url).query)["state"][0]

            with patch.object(azure_oauth, "exchange_code",
                              side_effect=azure_oauth.AzureDevOpsAuthError(
                                  "AADSTS7000215: Invalid client secret.")):
                connected, message = service.complete_azure_authorization(
                    "code-1", state)

            self.assertFalse(connected)
            self.assertIn("Invalid client secret", message)
            self.assertFalse(service.azure_connection()["connected"])

    def test_disconnect_forgets_the_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            _, url = service.start_azure_authorization()
            state = parse_qs(urlparse(url).query)["state"][0]
            with patch.object(azure_oauth, "exchange_code",
                              return_value={"access_token": "at", "refresh_token": "rt",
                                            "expires_at": None, "scope": ""}), \
                    patch.object(azure_oauth, "fetch_account", return_value=""):
                service.complete_azure_authorization("code-1", state)

            service.disconnect_azure()

            self.assertFalse(service.azure_connection()["connected"])


class VerifyTasksConnectionTest(unittest.TestCase):
    """The settings page checks credentials by pulling tasks with them."""

    JIRA_CREDENTIALS = {
        "base_url": "https://acme.atlassian.net",
        "account_email": "agent@acme.test",
        "api_token": "token",
        "project": "CORE",
    }

    def _service(self, root: Path) -> AdminService:
        service = AdminService.__new__(AdminService)
        service.root = root
        service.skills_dir = _write_issue_skill(root)
        service.data_dir = root / ".codee"
        service.data_dir.mkdir()
        settings = Settings(
            credentials={"jira": {"base_url": "https://stale.test"}})
        service.context = CodeeMainContext(
            data_dir=service.data_dir, settings=settings)
        save_settings(service.data_dir, settings)
        return service

    def _tasks_check(self, checks: list[dict]) -> dict:
        self.assertEqual(checks[0]["name"], TASKS_CHECK)
        return checks[0]

    def test_the_form_credentials_are_used_not_the_saved_ones(self) -> None:
        # The point of the check is to try credentials before committing them.
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            response = Mock(status_code=200)
            response.json.return_value = {"issues": []}

            with patch("codee_tasks_jira.provider.requests.get",
                       return_value=response) as get:
                checks = list(service.verify_tasks_connection(
                    "jira", self.JIRA_CREDENTIALS))

            check = self._tasks_check(checks)
            self.assertTrue(check["ok"], check["message"])
            self.assertTrue(get.call_args.args[0].startswith(
                "https://acme.atlassian.net"))

    def test_it_polls_the_statuses_the_issue_skills_declare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            response = Mock(status_code=200)
            response.json.return_value = {"issues": []}

            with patch("codee_tasks_jira.provider.requests.get",
                       return_value=response) as get:
                list(service.verify_tasks_connection(
                    "jira", self.JIRA_CREDENTIALS))

            self.assertIn('status in ("Ready")',
                          get.call_args.kwargs["params"]["jql"])

    def test_missing_credentials_are_refused_before_any_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            with patch("codee_tasks_jira.provider.requests.get") as get:
                checks = list(service.verify_tasks_connection(
                    "jira", {"base_url": "https://acme.atlassian.net"}))

            # Nothing to say about MCP when the credentials aren't there yet.
            self.assertEqual(len(checks), 1)
            self.assertFalse(checks[0]["ok"])
            self.assertIn("Not configured", checks[0]["message"])
            get.assert_not_called()

    def test_an_unknown_provider_is_reported_rather_than_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            checks = list(service.verify_tasks_connection("trello", {}))

            self.assertFalse(checks[0]["ok"])
            self.assertIn("trello", checks[0]["message"])

    def test_saved_settings_are_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            response = Mock(status_code=200)
            response.json.return_value = {"issues": []}

            with patch("codee_tasks_jira.provider.requests.get",
                       return_value=response):
                list(service.verify_tasks_connection(
                    "jira", self.JIRA_CREDENTIALS))

            self.assertEqual(service.load_settings().credentials["jira"],
                             {"base_url": "https://stale.test"})


class VerifyTasksMcpCheckTest(unittest.TestCase):
    """The second check drives the backend through MCP with a coding agent."""

    JIRA_CREDENTIALS = VerifyTasksConnectionTest.JIRA_CREDENTIALS

    def _service(self, root: Path, with_mcp: bool = True) -> AdminService:
        service = VerifyTasksConnectionTest._service(self, root)
        if with_mcp:
            service.setup_tasks_mcp("jira", self.JIRA_CREDENTIALS)
        return service

    def _run(self, service: AdminService, agent_reply: str | Exception,
             issues: list | None = None) -> dict:
        """Run both checks with the pull stubbed, and return the MCP one."""
        response = Mock(status_code=200)
        response.json.return_value = {"issues": issues or []}
        agent = Mock()
        if isinstance(agent_reply, Exception):
            agent.run.side_effect = agent_reply
        else:
            agent.run.return_value = agent_reply
        with patch("codee_tasks_jira.provider.requests.get",
                   return_value=response), \
                patch.object(AdminService, "_build_coding_agent",
                             return_value=agent):
            # Drained inside the patches: the checks are a generator, so leaving
            # this block first would run them for real against JIRA.
            checks = list(service.verify_tasks_connection(
                "jira", self.JIRA_CREDENTIALS))
        self.assertEqual([check["name"] for check in checks],
                         [TASKS_CHECK, MCP_CHECK])
        self.agent = agent
        self.prompt = agent.run.call_args.args[0] if agent.run.called else ""
        return checks[1]

    def test_an_unconfigured_server_fails_the_check_without_an_agent_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory), with_mcp=False)

            check = self._run(service, '{"ok": true}')

            self.assertFalse(check["ok"])
            self.assertEqual(check["message"],
                             "MCP server is not configured, click Setup MCP "
                             "server above")
            self.assertEqual(self.prompt, "")

    def test_a_reported_success_names_the_task_the_agent_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            check = self._run(service, json.dumps(
                {"ok": True, "task": "CORE-42", "status": "Done"}))

            self.assertTrue(check["ok"], check["message"])
            self.assertIn("CORE-42", check["message"])
            self.assertIn("Done", check["message"])

    def test_azure_devops_mcp_check_uses_the_agents_best_model(self) -> None:
        class AzureProvider:
            MCP_SERVER_NAME = "ado"

            def mcp_check_steps(self, summary: str) -> list[str]:
                return [f"Create {summary}"]

        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            agent = Mock(**{
                "best_model.return_value": "claude-opus-5",
                "run.return_value": '{"ok": true, "task": "42"}',
            })

            with patch.object(service, "tasks_mcp_configured",
                              return_value=True), \
                    patch.object(AdminService, "_build_coding_agent",
                                 return_value=agent):
                check = service._check_tasks_mcp(
                    "azure_devops", AzureProvider(), blocked=False)

            self.assertTrue(check["ok"], check["message"])
            agent.best_model.assert_called_once_with()
            self.assertEqual(agent.run.call_args.args[2], "claude-opus-5")

    def test_jira_mcp_check_uses_the_agents_best_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            self._run(service, '{"ok": true, "task": "CORE-1"}')

            self.agent.best_model.assert_called_once_with()
            self.assertEqual(
                self.agent.run.call_args.args[2],
                self.agent.best_model.return_value)

    def test_the_prompt_forbids_every_route_other_than_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            self._run(service, '{"ok": true, "task": "CORE-1"}')

            self.assertIn("mcp-atlassian", self.prompt)
            self.assertIn("No other way is allowed", self.prompt)
            # Both provider steps, in order.
            self.assertIn("1. Create a new Task in JIRA project CORE",
                          self.prompt)
            self.assertIn("2. Move that issue to a Done or Cancelled status",
                          self.prompt)
            self.assertIn("2. Move that issue to a Done or Cancelled status",
                          self.prompt)
            self.assertIn("complete MCP tool request exactly as sent",
                          self.prompt)
            self.assertIn("complete MCP response or error exactly as received",
                          self.prompt)

    def test_a_fenced_reply_is_still_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            check = self._run(
                service, '```json\n{"ok": true, "task": "CORE-7"}\n```')

            self.assertTrue(check["ok"], check["message"])

    def test_a_reported_failure_is_passed_through(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            check = self._run(service, json.dumps(
                {"ok": False, "error": "No create-issue tool was offered."}))

            self.assertFalse(check["ok"])
            self.assertEqual(check["message"],
                             "No create-issue tool was offered.")

    def test_a_reported_failure_includes_the_full_mcp_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            request = {
                "tool": "ado-wit_work_item_write",
                "arguments": {"fields": [{"name": "System.Title",
                                          "value": "Codee check"}]},
            }
            response = {
                "error": {"code": "invalid_type",
                          "message": "Input validation failed"}}

            check = self._run(service, json.dumps({
                "ok": False,
                "error": "Creating the work item failed.",
                "request": request,
                "response": response,
            }))

            self.assertFalse(check["ok"])
            self.assertIn("MCP request:\n" + json.dumps(
                request, indent=2), check["message"])
            self.assertIn("MCP response:\n" + json.dumps(
                response, indent=2), check["message"])

    def test_a_non_json_failure_is_not_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            response = "x" * 500

            check = self._run(service, response)

            self.assertTrue(check["message"].endswith(response))

    def test_an_answer_that_is_not_the_asked_for_json_fails_the_check(self) -> None:
        # An agent that ignored the format can't be believed about the rest.
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            check = self._run(service, "Sure! I created and closed the task.")

            self.assertFalse(check["ok"])
            self.assertIn("did not report a result", check["message"])

    def test_an_agent_that_blows_up_is_reported_rather_than_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            check = self._run(service, RuntimeError("claude CLI exited 1"))

            self.assertFalse(check["ok"])
            self.assertIn("claude CLI exited 1", check["message"])

    def test_nothing_runs_until_the_next_check_is_asked_for(self) -> None:
        # What lets the page show the pull's verdict while MCP is still going.
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            response = Mock(status_code=200)
            response.json.return_value = {"issues": []}
            agent = Mock(**{"run.return_value": '{"ok": true, "task": "C-1"}'})

            with patch("codee_tasks_jira.provider.requests.get",
                       return_value=response) as get, \
                    patch.object(AdminService, "_build_coding_agent",
                                 return_value=agent):
                checks = service.verify_tasks_connection(
                    "jira", self.JIRA_CREDENTIALS)
                get.assert_not_called()

                self.assertEqual(next(checks)["name"], TASKS_CHECK)
                # One query per Codee work item, and the MCP check untouched
                # until it is asked for.
                self.assertEqual(get.call_count, 2)
                agent.run.assert_not_called()

                self.assertEqual(next(checks)["name"], MCP_CHECK)
                agent.run.assert_called_once()
                self.assertIsNone(next(checks, None))

    def test_a_failed_pull_skips_the_agent_run(self) -> None:
        # No point spending an agent on credentials JIRA just refused.
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            response = Mock(status_code=200)
            response.json.side_effect = ValueError("boom")
            agent = Mock()

            with patch("codee_tasks_jira.provider.requests.get",
                       return_value=response), \
                    patch.object(AdminService, "_build_coding_agent",
                                 return_value=agent):
                checks = list(service.verify_tasks_connection(
                    "jira", self.JIRA_CREDENTIALS))

            self.assertFalse(checks[0]["ok"])
            self.assertFalse(checks[1]["ok"])
            self.assertIn("Not attempted", checks[1]["message"])
            agent.run.assert_not_called()


class SetupTasksMcpTest(unittest.TestCase):
    """The settings page hands the provider's MCP server to the coding agent."""

    JIRA_CREDENTIALS = VerifyTasksConnectionTest.JIRA_CREDENTIALS

    def _service(self, root: Path) -> AdminService:
        service = AdminService.__new__(AdminService)
        service.root = root
        service.data_dir = root / ".codee"
        service.data_dir.mkdir()
        settings = Settings(
            credentials={"jira": {"base_url": "https://stale.test"}})
        service.context = CodeeMainContext(
            data_dir=service.data_dir, settings=settings)
        save_settings(service.data_dir, settings)
        return service

    def _config(self, root: Path) -> dict:
        return json.loads((root / ".mcp.json").read_text())

    def test_it_writes_the_form_credentials_into_the_project_mcp_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = self._service(root)

            done, message = service.setup_tasks_mcp(
                "jira", self.JIRA_CREDENTIALS)

            self.assertTrue(done, message)
            config = self._config(root)
            self.assertEqual(
                config["mcpServers"]["mcp-atlassian"]["env"]["JIRA_URL"],
                "https://acme.atlassian.net")
            self.assertIn("mcp-atlassian", config["servers"])

    def test_saved_settings_are_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = self._service(root)

            service.setup_tasks_mcp("jira", self.JIRA_CREDENTIALS)

            self.assertEqual(service.load_settings().credentials["jira"],
                             {"base_url": "https://stale.test"})

    def test_incomplete_credentials_write_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = self._service(root)

            done, message = service.setup_tasks_mcp(
                "jira", {"base_url": "https://acme.atlassian.net"})

            self.assertFalse(done)
            self.assertIn("aren't filled in", message)
            self.assertFalse((root / ".mcp.json").exists())

    def test_a_provider_that_cannot_describe_a_server_yet_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = self._service(root)

            done, message = service.setup_tasks_mcp("azure_devops", {})

            self.assertFalse(done)
            self.assertIn("azure_devops", message)
            self.assertFalse((root / ".mcp.json").exists())

    def test_azure_devops_writes_its_own_server_alongside_jiras(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = self._service(root)
            service.setup_tasks_mcp("jira", self.JIRA_CREDENTIALS)

            done, message = service.setup_tasks_mcp(
                "azure_devops",
                {"organization_url": "https://dev.azure.com/acme"})

            self.assertTrue(done, message)
            config = self._config(root)
            self.assertEqual(config["mcpServers"]["ado"], {
                "command": "npx",
                "args": ["-y", "@azure-devops/mcp@2.8.0", "acme",
                         "--authentication", "azcli"],
            })
            self.assertEqual(config["servers"]["ado"]["type"], "stdio")
            # Switching provider doesn't cost you the other one's server.
            self.assertIn("mcp-atlassian", config["mcpServers"])
            self.assertTrue(service.tasks_mcp_configured("azure_devops"))

    def test_an_unknown_provider_is_reported_rather_than_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            done, message = service.setup_tasks_mcp("trello", {})

            self.assertFalse(done)
            self.assertIn("trello", message)

    def test_it_reports_configured_only_once_the_server_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = self._service(root)

            self.assertFalse(service.tasks_mcp_configured("jira"))
            service.setup_tasks_mcp("jira", self.JIRA_CREDENTIALS)

            self.assertTrue(service.tasks_mcp_configured("jira"))

    def test_a_provider_without_an_mcp_server_is_never_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = self._service(root)
            service.setup_tasks_mcp("jira", self.JIRA_CREDENTIALS)

            self.assertFalse(service.tasks_mcp_configured("azure_devops"))
            self.assertFalse(service.tasks_mcp_configured("trello"))

    def test_a_broken_mcp_json_is_reported_rather_than_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = self._service(root)
            (root / ".mcp.json").write_text("{not json")

            done, message = service.setup_tasks_mcp(
                "jira", self.JIRA_CREDENTIALS)

            self.assertFalse(done)
            self.assertIn("not valid JSON", message)


class RemoveRedundantSkillTransitionsTest(unittest.TestCase):
    """Only a genuine detour removes an edge, never a repeated statement."""

    def _transition(self, source: str, target: str, label: str,
                    evidence: str) -> dict[str, str]:
        return {"source": source, "target": target,
                "label": label, "evidence": evidence}

    def test_a_bypass_of_a_two_step_path_is_removed(self) -> None:
        transitions = [
            self._transition("Ready", "In progress", "dev", "first"),
            self._transition("In progress", "Review", "dev", "second"),
            self._transition("Ready", "Review", "dev", "third"),
        ]

        retained = _remove_redundant_skill_transitions(transitions)

        self.assertEqual([(row["source"], row["target"]) for row in retained],
                         [("Ready", "In progress"), ("In progress", "Review")])

    def test_repeating_one_transition_keeps_it(self) -> None:
        """A skill that names the same target twice still gets its edge.

        story-planner moves the story to `AI Decomposition review` both when
        the plan is ready and when it stops to ask a question, so the agent
        reports the pair twice. Reading the second copy as a longer path made
        each delete the other and the transition disappeared from the graph.
        """
        transitions = [
            self._transition("AI Decomposition needed", "AI Decomposition review",
                             "story-planner", "plan posted"),
            self._transition("AI Decomposition needed", "AI Decomposition review",
                             "story-planner", "questions asked"),
        ]

        retained = _remove_redundant_skill_transitions(transitions)

        self.assertEqual(retained, transitions)

    def test_a_duplicated_bypass_is_still_removed(self) -> None:
        transitions = [
            self._transition("Ready", "In progress", "dev", "first"),
            self._transition("In progress", "Review", "dev", "second"),
            self._transition("Ready", "Review", "dev", "third"),
            self._transition("Ready", "Review", "dev", "fourth"),
        ]

        retained = _remove_redundant_skill_transitions(transitions)

        self.assertEqual([(row["source"], row["target"]) for row in retained],
                         [("Ready", "In progress"), ("In progress", "Review")])

    def test_another_skill_bypass_is_left_alone(self) -> None:
        """The detour has to belong to the same skill to count as one."""
        transitions = [
            self._transition("Ready", "In progress", "dev", "first"),
            self._transition("In progress", "Review", "dev", "second"),
            self._transition("Ready", "Review", "qa", "third"),
        ]

        retained = _remove_redundant_skill_transitions(transitions)

        self.assertEqual(len(retained), 3)


class IssuePromptTaskTest(unittest.TestCase):
    """Which prompts name a work item the dashboard can link."""

    def test_an_issue_trigger_prompt_names_its_work_item(self) -> None:
        self.assertEqual(
            issue_prompt_task("/story-planner NIM-44025"), "NIM-44025")

    def test_a_numeric_key_is_a_work_item_too(self) -> None:
        # Azure DevOps numbers its work items rather than keying them.
        self.assertEqual(issue_prompt_task("/task-developer 41337"), "41337")

    def test_a_prompt_with_no_work_item_names_none(self) -> None:
        self.assertEqual(issue_prompt_task("/daily-report"), "")

    def test_a_sentence_mentioning_a_key_is_not_a_trigger_prompt(self) -> None:
        # A typed prompt may mention anything; only the shape the executor
        # writes means "this run is about that work item".
        self.assertEqual(
            issue_prompt_task("Please look at NIM-44025 when you can"), "")
        self.assertEqual(
            issue_prompt_task("/story-planner NIM-44025 and NIM-2"), "")

    def test_an_empty_prompt_names_none(self) -> None:
        self.assertEqual(issue_prompt_task(""), "")
        self.assertEqual(issue_prompt_task(None), "")


class DashboardWorkItemLinkTest(unittest.TestCase):
    """A live run says where the work item it was triggered for can be read."""

    JIRA_CREDENTIALS = {
        "base_url": "https://acme.atlassian.net",
        "account_email": "agent@acme.test",
        "api_token": "token",
        "project": "CORE",
    }

    def _service(self, root: Path, credentials: dict | None = None,
                 provider: str = "jira") -> AdminService:
        service = AdminService.__new__(AdminService)
        service.root = root
        service.data_dir = root / ".codee"
        service.data_dir.mkdir()
        settings = Settings(
            tasks_provider=TasksProvider(provider),
            credentials={provider: credentials if credentials is not None
                         else self.JIRA_CREDENTIALS})
        service.context = CodeeMainContext(
            data_dir=service.data_dir, settings=settings)
        save_settings(service.data_dir, settings)
        return service

    def _active(self, service: AdminService, message: str) -> dict:
        runs_db.start_job("sid-1", message, main_context=service.context)
        return service.dashboard()["active"][0]

    def test_a_jira_run_links_its_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            job = self._active(service, "/story-planner NIM-44025")

            self.assertEqual(job["task_key"], "NIM-44025")
            self.assertEqual(job["task_url"],
                             "https://acme.atlassian.net/browse/NIM-44025")

    def test_an_azure_devops_run_links_its_work_item(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(
                Path(temporary_directory),
                {"organization_url": "https://dev.azure.com/acme"},
                provider="azure_devops")

            job = self._active(service, "/task-developer 41337")

            self.assertEqual(
                job["task_url"],
                "https://dev.azure.com/acme/_workitems/edit/41337")

    def test_a_prompt_naming_no_work_item_gets_no_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            job = self._active(service, "/daily-report")

            self.assertEqual((job["task_key"], job["task_url"]), ("", ""))

    def test_an_unconfigured_provider_leaves_the_run_unlinked(self) -> None:
        # The live list is what the dashboard is for: no link beats no list.
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory), {})

            job = self._active(service, "/story-planner NIM-44025")

            self.assertEqual(job["task_key"], "NIM-44025")
            self.assertEqual(job["task_url"], "")

    def test_the_rest_of_the_row_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            job = self._active(service, "/story-planner NIM-44025")

            self.assertEqual(job["session_id"], "sid-1")
            self.assertEqual(job["message"], "/story-planner NIM-44025")
            self.assertTrue(job["elapsed_label"])


if __name__ == "__main__":
    unittest.main()
