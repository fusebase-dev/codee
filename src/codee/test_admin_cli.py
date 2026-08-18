import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codee.admin_cli import RXCONFIG, _runtime_directory, main


class AdminCliTest(unittest.TestCase):
    def test_runtime_directory_writes_reflex_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch("codee.admin_cli.data_dir", return_value=root / ".codee"):
                runtime = _runtime_directory(root)

            self.assertEqual(runtime, root / ".codee" / "reflex")
            self.assertEqual((runtime / "rxconfig.py").read_text(), RXCONFIG)

    def test_main_launches_single_port_reflex(self) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            try:
                with (
                    patch("codee.admin_cli.project_root", return_value=root),
                    patch("codee.admin_cli.data_dir",
                          return_value=root / ".codee"),
                    patch("reflex.reflex.cli") as reflex_cli,
                    patch.dict(os.environ, {"CODEE_ADMIN_BASE_URL": ""}),
                    patch.object(
                        sys, "argv", ["codee-admin", "--port", "8502"]),
                ):
                    self.assertEqual(main(), 0)
                    launched_argv = sys.argv.copy()
                    project_root_value = os.environ["CODEE_PROJECT_ROOT"]
                    api_url = os.environ["REFLEX_API_URL"]

                reflex_cli.assert_called_once_with()
                self.assertEqual(
                    launched_argv,
                    [
                        "reflex",
                        "run",
                        "--env",
                        "prod",
                        "--single-port",
                        "--frontend-port",
                        "8502",
                    ],
                )
                self.assertEqual(Path.cwd().resolve(),
                                 (root / ".codee" / "reflex").resolve())
                self.assertEqual(
                    Path(project_root_value).resolve(), root.resolve())
                self.assertEqual(api_url, "http://127.0.0.1:8502")
            finally:
                os.chdir(original_directory)

    def test_main_serves_the_frontend_from_a_custom_domain(self) -> None:
        """The compiled bundle has to point the socket at the public origin."""
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            try:
                with (
                    patch("codee.admin_cli.project_root", return_value=root),
                    patch("codee.admin_cli.data_dir",
                          return_value=root / ".codee"),
                    patch("reflex.reflex.cli"),
                    patch.dict(
                        os.environ,
                        {"CODEE_ADMIN_BASE_URL": "https://codee.example.com/"}),
                    patch.object(
                        sys, "argv", ["codee-admin", "--port", "8502"]),
                ):
                    self.assertEqual(main(), 0)
                    launched_argv = sys.argv.copy()
                    api_url = os.environ["REFLEX_API_URL"]

                self.assertEqual(api_url, "https://codee.example.com")
                # The bind port stays local; only the advertised origin moves.
                self.assertEqual(launched_argv[-2:], ["--frontend-port", "8502"])
            finally:
                os.chdir(original_directory)


if __name__ == "__main__":
    unittest.main()
