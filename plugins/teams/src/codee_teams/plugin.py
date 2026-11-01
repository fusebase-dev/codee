"""Run a skill when a message is posted in the Teams channels it watches."""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse
from pathlib import Path

from codee_plugin_abstract.plugin import (
    AbstractCodeePlugin, AbstractSkillTrigger, FieldOption, LogEntry,
    PluginAction, PluginField, RunKilled, RunSkill, SignInError,
    TriggeredSkill)

from codee_teams.auth import SignedOut, TokenStore, authorization_url
from codee_teams.decide import (
    OPENAI, PROVIDERS, TYPESAFE, TYPESAFE_MODEL,
    TYPESAFE_THRESHOLD, DecisionError, decide)
from codee_teams.graph import ChannelInfo, Graph, GraphError, Message, User
from codee_teams.render import (
    Channel, conversation, format_messages, parse_channels, render_prompt)

log = logging.getLogger(__name__)

CHANNELS_FIELD = "x-teams-channels"
MODE_FIELD = "x-teams-mode"
TOPICS_FIELD = "x-teams-topics"
ALL_MESSAGES = "all"
SPECIFIC_TOPICS = "topics"
# The decision log: one JSON line per decision, trimmed back to the newest
# LOG_KEEP lines whenever it grows past LOG_MAX.
DECISION_LOG = "decisions.jsonl"
LOG_KEEP = 1000
LOG_MAX = 2000
TEST_DECISION = "test_decision"
# What "Test decision model" asks: a message plainly about the first topic.
TEST_TOPICS = ["Production incidents and outages", "Lunch and office plans"]
TEST_MESSAGE = ("The API returns 502 for every request since 10:00 and "
                "customers can't log in. Is anyone looking at it?")

# Decision-model calls per tick across every skill and channel, so a burst of
# messages in a busy channel can't hold the executor's loop for long.
MAX_DECISIONS_PER_TICK = 20
# Runs started per tick across every skill and channel. Each runs in a thread
# of its own, one per channel at a time (see TeamsMessageTrigger).
MAX_RUNS_PER_TICK = 3
# A run that keeps failing is given up after this many tries.
MAX_ATTEMPTS = 3
# Messages older than this never fire a run, and are forgotten from the state.
LOOKBACK = timedelta(days=14)
# How long the list of channels to search stays: listing them is a call per
# team, and each keystroke in the search box asks again.
CHANNELS_CACHE_SECONDS = 300
MAX_SEARCH_RESULTS = 50

DESCRIPTION = """\
Runs once for every new message in the channels listed above: a new
top-level message or a reply in a thread. In **Specific topics** mode it runs
only for a message the decision model (set on the Plugins page) judges to be
about one of the skill's topics. The model is asked about the message that
fired the run, read as part of its conversation: a reply that continues a
discussion about a topic ("are you sure?" in a thread about an outage) counts. Messages from the signed-in account
and from **Ignore messages from** (Plugins page) never fire it. Messages
posted before the skill was saved with a channel are skipped; anything posted
after that runs, even if Codee was paused or restarting at the time.

**What the skill gets**

The message that fired the run, with its full text, together with the
conversation around it, oldest first:

- **a reply in a thread:** the whole thread — the first message and every
  reply, including the new one;
- **a new top-level message:** up to 10 top-level messages posted before it
  (without their replies), then the new message itself, last.

The message that fired the run is always included and marked
*← triggered this run* (in JSON: `"triggered": true`). Each message carries
its ID, date, text and the sender's name, email and user ID. For a reply,
`{TEAMS_MESSAGES}` looks like this:

```
A new reply was posted in a thread. The whole thread, oldest first:

[message 1730452800000] 2026-11-01 09:20:00 UTC — Jane Doe <jane@contoso.com> (user id 8f2e…)
Deploy to prod finished.

[message 1730453100000] 2026-11-01 09:25:00 UTC — Bob Smith <bob@contoso.com> (user id 3c1a…)  ← triggered this run
Is prod down? I get 502s.
```

**Placeholders** — put them anywhere in the skill body:

| Placeholder | Replaced with |
| --- | --- |
| `{TEAMS_MESSAGES}` | the conversation described above, **including the message that fired the run** (marked), as readable text |
| `{TEAMS_MESSAGES_JSON}` | the same messages as JSON; the one that fired the run has `"triggered": true` |
| `{TEAMS_CHANNEL_ID}` | the channel ID |
| `{TEAMS_TEAM_ID}` | the team ID |
| `{TEAMS_MESSAGE_ID}` | the ID of the message that fired the run |
| `{TEAMS_THREAD_ID}` | the thread's first message ID (its own ID for a top-level message) |
| `{TEAMS_KIND}` | `reply` or `message` |
| `{TEAMS_TOPIC}` | the topic the message matched, as listed on the skill (empty in *All messages* mode) |

A body with none of them gets `{TEAMS_MESSAGES}` appended at the end.
"""


