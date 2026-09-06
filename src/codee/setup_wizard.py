"""The interactive first-run setup behind ``codee-agent init``.

Takes an empty directory to a Codee project that is ready to poll: the files
`codee-init` lays down, a coding agent, and a configured tasks provider.

Only part of that can be finished in a terminal. Jira authenticates with an API
token the user pastes in, so every field it needs can be typed here and checked
before anything is written. Azure DevOps authenticates through an Entra ID
authorization-code flow whose redirect URI is a route on the admin UI itself
(``codee_tasks_azure_devops.oauth.CALLBACK_PATH``) — there is no terminal
equivalent of that round trip, so the wizard collects nothing, starts the admin
UI and hands the user to the browser instead. Which providers fall on which
side is :data:`TERMINAL_SETUP`; a new provider joins it as soon as its
credentials are just values.
"""
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
import webbrowser
from getpass import getpass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from codee_main_context.context import (
    CodingAgent,
    CredentialField,
    TASKS_PROVIDER_FIELDS,
    TasksProvider,
)

from codee.coding_agents import CODING_AGENTS, installed_coding_agents
from codee.tasks_providers import TASKS_PROVIDERS

if TYPE_CHECKING:  # AdminService pulls in the whole admin stack; see `run`.
    from codee.admin_service import AdminService


# Providers whose whole configuration is values the user can type. Everything
# else is finished in the admin UI — see the module docstring.
TERMINAL_SETUP = frozenset({TasksProvider.JIRA})

# How long to wait for the admin UI to answer on its port before opening the
# browser anyway. The first start compiles the frontend and installs its node
# modules, which on a cold machine is minutes rather than seconds.
ADMIN_START_TIMEOUT = 600
ADMIN_POLL_INTERVAL = 1.0

SETTINGS_ROUTE = "/settings"

# What the agent-driven MCP check costs, said before it is offered: it is the
# one step of setup that reaches out and changes something in the user's
# tracker.
MCP_CHECK_WARNING = (
    "This runs the coding agent against your tracker through MCP: it creates a "
    "task and then closes it again, and it costs one agent run.")


# --- Output ------------------------------------------------------------------
#
# Everything the wizard prints goes through these, so the whole run lines up on
# one left margin and wraps at one width. Styling is applied at print time
# rather than baked into the strings, which is what lets it disappear when
# there is no terminal to style for.

INDENT = "  "
# Wide terminals get a rule that stops rather than one that runs to the edge:
# past this the eye stops reading it as an underline for the title.
MAX_WIDTH = 72
MIN_RULE = 4

BOLD = "1"
DIM = "2"
GREEN = "32"
RED = "31"

PASS_MARK = "\u2714"
FAIL_MARK = "\u2718"


