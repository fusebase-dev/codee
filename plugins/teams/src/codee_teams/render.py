"""Which channels a skill names, and the prompt a new message turns into."""
import json
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse

from codee_teams.graph import Message

# How many top-level messages before a new one go along with it.
CONTEXT_MESSAGES = 10

PLACEHOLDERS = ("TEAMS_TEAM_ID", "TEAMS_CHANNEL_ID", "TEAMS_MESSAGE_ID",
                "TEAMS_THREAD_ID", "TEAMS_KIND", "TEAMS_MESSAGES",
                "TEAMS_MESSAGES_JSON", "TEAMS_TOPIC")


@dataclass(frozen=True)
class Channel:
    team_id: str
    channel_id: str

    @property
    def key(self) -> str:
        return f"{self.team_id}|{self.channel_id}"


def parse_channels(value: str) -> tuple[list[Channel], list[str]]:
    """The channels a skill's field lists, and the entries that aren't any.

    One per line (or comma-separated), each either a link from Teams' "Get
    link to channel" or ``<team id>/<channel id>``. Anything after `` #`` on
    a line is a comment, the label the channel search writes there.
    """
    channels: list[Channel] = []
    invalid: list[str] = []
    lines = (re.sub(r"\s+#.*$", "", line) for line in (value or "").splitlines())
    for entry in (part for line in lines for part in line.split(",")):
        entry = entry.strip()
        if not entry:
            continue
        channel = _parse_channel(entry)
        if channel is None:
            invalid.append(entry)
        elif channel not in channels:
            channels.append(channel)
    return channels, invalid


def _parse_channel(entry: str) -> Channel | None:
    if entry.startswith("http"):
        url = urlparse(entry)
        parts = url.path.split("/")
        team = parse_qs(url.query).get("groupId", [""])[0]
        if "channel" in parts and team:
            channel = unquote(parts[parts.index("channel") + 1])
            if channel:
                return Channel(team, channel)
        return None
    team, _, channel = entry.partition("/")
    if team.strip() and channel.strip().startswith("19:"):
        return Channel(team.strip(), channel.strip())
    return None


def conversation(message: Message, threads: list[Message]) -> list[Message]:
    """What goes to the skill along with ``message``, oldest first.

    A reply brings its whole thread. A top-level message brings the
    ``CONTEXT_MESSAGES`` top-level messages before it, without their replies.
    """
    if message.reply_to:
        root = next((thread for thread in threads
                     if thread.id == message.reply_to), None)
        if root is None:
            return [message]
        return [root, *sorted(root.replies, key=lambda m: m.created)]
    earlier = sorted((thread for thread in threads
                      if thread.created < message.created),
                     key=lambda m: m.created)[-CONTEXT_MESSAGES:]
    return [*earlier, message]


def render_prompt(body: str, channel: Channel, message: Message,
                  messages: list[Message], topic: str = "") -> str:
    values = {
        # The topic the decision model matched, "" in "All messages" mode.
        "TEAMS_TOPIC": topic,
        "TEAMS_TEAM_ID": channel.team_id,
        "TEAMS_CHANNEL_ID": channel.channel_id,
        "TEAMS_MESSAGE_ID": message.id,
        "TEAMS_THREAD_ID": message.reply_to or message.id,
        "TEAMS_KIND": "reply" if message.reply_to else "message",
        "TEAMS_MESSAGES": format_messages(channel, message, messages),
        "TEAMS_MESSAGES_JSON": json.dumps(
            [{**item.as_dict(), "triggered": item.id == message.id}
             for item in messages], indent=2, ensure_ascii=False),
    }
    if not any("{" + name + "}" in body for name in PLACEHOLDERS):
        return f"{body.rstrip()}\n\n{values['TEAMS_MESSAGES']}"
    for name, value in values.items():
        body = body.replace("{" + name + "}", value)
    return body


def format_messages(channel: Channel, message: Message,
                    messages: list[Message]) -> str:
    if message.reply_to:
        intro = ("A new reply was posted in a thread. The whole thread, "
                 "oldest first:")
    else:
        intro = (f"A new message was posted. It comes last, after up to "
                 f"{CONTEXT_MESSAGES} earlier messages of the channel "
                 "(replies left out), oldest first:")
    lines = [f"Microsoft Teams channel {channel.channel_id} "
             f"(team {channel.team_id}).", intro, ""]
    for item in messages:
        user = item.user.name or "unknown"
        if item.user.email:
            user += f" <{item.user.email}>"
        if item.user.id:
            user += f" (user id {item.user.id})"
        marker = "  ← triggered this run" if item.id == message.id else ""
        lines.append(f"[message {item.id}] {_date(item.created)} — "
                     f"{user}{marker}")
        lines.append(item.text or "(no text)")
        lines.append("")
    return "\n".join(lines).rstrip()


def _date(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")
                                      ).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return value