SETUP = """\
Codee reads Teams as a user you sign in with, through an app registered in
Microsoft Entra ID. Registering it takes an Entra admin, or someone allowed to
register apps plus an admin to consent.

**1. Register the app and give it its permissions**

1. Open the [Microsoft Entra admin center](https://entra.microsoft.com) →
   **Identity** → **Applications** → **App registrations** → **New
   registration**.
2. **Name:** anything, e.g. `Codee Teams`.
3. **Supported account types:** *Accounts in this organizational directory
   only (Single tenant)*.
4. **Redirect URI:** platform **Web**, value
   `{REDIRECT_URI}`
5. **Register**.
6. In the new app, open **API permissions** → **Add a permission** →
   **Microsoft Graph** → **Delegated permissions**, and add all six:

   | Permission | What Codee uses it for |
   | --- | --- |
   | `ChannelMessage.Read.All` | read channel messages |
   | `Team.ReadBasic.All` | list your teams, to search for channels |
   | `Channel.ReadBasic.All` | list each team's channels, to search for them |
   | `User.ReadBasic.All` | read senders' names and emails |
   | `User.Read` | read who is signed in (usually already there) |
   | `offline_access` | stay signed in |

7. Click **Grant admin consent for &lt;your organization&gt;** and check that
   every permission shows a green *Granted* status. Without it, signing in
   fails with *"Need admin approval"*.

**2. Copy the IDs**

On the app's **Overview**, copy **Application (client) ID** and **Directory
(tenant) ID** into the fields below.

**3. Create a client secret**

**Certificates & secrets** → **Client secrets** → **New client secret**.
Copy its **Value** (not the *Secret ID*) into **Client secret** below; it is
shown only once. Note when it expires: Teams stops working then, until a new
secret is saved here.

**4. Sign in**

Fill in the fields below, **Save settings**, then **Sign in** with the account
Codee should read Teams as. It sees only the teams that account is a member
of, so add it to every team whose channels a skill watches.

Put the account the agent replies as under **Ignore messages from**, so its
own replies don't start new runs.

"""


