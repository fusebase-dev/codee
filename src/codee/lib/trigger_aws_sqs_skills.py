import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from codee_main_context.context import CodeeMainContext

from codee.lib import runs_db
from codee_main_context.context import project_root, skills_dir as default_skills_dir

REPO_ROOT = project_root()
SKILLS_DIR = default_skills_dir(REPO_ROOT)

RunClaude = Callable[[str, str, str], str]


@dataclass(frozen=True)
class AwsSqsTriggeredSkill:
    key: str
    name: str
    path: Path
    queue: str
    body: str
    model: str


@dataclass(frozen=True)
class AwsSqsMessage:
    content: str
    queue_url: str
    receipt_handle: str


class AwsSqsMessageSource(Protocol):
    def receive(self, skill: AwsSqsTriggeredSkill) -> AwsSqsMessage | None:
        pass

    def delete(self, message: AwsSqsMessage) -> None:
        pass


def trigger_aws_sqs_skills(
    run_claude: RunClaude,
    *,
    skills_dir: Path = SKILLS_DIR,
    sqs_message_source: AwsSqsMessageSource | None = None,
    main_context: CodeeMainContext
) -> None:
    """Scan skills with AWS SQS trigger frontmatter and run one message per skill."""
    skills = find_aws_sqs_triggered_skills(skills_dir)
    if not skills:
        print("[aws_sqs_skills] No AWS SQS-triggered skills found.")
        return

    if sqs_message_source is None:
        sqs_message_source = _build_boto3_message_source()
        if sqs_message_source is None:
            return

    for skill in skills:
        try:
            message = sqs_message_source.receive(skill)
        except Exception as exc:
            print(
                f"[aws_sqs_skills] Failed to poll SQS trigger for {skill.name}: {exc}")
            continue

        if message is None:
            continue

        print(
            f"[aws_sqs_skills] Running {skill.name} from SQS queue {skill.queue}")
        session_id = str(uuid.uuid4())
        prompt = render_aws_sqs_prompt(skill.body, message.content)
        try:
            response = run_claude(prompt, session_id, skill.model)
            print(
                f"[aws_sqs_skills] Claude response for {skill.name} ({len(response)} chars)")
            sqs_message_source.delete(message)
            runs_db.record_run(skill.name, "aws-sqs",
                               session_id, "succeeded", message=prompt,
                               main_context=main_context)
        except Exception as exc:
            print(f"[aws_sqs_skills] Failed to run {skill.name}: {exc}")
            runs_db.record_run(skill.name, "aws-sqs", session_id, "failed",
                               error=str(exc)[:500], message=prompt,
                               main_context=main_context)


def find_aws_sqs_triggered_skills(skills_dir: Path = SKILLS_DIR) -> list[AwsSqsTriggeredSkill]:
    skills: list[AwsSqsTriggeredSkill] = []
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            metadata, body = _parse_skill_file(path.read_text())
        except OSError as exc:
            print(f"[aws_sqs_skills] Failed to read {path}: {exc}")
            continue

        trigger = metadata.get("x-codee-trigger", "").strip().lower()
        if not trigger:
            continue

        name = metadata.get(
            "name", path.parent.name).strip() or path.parent.name
        if metadata.get("disable-model-invocation", "").strip().lower() != "true":
            print(
                f"[aws_sqs_skills] ERROR: {path} declares x-codee-trigger but is missing "
                "disable-model-invocation: true; skipping."
            )
            continue

        if trigger != "aws-sqs":
            continue

        queue = metadata.get("x-codee-aws-sqs-queue", "").strip()
        if not queue:
            print(
                f"[aws_sqs_skills] ERROR: {path} declares x-codee-trigger: aws-sqs but is missing "
                "x-codee-aws-sqs-queue; skipping."
            )
            continue

        skills.append(
            AwsSqsTriggeredSkill(
                key=_skill_key(path),
                name=name,
                path=path,
                queue=queue,
                body=body.strip(),
                model=metadata.get("model", "").strip(),
            )
        )
    return skills


def _build_boto3_message_source() -> "Boto3AwsSqsMessageSource | None":
    """Build the SQS client, or return None when AWS isn't configured.

    boto3 raises (NoRegionError, missing credentials, boto3 not installed) at
    client construction. That must not abort the executor tick -- the rest of
    run_once() still has cron skills, email skills and tasks to process -- so we
    report it once per process and let SQS triggers stay idle.
    """
    global _boto3_unavailable_reported
    try:
        return Boto3AwsSqsMessageSource()
    except Exception as exc:
        if not _boto3_unavailable_reported:
            _boto3_unavailable_reported = True
            print(
                f"[aws_sqs_skills] AWS SQS client unavailable ({exc}); SQS triggers "
                "stay idle until AWS is configured (e.g. AWS_DEFAULT_REGION)."
            )
        return None


_boto3_unavailable_reported = False


class Boto3AwsSqsMessageSource:
    def __init__(self, *, visibility_timeout: int = 7200) -> None:
        import boto3

        self._sqs = boto3.client("sqs")
        self._visibility_timeout = visibility_timeout
        self._queue_urls_by_skill: dict[str, str] = {}

    def receive(self, skill: AwsSqsTriggeredSkill) -> AwsSqsMessage | None:
        queue_url = self._resolve_queue_url(skill)
        print("Receiving from SQS queue:", queue_url)
        response = self._sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=0,
            VisibilityTimeout=self._visibility_timeout,
            AttributeNames=["All"],
            MessageAttributeNames=["All"],
        )
        messages = response.get("Messages", [])
        if not messages:
            return None

        raw_message = messages[0]
        return AwsSqsMessage(
            content=raw_message.get("Body", ""),
            queue_url=queue_url,
            receipt_handle=raw_message["ReceiptHandle"],
        )

    def delete(self, message: AwsSqsMessage) -> None:
        self._sqs.delete_message(
            QueueUrl=message.queue_url, ReceiptHandle=message.receipt_handle)

    def _resolve_queue_url(self, skill: AwsSqsTriggeredSkill) -> str:
        if skill.key in self._queue_urls_by_skill:
            return self._queue_urls_by_skill[skill.key]

        if skill.queue.startswith("http://") or skill.queue.startswith("https://"):
            queue_url = skill.queue
        else:
            queue_url = self._sqs.get_queue_url(
                QueueName=skill.queue)["QueueUrl"]

        self._queue_urls_by_skill[skill.key] = queue_url
        return queue_url


def render_aws_sqs_prompt(body: str, content: str) -> str:
    if "{CONTENT}" in body:
        return body.replace("{CONTENT}", content)
    return f"{body.rstrip()}\n\n{content}"


def _parse_skill_file(contents: str) -> tuple[dict[str, str], str]:
    lines = contents.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, contents

    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break

    if end_index is None:
        return {}, contents

    metadata: dict[str, str] = {}
    for line in lines[1:end_index]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        metadata[key.strip().lower()] = _strip_quotes(value.strip())

    body = "\n".join(lines[end_index + 1:]).lstrip("\n")
    return metadata, body


def _skill_key(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