def _color_enabled() -> bool:
    """Whether to emit ANSI codes at all.

    Off when the output is piped or redirected, so escape sequences never end
    up in a log file, and off when NO_COLOR is set, which is the convention
    users reach for when they mean it.
    """
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _style(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _color_enabled() else text


def _width() -> int:
    """The column to wrap and rule at, read fresh so a resized window applies."""
    return min(shutil.get_terminal_size((80, 24)).columns, MAX_WIDTH)


def heading(title: str) -> None:
    """A section title, underscored by a rule running out to the margin."""
    fill = max(MIN_RULE, _width() - len(INDENT) - len(title) - 1)
    print(f"\n{INDENT}{_style(BOLD, title)} {_style(DIM, '\u2500' * fill)}\n")


def line(text: str = "") -> None:
    """One line of body text at the wizard's margin. No argument prints a gap."""
    print(f"{INDENT}{text}" if text else "")


def wrapped(text: str, indent: str = INDENT, style: str = "") -> None:
    """Body text folded to the terminal width.

    URLs and paths are never broken mid-token: a wrapped one cannot be
    double-clicked or copied, and these messages are mostly made of them.

    Styling is applied per line after folding rather than to the string going
    in, because textwrap counts an escape sequence as visible characters and
    would wrap the text short by however long the codes are.
    """
    folded = textwrap.fill(text, width=_width(), initial_indent=indent,
                           subsequent_indent=indent, break_long_words=False,
                           break_on_hyphens=False)
    if not style:
        print(folded)
        return
    for row in folded.splitlines():
        print(f"{indent}{_style(style, row[len(indent):])}")


def ask(label: str, secret: bool = False) -> str:
    """Read one answer at the wizard's margin."""
    return (getpass if secret else input)(f"{INDENT}{label}").strip()


def report(name: str, ok: bool, message: str) -> None:
    """One check's outcome: a marked title, then its message underneath.

    Two lines rather than one because these messages are long — a provider
    refusing a token answers with a whole request URL — and a wrapped tail
    sitting under its own heading stays readable where a run-on line does not.
    """
    mark = _style(GREEN, PASS_MARK) if ok else _style(RED, FAIL_MARK)
    print(f"{INDENT}{mark} {_style(BOLD, name)}")
    wrapped(message, indent=INDENT + "  ")


def prompt(label: str, default: str = "", secret: bool = False,
           required: bool = True) -> str:
    """Ask for one value, re-asking until a required one is given.

    An empty answer takes the default, which is how re-running the wizard over
    a configured project keeps what is already there. A secret with a stored
    value is never echoed back as the default — the prompt says it is being
    kept instead, so a token cannot be read off the screen.
    """
    if secret and default:
        suffix = " [leave blank to keep the stored value]"
    elif default:
        suffix = f" [{default}]"
    else:
        suffix = ""

    while True:
        answer = ask(f"{label}{suffix}: ", secret=secret)
        if answer:
            return answer
        if default or not required:
            return default
        line("  Required.")


def choose(question: str, options: list[tuple[str, str]]) -> str:
    """Ask the user to pick one of ``options``, given as (value, label) pairs.

    The first option is the default, so pressing enter is always a safe answer.
    """
    line(question)
    for index, (_, label) in enumerate(options, start=1):
        line(f"  {index}) {_style(BOLD, label)}")
    while True:
        answer = ask(f"Choice [1-{len(options)}, default 1]: ")
        if not answer:
            return options[0][0]
        try:
            index = int(answer)
        except ValueError:
            index = 0
        if 1 <= index <= len(options):
            return options[index - 1][0]
        line(f"  Enter a number between 1 and {len(options)}.")


def choose_coding_agent() -> CodingAgent:
    """Which agent to drive, asked only when the answer isn't obvious.

    Detection is by CLI on PATH. One agent installed is not worth a question —
    it is said out loud and used. Two are a real choice. None still has to be
    answered, because settings need an agent either way, so the full list is
    offered with a note that nothing was found; installing it afterwards is
    enough, and no setting has to change.
    """
    heading("Coding agent")
    installed = installed_coding_agents()
    labels = {agent: CODING_AGENTS[agent].DISPLAY_NAME for agent in CODING_AGENTS}

    if len(installed) == 1:
        agent = installed[0]
        wrapped(f"Detected {labels[agent]} "
                f"(`{CODING_AGENTS[agent].CLI_COMMAND}` on PATH). Using it.")
        return agent

    if installed:
        found = ", ".join(labels[agent] for agent in installed)
        wrapped(f"Detected {len(installed)} coding agents: {found}.")
        options = [(agent.value, labels[agent]) for agent in installed]
    else:
        commands = ", ".join(f"`{CODING_AGENTS[agent].CLI_COMMAND}`"
                             for agent in CODING_AGENTS)
        wrapped(f"No coding agent CLI found on PATH ({commands}). Pick the one "
                "you will install — Codee needs it only when it runs a task.")
        options = [(agent.value, labels[agent]) for agent in CODING_AGENTS]

    line()
    return CodingAgent(choose("Which coding agent should Codee use?", options))


def choose_tasks_provider() -> TasksProvider:
    heading("Tasks provider")
    options = [(provider.value, TASKS_PROVIDERS[provider].DISPLAY_NAME)
               for provider in TASKS_PROVIDERS]
    return TasksProvider(
        choose("Where does Codee pick up its work?", options))


def normalize_base_url(value: str) -> str:
    """Make a pasted host usable as an API base URL.

    Users paste what the browser shows them, which is a bare host as often as
    a URL. A missing scheme would otherwise reach the provider as a relative
    URL and fail with a message about neither the field nor the fix.
    """
    value = value.strip().rstrip("/")
    if value and not urlparse(value).scheme:
        value = f"https://{value}"
    return value


def collect_credentials(provider: TasksProvider,
                        stored: dict[str, str]) -> dict[str, str]:
    """Ask for every field the provider declares, keeping what is already set.

    A field carrying a hint gets it printed above its prompt. Above rather than
    beside: these say where to find a value the user may have to go and look
    up, which is no use once they are already typing.
    """
    credentials = dict(stored)
    for index, field in enumerate(TASKS_PROVIDER_FIELDS[provider]):
        if field.hint:
            if index:
                line()
            wrapped(field.hint, style=DIM)
        value = prompt(field.label, default=stored.get(field.key, ""),
                       secret=field.secret)
        credentials[field.key] = (normalize_base_url(value)
                                  if _is_url_field(field) else value)
    return credentials


def _is_url_field(field: CredentialField) -> bool:
    return field.key.endswith("_url")


def prompt_max_parallel_agents(default: int) -> int:
    """How many agent runs may be in flight at once."""
    while True:
        answer = prompt("How many tasks may Codee work on in parallel?",
                        default=str(default))
        try:
            value = int(answer)
        except ValueError:
            line("  Enter a whole number.")
            continue
        if value >= 1:
            return value
        line("  At least 1.")


def confirm(question: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = ask(f"{question} {suffix} ").lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _report(check: dict) -> None:
    report(check["name"], check["ok"], check["message"])


def setup_in_terminal(service: "AdminService", provider: TasksProvider,
                      coding_agent: CodingAgent) -> bool:
    """Configure a provider whose credentials are just values, and prove it works.

    The order matters. Settings are saved first because the checks at the end
    run the coding agent named in them; the MCP server is written next, because
    one of those checks drives it and looks for it in `.mcp.json`; and only then
    is anything verified. A failing check is reported rather than rolled back —
    what was saved is what the user typed, and the settings page is where they
    will fix it.
    """
    name = TASKS_PROVIDERS[provider].DISPLAY_NAME
    settings = service.load_settings()
    stored = settings.credentials.get(provider.value, {})

    heading(f"{name} credentials")
    credentials = collect_credentials(provider, stored)
    line()
    max_parallel = prompt_max_parallel_agents(settings.max_parallel_agents)

    service.save_settings(
        tasks_provider=provider.value,
        coding_agent=coding_agent.value,
        max_parallel_agents=max_parallel,
        credentials=credentials,
        # Carried rather than defaulted: the wizard never asks for a custom
        # JQL/WIQL clause, and saving "" here would silently drop one that a
        # previous run through the Settings page had configured.
        task_filter=settings.task_filters.get(provider.value, ""),
    )
    line()
    wrapped(f"Saved to {service.data_dir / 'settings.json'}")

    heading("Checks")
    ok, message = service.setup_tasks_mcp(provider.value, credentials)
    report("MCP server", ok, message)

    checks = service.verify_tasks_connection(provider.value, credentials)
    connection = next(checks, None)
    if connection is None:
        return False
    line()
    _report(connection)
    if not connection["ok"]:
        # The second check drives a coding agent against the same credentials
        # this one just refused, so there is nothing left for it to prove.
        line()
        wrapped("Fix the credentials on the Settings page, or run "
                "`codee-agent init` again.")
        return False

    line()
    wrapped(MCP_CHECK_WARNING)
    line()
    if not confirm("Run the MCP check now?"):
        line("  Skipped. The Settings page can run it later.")
        return True
    line()
    for check in checks:
        _report(check)
    return True


def wait_for_admin_ui(port: int, process: subprocess.Popen) -> bool:
    """Block until the admin UI accepts connections, or until it gives up.

    Polled on the port rather than waited out on a timer: the first start
    compiles the frontend, which takes anywhere from seconds to minutes, and
    opening the browser before that finishes shows a connection error the user
    has no reason to read as "still starting".
    """
    deadline = time.monotonic() + ADMIN_START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(ADMIN_POLL_INTERVAL)
    return False


def hand_off_to_admin_ui(service: "AdminService", provider: TasksProvider,
                         coding_agent: CodingAgent, port: int) -> int:
    """Start Codee and open the browser on the page that finishes this provider.

    Everything answerable here is saved first, so the browser opens on a
    settings page that already knows which provider and which agent were
    chosen and only asks for what it alone can collect.
    """
    name = TASKS_PROVIDERS[provider].DISPLAY_NAME
    settings = service.load_settings()

    heading(f"Finishing {name} setup in the browser")
    max_parallel = prompt_max_parallel_agents(settings.max_parallel_agents)
    service.save_settings(
        tasks_provider=provider.value,
        coding_agent=coding_agent.value,
        max_parallel_agents=max_parallel,
        credentials=settings.credentials.get(provider.value, {}),
        task_filter=settings.task_filters.get(provider.value, ""),
    )

    line()
    wrapped(f"{name} signs in through a browser consent flow whose redirect "
            f"URI is served by Codee itself, so the rest of the setup happens "
            f"on the Settings page.")
    line()
    wrapped("It will ask you for an Entra ID app registration — the page "
            "itself explains how to create one. Have these ready:")
    line()
    for field in TASKS_PROVIDER_FIELDS[provider]:
        line(f"  \u00b7 {field.label}")

    url = f"http://localhost:{port}{SETTINGS_ROUTE}"
    line()
    wrapped("Starting Codee. This can take a few minutes the first time, "
            "while the admin UI builds.")
    line()
    line(f"  Opening  {_style(BOLD, url)}")
    line(f"  Stop it  Ctrl-C, then `uv run codee-start` from now on")
    line()

    process = subprocess.Popen(
        [sys.executable, "-m", "codee.start_cli", "--port", str(port)])
    try:
        if wait_for_admin_ui(port, process):
            webbrowser.open(url)
        else:
            line()
            wrapped(f"Codee did not come up on port {port}. "
                    f"See the output above.")
        return process.wait()
    except KeyboardInterrupt:
        return 130
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def run(destination: Path, port: int) -> int:
    """Ask everything, configure what can be configured, hand off the rest."""
    # Imported here rather than at module scope: it pulls in the whole admin
    # stack, which the scaffolding half of `codee-agent init` has no use for.
    from codee.admin_service import AdminService

    service = AdminService()
    coding_agent = choose_coding_agent()
    provider = choose_tasks_provider()

    if provider in TERMINAL_SETUP:
        configured = setup_in_terminal(service, provider, coding_agent)
        heading("Done")
        if configured:
            wrapped("Codee is configured. Start it with `uv run codee-start`.")
        else:
            wrapped("Codee is set up but not connected yet. Run "
                    "`uv run codee-start` and finish on the Settings page.")
        return 0

    return hand_off_to_admin_ui(service, provider, coding_agent, port)


def describe_manual_setup(port: int) -> None:
    """What to do when there is no terminal to ask questions on."""
    heading("Next")
    wrapped("Not running interactively, so nothing was configured. Run "
            "`uv run codee-start` and open "
            f"http://localhost:{port}{SETTINGS_ROUTE} to choose a coding "
            "agent and a tasks provider.")