class TeamsPlugin(AbstractCodeePlugin):
    NAME = "teams"
    DISPLAY_NAME = "Microsoft Teams"
    DESCRIPTION = SETUP
    SUPPORTS_SIGN_IN = True
    ACTIONS = [PluginAction(TEST_DECISION, "Test decision model")]
    LOG_LABEL = "Decision log"
    SETTINGS_FIELDS = [
        PluginField("tenant_id", "Directory (tenant) ID",
                    hint="Leave empty for any work or school account."),
        PluginField("client_id", "Application (client) ID",
                    hint="An Entra app with the delegated Graph permissions "
                         "ChannelMessage.Read.All, User.ReadBasic.All, "
                         "User.Read, offline_access, Team.ReadBasic.All and "
                         "Channel.ReadBasic.All, admin-consented."),
        PluginField("client_secret", "Client secret", secret=True),
        PluginField("ignore_senders", "Ignore messages from",
                    multiline=True, placeholder="bot@example.com",
                    hint="Emails or user IDs, one per line. The signed-in "
                         "account is always ignored."),
        PluginField("decision_provider", "Decision model",
                    choices=PROVIDERS, default=OPENAI,
                    hint="Decides, for skills in Specific topics mode, "
                         "whether a new message is about one of the "
                         "skill's topics."),
        PluginField("openai_api_key", "OpenAI API key", secret=True,
                    visible_when=("decision_provider", OPENAI)),
        PluginField("openai_base_url", "OpenAI base URL",
                    placeholder="https://api.openai.com/v1",
                    visible_when=("decision_provider", OPENAI),
                    hint="Leave empty for OpenAI. For Azure OpenAI: "
                         "https://<resource>.openai.azure.com/openai/v1, with "
                         "the resource's key above and a gpt-6-luna "
                         "deployment named gpt-6-luna."),
        PluginField("typesafe_api_key", "TypeSafe API key", secret=True,
                    visible_when=("decision_provider", TYPESAFE),
                    hint="From console.typesafe.ai/keys."),
        PluginField("typesafe_model", "TypeSafe model", default=TYPESAFE_MODEL,
                    visible_when=("decision_provider", TYPESAFE),
                    hint="jev-latest, or a version such as jev-1.14.0 to "
                         "keep the threshold below tuned to one model."),
        PluginField("typesafe_threshold", "Match threshold",
                    default=str(TYPESAFE_THRESHOLD),
                    visible_when=("decision_provider", TYPESAFE),
                    hint="TypeSafe answers with the probability that a "
                         "message is about a topic (0 to 1); from this "
                         "value up, it counts as a match."),
    ]

    def __init__(self, graph: Graph | None = None) -> None:
        self._graph = graph
        self._channels: tuple[float, list[ChannelInfo]] | None = None

    @property
    def tokens(self) -> TokenStore:
        return TokenStore(self.data_dir)

    def search_field(self, key, query, settings):
        """Channels of the signed-in account's teams matching ``query``."""
        if key != CHANNELS_FIELD:
            return []
        words = query.lower().split()
        matches = [channel for channel in self._joined_channels(settings)
                   if all(word in f"{channel.team_name} {channel.channel_name}"
                          .lower() for word in words)]
        return [FieldOption(value=f"{channel.team_id}/{channel.channel_id}",
                            label=_label(channel))
                for channel in matches[:MAX_SEARCH_RESULTS]]

    def describe_field_values(self, key, values, settings):
        """Names for channels stored as bare IDs or pasted links."""
        if key != CHANNELS_FIELD:
            return {}
        known = {Channel(c.team_id, c.channel_id): _label(c)
                 for c in self._joined_channels(settings)}
        labels = {}
        for value in values:
            channels, _ = parse_channels(value)
            if channels and channels[0] in known:
                labels[value] = known[channels[0]]
            elif channels:
                # Its team isn't one the account has joined, so it never sees
                # this channel's messages either: say so rather than show IDs.
                labels[value] = (f"{_name_in_link(value) or 'Unknown channel'}"
                                 " — not visible to the signed-in account")
        return labels

    def _joined_channels(self, settings) -> list[ChannelInfo]:
        if self._channels and time.monotonic() - self._channels[0] \
                < CHANNELS_CACHE_SECONDS:
            return self._channels[1]
        graph = self._graph or Graph(
            lambda: self.tokens.access_token(settings))
        try:
            channels = sorted(graph.joined_channels(),
                              key=lambda c: (c.team_name.lower(),
                                             c.channel_name.lower()))
        except SignedOut as exc:
            raise SignInError(str(exc)) from exc
        except GraphError as exc:
            if exc.status == 403:
                raise SignInError(
                    "Teams did not allow listing teams. In the Entra app, add "
                    "the delegated permissions Team.ReadBasic.All and "
                    "Channel.ReadBasic.All, grant admin consent, then Sign out "
                    "and Sign in on the Plugins page (see How to set it up "
                    "there).") from exc
            raise SignInError(str(exc)) from exc
        self._channels = (time.monotonic(), channels)
        return channels

    def read_log(self, limit):
        """The latest decisions on Specific topics skills, newest first."""
        return [_log_entry(record) for record in
                reversed(_DecisionLog(self.data_dir / DECISION_LOG)
                         .read(limit))]

    def run_action(self, key, settings):
        """Ask the selected decision model about a sample message."""
        if key != TEST_DECISION:
            raise ValueError(f"No action {key}")
        message = Message(id="test", created="2026-01-01T10:05:00Z",
                          user=User(name="Test user"), text=TEST_MESSAGE)
        provider = dict(PROVIDERS).get(
            settings.get("decision_provider") or OPENAI, "the model")
        verdict = decide(settings, TEST_TOPICS, message, format_messages(
            Channel("test-team", "test-channel"), message, [message]))
        detail = f" ({verdict.reason})" if verdict.reason else ""
        if verdict.matched and verdict.topic == TEST_TOPICS[0]:
            topics = " or ".join(f"\"{topic}\"" for topic in TEST_TOPICS)
            return (f"{provider} works. Asked whether \"{TEST_MESSAGE}\" is "
                    f"about {topics}, it picked \"{verdict.topic}\""
                    f"{detail}, as expected.")
        picked = (f"picked \"{verdict.topic}\"" if verdict.matched
                  else "matched no topic")
        return (f"{provider} answered, but {picked}{detail}; expected "
                f"\"{TEST_TOPICS[0]}\". The connection works; check the "
                "model or threshold.")

    def sign_in_url(self, settings, redirect_uri, state, code_verifier):
        return authorization_url(settings, redirect_uri, state, code_verifier)

    def complete_sign_in(self, settings, redirect_uri, code, code_verifier):
        account = self.tokens.complete(settings, redirect_uri, code,
                                       code_verifier)
        return account.get("email") or account.get("name", "")

    def signed_in_as(self) -> str:
        account = self.tokens.account()
        return account.get("email") or account.get("name", "")

    def sign_out(self) -> None:
        self.tokens.clear()

    def triggers(self):
        return [TeamsMessageTrigger(self)]


