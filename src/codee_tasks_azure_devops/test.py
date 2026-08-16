import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from codee_database import oauth_tokens
from codee_database.database import get_db_connection
from codee_main_context.context import CodeeMainContext, Settings, TasksProvider

from codee_tasks_azure_devops import oauth
from codee_tasks_azure_devops.oauth import (
    AzureDevOpsAuth, AzureDevOpsAuthError, OAuthConfig, is_expired)
from codee_tasks_azure_devops.provider import AzureDevOpsTasksProvider


def _config(**overrides) -> OAuthConfig:
    values = {
        "organization_url": "https://dev.azure.com/acme",
        "tenant_id": "tenant-1",
        "client_id": "client-1",
        "client_secret": "secret-1",
    }
    values.update(overrides)
    return OAuthConfig(**values)


def _response(payload: dict, status_code: int = 200) -> Mock:
    response = Mock()
    response.status_code = status_code
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _iso(offset: timedelta) -> str:
    return (datetime.now(timezone.utc) + offset).isoformat()


def _settings(**credentials) -> Settings:
    return Settings(tasks_provider=TasksProvider.AZURE_DEVOPS,
                    credentials={"azure_devops": credentials})


class OAuthConfigTest(unittest.TestCase):
    def test_from_settings_reads_provider_credentials(self) -> None:
        settings = _settings(organization_url="https://dev.azure.com/acme/",
                             client_id="client-1",
                             client_secret="secret-1")

        config = OAuthConfig.from_settings(settings)

        # The trailing slash would otherwise double up in every API URL.
        self.assertEqual(config.organization_url, "https://dev.azure.com/acme")
        self.assertTrue(config.is_complete())

    def test_a_leftover_project_credential_is_ignored(self) -> None:
        # Settings written when a project field still existed must not keep the
        # queries narrowed to it; there is nothing left that reads the key.
        config = OAuthConfig.from_settings(
            _settings(organization_url="https://dev.azure.com/acme",
                      project="Core", client_id="c", client_secret="s"))

        self.assertTrue(config.is_complete())
        self.assertNotIn("Core", str(config))

    def test_is_incomplete_without_a_secret(self) -> None:
        self.assertFalse(_config(client_secret="").is_complete())

    def test_missing_tenant_falls_back_to_any_work_directory(self) -> None:
        config = _config(tenant_id="")
        self.assertIn("/organizations/", config.authorize_endpoint)

    def test_the_organization_name_comes_out_of_either_url_form(self) -> None:
        # Both name `acme`; which one the user typed shouldn't matter.
        self.assertEqual(
            _config(organization_url="https://dev.azure.com/acme").organization,
            "acme")
        self.assertEqual(
            _config(organization_url="https://acme.visualstudio.com").organization,
            "acme")

    def test_no_organization_url_names_no_organization(self) -> None:
        self.assertEqual(_config(organization_url="").organization, "")


