import io
import json
import queue
import unittest
from unittest.mock import Mock, patch

from codee_agent_abstract import acp
from codee_agent_abstract.provider import AgentModel


def _effort_option(values: list[str], current: str) -> dict:
    return {"id": "effort", "category": "thought_level", "type": "select",
            "currentValue": current,
            "options": [{"value": value, "name": value} for value in values]}


def _model_option(values: list[str]) -> dict:
    return {"id": "model", "category": "model", "type": "select",
            "currentValue": values[0],
            "options": [{"value": value, "name": value.title()}
                        for value in values]}


class _FakeServer:
    """An ACP server on the other end of Popen's pipes, answering from a table.

    ``efforts`` maps a model id to the effort option the server reports once
    that model is selected: None for a model with no effort option, and an
    Exception for one the server refuses to switch to.
    """

    def __init__(self, session: dict, efforts: dict):
        self._session = session
        self._efforts = efforts
        self._out: queue.Queue[str | None] = queue.Queue()
        self.requests: list[dict] = []
        self.stdin = self
        self.stdout = self
        self.stderr = io.StringIO("")
        self.returncode = None

    def write(self, text: str) -> None:
        request = json.loads(text)
        self.requests.append(request)
        reply = {"jsonrpc": "2.0", "id": request["id"]}
        method = request["method"]
        if method == "initialize":
            reply["result"] = {"protocolVersion": 1}
        elif method == "session/new":
            reply["result"] = self._session
        else:
            effort = self._efforts.get(request["params"]["value"])
            if isinstance(effort, Exception):
                reply["error"] = {"message": str(effort)}
            else:
                reply["result"] = {"configOptions": [effort] if effort else []}
        # Notifications arrive between replies on the real wire.
        self._out.put(json.dumps({"jsonrpc": "2.0",
                                  "method": "session/update", "params": {}}))
        self._out.put(json.dumps(reply) + "\n")

    def flush(self) -> None:
        pass

    def __iter__(self):
        while (line := self._out.get()) is not None:
            yield line

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self._out.put(None)


class AcpCatalogTest(unittest.TestCase):
    def _fetch(self, server: _FakeServer) -> list[AgentModel]:
        with patch("codee_agent_abstract.acp.subprocess.Popen",
                   return_value=server) as popen:
            models = acp.fetch_models(["agent", "--acp"])
        self.assertEqual(popen.call_args.args[0], ["agent", "--acp"])
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        return models

    def test_a_copilot_style_catalog_gets_each_models_efforts(self) -> None:
        # Copilot lists `auto` twice and answers with availableModels; the
        # current effort value is the model's own default.
        server = _FakeServer(
            {"sessionId": "s1",
             "models": {"availableModels": [
                 {"modelId": "auto", "name": "Auto"},
                 {"modelId": "auto", "name": "Auto again"},
                 {"modelId": "claude-opus-5.5", "name": "Claude Opus 5.5"},
                 {"modelId": "gpt-6-sol"},
                 {"modelId": "broken"},
             ]},
             "configOptions": [_model_option(["auto"])]},
            {"auto": None,
             "claude-opus-5.5": _effort_option(["low", "high", "max"], "high"),
             "gpt-6-sol": _effort_option(["none", "low"], "low"),
             "broken": RuntimeError("unknown model")})

        models = self._fetch(server)

        self.assertEqual(models, [
            AgentModel("auto", "Auto"),
            AgentModel("claude-opus-5.5", "Claude Opus 5.5",
                       ("low", "high", "max"), "high"),
            AgentModel("gpt-6-sol", "gpt-6-sol", ("none", "low"), "low"),
            AgentModel("broken", "broken"),
        ])
        switched = [request["params"] for request in server.requests
                    if request["method"] == "session/set_config_option"]
        self.assertEqual(switched[1], {"sessionId": "s1", "configId": "model",
                                       "value": "claude-opus-5.5"})

    def test_an_opencode_style_catalog_drops_the_default_choice(self) -> None:
        # OpenCode only answers with the config option, and lists "default"
        # among the efforts; that is what an unset effort already means.
        server = _FakeServer(
            {"sessionId": "s2",
             "configOptions": [_model_option(["anthropic/claude-opus-5-5"])]},
            {"anthropic/claude-opus-5-5": _effort_option(
                ["low", "max", "default"], "default")})

        self.assertEqual(self._fetch(server), [
            AgentModel("anthropic/claude-opus-5-5",
                       "Anthropic/Claude-Opus-5-5", ("low", "max"), "")])

    def test_a_server_without_config_options_still_lists_its_models(self) -> None:
        server = _FakeServer(
            {"sessionId": "s3",
             "models": {"availableModels": [{"modelId": "m1", "name": "M1"}]}},
            {})

        self.assertEqual(self._fetch(server), [AgentModel("m1", "M1")])
        self.assertNotIn("session/set_config_option",
                         [request["method"] for request in server.requests])


class AcpAwaitResultTest(unittest.TestCase):
    def _queue(self, *lines: str) -> "queue.Queue[str]":
        lines_queue: queue.Queue[str] = queue.Queue()
        for line in lines:
            lines_queue.put(line)
        return lines_queue

    def test_reads_the_session_new_result_past_other_traffic(self) -> None:
        lines = self._queue(
            json.dumps({"jsonrpc": "2.0", "id": 1,
                       "result": {"protocolVersion": 1}}),
            json.dumps(
                {"jsonrpc": "2.0", "method": "session/update", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2,
                       "result": {"sessionId": "s1"}}),
        )

        result = acp.await_result(Mock(poll=Mock(return_value=None)), lines, 2)

        self.assertEqual(result["sessionId"], "s1")

    def test_an_early_exit_raises_instead_of_waiting_out_the_deadline(self) -> None:
        process = Mock(poll=Mock(return_value=1), returncode=1,
                       stderr=Mock(read=Mock(return_value="not logged in")))

        with self.assertRaises(RuntimeError) as caught:
            acp.await_result(process, self._queue(), 2)

        self.assertIn("not logged in", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