class TeamsMessageTrigger(AbstractSkillTrigger):
    TYPE = "teams-message"
    POLL_SECONDS = 30
    SKILL_FIELDS = [
        PluginField(CHANNELS_FIELD, "Teams channels", searchable=True,
                    placeholder="Search Teams channels…",
                    hint="The channels whose new messages run this skill."),
        PluginField(MODE_FIELD, "Messages",
                    choices=((ALL_MESSAGES, "All messages"),
                             (SPECIFIC_TOPICS, "Specific topics")),
                    default=ALL_MESSAGES,
                    hint="Specific topics: a decision model (Plugins page) "
                         "first checks that the message is about one of the "
                         "topics below; only then does the skill run."),
        PluginField(TOPICS_FIELD, "Topics", multiline=True,
                    visible_when=(MODE_FIELD, SPECIFIC_TOPICS),
                    placeholder="Production incidents and outages\n"
                                "Questions about the billing service",
                    hint="One per line. Describe each the way you would to a "
                         "colleague; the model matches meaning, not words."),
    ]
    DESCRIPTION = DESCRIPTION

    def __init__(self, plugin: TeamsPlugin, graph: Graph | None = None) -> None:
        self.plugin = plugin
        self._graph = graph
        # One run per channel at a time, across every skill watching it: the
        # channels with a run going, and what finished runs came back with,
        # (state key, message id, succeeded), for the next tick to apply.
        # Lives as long as the executor, like the trigger itself.
        self._lock = threading.Lock()
        self._busy: set[str] = set()
        self._outcomes: list[tuple[str, str, bool]] = []

    def _spawn(self, work) -> None:
        """Run ``work`` off the executor's loop. Tests replace this.

        Not a daemon: stopping Codee waits for a running agent rather than
        killing it mid-answer, as the executor's task agents do.
        """
        threading.Thread(target=work, name="teams-skill").start()

    def tick(self, skills: list[TriggeredSkill], run_skill: RunSkill,
             settings: dict[str, str]) -> None:
        graph = self._graph or Graph(
            lambda: self.plugin.tokens.access_token(settings))
        state = _State(self.plugin.data_dir / "state.json")
        self._apply_outcomes(state)
        ignored = _ignored(settings, self.plugin.tokens.account())
        now = datetime.now(timezone.utc)
        threads_by_channel: dict[str, list[Message]] = {}
        budget = MAX_RUNS_PER_TICK
        self._settings = settings
        self._decisions_left = MAX_DECISIONS_PER_TICK
        try:
            for skill in skills:
                channels, invalid = parse_channels(
                    skill.fields.get(CHANNELS_FIELD, ""))
                for entry in invalid:
                    log.error("%s: %r is not a Teams channel link", skill.slug,
                              entry)
                for channel in channels:
                    if channel.key not in threads_by_channel:
                        try:
                            threads_by_channel[channel.key] = \
                                graph.recent_threads(channel.team_id,
                                                     channel.channel_id)
                        except GraphError as exc:
                            log.error("%s: %s", skill.slug, exc)
                            threads_by_channel[channel.key] = []
                            continue
                    budget = self._channel(
                        skill, channel, threads_by_channel[channel.key],
                        run_skill, state, ignored, now, budget)
                    if budget <= 0:
                        return
        except SignedOut as exc:
            log.error("%s", exc)
        finally:
            state.save()

    def _channel(self, skill: TriggeredSkill, channel: Channel,
                 threads: list[Message], run_skill: RunSkill, state: "_State",
                 ignored: set[str], now: datetime, budget: int) -> int:
        """Start the skill on the channel's next new message; returns the budget left.

        A channel with a run going is left alone: its new messages wait,
        undecided, for the next tick after the run, when the thread they're
        judged against includes whatever that run answered.
        """
        key = f"{skill.slug}|{channel.key}"
        with self._lock:
            if channel.key in self._busy:
                return budget
        messages = [m for thread in threads for m in (thread, *thread.replies)]
        if not state.watching(key):
            # The first look records the channel's history, so it doesn't fire
            # the skill. History is what was posted before the skill was saved
            # with this channel: a message posted after that, but before this
            # first look (Codee paused, restarting, or a tick still busy), is
            # new and runs below.
            since = _saved_at(skill)
            state.start(key, [m for m in messages
                              if not since or _iso(m.created) <= since])
        cutoff = (now - LOOKBACK).isoformat()
        if skill.fields.get(MODE_FIELD) == SPECIFIC_TOPICS and not \
                skill.fields.get(TOPICS_FIELD, "").strip():
            log.error("%s: Specific topics mode with no topics; add some on "
                      "the skill.", skill.slug)
            return budget
        new = sorted((m for m in messages
                      if not state.seen(key, m.id) and _iso(m.created) > cutoff),
                     key=lambda m: m.created)
        for message in new:
            if {message.user.id.lower(), message.user.email.lower()} & ignored:
                state.mark(key, message)
                continue
            if budget <= 0:
                break
            messages_around = conversation(message, threads)
            topic = ""
            if skill.fields.get(MODE_FIELD) == SPECIFIC_TOPICS:
                verdict = self._decide(skill, channel, message,
                                       messages_around, state, key)
                if verdict is None:
                    # No answer this tick: tried again on the next one.
                    if self._decisions_left <= 0:
                        break
                    continue
                if not verdict.matched:
                    log.info("%s: Teams message %s is not about its topics "
                             "(%s)", skill.slug, message.id, verdict.reason)
                    state.mark(key, message)
                    continue
                topic = verdict.topic
                log.info("%s: Teams message %s is about %r (%s)", skill.slug,
                         message.id, topic, verdict.reason)
            budget -= 1
            prompt = render_prompt(skill.body, channel, message,
                                   messages_around, topic)
            # Counted as handled from now on; a failed run puts it back
            # (see _apply_outcomes).
            state.mark(key, message, keep_attempts=True)
            with self._lock:
                self._busy.add(channel.key)
            self._spawn(lambda: self._run(skill, channel, key, message,
                                          prompt, run_skill))
            # One message per channel at a time: the next waits for this run.
            break
        state.forget_before(key, cutoff)
        return budget

    def _run(self, skill: TriggeredSkill, channel: Channel, key: str,
             message: Message, prompt: str, run_skill: RunSkill) -> None:
        ok = True
        try:
            run_skill(skill, prompt)
        except RunKilled:
            # Stopped on purpose from the dashboard: done, not to be retried.
            log.info("%s for Teams message %s was killed", skill.slug,
                     message.id)
        except Exception as exc:
            ok = False
            log.warning("%s for Teams message %s failed: %s", skill.slug,
                        message.id, exc)
        finally:
            with self._lock:
                self._outcomes.append((key, message.id, ok))
                self._busy.discard(channel.key)

    def _apply_outcomes(self, state: "_State") -> None:
        """Record how the runs since the last tick went.

        A failed run's message goes back to waiting, to be tried again, up
        to MAX_ATTEMPTS tries in all. A run killed from the dashboard is done.
        """
        with self._lock:
            outcomes, self._outcomes = self._outcomes, []
        for key, message_id, ok in outcomes:
            if not state.watching(key):
                continue
            if ok:
                state.done(key, message_id)
                continue
            attempts = state.failed(key, message_id)
            if attempts < MAX_ATTEMPTS:
                state.unmark(key, message_id)
                log.warning("Teams message %s will be tried again (try %d of "
                            "%d)", message_id, attempts + 1, MAX_ATTEMPTS)
            else:
                state.done(key, message_id)

    def _decide(self, skill: TriggeredSkill, channel: Channel,
                message: Message, messages_around: list[Message],
                state: "_State", key: str):
        """Ask the decision model about ``message``; None if no answer.

        A failed call counts as a try, like a failed run: after MAX_ATTEMPTS
        the message is given up, so a broken key can't retry forever.
        """
        topics = [line.strip() for line in
                  skill.fields.get(TOPICS_FIELD, "").splitlines()
                  if line.strip()]
        if self._decisions_left <= 0:
            return None
        self._decisions_left -= 1
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "skill": skill.slug,
            "provider": dict(PROVIDERS).get(
                self._settings.get("decision_provider") or OPENAI, ""),
            "message_id": message.id,
            "reply": bool(message.reply_to),
            "sender": message.user.name,
            "email": message.user.email,
            "text": message.text,
            "link": message.web_url,
            "topics": topics,
        }
        log_file = _DecisionLog(self.plugin.data_dir / DECISION_LOG)
        try:
            verdict = decide(self._settings, topics, message,
                             format_messages(channel, message, messages_around))
        except DecisionError as exc:
            log_file.append({**record, "error": str(exc)})
            attempts = state.failed(key, message.id)
            log.warning("%s: deciding on Teams message %s failed (try %d of "
                        "%d): %s", skill.slug, message.id, attempts,
                        MAX_ATTEMPTS, exc)
            if attempts >= MAX_ATTEMPTS:
                state.mark(key, message)
            return None
        log_file.append({**record, "matched": verdict.matched,
                         "topic": verdict.topic, "reason": verdict.reason,
                         "scores": verdict.scores})
        return verdict


