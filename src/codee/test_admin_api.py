import unittest
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
