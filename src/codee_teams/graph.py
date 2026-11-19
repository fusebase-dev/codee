"""The few Microsoft Graph calls the trigger needs, reduced to plain messages."""
import re
from dataclasses import asdict, dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Callable

import requests

from codee_teams.auth import GRAPH, TIMEOUT

# The most a single page of channel messages holds.
PAGE_SIZE = 50


@dataclass(frozen=True)
class User:
    id: str = ""
    name: str = ""
    email: str = ""


@dataclass(frozen=True)
class Message:
    id: str
    created: str
    user: User
    text: str
    # The root message's ID for a reply, "" for a top-level message.
    reply_to: str = ""
    web_url: str = ""
    replies: tuple["Message", ...] = field(default=(), compare=False)

    def as_dict(self) -> dict:
        data = asdict(self)
        data.pop("replies")
        return data


class GraphError(RuntimeError):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ChannelInfo:
    team_id: str
    team_name: str
    channel_id: str
    channel_name: str


class Graph:
    """Graph calls made with whatever token ``token()`` returns."""

    def __init__(self, token: Callable[[], str]) -> None:
        self._token = token
        self._emails: dict[str, str] = {}

    def recent_threads(self, team_id: str, channel_id: str) -> list[Message]:
        """The channel's most recently active threads, each with all replies.

        Graph sorts the threads by the last change anywhere in them, so a
        thread that just got a reply is among these however old it is.
        """
        page = self._get(f"/teams/{team_id}/channels/{channel_id}/messages",
                         {"$top": PAGE_SIZE, "$expand": "replies"})
        threads = []
        for raw in page.get("value", []):
            replies = list(raw.get("replies") or [])
            next_link = raw.get("replies@odata.nextLink")
            while next_link:
                more = self._get(next_link)
                replies.extend(more.get("value", []))
                next_link = more.get("@odata.nextLink")
            root = self._message(raw)
            if root is None:
                continue
            threads.append(Message(
                **{**root.__dict__, "replies": tuple(
                    message for reply in replies
                    if (message := self._message(reply, root.id)) is not None)}))
        return threads

    def joined_channels(self) -> list[ChannelInfo]:
        """Every channel of every team the signed-in account has joined."""
        channels = []
        for team in self._all("/me/joinedTeams", {"$select": "id,displayName"}):
            for channel in self._all(f"/teams/{team['id']}/channels",
                                     {"$select": "id,displayName"}):
                channels.append(ChannelInfo(
                    team["id"], team.get("displayName", ""),
                    channel["id"], channel.get("displayName", "")))
        return channels

    def _all(self, path: str, params: dict | None = None) -> list[dict]:
        page = self._get(path, params)
        items = list(page.get("value", []))
        while page.get("@odata.nextLink"):
            page = self._get(page["@odata.nextLink"])
            items.extend(page.get("value", []))
        return items

    def _message(self, raw: dict, reply_to: str = "") -> Message | None:
        # System events (someone joined, a tab was added) and deleted messages
        # are nothing a skill should answer.
        if raw.get("messageType") != "message" or raw.get("deletedDateTime"):
            return None
        sender = (raw.get("from") or {}).get("user") or {}
        application = (raw.get("from") or {}).get("application") or {}
        user = User(id=sender.get("id", ""),
                    name=sender.get("displayName")
                    or application.get("displayName", ""),
                    email=self._email(sender.get("id", "")))
        body = raw.get("body") or {}
        text = body.get("content", "")
        if body.get("contentType") == "html":
            text = html_to_text(text)
        subject = (raw.get("subject") or "").strip()
        if subject:
            text = f"{subject}\n{text}"
        return Message(id=raw["id"], created=raw.get("createdDateTime", ""),
                       user=user, text=text.strip(),
                       reply_to=raw.get("replyToId") or reply_to,
                       web_url=raw.get("webUrl", ""))

    def _email(self, user_id: str) -> str:
        """A user's email, looked up once per process; "" if it can't be."""
        if not user_id:
            return ""
        if user_id not in self._emails:
            try:
                profile = self._get(f"/users/{user_id}",
                                    {"$select": "mail,userPrincipalName"})
                self._emails[user_id] = (profile.get("mail")
                                         or profile.get("userPrincipalName")
                                         or "")
            except GraphError:
                self._emails[user_id] = ""
        return self._emails[user_id]

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = path if path.startswith("https://") else GRAPH + path
        response = requests.get(url, params=params, timeout=TIMEOUT, headers={
            "Authorization": f"Bearer {self._token()}"})
        if not response.ok:
            try:
                error = response.json().get("error", {}).get("message", "")
            except ValueError:
                error = ""
            raise GraphError(f"Graph {response.status_code} on {path}: "
                             f"{error or response.reason}",
                             response.status_code)
        return response.json()


class _Text(HTMLParser):
    BREAKS = {"br", "p", "div", "li", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BREAKS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    text = unescape("".join(parser.parts))
    return re.sub(r"\n{3,}", "\n\n", text).strip()