class _DecisionLog:
    """Decisions as JSON lines, for the Plugins page's decision log.

    Written by the executor and read by the admin; whole lines are appended,
    so a reader never sees half an entry.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
            lines = self.path.read_text(encoding="utf-8").splitlines()
            if len(lines) > LOG_MAX:
                temp = self.path.with_name(self.path.name + ".tmp")
                temp.write_text("\n".join(lines[-LOG_KEEP:]) + "\n",
                                encoding="utf-8")
                os.replace(temp, self.path)
        except OSError as exc:
            # The log is for people calibrating a skill; losing a line must
            # never cost the decision itself.
            log.warning("Could not write the decision log: %s", exc)

    def read(self, limit: int) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records = []
        for line in lines[-limit:]:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records


def _log_entry(record: dict) -> LogEntry:
    """One decision as the log dialog shows it."""
    text = record.get("text") or "(no text)"
    sender = record.get("sender") or "someone"
    first_line = text.splitlines()[0] if text.strip() else text
    title = (f"{record.get('skill', '')} · {sender}: "
             f"{first_line[:90]}{'…' if len(first_line) > 90 else ''}")
    lines = [f"Message: {text}",
             f"From: {sender}"
             + (f" <{record['email']}>" if record.get("email") else "")
             + ("  ·  reply in a thread" if record.get("reply")
                else "  ·  new message")]
    if record.get("error"):
        status = "error"
        lines.append(f"Error: {record['error']}  (tried again on the next "
                     "tick)")
    else:
        status = "ok" if record.get("matched") else "skip"
        decision = (f"matched \"{record.get('topic')}\""
                    if record.get("matched") else "no match")
        if record.get("reason"):
            decision += f" ({record['reason']})"
        lines.append(f"Decision: {decision}")
        scores = record.get("scores") or {}
        if scores:
            lines.append("Scores: " + "  ·  ".join(
                f"{name} {value:.2f}" for name, value in
                sorted(scores.items(), key=lambda item: -item[1])))
    lines.append("Topics asked: " + "; ".join(record.get("topics") or []))
    if record.get("provider"):
        lines.append(f"Model: {record['provider']}")
    if record.get("link"):
        lines.append(f"Link: {record['link']}")
    return LogEntry(time=record.get("time", ""), status=status, title=title,
                    detail="\n".join(lines))


def _name_in_link(value: str) -> str:
    """The channel name a Teams link carries in its path, "" if none."""
    parts = urlparse(value).path.split("/")
    if value.startswith("http") and "channel" in parts:
        index = parts.index("channel") + 2
        return unquote(parts[index]) if index < len(parts) else ""
    return ""


def _saved_at(skill: TriggeredSkill) -> str:
    """When the skill file last changed, as ISO time; "" if it can't be read.

    Saving the skill is when a channel starts being watched: the editor
    rewrites the file then, and so does a Git pull bringing the change in.
    """
    try:
        return datetime.fromtimestamp(skill.path.stat().st_mtime,
                                      timezone.utc).isoformat()
    except OSError:
        return ""


def _label(channel: ChannelInfo) -> str:
    return f"{channel.team_name} / {channel.channel_name}"


def _ignored(settings: dict[str, str], account: dict) -> set[str]:
    values = {line.strip().lower()
              for line in settings.get("ignore_senders", "").splitlines()
              if line.strip()}
    values |= {value.lower() for value in (account.get("id", ""),
                                            account.get("email", "")) if value}
    return values


def _iso(value: str) -> str:
    """A Graph timestamp in the form ``datetime.isoformat`` compares with."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return ""


