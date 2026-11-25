import base64
import contextlib
import hashlib
import json
import re
import secrets
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import anyio
import uvicorn
from starlette.applications import Starlette
from starlette.testclient import TestClient

from codee.lib import agent_requests, runs_db
from codee.lib.runtime_control import set_paused
from codee_main_context.context import CodeeMainContext
from codee_mcp import store as store_module
from codee_mcp.plugin import McpPlugin
from codee_mcp.server import ToolError, check_arguments, send_message
from codee_plugin_abstract.plugin import PluginHost

BASE = "https://codee.example.com"
REDIRECT = "http://localhost:9999/callback"
USER = {"X-Forwarded-Email": "ann@example.com"}


def _plugin(root: Path, base: str = BASE):
    plugin = McpPlugin()
    plugin.data_dir = root / "plugins" / "mcp"
    context = CodeeMainContext(data_dir=root)
    host = PluginHost(lambda: base, lambda: {}, context)
    return plugin, context, Starlette(routes=plugin.http_routes(host))


class Case(unittest.TestCase):
    base = BASE

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.plugin, self.context, app = _plugin(
            Path(directory.name), self.base)
        self.http = TestClient(app, base_url=self.base,
                               follow_redirects=False)
        self.verifier = secrets.token_urlsafe(48)

    def register(self, uri: str = REDIRECT, **extra):
        return self.http.post("/mcp/oauth/register", json={
            "client_name": "<b>Agent</b>", "redirect_uris": [uri], **extra})

    def authorize(self, client_id: str, headers=USER, **changes):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(
            self.verifier.encode()).digest()).decode().rstrip("=")
        return self.http.get("/mcp/oauth/authorize", headers=headers, params={
            "client_id": client_id, "response_type": "code",
            "redirect_uri": REDIRECT, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "s1",
            "resource": self.base + "/mcp", **changes})

    def answer(self, location: str, answer: str = "allow", headers=USER,
               origin: str | None = None):
        request_id = parse_qs(urlsplit(location).query)["request"][0]
        return self.http.post(
            "/mcp/oauth/consent", data={"request": request_id,
                                        "answer": answer},
            headers={**headers, "Origin": origin or self.base})

    def code(self) -> tuple[str, str]:
        client_id = self.register().json()["client_id"]
        done = self.answer(self.authorize(client_id).headers["location"])
        return client_id, parse_qs(
            urlsplit(done.headers["location"]).query)["code"][0]

    def exchange(self, client_id: str, code: str, **changes):
        return self.http.post("/mcp/oauth/token", data={
            "grant_type": "authorization_code", "client_id": client_id,
            "code": code, "redirect_uri": REDIRECT,
            "code_verifier": self.verifier, **changes})

    def refresh(self, client_id: str, token: str):
        return self.http.post("/mcp/oauth/token", data={
            "grant_type": "refresh_token", "client_id": client_id,
            "refresh_token": token})

    def call(self, token: str = "", **headers):
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.http.post("/mcp", headers={
            "Accept": "application/json, text/event-stream", **headers},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


class OAuthTest(Case):
    def test_the_card_shows_the_server_url(self) -> None:
        self.assertIn("{ADMIN_URL}/mcp", self.plugin.describe({}))
        self.assertFalse(self.plugin.ENABLED_BY_DEFAULT)

    def test_no_token_points_at_the_metadata(self) -> None:
        response = self.call()
        self.assertEqual(response.status_code, 401)
        url = re.search(r'resource_metadata="([^"]+)"',
                        response.headers["www-authenticate"])[1]
        resource = self.http.get(url).json()
        self.assertEqual(resource["resource"], BASE + "/mcp")
        self.assertEqual(resource["authorization_servers"], [BASE])
        server = self.http.get("/.well-known/oauth-authorization-server")
        self.assertEqual(server.json()["issuer"], BASE)

    def test_the_whole_flow(self) -> None:
        client_id, code = self.code()
        tokens = self.exchange(client_id, code).json()
        response = self.call(tokens["access_token"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("send_message", response.text)
        # A refresh token or a made-up one is not an access token.
        self.assertEqual(self.call(tokens["refresh_token"]).status_code, 401)
        self.assertEqual(self.call("x").status_code, 401)

    def test_the_redirect_carries_state_and_issuer(self) -> None:
        client_id = self.register().json()["client_id"]
        done = self.answer(self.authorize(client_id).headers["location"])
        query = parse_qs(urlsplit(done.headers["location"]).query)
        self.assertEqual((query["state"], query["iss"]), (["s1"], [BASE]))
        self.assertTrue(done.headers["location"].startswith(REDIRECT))

    def test_clients_get_no_secret_and_unsafe_redirects_are_refused(self):
        body = self.register(token_endpoint_auth_method="client_secret_post")
        self.assertIsNone(body.json().get("client_secret"))
        self.assertEqual(body.json()["token_endpoint_auth_method"], "none")
        for uri in ("http://evil.example/cb", "javascript:alert(1)",
                    "data:text/html,x"):
            self.assertEqual(self.register(uri).status_code, 400, uri)
        for uri in ("https://claude.ai/cb", "cursor://anysphere/cb",
                    "http://127.0.0.1:1/cb"):
            self.assertEqual(self.register(uri).status_code, 201, uri)

    def test_without_the_proxys_header_nothing_is_approved(self) -> None:
        client_id = self.register().json()["client_id"]
        self.assertEqual(self.authorize(client_id, {}).status_code, 403)
        location = self.authorize(client_id).headers["location"]
        self.assertEqual(self.answer(location, headers={}).status_code, 403)

    def test_consent_belongs_to_the_user_and_the_site(self) -> None:
        client_id = self.register().json()["client_id"]
        location = self.authorize(client_id).headers["location"]
        page = self.http.get(location, headers=USER)
        self.assertIn("localhost:9999", page.text)
        self.assertNotIn("<b>Agent</b>", page.text)
        self.assertIn("frame-ancestors 'none'",
                      page.headers["content-security-policy"])
        self.assertEqual(self.answer(
            location, origin="https://evil.example").status_code, 403)
        self.assertEqual(self.answer(location, headers={
            "X-Forwarded-Email": "bob@example.com"}).status_code, 400)
        self.assertEqual(self.answer(location).status_code, 303)
        self.assertEqual(self.answer(location).status_code, 400)

    def test_denying_returns_an_error(self) -> None:
        client_id = self.register().json()["client_id"]
        done = self.answer(self.authorize(client_id).headers["location"],
                           "deny")
        self.assertIn("error=access_denied", done.headers["location"])
        self.assertNotIn("code=", done.headers["location"])

    def test_bad_authorize_requests(self) -> None:
        client_id = self.register().json()["client_id"]
        other = self.authorize(client_id, redirect_uri="https://evil.example")
        self.assertEqual(other.status_code, 400)
        wrong = self.authorize(client_id, resource="https://other.example/mcp")
        self.assertIn("error=invalid_target", wrong.headers["location"])
        plain = self.authorize(client_id, code_challenge_method="plain")
        self.assertIn("error=invalid_request", plain.headers["location"])

    def test_pkce_and_client_binding(self) -> None:
        client_id, code = self.code()
        other = self.register().json()["client_id"]
        self.assertEqual(self.exchange(
            client_id, code, code_verifier="x" * 50).status_code, 400)
        self.assertEqual(self.exchange(other, code).status_code, 400)
        self.assertEqual(self.exchange(client_id, code).status_code, 200)

    def test_a_replayed_code_ends_the_grant(self) -> None:
        client_id, code = self.code()
        tokens = self.exchange(client_id, code).json()
        self.assertEqual(self.exchange(client_id, code).status_code, 400)
        self.assertEqual(self.call(tokens["access_token"]).status_code, 401)

    def test_a_replayed_refresh_token_ends_the_grant(self) -> None:
        client_id, code = self.code()
        first = self.exchange(client_id, code).json()
        second = self.refresh(client_id, first["refresh_token"]).json()
        self.assertEqual(self.call(second["access_token"]).status_code, 200)
        self.assertEqual(self.refresh(
            client_id, first["refresh_token"]).status_code, 400)
        self.assertEqual(self.call(second["access_token"]).status_code, 401)

    def test_tokens_expire_and_the_grant_is_not_extended(self) -> None:
        client_id, code = self.code()
        tokens = self.exchange(client_id, code).json()
        now = time.time()
        with patch.object(store_module.time, "time",
                          return_value=now + store_module.ACCESS_TTL + 1):
            self.assertEqual(self.call(tokens["access_token"]).status_code,
                             401)
            tokens = self.refresh(client_id, tokens["refresh_token"]).json()
        with patch.object(store_module.time, "time",
                          return_value=now + store_module.GRANT_TTL + 1):
            self.assertEqual(self.refresh(
                client_id, tokens["refresh_token"]).status_code, 400)

    def test_revoke_all(self) -> None:
        client_id, code = self.code()
        tokens = self.exchange(client_id, code).json()
        self.assertIn("1 client approval.",
                      self.plugin.run_action("revoke_all", {}))
        self.assertEqual(self.call(tokens["access_token"]).status_code, 401)

    def test_only_hashes_are_stored_in_a_private_file(self) -> None:
        client_id, code = self.code()
        tokens = self.exchange(client_id, code).json()
        path = self.plugin.store().path
        raw = path.read_bytes()
        for secret in (code, tokens["access_token"], tokens["refresh_token"]):
            self.assertNotIn(secret.encode(), raw)
        self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_unused_clients_are_dropped_and_capped(self) -> None:
        with patch.object(store_module, "MAX_CLIENTS", 1):
            self.assertEqual(self.register().status_code, 201)
            self.assertEqual(self.register().status_code, 400)
            later = time.time() + store_module.UNUSED_CLIENT_TTL + 1
            with patch.object(store_module.time, "time", return_value=later):
                self.assertEqual(self.register().status_code, 201)

    def test_foreign_origin_and_host_are_refused(self) -> None:
        client_id, code = self.code()
        token = self.exchange(client_id, code).json()["access_token"]
        self.assertEqual(self.call(
            token, Origin="https://evil.example").status_code, 403)
        self.assertEqual(self.call(token, Host="evil").status_code, 421)
        self.assertEqual(self.http.get("/mcp").status_code, 405)


class LocalOAuthTest(Case):
    base = "http://localhost:8501"

    def test_localhost_needs_no_proxy(self) -> None:
        client_id = self.register().json()["client_id"]
        location = self.authorize(client_id, {}).headers["location"]
        self.assertEqual(self.answer(location, headers={}).status_code, 303)


class WorkerCase(Case):
    """The queue, with a fake agent in place of the executor's."""

    def setUp(self) -> None:
        super().setUp()
        self.calls: list[tuple] = []
        self.gate = threading.Event()
        self.gate.set()
        self.error: Exception | None = None
        self.opened = ""
        self.worker = agent_requests.RequestWorker(
            self.run_agent, self.context, lambda: False, lambda: 2)
        agent_requests.init(self.context)
        self.addCleanup(self.drain)

    def drain(self) -> None:
        """Let the agent threads finish before their database is deleted."""
        self.gate.set()
        deadline = time.monotonic() + 10
        while self.worker._running and time.monotonic() < deadline:
            time.sleep(0.02)

    def run_agent(self, message, session_id, model="", agent="", *, label="",
                  resume=False, on_job=None, on_session=None):
        on_job(None)
        on_session(self.opened or session_id)
        self.calls.append((message, session_id, model, agent, resume))
        self.gate.wait(10)
        if self.error:
            raise self.error
        return f"re: {label}"

    def pump(self) -> None:
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                self.worker.tick()
                time.sleep(0.02)
        thread = threading.Thread(target=loop)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.gate.set)
        self.addCleanup(stop.set)

    def send(self, owner="ann", progress=None, **arguments):
        async def quiet(value, message):
            pass
        return anyio.run(send_message, self.context, owner, arguments,
                         progress or quiet)


class SendMessageTest(WorkerCase):
    def setUp(self) -> None:
        super().setUp()
        self.pump()

    def test_starts_and_continues_a_conversation(self) -> None:
        first = self.send(message="hi", provider="codex", model="gpt-x")
        second = self.send(message="more", session_id=first["session_id"])
        self.assertEqual(first["response"], "re: hi")
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(self.calls[1], (
            "more", first["session_id"], "gpt-x", "codex", True))
        runs = runs_db.recent_runs(main_context=self.context)
        self.assertEqual([run["trigger_type"] for run in runs], ["mcp"] * 2)

    def test_the_session_the_agent_opened_is_returned(self) -> None:
        self.opened = "codex-thread"
        first = self.send(message="hi", provider="codex")
        self.assertEqual(first["session_id"], "codex-thread")
        self.send(message="more", session_id="codex-thread")
        self.assertEqual(self.calls[1][1], "codex-thread")

    def test_someone_elses_or_an_unknown_session_is_refused(self) -> None:
        first = self.send(message="hi")
        for owner, session in (("bob", first["session_id"]), ("ann", "nope")):
            with self.assertRaisesRegex(ToolError, "Unknown session_id"):
                self.send(owner, message="x", session_id=session)

    def test_a_session_keeps_its_provider(self) -> None:
        first = self.send(message="hi", provider="codex")
        with self.assertRaisesRegex(ToolError, "runs on codex"):
            self.send(message="x", session_id=first["session_id"],
                      provider="claude_code")

    def test_a_failed_run_names_the_session(self) -> None:
        self.error = RuntimeError("boom")
        with self.assertRaisesRegex(ToolError, "boom .session_id"):
            self.send(message="hi")

    def test_a_dash_is_not_an_option(self) -> None:
        self.send(message="--version")
        self.assertEqual(self.calls[0][0], "\n--version")

    def test_paused(self) -> None:
        set_paused(self.context, True)
        with self.assertRaisesRegex(ToolError, "paused"):
            self.send(message="hi")

    def test_progress_names_the_session(self) -> None:
        self.gate.clear()
        seen = []

        async def progress(value, message):
            seen.append(message)
            self.gate.set()
        with patch("codee_mcp.server.PROGRESS_SECONDS", 0):
            answer = self.send(message="hi", progress=progress)
        self.assertTrue(any(answer["session_id"] in line for line in seen)
                        or "Waiting" in seen[0])

    def test_cancelling_stops_the_request(self) -> None:
        self.gate.clear()

        async def main():
            with anyio.move_on_after(1):
                await send_message(self.context, "ann", {"message": "hi"},
                                   None)
        anyio.run(main)
        with agent_requests.closing(agent_requests.get_db_connection(
                self.context)) as conn:
            rows = conn.execute("SELECT status FROM agent_requests").fetchall()
        self.assertEqual(rows, [("cancelled",)])


class QueueTest(WorkerCase):
    def test_one_turn_at_a_time(self) -> None:
        self.worker._remember(agent_requests.AgentRequest(
            "r", "ann", "", "codex", "", "s", False, "", "", "", None), "s")
        agent_requests.submit(self.context, "ann", "a", session_id="s")
        with self.assertRaisesRegex(agent_requests.RequestError, "still"):
            agent_requests.submit(self.context, "ann", "b", session_id="s")

    def test_the_limit_holds(self) -> None:
        self.gate.clear()
        self.addCleanup(self.gate.set)
        ids = [agent_requests.submit(self.context, "ann", "x", "codex")
               for _ in range(3)]
        self.worker.tick()
        states = [agent_requests.get(self.context, i).status for i in ids]
        self.assertEqual(states, ["running", "running", "queued"])

    def test_a_restart_fails_what_was_open(self) -> None:
        request = agent_requests.submit(self.context, "ann", "x", "codex")
        with patch.object(threading.Thread, "start"):
            self.worker.start()
        self.assertEqual(
            agent_requests.get(self.context, request).status, "failed")

    def test_cancelling_a_running_request_kills_its_job(self) -> None:
        request = agent_requests.submit(self.context, "ann", "x", "codex")
        self.worker._claim()
        self.worker._update(request, job_id=7)
        with patch.object(runs_db, "kill_job") as kill:
            self.assertTrue(agent_requests.cancel(self.context, request))
            self.assertFalse(agent_requests.cancel(self.context, request))
        kill.assert_called_once_with(7, main_context=self.context)

    def test_arguments(self) -> None:
        for bad in ({}, {"message": " "}, {"message": 1},
                    {"message": "x", "provider": "bash"},
                    {"message": "x", "model": "--help"},
                    {"message": "x", "cwd": "/"}):
            with self.assertRaises(ToolError, msg=bad):
                check_arguments(bad)


class RealClientTest(WorkerCase):
    """The SDK's own client over a socket, on either protocol generation."""

    def setUp(self) -> None:
        with contextlib.closing(socket.socket()) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        super().setUp()
        config = uvicorn.Config(self.http.app, port=port, log_level="error",
                                lifespan="off")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(setattr, server, "should_exit", True)
        while not server.started:
            time.sleep(0.02)
        self.pump()

    def test_both_generations(self) -> None:
        import httpx2
        from mcp.client import Client
        from mcp.client.streamable_http import streamable_http_client

        client_id, code = self.code()
        token = self.exchange(client_id, code).json()["access_token"]

        async def main(mode):
            async with httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"}) as http:
                transport = streamable_http_client(
                    self.base + "/mcp", http_client=http)
                async with Client(transport, mode=mode) as client:
                    tools = await client.list_tools()
                    result = await client.call_tool(
                        "send_message", {"message": "hi"})
                    again = await client.call_tool("send_message", {
                        "message": "more", "session_id":
                        result.structured_content["session_id"]})
                    failed = await client.call_tool(
                        "send_message", {"message": "x", "session_id": "no"})
                    return tools, result, again, failed

        for mode in ("2026-07-28", "legacy"):
            with self.subTest(mode=mode):
                tools, result, again, failed = anyio.run(main, mode)
                self.assertEqual([t.name for t in tools.tools],
                                 ["send_message"])
                self.assertEqual(json.loads(result.content[0].text),
                                 result.structured_content)
                self.assertEqual(again.structured_content, {
                    "response": "re: more", "session_id":
                    result.structured_content["session_id"]})
                self.assertTrue(failed.is_error)


if __name__ == "__main__":
    unittest.main()
