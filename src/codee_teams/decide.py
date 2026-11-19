"""Whether a Teams message is about one of a skill's topics.

Asked of a decision model before a skill in "Specific topics" mode runs. The
question is always about the message that fired the run; the conversation
around it is passed only so the model can read that message in context.
"""
from dataclasses import dataclass, field

from urllib.parse import urlparse

import requests

from codee_teams.graph import Message

OPENAI = "openai"
TYPESAFE = "typesafe"
PROVIDERS = ((OPENAI, "OpenAI (Decisions API)"),
             (TYPESAFE, "TypeSafe (Jev)"))

OPENAI_BASE_URL = "https://api.openai.com/v1"
# The only model the Decisions API takes for now.
OPENAI_MODEL = "gpt-6-luna"
# How the triggering message is to be read. Judged on its own, a follow-up
# such as "are you sure?" in a thread about a prod outage is about nothing,
# so it is read as part of the conversation it belongs to.
IN_CONTEXT = (
    "Read the triggering message as part of the conversation it belongs to: "
    "a reply that continues a discussion about the topic counts, even when it "
    "doesn't name the topic itself (a follow-up question, a doubt about an "
    "answer, more details). A message that turns to something else does not "
    "take a topic from the messages around it.")
# The choice that means "about none of the skill's topics".
NONE = "none of these"
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_MODEL = "jev-latest"
# TypeSafe answers a yes/no question with the probability of yes; a topic
# matches from this probability up.
TYPESAFE_THRESHOLD = 0.7
TIMEOUT = 60


class DecisionError(RuntimeError):
    """The decision could not be had; the message is tried again later."""


@dataclass(frozen=True)
class Decision:
    matched: bool
    # The topic it matched, exactly as the skill lists it; "" if none.
    topic: str = ""
    reason: str = ""
    # Every option's score as the model gave it (topics, and "none of these"
    # for OpenAI), for the decision log.
    scores: dict[str, float] = field(default_factory=dict)


def decide(settings: dict[str, str], topics: list[str], message: Message,
           conversation: str) -> Decision:
    provider = settings.get("decision_provider", "").strip() or OPENAI
    if provider == TYPESAFE:
        return _typesafe(settings, topics, message, conversation)
    if provider == OPENAI:
        return _openai(settings, topics, message, conversation)
    raise DecisionError(f"Unknown decision provider {provider!r}")


def _triggering(message: Message) -> str:
    sender = message.user.name or message.user.email or "someone"
    return f"{sender} wrote:\n{message.text or '(no text)'}"


def _openai(settings, topics, message, conversation) -> Decision:
    """One choice question to OpenAI's Decisions API: which topic, or none.

    Each topic is a choice of its own, plus a "none" fallback for a message
    about none of them, as the Decisions guide advises.
    """
    key = settings.get("openai_api_key", "").strip()
    if not key:
        raise DecisionError("Fill in the OpenAI API key on the Plugins page.")
    base = (settings.get("openai_base_url", "").strip()
            or OPENAI_BASE_URL).rstrip("/")
    unique = list(dict.fromkeys(topics))
    question = {
        "type": "choice",
        "name": "topic",
        "instructions": (
            "Which of these topics is the TRIGGERING MESSAGE about? "
            f"{IN_CONTEXT} Pick {NONE!r} when it is about none of them."),
        "choices": [*({"value": topic} for topic in unique),
                    {"value": NONE,
                     "description": "About none of the topics above."}],
    }
    state = (f"TRIGGERING MESSAGE:\n{_triggering(message)}\n\n"
             f"CONVERSATION AROUND IT (context only):\n{conversation}")
    try:
        response = requests.post(
            f"{base}/decisions", timeout=TIMEOUT,
            headers=_openai_auth(base, key),
            json={"model": OPENAI_MODEL, "input": state,
                  "questions": [question]})
    except requests.RequestException as exc:
        raise DecisionError(f"OpenAI request failed: {exc}") from exc
    if not response.ok:
        raise DecisionError(f"OpenAI {response.status_code}: "
                            f"{_error(response)}")
    try:
        answer = response.json()["answers"][0]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise DecisionError(f"OpenAI gave no usable answer: {exc}") from exc
    if answer.get("type") == "refusal":
        # A refused question is an answer, not an outage: asking again would
        # only be refused again.
        return Decision(False, reason="the model declined to decide")
    choice = answer.get("choice")
    scores = {str(item.get("value")): float(item["probability"])
              for item in answer.get("probabilities") or []
              if isinstance(item.get("probability"), (int, float))}
    chance = scores.get(str(choice))
    reason = f"{chance:.2f}" if chance is not None else ""
    if choice not in unique:
        return Decision(False, reason=f"none of the topics {reason}".strip(),
                        scores=scores)
    return Decision(True, choice, reason, scores)


def _openai_auth(base: str, key: str) -> dict[str, str]:
    """The key header: Azure OpenAI documents ``api-key``, OpenAI a bearer."""
    host = urlparse(base).hostname or ""
    if host.endswith((".openai.azure.com", ".services.ai.azure.com",
                      ".cognitiveservices.azure.com")):
        return {"api-key": key}
    return {"Authorization": f"Bearer {key}"}


def _typesafe(settings, topics, message, conversation) -> Decision:
    key = settings.get("typesafe_api_key", "").strip()
    if not key:
        raise DecisionError("Fill in the TypeSafe API key on the Plugins page.")
    model = settings.get("typesafe_model", "").strip() or TYPESAFE_MODEL
    try:
        threshold = float(settings.get("typesafe_threshold", "")
                          or TYPESAFE_THRESHOLD)
    except ValueError:
        threshold = TYPESAFE_THRESHOLD
    state = (f"TRIGGERING MESSAGE:\n{_triggering(message)}\n\n"
             f"CONVERSATION AROUND IT (context only):\n{conversation}")
    # One yes/no question per topic, each about the triggering message.
    questions = {f"topic_{index}": {
        "type": "noul",
        "instructions": (f"Is the TRIGGERING MESSAGE about this topic: "
                         f"{topic}? {IN_CONTEXT}"),
    } for index, topic in enumerate(topics)}
    try:
        response = requests.post(
            TYPESAFE_URL, timeout=TIMEOUT,
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "state": state, "questions": questions})
    except requests.RequestException as exc:
        raise DecisionError(f"TypeSafe request failed: {exc}") from exc
    if not response.ok:
        raise DecisionError(f"TypeSafe {response.status_code}: "
                            f"{_error(response)}")
    try:
        answers = response.json()["answers"]
        scores = {topic: float(answers[f"topic_{index}"]["noul"])
                  for index, topic in enumerate(topics)}
    except (KeyError, TypeError, ValueError) as exc:
        raise DecisionError(f"TypeSafe gave no usable answer: {exc}") from exc
    topic, score = max(scores.items(), key=lambda item: item[1])
    summary = ", ".join(f"{name}: {value:.2f}" for name, value in scores.items())
    if score < threshold:
        return Decision(False, reason=f"below {threshold:.2f} ({summary})",
                        scores=scores)
    return Decision(True, topic, f"{score:.2f} ({summary})", scores)


def _error(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.reason or "no details"
    error = body.get("error", body) if isinstance(body, dict) else body
    if isinstance(error, dict):
        return str(error.get("message") or error.get("detail") or error)
    return str(error)