class _State:
    """Per skill and channel: when watching started, which messages are done.

    Kept in the plugin's data directory, so a restart neither replays old
    messages nor loses new ones.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.changed = False

    def watching(self, key: str) -> bool:
        return key in self.data

    def start(self, key: str, messages: list[Message]) -> None:
        self.data[key] = {"seen": {m.id: _iso(m.created) for m in messages},
                          "attempts": {}}
        self.changed = True

    def seen(self, key: str, message_id: str) -> bool:
        return message_id in self.data[key]["seen"]

    def mark(self, key: str, message: Message,
             keep_attempts: bool = False) -> None:
        self.data[key]["seen"][message.id] = _iso(message.created)
        if not keep_attempts:
            self.data[key]["attempts"].pop(message.id, None)
        self.changed = True

    def unmark(self, key: str, message_id: str) -> None:
        """Make a message new again, for another try."""
        self.data[key]["seen"].pop(message_id, None)
        self.changed = True

    def done(self, key: str, message_id: str) -> None:
        """Stop counting tries for a message that is finished with."""
        if self.data[key]["attempts"].pop(message_id, None) is not None:
            self.changed = True

    def failed(self, key: str, message_id: str) -> int:
        attempts = self.data[key]["attempts"]
        attempts[message_id] = attempts.get(message_id, 0) + 1
        self.changed = True
        return attempts[message_id]

    def forget_before(self, key: str, cutoff: str) -> None:
        seen = self.data[key]["seen"]
        old = [message_id for message_id, created in seen.items()
               if created and created < cutoff]
        for message_id in old:
            del seen[message_id]
        self.changed = self.changed or bool(old)

    def save(self) -> None:
        if not self.changed:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(self.data, indent=2))
        os.replace(temp, self.path)
