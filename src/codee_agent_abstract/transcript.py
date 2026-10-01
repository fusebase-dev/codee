"""Reading the end of a transcript a coding agent's CLI writes as it runs.

Shared by the agents that implement ``peep``: each knows where its CLI files a
session and what the lines mean, and this knows how to read the last of them
without loading a transcript that can run to many megabytes.
"""
import json
from pathlib import Path

# Enough for the last few dozen steps of any CLI seen so far. A single line
# bigger than this (a huge tool result) is skipped rather than half-parsed.
TAIL_BYTES = 512 * 1024
# How much of one step the dashboard is sent. Thinking and replies can be long,
# and the dialog only needs enough to tell what the agent is up to.
MAX_ENTRY_CHARS = 4000


def tail_jsonl(path: Path, max_bytes: int = TAIL_BYTES) -> list[dict]:
    """The JSON objects on the last ``max_bytes`` of ``path``, in file order.

    A line cut by the start of the window is dropped, and so is one the CLI is
    still writing. [] when the file can't be read.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = max(size - max_bytes, 0)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def clip(text: str, limit: int = MAX_ENTRY_CHARS) -> str:
    """``text`` trimmed, and cut to ``limit`` characters with a marker if longer."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …"


def tool_summary(name: str, arguments: object) -> str:
    """One line naming a tool call and what it was called on.

    The field that says the most is picked when the arguments carry one (the
    command, the file, the pattern), and the whole of them otherwise.
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return clip(f"{name}: {arguments}", 600)
    if isinstance(arguments, dict):
        for key in ("command", "cmd", "file_path", "path", "pattern", "url",
                    "query", "description", "prompt"):
            value = arguments.get(key)
            if value:
                if isinstance(value, list):
                    value = " ".join(str(part) for part in value)
                return clip(f"{name}: {value}", 600)
        if not arguments:
            return name
        arguments = json.dumps(arguments, ensure_ascii=False)
    return clip(f"{name}: {arguments}", 600)
