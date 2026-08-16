import json
import tempfile
import unittest
from pathlib import Path

from codee.lib.mcp_config import find_mcp_server, mcp_file, write_mcp_server
from codee_tasks_abstract.provider import McpServer


SERVER = McpServer(
    name="mcp-atlassian",
    command="uvx",
    args=["mcp-atlassian"],
    env={"JIRA_URL": "https://acme.atlassian.net"},
)


class WriteMcpServerTest(unittest.TestCase):
    def _write(self, root: Path, server: McpServer = SERVER) -> dict:
        write_mcp_server(root, server)
        return json.loads(mcp_file(root).read_text())

    def test_it_writes_a_section_each_agent_can_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._write(Path(directory))

            # Claude Code infers stdio from `command`; Copilot needs it spelled out.
            self.assertEqual(config["mcpServers"]["mcp-atlassian"], {
                "command": "uvx",
                "args": ["mcp-atlassian"],
                "env": {"JIRA_URL": "https://acme.atlassian.net"},
            })
            self.assertEqual(config["servers"]["mcp-atlassian"]["type"], "stdio")
            self.assertEqual(config["servers"]["mcp-atlassian"]["command"], "uvx")

    def test_running_it_again_updates_the_entry_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root)

            config = self._write(root, McpServer(
                name="mcp-atlassian", command="uvx", args=["mcp-atlassian"],
                env={"JIRA_URL": "https://new.atlassian.net"}))

            self.assertEqual(config["mcpServers"]["mcp-atlassian"]["env"],
                             {"JIRA_URL": "https://new.atlassian.net"})
            self.assertEqual(len(config["mcpServers"]), 1)

    def test_other_servers_and_other_keys_survive(self) -> None:
        # The file is shared with whatever else the project configured by hand.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mcp_file(root).write_text(json.dumps({
                "mcpServers": {"playwright": {"command": "npx"}},
                "inputs": [{"id": "token"}],
            }))

            config = self._write(root)

            self.assertEqual(config["mcpServers"]["playwright"], {"command": "npx"})
            self.assertIn("mcp-atlassian", config["mcpServers"])
            self.assertEqual(config["inputs"], [{"id": "token"}])

    def test_a_broken_file_is_refused_rather_than_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mcp_file(root).write_text("{not json")

            with self.assertRaises(ValueError) as raised:
                write_mcp_server(root, SERVER)

            self.assertIn(".mcp.json", str(raised.exception))
            self.assertEqual(mcp_file(root).read_text(), "{not json")

    def test_a_section_of_the_wrong_shape_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mcp_file(root).write_text(json.dumps({"servers": []}))

            with self.assertRaises(ValueError) as raised:
                write_mcp_server(root, SERVER)

            self.assertIn("servers", str(raised.exception))

    def test_no_temp_file_is_left_behind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root)

            self.assertEqual([path.name for path in root.iterdir()], [".mcp.json"])


class FindMcpServerTest(unittest.TestCase):
    """The settings page asks whether the server is installed on every load."""

    def test_it_returns_the_entry_that_was_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_mcp_server(root, SERVER)

            self.assertEqual(find_mcp_server(root, "mcp-atlassian")["command"],
                             "uvx")

    def test_a_missing_file_is_simply_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(find_mcp_server(Path(directory), "mcp-atlassian"))

    def test_another_server_does_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mcp_file(root).write_text(
                json.dumps({"mcpServers": {"playwright": {"command": "npx"}}}))

            self.assertIsNone(find_mcp_server(root, "mcp-atlassian"))

    def test_a_broken_file_reads_as_not_configured_rather_than_raising(self) -> None:
        # Setting it up is what reports the problem; this only draws a checkmark.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mcp_file(root).write_text("{not json")

            self.assertIsNone(find_mcp_server(root, "mcp-atlassian"))

    def test_a_hand_written_entry_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mcp_file(root).write_text(
                json.dumps({"mcpServers": {"mcp-atlassian": {"command": "uvx"}}}))

            self.assertIsNotNone(find_mcp_server(root, "mcp-atlassian"))


if __name__ == "__main__":
    unittest.main()
