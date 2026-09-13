import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, quote, urlparse

import requests

from codee_agent_claude_code import credentials, oauth
from codee_agent_claude_code.account import (
    AccountUnavailable, fetch_account, read_account)
from codee_agent_claude_code.provider import ClaudeCodeAgent
from codee_agent_claude_code.usage import (
    OAUTH_BETA, UsageUnavailable, fetch_usage, read_usage)
from codee_main_context.context import Settings

SESSION = "82232f47-df60-4cb3-8c3a-de12074c9205"


def _completed(result: str = "ok", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stderr=stderr,
        stdout=json.dumps({"result": result}))


def _response(status_code: int, text: str = "", json_body=None):
    """One usage endpoint reply, as requests hands it back."""
    response = Mock(spec=["status_code", "text", "json"])
    response.status_code = status_code
    response.text = text
    response.json = Mock(return_value=json_body if json_body is not None else {})
    return response


class ClaudeCodeRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = ClaudeCodeAgent(Settings(), Path("/repo"))

    def _run(self, model: str = "") -> list[str]:
        with patch("subprocess.run", return_value=_completed()) as run:
            self.agent.run("/do-it CORE-1", SESSION, model)
        return run.call_args.args[0]

    def test_a_skill_run_leaves_the_model_to_the_frontmatter(self) -> None:
        # Claude Code reads the skill's `model:` itself; a flag would override it.
        self.assertNotIn("--model", self._run())

    def test_an_explicit_model_is_passed_on_the_command_line(self) -> None:
        cmd = self._run(model="opus")

        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")

    def test_cli_output_is_decoded_as_utf8(self) -> None:
        with patch("subprocess.run", return_value=_completed()) as run:
            self.agent.run("/do-it CORE-1", SESSION)

        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_missing_captured_streams_do_not_raise_type_error(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout=None, stderr=None)

        with patch("subprocess.run", return_value=completed):
            self.assertEqual(self.agent.run("/do-it CORE-1", SESSION), "")

    def test_the_best_model_is_a_version_free_alias(self) -> None:
        # The alias tracks the newest Opus, so no release needs an edit here.
        self.assertEqual(ClaudeCodeAgent.best_model(), "opus")


class ClaudeCodeUsageTest(unittest.TestCase):
    """Reading a subscription's remaining allowance out of the usage response."""

    def test_a_fresh_account_is_not_limited(self) -> None:
        usage = read_usage({
            "five_hour": {"utilization": 1.0, "locked_reason": None},
            "seven_day": {"utilization": 70.0, "locked_reason": None},
        })

        self.assertFalse(usage.limited)
        self.assertEqual(usage.windows, {"five_hour": 1.0, "seven_day": 70.0})

    def test_a_full_session_window_is_limited(self) -> None:
        usage = read_usage({"five_hour": {"utilization": 100.0},
                            "seven_day": {"utilization": 12.0}})

        self.assertTrue(usage.limited)
        self.assertIn("five_hour", usage.reason)

    def test_a_full_weekly_window_is_limited(self) -> None:
        usage = read_usage({"five_hour": {"utilization": 0.0},
                            "seven_day": {"utilization": 100.0}})

        self.assertTrue(usage.limited)
        self.assertIn("seven_day", usage.reason)

    def test_a_locked_window_is_limited_whatever_it_has_spent(self) -> None:
        # Suspended or over a spend cap: the key stops working just as firmly
        # as a full window, and the percentage alone would never say so.
        usage = read_usage({"five_hour": {"utilization": 3.0,
                                          "locked_reason": "spend_cap"}})

        self.assertTrue(usage.limited)
        self.assertIn("spend_cap", usage.reason)

    def test_a_per_model_window_is_not_rotated_on(self) -> None:
        # A scoped weekly stops one model, not the key. Rotating on it would
        # burn through every subscription for a limit the agent never hit.
        usage = read_usage({"five_hour": {"utilization": 4.0},
                            "seven_day": {"utilization": 9.0},
                            "seven_day_opus": {"utilization": 100.0}})

        self.assertFalse(usage.limited)

    def test_a_window_the_endpoint_stops_reporting_reads_as_fine(self) -> None:
        # The response has grown fields over time. A missing one must not pin
        # the executor to a key by looking like an error.
        self.assertFalse(read_usage({}).limited)
        self.assertFalse(read_usage({"five_hour": None}).limited)
        self.assertFalse(read_usage({"five_hour": {"utilization": None}}).limited)

    def test_a_rejected_key_counts_as_spent(self) -> None:
        # Expired or revoked. As unusable as an exhausted one, and the same
        # answer is the right one: rotate off it.
        with patch("requests.get", return_value=_response(401)):
            usage = fetch_usage("dead-key")

        self.assertTrue(usage.limited)
        self.assertIn("401", usage.reason)

    def test_a_server_error_leaves_the_question_unanswered(self) -> None:
        with patch("requests.get", return_value=_response(503, "upstream down")):
            with self.assertRaises(UsageUnavailable):
                fetch_usage("key")

    def test_an_unreachable_endpoint_leaves_the_question_unanswered(self) -> None:
        with patch("requests.get",
                   side_effect=requests.ConnectionError("no route")):
            with self.assertRaises(UsageUnavailable):
                fetch_usage("key")

    def test_the_key_is_sent_as_a_bearer_token(self) -> None:
        with patch("requests.get", return_value=_response(
                200, json_body={"five_hour": {"utilization": 0.0}})) as get:
            fetch_usage("key-one")

        headers = get.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer key-one")
        self.assertEqual(headers["anthropic-beta"], OAUTH_BETA)


