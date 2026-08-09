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

On the first run, the command initializes Codee instructions and skills.

## Debug mode

```bash
uv run codee-start --debug       # Codee debug output
uv run codee-start --debug-all   # ...plus reflex, boto3, urllib3 and friends
```

### Skills

Codee uses skills to perform tasks. It is the same skills which are used in Claude Code, Codex and other agents.


TODO: add skill types, x-codee skills extension