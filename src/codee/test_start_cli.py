import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from codee_main_context.logging import DEBUG_ENV_VAR

from codee.start_cli import _ensure_initialized, main


class StartCliTest(unittest.TestCase):
    @patch("codee.start_cli.init_main")
    def test_initializes_empty_current_directory(self, init_main: Mock) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                _ensure_initialized()
            finally:
                os.chdir(original_directory)

        init_main.assert_called_once_with()

    @patch("codee.start_cli.init_main")
    def test_skips_initialization_when_target_exists(self, init_main: Mock) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                Path(".claude").mkdir()
                with self.assertLogs("codee.start_cli", level="INFO") as logs:
                    _ensure_initialized()
            finally:
                os.chdir(original_directory)

        init_main.assert_not_called()
        self.assertEqual(
            logs.output,
            ["INFO:codee.start_cli:Codee is already initialized; skipping codee-init"],
        )

    @patch("codee.start_cli.init_main")
    def test_creates_working_directories_when_initialization_is_skipped(
        self, init_main: Mock
    ) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                Path(".claude").mkdir()
                with self.assertLogs("codee.start_cli", level="INFO"):
                    _ensure_initialized()
                self.assertTrue(Path("repositories").is_dir())
                self.assertTrue(Path("temp").is_dir())
                self.assertTrue(Path("memory").is_dir())
                self.assertEqual(
                    Path(".gitignore").read_text(), "/repositories\n/temp\n")
            finally:
                os.chdir(original_directory)

        init_main.assert_not_called()

    @patch("codee.start_cli._ensure_initialized")
    @patch("codee.start_cli.subprocess.Popen")
    def test_starts_both_services_and_stops_survivor(
        self, popen: Mock, ensure_initialized: Mock
    ) -> None:
        executor = Mock()
        executor.poll.return_value = 0
        admin = Mock()
        admin.poll.return_value = None
        popen.side_effect = [executor, admin]

        with patch.object(sys, "argv", ["codee-start", "--server.port", "8502"]):
            self.assertEqual(main(), 0)

        self.assertEqual(
            popen.call_args_list,
            [
                call([sys.executable, "-m", "codee.executor"]),
                call(
                    [
                        sys.executable,
                        "-m",
                        "codee.admin_cli",
                        "--server.port",
                        "8502",
                    ]
                ),
            ],
        )
        executor.terminate.assert_not_called()
        admin.terminate.assert_called_once_with()
        admin.wait.assert_called_once_with(timeout=5)
        ensure_initialized.assert_called_once_with()

    @patch("codee.start_cli._ensure_initialized")
    @patch("codee.start_cli.subprocess.Popen")
    def test_debug_flag_is_exported_and_not_forwarded(
        self, popen: Mock, ensure_initialized: Mock
    ) -> None:
        executor = Mock()
        executor.poll.return_value = 0
        admin = Mock()
        admin.poll.return_value = None
        popen.side_effect = [executor, admin]

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(DEBUG_ENV_VAR, None)
            with patch.object(sys, "argv", ["codee-start", "--debug", "--port", "8502"]):
                self.assertEqual(main(), 0)
            # Subprocesses inherit the environment, so this is what turns debug
            # on in the executor and the admin UI.
            self.assertEqual(os.environ[DEBUG_ENV_VAR], "1")

        self.assertEqual(
            popen.call_args_list[1],
            call([sys.executable, "-m", "codee.admin_cli", "--port", "8502"]),
        )

    @patch("codee.start_cli._ensure_initialized")
    @patch("codee.start_cli.time.sleep", side_effect=KeyboardInterrupt)
    @patch("codee.start_cli.subprocess.Popen")
    def test_ctrl_c_stops_both_services(
        self, popen: Mock, sleep: Mock, ensure_initialized: Mock
    ) -> None:
        executor = Mock()
        executor.poll.return_value = None
        admin = Mock()
        admin.poll.return_value = None
        popen.side_effect = [executor, admin]

        with patch.object(sys, "argv", ["codee-start"]):
            self.assertEqual(main(), 130)

        executor.terminate.assert_called_once_with()
        admin.terminate.assert_called_once_with()
        ensure_initialized.assert_called_once_with()

    @patch("codee.start_cli._ensure_initialized")
    @patch("codee.start_cli.subprocess.Popen")
    def test_kills_service_that_does_not_stop(
        self, popen: Mock, ensure_initialized: Mock
    ) -> None:
        executor = Mock()
        executor.poll.return_value = 1
        admin = Mock()
        admin.poll.return_value = None
        admin.wait.side_effect = [subprocess.TimeoutExpired("admin", 5), 0]
        popen.side_effect = [executor, admin]

        with patch.object(sys, "argv", ["codee-start"]):
            self.assertEqual(main(), 1)

        admin.kill.assert_called_once_with()
        self.assertEqual(admin.wait.call_args_list, [call(timeout=5), call()])
        ensure_initialized.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
