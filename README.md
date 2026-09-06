# Codee - a virtual co-worker

The goal of this project is to provide an assistant that will integrate into your existing environment in order to help you to work on tasks (development, code review, testing).

Codee works with Jira and Azure DevOps as tasks providers, new providers are quite easy to create, PRs are welcome.

As agents it currently works with Claude Code and Github Copilot.

## Run Codee

Codee lives in its own directory, where it keeps skills, memory, temp files and config.
Create one and initialize it:

```bash
mkdir my-codee && cd my-codee
uvx codee-agent init
```

`init` scaffolds the project, detects the coding agents installed on the machine and asks
which one to use, then walks you through connecting Jira (entirely in the terminal) or
Azure DevOps (which finishes in the browser, since it needs an Entra ID consent flow).

Afterwards, run Codee with:

```bash
uv run codee-start
```

## Debug mode

```bash
uv run codee-start --debug       # Codee debug output
uv run codee-start --debug-all   # ...plus reflex, boto3, urllib3 and friends
```

### Skills

Codee uses skills to perform tasks. It is the same skills which are used in Claude Code, Codex and other agents.
