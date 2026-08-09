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
                    patch.object(
                        sys, "argv", ["codee-admin", "--port", "8502"]),
                ):
                    self.assertEqual(main(), 0)
                    launched_argv = sys.argv.copy()

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
                    Path(os.environ["CODEE_PROJECT_ROOT"]
                         ).resolve(), root.resolve()
                )
                self.assertEqual(
                    os.environ["REFLEX_API_URL"], "http://127.0.0.1:8502")
            finally:
                os.chdir(original_directory)


if __name__ == "__main__":
    unittest.main()
