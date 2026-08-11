import io
import logging
import os
import unittest
from unittest.mock import patch

from codee_main_context.logging import (
    DEBUG_ENV_VAR, configure_logging, debug_enabled, enable_debug, get_logger,
    verbose_debug_enabled)


class LoggingTest(unittest.TestCase):
    def setUp(self) -> None:
        root = logging.getLogger()
        self._handlers = root.handlers[:]
        self._level = root.level
        self.stream = io.StringIO()

    def tearDown(self) -> None:
        root = logging.getLogger()
        root.handlers[:] = self._handlers
        root.setLevel(self._level)
        logging.getLogger("botocore").setLevel(logging.NOTSET)

    def test_debug_enabled_reads_the_environment(self) -> None:
        for value in ("1", "true", "TRUE", "yes", " on ", "all"):
            with patch.dict(os.environ, {DEBUG_ENV_VAR: value}):
                self.assertTrue(debug_enabled(), value)

        for value in ("", "0", "false", "no"):
            with patch.dict(os.environ, {DEBUG_ENV_VAR: value}):
                self.assertFalse(debug_enabled(), value)

    def test_verbose_only_for_all(self) -> None:
        with patch.dict(os.environ, {DEBUG_ENV_VAR: "1"}):
            self.assertFalse(verbose_debug_enabled())
        with patch.dict(os.environ, {DEBUG_ENV_VAR: "all"}):
            self.assertTrue(verbose_debug_enabled())

    def test_enable_debug_exports_the_flag_for_subprocesses(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(DEBUG_ENV_VAR, None)
            enable_debug()
            self.assertEqual(os.environ[DEBUG_ENV_VAR], "1")
            enable_debug(verbose=True)
            self.assertEqual(os.environ[DEBUG_ENV_VAR], "all")

    def test_debug_messages_are_dropped_by_default(self) -> None:
        with patch.dict(os.environ, {DEBUG_ENV_VAR: ""}):
            configure_logging(stream=self.stream)
            log = get_logger("codee.sample")
            log.debug("hidden detail")
            log.info("visible line")

        output = self.stream.getvalue()
        self.assertNotIn("hidden detail", output)
        self.assertIn("visible line", output)

    def test_debug_messages_appear_when_enabled(self) -> None:
        with patch.dict(os.environ, {DEBUG_ENV_VAR: "1"}):
            configure_logging(stream=self.stream)
            get_logger("codee.sample").debug("task %s queued", "NIM-1")

        output = self.stream.getvalue()
        self.assertIn("DEBUG", output)
        self.assertIn("task NIM-1 queued", output)
        self.assertIn("codee.sample", output)

    def test_explicit_debug_also_exports_the_flag(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(DEBUG_ENV_VAR, None)
            configure_logging(debug=True, stream=self.stream)
            self.assertEqual(os.environ[DEBUG_ENV_VAR], "1")
            self.assertEqual(logging.getLogger().level, logging.DEBUG)

    def test_noisy_libraries_stay_quiet_until_debug_all(self) -> None:
        with patch.dict(os.environ, {DEBUG_ENV_VAR: "1"}):
            configure_logging(stream=self.stream)
            self.assertEqual(logging.getLogger(
                "botocore").level, logging.INFO)

        with patch.dict(os.environ, {DEBUG_ENV_VAR: "all"}):
            logging.getLogger("botocore").setLevel(logging.NOTSET)
            configure_logging(stream=self.stream)
            self.assertEqual(logging.getLogger(
                "botocore").level, logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
