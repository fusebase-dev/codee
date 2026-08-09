# Codee - a virtual co-worker

The goal of this project is to provide an assistant that will integrate into your existing environment in order to help you to solve tasks.

Codee works with Jira and Azure DevOps as tasks provider, new providers are quite easy to create, PRs are welcome.

As agents it current works with Claude Code and Github Copilot.

## Run Codee

Codee is a python package, you first need to install it.

TODO: provide install instructions

You need to create a separate repo for Codee where it will store skills, memory, temp files, config, etc.

Run Codee:

`uv run codee-start`

On first run, the command initializes Codee instructions and skills.

Both `codee-init` and `codee-start` create the `repositories/`, `temp/` and `memory/` working directories in the current directory, and add `/repositories` and `/temp` to `.gitignore` (creating it if needed). `memory/` is deliberately left tracked, because the admin UI commits and pushes memory edits. `codee-start` does this even when initialization is skipped.

Issue skills declare the statuses that trigger them in their frontmatter:

```yaml
disable-model-invocation: true
x-codee-trigger: issue
x-codee-issue-type: story
x-codee-issue-status: ['[AI] Ready for development', '[AI] In progress']
```

Status matching is case-insensitive. `x-codee-issue-type` is required and accepts exactly one value: `story` or `task`.

## Debug mode

```bash
uv run codee-start --debug       # Codee debug output
uv run codee-start --debug-all   # ...plus reflex, boto3, urllib3 and friends
```

`codee-start` runs the executor and the admin UI as subprocesses, so the flag is
carried in the environment (`CODEE_DEBUG`) and inherited by both. Setting the
variable directly works the same and is the way to debug a single process:

```bash
CODEE_DEBUG=1 uv run codee-start          # same as --debug
CODEE_DEBUG=all uv run codee-start        # same as --debug-all
CODEE_DEBUG=1 .venv/bin/python -m codee.executor   # just the executor
```

Off, the log level is `INFO`. On, it is `DEBUG` and every line also carries the
line number and function it came from:

```
14:22:07 INFO    codee.executor: Found 2 task(s).
14:22:07 DEBUG   codee_agent_claude_code.provider:44 run: cwd=/srv/app cmd=claude -p /work-on-task NIM-1 ...
```

### Adding debug messages

Ask for a module-level logger, then log with `%s` placeholders — arguments are
only formatted if the message is actually emitted, so a `log.debug` in a hot
loop costs nothing when debug is off:

```python
from codee_main_context.logging import get_logger

log = get_logger(__name__)


def poll(tasks):
    log.debug("polling returned %d task(s): %s", len(tasks), [t.key for t in tasks])
    log.info("Processing %s", tasks[0].key)
    log.warning("Retrying %s: %s", tasks[0].key, error)
```

`get_logger(__name__)` is what puts `codee.executor` in front of the message, so
messages don't need their own `[executor]` prefix. Only entry points
(`main()` functions) call `configure_logging()`, and only once — modules just
log. Levels: `debug` for per-tick detail, `info` for things worth seeing in
normal operation, `warning`/`error` for anything that failed.

Modules that still use `print` (the trigger helpers, the mail server, the task
providers) keep writing to stdout unchanged; convert them to `get_logger` as
you touch them.

## Configuration

- `CODEE_DEBUG`: `1` for debug logging, `all` to also un-mute third-party libraries.
- `CODEE_SESSION_VIEWER_URL`: optional session URL template, such as `https://sessions.example.com/{session_id}`.
- `CODEE_ALLOWED_SENDER_DOMAINS`: comma-separated domains allowed to invoke email-triggered skills. Email triggers reject all senders when this is unset.

Provider credentials are configured in the admin UI and stored locally in `.codee/settings.json`. The `.codee` directory is excluded from version control.