import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from codee_plugin_abstract.plugin import SignInError, TriggeredSkill

from codee_teams import graph as graph_module
from codee_teams.auth import TokenStore, authorization_url
from codee_teams.graph import (
    ChannelInfo, Graph, GraphError, Message, User, html_to_text)
from codee_teams.plugin import (
    CHANNELS_FIELD, MAX_RUNS_PER_TICK, TeamsMessageTrigger, TeamsPlugin)
from codee_teams.render import (
    Channel, conversation, parse_channels, render_prompt)

TEAM = "11111111-2222-3333-4444-555555555555"
CHANNEL = "19:abc123@thread.tacv2"
LINK = ("https://teams.microsoft.com/l/channel/19%3aabc123%40thread.tacv2/"
        f"General?groupId={TEAM}&tenantId=t")


def _at(minutes: int) -> str:
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    return (base + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


def _message(id_, minutes, text="hi", user="u1", reply_to=""):
    return Message(id=id_, created=_at(minutes),
                   user=User(id=user, name=f"User {user}",
                             email=f"{user}@example.com"),
                   text=text, reply_to=reply_to)


def _thread(root, *replies):
    return Message(**{**root.__dict__, "replies": tuple(replies)})


class FakeGraph:
    def __init__(self, threads):
        self.threads = threads
        self.calls = 0

    def recent_threads(self, team_id, channel_id):
        self.calls += 1
        return self.threads


def _skill(body="Answer: {TEAMS_MESSAGES}", channels=LINK, slug="support"):
    return TriggeredSkill(slug=slug, name=slug, path=Path("SKILL.md"),
                          body=body, fields={CHANNELS_FIELD: channels})


class ParseChannelsTest(unittest.TestCase):
    def test_reads_links_and_ids_and_reports_the_rest(self) -> None:
        channels, invalid = parse_channels(
            f"{LINK}\n{TEAM}/19:other@thread.tacv2, nonsense\n{LINK}")

        self.assertEqual(channels, [Channel(TEAM, CHANNEL),
                                    Channel(TEAM, "19:other@thread.tacv2")])
        self.assertEqual(invalid, ["nonsense"])


class ChannelSearchTest(unittest.TestCase):
    class Channels:
        def __init__(self, result):
            self.result, self.calls = result, 0

        def joined_channels(self):
            self.calls += 1
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    def test_finds_channels_by_team_and_channel_words_and_caches(self) -> None:
        graph = self.Channels([
            ChannelInfo("t1", "Support", "19:a@thread.tacv2", "General"),
            ChannelInfo("t1", "Support", "19:b@thread.tacv2", "Escalations"),
            ChannelInfo("t2", "Engineering", "19:c@thread.tacv2", "General"),
        ])
        plugin = TeamsPlugin(graph)

        found = plugin.search_field(CHANNELS_FIELD, "supp gen", {})
        everything = plugin.search_field(CHANNELS_FIELD, "", {})

        self.assertEqual([o.label for o in found], ["Support / General"])
        self.assertEqual(found[0].value, "t1/19:a@thread.tacv2")
        self.assertEqual(len(everything), 3)
        self.assertEqual(graph.calls, 1)

    def test_names_channels_stored_as_ids_or_links(self) -> None:
        plugin = TeamsPlugin(self.Channels([
            ChannelInfo(TEAM, "Support", CHANNEL, "General")]))

        labels = plugin.describe_field_values(
            CHANNELS_FIELD, [LINK, f"{TEAM}/{CHANNEL}", "t9/19:x@thread.tacv2"],
            {})

        self.assertEqual(labels, {
            LINK: "Support / General",
            f"{TEAM}/{CHANNEL}": "Support / General",
            "t9/19:x@thread.tacv2":
                "Unknown channel — not visible to the signed-in account"})

    def test_a_link_to_an_unseen_team_is_named_from_the_link(self) -> None:
        plugin = TeamsPlugin(self.Channels([]))
        link = LINK.replace("General", "Deploy%20Alerts")

        labels = plugin.describe_field_values(CHANNELS_FIELD, [link], {})

        self.assertEqual(labels[link], "Deploy Alerts — not visible to the "
                                       "signed-in account")

    def test_a_missing_permission_says_what_to_do(self) -> None:
        plugin = TeamsPlugin(self.Channels(GraphError("Forbidden", 403)))

        with self.assertRaisesRegex(SignInError, "Team.ReadBasic.All"):
            plugin.search_field(CHANNELS_FIELD, "x", {})

    def test_a_picked_line_parses_back_to_its_channel(self) -> None:
        channels, invalid = parse_channels(
            "t1/19:a@thread.tacv2  # Support, team / General\n" + LINK)

        self.assertEqual(channels, [Channel("t1", "19:a@thread.tacv2"),
                                    Channel(TEAM, CHANNEL)])
        self.assertEqual(invalid, [])


class ConversationTest(unittest.TestCase):
    def test_a_reply_brings_the_whole_thread(self) -> None:
        reply = _message("r2", 20, reply_to="m1")
        thread = _thread(_message("m1", 0), _message("r1", 10,
                                                     reply_to="m1"), reply)

        self.assertEqual([m.id for m in conversation(reply, [thread])],
                         ["m1", "r1", "r2"])

    def test_a_message_brings_the_ten_before_it_without_replies(self) -> None:
        threads = [_thread(_message(f"m{i}", i), _message(f"r{i}", i + 1,
                                                          reply_to=f"m{i}"))
                   for i in range(15)]
        newest = threads[-1]

        ids = [m.id for m in conversation(newest, threads)]

        self.assertEqual(ids, [f"m{i}" for i in range(4, 15)])


class RenderPromptTest(unittest.TestCase):
    def test_replaces_every_placeholder(self) -> None:
        message = _message("r1", 5, text="Is prod down?", reply_to="m1")
        messages = [_message("m1", 0, text="Deploy done"), message]
        body = ("{TEAMS_KIND} {TEAMS_MESSAGE_ID} in {TEAMS_THREAD_ID} on "
                "{TEAMS_CHANNEL_ID}/{TEAMS_TEAM_ID}\n{TEAMS_MESSAGES}\n"
                "{TEAMS_MESSAGES_JSON}")

        prompt = render_prompt(body, Channel(TEAM, CHANNEL), message, messages)

        self.assertTrue(prompt.startswith(
            f"reply r1 in m1 on {CHANNEL}/{TEAM}\n"))
        self.assertIn("[message r1]", prompt)
        self.assertIn("User u1 <u1@example.com> (user id u1)  ← triggered "
                      "this run", prompt)
        data = json.loads(prompt[prompt.index("\n[\n") + 1:])
        self.assertEqual([item["id"] for item in data], ["m1", "r1"])
        self.assertEqual(data[1]["triggered"], True)
        self.assertEqual(data[0]["user"]["email"], "u1@example.com")

    def test_appends_the_messages_when_the_body_has_no_placeholder(self) -> None:
        message = _message("m1", 0, text="Hello")

        prompt = render_prompt("Reply politely.", Channel(TEAM, CHANNEL),
                               message, [message])

        self.assertTrue(prompt.startswith("Reply politely.\n\nMicrosoft Teams "
                                          f"channel {CHANNEL}"))
        self.assertIn("Hello", prompt)


class TriggerBase(unittest.TestCase):
    """A Teams plugin in a temporary directory, signed in, and a fake run."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.plugin = TeamsPlugin()
        self.plugin.data_dir = Path(self._dir.name)
        TokenStore(self.plugin.data_dir).save({"account": {
            "id": "me", "email": "codee@example.com"}})
        self.ran: list[tuple[str, str]] = []

    def _run(self, skill, prompt):
        self.ran.append((skill.slug, prompt))
        return "ok"

    def _trigger(self) -> TeamsMessageTrigger:
        """One trigger for every tick, as the executor keeps one.

        Runs happen right away by default; ``self.held`` set to a list keeps
        them there instead, to be run when the test says.
        """
        if not hasattr(self, "trigger"):
            self.held = None
            self.trigger = TeamsMessageTrigger(self.plugin)
            self.trigger._spawn = lambda work: (
                self.held.append(work) if self.held is not None else work())
        return self.trigger

    def _tick(self, threads, skills=None, settings=None, run=None):
        graph = FakeGraph(threads)
        trigger = self._trigger()
        trigger._graph = graph
        trigger.tick(skills or [_skill()], run or self._run, settings or {})
        return graph


class TriggerTest(TriggerBase):
    def test_the_first_look_runs_nothing(self) -> None:
        self._tick([_thread(_message("m1", 0))])

        self.assertEqual(self.ran, [])

    def test_a_message_posted_after_saving_but_before_the_first_look_runs(self) -> None:
        skill_file = Path(self._dir.name) / "SKILL.md"
        skill_file.write_text("---\n---\n")
        saved = datetime.now(timezone.utc) - timedelta(minutes=30)
        os.utime(skill_file, (saved.timestamp(), saved.timestamp()))
        skill = TriggeredSkill(slug="support", name="support", path=skill_file,
                               body="{TEAMS_MESSAGES}",
                               fields={CHANNELS_FIELD: LINK})
        # _at(0) is an hour ago: before the save. _at(45) is after it.
        history = _thread(_message("old", 0, text="before the skill"))
        posted_while_paused = _thread(_message("new", 45, text="after it"))

        self._tick([history, posted_while_paused], skills=[skill])

        self.assertEqual(len(self.ran), 1)
        self.assertIn("after it", self.ran[0][1])
        self.assertNotIn("triggered this run\nbefore the skill", self.ran[0][1])

    def test_runs_once_per_new_message_and_reply(self) -> None:
        old = _thread(_message("m1", 0))
        self._tick([old])

        reply = _message("r1", 5, text="a reply", reply_to="m1")
        new = _message("m2", 6, text="a new post")
        self._tick([_thread(old, reply), _thread(new)])
        self._tick([_thread(old, reply), _thread(new)])

        self.assertEqual(len(self.ran), 2)
        self.assertIn("A new reply was posted in a thread", self.ran[0][1])
        self.assertIn("a new post", self.ran[1][1])

    def test_ignores_the_signed_in_account_and_listed_senders(self) -> None:
        self._tick([_thread(_message("m1", 0))])

        self._tick([_thread(_message("m1", 0)),
                    _thread(_message("m2", 1, user="me")),
                    _thread(_message("m3", 2, user="bot"))],
                   settings={"ignore_senders": "BOT@example.com"})

        self.assertEqual(self.ran, [])

    def test_one_message_per_channel_per_tick_oldest_first(self) -> None:
        self._tick([])
        threads = [_thread(_message(f"m{i}", i, text=f"text {i}"))
                   for i in range(3)]

        for expected in (1, 2, 3, 3):
            self._tick(threads)
            self.assertEqual(len(self.ran), expected)
        self.assertEqual([prompt.split("text ")[-1][:1] for _, prompt in
                          self.ran], ["0", "1", "2"])

    def test_a_busy_channel_waits_for_its_run(self) -> None:
        self._tick([])
        self._trigger()
        self.held = []
        threads = [_thread(_message("m1", 1)), _thread(_message("m2", 2))]

        self._tick(threads)
        self._tick(threads)
        self.assertEqual(len(self.held), 1)   # m2 waits: m1 is still running

        self.held.pop()()                     # m1's run finishes
        self._tick(threads)
        self.assertEqual(len(self.held), 1)   # now m2 starts

    def test_different_channels_run_side_by_side(self) -> None:
        other = f"{TEAM}/19:other@thread.tacv2"
        skills = [_skill(slug="a"), _skill(slug="b", channels=other)]
        self._tick([], skills=skills)
        self._trigger()
        self.held = []

        self._tick([_thread(_message("m1", 1))], skills=skills)

        self.assertEqual(len(self.held), 2)

    def test_runs_started_per_tick_are_capped(self) -> None:
        channels = [f"{TEAM}/19:c{i}@thread.tacv2" for i in range(5)]
        skills = [_skill(slug=f"s{i}", channels=channel)
                  for i, channel in enumerate(channels)]
        self._tick([], skills=skills)
        self._trigger()
        self.held = []

        self._tick([_thread(_message("m1", 1))], skills=skills)

        self.assertEqual(len(self.held), MAX_RUNS_PER_TICK)

    def test_a_failed_run_is_retried_then_given_up(self) -> None:
        self._tick([])
        threads = [_thread(_message("m1", 0))]
        attempts = []

        def failing(skill, prompt):
            attempts.append(prompt)
            raise RuntimeError("agent failed")

        for _ in range(5):
            self._tick(threads, run=failing)

        self.assertEqual(len(attempts), 3)

    def test_a_killed_run_is_not_retried(self) -> None:
        from codee_plugin_abstract.plugin import RunKilled
        self._tick([])
        threads = [_thread(_message("m1", 0))]
        runs = []

        def killed(skill, prompt):
            runs.append(prompt)
            raise RunKilled("killed from the dashboard")

        for _ in range(3):
            self._tick(threads, run=killed)

        self.assertEqual(len(runs), 1)

    def test_two_skills_on_one_channel_take_turns_and_share_one_fetch(self) -> None:
        skills = [_skill(slug="a"), _skill(slug="b")]
        self._tick([], skills=skills)
        self._trigger()
        self.held = []

        graph = self._tick([_thread(_message("m1", 0))], skills=skills)
        self.assertEqual(len(self.held), 1)   # one run on the channel at a time
        self.assertEqual(graph.calls, 1)
        self.held.pop()()
        self._tick([_thread(_message("m1", 0))], skills=skills)
        self.held.pop()()

        self.assertEqual([slug for slug, _ in self.ran], ["a", "b"])

    def test_state_survives_a_restart(self) -> None:
        self._tick([])
        threads = [_thread(_message("m1", 0))]
        self._tick(threads)

        TeamsPlugin.__init__(self.plugin)  # a fresh process reads the file
        self._tick(threads)

        self.assertEqual(len(self.ran), 1)


class GraphTest(unittest.TestCase):
    def _response(self, payload):
        class Response:
            ok = True
            status_code = 200

            def json(self_inner):
                return payload
        return Response()

    def test_reduces_graph_messages_and_follows_reply_pages(self) -> None:
        pages = {
            "messages": {"value": [{
                "id": "m1", "messageType": "message",
                "createdDateTime": "2026-11-01T09:00:00Z",
                "from": {"user": {"id": "u1", "displayName": "Jane"}},
                "body": {"contentType": "html",
                         "content": "<p>Hi <b>all</b></p><p>a&amp;b</p>"},
                "replies": [{"id": "r1", "messageType": "message",
                             "replyToId": "m1",
                             "createdDateTime": "2026-11-01T09:01:00Z",
                             "from": {"user": {"id": "u1"}},
                             "body": {"contentType": "text",
                                      "content": "first"}}],
                "replies@odata.nextLink": "https://graph/next",
            }, {"id": "sys", "messageType": "systemEventMessage"}]},
            "next": {"value": [{"id": "r2", "messageType": "message",
                                "replyToId": "m1",
                                "createdDateTime": "2026-11-01T09:02:00Z",
                                "deletedDateTime": None,
                                "from": {"user": {"id": "u2"}},
                                "body": {"content": "second"}}]},
            "user": {"mail": "jane@example.com"},
        }

        def get(url, params=None, **kwargs):
            if url.endswith("/messages"):
                return self._response(pages["messages"])
            if url == "https://graph/next":
                return self._response(pages["next"])
            return self._response(pages["user"])

        with patch.object(graph_module.requests, "get", side_effect=get):
            threads = Graph(lambda: "token").recent_threads(TEAM, CHANNEL)

        self.assertEqual([t.id for t in threads], ["m1"])
        self.assertEqual(threads[0].text, "Hi all\na&b")
        self.assertEqual(threads[0].user.email, "jane@example.com")
        self.assertEqual([r.id for r in threads[0].replies], ["r1", "r2"])
        self.assertEqual(threads[0].replies[1].reply_to, "m1")

    def test_html_to_text_keeps_line_breaks(self) -> None:
        self.assertEqual(html_to_text("<div>one<br>two</div><ul><li>x</li></ul>"),
                         "one\ntwo\n- x")


class AuthTest(unittest.TestCase):
    def test_sign_in_needs_the_app_details(self) -> None:
        with self.assertRaises(SignInError):
            authorization_url({}, "http://x/cb", "state", "verifier")

    def test_the_sign_in_url_asks_for_the_delegated_scopes_with_pkce(self) -> None:
        url = authorization_url({"tenant_id": "contoso", "client_id": "cid",
                                 "client_secret": "s"}, "http://x/cb",
                                "state", "verifier")

        self.assertTrue(url.startswith(
            "https://login.microsoftonline.com/contoso/oauth2/v2.0/authorize?"))
        self.assertIn("ChannelMessage.Read.All", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertNotIn("verifier", url)


if __name__ == "__main__":
    unittest.main()


class DecideTest(unittest.TestCase):
    from codee_teams import decide as decide_module

    def _post(self, payload, status=200):
        calls = []

        class Response:
            ok = status < 400
            status_code = status
            reason = "error"

            def json(self_inner):
                return payload

        def post(url, **kwargs):
            calls.append((url, kwargs))
            return Response()
        return post, calls

    def _message(self):
        return _message("m9", 30, text="Is prod down? I get 502s.", user="bob")

    def test_openai_decisions_get_one_choice_question(self) -> None:
        from codee_teams.decide import decide
        post, calls = self._post({"model": "gpt-6-luna", "answers": [{
            "type": "choice", "name": "topic",
            "choice": "Production incidents",
            "probabilities": [
                {"value": "Production incidents", "probability": 0.91},
                {"value": "Billing", "probability": 0.04},
                {"value": "none of these", "probability": 0.05}],
            "confidence": 0.9}]})
        settings = {"decision_provider": "openai", "openai_api_key": "sk-1"}

        with patch.object(self.decide_module.requests, "post", side_effect=post):
            verdict = decide(settings, ["Production incidents", "Billing"],
                             self._message(), "the conversation")

        self.assertEqual((verdict.matched, verdict.topic, verdict.reason),
                         (True, "Production incidents", "0.91"))
        url, kwargs = calls[0]
        self.assertEqual(url, "https://api.openai.com/v1/decisions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-1")
        body = kwargs["json"]
        self.assertEqual(body["model"], "gpt-6-luna")
        self.assertTrue(body["input"].startswith(
            "TRIGGERING MESSAGE:\nUser bob wrote:\nIs prod down? I get 502s."))
        self.assertIn("the conversation", body["input"])
        (question,) = body["questions"]
        self.assertEqual(question["type"], "choice")
        self.assertIn("TRIGGERING MESSAGE", question["instructions"])
        self.assertIn("part of the conversation it belongs to",
                      question["instructions"])
        self.assertNotIn("does not count", question["instructions"])
        self.assertEqual([c["value"] for c in question["choices"]],
                         ["Production incidents", "Billing", "none of these"])

    def test_openai_none_refusal_and_errors(self) -> None:
        from codee_teams.decide import DecisionError, decide
        settings = {"openai_api_key": "k", "openai_base_url": "https://gw/v1/"}

        post, calls = self._post({"answers": [{
            "type": "choice", "choice": "none of these",
            "probabilities": [{"value": "none of these", "probability": 0.8}]}]})
        with patch.object(self.decide_module.requests, "post", side_effect=post):
            verdict = decide(settings, ["Billing"], self._message(), "")
        self.assertFalse(verdict.matched)
        self.assertEqual(calls[0][0], "https://gw/v1/decisions")

        post, _ = self._post({"answers": [{"type": "refusal", "name": "topic"}]})
        with patch.object(self.decide_module.requests, "post", side_effect=post):
            verdict = decide(settings, ["Billing"], self._message(), "")
        self.assertEqual((verdict.matched, verdict.reason),
                         (False, "the model declined to decide"))

        post, _ = self._post({"error": {"message": "invalid key"}}, status=401)
        with patch.object(self.decide_module.requests, "post", side_effect=post):
            with self.assertRaisesRegex(DecisionError, "401: invalid key"):
                decide(settings, ["Billing"], self._message(), "")

        with self.assertRaisesRegex(DecisionError, "API key"):
            decide({}, ["Billing"], self._message(), "")

    def test_azure_openai_gets_its_api_key_header(self) -> None:
        from codee_teams.decide import decide
        post, calls = self._post({"answers": [{"type": "choice",
                                               "choice": "none of these"}]})
        settings = {"openai_api_key": "az-1", "openai_base_url":
                    "https://contoso.openai.azure.com/openai/v1/"}

        with patch.object(self.decide_module.requests, "post", side_effect=post):
            decide(settings, ["Billing"], self._message(), "")

        url, kwargs = calls[0]
        self.assertEqual(url,
                         "https://contoso.openai.azure.com/openai/v1/decisions")
        self.assertEqual(kwargs["headers"], {"api-key": "az-1"})
        self.assertEqual(kwargs["json"]["model"], "gpt-6-luna")

    def test_typesafe_asks_one_noul_question_per_topic(self) -> None:
        from codee_teams.decide import decide
        post, calls = self._post({"model": "jev-latest", "answers": {
            "topic_0": {"type": "noul", "noul": 0.93},
            "topic_1": {"type": "noul", "noul": 0.12}}})
        settings = {"decision_provider": "typesafe", "typesafe_api_key": "ts-1"}

        with patch.object(self.decide_module.requests, "post", side_effect=post):
            verdict = decide(settings, ["Production incidents", "Billing"],
                             self._message(), "the conversation")

        self.assertEqual((verdict.matched, verdict.topic),
                         (True, "Production incidents"))
        url, kwargs = calls[0]
        self.assertEqual(url, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer ts-1")
        body = kwargs["json"]
        self.assertEqual(body["model"], "jev-latest")
        self.assertTrue(body["state"].startswith(
            "TRIGGERING MESSAGE:\nUser bob wrote:\nIs prod down?"))
        self.assertEqual(sorted(body["questions"]), ["topic_0", "topic_1"])
        question = body["questions"]["topic_1"]
        self.assertEqual(question["type"], "noul")
        self.assertIn("Is the TRIGGERING MESSAGE about this topic: Billing?",
                      question["instructions"])
        self.assertIn("a reply that continues a discussion about the topic "
                      "counts", question["instructions"])

    def test_typesafe_below_the_threshold_is_no_match(self) -> None:
        from codee_teams.decide import decide
        post, _ = self._post({"answers": {"topic_0": {"noul": 0.62}}})
        settings = {"decision_provider": "typesafe", "typesafe_api_key": "k",
                    "typesafe_threshold": "0.8"}

        with patch.object(self.decide_module.requests, "post", side_effect=post):
            verdict = decide(settings, ["Billing"], self._message(), "")

        self.assertFalse(verdict.matched)
        self.assertIn("below 0.80", verdict.reason)


class TopicBase(TriggerBase):
    """A Specific topics skill, and a stand-in for the decision model."""

    def _topic_skill(self):
        from codee_teams.plugin import MODE_FIELD, TOPICS_FIELD
        return TriggeredSkill(
            slug="incidents", name="incidents", path=Path("SKILL.md"),
            body="Topic: {TEAMS_TOPIC}\n{TEAMS_MESSAGES}",
            fields={CHANNELS_FIELD: LINK, MODE_FIELD: "topics",
                    TOPICS_FIELD: "Production incidents\nBilling\n"})

    def _decide(self, verdicts):
        from codee_teams import plugin as plugin_module
        asked = []

        def decide(settings, topics, message, conversation):
            asked.append((topics, message.id, conversation))
            verdict = verdicts.pop(0)
            if isinstance(verdict, Exception):
                raise verdict
            return verdict
        return patch.object(plugin_module, "decide", side_effect=decide), asked



class TopicModeTest(TopicBase):
    """A skill in Specific topics mode runs only on what the model matches."""

    def test_runs_only_for_a_matched_message_and_passes_the_topic(self) -> None:
        from codee_teams.decide import Decision
        skill = self._topic_skill()
        self._tick([], skills=[skill])
        threads = [_thread(_message("m1", 1, text="lunch?")),
                   _thread(_message("m2", 2, text="prod is down"))]
        patcher, asked = self._decide([
            Decision(False, reason="small talk"),
            Decision(True, "Production incidents", "outage")])

        with patcher:
            self._tick(threads, skills=[skill])
            self._tick(threads, skills=[skill])

        self.assertEqual([(topics, mid) for topics, mid, _ in asked],
                         [(["Production incidents", "Billing"], "m1"),
                          (["Production incidents", "Billing"], "m2")])
        self.assertIn("← triggered this run", asked[1][2])
        self.assertEqual(len(self.ran), 1)
        self.assertTrue(self.ran[0][1].startswith(
            "Topic: Production incidents\n"))
        self.assertIn("prod is down", self.ran[0][1])

    def test_a_failing_decision_is_retried_then_given_up(self) -> None:
        from codee_teams.decide import DecisionError
        skill = self._topic_skill()
        self._tick([], skills=[skill])
        patcher, asked = self._decide([DecisionError("down")] * 5)

        with patcher:
            for _ in range(5):
                self._tick([_thread(_message("m1", 1))], skills=[skill])

        self.assertEqual(len(asked), 3)
        self.assertEqual(self.ran, [])

    def test_all_messages_mode_never_asks_the_model(self) -> None:
        self._tick([])
        patcher, asked = self._decide([])

        with patcher:
            self._tick([_thread(_message("m1", 1))])

        self.assertEqual(asked, [])
        self.assertEqual(len(self.ran), 1)


class TestDecisionActionTest(unittest.TestCase):
    def _run(self, result):
        from codee_teams import plugin as plugin_module
        asked = []

        def decide(settings, topics, message, conversation):
            asked.append((settings, topics, message.text))
            if isinstance(result, Exception):
                raise result
            return result
        with patch.object(plugin_module, "decide", side_effect=decide):
            outcome = TeamsPlugin().run_action(
                "test_decision", {"decision_provider": "typesafe"})
        return outcome, asked

    def test_reports_the_expected_pick(self) -> None:
        from codee_teams.decide import Decision
        from codee_teams.plugin import TEST_TOPICS

        outcome, asked = self._run(Decision(True, TEST_TOPICS[0], "0.97"))

        self.assertTrue(outcome.startswith("TypeSafe (Jev) works."))
        self.assertIn("(0.97), as expected", outcome)
        self.assertEqual(asked[0][1], TEST_TOPICS)
        self.assertIn("502", asked[0][2])

    def test_says_when_the_model_answers_but_picks_wrong(self) -> None:
        from codee_teams.decide import Decision

        outcome, _ = self._run(Decision(False, reason="below 0.70"))

        self.assertIn("matched no topic (below 0.70)", outcome)
        self.assertIn("check the model or threshold", outcome)

    def test_a_failure_is_raised_for_the_card_to_show(self) -> None:
        from codee_teams.decide import DecisionError

        with self.assertRaisesRegex(DecisionError, "401"):
            self._run(DecisionError("TypeSafe 401: bad key"))


class DecisionLogTest(TopicBase):
    """Every decision lands in the log the Plugins page shows."""

    def test_match_no_match_and_error_are_logged_newest_first(self) -> None:
        from codee_teams.decide import Decision, DecisionError
        skill = self._topic_skill()
        self._tick([], skills=[skill])
        threads = [_thread(_message("m1", 1, text="lunch?")),
                   _thread(_message("m2", 2, text="prod is down\nsee logs"),
                           _message("r1", 3, text="are you sure?",
                                    reply_to="m2"))]
        patcher, _ = self._decide([
            Decision(False, reason="none of the topics 0.60",
                     scores={"none of these": 0.6, "Billing": 0.3}),
            Decision(True, "Production incidents", "0.88",
                     {"Production incidents": 0.88, "none of these": 0.1}),
            DecisionError("OpenAI 429: slow down")])

        with patcher:
            self._tick(threads, skills=[skill])
            # The reply is decided on the next tick: the channel had a run.
            self._tick(threads, skills=[skill])

        entries = self.plugin.read_log(50)
        self.assertEqual([e.status for e in entries], ["error", "ok", "skip"])
        error, match, skip = entries
        self.assertIn("are you sure?", error.title)
        self.assertIn("reply in a thread", error.detail)
        self.assertIn("Error: OpenAI 429: slow down", error.detail)
        self.assertEqual(match.title, "incidents · User u1: prod is down")
        self.assertIn('Decision: matched "Production incidents" (0.88)',
                      match.detail)
        self.assertIn("Scores: Production incidents 0.88  ·  none of these "
                      "0.10", match.detail)
        self.assertIn("Topics asked: Production incidents; Billing",
                      match.detail)
        self.assertIn("Decision: no match (none of the topics 0.60)",
                      skip.detail)

    def test_all_messages_mode_logs_nothing(self) -> None:
        self._tick([])
        self._tick([_thread(_message("m1", 1))])

        self.assertEqual(self.plugin.read_log(50), [])

    def test_the_log_trims_itself(self) -> None:
        from codee_teams import plugin as plugin_module
        log_file = plugin_module._DecisionLog(
            self.plugin.data_dir / plugin_module.DECISION_LOG)

        with patch.object(plugin_module, "LOG_MAX", 5), \
                patch.object(plugin_module, "LOG_KEEP", 3):
            for index in range(6):
                log_file.append({"index": index})

        self.assertEqual([r["index"] for r in log_file.read(50)], [3, 4, 5])