class ClaudeCodeCredentialsTest(unittest.TestCase):
    """The one field Codee writes in Claude Code's credentials file."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.home = Path(self._temporary.name)
        environment = patch.dict(
            os.environ, {credentials.CONFIG_DIR_ENV: str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.path = self.home / credentials.CREDENTIALS_NAME

    def test_the_config_dir_override_is_followed(self) -> None:
        # An installation that moved its config must not get a second
        # credentials file written next to a directory nothing reads.
        self.assertEqual(credentials.credentials_file(), self.path)

    def test_a_missing_file_reads_as_no_token(self) -> None:
        self.assertEqual(credentials.read_access_token(), "")

    def test_an_unreadable_file_reads_as_no_token(self) -> None:
        self.path.write_text("{not json")

        self.assertEqual(credentials.read_access_token(), "")

    def test_signing_in_creates_the_file_when_there_is_none(self) -> None:
        credentials.write_credentials("token-1", "refresh-1", 1789300823639)

        self.assertEqual(credentials.read_access_token(), "token-1")

    def test_the_refresh_token_and_expiry_are_written_too(self) -> None:
        # A session token is good for hours and an agent run can take two, so
        # the CLI has to be left able to renew it mid-run. Writing the access
        # token alone would strand a long run the moment it aged out.
        credentials.write_credentials("token-1", "refresh-1", 1789300823639,
                                      scopes="user:profile user:inference",
                                      subscription_type="max")

        stored = json.loads(self.path.read_text())["claudeAiOauth"]
        self.assertEqual(stored["refreshToken"], "refresh-1")
        self.assertEqual(stored["expiresAt"], 1789300823639)
        self.assertEqual(stored["scopes"], ["user:profile", "user:inference"])
        self.assertEqual(stored["subscriptionType"], "max")

    def test_signing_in_reads_back_as_the_whole_credential(self) -> None:
        credentials.write_credentials("token-1", "refresh-1", 1789300823639)

        stored = credentials.read_credentials()

        self.assertEqual(stored.access_token, "token-1")
        self.assertEqual(stored.refresh_token, "refresh-1")
        self.assertEqual(stored.expires_at, 1789300823639)

    def test_what_the_file_already_says_about_the_plan_is_not_blanked(self) -> None:
        # A caller that knows neither must not wipe what a previous sign-in
        # recorded; only a sign-in reports the scopes and the plan.
        credentials.write_credentials("token-1", "refresh-1", 1,
                                      scopes="user:profile",
                                      subscription_type="max")

        credentials.write_credentials("token-2", "refresh-2", 2)

        stored = json.loads(self.path.read_text())["claudeAiOauth"]
        self.assertEqual(stored["accessToken"], "token-2")
        self.assertEqual(stored["scopes"], ["user:profile"])
        self.assertEqual(stored["subscriptionType"], "max")

    def test_anything_else_in_the_file_survives(self) -> None:
        self.path.write_text(json.dumps({"somethingElse": {"kept": True}}))

        credentials.write_credentials("token-1")

        stored = json.loads(self.path.read_text())
        self.assertEqual(stored["somethingElse"], {"kept": True})

    def test_writing_leaves_no_temp_file_behind(self) -> None:
        credentials.write_credentials("token-1")

        self.assertEqual([path.name for path in self.home.iterdir()],
                         [credentials.CREDENTIALS_NAME])


class ClaudeCodeAccountTest(unittest.TestCase):
    """Reading which account an access key belongs to."""

    PROFILE = {
        "account": {"email": "someone@example.com", "display_name": "Someone",
                    "full_name": "Some One"},
        "organization": {"name": "Example Ltd"},
    }

    def test_the_email_is_what_identifies_the_subscription(self) -> None:
        # Two subscriptions of the same person share a name but never an email.
        self.assertEqual(read_account(self.PROFILE), "someone@example.com")

    def test_a_profile_with_no_email_falls_back_to_a_name(self) -> None:
        # Vague beats blank: a row with no label reads as a failed lookup.
        profile = {"account": {"display_name": "Someone"},
                   "organization": {"name": "Example Ltd"}}

        self.assertEqual(read_account(profile), "Someone")

    def test_a_profile_with_no_account_falls_back_to_the_organization(self) -> None:
        self.assertEqual(read_account({"organization": {"name": "Example Ltd"}}),
                         "Example Ltd")

    def test_a_profile_naming_nobody_names_nobody(self) -> None:
        self.assertEqual(read_account({}), "")
        self.assertEqual(read_account({"account": None}), "")
        self.assertEqual(read_account({"account": {"email": "  "}}), "")

    def test_the_account_is_fetched_as_the_key_s_owner(self) -> None:
        with patch("requests.get", return_value=_response(
                200, json_body=self.PROFILE)) as get:
            self.assertEqual(fetch_account("key-one"), "someone@example.com")

        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"],
                         "Bearer key-one")

    def test_a_rejected_key_has_no_account_to_show(self) -> None:
        with patch("requests.get", return_value=_response(401)):
            with self.assertRaises(AccountUnavailable):
                fetch_account("dead-key")

    def test_a_refusal_says_what_the_api_said(self) -> None:
        # A key revoked and a key short of a scope are both 401, and only the
        # body tells them apart — so it has to reach the page, not just a
        # generic "rejected" nobody can act on.
        with patch("requests.get", return_value=_response(
                401, '{"error":{"message":"missing scope user:profile"}}')):
            with self.assertRaises(AccountUnavailable) as refused:
                fetch_account("key-one")

        self.assertIn("401", str(refused.exception))
        self.assertIn("missing scope user:profile", str(refused.exception))

    def test_an_unreachable_endpoint_has_no_account_to_show(self) -> None:
        with patch("requests.get",
                   side_effect=requests.ConnectionError("no route")):
            with self.assertRaises(AccountUnavailable):
                fetch_account("key-one")

    def test_a_profile_naming_nobody_is_not_an_answer(self) -> None:
        # Caching "" would label the row blank for good and never ask again.
        with patch("requests.get", return_value=_response(200, json_body={})):
            with self.assertRaises(AccountUnavailable):
                fetch_account("key-one")


class ClaudeCodeSignInTest(unittest.TestCase):
    """The OAuth flow Codee connects an account with, the same one `claude /login` runs."""

    TOKEN_RESPONSE = {"access_token": "access-1", "refresh_token": "refresh-1",
                      "expires_in": 3600, "scope": "user:profile user:inference",
                      "subscription_type": "max"}

    def test_the_authorization_url_names_the_claude_code_client(self) -> None:
        # The whole reason for this flow: the client it signs in through is
        # what decides the scopes, and those are what let the token be asked
        # whose account it is and how much allowance it has left.
        url = oauth.start_authorization().url

        self.assertTrue(url.startswith(oauth.AUTHORIZE_URL))
        self.assertIn(f"client_id={oauth.CLIENT_ID}", url)
        self.assertIn("user%3Aprofile", url)
        self.assertIn("user%3Ainference", url)

    def test_the_authorization_url_redirects_to_the_page_that_prints_the_code(self) -> None:
        # Codee has no callback Anthropic would accept, and the machine may
        # have no browser at all, so this is the only flow that can work here.
        self.assertIn(quote(oauth.MANUAL_REDIRECT_URI, safe=""),
                      oauth.start_authorization().url)

    def test_the_challenge_is_the_sha256_of_the_verifier(self) -> None:
        # PKCE, and the reason the verifier must never leave the server: it is
        # what proves the code was redeemed by whoever asked for it.
        authorization = oauth.start_authorization()

        expected = base64.urlsafe_b64encode(
            hashlib.sha256(authorization.code_verifier.encode()).digest()
        ).decode().rstrip("=")
        query = parse_qs(urlparse(authorization.url).query)
        self.assertEqual(query["code_challenge"], [expected])
        self.assertEqual(query["code_challenge_method"], ["S256"])

    def test_two_sign_ins_share_no_secrets(self) -> None:
        first, second = oauth.start_authorization(), oauth.start_authorization()

        self.assertNotEqual(first.state, second.state)
        self.assertNotEqual(first.code_verifier, second.code_verifier)

    def test_the_pasted_code_is_split_from_the_state(self) -> None:
        # Anthropic prints them joined, and the whole string is what gets
        # copied.
        self.assertEqual(oauth.split_code("the-code#the-state"),
                         ("the-code", "the-state"))

    def test_a_bare_code_is_accepted_with_no_state(self) -> None:
        self.assertEqual(oauth.split_code("  the-code  "), ("the-code", ""))

    def test_exchanging_a_code_yields_the_account_s_tokens(self) -> None:
        with patch("requests.post", return_value=_response(
                200, json_body=self.TOKEN_RESPONSE)) as post:
            tokens = oauth.exchange_code("the-code", "the-state", "verifier")

        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["grant_type"], "authorization_code")
        self.assertEqual(sent["code"], "the-code")
        self.assertEqual(sent["code_verifier"], "verifier")
        self.assertEqual(sent["redirect_uri"], oauth.MANUAL_REDIRECT_URI)
        self.assertEqual(tokens.access_token, "access-1")
        self.assertEqual(tokens.refresh_token, "refresh-1")
        self.assertEqual(tokens.subscription_type, "max")
        self.assertIn("user:profile", tokens.scopes)

    def test_the_expiry_comes_back_in_milliseconds(self) -> None:
        # The unit the CLI's credentials file uses; seconds would land in 1970
        # and expire the account the moment it was connected.
        before = time.time()

        with patch("requests.post", return_value=_response(
                200, json_body=self.TOKEN_RESPONSE)):
            tokens = oauth.exchange_code("the-code", "the-state", "verifier")

        self.assertAlmostEqual(tokens.expires_at, (before + 3600) * 1000,
                               delta=5000)

    def test_a_response_naming_no_lifetime_reads_as_already_expired(self) -> None:
        # So the next use renews it rather than trusting a token of unknown age.
        with patch("requests.post", return_value=_response(
                200, json_body={"access_token": "access-1"})):
            self.assertEqual(
                oauth.exchange_code("c", "s", "v").expires_at, 0)

    def test_a_spent_code_says_what_went_wrong(self) -> None:
        # By far the most likely failure — the code is single-use and expires
        # in minutes — and "401" alone would send the user looking at their
        # account instead of at the clock.
        with patch("requests.post", return_value=_response(401)):
            with self.assertRaises(oauth.OAuthApiError) as refused:
                oauth.exchange_code("the-code", "the-state", "verifier")

        self.assertIn("once", str(refused.exception))

    def test_a_response_carrying_no_token_is_a_failure(self) -> None:
        with patch("requests.post", return_value=_response(200, json_body={})):
            with self.assertRaises(oauth.OAuthApiError):
                oauth.exchange_code("the-code", "the-state", "verifier")

    def test_renewing_sends_the_refresh_token_and_the_same_scopes(self) -> None:
        with patch("requests.post", return_value=_response(
                200, json_body=self.TOKEN_RESPONSE)) as post:
            tokens = oauth.refresh_tokens("refresh-0")

        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["grant_type"], "refresh_token")
        self.assertEqual(sent["refresh_token"], "refresh-0")
        self.assertEqual(sent["scope"], " ".join(oauth.SCOPES))
        self.assertEqual(tokens.access_token, "access-1")

    def test_an_unreachable_token_endpoint_is_reported_not_raised_raw(self) -> None:
        with patch("requests.post",
                   side_effect=requests.ConnectionError("no route")):
            with self.assertRaises(oauth.OAuthApiError):
                oauth.refresh_tokens("refresh-0")

    def test_the_refresh_token_s_own_lifetime_is_read_back(self) -> None:
        # The one that ends an account rather than merely ageing its token:
        # past it nothing can be renewed and the user has to sign in again.
        before = time.time()

        with patch("requests.post", return_value=_response(200, json_body={
                **self.TOKEN_RESPONSE, "refresh_token_expires_in": 2592000})):
            tokens = oauth.exchange_code("the-code", "the-state", "verifier")

        self.assertAlmostEqual(tokens.refresh_expires_at,
                               (before + 2592000) * 1000, delta=5000)

    def test_an_unstated_refresh_lifetime_reads_as_unknown_not_expired(self) -> None:
        # Zero has to mean "never said". Treating it as expired would retire a
        # working account and send the user to reconnect it for nothing.
        with patch("requests.post", return_value=_response(
                200, json_body=self.TOKEN_RESPONSE)):
            self.assertEqual(
                oauth.exchange_code("c", "s", "v").refresh_expires_at, 0)

    def test_a_refused_renewal_is_told_apart_from_an_unreachable_one(self) -> None:
        # Opposite meanings for a caller holding a refresh token: one says the
        # credential is finished, the other says nothing at all about it.
        with patch("requests.post", return_value=_response(400, "invalid_grant")):
            with self.assertRaises(oauth.OAuthRefused):
                oauth.refresh_tokens("refresh-0")

        with patch("requests.post", return_value=_response(503, "down")):
            with self.assertRaises(oauth.OAuthApiError) as error:
                oauth.refresh_tokens("refresh-0")
        self.assertNotIsInstance(error.exception, oauth.OAuthRefused)
