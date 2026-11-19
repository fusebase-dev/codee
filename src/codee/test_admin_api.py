import unittest
import unittest.mock
from unittest.mock import patch

from starlette.testclient import TestClient

from codee import admin_api


class AzureDevOpsCallbackTest(unittest.TestCase):
    """The OAuth callback route: never renders, always bounces back to /settings."""

    def setUp(self) -> None:
        # follow_redirects off: the 303 target is the thing under test.
        self.client = TestClient(admin_api.api_app, follow_redirects=False)

    def _location(self, response) -> str:
        self.assertEqual(response.status_code, 303)
        return response.headers["location"]

    def test_successful_callback_redirects_with_the_outcome(self) -> None:
        with patch.object(admin_api._service, "complete_azure_authorization",
                          return_value=(True, "Connected to Azure DevOps as dev@acme.com")) as complete:
            response = self.client.get(
                admin_api.CALLBACK_PATH, params={"code": "code-1", "state": "state-1"})

        complete.assert_called_once_with("code-1", "state-1")
        location = self._location(response)
        self.assertTrue(location.startswith("/settings?"))
        self.assertIn("azure=connected", location)
        self.assertIn("dev%40acme.com", location)

    def test_failed_exchange_redirects_as_an_error(self) -> None:
        with patch.object(admin_api._service, "complete_azure_authorization",
                          return_value=(False, "Invalid client secret.")):
            response = self.client.get(
                admin_api.CALLBACK_PATH, params={"code": "code-1", "state": "state-1"})

        self.assertIn("azure=error", self._location(response))

    def test_refused_consent_reports_azures_own_message(self) -> None:
        with patch.object(admin_api._service, "complete_azure_authorization") as complete:
            response = self.client.get(admin_api.CALLBACK_PATH, params={
                "error": "access_denied",
                "error_description": "AADSTS65004: User declined.\r\nTrace ID: abc",
            })

        complete.assert_not_called()
        location = self._location(response)
        self.assertIn("azure=error", location)
        self.assertIn("User+declined.", location)
        # The trace id lines are noise in a toast.
        self.assertNotIn("Trace", location)

    def test_callback_without_a_code_is_rejected(self) -> None:
        with patch.object(admin_api._service, "complete_azure_authorization") as complete:
            response = self.client.get(
                admin_api.CALLBACK_PATH, params={"state": "state-1"})

        complete.assert_not_called()
        self.assertIn("azure=error", self._location(response))


if __name__ == "__main__":
    unittest.main()


class PluginHttpTest(unittest.TestCase):
    """/p/<name>/<path>: handed to the plugin, 404 when it is off or absent."""

    def setUp(self) -> None:
        self.client = TestClient(admin_api.api_app)
        self.plugin = unittest.mock.Mock(NAME="files")

    def _target(self, value):
        return patch.object(admin_api._service, "plugin_http_target",
                            return_value=value)

    def test_the_plugin_gets_the_path_and_its_settings(self) -> None:
        from starlette.responses import PlainTextResponse
        self.plugin.serve_http.return_value = PlainTextResponse("hi")
        with self._target((self.plugin, {"container": "c"})) as target:
            response = self.client.get("/p/files/a/b.html")
        target.assert_called_once_with("files")
        self.assertEqual(response.text, "hi")
        _, path, settings = self.plugin.serve_http.call_args.args
        self.assertEqual((path, settings), ("a/b.html", {"container": "c"}))

    def test_a_missing_or_disabled_plugin_is_a_404(self) -> None:
        with self._target(None):
            self.assertEqual(self.client.get("/p/nope/x").status_code, 404)

    def test_a_plugin_returning_none_is_a_404(self) -> None:
        self.plugin.serve_http.return_value = None
        with self._target((self.plugin, {})):
            self.assertEqual(self.client.get("/p/files/x").status_code, 404)

    def test_a_plugin_error_is_a_502_not_a_crash(self) -> None:
        self.plugin.serve_http.side_effect = RuntimeError("boom")
        with self._target((self.plugin, {})):
            self.assertEqual(self.client.get("/p/files/x").status_code, 502)