class AuthorizationUrlTest(unittest.TestCase):
    def test_url_carries_pkce_challenge_and_read_scope(self) -> None:
        verifier = "verifier-value"

        url = oauth.build_authorization_url(
            _config(), "http://localhost:8501/cb", "state-1", verifier)

        self.assertIn("login.microsoftonline.com/tenant-1/oauth2/v2.0/authorize", url)
        self.assertIn(f"code_challenge={oauth.code_challenge_for(verifier)}", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("offline_access", url)
        self.assertIn(oauth.AZURE_DEVOPS_RESOURCE_ID, url)
        # The verifier itself must never leave the backend.
        self.assertNotIn(verifier, url)

    def test_code_challenge_is_unpadded_url_safe_base64(self) -> None:
        challenge = oauth.code_challenge_for("verifier-value")
        self.assertNotIn("=", challenge)
        self.assertNotIn("+", challenge)
        self.assertNotIn("/", challenge)


class TokenExchangeTest(unittest.TestCase):
    def test_exchange_converts_expires_in_to_an_absolute_deadline(self) -> None:
        with patch.object(oauth.requests, "post",
                          return_value=_response({"access_token": "at", "refresh_token": "rt",
                                                  "expires_in": 3599})) as post:
            tokens = oauth.exchange_code(
                _config(), "http://localhost:8501/cb", "code-1", "verifier-1")

        sent = post.call_args.kwargs["data"]
        self.assertEqual(sent["grant_type"], "authorization_code")
        self.assertEqual(sent["code_verifier"], "verifier-1")
        self.assertEqual(sent["client_secret"], "secret-1")
        self.assertEqual(tokens["refresh_token"], "rt")
        deadline = datetime.fromisoformat(tokens["expires_at"])
        self.assertGreater(deadline, datetime.now(timezone.utc))

    def test_error_response_raises_with_the_first_description_line(self) -> None:
        payload = {"error": "invalid_client",
                   "error_description": "AADSTS7000215: Invalid client secret.\r\n"
                                        "Trace ID: abc\r\nCorrelation ID: def"}
        with patch.object(oauth.requests, "post",
                          return_value=_response(payload, status_code=401)):
            with self.assertRaises(AzureDevOpsAuthError) as raised:
                oauth.exchange_code(_config(), "http://cb", "code", "verifier")

        self.assertEqual(str(raised.exception),
                         "AADSTS7000215: Invalid client secret.")
        # Fixable in Settings; the refresh token is still good.
        self.assertFalse(raised.exception.terminal)

    def test_revoked_grant_is_terminal(self) -> None:
        payload = {"error": "invalid_grant",
                   "error_description": "AADSTS700082: The refresh token has expired."}
        with patch.object(oauth.requests, "post",
                          return_value=_response(payload, status_code=400)):
            with self.assertRaises(AzureDevOpsAuthError) as raised:
                oauth.refresh_access_token(_config(), "rt")

        self.assertTrue(raised.exception.terminal)

    def test_entra_outage_is_not_terminal(self) -> None:
        for status_code in (500, 503, 429):
            with self.subTest(status_code=status_code):
                with patch.object(oauth.requests, "post",
                                  return_value=_response({}, status_code=status_code)):
                    with self.assertRaises(AzureDevOpsAuthError) as raised:
                        oauth.refresh_access_token(_config(), "rt")

                self.assertFalse(raised.exception.terminal)

    def test_unreachable_entra_is_not_terminal(self) -> None:
        with patch.object(oauth.requests, "post",
                          side_effect=oauth.requests.ConnectionError("no route to host")):
            with self.assertRaises(AzureDevOpsAuthError) as raised:
                oauth.refresh_access_token(_config(), "rt")

        self.assertFalse(raised.exception.terminal)


class ExpiryTest(unittest.TestCase):
    def test_token_inside_the_safety_margin_counts_as_expired(self) -> None:
        self.assertTrue(is_expired(_iso(timedelta(seconds=30))))

    def test_token_with_time_to_spare_is_not_expired(self) -> None:
        self.assertFalse(is_expired(_iso(timedelta(minutes=30))))

    def test_unusable_expiry_forces_a_refresh(self) -> None:
        self.assertTrue(is_expired(None))
        self.assertTrue(is_expired("not-a-timestamp"))

    def test_naive_timestamp_is_read_as_utc(self) -> None:
        naive = (datetime.now(timezone.utc)
                 + timedelta(minutes=30)).replace(tzinfo=None).isoformat()
        self.assertFalse(is_expired(naive))


class AuthTokenLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.context = CodeeMainContext(data_dir=Path(self._temporary.name))
        self.auth = AzureDevOpsAuth(_config(), self.context)

    def _store(self, expires_at: str, refresh_token: str | None = "rt") -> None:
        oauth_tokens.save_tokens(
            oauth.PROVIDER, access_token="at", refresh_token=refresh_token,
            expires_at=expires_at, account="dev@acme.com",
            main_context=self.context)

    def test_unconnected_provider_reports_no_connection(self) -> None:
        self.assertFalse(self.auth.is_connected())
        with self.assertRaises(AzureDevOpsAuthError):
            self.auth.access_token()

    def test_valid_token_is_returned_without_calling_entra(self) -> None:
        self._store(_iso(timedelta(minutes=30)))

        with patch.object(oauth, "refresh_access_token") as refresh:
            self.assertEqual(self.auth.access_token(), "at")

        refresh.assert_not_called()

    def test_expired_token_is_refreshed_and_persisted(self) -> None:
        self._store(_iso(timedelta(seconds=-10)))
        fresh = {"access_token": "at2", "refresh_token": "rt2",
                 "expires_at": _iso(timedelta(hours=1)), "scope": oauth.SCOPE}

        with patch.object(oauth, "refresh_access_token", return_value=fresh) as refresh:
            self.assertEqual(self.auth.access_token(), "at2")

        refresh.assert_called_once()
        stored = self.auth.connection()
        self.assertEqual(stored["access_token"], "at2")
        self.assertEqual(stored["refresh_token"], "rt2")
        # The account label survives a refresh; Entra doesn't resend it.
        self.assertEqual(stored["account"], "dev@acme.com")

    def test_refresh_without_a_new_refresh_token_keeps_the_old_one(self) -> None:
        self._store(_iso(timedelta(seconds=-10)))
        fresh = {"access_token": "at2", "refresh_token": None,
                 "expires_at": _iso(timedelta(hours=1))}

        with patch.object(oauth, "refresh_access_token", return_value=fresh):
            self.auth.access_token()

        self.assertEqual(self.auth.connection()["refresh_token"], "rt")

    def test_rejected_refresh_token_drops_the_connection(self) -> None:
        self._store(_iso(timedelta(seconds=-10)))

        with patch.object(oauth, "refresh_access_token",
                          side_effect=AzureDevOpsAuthError("invalid_grant",
                                                           terminal=True)):
            with self.assertRaises(AzureDevOpsAuthError):
                self.auth.access_token()

        # Left connected, every poll would retry a token Entra will never accept.
        self.assertFalse(self.auth.is_connected())

    def test_transient_refresh_failure_keeps_the_connection(self) -> None:
        # An Entra outage or a dropped network must not cost a manual reconsent.
        self._store(_iso(timedelta(seconds=-10)))

        with patch.object(oauth, "refresh_access_token",
                          side_effect=AzureDevOpsAuthError("Could not reach Entra ID")):
            with self.assertRaises(AzureDevOpsAuthError):
                self.auth.access_token()

        self.assertTrue(self.auth.is_connected())
        self.assertEqual(self.auth.connection()["refresh_token"], "rt")

    def test_connection_survives_a_transient_failure_and_recovers(self) -> None:
        self._store(_iso(timedelta(seconds=-10)))
        fresh = {"access_token": "at2", "refresh_token": "rt2",
                 "expires_at": _iso(timedelta(hours=1))}

        with patch.object(oauth, "refresh_access_token",
                          side_effect=[AzureDevOpsAuthError("Entra ID is down"), fresh]):
            with self.assertRaises(AzureDevOpsAuthError):
                self.auth.access_token()
            self.assertEqual(self.auth.access_token(), "at2")

    def test_expired_token_without_a_refresh_token_drops_the_connection(self) -> None:
        self._store(_iso(timedelta(seconds=-10)), refresh_token=None)

        with self.assertRaises(AzureDevOpsAuthError):
            self.auth.access_token()

        self.assertFalse(self.auth.is_connected())


class PendingAuthorizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.context = CodeeMainContext(data_dir=Path(self._temporary.name))

    def test_pending_state_round_trips_once(self) -> None:
        oauth_tokens.create_pending(
            oauth.PROVIDER, "state-1", "verifier-1", "http://localhost:8501/cb",
            main_context=self.context)

        first = oauth_tokens.consume_pending(
            oauth.PROVIDER, "state-1", main_context=self.context)
        replayed = oauth_tokens.consume_pending(
            oauth.PROVIDER, "state-1", main_context=self.context)

        self.assertEqual(first["code_verifier"], "verifier-1")
        self.assertEqual(first["redirect_uri"], "http://localhost:8501/cb")
        self.assertIsNone(replayed)

    def test_unknown_state_is_rejected(self) -> None:
        self.assertIsNone(oauth_tokens.consume_pending(
            oauth.PROVIDER, "forged-state", main_context=self.context))

    def test_expired_pending_row_is_rejected(self) -> None:
        oauth_tokens.create_pending(
            oauth.PROVIDER, "state-1", "verifier-1", "http://cb",
            main_context=self.context)
        stale = (datetime.now(timezone.utc)
                 - oauth_tokens.PENDING_TTL - timedelta(minutes=1)).isoformat()
        with closing(get_db_connection(self.context)) as conn, conn:
            conn.execute("UPDATE oauth_pending SET created_at = ?", (stale,))

        self.assertIsNone(oauth_tokens.consume_pending(
            oauth.PROVIDER, "state-1", main_context=self.context))


class TasksProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.context = CodeeMainContext(data_dir=Path(self._temporary.name))
        self.provider = AzureDevOpsTasksProvider(
            _settings(organization_url="https://dev.azure.com/acme",
                      client_id="client-1",
                      client_secret="secret-1"),
            self.context)

    def _connect(self) -> None:
        oauth_tokens.save_tokens(
            oauth.PROVIDER, access_token="at", refresh_token="rt",
            expires_at=_iso(timedelta(hours=1)), account="dev@acme.com",
            main_context=self.context)

    def test_wiql_quotes_statuses_and_orders_by_priority(self) -> None:
        wiql = self.provider._build_wiql(["Ready", "Bob's queue"])

        self.assertIn("[System.State] IN ('Ready', 'Bob''s queue')", wiql)
        self.assertIn("[System.AssignedTo] = @Me", wiql)
        self.assertIn("ORDER BY [Microsoft.VSTS.Common.Priority] ASC", wiql)

    def test_wiql_only_asks_for_codee_work_item_types(self) -> None:
        wiql = self.provider._build_wiql(["Ready"])

        self.assertIn(
            "[System.WorkItemType] IN ('Codee Task', 'Codee Story')", wiql)

    def test_wiql_names_no_project_at_all(self) -> None:
        wiql = self.provider._build_wiql(["Ready"])

        self.assertNotIn("System.TeamProject", wiql)
        # @project would need a project in the route, which there isn't.
        self.assertNotIn("@project", wiql)

    def test_the_query_is_organization_scoped(self) -> None:
        self._connect()
        with patch("codee_tasks_azure_devops.provider.requests.post",
                   side_effect=[_response({"workItems": []})]) as post:
            self.provider.get_tasks(["Ready"])

        self.assertEqual(post.call_args.args[0],
                         "https://dev.azure.com/acme/_apis/wit/wiql")

    def test_describe_says_the_query_covers_all_projects(self) -> None:
        self.assertIn("all projects", self.provider.describe())

    def test_not_configured_until_oauth_completes(self) -> None:
        self.assertFalse(self.provider.is_configured())
        self._connect()
        self.assertTrue(self.provider.is_configured())

    def test_no_statuses_means_no_requests(self) -> None:
        self._connect()
        with patch.object(oauth.requests, "post") as post:
            self.assertEqual(self.provider.get_tasks([]), [])
        post.assert_not_called()

    def test_get_tasks_maps_fields_and_keeps_the_query_order(self) -> None:
        self._connect()
        wiql = _response({"workItems": [{"id": 11}, {"id": 12}]})
        # Deliberately out of order: the batch endpoint doesn't preserve WIQL order.
        items = _response({"value": [
            {"id": 12, "fields": {"System.Title": "Second",
                                  "System.State": "Ready",
                                  "System.WorkItemType": "Codee Task",
                                  "Microsoft.VSTS.Common.Priority": 2,
                                  "System.Tags": "ai; backend"}},
            {"id": 11, "fields": {"System.Title": "First",
                                  "System.State": "Ready",
                                  "System.WorkItemType": "Codee Task",
                                  "Microsoft.VSTS.Common.Priority": 1,
                                  "System.Parent": 9}},
        ]})
        parents = _response({"value": [
            {"id": 9, "fields": {"System.Title": "Story",
                                 "System.State": "Active",
                                 "System.WorkItemType": "User Story",
                                 "System.Tags": "epic"}},
        ]})

        with patch("codee_tasks_azure_devops.provider.requests.post",
                   side_effect=[wiql, items, parents]):
            tasks = self.provider.get_tasks(["Ready"])

        self.assertEqual([task.key for task in tasks], ["11", "12"])
        self.assertEqual(tasks[0].summary, "First")
        self.assertEqual(tasks[0].priority, "Highest")
        self.assertEqual(tasks[0].issue_type, "Task")
        self.assertEqual(tasks[0].parent.key, "9")
        self.assertEqual(tasks[0].parent.labels, ["epic"])
        # A parent outside the Codee types keeps whatever Azure DevOps calls it.
        self.assertEqual(tasks[0].parent.issue_type, "User Story")
        self.assertEqual(tasks[1].labels, ["ai", "backend"])
        self.assertEqual(tasks[1].priority, "High")

    def test_codee_story_maps_to_the_story_issue_type(self) -> None:
        self._connect()
        wiql = _response({"workItems": [{"id": 21}]})
        items = _response({"value": [
            {"id": 21, "fields": {"System.Title": "A story",
                                  "System.State": "Ready",
                                  "System.WorkItemType": "Codee Story"}},
        ]})

        with patch("codee_tasks_azure_devops.provider.requests.post",
                   side_effect=[wiql, items]):
            tasks = self.provider.get_tasks(["Ready"])

        self.assertEqual(tasks[0].issue_type, "Story")

    def test_a_child_of_a_codee_story_is_flagged(self) -> None:
        self._connect()
        tasks = self._tasks_with_parent_type("Codee Story")

        self.assertTrue(tasks[0].is_parent_codee_story)

    def test_a_child_of_a_plain_story_is_not_flagged(self) -> None:
        self._connect()
        # Both types map to the "Story" issue type, so only the raw work item
        # type separates a Codee story from a story a human owns.
        tasks = self._tasks_with_parent_type("Story")

        self.assertEqual(tasks[0].parent.issue_type, "Story")
        self.assertFalse(tasks[0].is_parent_codee_story)

    def test_a_task_without_a_parent_is_not_flagged(self) -> None:
        self._connect()
        wiql = _response({"workItems": [{"id": 31}]})
        items = _response({"value": [
            {"id": 31, "fields": {"System.Title": "Orphan",
                                  "System.State": "Ready",
                                  "System.WorkItemType": "Codee Task"}},
        ]})

        with patch("codee_tasks_azure_devops.provider.requests.post",
                   side_effect=[wiql, items]):
            tasks = self.provider.get_tasks(["Ready"])

        self.assertFalse(tasks[0].is_parent_codee_story)

    def _tasks_with_parent_type(self, parent_type: str) -> list:
        wiql = _response({"workItems": [{"id": 31}]})
        items = _response({"value": [
            {"id": 31, "fields": {"System.Title": "A child",
                                  "System.State": "Ready",
                                  "System.WorkItemType": "Codee Task",
                                  "System.Parent": 30}},
        ]})
        parents = _response({"value": [
            {"id": 30, "fields": {"System.Title": "The parent",
                                  "System.State": "Active",
                                  "System.WorkItemType": parent_type}},
        ]})

        with patch("codee_tasks_azure_devops.provider.requests.post",
                   side_effect=[wiql, items, parents]):
            return self.provider.get_tasks(["Ready"])

    def test_empty_result_skips_the_batch_call(self) -> None:
        self._connect()
        with patch("codee_tasks_azure_devops.provider.requests.post",
                   side_effect=[_response({"workItems": []})]) as post:
            self.assertEqual(self.provider.get_tasks(["Ready"]), [])
        self.assertEqual(post.call_count, 1)

    def test_expired_authorization_yields_no_tasks_instead_of_raising(self) -> None:
        # A polling executor must survive a revoked authorization.
        with patch("codee_tasks_azure_devops.provider.requests.post") as post:
            self.assertEqual(self.provider.get_tasks(["Ready"]), [])
        post.assert_not_called()


    # The settings page pulls for real and reports the failure instead of
    # hiding it, which is what makes a "Verify connection" answer worth trusting.

    def test_no_statuses_drops_the_state_clause_rather_than_emptying_it(self) -> None:
        # `IN ()` is not valid WIQL, and a check run before any issue-triggered
        # skill exists still has to reach Azure DevOps.
        wiql = self.provider._build_wiql([])

        self.assertNotIn("[System.State] IN", wiql)
        self.assertIn("[System.AssignedTo] = @Me", wiql)

    def test_a_successful_pull_names_the_work_items_it_found(self) -> None:
        self._connect()
        wiql = _response({"workItems": [{"id": 11}]})
        items = _response({"value": [
            {"id": 11, "fields": {"System.Title": "Fix the thing",
                                  "System.State": "Ready",
                                  "System.WorkItemType": "Codee Task"}},
        ]})

        with patch("codee_tasks_azure_devops.provider.requests.post",
                   side_effect=[wiql, items]):
            verified, message = self.provider.verify_connection(["Ready"])

        self.assertTrue(verified)
        self.assertIn("11 Fix the thing", message)

    def test_a_rejected_query_reports_what_azure_devops_said(self) -> None:
        self._connect()
        response = Mock(status_code=400, text="")
        response.json.return_value = {"message": "TF51005: no such field."}
        response.raise_for_status.side_effect = requests.HTTPError(
            "400 Client Error", response=response)

        with patch("codee_tasks_azure_devops.provider.requests.post",
                   return_value=response):
            verified, message = self.provider.verify_connection(["Ready"])

        self.assertFalse(verified)
        self.assertIn("HTTP 400", message)
        self.assertIn("TF51005", message)

    def test_a_missing_authorization_is_reported_not_swallowed(self) -> None:
        verified, message = self.provider.verify_connection(["Ready"])

        self.assertFalse(verified)
        self.assertIn("sign-in failed", message)


class AzureDevOpsMcpTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.context = CodeeMainContext(data_dir=Path(self._temporary.name))

    def _provider(self, **credentials) -> AzureDevOpsTasksProvider:
        values = {"organization_url": "https://dev.azure.com/acme",
                  "client_id": "client-1", "client_secret": "secret-1"}
        values.update(credentials)
        return AzureDevOpsTasksProvider(_settings(**values), self.context)

    def _connect(self) -> None:
        oauth_tokens.save_tokens(
            oauth.PROVIDER, access_token="at", refresh_token="rt",
            expires_at=_iso(timedelta(hours=1)), account="dev@acme.com",
            main_context=self.context)

    def test_the_server_addresses_the_organization_through_the_azure_cli(self) -> None:
        server = self._provider().mcp_server()

        self.assertEqual(server.name, "ado")
        self.assertEqual(server.command, "npx")
        self.assertEqual(server.args, ["-y", "@azure-devops/mcp", "acme",
                                       "--authentication", "azcli"])
        # It signs in through `az login`, so it carries no credentials of ours.
        self.assertEqual(server.env, {})

    def test_no_organization_url_yields_no_server(self) -> None:
        self.assertIsNone(self._provider(organization_url="").mcp_server())

    def test_the_check_creates_the_work_item_type_the_executor_polls(self) -> None:
        self._connect()

        steps = self._provider().mcp_check_steps("Codee check 1234")

        self.assertEqual(len(steps), 2)
        self.assertIn('"Codee Task" work item', steps[0])
        self.assertIn("acme organization", steps[0])
        self.assertIn('title "Codee check 1234"', steps[0])
        self.assertIn("assigned to dev@acme.com", steps[0])
        self.assertIn("Done, Closed or Removed", steps[1])

    def test_no_check_steps_before_the_account_is_known(self) -> None:
        # Nothing to assign the work item to until the OAuth consent is done.
        self.assertIsNone(self._provider().mcp_check_steps("x"))


def main():
    print("OK")


if __name__ == "__main__":
    unittest.main()
